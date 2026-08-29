import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth import get_authentication_store, get_enrolment_store, get_passkey_store, router as auth_router
from app.build_info import build_info, load_project_version
from app.auth_store import AuthenticationStore, PostgresAuthenticationStore
from app.passkeys import PostgresPasskeyStore
from app.config import get_admin_username, get_database_conninfo, get_webauthn_origin
from app.request_security import trusted_proxy_networks
from app.gallery import router as gallery_router
from app.people import router as people_router
from app.gallery_intelligence import (
    PostgresGalleryIntelligenceStore,
    get_gallery_intelligence_store,
    process_next_gallery_intelligence_job,
)
from app.gallery_people import PostgresGalleryPeopleStore, get_gallery_people_store
from app.video_intelligence import (
    PostgresVideoIntelligenceStore,
    get_video_intelligence_store,
    process_next_video_analysis_job,
    reconcile_video_analysis_job,
)
from app.incoming import (
    get_arrival_hall_path,
    get_arrival_hall_file_owner,
    router as arrival_hall_router,
)
from app.movies import (
    get_movies_library_path,
    reconcile_historical_exclusive_movie_paths,
    router as movies_router,
)
from app.movie_playback import router as movie_playback_router
from app.music import router as music_router
from app.music_playback import router as music_playback_router
from app.reading_room_catalogue import router as reading_room_catalogue_router
from app.vault_libraries import router as vault_libraries_router
from app.vault_master_api import (
    get_destination_paths,
    router as vault_master_router,
)
from app.vault_master import (
    INCOMING_SOURCE,
    PostgresVaultMasterStore,
    enqueue_root,
    get_vault_master_store,
    process_next_batch,
    process_next_move,
)
from app.federation import FederationStore
from app.federated_downloads import reconcile_next_theatre_download_receipt
from app.arrival_managed_publisher import (
    queue_item as queue_arrival_managed_item,
    reconcile_next_receipt as reconcile_next_arrival_managed_receipt,
)
from app.theatre_movie_rename import (
    reconcile_next_receipt as reconcile_next_theatre_movie_rename_receipt,
)
from app.vault_master_jellyfin import (
    get_jellyfin_metadata_client,
    publish_jellyfin_media_updates,
    run_jellyfin_movie_import,
    run_jellyfin_music_import,
)
from app.vault_master_music import router as vault_master_music_router
from app.vault_master_ai import PostgresAiStore, get_ai_store, process_next_ai_job
from app.vault_master_ingestion_ai import (
    PostgresIngestionAiStore,
    get_ingestion_ai_store,
    process_next_ingestion_ai_job,
    queue_pending_ingestion_image_analysis,
)
from app.vault_master_autopilot import (
    PostgresAutopilotStore,
    get_autopilot_store,
    process_autopilot_batch,
    reconcile_autopilot_runs,
)
from app.vault_master_intake import (
    PostgresIntakeStore,
    get_intake_store,
    router as vault_master_intake_router,
)
from app.vault_master_reading import PostgresReadingRoomStore, get_reading_room_store
from app.vault_master_reading_review import (
    PostgresPublicationReviewStore,
    get_publication_review_store,
)
from app.vault_control import router as vault_control_router
from app.vault_control_intake import router as vault_control_intake_router
from app.vault_storage_api import router as vault_storage_router
from app.vault_storage_control import reconcile_pending_slot_integrations
from app.vault_services import router as vault_services_router, set_worker_state
from app.vault_control_users import router as vault_control_users_router
from app.user_state import router as user_state_router
from app.tv_shows import PostgresTvShowStore, router as tv_shows_router
from app.tv_playback import router as tv_playback_router
from app.tv_jellyfin_import import import_pending_tv_metadata


def _arrival_hall_owner_reference(path: Path) -> str | UUID | None:
    """Return immutable manifest ownership for the system scan worker.

    Pre-UUID manifests are retained only as an exact-account migration bridge;
    no requester or administrator identity is substituted.
    """
    owner = get_arrival_hall_file_owner(get_arrival_hall_path(), path)
    if owner is None:
        return None
    try:
        return UUID(owner)
    except ValueError:
        return owner


logger = logging.getLogger("pv.vault-master.worker")


def queue_gallery_intelligence_for_published_asset(
    store, gallery_store, item_id, username: str
) -> bool:
    """Queue GI only for the incoming item that has just been published.

    This deliberately does not enumerate the Gallery catalogue. Historical
    assets are processed only through the explicit administrator backfill API.
    """
    moved_item = store.get_item(item_id)
    if moved_item is None or not moved_item.proposed_destination:
        return False
    published_asset = store.get_catalogued_asset(moved_item.proposed_destination)
    if published_asset is None or published_asset.asset_type.casefold() != "gallery":
        return False
    gallery_store.queue(published_asset.id, username)
    return True


def queue_video_intelligence_for_published_asset(store, video_store, item_id) -> bool:
    """Queue one newly published Home Video without sweeping the catalogue.

    This hook is deliberately invoked only for the item returned by the normal
    successful-move path.  It neither enumerates existing Home Videos nor
    participates in Arrival Hall routing.  The existing Video Intelligence
    store remains the single deduplication authority for automatic and manual
    requests alike.
    """
    moved_item = store.get_item(item_id)
    if (
        moved_item is None
        or moved_item.state != "moved"
        or not moved_item.proposed_destination
    ):
        logger.info(
            "Video Intelligence automatic queue skipped: "
            "item_id=%s is not a successful publication",
            item_id,
        )
        return False
    published_asset = store.get_catalogued_asset(moved_item.proposed_destination)
    if (
        published_asset is None
        or published_asset.asset_type != "Home Videos"
        or not published_asset.vault_path.startswith("/vault/Home Videos/")
    ):
        logger.info(
            "Video Intelligence automatic queue skipped: "
            "item_id=%s is not a published Home Video",
            item_id,
        )
        return False

    previous = video_store.latest_job(published_asset.id)
    try:
        if published_asset.owner_user_id is None:
            raise ValueError("Video Intelligence requires a resolved asset owner")
        job = video_store.queue(
            published_asset.id,
            published_asset.owner_username,
            published_asset.owner_user_id,
        )
    except Exception as error:
        # Publication is already complete.  Retain only a bounded failure
        # record; a later owner-selected Analyse video action remains usable.
        detail = str(error).replace("\n", " ")[:500]
        logger.warning(
            "Video Intelligence automatic queue failed: asset_id=%s error=%s",
            published_asset.id,
            detail,
        )
        try:
            store.record_catalogued_asset_history(
                published_asset.id,
                published_asset.owner_username,
                "video_intelligence_auto_queue_failed",
                {"error": detail},
            )
        except Exception:
            logger.warning(
                "Video Intelligence automatic queue failure could not be audited: asset_id=%s",
                published_asset.id,
            )
        return False

    active_statuses = {"queued", "sampling", "analysing", "reconciling"}
    if previous is not None:
        reason = (
            "already_active"
            if previous.status in active_statuses
            else "already_analysed"
        )
        logger.info(
            "Video Intelligence automatic queue skipped: asset_id=%s reason=%s job_id=%s",
            published_asset.id,
            reason,
            job.id,
        )
        return False
    logger.info(
        "Video Intelligence automatic queue created: asset_id=%s job_id=%s",
        published_asset.id,
        job.id,
    )
    return True


async def run_vault_master_worker() -> None:
    poll_seconds = max(
        1,
        int(os.getenv("PV_VAULT_MASTER_POLL_SECONDS", "5")),
    )
    automatic_scan_seconds = max(
        30,
        int(os.getenv("PV_VAULT_MASTER_SCAN_SECONDS", "60")),
    )
    next_automatic_scan = time.monotonic() + automatic_scan_seconds
    next_federation_delivery = time.monotonic()
    federation_reconciliation_seconds = max(900, int(os.getenv("PV_FEDERATION_RECONCILIATION_SECONDS", "3600")))
    next_federation_reconciliation = time.monotonic() + federation_reconciliation_seconds
    movie_import_seconds = max(
        60,
        int(
            os.getenv(
                "PV_VAULT_MASTER_MOVIE_IMPORT_SECONDS",
                "300",
            )
        ),
    )
    next_movie_import = time.monotonic()
    next_music_import = time.monotonic()
    next_tv_import = time.monotonic()
    sidecar_reconciliation_seconds = max(
        300,
        int(
            os.getenv(
                "PV_VAULT_MASTER_SIDECAR_RECONCILIATION_SECONDS",
                "3600",
            )
        ),
    )
    next_sidecar_reconciliation = time.monotonic()
    next_storage_integration_reconciliation = time.monotonic()
    historical_exclusive_reconciliation_complete = False

    while True:
        store = None
        intake_open = False
        moved = None
        processed = None
        try:
            store = get_vault_master_store()
            get_reading_room_store()
            get_publication_review_store()
            if not historical_exclusive_reconciliation_complete:
                repaired = await asyncio.to_thread(
                    reconcile_historical_exclusive_movie_paths,
                    store,
                    get_movies_library_path(),
                )
                historical_exclusive_reconciliation_complete = True
                if repaired:
                    logger.info(
                        "Reconciled historical Exclusive Movie paths: asset_ids=%s",
                        ",".join(map(str, repaired)),
                    )
            intake_open = await asyncio.to_thread(get_intake_store().global_enabled)
            theatre_rename = await asyncio.to_thread(
                reconcile_next_theatre_movie_rename_receipt, store
            )
            if theatre_rename is not None:
                logger.info(
                    "Managed Theatre movie rename reconciled: asset_id=%s",
                    theatre_rename,
                )
                processed = theatre_rename
            theatre_download = (
                None
                if processed is not None
                else await asyncio.to_thread(reconcile_next_theatre_download_receipt, store)
            )
            if theatre_download is not None:
                logger.info("Federated Theatre download receipt reconciled: asset_id=%s", theatre_download)
                processed = theatre_download
            else:
                managed_arrival = await asyncio.to_thread(reconcile_next_arrival_managed_receipt, store)
                if managed_arrival is not None:
                    logger.info("Arrival Hall managed receipt reconciled: asset_id=%s", managed_arrival)
                    processed = managed_arrival
            if processed is None and intake_open:
                moved = await asyncio.to_thread(
                    process_next_move,
                    store,
                    get_arrival_hall_path(),
                    get_destination_paths(),
                    publish_jellyfin_media_updates,
                    queue_arrival_managed_item,
                )
                processed = (
                    moved
                    if moved is not None
                    else await asyncio.to_thread(
                        process_next_batch,
                        store,
                        publish_jellyfin_media_updates,
                        _arrival_hall_owner_reference,
                    )
                )
            if moved is not None:
                next_music_import = time.monotonic() + 15
            # Queue only the asset that has just completed normal Vault Master
            # publication.  Historical Gallery scans are an explicit admin
            # backfill operation, never an idle-worker side effect.
            if moved is not None:
                await asyncio.to_thread(
                    queue_gallery_intelligence_for_published_asset,
                    store,
                    get_gallery_intelligence_store(),
                    moved,
                    get_admin_username(),
                )
                # Video Intelligence follows the same narrow post-publication
                # boundary: only the just-published Home Video is considered.
                # It never sweeps historical catalogue entries at startup or
                # during normal listing/scanning work.
                await asyncio.to_thread(
                    queue_video_intelligence_for_published_asset,
                    store,
                    get_video_intelligence_store(),
                    moved,
                )
            if (
                processed is None
                and intake_open
                and os.getenv("PV_VAULT_MASTER_AI_ENABLED", "false").lower()
                in {"1", "true", "yes", "on"}
            ):
                processed = await asyncio.to_thread(
                    process_next_ai_job, get_ai_store(), store
                )
                if processed is None:
                    await asyncio.to_thread(
                        queue_pending_ingestion_image_analysis,
                        get_ingestion_ai_store(),
                        store,
                        get_admin_username(),
                    )
                    processed = await asyncio.to_thread(
                        process_next_ingestion_ai_job,
                        get_ingestion_ai_store(),
                        store,
                    )
            if processed is None and intake_open:
                processed = await asyncio.to_thread(
                    reconcile_autopilot_runs,
                    get_autopilot_store(),
                    store,
                )
            if processed is None and intake_open:
                processed = await asyncio.to_thread(
                    process_autopilot_batch,
                    get_autopilot_store(),
                    get_ingestion_ai_store(),
                    store,
                    get_arrival_hall_path(),
                    get_destination_paths(),
                )
            # Gallery Intelligence is deliberately a post-publication metadata
            # worker.  It never participates in Arrival Hall routing or scores.
            if processed is None and intake_open:
                gallery_store = get_gallery_intelligence_store()
                processed = await asyncio.to_thread(
                    process_next_gallery_intelligence_job,
                    gallery_store,
                    store,
                    None,
                    get_ingestion_ai_store(),
                )
            # Video Intelligence is a post-publication worker. V5 queues only
            # the just-published Home Video above; it never performs a
            # historic sweep and still uses the same manual queue contract.
            if processed is None:
                processed = await asyncio.to_thread(
                    process_next_video_analysis_job,
                    get_video_intelligence_store(),
                    store,
                    get_gallery_people_store(),
                )
                if processed is not None:
                    # V3 reconciles only the selected V2 job just processed;
                    # it deliberately does not sweep historical videos.
                    await asyncio.to_thread(
                        reconcile_video_analysis_job,
                        get_video_intelligence_store(), store,
                        get_gallery_people_store(), get_gallery_intelligence_store(), processed,
                    )
        except Exception:
            logger.exception("Vault Master worker iteration failed")
            processed = None

        if (
            processed is None
            and intake_open
            and time.monotonic() >= next_automatic_scan
        ):
            if store is not None:
                try:
                    enqueue_root(
                        store,
                        get_arrival_hall_path(),
                        INCOMING_SOURCE,
                    )
                except Exception:
                    logger.exception(
                        "Vault Master automatic Arrival Hall scan could not "
                        "be queued"
                    )
            next_automatic_scan = (
                time.monotonic() + automatic_scan_seconds
            )

        # Movie metadata import has its own bounded schedule. Ordinary Arrival
        # Hall or catalogue work must not postpone that external-source import.
        if time.monotonic() >= next_movie_import:
            if store is not None:
                try:
                    imported, failed = await asyncio.to_thread(
                        run_jellyfin_movie_import,
                        store,
                    )
                    logger.info(
                        "Vault Master movie metadata import completed: "
                        "imported=%s failed=%s",
                        imported,
                        failed,
                    )
                except Exception:
                    logger.exception(
                        "Vault Master movie metadata import could not run"
                    )
            next_movie_import = time.monotonic() + movie_import_seconds

        if processed is None and time.monotonic() >= next_music_import:
            if store is not None:
                try:
                    imported, failed = await asyncio.to_thread(
                        run_jellyfin_music_import,
                        store,
                    )
                    logger.info(
                        "Vault Master music metadata import completed: imported=%s failed=%s",
                        imported,
                        failed,
                    )
                except (OSError, ValueError):
                    logger.debug("Music metadata root is not available yet")
                except Exception:
                    logger.exception("Vault Master music metadata import could not run")
            next_music_import = time.monotonic() + movie_import_seconds

        if time.monotonic() >= next_tv_import:
            try:
                imported = await asyncio.to_thread(
                    import_pending_tv_metadata,
                    PostgresTvShowStore(get_database_conninfo()),
                    get_jellyfin_metadata_client(),
                )
                if imported:
                    logger.info("Vault Master TV metadata import completed: imported=%s", imported)
            except Exception:
                logger.exception("Vault Master TV metadata import failed after bounded discovery")
            next_tv_import = time.monotonic() + movie_import_seconds

        if (
            processed is None
            and time.monotonic() >= next_sidecar_reconciliation
        ):
            if store is not None:
                try:
                    result = await asyncio.to_thread(store.reconcile_sidecars)
                    if result.repaired or result.failed:
                        logger.info(
                            "Vault Master sidecar reconciliation completed: "
                            "checked=%s current=%s repaired=%s failed=%s",
                            result.checked,
                            result.current,
                            result.repaired,
                            result.failed,
                        )
                except Exception:
                    logger.exception(
                        "Vault Master sidecar reconciliation could not run"
                    )
            next_sidecar_reconciliation = (
                time.monotonic() + sidecar_reconciliation_seconds
            )

        if processed is None and time.monotonic() >= next_federation_delivery:
            try:
                federation = FederationStore(get_database_conninfo())
                await asyncio.to_thread(federation.backfill_active_metadata)
                await asyncio.to_thread(federation.retry_stuck_deliveries)
                await asyncio.to_thread(federation.deliver_due)
                await asyncio.to_thread(federation.cleanup_stale_cache)
                await asyncio.to_thread(federation.recover_stale_download_operations)
                if time.monotonic() >= next_federation_reconciliation:
                    await asyncio.to_thread(federation.reconcile_authoritative_state)
                    next_federation_reconciliation = time.monotonic() + federation_reconciliation_seconds
            except Exception:
                logger.exception("Federation delivery worker could not run")
            next_federation_delivery = time.monotonic() + 15

        if processed is None and time.monotonic() >= next_storage_integration_reconciliation:
            try:
                reconciled = await asyncio.to_thread(reconcile_pending_slot_integrations)
                if reconciled:
                    logger.info("Storage slot integrations reconciled: count=%s", reconciled)
            except Exception:
                # Keep the root-owned slot in its explicit recovery state.
                # The next worker pass may retry only the same durable request.
                logger.exception("Storage slot integration reconciliation failed")
            next_storage_integration_reconciliation = time.monotonic() + 15

        if processed is None:
            await asyncio.sleep(poll_seconds)


def bootstrap_application_schema() -> None:
    """Perform all additive PostgreSQL schema work before serving requests.

    Store constructors and dependency getters deliberately have no schema side
    effects.  This is the sole normal-process bootstrap path, independent of
    optional worker scheduling.
    """
    # Validate the security-sensitive deployment boundary before serving.
    trusted_proxy_networks()
    get_webauthn_origin()
    authentication = get_authentication_store()
    if not isinstance(authentication, PostgresAuthenticationStore):
        raise RuntimeError("Backend schema bootstrap requires PostgreSQL authentication storage")
    authentication.initialize()
    passkeys = get_passkey_store()
    if not isinstance(passkeys, PostgresPasskeyStore):
        raise RuntimeError("Backend schema bootstrap requires PostgreSQL passkey storage")
    passkeys.initialize()
    enrolment = get_enrolment_store()
    enrolment.initialize()

    stores = (
        get_vault_master_store(),
        get_gallery_intelligence_store(),
        get_gallery_people_store(),
        get_video_intelligence_store(),
        get_ai_store(),
        get_ingestion_ai_store(),
        get_autopilot_store(),
        get_intake_store(),
        get_reading_room_store(),
        get_publication_review_store(),
    )
    expected_store_types = (
        PostgresVaultMasterStore,
        PostgresGalleryIntelligenceStore,
        PostgresGalleryPeopleStore,
        PostgresVideoIntelligenceStore,
        PostgresAiStore,
        PostgresIngestionAiStore,
        PostgresAutopilotStore,
        PostgresIntakeStore,
        PostgresReadingRoomStore,
        PostgresPublicationReviewStore,
    )
    for store, store_type in zip(stores, expected_store_types, strict=True):
        if not isinstance(store, store_type):
            raise RuntimeError(f"Backend schema bootstrap requires {store_type.__name__}")
        store.initialize()
    PostgresTvShowStore(get_database_conninfo()).initialize()

    vault_store = stores[0]
    assert isinstance(vault_store, PostgresVaultMasterStore)
    arrival_hall_root = os.getenv("PV_ARRIVAL_HALL_PATH")
    if arrival_hall_root:
        vault_store.migrate_source_root(
            INCOMING_SOURCE,
            os.getenv("PV_INCOMING_PATH", "/vault/Incoming"),
            arrival_hall_root,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    # Required application schemas are not worker-owned.  Complete this
    # controlled bootstrap before either worker-enabled or API-only traffic.
    await asyncio.to_thread(bootstrap_application_schema)
    worker_enabled = os.getenv(
        "PV_VAULT_MASTER_WORKER_ENABLED",
        "true",
    ).lower() in {"1", "true", "yes", "on"}
    if not worker_enabled:
        set_worker_state("disabled")
        yield
        return
    worker = asyncio.create_task(run_vault_master_worker())
    set_worker_state("running")
    yield
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    set_worker_state("stopped")

app = FastAPI(
    title="Personal Vault API",
    version=load_project_version(),
    lifespan=lifespan,
)


@app.middleware("http")
async def enforce_browser_request_boundary(request: Request, call_next):
    """Reject host-header and cross-origin cookie abuse before routing.

    Public WebAuthn/health requests remain usable; state changes carrying the
    host-only normal-session cookie must originate from the configured canonical
    origin. This is deliberately a small same-origin defence, not a CSRF token
    framework or a CORS expansion.
    """
    canonical_origin = get_webauthn_origin()
    expected_host = urlsplit(canonical_origin).netloc.casefold()
    host = request.headers.get("host", "").casefold()
    if host != expected_host:
        return JSONResponse({"detail": "Invalid host"}, status_code=400)
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.cookies.get("pv_session")
        and request.headers.get("origin") != canonical_origin
    ):
        return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith(("/api/auth/", "/api/vault-control/")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response
app.include_router(auth_router)
app.include_router(user_state_router)
app.include_router(tv_shows_router)
app.include_router(tv_playback_router)
app.include_router(gallery_router)
app.include_router(people_router)
app.include_router(arrival_hall_router, prefix="/api/arrival-hall")
app.include_router(arrival_hall_router, prefix="/api/incoming")
app.include_router(movies_router)
app.include_router(movie_playback_router)
app.include_router(music_router)
app.include_router(music_playback_router)
app.include_router(reading_room_catalogue_router)
app.include_router(vault_libraries_router)
app.include_router(vault_master_router)
app.include_router(vault_master_intake_router)
app.include_router(vault_master_music_router)
app.include_router(vault_control_router)
app.include_router(vault_control_intake_router)
app.include_router(vault_storage_router)
app.include_router(vault_services_router)
app.include_router(vault_control_users_router)


@app.get("/api/health")
def health_check(
    store: AuthenticationStore = Depends(get_authentication_store),
) -> dict[str, str]:
    store.healthcheck()

    return {
        "status": "ok",
        "service": "pv-backend",
        "database": "ok",
        **build_info(),
    }
