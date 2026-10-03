from __future__ import annotations

from dataclasses import asdict, dataclass
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

from app.vault_master_music_matching import propose, revision, normalized_title, rank_release, VERSION
from app.vault_master_music_approval import persist_mapping, imported_update

from app.auth import AuthenticatedUsername, authenticated_user_id
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


class MusicMetadataProviderTechnicalError(MusicMetadataProviderError):
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
    disambiguation: str | None = None


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

    def _request_json(self, url: str, *, attempts: int = 1) -> dict[str, object]:
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
            for attempt in range(attempts):
                delay = max(self._minimum_interval_seconds, 2 ** attempt)
                try:
                    with urlopen(request, timeout=self._timeout_seconds) as response:
                        payload = json.load(response)
                    break
                except HTTPError as error:
                    error.close()
                    if error.code not in {429, 502, 503, 504}:
                        raise MusicMetadataProviderTechnicalError(
                            "The online music catalogue rejected the request"
                        ) from error
                    # Do not retry before the provider permits it. Long/dated
                    # Retry-After values return control to the user immediately.
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    if retry_after is not None:
                        if not retry_after.isdigit() or int(retry_after) > 5:
                            raise MusicMetadataProviderError(
                                "The online music catalogue is temporarily unavailable"
                            ) from error
                        delay = max(delay, int(retry_after))
                    if attempt + 1 == attempts:
                        raise MusicMetadataProviderError(
                            "The online music catalogue is temporarily unavailable"
                        ) from error
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise MusicMetadataProviderTechnicalError(
                        "The online music catalogue returned invalid data"
                    ) from error
                except (URLError, TimeoutError, OSError) as error:
                    if attempt + 1 == attempts:
                        raise MusicMetadataProviderError(
                            "The online music catalogue is temporarily unavailable"
                        ) from error
                finally:
                    self._last_request = time.monotonic()
                time.sleep(delay)
        if not isinstance(payload, dict):
            raise MusicMetadataProviderTechnicalError(
                "The online music catalogue returned invalid data"
            )
        return payload

    def search_releases(self, artist: str, album: str, limit: int = 100) -> list[dict[str, object]]:
        query = (
            f'artist:"{_lucene_phrase(artist)}" '
            f"AND release:({_lucene_terms(album)})"
        )
        # Retrieve editions before ranking; a provider page is not a shortlist.
        if not 1 <= limit <= 100:
            raise ValueError("Music catalogue page size must be between 1 and 100")
        releases: list[object] = []
        offset = 0
        for _ in range(5):
            url = f"{self._base_url}/ws/2/release/?{urlencode({'query': query, 'fmt': 'json', 'limit': limit, 'offset': offset})}"
            payload = self._request_json(url, attempts=3)
            page = payload.get("releases")
            if not isinstance(page, list):
                raise MusicMetadataProviderTechnicalError(
                    "The online music catalogue returned invalid search data"
                )
            releases.extend(page)
            offset += len(page)
            total = payload.get("count")
            if total is None or (isinstance(total, int) and offset >= total):
                break
            if not page or not isinstance(total, int) or total < 0:
                raise MusicMetadataProviderTechnicalError(
                    "The online music catalogue returned incomplete search data"
                )
        else:
            # Never present a truncated response as a complete choice of editions.
            raise ValueError("Too many matching releases. Please refine the artist or album search.")
        results: list[dict[str, object]] = []
        seen_release_ids: set[str] = set()
        for release in releases:
            if not isinstance(release, dict):
                continue
            release_id = release.get("id")
            title = release.get("title")
            if not isinstance(release_id, str) or not isinstance(title, str):
                continue
            if release_id in seen_release_ids:
                continue
            seen_release_ids.add(release_id)
            media = release.get("media")
            group = release.get("release-group")
            labels = release.get("label-info")
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
                    "release_group_id": group.get("id") if isinstance(group, dict) and isinstance(group.get("id"), str) else None,
                    "disambiguation": release.get("disambiguation") if isinstance(release.get("disambiguation"), str) else None,
                    "formats": list(dict.fromkeys(m["format"] for m in (media if isinstance(media, list) else []) if isinstance(m, dict) and isinstance(m.get("format"), str))),
                    "barcode": release.get("barcode") if isinstance(release.get("barcode"), str) else None,
                    "catalog_numbers": list(dict.fromkeys(label["catalog-number"] for label in (labels if isinstance(labels, list) else []) if isinstance(label, dict) and isinstance(label.get("catalog-number"), str))),
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
            f"{self._base_url}/ws/2/release/{quote(release_id)}?{query}", attempts=3
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
            disambiguation=payload.get("disambiguation") or None,
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
    album_group_id: UUID | None = None
    folder: str = Field(min_length=1, max_length=500)
    artist: str = Field(min_length=1, max_length=300)
    album: str = Field(min_length=1, max_length=300)


class AlbumDeclaration(BaseModel):
    import_group_id: UUID
    artist_name: str = Field(min_length=1, max_length=300)
    album_title: str = Field(min_length=1, max_length=300)


class AlbumMembership(BaseModel):
    asset_ids: list[UUID] = Field(min_length=1, max_length=1000)


class AlbumCorrection(BaseModel):
    artist_name: str = Field(min_length=1, max_length=300)
    album_title: str = Field(min_length=1, max_length=300)


@router.post("/groups")
def declare_album(request: AlbumDeclaration, username: AuthenticatedUsername, store: MusicStore):
    try:
        return store.declare_music_album(authenticated_user_id(username), request.model_dump(mode="json"))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/groups/{album_id}/members")
def declare_members(album_id: UUID, request: AlbumMembership, username: AuthenticatedUsername, store: MusicStore):
    try:
        store.bind_music_album(album_id, authenticated_user_id(username), request.asset_ids)
        return {"album_group_id": album_id, "member_count": len(store.music_album_asset_ids(album_id))}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.put("/groups/{album_id}/sequence")
def set_album_sequence(album_id: UUID, request: AlbumMembership, username: AuthenticatedUsername, store: MusicStore):
    """Complete ordered UUID list; membership and owner remain unchanged."""
    try:
        return store.set_music_album_order(album_id, authenticated_user_id(username), request.asset_ids)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.patch("/groups/{album_id}")
def correct_album(album_id: UUID, request: AlbumCorrection, username: AuthenticatedUsername, store: MusicStore):
    try:
        return store.correct_music_album(album_id, authenticated_user_id(username), request.artist_name, request.album_title)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


class AlbumCandidate(BaseModel):
    release_group_id: str | None = None
    disambiguation: str | None = None
    formats: list[str] = Field(default_factory=list)
    barcode: str | None = None
    catalog_numbers: list[str] = Field(default_factory=list)
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


class ReviewedTrack(BaseModel):
    asset_id: UUID
    provider_track: str | None = None


class AlbumSelectionRequest(BaseModel):
    album_group_id: UUID | None = None
    folder: str = Field(min_length=1, max_length=500)
    release_id: str
    review_revision: str | None = None
    mapping: list[ReviewedTrack] | None = Field(default=None, max_length=200)
    apply_order: bool = False


class AlbumTrackMatch(BaseModel):
    asset_id: UUID | None
    filename: str | None
    disc_number: int
    track_number: int
    title: str
    artist: str
    matched: bool
    duration_seconds: float | None = None


class AlbumPreviewResult(BaseModel):
    review_revision: str | None = None
    local_matches: list[dict] = Field(default_factory=list)
    order_ready: bool = False
    identity_conflicts: list[str] = Field(default_factory=list)
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
    order_state: str | None = None
    sidecars_exported: bool = True
    folder: str
    release_id: str
    updated_track_count: int
    artwork_retained: bool


def _safe_folder(folder: str, *, allow_root_search: bool = False) -> PurePosixPath:
    cleaned = folder.strip().replace("\\", "/")
    if cleaned == ".":
        if allow_root_search:
            return PurePosixPath(".")
        raise ValueError("These tracks are stored directly in Music. Album grouping is required before reviewing or approving a match.")
    candidate = PurePosixPath(cleaned)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Music album folder is invalid")
    return candidate


def _owned_album_assets(store: VaultMasterStore, username: str, folder: str, *, allow_root_search: bool = False, album_group_id: UUID | None = None) -> list[CataloguedAsset]:
    if album_group_id is not None:
        album = store.get_music_album(album_group_id)
        if album is None or album.owner_user_id != authenticated_user_id(username):
            raise ValueError("Music album not found")
        member_ids = set(store.music_album_asset_ids(album_group_id))
        assets = [asset for asset in store.list_owned_catalogued_assets_by_user_id(album.owner_user_id) if asset.id in member_ids and asset.asset_type == "Music" and asset.mime_type.startswith("audio/")]
        if not assets or len(assets) != len(member_ids):
            raise ValueError("Music album members are unavailable")
        return sorted(assets, key=lambda asset: asset.filename.casefold())
    relative = _safe_folder(folder, allow_root_search=allow_root_search)
    prefix = str(MUSIC_VAULT_ROOT / relative)
    assets = [
        asset
        for asset in store.list_owned_catalogued_assets_by_user_id(authenticated_user_id(username))
        if asset.vault_path.startswith(f"{prefix}/")
        and (relative != PurePosixPath(".") or PurePosixPath(asset.vault_path).parent == MUSIC_VAULT_ROOT)
        and asset.mime_type.casefold().startswith("audio/")
        and (allow_root_search or store.get_asset_music_album(asset.id) is None)
    ]
    if not assets:
        raise ValueError("No owned Music tracks were found in this folder")
    return sorted(assets, key=lambda asset: asset.filename.casefold())


def _local_track_number(asset: CataloguedAsset, *, explicit_group: bool = False) -> tuple[int, int] | None:
    metadata = asset.effective_metadata
    track_value = metadata.get("track_number")
    disc_value = metadata.get("disc_number")
    if explicit_group:
        # Explicit albums never obtain track evidence from filenames or defaults.
        values = [re.fullmatch(r"\s*(0*[1-9][0-9]*)(?:/0*[1-9][0-9]*)?\s*", str(value)) for value in (disc_value, track_value)]
        return tuple(int(value[1]) for value in values) if all(values) else None
    match = TRACK_NUMBER_PATTERN.match(str(track_value or asset.filename))
    if match is None:
        return None
    disc_match = TRACK_NUMBER_PATTERN.match(str(disc_value or "1"))
    return (int(disc_match.group(1)) if disc_match else 1, int(match.group(1)))


def _match_release(
    assets: list[CataloguedAsset],
    release: ProviderRelease,
    *, explicit_group: bool = False,
) -> list[tuple[ProviderTrack, CataloguedAsset | None]]:
    local: dict[tuple[int, int], CataloguedAsset] = {}
    ambiguous = set()
    for asset in assets:
        number = _local_track_number(asset, explicit_group=explicit_group)
        if number is None:
            continue
        if number in local:
            if explicit_group:
                ambiguous.add(number)
                continue
            raise ValueError(
                "More than one local track has the same disc and track number"
            )
        local[number] = asset
    if explicit_group:
        coordinates = [(track.disc_number, track.track_number) for track in release.tracks]
        ambiguous.update(number for number in coordinates if coordinates.count(number) != 1)
        for number in ambiguous:
            local.pop(number, None)
    return [(track, local.get((track.disc_number, track.track_number))) for track in release.tracks]


def _preview(folder: str, assets: list[CataloguedAsset], release: ProviderRelease, *, explicit_group: bool = False) -> AlbumPreviewResult:
    matches = _match_release(assets, release, explicit_group=explicit_group)
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
                duration_seconds=track.duration_seconds,
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
        assets = _owned_album_assets(store, username, request.folder, allow_root_search=True, album_group_id=request.album_group_id)
        candidates = provider.search_releases(request.artist.strip(), request.album.strip())
        # Count is advisory, below locally compared artist/title identity. Never use order,
        # filenames, or a count mismatch as a discovery/approval gate.
        candidates = sorted(
            (AlbumCandidate(**candidate) for candidate in candidates),
            key=lambda candidate: rank_release(candidate, request.artist, request.album, assets),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except MusicMetadataProviderTechnicalError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    except MusicMetadataProviderError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return AlbumSearchResult(
        folder=request.folder,
        local_track_count=len(assets),
        candidates=candidates,
    )


@router.post("/preview", response_model=AlbumPreviewResult)
def preview_music_album(
    request: AlbumSelectionRequest,
    username: AuthenticatedUsername,
    store: MusicStore,
    provider: MusicProvider,
) -> AlbumPreviewResult:
    try:
        assets = _owned_album_assets(store, username, request.folder, album_group_id=request.album_group_id)
        release = provider.get_release(request.release_id)
        result = _preview(request.folder, assets, release, explicit_group=request.album_group_id is not None)
        if request.album_group_id is not None:
            proposals = propose(assets, release)
            result.review_revision = _review_revision(assets, release, store, request.album_group_id)
            result.local_matches = proposals
            result.order_ready = store.get_music_album_order(request.album_group_id)["state"] == "ready"
            proposed = {row["proposed_track"]: row for row in proposals if row["proposed_track"]}
            for track in result.tracks:
                row = proposed.get(f"{track.disc_number}:{track.track_number}")
                track.asset_id = UUID(row["asset_id"]) if row else None
                track.filename = row["filename"] if row else None
                track.matched = row is not None
            result.matched_track_count = len(proposed)
            result.unmatched_local_files = [r["filename"] for r in proposals if not r["proposed_track"]]
        if request.album_group_id is not None:
            album = store.get_music_album(request.album_group_id)
            result.identity_conflicts = [f"Provider {field}: {suggestion}. Your album identity remains {declared}." for field, suggestion, declared in (("artist", release.artist, album.artist_name), ("album", release.title, album.album_title)) if suggestion.casefold() != declared.casefold()]
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except MusicMetadataProviderError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return result


@router.post("/approve", response_model=AlbumApprovalResult, response_model_exclude_unset=True)
def approve_music_album(
    request: AlbumSelectionRequest,
    username: AuthenticatedUsername,
    store: MusicStore,
    provider: MusicProvider,
    storage_root: Path = Depends(get_metadata_storage_root),
) -> AlbumApprovalResult:
    try:
        assets = _owned_album_assets(store, username, request.folder, album_group_id=request.album_group_id)
        release = provider.get_release(request.release_id)
        if request.mapping is not None:
            if request.album_group_id is None:
                raise ValueError("Reviewed mapping requires an explicit album group")
            return _approve_reviewed_mapping(request, assets, release, username, store, provider, storage_root)
        matches = _match_release(assets, release, explicit_group=request.album_group_id is not None)
        matched = [(track, asset) for track, asset in matches if asset is not None]
        if not matched and request.album_group_id is None:
            raise ValueError("The selected release does not match any local track numbers")
        if request.album_group_id is not None:
            # Approval identifies the explicit album even when its tracks cannot
            # be matched. Album-level metadata is safe for every existing member;
            # unmatched titles, durations, coordinates and durable order stay put.
            matched_ids = {asset.id for _, asset in matched}
            matched.extend((None, asset) for asset in assets if asset.id not in matched_ids)
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
            "artist": (track.artist if track else None) or release.artist,
            "album": release.title,
            "album_artist": release.artist,
            "release_year": int(release.date[:4]) if release.date and release.date[:4].isdigit() else None,
            "genres": list(release.genres),
            "provider": {
                "name": "musicbrainz",
                "release_id": release.release_id,
                "release_group_id": release.release_group_id,
                "recording_id": track.recording_id if track else None,
            },
            "musicbrainz": {
                "release_id": release.release_id,
                "release_group_id": release.release_group_id,
                "recording_id": track.recording_id if track else None,
            },
            "artwork": {
                "provider": "cover_art_archive",
                **({"owned": {"primary": owned_primary}} if owned_primary else {}),
            },
        }
        if track is not None:
            metadata.update(display_title=track.title, track_number=track.track_number,
                            disc_number=track.disc_number, duration_seconds=track.duration_seconds)
        if request.album_group_id is not None:
            if owned_primary is None:
                metadata.pop("artwork", None)  # A release without a cover must not erase owned artwork.
            metadata["musicbrainz"]["suggested_artist"] = release.artist
            metadata["musicbrainz"]["suggested_album"] = release.title
            for key in ("artist", "album", "album_artist"):
                metadata.pop(key, None)
        imported = store.import_catalogued_asset_metadata(asset.id, metadata, "musicbrainz")
        if imported is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A matched Music asset disappeared during approval")
        if request.album_group_id is not None:
            # Retain provider values in their own layer. Existing manual values
            # outrank them; approving a release does not create manual overrides
            # or establish membership playback positions.
            updated_count += 1
            continue
        corrected = store.update_catalogued_asset_metadata(
            asset.id,
            {
                "display_title": track.title,
                **({"artist": track.artist or release.artist, "album": release.title, "album_artist": release.artist} if request.album_group_id is None else {}),
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


def _review_revision(assets, release, store, album_id):
    import hashlib
    state = json.dumps(store.get_music_album_order(album_id), sort_keys=True, default=str)
    return hashlib.sha256((revision(assets, release) + state).encode()).hexdigest()


def _approve_reviewed_mapping(request, assets, release, username, store, provider, storage_root):
    expected_order = store.get_music_album_order(request.album_group_id)
    if request.review_revision != _review_revision(assets, release, store, request.album_group_id):
        raise ValueError("Album or release evidence changed; review the mapping again")
    proposals={row["asset_id"]:row for row in propose(assets,release)}
    selections={entry.asset_id:entry.provider_track for entry in request.mapping}
    if len(selections)!=len(request.mapping) or set(selections)!={a.id for a in assets}:
        raise ValueError("Mapping must review every current local file exactly once")
    tracks={f"{t.disc_number}:{t.track_number}":t for t in release.tracks}
    chosen=[k for k in selections.values() if k is not None]
    if len(chosen)!=len(set(chosen)) or any(k not in tracks for k in chosen):
        raise ValueError("A provider track can be assigned once; choose valid unique positions")
    cover=provider.get_front_cover(release.release_id,max(1,int(os.getenv("PV_VAULT_MASTER_ARTWORK_MAX_BYTES",str(DEFAULT_ARTWORK_MAX_BYTES))))) if release.cover_art_available else None
    updates=[]; audit=[]
    for asset in assets:
        key=selections[asset.id]; track=tracks.get(key); proposal=proposals[str(asset.id)]
        previous=asset.imported_metadata.get("music_match",{})
        decision="override" if key!=proposal["proposed_track"] or (previous.get("decision")=="override") else "accepted"
        record={"version":VERSION,"release_id":release.release_id,"release_group_id":release.release_group_id,
                "provider_track":key,"decision":decision,"approved_by":str(authenticated_user_id(username)),
                "score":proposal["score"],"reason":proposal["reason"],"normalized_local":proposal.get("normalized_local"),
                "normalized_provider":normalized_title(track.title) if track else None,
                "local_duration":proposal.get("local_duration"),"provider_original":asdict(track) if track else None,
                "release_original":{"title":release.title,"artist":release.artist,"date":release.date,"country":release.country,"edition":release.disambiguation}}
        metadata={"release_year":int(release.date[:4]) if release.date and release.date[:4].isdigit() else None,
                  "genres":list(release.genres),"musicbrainz":{"release_id":release.release_id,"release_group_id":release.release_group_id,"recording_id":track.recording_id if track else None,"suggested_artist":release.artist,"suggested_album":release.title},
                  "provider":{"name":"musicbrainz","release_id":release.release_id,"release_group_id":release.release_group_id}}
        if track:
            metadata.update(display_title=track.title,disc_number=track.disc_number,track_number=track.track_number)
        if cover:
            metadata["artwork"]={"provider":"cover_art_archive","owned":{"primary":_retain_cover(asset,cover[0],cover[1],release.release_id,storage_root)}}
        record["owned_fields"]=list(metadata)
        metadata["music_match"]=record
        updates.append(imported_update(asset,metadata))
        audit.append({"asset_id":str(asset.id),"provider_track":key,"decision":decision})
    # All local AND provider tracks must be accounted for before a new sequence is established.
    complete=len(chosen)==len(assets)==len(tracks)
    sequence=sorted(selections,key=lambda k:(tracks[selections[k]].disc_number,tracks[selections[k]].track_number)) if complete else None
    exported=persist_mapping(store,request.album_group_id,authenticated_user_id(username),assets,updates,
                             {"version":VERSION,"release_id":release.release_id,"mapping":audit,"apply_order":request.apply_order},sequence,request.apply_order,expected_order)
    return AlbumApprovalResult(folder=request.folder,release_id=release.release_id,updated_track_count=len(updates),artwork_retained=cover is not None,
                               sidecars_exported=exported,order_state=store.get_music_album_order(request.album_group_id)["state"])
