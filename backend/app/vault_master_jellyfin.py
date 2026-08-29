from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import tempfile

from app.config import (
    get_jellyfin_api_key,
    get_jellyfin_url,
    get_metadata_storage_root,
)
from app.jellyfin import (
    JellyfinAudio,
    JellyfinClient,
    JellyfinMovie,
    JellyfinMovieDetails,
    JellyfinUnavailableError,
)
from app.theatre_movie_rename import queue_movie_rename
from app.vault_master import (
    CataloguedAsset,
    VaultMasterStore,
    canonical_movie_destination,
    matches_reliable_imported_movie_identity,
)


logger = logging.getLogger("pv.vault-master.jellyfin")
MOVIE_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
)
MOVIES_VAULT_ROOT = PurePosixPath("/vault/Theatre/Movies")
MUSIC_VAULT_ROOT = PurePosixPath("/vault/Music")
AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
)
DEFAULT_ARTWORK_MAX_BYTES = 25 * 1024 * 1024


def get_movies_metadata_root() -> Path:
    return Path(os.getenv("PV_MOVIES_PATH", "/media/movies"))


def get_jellyfin_publication_roots() -> tuple[Path, ...]:
    return (
        Path(os.getenv("PV_MOVIES_PATH", "/media/movies")),
        Path(os.getenv("PV_TV_SHOWS_PATH", "/media/tv")),
        Path(os.getenv("PV_MUSIC_PATH", "/media/music")),
    )


def publish_jellyfin_media_updates(paths: tuple[Path, ...]) -> int:
    movies_root, tv_root, music_root = (
        root.resolve(strict=False)
        for root in get_jellyfin_publication_roots()
    )
    roots = (movies_root, tv_root, music_root)
    publishable = []
    for path in paths:
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_file():
            raise ValueError("Jellyfin publication path is not a file")
        if any(resolved_path.is_relative_to(root) for root in roots):
            publishable.append(resolved_path)
    if not publishable:
        return 0
    client = get_jellyfin_metadata_client()
    client.notify_media_updated(tuple(publishable))
    # TV discovery is intentionally deferred to the bounded VM-079 importer.
    # Jellyfin 10.11 may ignore targeted TV notifications; that importer makes
    # at most one full-library fallback after the discovery interval.
    if any(path.is_relative_to(music_root) for path in publishable):
        client.refresh_library()
    return len(publishable)


def publish_jellyfin_media_update(path: Path) -> bool:
    if publish_jellyfin_media_updates((path,)) == 0:
        return False
    return True


def get_artwork_max_bytes() -> int:
    return max(
        1,
        int(
            os.getenv(
                "PV_VAULT_MASTER_ARTWORK_MAX_BYTES",
                str(DEFAULT_ARTWORK_MAX_BYTES),
            )
        ),
    )


def get_jellyfin_metadata_client() -> JellyfinClient:
    return JellyfinClient(
        base_url=get_jellyfin_url(),
        api_key=get_jellyfin_api_key(),
    )


def get_jellyfin_service_status() -> dict[str, object]:
    """Vault Master owns all contact with the credential-bearing media service."""
    return get_jellyfin_metadata_client().service_status()


def request_jellyfin_library_scan() -> None:
    get_jellyfin_metadata_client().refresh_library()


def jellyfin_movie_metadata(
    movie: JellyfinMovie,
    details: JellyfinMovieDetails,
    owned_artwork: dict[str, object] | None = None,
    owned_people: dict[str, dict[str, object]] | None = None,
    owned_features: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    artwork: dict[str, object] = {
        "primary_available": details.has_primary_image,
        "backdrop_available": details.has_backdrop_image,
    }
    if owned_artwork:
        artwork["owned"] = owned_artwork
    return {
        "display_title": details.title,
        "release_year": details.year,
        "official_rating": details.official_rating,
        "community_rating": details.community_rating,
        "runtime_ticks": details.runtime_ticks,
        "overview": details.overview,
        "tagline": details.tagline,
        "genres": list(details.genres),
        "studios": list(details.studios),
        "people": [
            {
                "provider_item_id": person.item_id,
                "name": person.name,
                "role": person.role,
                "type": person.person_type,
                "has_image": person.has_image,
                **(
                    {"owned_image": owned_people[person.item_id]}
                    if owned_people and person.item_id in owned_people
                    else {}
                ),
            }
            for person in details.people
        ],
        "extras": [
            {
                "provider_item_id": extra.item_id,
                "title": extra.name,
                "runtime_ticks": extra.runtime_ticks,
                **(
                    {"owned_image": owned_features[extra.item_id]}
                    if owned_features and extra.item_id in owned_features
                    else {}
                ),
            }
            for extra in details.extras
        ],
        "trailers": [
            {
                "provider_item_id": trailer.item_id,
                "title": trailer.name,
                "runtime_ticks": trailer.runtime_ticks,
                **(
                    {"owned_image": owned_features[trailer.item_id]}
                    if owned_features and trailer.item_id in owned_features
                    else {}
                ),
            }
            for trailer in details.trailers
        ],
        "provider_ids": dict(details.provider_ids),
        "edition": details.edition,
        "collections": list(details.collections),
        "chapters": [
            {
                "name": chapter.name,
                "start_ticks": chapter.start_ticks,
            }
            for chapter in details.chapters
        ],
        "subtitles": [
            {
                "index": subtitle.index,
                "title": subtitle.title,
                "language": subtitle.language,
                "codec": subtitle.codec,
                "is_external": subtitle.is_external,
            }
            for subtitle in details.subtitles
        ],
        "provider": {
            "name": "jellyfin",
            "item_id": movie.item_id,
            "media_source_id": movie.media_source_id,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        },
        "media": {
            "container": movie.container,
            "video_codec": movie.video_codec,
            "audio_codecs": list(movie.audio_codecs),
        },
        "artwork": artwork,
    }


def queue_provisional_movie_identity_rename(
    store: VaultMasterStore,
    asset: CataloguedAsset,
) -> bool:
    """Queue the existing guarded rename only for coherent provider identity."""
    title = asset.imported_metadata.get("display_title")
    year = asset.imported_metadata.get("release_year")
    publication_set = asset.detected_metadata.get("movie_publication_set")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(year, int)
        or year < 1000
        or year > 9999
        or "display_title" in asset.user_overrides
        or "release_year" in asset.user_overrides
        or not matches_reliable_imported_movie_identity(
            asset.detected_metadata, asset.imported_metadata, title, year
        )
        or asset.owner_user_id is None
        or publication_set is not None
    ):
        return False
    destination = canonical_movie_destination(
        title, year, Path(asset.filename).suffix
    )
    if asset.vault_path == destination or store.get_catalogued_asset(destination):
        return False
    snapshot = store.theatre_movie_rename_snapshot(asset.id, asset.owner_user_id)
    if snapshot is None:
        return False
    queue_movie_rename(snapshot, destination, title, year)
    return True


def import_jellyfin_movie(
    store: VaultMasterStore,
    asset: CataloguedAsset,
    movie: JellyfinMovie,
    details: JellyfinMovieDetails,
) -> CataloguedAsset:
    updated = store.import_catalogued_asset_metadata(
        asset.id,
        jellyfin_movie_metadata(movie, details),
        "jellyfin",
    )
    if updated is None:
        raise LookupError(f"Vault asset no longer exists: {asset.id}")
    return updated


def _existing_owned_artwork(
    asset: CataloguedAsset,
) -> dict[str, object]:
    artwork = asset.imported_metadata.get("artwork")
    if not isinstance(artwork, dict):
        return {}
    owned = artwork.get("owned")
    return dict(owned) if isinstance(owned, dict) else {}


def _existing_owned_people(
    asset: CataloguedAsset,
) -> dict[str, dict[str, object]]:
    people = asset.imported_metadata.get("people")
    if not isinstance(people, list):
        return {}
    owned: dict[str, dict[str, object]] = {}
    for person in people:
        if not isinstance(person, dict):
            continue
        provider_item_id = person.get("provider_item_id")
        record = person.get("owned_image")
        if isinstance(provider_item_id, str) and isinstance(record, dict):
            owned[provider_item_id] = dict(record)
    return owned


def _existing_owned_features(
    asset: CataloguedAsset,
) -> dict[str, dict[str, object]]:
    owned: dict[str, dict[str, object]] = {}
    for field in ("extras", "trailers"):
        entries = asset.imported_metadata.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            provider_item_id = entry.get("provider_item_id")
            record = entry.get("owned_image")
            if isinstance(provider_item_id, str) and isinstance(record, dict):
                owned[provider_item_id] = dict(record)
    return owned


def person_portrait_id(provider_item_id: str) -> str:
    return hashlib.sha256(provider_item_id.encode("utf-8")).hexdigest()[:16]


def feature_thumbnail_id(provider_item_id: str) -> str:
    return hashlib.sha256(provider_item_id.encode("utf-8")).hexdigest()[:16]


def _store_artwork(
    client: JellyfinClient,
    *,
    url: str,
    storage_root: Path,
    asset: CataloguedAsset,
    provider_item_id: str,
    kind: str,
    max_bytes: int,
) -> dict[str, object]:
    relative_path = Path("artwork") / str(asset.id) / kind
    destination = storage_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = client.open_resource(url)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{kind.replace('/', '-')}-",
        dir=destination.parent,
    )
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            for chunk in stream.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"Jellyfin {kind} exceeds the artwork size limit"
                    )
                temporary_file.write(chunk)
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    return {
        "storage_key": relative_path.as_posix(),
        "mime_type": (
            stream.content_type.split(";", 1)[0].strip()
            if stream.content_type
            else "application/octet-stream"
        ),
        "size_bytes": total,
        "provider": "jellyfin",
        "provider_item_id": provider_item_id,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


def _retain_jellyfin_artwork(
    client: JellyfinClient,
    asset: CataloguedAsset,
    movie: JellyfinMovie,
    details: JellyfinMovieDetails,
    storage_root: Path,
) -> dict[str, object]:
    owned = _existing_owned_artwork(asset)
    requests: list[tuple[str, str, int | None, int]] = []
    if details.has_primary_image:
        requests.append(("poster", "Primary", None, 1000))
    if details.has_backdrop_image:
        requests.append(("backdrop", "Backdrop", 0, 1920))

    for kind, image_type, image_index, max_width in requests:
        try:
            url = client.get_image_url(
                movie.item_id,
                image_type,
                image_index=image_index,
                max_width=max_width,
            )
            owned[kind] = _store_artwork(
                client,
                url=url,
                storage_root=storage_root,
                asset=asset,
                provider_item_id=movie.item_id,
                kind=kind,
                max_bytes=get_artwork_max_bytes(),
            )
        except (JellyfinUnavailableError, OSError, ValueError):
            logger.exception(
                "Jellyfin %s retention failed for vault_path=%s; "
                "keeping the last owned copy",
                kind,
                asset.vault_path,
            )
    return owned


def _retain_jellyfin_people(
    client: JellyfinClient,
    asset: CataloguedAsset,
    details: JellyfinMovieDetails,
    storage_root: Path,
) -> dict[str, dict[str, object]]:
    owned = _existing_owned_people(asset)
    for person in details.people:
        if not person.has_image:
            continue
        portrait_id = person_portrait_id(person.item_id)
        try:
            url = client.get_image_url(
                person.item_id,
                "Primary",
                max_width=500,
            )
            owned[person.item_id] = _store_artwork(
                client,
                url=url,
                storage_root=storage_root,
                asset=asset,
                provider_item_id=person.item_id,
                kind=f"people/{portrait_id}",
                max_bytes=get_artwork_max_bytes(),
            )
        except (JellyfinUnavailableError, OSError, ValueError):
            logger.exception(
                "Jellyfin person portrait retention failed for "
                "vault_path=%s person=%s; keeping the last owned copy",
                asset.vault_path,
                person.name,
            )
    return owned


def _retain_jellyfin_features(
    client: JellyfinClient,
    asset: CataloguedAsset,
    details: JellyfinMovieDetails,
    storage_root: Path,
) -> dict[str, dict[str, object]]:
    owned = _existing_owned_features(asset)
    for feature in (*details.extras, *details.trailers):
        if not feature.has_image:
            continue
        thumbnail_id = feature_thumbnail_id(feature.item_id)
        try:
            owned[feature.item_id] = _store_artwork(
                client,
                url=client.get_image_url(
                    feature.item_id,
                    "Primary",
                    max_width=960,
                ),
                storage_root=storage_root,
                asset=asset,
                provider_item_id=feature.item_id,
                kind=f"features/{thumbnail_id}",
                max_bytes=get_artwork_max_bytes(),
            )
        except (JellyfinUnavailableError, OSError, ValueError):
            logger.exception(
                "Jellyfin feature thumbnail retention failed for "
                "vault_path=%s feature=%s; keeping the last owned copy",
                asset.vault_path,
                feature.name,
            )
    return owned


def import_jellyfin_movie_library(
    store: VaultMasterStore,
    movies_root: Path,
    client: JellyfinClient,
    *,
    artwork_root: Path | None = None,
) -> tuple[int, int]:
    root = movies_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Movies metadata root is not a directory")

    imported_count = 0
    failed_count = 0
    for path in sorted(root.rglob("*")):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.casefold() not in MOVIE_EXTENSIONS
        ):
            continue
        relative_path = path.relative_to(root)
        vault_path = str(
            MOVIES_VAULT_ROOT
            / PurePosixPath(relative_path.as_posix())
        )
        asset = store.get_catalogued_asset(vault_path)
        if asset is None:
            continue
        try:
            movie = client.find_movie_by_path(path)
            if movie is None:
                continue
            details = client.get_movie_details(movie)
            owned_artwork = (
                _retain_jellyfin_artwork(
                    client,
                    asset,
                    movie,
                    details,
                    artwork_root,
                )
                if artwork_root is not None
                else _existing_owned_artwork(asset)
            )
            owned_people = (
                _retain_jellyfin_people(
                    client,
                    asset,
                    details,
                    artwork_root,
                )
                if artwork_root is not None
                else _existing_owned_people(asset)
            )
            owned_features = (
                _retain_jellyfin_features(
                    client,
                    asset,
                    details,
                    artwork_root,
                )
                if artwork_root is not None
                else _existing_owned_features(asset)
            )
            updated = store.import_catalogued_asset_metadata(
                asset.id,
                jellyfin_movie_metadata(
                    movie,
                    details,
                    owned_artwork,
                    owned_people,
                    owned_features,
                ),
                "jellyfin",
            )
            if updated is None:
                raise LookupError(
                    f"Vault asset no longer exists: {asset.id}"
                )
            try:
                queue_provisional_movie_identity_rename(store, updated)
            except (OSError, ValueError):
                logger.exception(
                    "Reliable Jellyfin identity could not queue the managed "
                    "movie rename for vault_path=%s",
                    vault_path,
                )
            imported_count += 1
        except (JellyfinUnavailableError, LookupError):
            failed_count += 1
            logger.exception(
                "Jellyfin metadata import failed for vault_path=%s",
                vault_path,
            )

    return imported_count, failed_count


def run_jellyfin_movie_import(
    store: VaultMasterStore,
) -> tuple[int, int]:
    return import_jellyfin_movie_library(
        store,
        get_movies_metadata_root(),
        get_jellyfin_metadata_client(),
        artwork_root=get_metadata_storage_root(),
    )


def get_music_metadata_root() -> Path:
    return Path(os.getenv("PV_MUSIC_PATH", "/media/music"))


def jellyfin_audio_metadata(
    audio: JellyfinAudio,
    details: dict[str, object],
    owned_artwork: dict[str, object] | None = None,
) -> dict[str, object]:
    imported_at = datetime.now(timezone.utc).isoformat()
    return {
        **details,
        "provider": {
            "name": "jellyfin",
            "item_id": audio.item_id,
            "media_source_id": audio.media_source_id,
            "imported_at": imported_at,
        },
        "media": {
            "container": audio.container,
            "audio_codec": audio.audio_codec,
        },
        "artwork": {
            "primary_available": audio.has_primary_image,
            **({"owned": owned_artwork} if owned_artwork else {}),
        },
    }


def _retain_jellyfin_audio_artwork(
    client: JellyfinClient,
    asset: CataloguedAsset,
    audio: JellyfinAudio,
    storage_root: Path,
) -> dict[str, object]:
    existing = _existing_owned_artwork(asset)
    if not audio.has_primary_image:
        return existing
    try:
        artwork_item_id = audio.artwork_item_id or audio.item_id
        existing["primary"] = _store_artwork(
            client,
            url=client.get_image_url(
                artwork_item_id,
                "Primary",
                max_width=1000,
            ),
            storage_root=storage_root,
            asset=asset,
            provider_item_id=artwork_item_id,
            kind="primary",
            max_bytes=get_artwork_max_bytes(),
        )
    except (JellyfinUnavailableError, OSError, ValueError):
        logger.exception(
            "Jellyfin music artwork retention failed for vault_path=%s; keeping the last owned copy",
            asset.vault_path,
        )
    return existing


def import_jellyfin_music_library(
    store: VaultMasterStore,
    music_root: Path,
    client: JellyfinClient,
    *,
    artwork_root: Path | None = None,
) -> tuple[int, int]:
    root = music_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Music metadata root is not a directory")
    imported_count = 0
    failed_count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        vault_path = str(
            MUSIC_VAULT_ROOT
            / PurePosixPath(path.relative_to(root).as_posix())
        )
        asset = store.get_catalogued_asset(vault_path)
        if asset is None:
            continue
        try:
            audio = client.find_audio_by_path(path)
            if audio is None:
                continue
            details = client.get_audio_details(audio)
            lyrics_loader = getattr(client, "get_audio_lyrics", None)
            try:
                lyrics = (
                    lyrics_loader(audio)
                    if callable(lyrics_loader)
                    else None
                )
            except JellyfinUnavailableError:
                logger.exception(
                    "Jellyfin music lyrics import failed for vault_path=%s; "
                    "keeping the last retained lyrics",
                    vault_path,
                )
                existing_lyrics = asset.imported_metadata.get("lyrics")
                lyrics = (
                    existing_lyrics
                    if isinstance(existing_lyrics, dict)
                    else None
                )
            if lyrics is not None:
                details["lyrics"] = lyrics
            owned = (
                _retain_jellyfin_audio_artwork(client, asset, audio, artwork_root)
                if artwork_root is not None
                else _existing_owned_artwork(asset)
            )
            imported_metadata = jellyfin_audio_metadata(audio, details, owned)
            if isinstance(asset.imported_metadata.get("musicbrainz"), dict):
                imported_metadata["jellyfin"] = imported_metadata["provider"]
                imported_metadata["provider"] = asset.imported_metadata.get(
                    "provider",
                    {"name": "musicbrainz"},
                )
                for identity_field in (
                    "display_title",
                    "artist",
                    "album",
                    "album_artist",
                    "track_number",
                    "disc_number",
                    "release_year",
                    "genres",
                ):
                    imported_metadata.pop(identity_field, None)
            updated = store.import_catalogued_asset_metadata(
                asset.id,
                imported_metadata,
                "jellyfin",
            )
            if updated is None:
                raise LookupError(f"Vault asset no longer exists: {asset.id}")
            imported_count += 1
        except (JellyfinUnavailableError, LookupError):
            failed_count += 1
            logger.exception("Jellyfin music metadata import failed for vault_path=%s", vault_path)
    return imported_count, failed_count


def run_jellyfin_music_import(store: VaultMasterStore) -> tuple[int, int]:
    return import_jellyfin_music_library(
        store,
        get_music_metadata_root(),
        get_jellyfin_metadata_client(),
        artwork_root=get_metadata_storage_root(),
    )
