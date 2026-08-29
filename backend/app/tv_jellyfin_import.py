"""One-shot PV-DEC-071 TV discovery and metadata import coordinator."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from app.jellyfin import JellyfinClient, JellyfinUnavailableError
from app.tv_shows import PendingTvEpisode, PostgresTvShowStore


def _tv_path(vault_path: str) -> Path:
    root = Path(os.getenv("PV_TV_SHOWS_PATH", "/media/tv")).resolve(strict=False)
    relative = Path(vault_path).relative_to("/vault/Theatre/TV Shows")
    return (root / relative).resolve(strict=False)


def _retain_hierarchy_artwork(
    store: PostgresTvShowStore, client: JellyfinClient, episode_id, details: dict[str, object]
) -> None:
    """Copy provider artwork into PV-owned hierarchy records; provider URLs never persist."""
    series_id = details.get("SeriesId") if isinstance(details.get("SeriesId"), str) else None
    season_id = details.get("SeasonId") if isinstance(details.get("SeasonId"), str) else None
    if not series_id and not season_id:
        return
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT show_id, season_id FROM vault_tv_episodes WHERE id=%s", (episode_id,))
        row = cursor.fetchone()
    if row is None:
        return
    for provider_id, structural_id in ((series_id, None), (season_id, row["season_id"])):
        if not provider_id:
            continue
        try:
            metadata = client.get_tv_item_metadata(provider_id)
            image_tags = metadata.get("ImageTags") if isinstance(metadata.get("ImageTags"), dict) else {}
            if not isinstance(image_tags.get("Primary"), str):
                continue
            stream = client.open_resource(client.get_image_url(provider_id, "Primary", max_width=1000))
            try:
                content = b"".join(stream.iter_bytes())
                mime_type = (stream.content_type or "image/jpeg").split(";", 1)[0]
            finally:
                stream.response.close()
            store.retain_hierarchy_artwork(row["show_id"], structural_id, "poster", content, mime_type)
        except (JellyfinUnavailableError, OSError, ValueError):
            # Metadata import remains durable even when nonessential artwork is unavailable.
            continue


def _retain_episode_artwork(
    store: PostgresTvShowStore, client: JellyfinClient, episode_id, item_id: str, details: dict[str, object]
) -> dict[str, str] | None:
    """Copy a Jellyfin Episode primary still into PV-owned Episode storage."""
    image_tags = details.get("ImageTags") if isinstance(details.get("ImageTags"), dict) else {}
    if not isinstance(image_tags.get("Primary"), str):
        return None
    try:
        stream = client.open_resource(client.get_image_url(item_id, "Primary", max_width=1000))
        try:
            content = b"".join(stream.iter_bytes())
            mime_type = (stream.content_type or "image/jpeg").split(";", 1)[0]
        finally:
            stream.response.close()
        store.retain_episode_artwork(episode_id, "primary", content, mime_type)
        return {
            "storage_key": f"tv-artwork/episodes/{episode_id}/primary",
            "mime_type": mime_type,
        }
    except (JellyfinUnavailableError, OSError, ValueError):
        # A missing/nonessential still must not alter publication or indexing state.
        return None


def import_pending_tv_metadata(
    store: PostgresTvShowStore,
    client: JellyfinClient,
    *,
    discovery_wait_seconds: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Use discovery, then one full-refresh fallback, then fail closed.

    This deliberately has no polling loop: there is one bounded wait and at
    most one refresh followed by one verification pass.
    """
    metadata_pending = store.pending_metadata_episodes()
    artwork_pending = store.pending_episode_artwork() if isinstance(store, PostgresTvShowStore) else []
    metadata_ids = {entry.id for entry in metadata_pending}
    artwork_ids = {entry.id for entry in artwork_pending}
    pending_by_id = {entry.id: entry for entry in [*metadata_pending, *artwork_pending]}
    pending = list(pending_by_id.values())
    if not pending:
        return 0
    sleep(max(0.0, discovery_wait_seconds))
    indexed = {entry.id: client.find_episode_by_path(_tv_path(entry.vault_path)) for entry in pending}
    missing = [entry for entry in pending if indexed[entry.id] is None]
    if missing:
        client.refresh_library()
        indexed = {entry.id: client.find_episode_by_path(_tv_path(entry.vault_path)) for entry in pending}
        missing = [entry for entry in pending if indexed[entry.id] is None]
        if missing:
            # Initial metadata/indexing publication remains fail-closed.  An
            # additive artwork-only historical backfill never rewrites a
            # previously published set into a failed state.
            if metadata_pending:
                store.mark_metadata_import_failed([entry.id for entry in metadata_pending])
            raise JellyfinUnavailableError("TV indexing did not complete after the single refresh fallback")
    for entry in pending:
        indexed_episode = indexed[entry.id]
        if indexed_episode is None:
            raise JellyfinUnavailableError("TV indexing result is incomplete")
        details = client.get_tv_item_metadata(indexed_episode.item_id)
        if entry.id in metadata_ids:
            title = details.get("Name") if isinstance(details.get("Name"), str) else f"Episode {entry.episode_number}"
            number = details.get("IndexNumber") if isinstance(details.get("IndexNumber"), int) else entry.episode_number
            provider_ids = details.get("ProviderIds") if isinstance(details.get("ProviderIds"), dict) else {}
            imported = {
                "display_title": title,
                "episode_number": number,
                "overview": details.get("Overview") if isinstance(details.get("Overview"), str) else None,
                "runtime_ticks": details.get("RunTimeTicks") if isinstance(details.get("RunTimeTicks"), int) else None,
                "provider_ids": provider_ids,
                "provider": {"name": "jellyfin", "item_id": indexed_episode.item_id, "media_source_id": indexed_episode.media_source_id},
            }
            store.import_episode_metadata(entry.id, title=title, episode_number=number, imported_metadata=imported, provider_ids={str(key): str(value) for key, value in provider_ids.items() if isinstance(key, str) and isinstance(value, str)})
            # The canonical asset layer owns durable sidecars.  The structural TV
            # tables retain hierarchy identity; this call records the same provider
            # import on the Episode asset and rewrites its owned sidecar atomically.
            if isinstance(store, PostgresTvShowStore):
                from app.vault_master import PostgresVaultMasterStore
                asset_store = PostgresVaultMasterStore(store.conninfo)
                asset_store.import_catalogued_asset_metadata(entry.asset_id, imported, "jellyfin")
            store.import_hierarchy_provider_ids(
                entry.id,
                details.get("SeriesId") if isinstance(details.get("SeriesId"), str) else None,
                details.get("SeasonId") if isinstance(details.get("SeasonId"), str) else None,
            )
            if isinstance(store, PostgresTvShowStore):
                _retain_hierarchy_artwork(store, client, entry.id, details)
        if isinstance(store, PostgresTvShowStore) and entry.id in artwork_ids:
            descriptor = _retain_episode_artwork(store, client, entry.id, indexed_episode.item_id, details)
            # Re-export the asset sidecar after the local-artwork descriptor is
            # retained.  The descriptor has no provider URL and is stable for
            # this immutable Episode identity.
            if descriptor:
                from app.vault_master import PostgresVaultMasterStore
                asset_store = PostgresVaultMasterStore(store.conninfo)
                asset_store.import_catalogued_asset_metadata(
                    entry.asset_id,
                    {"tv_artwork": {"primary": descriptor}},
                    "jellyfin",
                )
            else:
                # A missing or unavailable provider still is a durable clean
                # fallback, not a reason to repeatedly poll Jellyfin.
                store.mark_episode_artwork_unavailable(entry.id)
    return len(metadata_pending)
