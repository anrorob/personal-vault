from pathlib import Path, PurePosixPath
import hashlib
import os
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUsername, authenticated_user_id
from app.vault_libraries import AUDIO_EXTENSIONS, scan_vault_library, _managed_section_entries
from app.vault_master import CataloguedAsset, VaultMasterStore, get_vault_master_store, asset_is_editable_by
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
    album_group_id: str | None = None
    album_position: int | None = None
    album_order_state: str | None = None
    album_member_count: int | None = None
    owner_user_id: str | None = None
    can_edit: bool = False
    identity_conflicts: list[str] = Field(default_factory=list)
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


def _to_track(
    path: Path, root: Path, asset: CataloguedAsset, album_group=None, *, can_edit: bool = False
) -> MusicTrack:
    metadata = asset.effective_metadata
    artist = _text(metadata.get("artist")) or "Unknown artist"
    album = _text(metadata.get("album")) or ""
    if album_group is not None:
        artist, album = album_group.artist_name, album_group.album_title
    genres = _texts(metadata.get("genres") or metadata.get("genre"))
    lyrics = metadata.get("lyrics")
    provider = asset.imported_metadata.get("provider")
    suggestions = asset.imported_metadata.get("music_identity_suggestion")
    # A playback-provider marker is not album identification. In particular,
    # declared group labels must not fill missing provider identity evidence.
    identity = suggestions if album_group is not None and isinstance(suggestions, dict) else asset.imported_metadata
    provider_identity = all(
        (value := _text(identity.get(key))) and not value.casefold().startswith("unknown")
        for key in ("artist", "album")
    )
    musicbrainz = asset.imported_metadata.get("musicbrainz")
    identified = (
        (
            isinstance(provider, dict)
            and provider.get("name") == "jellyfin"
            and provider_identity
        )
        or (isinstance(musicbrainz, dict) and bool(musicbrainz.get("release_id")))
    )
    conflicts = []
    if album_group is not None and isinstance(suggestions, dict):
        for key, declared in (("artist", album_group.artist_name), ("album", album_group.album_title)):
            suggested = _text(suggestions.get(key))
            if suggested and suggested.casefold() != declared.casefold():
                conflicts.append(f"Provider {key}: {suggested}. Your album identity remains {declared}.")
    track_id = _track_id(path, root)
    return MusicTrack(
        id=track_id,
        can_edit=can_edit,
        asset_id=str(asset.id),
        title=_text(metadata.get("display_title")) or asset.display_title,
        artist=artist,
        album=album,
        album_artist=album_group.artist_name if album_group else _text(metadata.get("album_artist")),
        album_group_id=str(album_group.id) if album_group else None,
        owner_user_id=str(asset.owner_user_id) if asset.owner_user_id else None,
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
        identity_conflicts=conflicts,
        enrichment_status="identified" if identified and not conflicts else "needs_review",
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
        _to_track(path, library_path, asset, store.get_asset_music_album(asset.id), can_edit=asset_is_editable_by(asset, username))
        for path, vault_path in discovered
        if (asset := assets.get(vault_path)) is not None
        and "storage_placement" not in asset.metadata
    ]
    tracks.extend(
        _to_track(library_path / asset.vault_path.removeprefix("/vault/Music/"), library_path, asset, store.get_asset_music_album(asset.id), can_edit=asset_is_editable_by(asset, username))
        for _, asset in _managed_section_entries("Music", store, username)
        if asset.mime_type.startswith("audio/")
    )
    # Only a complete visible membership can be advertised as album-playable.
    from uuid import UUID
    for group_id in {track.album_group_id for track in tracks if track.album_group_id}:
        order = store.get_music_album_order(UUID(group_id))
        members = [track for track in tracks if track.album_group_id == group_id]
        complete = order['state'] == 'ready' and {track.asset_id for track in members} == set(map(str, order['asset_ids'])) and len(members) == order['member_count']
        positions = {str(asset_id): index for index, asset_id in enumerate(order['asset_ids'], 1)} if complete else {}
        for track in members:
            track.album_position = positions.get(track.asset_id)
            track.album_order_state = 'ready' if complete else 'unresolved'
            track.album_member_count = order['member_count']
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


class MusicAlbumDetail(BaseModel):
    id: UUID
    title: str
    artist: str
    can_edit: bool
    order_state: str
    tracks: list[MusicTrack]
    artwork_url: str | None
    release_year: int | None


@router.get("/albums/{album_id}", response_model=MusicAlbumDetail)
def get_music_album_detail(
    album_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    library_path: MusicLibraryPath,
    store: MusicCatalogue,
) -> MusicAlbumDetail:
    response.headers["Cache-Control"] = "private, no-store"
    owner_id = authenticated_user_id(username)
    album = store.get_music_album(album_id)
    if album is None:
        raise HTTPException(404, "Album not found")
    # Reuse catalogue visibility and complete-membership checks, never group IDs
    # or owner labels as permission to expose otherwise inaccessible tracks.
    tracks = [
        track for track in list_music(response, username, library_path, store)
        if track.album_group_id == str(album_id)
    ]
    if not tracks and album.owner_user_id != owner_id:
        raise HTTPException(404, "Album not found")
    ready = bool(tracks) and all(
        track.album_order_state == "ready" for track in tracks
    )
    if ready:
        tracks.sort(key=lambda track: track.album_position)
    # Unresolved members have no playback order. Do not inherit list_music's
    # presentation/title sorting as an apparent album sequence.
    else:
        by_id = {track.asset_id: track for track in tracks}
        tracks = [
            by_id[str(asset_id)]
            for asset_id in store.music_album_asset_ids(album_id)
            if str(asset_id) in by_id
        ]
    years = {track.release_year for track in tracks if track.release_year is not None}
    return MusicAlbumDetail(
        id=album.id,
        title=album.album_title,
        artist=album.artist_name,
        can_edit=album.owner_user_id == owner_id,
        order_state="ready" if ready else "unresolved",
        tracks=tracks,
        artwork_url=next(
            (track.artwork_url for track in tracks if track.artwork_url), None
        ),
        release_year=next(iter(years)) if len(years) == 1 else None,
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
    for entry, asset in _managed_section_entries("Music", store, username):
        logical_path = library_path / asset.vault_path.removeprefix("/vault/Music/")
        if asset.mime_type.startswith("audio/") and _track_id(logical_path, library_path) == track_id:
            return entry.path, asset
    for path, vault_path in discover_music(library_path):
        if _track_id(path, library_path) != track_id:
            continue
        asset = store.get_visible_catalogued_assets([vault_path], username).get(vault_path)
        if asset is not None and "storage_placement" not in asset.metadata:
            return path, asset
        break
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
