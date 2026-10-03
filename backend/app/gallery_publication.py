"""One owner-scoped asynchronous enrichment path after Gallery publication."""
import logging

from app.gallery_florence import GALLERY_FLORENCE_TASK_VERSION
from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION

logger = logging.getLogger(__name__)


def current_florence(evidence):
    return bool(evidence and evidence.caption.strip()
                and evidence.model_id == AI_MODEL_ID
                and evidence.model_revision == AI_MODEL_REVISION
                and evidence.task_version in {INGESTION_TASK_VERSION, GALLERY_FLORENCE_TASK_VERSION})


def queue_missing_gallery_florence(vault_store, ingestion_store, florence_store, asset, username):
    """Shared publication/backfill predicate; never authorize publication here."""
    if (asset is None or asset.asset_type != "Gallery" or asset.owner_user_id is None
            or not asset.vault_path.startswith("/vault/Gallery/")):
        return False
    if current_florence(florence_store.latest_evidence(asset.id, asset.owner_user_id)):
        return False
    from app.gallery_reconciliation import published_source_items
    if ingestion_store is not None and any(
        current_florence(evidence)
        for item in published_source_items(vault_store, asset)
        for evidence in ingestion_store.list_evidence(item.id, asset.owner_user_id)
    ):
        return False
    job = florence_store.active_or_latest_job(asset.id)
    if job is not None and job.status in {"queued", "processing"}:
        return False
    # Failed jobs are retried only by an explicit backfill or this publication
    # event; the idle worker never continuously requeues failures.
    return florence_store.queue(asset.id, asset.owner_user_id, str(username)) is not None


def queue_gallery_publication(vault_store, gallery_store, ingestion_store, florence_store, asset, username):
    if (asset is None or asset.asset_type != "Gallery" or asset.owner_user_id is None
            or not asset.vault_path.startswith("/vault/Gallery/")):
        return False
    # Separate failures: a provider/queue outage cannot suppress another stage
    # and cannot reverse an already durable canonical publication.
    for stage, queue in (
        ("florence", lambda: queue_missing_gallery_florence(
            vault_store, ingestion_store, florence_store, asset, username)),
        ("gallery", lambda: gallery_store.queue(asset.id, username)),
    ):
        try:
            queue()
        except Exception:
            logger.exception("Post-publication %s queue failed: asset_id=%s", stage, asset.id)
    return True
