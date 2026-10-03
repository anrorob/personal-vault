from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.gallery_florence import GalleryFlorenceJob, gallery_source, process_next_gallery_florence_job
from tests.test_gallery_intelligence import gallery_asset


class FakeFlorenceStore:
    def __init__(self, job): self.job = job; self.completed = []; self.failed = []
    def claim_next_job(self):
        value, self.job = self.job, None
        return value
    def complete(self, job, caption, text, elapsed): self.completed.append((job, caption, text, elapsed))
    def fail(self, job_id, error): self.failed.append((job_id, str(error)))


def test_published_asset_florence_recovery_uses_current_canonical_path(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    current = replace(asset, vault_path="/vault/Gallery/renamed.jpg", filename="renamed.jpg", user_overrides={"display_title": "Owner title"})
    source = tmp_path / "Gallery" / "renamed.jpg"
    (tmp_path / "Gallery" / "holiday.jpg").rename(source)
    vault.catalogued_assets = {current.vault_path: current}
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.vault_master_ingestion_ai.request_florence_analysis", lambda _: ("Recovered caption", "", 7))
    job = GalleryFlorenceJob(uuid4(), current.id, current.owner_user_id, "owner", "queued", 0, None, datetime.now(timezone.utc))
    store = FakeFlorenceStore(job)
    assert process_next_gallery_florence_job(store, vault) == job.id
    assert store.completed[0][0].asset_id == current.id
    assert store.completed[0][1] == "Recovered caption"
    assert vault.get_catalogued_asset_by_id(current.id).user_overrides == {"display_title": "Owner title"}


def test_florence_recovery_failure_is_recorded_not_silently_completed(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.vault_master_ingestion_ai.request_florence_analysis", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    job = GalleryFlorenceJob(uuid4(), asset.id, asset.owner_user_id, "owner", "queued", 0, None, datetime.now(timezone.utc))
    store = FakeFlorenceStore(job)
    process_next_gallery_florence_job(store, vault)
    assert store.completed == [] and store.failed == [(job.id, "offline")]
