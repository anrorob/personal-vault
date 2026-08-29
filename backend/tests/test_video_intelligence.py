from dataclasses import replace
from datetime import datetime, timezone
import inspect
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import queue_video_intelligence_for_published_asset
from app.video_intelligence import (
    MemoryVideoIntelligenceStore,
    VIDEO_RECONCILIATION_VERSION,
    VideoAnalysisFrame,
    VideoFrameEvidence,
    VideoReconciliationResult,
    _narrative_from_persisted_evidence,
    process_next_video_analysis_job,
    reconcile_video_analysis_job,
    select_deterministic_frame_positions,
)
from app.gallery_intelligence import MemoryGalleryIntelligenceStore
from app.gallery_people import MemoryGalleryPeopleStore
from app.vault_libraries import get_personal_videos_path
from app.vault_master import ImportItem, MemoryVaultMasterStore
from tests.conftest import TEST_USERNAME
from tests.test_vault_libraries import authenticate, catalogue_file


def test_sampling_is_deterministic_bounded_and_keeps_temporal_reasons() -> None:
    scenes = (5_000, 25_000, 45_000, 65_000, 85_000)
    first = select_deterministic_frame_positions(
        120_000, scene_change_timestamps_ms=scenes
    )
    second = select_deterministic_frame_positions(
        120_000, scene_change_timestamps_ms=scenes
    )
    assert first == second
    assert len(first) <= 18
    assert first[0] == (0, "start")
    assert first[-1][1] == "end"
    assert all(
        second[index][0] > first[index - 1][0]
        for index in range(1, len(first))
    )
    assert any(reason == "scene_change" for _, reason in first)


def test_very_short_video_keeps_start_midpoint_and_end() -> None:
    positions = select_deterministic_frame_positions(3_300)
    assert positions == ((0, "start"), (1_650, "midpoint"), (3_050, "end"))
    assert positions[-1][0] < 3_300
    assert positions[-1][0] >= 3_000


def test_short_video_keeps_distinct_meaningful_anchors_where_possible() -> None:
    assert select_deterministic_frame_positions(20) == (
        (0, "start"), (10, "midpoint"), (18, "end"),
    )


def test_memory_job_prevents_duplicate_active_work_and_allows_reanalysis() -> None:
    store = MemoryVideoIntelligenceStore()
    asset_id = uuid4()
    owner_user_id = uuid4()
    first = store.queue(asset_id, TEST_USERNAME, owner_user_id)
    assert store.queue(asset_id, TEST_USERNAME, owner_user_id).id == first.id
    store.transition(first.id, "completed")
    assert store.queue(asset_id, TEST_USERNAME, owner_user_id).id == first.id
    retried = store.queue(asset_id, TEST_USERNAME, owner_user_id, reanalyse=True)
    assert retried.id != first.id
    assert retried.requested_reanalysis is True


def _published_item_for_asset(
    vault: MemoryVaultMasterStore, asset, *, category: str | None = None,
    state: str = "moved",
) -> ImportItem:
    item = ImportItem(
        id=uuid4(),
        batch_id=uuid4(),
        source_kind="incoming",
        source_path=f"/vault/Arrival Hall/{asset.filename}",
        relative_path=asset.filename,
        filename=asset.filename,
        size_bytes=asset.size_bytes,
        mime_type=asset.mime_type,
        modified_at=datetime.now(timezone.utc),
        sha256=asset.sha256,
        state=state,
        duplicate_of_id=None,
        proposed_category=category or asset.asset_type,
        proposed_destination=asset.vault_path,
        proposal_reason="test",
        proposal_confidence="test",
        metadata={},
        metadata_overrides={},
        owner_username=asset.owner_username,
    )
    vault.items[item.source_path] = item
    return item


def test_post_publication_hook_queues_one_new_home_video_only(tmp_path: Path) -> None:
    vault = MemoryVaultMasterStore()
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    asset = catalogue_file(
        vault, videos, source, asset_type="Home Videos",
        vault_root="/vault/Home Videos", mime_type="video/mp4",
    )
    item = _published_item_for_asset(vault, asset)
    intelligence = MemoryVideoIntelligenceStore()

    assert queue_video_intelligence_for_published_asset(vault, intelligence, item.id)
    first = intelligence.latest_job(asset.id)
    assert first is not None
    assert first.status == "queued"
    assert first.requested_by == TEST_USERNAME
    assert not first.requested_reanalysis
    # A user pressing Analyse/Reanalyse immediately after publication reuses
    # the active automatic job rather than creating duplicate work.
    assert intelligence.queue(asset.id, TEST_USERNAME, asset.owner_user_id, reanalyse=True).id == first.id
    assert not queue_video_intelligence_for_published_asset(vault, intelligence, item.id)
    assert len(intelligence.jobs) == 1
    intelligence.transition(first.id, "completed")
    assert not queue_video_intelligence_for_published_asset(vault, intelligence, item.id)
    assert len(intelligence.jobs) == 1
    assert source.read_bytes() == b"original"


def test_post_publication_hook_skips_non_home_video_and_historical_assets(tmp_path: Path) -> None:
    vault = MemoryVaultMasterStore()
    videos = tmp_path / "videos"
    gallery = tmp_path / "Gallery"
    videos.mkdir()
    gallery.mkdir()
    historical_source = videos / "already-there.mp4"
    historical_source.write_bytes(b"historical")
    historical = catalogue_file(
        vault, videos, historical_source, asset_type="Home Videos",
        vault_root="/vault/Home Videos", mime_type="video/mp4",
    )
    image = gallery / "photo.jpg"
    image.write_bytes(b"image")
    non_video = catalogue_file(
        vault, gallery, image, asset_type="Gallery", vault_root="/vault/Gallery",
        mime_type="image/jpeg",
    )
    item = _published_item_for_asset(vault, non_video)
    intelligence = MemoryVideoIntelligenceStore()

    assert not queue_video_intelligence_for_published_asset(vault, intelligence, item.id)
    assert not intelligence.jobs
    assert intelligence.latest_job(historical.id) is None


def test_post_publication_hook_rejects_held_or_failed_items(tmp_path: Path) -> None:
    vault = MemoryVaultMasterStore()
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    asset = catalogue_file(
        vault, videos, source, asset_type="Home Videos",
        vault_root="/vault/Home Videos", mime_type="video/mp4",
    )
    intelligence = MemoryVideoIntelligenceStore()
    held = _published_item_for_asset(vault, asset, state="approved")
    assert not queue_video_intelligence_for_published_asset(vault, intelligence, held.id)

    failed = _published_item_for_asset(vault, asset, state="move_failed")
    assert not queue_video_intelligence_for_published_asset(vault, intelligence, failed.id)
    assert not intelligence.jobs


def test_post_publication_queue_failure_does_not_undo_home_video_publication(tmp_path: Path) -> None:
    class FailingVideoStore(MemoryVideoIntelligenceStore):
        def queue(self, *args, **kwargs):
            raise RuntimeError("local video queue unavailable")

    vault = MemoryVaultMasterStore()
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    asset = catalogue_file(
        vault, videos, source, asset_type="Home Videos",
        vault_root="/vault/Home Videos", mime_type="video/mp4",
    )
    item = _published_item_for_asset(vault, asset)

    assert not queue_video_intelligence_for_published_asset(vault, FailingVideoStore(), item.id)
    assert vault.get_catalogued_asset_by_id(asset.id) == asset
    assert any(
        entry["action"] == "video_intelligence_auto_queue_failed"
        for entry in vault.asset_history
    )
    assert source.read_bytes() == b"original"


def test_v5_hook_is_narrowly_post_publication_and_never_sweeps_history() -> None:
    from app.main import run_vault_master_worker

    hook_source = inspect.getsource(queue_video_intelligence_for_published_asset)
    worker_source = inspect.getsource(run_vault_master_worker)

    assert "list_owned_catalogued_assets" not in hook_source
    assert "assess_destination" not in hook_source
    assert "queue_video_intelligence_for_published_asset" in worker_source
    assert worker_source.index("if moved is not None:") < worker_source.index(
        "queue_video_intelligence_for_published_asset"
    )


def test_frame_evidence_and_reconciliation_remain_time_linked() -> None:
    store = MemoryVideoIntelligenceStore()
    job = store.queue(uuid4(), TEST_USERNAME, uuid4())
    frames = store.add_frames(job.id, ((0, "start"), (20_000, "interval")))
    assert [(frame.ordinal, frame.timestamp_ms) for frame in frames] == [(1, 0), (2, 20_000)]
    evidence = VideoFrameEvidence(
        uuid4(), frames[1].id, "florence", "florence-2-large", "revision", "video-frame-v1", {"caption": "coast"}, {"caption": "coast"}, 1_000, frames[1].created_at
    )
    store.save_evidence(evidence)
    assert store.evidence_for_frame(frames[1].id) == [evidence]
    result = VideoReconciliationResult(uuid4(), job.asset_id, job.id, "A coastal outing.", "video-reconciliation-v1", ("partial",), {"frame_ids": [str(frames[1].id)]}, frames[1].created_at)
    store.save_reconciliation(result)
    assert store.reconciliations[job.asset_id].warnings == ("partial",)
    assert store.reconciliations[job.asset_id].generated_narrative == "A coastal outing."


def _configure_video_api(
    tmp_path: Path, monkeypatch
) -> tuple[MemoryVaultMasterStore, MemoryVideoIntelligenceStore, Path]:
    from app.main import app
    import app.vault_libraries as libraries

    videos = tmp_path / "videos"
    videos.mkdir()
    store = MemoryVaultMasterStore()
    intelligence = MemoryVideoIntelligenceStore()
    metadata = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    app.dependency_overrides[get_personal_videos_path] = lambda: videos
    from app.vault_master import get_vault_master_store
    app.dependency_overrides[get_vault_master_store] = lambda: store
    monkeypatch.setattr(libraries, "get_video_intelligence_store", lambda: intelligence)
    monkeypatch.setattr(libraries, "get_gallery_intelligence_store", lambda: metadata)
    monkeypatch.setattr(libraries, "get_gallery_people_store", lambda: people)
    return store, intelligence, videos


def test_owner_only_details_and_single_video_queue(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    store, intelligence, videos = _configure_video_api(tmp_path, monkeypatch)
    source = videos / "clip.mp4"
    source.write_bytes(b"unchanged-video-bytes")
    asset = catalogue_file(store, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    original = source.read_bytes()
    authenticate(client)

    listing = client.get("/api/personal-videos").json()
    details = client.get(f"/api/personal-videos/{listing[0]['id']}/details")
    assert details.status_code == 200
    assert details.json()["asset_id"] == str(asset.id)
    assert "asset_id" not in listing[0]

    queued = client.post("/api/personal-videos/intelligence/jobs", json={"asset_id": str(asset.id)})
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert client.post("/api/personal-videos/intelligence/jobs", json={"asset_id": str(asset.id)}).json()["id"] == queued.json()["id"]
    assert intelligence.latest_job(asset.id).id.hex == queued.json()["id"].replace("-", "")
    intelligence.transition(intelligence.latest_job(asset.id).id, "analysis_complete")
    reconciled = client.post("/api/personal-videos/intelligence/reconcile", json={"asset_id": str(asset.id)})
    assert reconciled.status_code == 202
    assert reconciled.json()["status"] == "completed_with_warnings"
    assert source.read_bytes() == original


def test_video_queue_rejects_other_owner_asset(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    store, _, videos = _configure_video_api(tmp_path, monkeypatch)
    source = videos / "private.mp4"
    source.write_bytes(b"private")
    asset = catalogue_file(store, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4", owner_username="someone-else")
    authenticate(client)
    response = client.post("/api/personal-videos/intelligence/jobs", json={"asset_id": str(asset.id)})
    assert response.status_code == 404


def test_video_foundation_has_no_arrival_hall_or_routing_dependency() -> None:
    source = (Path(__file__).parents[1] / "app" / "video_intelligence.py").read_text(
        encoding="utf-8"
    )
    assert "assess_destination" not in source
    assert "arrival_hall" not in source


def test_worker_persists_independent_frame_evidence_and_cleans_cache(
    tmp_path: Path, monkeypatch
) -> None:
    import app.gallery_intelligence as gallery_intelligence
    import app.gallery_people_worker as people_worker
    import app.vault_master_ingestion_ai as ingestion_ai
    import app.video_intelligence as video

    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    original = b"original-video-bytes"
    source.write_bytes(original)
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    store = MemoryVideoIntelligenceStore()
    job = store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    monkeypatch.setenv("PV_PERSONAL_VIDEOS_PATH", str(videos))
    monkeypatch.setenv("PV_VIDEO_ANALYSIS_CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(video, "probe_video_duration_ms", lambda _: 42_000)
    monkeypatch.setattr(video, "_scene_candidates", lambda *_: (21_000,))
    def write_frame(_source, _time, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"frame")
    monkeypatch.setattr(video, "extract_frame", write_frame)
    monkeypatch.setattr(ingestion_ai, "request_florence_analysis", lambda _: ("A lake.", "detailed-caption", 12))
    monkeypatch.setattr(gallery_intelligence, "request_rampp_tags", lambda _: type("Result", (), {"provider": "rampp", "model_id": "ram_plus", "model_revision": "v1", "task_version": "rampp-v1", "raw_response": '["lake"]', "processing_ms": 9})())
    monkeypatch.setattr(people_worker, "request_people_analysis", lambda *_: {"task_version": "gallery-people-v2", "body": {"boxes": []}, "faces": {"boxes": [{"recognition_result": "unknown"}]}})

    assert process_next_video_analysis_job(store, vault, type("People", (), {"reference_embeddings": lambda *_: []})()) == job.id
    completed = store.latest_job(asset.id)
    assert completed is not None and completed.status == "analysis_complete"
    frames = store.frames_for_job(job.id)
    assert len(frames) >= 3
    assert all(frame.status == "completed" for frame in frames)
    assert all(len(store.evidence_for_frame(frame.id)) == 3 for frame in frames)
    assert not (tmp_path / "cache" / str(job.id)).exists()
    assert source.read_bytes() == original


def test_worker_retains_partial_specialist_failure(tmp_path: Path, monkeypatch) -> None:
    import app.gallery_intelligence as gallery_intelligence
    import app.gallery_people_worker as people_worker
    import app.vault_master_ingestion_ai as ingestion_ai
    import app.video_intelligence as video

    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    store = MemoryVideoIntelligenceStore()
    job = store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    monkeypatch.setenv("PV_PERSONAL_VIDEOS_PATH", str(videos))
    monkeypatch.setenv("PV_VIDEO_ANALYSIS_CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(video, "probe_video_duration_ms", lambda _: 3_300)
    monkeypatch.setattr(video, "_scene_candidates", lambda *_: ())
    def write_frame(_source, _time, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"frame")
    monkeypatch.setattr(video, "extract_frame", write_frame)
    monkeypatch.setattr(ingestion_ai, "request_florence_analysis", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(gallery_intelligence, "request_rampp_tags", lambda _: type("Result", (), {"provider": "rampp", "model_id": "ram_plus", "model_revision": "v1", "task_version": "rampp-v1", "raw_response": '[]', "processing_ms": 9})())
    monkeypatch.setattr(people_worker, "request_people_analysis", lambda *_: {"task_version": "gallery-people-v2", "body": {"boxes": []}, "faces": {"boxes": []}})

    process_next_video_analysis_job(store, vault, type("People", (), {"reference_embeddings": lambda *_: []})())
    completed = store.latest_job(asset.id)
    assert completed is not None and completed.status == "analysis_complete"
    assert "florence failed" in (completed.warning or "")
    assert all(len(store.evidence_for_frame(frame.id)) == 3 for frame in store.frames_for_job(job.id))


def test_worker_retains_bounded_ffmpeg_diagnostic_and_cleans_cache(tmp_path: Path, monkeypatch) -> None:
    import app.video_intelligence as video

    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    store = MemoryVideoIntelligenceStore()
    job = store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    monkeypatch.setenv("PV_PERSONAL_VIDEOS_PATH", str(videos))
    monkeypatch.setenv("PV_VIDEO_ANALYSIS_CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(video, "probe_video_duration_ms", lambda _: 3_300)
    monkeypatch.setattr(video, "_scene_candidates", lambda *_: ())

    def fail_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(234, ["ffmpeg"], stderr=("decoder error " * 200))

    monkeypatch.setattr(video.subprocess, "run", fail_run)
    process_next_video_analysis_job(store, vault, type("People", (), {"reference_embeddings": lambda *_: []})())
    completed = store.latest_job(asset.id)
    assert completed is not None and completed.status == "analysis_complete"
    assert completed.frames_failed == 3
    assert "decoder error" in (completed.warning or "")
    assert len(completed.warning or "") < 3_000
    assert not (tmp_path / "cache" / str(job.id)).exists()


def test_reconciliation_uses_persisted_evidence_in_chronological_order(tmp_path: Path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    video_store = MemoryVideoIntelligenceStore()
    people_store = MemoryGalleryPeopleStore()
    metadata_store = MemoryGalleryIntelligenceStore()
    owner = people_store.create_person(TEST_USERNAME, "Owner")
    job = video_store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    frames = video_store.add_frames(job.id, ((0, "start"), (1_000, "midpoint"), (2_000, "end")))
    for frame, caption in zip(frames, ("A man beside a motorcycle.", "A man riding a motorcycle.", "A coastal road.")):
        video_store.update_frame(frame.id, "completed")
        video_store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "florence", "florence-2-large", None, "caption", {"description": caption}, {"description": caption}, 1, frame.created_at))
        video_store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "rampp", "ram++", "r1", "tag", {"tags": ["motorcycle"]}, {"tags": ["motorcycle"]}, 1, frame.created_at))
        video_store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "people", "people", None, "people", {}, {"faces": {"boxes": [{"recognition_result": "known", "candidate_person_id": str(owner.id)}]}}, 1, frame.created_at))
    video_store.transition(job.id, "analysis_complete")

    assert reconcile_video_analysis_job(video_store, vault, people_store, metadata_store, job.id) == job.id
    result = video_store.latest_reconciliation(asset.id)
    assert result is not None
    assert result.reconciliation_version == VIDEO_RECONCILIATION_VERSION
    assert result.generated_narrative is not None
    assert "Owner" in result.generated_narrative
    assert "riding motorcycles" in result.generated_narrative
    assert [person.person_id for person in people_store.effective_people(asset.id, TEST_USERNAME)] == [owner.id]
    assert {(term.namespace, term.slug) for term in metadata_store.effective(asset.id)} == {("content_tag", "motorcycle")}
    assert video_store.latest_job(asset.id).status == "completed"
    assert result.evidence_references["florence_frame_ids"] == [str(frame.id) for frame in frames]
    assert source.read_bytes() == b"original"


def test_reconciliation_respects_exclusion_and_completes_with_warnings(tmp_path: Path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    video_store = MemoryVideoIntelligenceStore()
    people_store = MemoryGalleryPeopleStore()
    metadata_store = MemoryGalleryIntelligenceStore()
    owner = people_store.create_person(TEST_USERNAME, "Owner")
    people_store.decide(asset.id, owner.id, "exclude", TEST_USERNAME)
    job = video_store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    frame = video_store.add_frames(job.id, ((0, "start"),))[0]
    video_store.update_frame(frame.id, "failed", error="Florence unavailable")
    video_store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "people", "people", None, "people", {}, {"faces": {"boxes": [{"recognition_result": "known", "candidate_person_id": str(owner.id)}]}}, 1, frame.created_at))
    video_store.transition(job.id, "analysis_complete")

    reconcile_video_analysis_job(video_store, vault, people_store, metadata_store, job.id)
    result = video_store.latest_reconciliation(asset.id)
    assert result is not None and result.generated_narrative is None
    assert people_store.effective_people(asset.id, TEST_USERNAME) == []
    assert video_store.latest_job(asset.id).status == "completed_with_warnings"
    assert "Frame analysis failed" in (video_store.latest_job(asset.id).warning or "")
    assert people_store.list_people(TEST_USERNAME) == [owner]


def _narrative_fixture(captions: tuple[str, ...], tags: tuple[tuple[str, ...], ...], people_names: list[str] | None = None) -> str | None:
    now = datetime.now(timezone.utc)
    asset_id = uuid4()
    job_id = uuid4()
    frames = [
        VideoAnalysisFrame(uuid4(), job_id, asset_id, index * 1_000, index + 1, "interval", "completed", None, None, None, now, now)
        for index in range(len(captions))
    ]
    evidence: list[VideoFrameEvidence] = []
    for frame, caption, frame_tags in zip(frames, captions, tags):
        evidence.append(VideoFrameEvidence(uuid4(), frame.id, "florence", "florence-2-large", None, "caption", {"description": caption}, {"description": caption}, 1, now))
        evidence.append(VideoFrameEvidence(uuid4(), frame.id, "rampp", "ram++", None, "tag", {"tags": list(frame_tags)}, {"tags": list(frame_tags)}, 1, now))
    return _narrative_from_persisted_evidence(frames, evidence, people_names or [])


def test_v31_compresses_overlapping_kayak_observations() -> None:
    narrative = _narrative_fixture(
        (
            "The image shows a man and a woman in a yellow kayak on a calm lake. They are wearing orange life jackets and using blue paddles. Trees, a rocky cliff and tents are along the shore.",
            "This image shows two people in a yellow kayak on a calm lake, wearing life jackets and using blue paddles. Trees and a campsite are on the shore.",
            "The image shows two people kayaking on a calm lake in a yellow kayak with blue paddles and life jackets. Trees and cliffs are in the background.",
        ),
        (("kayak", "lake", "life jacket", "paddle", "person"),) * 3,
    )
    assert narrative is not None
    assert narrative.count("kayaking") == 1
    assert "two people" in narrative
    assert "calm lake" in narrative
    assert "followed by" not in narrative
    assert "ending with" not in narrative
    assert "the image shows" not in narrative.casefold()


def test_v31_keeps_real_scene_transitions_concise() -> None:
    narrative = _narrative_fixture(
        ("The image shows people at the beach.", "The image shows people riding motorcycles on a coastal road.", "The image shows people at a restaurant."),
        (("beach", "person"), ("motorcycle", "riding", "person"), ("restaurant", "person")),
    )
    assert narrative is not None
    assert "then" in narrative and "beach" in narrative and "riding motorcycles" in narrative and "restaurant" in narrative


def test_v31_uses_effective_people_without_demographic_caption_wording() -> None:
    narrative = _narrative_fixture(
        ("The image shows a man and a woman in a yellow kayak on a calm lake.",) * 3,
        (("kayak", "lake", "person"),) * 3,
        ["Tym", "Ela"],
    )
    assert narrative is not None
    assert "Tym and Ela" in narrative
    assert "man and a woman" not in narrative


def test_v31_uses_one_known_person_with_an_unidentified_companion() -> None:
    narrative = _narrative_fixture(
        ("The image shows two people in a kayak on a lake.",) * 3,
        (("kayak", "lake", "person"),) * 3,
        ["Tym"],
    )
    assert narrative is not None
    assert "Tym with another person" in narrative


def test_v31_reconciliation_refresh_preserves_old_result_and_never_calls_specialists(tmp_path: Path, monkeypatch) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    store = MemoryVideoIntelligenceStore()
    people_store = MemoryGalleryPeopleStore()
    metadata_store = MemoryGalleryIntelligenceStore()
    job = store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    frames = store.add_frames(job.id, ((0, "start"), (1_000, "midpoint"), (2_000, "end")))
    for frame in frames:
        store.update_frame(frame.id, "completed")
        store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "florence", "florence", None, "caption", {"description": "The image shows two people kayaking on a calm lake."}, {"description": "The image shows two people kayaking on a calm lake."}, 1, frame.created_at))
        store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "rampp", "rampp", None, "tag", {"tags": ["kayak", "lake", "person"]}, {"tags": ["kayak", "lake", "person"]}, 1, frame.created_at))
    store.transition(job.id, "analysis_complete")
    assert reconcile_video_analysis_job(store, vault, people_store, metadata_store, job.id) == job.id
    original = store.latest_reconciliation(asset.id)
    assert original is not None
    assert reconcile_video_analysis_job(store, vault, people_store, metadata_store, job.id, refresh=True) == job.id
    updated = store.latest_reconciliation(asset.id)
    assert updated is not None and updated.id != original.id
    assert len(store.reconciliation_history[job.id]) == 2
    assert source.read_bytes() == b"original"


def test_v31_refresh_keeps_a_user_narrative_effective(tmp_path: Path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    vault = MemoryVaultMasterStore()
    asset = catalogue_file(vault, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    vault.catalogued_assets[asset.vault_path] = replace(asset, user_overrides={"video_narrative": "Owner's own description."})
    store = MemoryVideoIntelligenceStore()
    people_store = MemoryGalleryPeopleStore()
    metadata_store = MemoryGalleryIntelligenceStore()
    job = store.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    frame = store.add_frames(job.id, ((0, "start"),))[0]
    store.update_frame(frame.id, "completed")
    store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "florence", "florence", None, "caption", {"description": "The image shows a kayak on a lake."}, {"description": "The image shows a kayak on a lake."}, 1, frame.created_at))
    store.transition(job.id, "analysis_complete")
    reconcile_video_analysis_job(store, vault, people_store, metadata_store, job.id)
    assert store.latest_reconciliation(asset.id).generated_narrative is not None  # type: ignore[union-attr]
    assert vault.catalogued_assets[asset.vault_path].user_overrides["video_narrative"] == "Owner's own description."


def test_v31_reconciliation_has_no_specialist_or_frame_extraction_dependency() -> None:
    source = inspect.getsource(reconcile_video_analysis_job)
    for forbidden in ("extract_frame", "request_florence_analysis", "request_rampp_tags", "request_people_analysis"):
        assert forbidden not in source


def test_v4_video_metadata_controls_use_canonical_asset_authority(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    import app.vault_libraries as libraries

    store, intelligence, videos = _configure_video_api(tmp_path, monkeypatch)
    source = videos / "clip.mp4"
    source.write_bytes(b"original")
    asset = catalogue_file(store, videos, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    people_store = libraries.get_gallery_people_store()
    metadata_store = libraries.get_gallery_intelligence_store()
    owner = people_store.create_person(TEST_USERNAME, "Owner")
    tym = people_store.create_person(TEST_USERNAME, "Tym")
    people_store.associate(asset.id, owner.id, "vault_master")
    metadata_store.persist_canonical_assignments(asset.id, (("content_tag", "motorcycle"),), model_id="rampp", model_revision=None, task_version="test")
    authenticate(client)
    video_id = client.get("/api/personal-videos").json()[0]["id"]

    details = client.get(f"/api/personal-videos/{video_id}/details")
    assert details.status_code == 200
    assert details.json()["analysis"] is None
    assert [value["display_name"] for value in details.json()["people"]] == ["Owner"]
    assert [value["slug"] for value in details.json()["content_tags"]] == ["motorcycle"]

    assert client.patch(f"/api/personal-videos/intelligence/{asset.id}/people", json={"person_id": str(owner.id), "decision": "exclude"}).status_code == 200
    assert client.patch(f"/api/personal-videos/intelligence/{asset.id}/people", json={"person_id": str(tym.id), "decision": "include"}).status_code == 200
    assert client.patch(f"/api/personal-videos/intelligence/{asset.id}/tags", json={"namespace": "content_tag", "slug": "motorcycle", "decision": "exclude"}).status_code == 200
    assert client.patch(f"/api/personal-videos/intelligence/{asset.id}/tags", json={"namespace": "content_tag", "slug": "outdoors", "decision": "include"}).status_code == 200
    narrative = client.patch(f"/api/personal-videos/intelligence/{asset.id}/narrative", json={"narrative": "A user-edited video description."})
    assert narrative.status_code == 200
    updated = client.get(f"/api/personal-videos/{video_id}/details").json()
    assert updated["narrative"] == "A user-edited video description."
    assert updated["narrative_source"] == "user"
    assert [value["display_name"] for value in updated["people"]] == ["Tym"]
    assert [value["slug"] for value in updated["content_tags"]] == ["outdoors"]

    job = intelligence.queue(asset.id, TEST_USERNAME, asset.owner_user_id)
    assert client.post("/api/personal-videos/intelligence/jobs", json={"asset_id": str(asset.id)}).json()["id"] == str(job.id)
    assert client.post("/api/personal-videos/intelligence/jobs", json={"asset_id": str(asset.id), "reanalyse": True}).status_code == 202
    assert source.read_bytes() == b"original"
