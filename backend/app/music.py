from pathlib import Path, PurePosixPath
import hashlib
import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.auth import AuthenticatedUsername
from app.vault_libraries import AUDIO_EXTENSIONS, scan_vault_library
from app.vault_master import CataloguedAsset, VaultMasterStore, asset_is_editable_by, get_vault_master_store
from app.vault_master_jellyfin import run_jellyfin_music_import


router = APIRouter(prefix="/api/music", tags=["music"])
MUSIC_VAULT_ROOT = PurePosixPath("/vault/Music")


def get_music_library_path() -> Path:
    return Path(os.getenv("PV_MUSIC_PATH", "/media/music"))


MusicLibraryPath = Annotated[Path, Depends(get_music_library_path)]
MusicCatalogue = Annotated[VaultMasterStore, Depends(get_vault_master_store)]


class MusicTrack(BaseModel):
    id: str
    asset_id: str
    title: str
    artist: str
    album: str
    album_artist: str | None
    album_folder: str
    genre: str | None
    genres: list[str]
    track_number: int | None
    disc_number: int | None
    release_year: int | None
    overview: str | None
    duration_seconds: float | None
    artwork_url: str | None
    lyrics_available: bool
    enrichment_status: str
    playback_url: str


class MusicRefreshResult(BaseModel):
    imported: int
    failed: int


class MusicLyrics(BaseModel):
    text: str
    lines: list[dict[str, object]]
    metadata: dict[str, object]


def _text(value: object) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value is not None and str(value).strip() else None


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)(?:\s*/\s*\d+)?\s*", value)
        return int(match.group(1)) if match else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sortable_music_number(
    value: int | None,
    *,
    missing: int,
    missing_is_known: bool = False,
) -> tuple[int, int]:
    """Return a numeric sort key for values such as ``01`` and ``3/13``."""
    if value is None:
        return (missing, 0 if missing_is_known else 1)
    return (value, 0)


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return [_text(value)] if _text(value) else []
    return [text for item in value if (text := _text(item))]


def _vault_path(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve())
    return str(MUSIC_VAULT_ROOT.joinpath(*relative.parts))


def _track_id(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]


def _artwork_url(asset: CataloguedAsset) -> str | None:
    artwork = asset.imported_metadata.get("artwork")
    owned = artwork.get("owned") if isinstance(artwork, dict) else None
    record = owned.get("primary") if isinstance(owned, dict) else None
    if not isinstance(record, dict):
        return None
    if record.get("storage_key") != f"artwork/{asset.id}/primary":
        return None
    if not str(record.get("mime_type", "")).casefold().startswith("image/"):
        return None
    return f"/api/vault-master/assets/{asset.id}/artwork/primary"


def _to_track(path: Path, root: Path, asset: CataloguedAsset) -> MusicTrack:
    metadata = asset.effective_metadata
    artist = _text(metadata.get("artist")) or "Unknown artist"
    album = _text(metadata.get("album")) or "Unknown album"
    genres = _texts(metadata.get("genres") or metadata.get("genre"))
    lyrics = metadata.get("lyrics")
    provider = asset.imported_metadata.get("provider")
    identified = (
        (
            isinstance(provider, dict)
            and provider.get("name") in {"jellyfin", "musicbrainz"}
        )
        or isinstance(asset.imported_metadata.get("musicbrainz"), dict)
    ) and not artist.casefold().startswith("unknown") and not album.casefold().startswith(
        "unknown"
    )
    track_id = _track_id(path, root)
    return MusicTrack(
        id=track_id,
        asset_id=str(asset.id),
        title=_text(metadata.get("display_title")) or asset.display_title,
        artist=artist,
        album=album,
        album_artist=_text(metadata.get("album_artist")),
        album_folder=path.resolve().relative_to(root.resolve()).parent.as_posix(),
        genre=genres[0] if genres else None,
        genres=genres,
        track_number=_integer(metadata.get("track_number")),
        disc_number=_integer(metadata.get("disc_number")),
        release_year=_integer(metadata.get("release_year")),
        overview=_text(metadata.get("overview")),
        duration_seconds=_number(metadata.get("duration_seconds")),
        artwork_url=_artwork_url(asset),
        lyrics_available=(
            isinstance(lyrics, dict)
            and isinstance(lyrics.get("text"), str)
            and bool(lyrics["text"].strip())
        ),
        enrichment_status="identified" if identified else "needs_review",
        playback_url=f"/api/music/{track_id}/stream",
    )


def discover_music(root: Path) -> list[tuple[Path, str]]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise OSError("Music storage is unavailable")
    return [
        (entry.path, _vault_path(entry.path, resolved))
        for entry in scan_vault_library(resolved, allowed_extensions=AUDIO_EXTENSIONS)
    ]


@router.get("", response_model=list[MusicTrack])
def list_music(
    response: Response,
    username: AuthenticatedUsername,
    library_path: MusicLibraryPath,
    store: MusicCatalogue,
) -> list[MusicTrack]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        discovered = discover_music(library_path)
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Music storage is unavailable",
        ) from None
    assets = store.get_visible_catalogued_assets(
        [vault_path for _, vault_path in discovered], username
    )
    tracks = [
        _to_track(path, library_path, asset)
        for path, vault_path in discovered
        if (asset := assets.get(vault_path)) is not None
    ]
    return sorted(
        tracks,
        key=lambda track: (
            (track.album_artist or track.artist).casefold(),
            track.album.casefold(),
            _sortable_music_number(
                track.disc_number,
                missing=1,
                missing_is_known=True,
            ),
            _sortable_music_number(track.track_number, missing=2**31 - 1),
            track.title.casefold(),
        ),
    )


@router.post("/refresh", response_model=MusicRefreshResult)
def refresh_music_metadata(
    response: Response,
    username: AuthenticatedUsername,
    store: MusicCatalogue,
) -> MusicRefreshResult:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        imported, failed = run_jellyfin_music_import(store)
    except (OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Music information provider is unavailable",
        ) from None
    return MusicRefreshResult(imported=imported, failed=failed)


@router.get("/{track_id}/lyrics", response_model=MusicLyrics)
def get_music_lyrics(
    track_id: str,
    response: Response,
    username: AuthenticatedUsername,
    library_path: MusicLibraryPath,
    store: MusicCatalogue,
) -> MusicLyrics:
    response.headers["Cache-Control"] = "private, no-store"
    _, asset = resolve_visible_track(track_id, username, library_path, store)
    if not asset_is_editable_by(asset, username):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lyrics are not available",
        )
    lyrics = asset.effective_metadata.get("lyrics")
    if not isinstance(lyrics, dict) or not isinstance(lyrics.get("text"), str):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lyrics are not available",
        )
    lines = lyrics.get("lines")
    metadata = lyrics.get("metadata")
    return MusicLyrics(
        text=lyrics["text"],
        lines=(
            [line for line in lines if isinstance(line, dict)]
            if isinstance(lines, list)
            else []
        ),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def resolve_visible_track(
    track_id: str,
    username: str,
    library_path: Path,
    store: VaultMasterStore,
) -> tuple[Path, CataloguedAsset]:
    for path, vault_path in discover_music(library_path):
        if _track_id(path, library_path) != track_id:
            continue
        asset = store.get_visible_catalogued_assets([vault_path], username).get(vault_path)
        if asset is not None:
            return path, asset
        break
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
