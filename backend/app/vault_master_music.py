from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
import time
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUsername
from app.config import get_metadata_storage_root
from app.vault_master import CataloguedAsset, VaultMasterStore, get_vault_master_store


router = APIRouter(prefix="/api/vault-master/music/albums", tags=["vault master music"])
MUSIC_VAULT_ROOT = PurePosixPath("/vault/Music")
MUSICBRAINZ_BASE_URL = "https://musicbrainz.org"
COVER_ART_BASE_URL = "https://coverartarchive.org"
DEFAULT_ARTWORK_MAX_BYTES = 25 * 1024 * 1024
MBID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
TRACK_NUMBER_PATTERN = re.compile(r"^\s*(\d{1,3})(?:\D|$)")


class MusicMetadataProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderTrack:
    disc_number: int
    track_number: int
    number: str
    title: str
    artist: str
    recording_id: str | None
    duration_seconds: float | None


@dataclass(frozen=True)
class ProviderRelease:
    release_id: str
    release_group_id: str | None
    title: str
    artist: str
    date: str | None
    country: str | None
    genres: tuple[str, ...]
    tracks: tuple[ProviderTrack, ...]
    cover_art_available: bool


def _artist_credit(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for credit in value:
        if not isinstance(credit, dict):
            continue
        name = credit.get("name")
        artist = credit.get("artist")
        if not isinstance(name, str) and isinstance(artist, dict):
            name = artist.get("name")
        if isinstance(name, str):
            parts.append(name)
        join_phrase = credit.get("joinphrase")
        if isinstance(join_phrase, str):
            parts.append(join_phrase)
    return "".join(parts).strip()


def _lucene_phrase(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _lucene_terms(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


class MusicBrainzClient:
    def __init__(
        self,
        *,
        base_url: str = MUSICBRAINZ_BASE_URL,
        cover_art_base_url: str = COVER_ART_BASE_URL,
        user_agent: str = "PersonalVault/0.1 (private local archive)",
        timeout_seconds: float = 15,
        minimum_interval_seconds: float = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cover_art_base_url = cover_art_base_url.rstrip("/")
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        self._minimum_interval_seconds = minimum_interval_seconds
        self._request_lock = threading.Lock()
        self._last_request = 0.0

    def _request_json(self, url: str) -> dict[str, object]:
        with self._request_lock:
            wait = self._minimum_interval_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self._user_agent,
                },
            )
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    payload = json.load(response)
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                raise MusicMetadataProviderError(
                    "The online music catalogue is unavailable"
                ) from error
            finally:
                self._last_request = time.monotonic()
        if not isinstance(payload, dict):
            raise MusicMetadataProviderError(
                "The online music catalogue returned invalid data"
            )
        return payload

    def search_releases(self, artist: str, album: str, limit: int = 5) -> list[dict[str, object]]:
        query = (
            f'artist:"{_lucene_phrase(artist)}" '
            f"AND release:({_lucene_terms(album)})"
        )
        url = f"{self._base_url}/ws/2/release/?{urlencode({'query': query, 'fmt': 'json', 'limit': limit})}"
        payload = self._request_json(url)
        releases = payload.get("releases")
        if not isinstance(releases, list):
            return []
        results: list[dict[str, object]] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            release_id = release.get("id")
            title = release.get("title")
            if not isinstance(release_id, str) or not isinstance(title, str):
                continue
            media = release.get("media")
            track_count = sum(
                int(medium.get("track-count", 0))
                for medium in media
                if isinstance(medium, dict)
                and isinstance(medium.get("track-count", 0), int)
            ) if isinstance(media, list) else 0
            cover_archive = release.get("cover-art-archive")
            results.append(
                {
                    "release_id": release_id,
                    "title": title,
                    "artist": _artist_credit(release.get("artist-credit")),
                    "date": release.get("date") if isinstance(release.get("date"), str) else None,
                    "country": release.get("country") if isinstance(release.get("country"), str) else None,
                    "track_count": track_count,
                    "score": (
                        int(release["score"])
                        if str(release.get("score", "")).isdigit()
                        else 0
                    ),
                    "cover_art_available": bool(
                        isinstance(cover_archive, dict) and cover_archive.get("front")
                    ),
                }
            )
        return results

    def get_release(self, release_id: str) -> ProviderRelease:
        if not MBID_PATTERN.fullmatch(release_id):
            raise ValueError("Invalid MusicBrainz release identifier")
        query = urlencode(
            {
                "inc": "recordings+artist-credits+release-groups+genres",
                "fmt": "json",
            }
        )
        payload = self._request_json(
            f"{self._base_url}/ws/2/release/{quote(release_id)}?{query}"
        )
        title = payload.get("title")
        if not isinstance(title, str):
            raise MusicMetadataProviderError("The selected release is invalid")
        artist = _artist_credit(payload.get("artist-credit"))
        release_group = payload.get("release-group")
        release_group_id = (
            release_group.get("id")
            if isinstance(release_group, dict) and isinstance(release_group.get("id"), str)
            else None
        )
        genres_source = payload.get("genres")
        if not isinstance(genres_source, list) and isinstance(release_group, dict):
            genres_source = release_group.get("genres")
        genres = tuple(
            str(genre["name"])
            for genre in (genres_source if isinstance(genres_source, list) else [])
            if isinstance(genre, dict) and isinstance(genre.get("name"), str)
        )
        provider_tracks: list[ProviderTrack] = []
        media = payload.get("media")
        for medium_index, medium in enumerate(media if isinstance(media, list) else [], start=1):
            if not isinstance(medium, dict):
                continue
            disc_number = medium.get("position")
            if not isinstance(disc_number, int):
                disc_number = medium_index
            tracks = medium.get("tracks")
            for track_index, track in enumerate(tracks if isinstance(tracks, list) else [], start=1):
                if not isinstance(track, dict):
                    continue
                position = track.get("position")
                if not isinstance(position, int):
                    position = track_index
                number = track.get("number")
                recording = track.get("recording")
                track_title = track.get("title")
                if not isinstance(track_title, str) and isinstance(recording, dict):
                    track_title = recording.get("title")
                if not isinstance(track_title, str):
                    continue
                length = track.get("length")
                provider_tracks.append(
                    ProviderTrack(
                        disc_number=disc_number,
                        track_number=position,
                        number=str(number) if number is not None else str(position),
                        title=track_title,
                        artist=_artist_credit(track.get("artist-credit")) or artist,
                        recording_id=(
                            recording.get("id")
                            if isinstance(recording, dict) and isinstance(recording.get("id"), str)
                            else None
                        ),
                        duration_seconds=(float(length) / 1000 if isinstance(length, (int, float)) else None),
                    )
                )
        cover_archive = payload.get("cover-art-archive")
        return ProviderRelease(
            release_id=release_id,
            release_group_id=release_group_id,
            title=title,
            artist=artist,
            date=payload.get("date") if isinstance(payload.get("date"), str) else None,
            country=payload.get("country") if isinstance(payload.get("country"), str) else None,
            genres=genres,
            tracks=tuple(provider_tracks),
            cover_art_available=bool(
                isinstance(cover_archive, dict) and cover_archive.get("front")
            ),
        )

    def get_front_cover(self, release_id: str, max_bytes: int) -> tuple[bytes, str] | None:
        if not MBID_PATTERN.fullmatch(release_id):
            raise ValueError("Invalid MusicBrainz release identifier")
        request = Request(
            f"{self._cover_art_base_url}/release/{quote(release_id)}/front-500",
            headers={"Accept": "image/*", "User-Agent": self._user_agent},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else request.full_url
                final_host = (urlsplit(final_url).hostname or "").casefold()
                if not (
                    final_host == "coverartarchive.org"
                    or final_host == "archive.org"
                    or final_host.endswith(".archive.org")
                ):
                    raise MusicMetadataProviderError("Cover artwork redirected to an untrusted host")
                data = response.read(max_bytes + 1)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        except HTTPError as error:
            if error.code == 404:
                return None
            raise MusicMetadataProviderError("Cover artwork is unavailable") from error
        except (URLError, TimeoutError, OSError) as error:
            raise MusicMetadataProviderError("Cover artwork is unavailable") from error
        if len(data) > max_bytes:
            raise MusicMetadataProviderError("Cover artwork exceeds the size limit")
        if not content_type.startswith("image/"):
            raise MusicMetadataProviderError("Cover artwork has an invalid content type")
        return data, content_type


@lru_cache
def get_musicbrainz_client() -> MusicBrainzClient:
    return MusicBrainzClient(
        user_agent=os.getenv(
            "PV_MUSICBRAINZ_USER_AGENT",
            "PersonalVault/0.1 (private local archive)",
        )
    )


MusicStore = Annotated[VaultMasterStore, Depends(get_vault_master_store)]
MusicProvider = Annotated[MusicBrainzClient, Depends(get_musicbrainz_client)]


class AlbumIdentityRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=500)
    artist: str = Field(min_length=1, max_length=300)
    album: str = Field(min_length=1, max_length=300)


class AlbumCandidate(BaseModel):
    release_id: str
    title: str
    artist: str
    date: str | None
    country: str | None
    track_count: int
    score: int
    cover_art_available: bool


class AlbumSearchResult(BaseModel):
    folder: str
    local_track_count: int
    candidates: list[AlbumCandidate]


class AlbumSelectionRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=500)
    release_id: str


class AlbumTrackMatch(BaseModel):
    asset_id: UUID | None
    filename: str | None
    disc_number: int
    track_number: int
    title: str
    artist: str
    matched: bool


class AlbumPreviewResult(BaseModel):
    folder: str
    release_id: str
    title: str
    artist: str
    date: str | None
    country: str | None
    genres: list[str]
    cover_art_available: bool
    local_track_count: int
    matched_track_count: int
    tracks: list[AlbumTrackMatch]
    unmatched_local_files: list[str]


class AlbumApprovalResult(BaseModel):
    folder: str
    release_id: str
    updated_track_count: int
    artwork_retained: bool


def _safe_folder(folder: str) -> PurePosixPath:
    candidate = PurePosixPath(folder.strip().replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Music album folder is invalid")
    return candidate


def _owned_album_assets(store: VaultMasterStore, username: str, folder: str) -> list[CataloguedAsset]:
    relative = _safe_folder(folder)
    prefix = str(MUSIC_VAULT_ROOT / relative)
    assets = [
        asset
        for asset in store.list_owned_catalogued_assets(username)
        if asset.vault_path.startswith(f"{prefix}/")
        and asset.mime_type.casefold().startswith("audio/")
    ]
    if not assets:
        raise ValueError("No owned Music tracks were found in this folder")
    return sorted(assets, key=lambda asset: asset.filename.casefold())


def _local_track_number(asset: CataloguedAsset) -> tuple[int, int] | None:
    metadata = asset.effective_metadata
    track_value = metadata.get("track_number")
    disc_value = metadata.get("disc_number")
    match = TRACK_NUMBER_PATTERN.match(str(track_value or asset.filename))
    if match is None:
        return None
    disc_match = TRACK_NUMBER_PATTERN.match(str(disc_value or "1"))
    return (int(disc_match.group(1)) if disc_match else 1, int(match.group(1)))


def _match_release(
    assets: list[CataloguedAsset],
    release: ProviderRelease,
) -> list[tuple[ProviderTrack, CataloguedAsset | None]]:
    local: dict[tuple[int, int], CataloguedAsset] = {}
    for asset in assets:
        number = _local_track_number(asset)
        if number is None:
            continue
        if number in local:
            raise ValueError(
                "More than one local track has the same disc and track number"
            )
        local[number] = asset
    return [(track, local.get((track.disc_number, track.track_number))) for track in release.tracks]


def _preview(folder: str, assets: list[CataloguedAsset], release: ProviderRelease) -> AlbumPreviewResult:
    matches = _match_release(assets, release)
    matched_asset_ids = {asset.id for _, asset in matches if asset is not None}
    return AlbumPreviewResult(
        folder=folder,
        release_id=release.release_id,
        title=release.title,
        artist=release.artist,
        date=release.date,
        country=release.country,
        genres=list(release.genres),
        cover_art_available=release.cover_art_available,
        local_track_count=len(assets),
        matched_track_count=sum(asset is not None for _, asset in matches),
        tracks=[
            AlbumTrackMatch(
                asset_id=asset.id if asset else None,
                filename=asset.filename if asset else None,
                disc_number=track.disc_number,
                track_number=track.track_number,
                title=track.title,
                artist=track.artist,
                matched=asset is not None,
            )
            for track, asset in matches
        ],
        unmatched_local_files=[
            asset.filename for asset in assets if asset.id not in matched_asset_ids
        ],
    )


def _retain_cover(
    asset: CataloguedAsset,
    data: bytes,
    mime_type: str,
    release_id: str,
    storage_root: Path,
) -> dict[str, object]:
    relative = Path("artwork") / str(asset.id) / "primary"
    destination = storage_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".primary-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return {
        "storage_key": relative.as_posix(),
        "mime_type": mime_type,
        "size_bytes": len(data),
        "provider": "cover_art_archive",
        "provider_item_id": release_id,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/search", response_model=AlbumSearchResult)
def search_music_album(
    request: AlbumIdentityRequest,
    username: AuthenticatedUsername,
    store: MusicStore,
    provider: MusicProvider,
) -> AlbumSearchResult:
    try:
        assets = _owned_album_assets(store, username, request.folder)
        candidates = provider.search_releases(request.artist.strip(), request.album.strip())
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except MusicMetadataProviderError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return AlbumSearchResult(
        folder=request.folder,
        local_track_count=len(assets),
        candidates=[AlbumCandidate(**candidate) for candidate in candidates],
    )


@router.post("/preview", response_model=AlbumPreviewResult)
def preview_music_album(
    request: AlbumSelectionRequest,
    username: AuthenticatedUsername,
    store: MusicStore,
    provider: MusicProvider,
) -> AlbumPreviewResult:
    try:
        assets = _owned_album_assets(store, username, request.folder)
        release = provider.get_release(request.release_id)
        result = _preview(request.folder, assets, release)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except MusicMetadataProviderError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return result


@router.post("/approve", response_model=AlbumApprovalResult)
def approve_music_album(
    request: AlbumSelectionRequest,
    username: AuthenticatedUsername,
    store: MusicStore,
    provider: MusicProvider,
    storage_root: Path = Depends(get_metadata_storage_root),
) -> AlbumApprovalResult:
    try:
        assets = _owned_album_assets(store, username, request.folder)
        release = provider.get_release(request.release_id)
        matches = _match_release(assets, release)
        matched = [(track, asset) for track, asset in matches if asset is not None]
        if not matched:
            raise ValueError("The selected release does not match any local track numbers")
        cover = (
            provider.get_front_cover(
                release.release_id,
                max(1, int(os.getenv("PV_VAULT_MASTER_ARTWORK_MAX_BYTES", str(DEFAULT_ARTWORK_MAX_BYTES)))),
            )
            if release.cover_art_available
            else None
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except MusicMetadataProviderError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error

    updated_count = 0
    for track, asset in matched:
        assert asset is not None
        owned_primary = (
            _retain_cover(asset, cover[0], cover[1], release.release_id, storage_root)
            if cover is not None
            else None
        )
        metadata: dict[str, object] = {
            "display_title": track.title,
            "artist": track.artist or release.artist,
            "album": release.title,
            "album_artist": release.artist,
            "track_number": track.track_number,
            "disc_number": track.disc_number,
            "release_year": int(release.date[:4]) if release.date and release.date[:4].isdigit() else None,
            "genres": list(release.genres),
            "duration_seconds": track.duration_seconds,
            "provider": {
                "name": "musicbrainz",
                "release_id": release.release_id,
                "release_group_id": release.release_group_id,
                "recording_id": track.recording_id,
            },
            "musicbrainz": {
                "release_id": release.release_id,
                "release_group_id": release.release_group_id,
                "recording_id": track.recording_id,
            },
            "artwork": {
                "provider": "cover_art_archive",
                **({"owned": {"primary": owned_primary}} if owned_primary else {}),
            },
        }
        imported = store.import_catalogued_asset_metadata(asset.id, metadata, "musicbrainz")
        if imported is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A matched Music asset disappeared during approval")
        corrected = store.update_catalogued_asset_metadata(
            asset.id,
            {
                "display_title": track.title,
                "artist": track.artist or release.artist,
                "album": release.title,
                "album_artist": release.artist,
                "track_number": track.track_number,
                "disc_number": track.disc_number,
            },
            username,
        )
        if corrected is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A matched Music asset disappeared during approval")
        updated_count += 1
    return AlbumApprovalResult(
        folder=request.folder,
        release_id=release.release_id,
        updated_track_count=updated_count,
        artwork_retained=cover is not None,
    )
