import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import AuthenticatedUsername
from app.vault_master import (
    CataloguedAsset,
    VaultMasterStore,
    get_vault_master_store,
)


router = APIRouter(prefix="/api/movies", tags=["movies"])
logger = logging.getLogger("pv.movies")

SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {
        ".avi",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".webm",
    }
)
MOVIE_FOLDER_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)$"
)
YEAR_PATTERN = re.compile(r"\b(?P<year>\d{4})\b")
MOVIE_VAULT_ROOT = PurePosixPath("/vault/Theatre/Movies")
MOVIE_COMPANION_DIRECTORY_NAMES = frozenset(
    {"extras", "featurettes", "deleted scenes", "deleted_scenes", "trailers"}
)


class MovieSummary(BaseModel):
    id: str
    asset_id: str | None = None
    title: str
    year: int | None
    poster_url: str | None = None
    is_exclusive_movie: bool = False


class MoviePerson(BaseModel):
    name: str
    role: str | None
    type: str | None
    image_url: str | None


class MovieExtra(BaseModel):
    id: str
    title: str
    runtime_minutes: int | None
    thumbnail_url: str | None
    playback_available: bool


class MovieChapter(BaseModel):
    name: str
    start_minutes: int


class MovieSubtitle(BaseModel):
    title: str | None
    language: str | None
    codec: str | None
    is_external: bool


class MovieDetails(BaseModel):
    id: str
    asset_id: str | None
    title: str
    year: int | None
    official_rating: str | None
    community_rating: float | None
    runtime_minutes: int | None
    overview: str | None
    tagline: str | None
    genres: list[str]
    studios: list[str]
    people: list[MoviePerson]
    extras: list[MovieExtra]
    trailers: list[MovieExtra]
    edition: str | None
    collections: list[str]
    chapters: list[MovieChapter]
    subtitles: list[MovieSubtitle]
    provider_imported_at: str | None
    poster_url: str | None
    backdrop_url: str | None
    container: str | None
    video_codec: str | None
    audio_codecs: list[str]
    is_exclusive_movie: bool = False


class ExclusiveMovieState(BaseModel):
    is_exclusive_movie: bool
    message: str


@dataclass(frozen=True)
class DiscoveredMovie:
    summary: MovieSummary
    source_path: Path
    size: int


def get_movies_library_path() -> Path:
    return Path(os.getenv("PV_MOVIES_PATH", "/media/movies"))


def get_movie_library_roots() -> dict[PurePosixPath, Path]:
    """Map the one canonical Theatre root to the backend's read-only mount."""
    return {MOVIE_VAULT_ROOT: get_movies_library_path()}


MoviesLibraryPath = Annotated[
    Path,
    Depends(get_movies_library_path),
]


VaultMasterCatalogue = Annotated[
    VaultMasterStore,
    Depends(get_vault_master_store),
]
MovieLibraryRoots = Annotated[
    dict[PurePosixPath, Path], Depends(get_movie_library_roots)
]


def _fallback_title_and_year(video_path: Path) -> tuple[str, int | None]:
    title = re.sub(r"[._]+", " ", video_path.stem)
    title = re.sub(r"\s+", " ", title).strip()
    year_match = YEAR_PATTERN.search(title)
    year = int(year_match.group("year")) if year_match else None

    if year_match:
        title = (
            f"{title[:year_match.start()]} {title[year_match.end():]}"
        )
        title = re.sub(r"\s+", " ", title).strip(" -._")

    return title, year


def _discover_movie_library(
    library_path: Path,
) -> list[DiscoveredMovie]:
    library_path = library_path.resolve()

    if not library_path.is_dir():
        raise FileNotFoundError("Movie library is unavailable")

    movies_by_id: dict[str, DiscoveredMovie] = {}

    for video_path in library_path.rglob("*"):
        if (
            video_path.is_symlink()
            or not video_path.is_file()
            or video_path.suffix.casefold() not in SUPPORTED_VIDEO_EXTENSIONS
        ):
            continue

        relative_path = video_path.relative_to(library_path)
        folder_match = MOVIE_FOLDER_PATTERN.fullmatch(
            relative_path.parts[0]
        )

        if folder_match:
            title = folder_match.group("title").strip()
            year = int(folder_match.group("year"))
            identity = relative_path.parts[0]
        else:
            title, year = _fallback_title_and_year(video_path)
            identity = relative_path.as_posix()

        movie_id = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:16]

        candidate = DiscoveredMovie(
            summary=MovieSummary(
                id=movie_id,
                title=title,
                year=year,
            ),
            source_path=video_path,
            size=video_path.stat().st_size,
        )
        existing = movies_by_id.get(movie_id)

        if (
            existing is None
            or candidate.size > existing.size
            or (
                candidate.size == existing.size
                and candidate.source_path.as_posix()
                < existing.source_path.as_posix()
            )
        ):
            movies_by_id[movie_id] = candidate

    return sorted(
        movies_by_id.values(),
        key=lambda discovered: (
            discovered.summary.title.casefold(),
            discovered.summary.year or 0,
            discovered.summary.id,
        ),
    )
def scan_movie_library(library_path: Path) -> list[MovieSummary]:
    return [
        discovered.summary
        for discovered in _discover_movie_library(library_path)
    ]


def _find_discovered_movie(
    library_path: Path,
    movie_id: str,
) -> DiscoveredMovie | None:
    return next(
        (
            discovered
            for discovered in _discover_movie_library(library_path)
            if discovered.summary.id == movie_id
        ),
        None,
    )


def _runtime_minutes(runtime_ticks: int | None) -> int | None:
    if runtime_ticks is None:
        return None

    return round(runtime_ticks / 10_000_000 / 60)


def _movie_vault_path(source_path: Path, library_path: Path) -> str:
    relative_path = source_path.resolve().relative_to(
        library_path.resolve()
    )
    return str(MOVIE_VAULT_ROOT.joinpath(*relative_path.parts))


def resolve_catalogued_movie_path(
    asset: CataloguedAsset,
    library_roots: dict[PurePosixPath, Path],
) -> Path:
    """Resolve a primary Movie file from its canonical Vault path.

    The public Movie identifier remains the asset UUID while this mapping makes
    its main file independently movable between approved physical roots.
    """
    vault_path = PurePosixPath(asset.vault_path)
    for vault_root, filesystem_root in library_roots.items():
        try:
            relative = vault_path.relative_to(vault_root)
        except ValueError:
            continue
        root = filesystem_root.resolve(strict=True)
        candidate = (root / Path(*relative.parts)).resolve(strict=True)
        if not candidate.is_relative_to(root) or not candidate.is_file():
            break
        return candidate
    raise ValueError("The Movie primary file is unavailable")


def reconcile_historical_exclusive_movie_paths(
    store: VaultMasterCatalogue,
    library_path: Path,
) -> tuple[UUID, ...]:
    """Repair only provable relocation-era records back to canonical Theatre.

    The prior design left an Exclusive-root primary path after a failed copy.
    A candidate is corrected only when its immutable UUID has retained history
    naming a Theatre primary, and that exact file still matches the catalogue
    size and SHA-256.  Ambiguous records remain untouched.
    """
    repaired: list[UUID] = []
    for asset in store.list_catalogued_assets_by_vault_path_prefix(
        "/vault/Exclusive Movies/"
    ):
        if (
            asset.asset_type.casefold() not in {"movie", "movies"}
            or not _asset_is_exclusive_movie(asset)
        ):
            continue
        historical_paths = [
            entry.get("previous_values", {}).get("vault_path")
            for entry in store.list_catalogued_asset_history(asset.id)
            if entry.get("action") == "exclusive_movie_verified_copy"
        ]
        theatre_path = next(
            (
                path
                for path in reversed(historical_paths)
                if isinstance(path, str) and path.startswith(f"{MOVIE_VAULT_ROOT}/")
            ),
            None,
        )
        if theatre_path is None:
            continue
        try:
            relative = PurePosixPath(theatre_path).relative_to(MOVIE_VAULT_ROOT)
            root = library_path.resolve(strict=True)
            candidate = (root / Path(*relative.parts)).resolve(strict=True)
            if (
                not candidate.is_relative_to(root)
                or not candidate.is_file()
                or candidate.stat().st_size != asset.size_bytes
            ):
                continue
            checksum = hashlib.sha256()
            with candidate.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    checksum.update(chunk)
            digest = checksum.hexdigest()
        except (OSError, ValueError):
            continue
        if digest != asset.sha256:
            continue
        updated = store.relocate_catalogued_asset(
            asset.id,
            asset.vault_path,
            theatre_path,
            asset.owner_username,
            "historical_exclusive_movie_path_reconciled",
        )
        if updated is not None:
            repaired.append(asset.id)
    return tuple(repaired)


def _asset_for_movie_id(
    movie_id: str,
    username: str,
    library_path: Path,
    store: VaultMasterCatalogue,
) -> CataloguedAsset | None:
    """Prefer the path-independent UUID; retain old scan IDs for old links."""
    try:
        asset = store.get_visible_catalogued_asset_by_id(UUID(movie_id), username)
    except ValueError:
        asset = None
    if (
        asset is not None
        and asset.asset_type.casefold() in {"movie", "movies"}
        and _is_movie_library_title(asset)
    ):
        return asset
    discovered = _find_discovered_movie(library_path, movie_id)
    if discovered is None:
        return None
    vault_path = _movie_vault_path(discovered.source_path, library_path)
    return store.get_visible_catalogued_assets([vault_path], username).get(vault_path)


def _asset_is_exclusive_movie(asset: CataloguedAsset) -> bool:
    return bool(asset.effective_metadata.get("exclusive_movie", False))


def _is_movie_library_title(asset: CataloguedAsset) -> bool:
    """Publish canonical films, not separately imported companion videos."""
    path = PurePosixPath(asset.vault_path)
    try:
        relative = path.relative_to(MOVIE_VAULT_ROOT)
    except ValueError:
        return False
    return not any(
        part.casefold() in MOVIE_COMPANION_DIRECTORY_NAMES
        for part in relative.parts[:-1]
    )


def _owned_artwork_url(
    asset: CataloguedAsset | None,
    kind: Literal["poster", "backdrop"],
) -> str | None:
    if asset is None:
        return None

    artwork = asset.imported_metadata.get("artwork")
    owned = artwork.get("owned") if isinstance(artwork, dict) else None
    record = owned.get(kind) if isinstance(owned, dict) else None
    expected_key = f"artwork/{asset.id}/{kind}"

    if (
        not isinstance(record, dict)
        or record.get("storage_key") != expected_key
        or not str(record.get("mime_type", "")).casefold().startswith(
            "image/"
        )
    ):
        return None

    return f"/api/vault-master/assets/{asset.id}/artwork/{kind}"


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(entry)
        for entry in value
        if entry is not None and str(entry).strip()
    ]


def _catalogued_people(
    value: object,
    asset: CataloguedAsset,
) -> list[MoviePerson]:
    if not isinstance(value, list):
        return []
    people: list[MoviePerson] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = _optional_string(entry.get("name"))
        if name is None:
            continue
        provider_item_id = entry.get("provider_item_id")
        portrait_id = (
            hashlib.sha256(provider_item_id.encode("utf-8")).hexdigest()[:16]
            if isinstance(provider_item_id, str)
            else None
        )
        owned_image = entry.get("owned_image")
        expected_key = (
            f"artwork/{asset.id}/people/{portrait_id}"
            if portrait_id is not None
            else None
        )
        image_url = (
            f"/api/vault-master/assets/{asset.id}/people/{portrait_id}"
            if (
                isinstance(owned_image, dict)
                and owned_image.get("storage_key") == expected_key
                and str(
                    owned_image.get("mime_type", "")
                ).casefold().startswith("image/")
            )
            else None
        )
        people.append(
            MoviePerson(
                name=name,
                role=_optional_string(entry.get("role")),
                type=_optional_string(entry.get("type")),
                image_url=image_url,
            )
        )
    return people


def _catalogued_extras(
    value: object,
    asset: CataloguedAsset,
) -> list[MovieExtra]:
    if not isinstance(value, list):
        return []
    extras: list[MovieExtra] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            continue
        title = _optional_string(entry.get("title"))
        if title is None:
            continue
        provider_item_id = entry.get("provider_item_id")
        feature_id = (
            hashlib.sha256(
                provider_item_id.encode("utf-8")
            ).hexdigest()[:16]
            if isinstance(provider_item_id, str) and provider_item_id
            else hashlib.sha256(
                f"{index}:{title}:{entry.get('runtime_ticks')}".encode("utf-8")
            ).hexdigest()[:16]
        )
        owned_image = entry.get("owned_image")
        expected_key = f"artwork/{asset.id}/features/{feature_id}"
        thumbnail_url = (
            f"/api/vault-master/assets/{asset.id}/features/{feature_id}"
            if (
                isinstance(owned_image, dict)
                and owned_image.get("storage_key") == expected_key
                and str(
                    owned_image.get("mime_type", "")
                ).casefold().startswith("image/")
            )
            else None
        )
        extras.append(
            MovieExtra(
                id=feature_id,
                title=title,
                runtime_minutes=_runtime_minutes(
                    _optional_int(entry.get("runtime_ticks"))
                ),
                thumbnail_url=thumbnail_url,
                playback_available=(
                    isinstance(provider_item_id, str)
                    and bool(provider_item_id)
                ),
            )
        )
    return extras


def _catalogued_chapters(value: object) -> list[MovieChapter]:
    if not isinstance(value, list):
        return []
    chapters: list[MovieChapter] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = _optional_string(entry.get("name"))
        start_ticks = _optional_int(entry.get("start_ticks"))
        if name is not None and start_ticks is not None:
            chapters.append(
                MovieChapter(
                    name=name,
                    start_minutes=round(start_ticks / 10_000_000 / 60),
                )
            )
    return chapters


def _catalogued_subtitles(value: object) -> list[MovieSubtitle]:
    if not isinstance(value, list):
        return []
    return [
        MovieSubtitle(
            title=_optional_string(entry.get("title")),
            language=_optional_string(entry.get("language")),
            codec=_optional_string(entry.get("codec")),
            is_external=bool(entry.get("is_external")),
        )
        for entry in value
        if isinstance(entry, dict)
    ]


@router.get("", response_model=list[MovieSummary])
def list_movies(
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    store: VaultMasterCatalogue,
    view: Literal["all", "exclusive"] = "all",
) -> list[MovieSummary]:
    return [
        MovieSummary(
            id=str(asset.id),
            asset_id=str(asset.id),
            title=_optional_string(asset.effective_metadata.get("display_title"))
            or asset.display_title,
            year=_optional_int(asset.effective_metadata.get("release_year")),
            poster_url=_owned_artwork_url(asset, "poster"),
            is_exclusive_movie=_asset_is_exclusive_movie(asset),
        )
        for asset in store.list_visible_movie_assets(username)
        if _is_movie_library_title(asset)
        and (view == "all" or _asset_is_exclusive_movie(asset))
    ]


@router.get("/{movie_id}/details", response_model=MovieDetails)
def get_movie_details(
    movie_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    library_roots: MovieLibraryRoots,
    store: VaultMasterCatalogue,
) -> MovieDetails:
    asset = _asset_for_movie_id(movie_id, username, library_path, store)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Movie not found",
        )
    metadata = asset.effective_metadata
    media = metadata.get("media")
    media = media if isinstance(media, dict) else {}
    local_streams = metadata.get("streams")
    local_streams = local_streams if isinstance(local_streams, list) else []
    local_audio_codecs = list(
        dict.fromkeys(
            stream.get("codec")
            for stream in local_streams
            if isinstance(stream, dict)
            and stream.get("type") == "audio"
            and isinstance(stream.get("codec"), str)
        )
    )
    provider = metadata.get("provider")
    provider = provider if isinstance(provider, dict) else {}
    poster_url = _owned_artwork_url(asset, "poster")
    backdrop_url = _owned_artwork_url(asset, "backdrop")

    return MovieDetails(
        id=str(asset.id),
        asset_id=str(asset.id) if asset else None,
        title=_optional_string(metadata.get("display_title"))
        or asset.display_title,
        year=_optional_int(metadata.get("release_year")),
        official_rating=_optional_string(metadata.get("official_rating")),
        community_rating=_optional_float(
            metadata.get("community_rating")
        ),
        runtime_minutes=_runtime_minutes(
            _optional_int(metadata.get("runtime_ticks"))
        ),
        overview=_optional_string(metadata.get("overview")),
        tagline=_optional_string(metadata.get("tagline")),
        genres=_string_list(metadata.get("genres")),
        studios=_string_list(metadata.get("studios")),
        people=_catalogued_people(metadata.get("people"), asset),
        extras=_catalogued_extras(metadata.get("extras"), asset),
        trailers=_catalogued_extras(metadata.get("trailers"), asset),
        edition=_optional_string(metadata.get("edition")),
        collections=_string_list(metadata.get("collections")),
        chapters=_catalogued_chapters(metadata.get("chapters")),
        subtitles=_catalogued_subtitles(metadata.get("subtitles")),
        provider_imported_at=_optional_string(provider.get("imported_at")),
        poster_url=poster_url,
        backdrop_url=backdrop_url,
        container=_optional_string(media.get("container"))
        or _optional_string(metadata.get("video_format"))
        or _optional_string(metadata.get("container_format")),
        video_codec=_optional_string(media.get("video_codec"))
        or _optional_string(metadata.get("video_codec")),
        audio_codecs=_string_list(media.get("audio_codecs"))
        or local_audio_codecs,
        is_exclusive_movie=_asset_is_exclusive_movie(asset),
    )


@router.post("/{movie_id}/exclusive", response_model=ExclusiveMovieState)
def toggle_exclusive_movie_selection(
    movie_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    store: VaultMasterCatalogue,
) -> ExclusiveMovieState:
    asset = _asset_for_movie_id(movie_id, username, library_path, store)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Movie not found",
        )
    updated = store.set_movie_exclusive_state(
        asset.id, username, not _asset_is_exclusive_movie(asset)
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner may change Exclusive status")
    return ExclusiveMovieState(
        is_exclusive_movie=_asset_is_exclusive_movie(updated),
        message=("Marked as Exclusive." if _asset_is_exclusive_movie(updated) else "Removed from Exclusive."),
    )
