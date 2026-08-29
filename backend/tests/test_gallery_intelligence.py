import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.gallery_intelligence import (
    BULK_COMPLETION_GRACE,
    GalleryConcept,
    GalleryIntelligenceClassification,
    MemoryGalleryIntelligenceStore,
    normalise_rampp_tags,
    process_next_gallery_intelligence_job,
    queue_published_gallery_assets,
    resolve_gallery_concepts,
)
from app.gallery_people import MemoryGalleryPeopleStore
from app.main import queue_gallery_intelligence_for_published_asset
from app.vault_master import CataloguedAsset, ImportItem, MemoryVaultMasterStore
from app.vault_master_ingestion_ai import IngestionAiEvidence, MemoryIngestionAiStore


def gallery_asset(tmp_path: Path) -> tuple[MemoryVaultMasterStore, CataloguedAsset]:
    gallery = tmp_path / "Gallery"
    gallery.mkdir()
    image = gallery / "holiday.jpg"
    image.write_bytes(b"original-image-bytes")
    asset = CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="Holiday", captured_on=date(2024, 1, 1),
        location=None, vault_path="/vault/Gallery/holiday.jpg", filename="holiday.jpg", size_bytes=image.stat().st_size,
        mime_type="image/jpeg", sha256="a" * 64, metadata={}, metadata_provenance={}, owner_username="owner",
    )
    vault = MemoryVaultMasterStore()
    vault.catalogued_assets[asset.vault_path] = asset
    return vault, asset


def retained_florence_description(
    vault: MemoryVaultMasterStore,
    asset: CataloguedAsset,
    caption: str,
    *,
    created_at: datetime | None = None,
) -> MemoryIngestionAiStore:
    """Create the authoritative moved-item Florence evidence used by Gallery."""
    ingestion = MemoryIngestionAiStore()
    item = ImportItem(
        id=uuid4(), batch_id=uuid4(), source_kind="incoming",
        source_path=f"/vault/Incoming/{asset.filename}", relative_path=asset.filename,
        filename=asset.filename, size_bytes=asset.size_bytes, mime_type=asset.mime_type,
        modified_at=datetime.now(timezone.utc), sha256=asset.sha256, state="moved",
        duplicate_of_id=None, proposed_category="Gallery", proposed_destination=asset.vault_path,
        proposal_reason="published", proposal_confidence="high", metadata={}, metadata_overrides={},
        owner_username=asset.owner_username,
    )
    vault.items[item.source_path] = item
    evidence = IngestionAiEvidence(
        id=uuid4(), job_id=uuid4(), item_id=item.id, content_type="personal_photo",
        caption=caption, ocr_text="", confidence=0.9, reasons=(),
        model_id="microsoft/Florence-2-large", model_revision="florence-revision",
        task_version="arrival-image-analysis-v1", processing_ms=12,
        recommended_destination="/vault/Gallery", decision_score=85,
        routing_band="automatic_eligible", confidence_components={}, conflicts=(),
        automatic_disqualifiers=(), decision_model_version="intelligent-routing-v4",
        requested_by=asset.owner_username,
        created_at=created_at or datetime.now(timezone.utc),
    )
    ingestion.evidence[evidence.id] = evidence
    return ingestion


def test_multiple_terms_are_persisted_by_canonical_asset_uuid() -> None:
    store = MemoryGalleryIntelligenceStore()
    asset_id = uuid4()
    job = store.queue(asset_id, "owner")
    assert job.asset_id == asset_id
    claimed = store.claim_next_job()
    assert claimed is not None
    store.complete(claimed.id, (("photo_type", "portrait"), ("photo_type", "vehicle"), ("content_tag", "motorcycle")), 0.01)
    assert {(entry.namespace, entry.slug) for entry in store.effective(asset_id)} == {
        ("photo_type", "portrait"), ("photo_type", "vehicle"), ("content_tag", "motorcycle")
    }


def test_user_exclusion_survives_reanalysis_and_can_be_reversed() -> None:
    store = MemoryGalleryIntelligenceStore()
    asset_id = uuid4()
    first = store.queue(asset_id, "owner")
    store.complete(store.claim_next_job().id, (("photo_type", "landscape"),), 0.99)  # type: ignore[union-attr]
    store.decide(asset_id, "photo_type", "landscape", "exclude")
    # Reanalysis is an explicit owner action; normal publication scans do not
    # retry completed jobs automatically.
    second = store.queue(asset_id, "owner", force=True)
    store.complete(store.claim_next_job().id, (("photo_type", "landscape"),), 0.01)  # type: ignore[union-attr]
    assert store.effective(asset_id) == []
    store.decide(asset_id, "photo_type", "landscape", "include")
    assert [entry.slug for entry in store.effective(asset_id)] == ["landscape"]
    assert first.asset_id == second.asset_id == asset_id


def test_rampp_mapping_uses_emitted_specialist_tags_without_a_pv_threshold() -> None:
    assert normalise_rampp_tags(["selfie", "motorbike", "cat", "park"]) == (
        ("photo_type", "selfie"), ("photo_type", "vehicle"), ("content_tag", "motorcycle"),
        ("photo_type", "animal"), ("content_tag", "animal"), ("content_tag", "cat"), ("content_tag", "outdoors"),
    )
    # Florence prose and raw person tags never manufacture Portrait.
    assert normalise_rampp_tags(["woman", "person"]) == ()
    # A model-provided confidence is persisted for provenance, not evaluated.
    store = MemoryGalleryIntelligenceStore(); asset_id = uuid4(); job = store.queue(asset_id, "owner")
    store.complete(store.claim_next_job().id, (("photo_type", "portrait"),), 0.00001)  # type: ignore[union-attr]
    assert store.effective(asset_id)[0].confidence == 0.00001


def test_rampp_bird_tag_maps_to_animal() -> None:
    assert normalise_rampp_tags(["bird"]) == (
        ("photo_type", "animal"),
        ("content_tag", "animal"),
    )


def test_rampp_stork_tag_maps_to_animal() -> None:
    assert normalise_rampp_tags(["stork"]) == (
        ("photo_type", "animal"),
        ("content_tag", "animal"),
    )


def test_rampp_bird_and_nest_map_to_animal_from_bird_evidence() -> None:
    assert normalise_rampp_tags(["bird", "nest"]) == (
        ("photo_type", "animal"),
        ("content_tag", "animal"),
    )


def test_rampp_nest_alone_does_not_map_to_animal() -> None:
    assert normalise_rampp_tags(["nest"]) == ()


def test_concept_resolver_follows_stork_to_bird_to_animal() -> None:
    assert normalise_rampp_tags(["stork"]) == (
        ("photo_type", "animal"),
        ("content_tag", "animal"),
    )


def test_concept_resolver_accepts_eagle_as_data_without_python_mapping() -> None:
    concepts = (
        GalleryConcept("animal"),
        GalleryConcept("bird", "animal"),
        GalleryConcept("eagle", "bird"),
    )
    terms = (("animal", "photo_type", "animal"),)
    assert resolve_gallery_concepts(["eagle"], concepts, terms) == (("photo_type", "animal"),)


def test_concept_resolver_ignores_unknown_inactive_and_cycles() -> None:
    concepts = (
        GalleryConcept("animal"),
        GalleryConcept("bird", "animal"),
        GalleryConcept("disabled", "animal", active=False),
        GalleryConcept("cycle-a", "cycle-b"),
        GalleryConcept("cycle-b", "cycle-a"),
    )
    terms = (("animal", "photo_type", "animal"), ("cycle-a", "content_tag", "animal"))
    assert resolve_gallery_concepts(["unknown", "disabled"], concepts, terms) == ()
    assert resolve_gallery_concepts(["cycle-a"], concepts, terms) == (("content_tag", "animal"),)


def test_user_exclusion_survives_resolver_reanalysis(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    store = MemoryGalleryIntelligenceStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    store.decide(asset.id, "photo_type", "animal", "exclude", "owner")
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification('["bird"]', "model", "revision", "task", 10),
    )
    store.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(store, vault)
    assert {(entry.namespace, entry.slug) for entry in store.effective(asset.id)} == {
        ("content_tag", "animal")
    }


def test_published_gallery_job_is_post_publication_and_never_mutates_source(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    store = MemoryGalleryIntelligenceStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification(
            '["motorcycle", "park"]', "ram_plus_swin_large_14m", "pinned", "gallery-intelligence-rampp-v1", 10,
        ),
    )
    source = tmp_path / "Gallery" / "holiday.jpg"; original = source.read_bytes()
    assert queue_published_gallery_assets(store, vault, "owner", limit=1) == 1
    assert process_next_gallery_intelligence_job(store, vault) is not None
    assert source.read_bytes() == original
    assert source.name == "holiday.jpg"
    assert {(entry.namespace, entry.slug) for entry in store.effective(asset.id)} == {
        ("photo_type", "vehicle"), ("content_tag", "motorcycle"), ("content_tag", "outdoors")
    }
    evidence = store.evidence[next(iter(store.jobs))]
    assert evidence.task_version == "gallery-intelligence-rampp-v1"
    assert evidence.canonical_assignments == (
        ("photo_type", "vehicle"),
        ("content_tag", "motorcycle"), ("content_tag", "outdoors"),
    )


def test_img_5308_uses_rampp_park_evidence_without_turning_person_into_portrait(
    tmp_path: Path, monkeypatch
) -> None:
    vault, asset = gallery_asset(tmp_path)
    store = MemoryGalleryIntelligenceStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification(
            '["woman", "park", "picnic table"]', "ram_plus_swin_large_14m", "pinned", "gallery-intelligence-rampp-v1", 10,
        ),
    )
    store.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(store, vault)
    assert {(entry.namespace, entry.slug) for entry in store.effective(asset.id)} == {
        ("content_tag", "outdoors"),
    }


def test_person_in_wider_scene_is_not_portrait_without_rampp_supported_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    vault, asset = gallery_asset(tmp_path)
    store = MemoryGalleryIntelligenceStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification(
            '["person", "beach"]', "ram_plus_swin_large_14m", "pinned", "gallery-intelligence-rampp-v1", 10,
        ),
    )
    store.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(store, vault)
    assert {(entry.namespace, entry.slug) for entry in store.effective(asset.id)} == {
        ("content_tag", "beach"), ("content_tag", "outdoors")
    }


def test_people_worker_persists_evidence_and_only_associates_known_faces(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    known = people.create_person("owner", "Known person")
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: GalleryIntelligenceClassification('[]', "rampp", "revision", "task", 1))
    monkeypatch.setattr("app.gallery_people_worker.request_people_analysis", lambda _source, _references: {"task_version": "gallery-people-v2", "body": {"provider": "yolox", "model": "yolox-tiny", "boxes": [{"box": {"x": 1}}]}, "faces": {"provider": "mediapipe", "model": "face_detection_full_range_sparse", "embedding_model": "facenet512", "boxes": [{"box": {"x": 2}, "embedding_b64": "AAAAAA==", "embedding_dimension": 1, "candidate_person_id": str(known.id), "native_distance": 0.2, "recognition_result": "known"}, {"box": {"x": 3}, "embedding_b64": "AAAAAA==", "embedding_dimension": 1, "recognition_result": "unknown"}]}})
    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people)
    assert len(people.person_detections) == 1
    assert len(people.face_detections) == 2
    assert [value.person_id for value in people.effective_people(asset.id, "owner")] == [known.id]
    assert len(people.people) == 1  # Unknown never becomes a Person record.
    assert intelligence.latest_job(asset.id).people_status == "completed"  # type: ignore[union-attr]


def test_people_reanalysis_retains_history_but_exposes_only_latest_detection_set(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: GalleryIntelligenceClassification('[]', "rampp", "revision", "task", 1))
    payloads = iter((
        {"body": {"boxes": []}, "faces": {"provider": "mediapipe", "boxes": [{"box": {"x": index, "y": 1, "w": 20, "h": 20}, "embedding_b64": "AAAAAA=="} for index in range(3)]}},
        {"body": {"boxes": []}, "faces": {"provider": "mediapipe", "boxes": [{"box": {"x": index + 10, "y": 1, "w": 20, "h": 20}, "embedding_b64": "AAAAAA=="} for index in range(3)]}},
    ))
    monkeypatch.setattr("app.gallery_people_worker.request_people_analysis", lambda *_: next(payloads))

    first = intelligence.queue(asset.id, "owner", people_only=True)
    process_next_gallery_intelligence_job(intelligence, vault, people)
    second = intelligence.queue(asset.id, "owner", force=True, people_only=True)
    process_next_gallery_intelligence_job(intelligence, vault, people)

    assert len(people.face_detections) == 6  # retained provenance from both runs
    latest = intelligence.latest_successful_people_job(asset.id)
    assert latest and latest.id == second.id
    current = people.face_detections_for_asset(asset.id, "owner", latest.id, latest.started_at)
    assert [face.bounding_box["x"] for face in current] == [10, 11, 12]
    assert all(people.face_detections[face.id]["producing_job_id"] == second.id for face in current)
    assert any(row.get("producing_job_id") == first.id for row in people.face_detections.values())


def test_confirmed_reference_remains_usable_after_people_reanalysis(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    known = people.create_person("owner", "Owner")
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: GalleryIntelligenceClassification('[]', "rampp", "revision", "task", 1))
    payloads = iter((
        {"body": {"boxes": []}, "faces": {"boxes": [{"box": {"x": 1}, "embedding_b64": "AAAAAA=="}]}},
        {"body": {"boxes": []}, "faces": {"boxes": [{"box": {"x": 2}, "embedding_b64": "AAAAAA==", "candidate_person_id": str(known.id), "recognition_result": "known"}]}},
    ))
    monkeypatch.setattr("app.gallery_people_worker.request_people_analysis", lambda *_: next(payloads))

    first = intelligence.queue(asset.id, "owner", people_only=True)
    process_next_gallery_intelligence_job(intelligence, vault, people)
    first_face = people.face_detections_for_asset(asset.id, "owner", first.id, first.started_at)[0]
    people.identify_face(asset.id, first_face.id, known.id, "owner")
    assert {reference.person_id for reference in people.reference_embeddings("owner")} == {known.id}

    second = intelligence.queue(asset.id, "owner", force=True, people_only=True)
    process_next_gallery_intelligence_job(intelligence, vault, people)
    latest = intelligence.latest_successful_people_job(asset.id)
    assert latest and latest.id == second.id
    current = people.face_detections_for_asset(asset.id, "owner", latest.id, latest.started_at)
    assert current[0].person_name == "Owner"
    assert people.face_detections[first_face.id]["reference_person_id"] == known.id


@pytest.mark.parametrize(
    "filename, face_count",
    [
        ("IMG_5287.JPG", 1),
        ("20170210_202909375_iOS.jpg", 1),
        ("WP_20140215_003.jpg", 1),
        ("DSC_0018.jpg", 1),
        ("IMG_4052.jpeg", 1),
        ("IMG_0078.JPG", 2),  # multi-face support; exact recall is specialist-native.
        ("IMG_4211.jpeg", 0),  # bird/no-face control
    ],
)
def test_mediapipe_face_evidence_preserves_unknown_without_inventing_people(
    tmp_path: Path, monkeypatch, filename: str, face_count: int
) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: GalleryIntelligenceClassification('[]', "rampp", "revision", "task", 1))
    boxes = [{"box": {"x": index + 1, "y": 2, "w": 20, "h": 20}, "embedding_b64": "AAAAAA==", "embedding_dimension": 1, "recognition_result": "unknown"} for index in range(face_count)]
    monkeypatch.setattr("app.gallery_people_worker.request_people_analysis", lambda *_: {"task_version": "gallery-people-v2", "body": {"boxes": []}, "faces": {"provider": "mediapipe", "model": "face_detection_full_range_sparse", "embedding_model": "facenet512", "boxes": boxes}})
    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people)
    assert len(people.face_detections) == face_count, filename
    assert people.people == {}, filename
    assert people.effective_people(asset.id, "owner") == [], filename


def test_people_service_request_uses_mediapipe_boxes_before_facenet(tmp_path: Path, monkeypatch) -> None:
    from app import gallery_people_worker

    source = tmp_path / "IMG_5287.JPG"
    source.write_bytes(b"private-image")
    calls = []

    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, *_): return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/detect"):
            return Response({"provider": "mediapipe", "model": "face_detection_full_range_sparse", "model_revision": "0.10.21", "boxes": [{"box": {"x": 1, "y": 2, "w": 3, "h": 4}}]})
        return Response({"body": {"boxes": []}, "faces": {"provider": "mediapipe", "boxes": []}})

    monkeypatch.setattr(gallery_people_worker, "urlopen", fake_urlopen)
    gallery_people_worker.request_people_analysis(source, [])
    assert [call.full_url.rsplit("/", 1)[-1] for call in calls] == ["detect", "analyse"]
    assert json.loads(calls[1].get_header("X-pv-face-detection"))["provider"] == "mediapipe"


def test_people_worker_failure_does_not_change_completed_metadata_or_routing(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: GalleryIntelligenceClassification('["motorcycle"]', "rampp", "revision", "task", 1))
    monkeypatch.setattr("app.gallery_people_worker.request_people_analysis", lambda *_: (_ for _ in ()).throw(RuntimeError("unavailable")))
    before = replace(asset, effective_metadata={"ingestion_evidence": {"decision_score": 85}})
    vault.catalogued_assets[asset.vault_path] = before
    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people)
    assert intelligence.latest_job(asset.id).status == "completed"  # type: ignore[union-attr]
    assert intelligence.latest_job(asset.id).people_status == "failed"  # type: ignore[union-attr]
    assert {(value.namespace, value.slug) for value in intelligence.effective(asset.id)} == {("photo_type", "vehicle"), ("content_tag", "motorcycle")}
    assert vault.get_catalogued_asset_by_id(asset.id).effective_metadata["ingestion_evidence"]["decision_score"] == 85  # type: ignore[union-attr]


def test_people_user_exclusion_remains_effective_after_known_match(tmp_path: Path, monkeypatch) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    known = people.create_person("owner", "Corrected person")
    people.decide(asset.id, known.id, "exclude", "owner")
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: GalleryIntelligenceClassification('[]', "rampp", "revision", "task", 1))
    monkeypatch.setattr("app.gallery_people_worker.request_people_analysis", lambda *_: {"body": {"boxes": []}, "faces": {"boxes": [{"box": {"x": 2}, "embedding_b64": "AAAAAA==", "candidate_person_id": str(known.id), "recognition_result": "known"}]}})
    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people)
    assert people.effective_people(asset.id, "owner") == []


@pytest.mark.parametrize("outcome", ("success", "empty", "failure"))
def test_gallery_intelligence_never_changes_existing_routing_score(
    tmp_path: Path, monkeypatch, outcome: str
) -> None:
    vault, asset = gallery_asset(tmp_path)
    store = MemoryGalleryIntelligenceStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    if outcome == "failure":
        monkeypatch.setattr("app.gallery_intelligence.request_rampp_tags", lambda _: (_ for _ in ()).throw(RuntimeError("RAM++ unavailable")))
    else:
        response = '["motorcycle"]' if outcome == "success" else '[]'
        monkeypatch.setattr(
            "app.gallery_intelligence.request_rampp_tags",
            lambda _: GalleryIntelligenceClassification(response, "model", "revision", "gallery-intelligence-rampp-v1", 10),
        )
    before = replace(asset, effective_metadata={"ingestion_evidence": {"decision_score": 85, "routing_band": "automatic_eligible"}})
    vault.catalogued_assets[asset.vault_path] = before
    store.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(store, vault)
    after = vault.get_catalogued_asset_by_id(asset.id)
    assert after is not None and after.effective_metadata["ingestion_evidence"]["decision_score"] == 85
    assert store.jobs[next(iter(store.jobs))].status == ("failed" if outcome == "failure" else "completed")


def test_backfill_advances_only_through_never_analysed_gallery_assets(tmp_path: Path) -> None:
    vault, asset = gallery_asset(tmp_path)
    second = replace(asset, id=uuid4(), vault_path="/vault/Gallery/second.jpg", filename="second.jpg")
    vault.catalogued_assets[second.vault_path] = second
    store = MemoryGalleryIntelligenceStore()
    assert queue_published_gallery_assets(store, vault, "owner", limit=1) == 1
    assert queue_published_gallery_assets(store, vault, "owner", limit=1) == 1
    assert len(store.jobs) == 2
    assert queue_published_gallery_assets(store, vault, "owner", limit=1) == 0
    assert queue_published_gallery_assets(store, vault, "owner", limit=1, force=True) == 1
    assert len(store.jobs) == 3


def test_normal_backfill_skips_completed_empty_queued_processing_and_failed_jobs(tmp_path: Path) -> None:
    vault, first = gallery_asset(tmp_path)
    assets = [first]
    for index in range(1, 6):
        asset = replace(
            first,
            id=uuid4(),
            vault_path=f"/vault/Gallery/{index:02d}.jpg",
            filename=f"{index:02d}.jpg",
        )
        vault.catalogued_assets[asset.vault_path] = asset
        assets.append(asset)
    store = MemoryGalleryIntelligenceStore()
    completed_empty = store.queue(assets[0].id, "owner")
    completed_claim = store.claim_next_job()
    assert completed_claim is not None
    store.complete(completed_empty.id, (), None)
    store.queue(assets[1].id, "owner")
    store.queue(assets[2].id, "owner")
    assert store.claim_next_job() is not None
    assert store.claim_next_job() is not None
    failed = store.queue(assets[3].id, "owner")
    store.fail(failed.id, "RAM++ unavailable")

    assert queue_published_gallery_assets(store, vault, "owner", limit=50) == 2
    assert {job.asset_id for job in store.jobs.values()} == {asset.id for asset in assets}
    assert queue_published_gallery_assets(store, vault, "owner", limit=50) == 0
    assert store.latest_job(assets[3].id).status == "failed"  # type: ignore[union-attr]


def test_normal_backfill_reaches_every_unanalysed_asset_in_stable_batches(tmp_path: Path) -> None:
    vault, first = gallery_asset(tmp_path)
    for index in range(1, 52):
        asset = replace(
            first,
            id=uuid4(),
            vault_path=f"/vault/Gallery/{index:03d}.jpg",
            filename=f"{index:03d}.jpg",
        )
        vault.catalogued_assets[asset.vault_path] = asset
    store = MemoryGalleryIntelligenceStore()

    assert queue_published_gallery_assets(store, vault, "owner", limit=50) == 50
    assert queue_published_gallery_assets(store, vault, "owner", limit=50) == 2
    assert queue_published_gallery_assets(store, vault, "owner", limit=50) == 0
    assert len(store.jobs) == 52


def test_combined_gallery_catchup_queues_only_missing_enabled_stages(tmp_path: Path) -> None:
    from app.gallery_intelligence import queue_gallery_analysis_catchup

    vault, first = gallery_asset(tmp_path)
    stage_a_only = replace(first, id=uuid4(), vault_path="/vault/Gallery/stage-a.jpg", filename="stage-a.jpg")
    people_only = replace(first, id=uuid4(), vault_path="/vault/Gallery/people.jpg", filename="people.jpg")
    neither = replace(first, id=uuid4(), vault_path="/vault/Gallery/neither.jpg", filename="neither.jpg")
    both = replace(first, id=uuid4(), vault_path="/vault/Gallery/both.jpg", filename="both.jpg")
    del vault.catalogued_assets[first.vault_path]
    for asset in (stage_a_only, people_only, neither, both):
        vault.catalogued_assets[asset.vault_path] = asset
    store = MemoryGalleryIntelligenceStore()
    gi = store.queue(stage_a_only.id, "owner", force=True)
    store.complete(gi.id, (), None)
    people = store.queue(people_only.id, "owner", force=True, people_only=True)
    store.complete(people.id, (), None)
    store.mark_people_status(people.id, "completed")
    complete = store.queue(both.id, "owner", force=True)
    store.complete(complete.id, (), None)
    store.mark_people_status(complete.id, "completed")

    assert queue_gallery_analysis_catchup(store, vault, "owner", limit=50) == 3
    new = list(store.jobs.values())[-3:]
    assert {(job.asset_id, job.people_only, job.skip_people) for job in new} == {
        (stage_a_only.id, True, False),
        (people_only.id, False, True),
        (neither.id, False, False),
    }
    # Existing completed stages and queued catch-up jobs are never duplicated.
    assert queue_gallery_analysis_catchup(store, vault, "owner", limit=50) == 0


def test_bulk_progress_hides_settled_history_but_keeps_active_progress() -> None:
    store = MemoryGalleryIntelligenceStore()
    owner_user_id = uuid4()
    active_run = store.start_bulk_run("owner", owner_user_id)
    active = store.queue(uuid4(), "owner", force=True, bulk_run_id=active_run)
    assert store.latest_bulk_run(owner_user_id) is not None

    store.claim_next_job()
    store.complete(active.id, (), None)
    assert store.latest_bulk_run(owner_user_id) is not None

    completed = store.latest_job(active.asset_id)
    assert completed and completed.completed_at
    store.jobs[completed.id] = replace(
        completed,
        completed_at=completed.completed_at - BULK_COMPLETION_GRACE - timedelta(seconds=1),
    )
    assert store.latest_bulk_run(owner_user_id) is None


def test_worker_reconciles_retained_florence_no_face_person_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    ingestion = retained_florence_description(
        vault, asset, "A wooded outdoor scene beside a picnic table."
    )
    old = next(iter(ingestion.evidence.values()))
    latest = replace(
        old,
        id=uuid4(),
        job_id=uuid4(),
        caption="A woman kneeling outdoors beside a picnic table.",
        created_at=old.created_at + timedelta(seconds=1),
    )
    ingestion.evidence[latest.id] = latest
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification("[]", "rampp", "revision", "task", 1),
    )
    monkeypatch.setattr(
        "app.gallery_people_worker.request_people_analysis",
        lambda *_: {"body": {"boxes": []}, "faces": {"boxes": []}},
    )

    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people, ingestion)

    result = intelligence.reconciliation(asset.id)
    assert result is not None
    assert result.unresolved_person_presence is True
    assert result.people_ids == ()
    assert result.evidence["person_presence"] is True
    assert result.evidence["florence"] == {
        "available": True,
        "evidence_id": str(latest.id),
        "provider": "florence",
        "model_id": "microsoft/Florence-2-large",
        "model_revision": "florence-revision",
        "task_version": "arrival-image-analysis-v1",
        "created_at": latest.created_at.isoformat(),
        "human_presence": True,
    }
    assert intelligence.latest_job(asset.id).status == "completed"  # type: ignore[union-attr]


def test_worker_does_not_treat_yolox_only_box_as_person_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    ingestion = retained_florence_description(vault, asset, "A wooded landscape at dusk.")
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification("[]", "rampp", "revision", "task", 1),
    )
    monkeypatch.setattr(
        "app.gallery_people_worker.request_people_analysis",
        lambda *_: {"body": {"boxes": [{"box": {"x": 1}}]}, "faces": {"boxes": []}},
    )

    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people, ingestion)

    result = intelligence.reconciliation(asset.id)
    assert result is not None
    assert result.unresolved_person_presence is False
    assert result.evidence["florence"]["human_presence"] is False
    assert result.evidence["yolox_supporting_only"] is True


def test_worker_retains_unresolved_presence_from_florence_and_rampp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    ingestion = retained_florence_description(vault, asset, "A group of people on a beach.")
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    monkeypatch.setattr(
        "app.gallery_intelligence.request_rampp_tags",
        lambda _: GalleryIntelligenceClassification('["person", "beach"]', "rampp", "revision", "task", 1),
    )
    monkeypatch.setattr(
        "app.gallery_people_worker.request_people_analysis",
        lambda *_: {"body": {"boxes": []}, "faces": {"boxes": []}},
    )

    intelligence.queue(asset.id, "owner")
    process_next_gallery_intelligence_job(intelligence, vault, people, ingestion)

    result = intelligence.reconciliation(asset.id)
    assert result is not None and result.unresolved_person_presence is True
    assert result.evidence["florence"]["human_presence"] is True
    assert result.evidence["rampp_tags"] == ["person", "beach"]


def test_people_only_reconciliation_uses_retained_rampp_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    monkeypatch.setenv("PV_GALLERY_PATH", str(tmp_path / "Gallery"))
    first = intelligence.queue(asset.id, "owner")
    claimed = intelligence.claim_next_job()
    assert claimed is not None and claimed.id == first.id
    intelligence.complete(
        first.id,
        (),
        None,
        GalleryIntelligenceClassification('["person"]', "rampp", "revision", "task", 1),
    )
    monkeypatch.setattr(
        "app.gallery_people_worker.request_people_analysis",
        lambda *_: {"body": {"boxes": []}, "faces": {"boxes": []}},
    )

    intelligence.queue(asset.id, "owner", force=True, people_only=True)
    process_next_gallery_intelligence_job(intelligence, vault, people)

    result = intelligence.reconciliation(asset.id)
    assert result is not None and result.unresolved_person_presence is True
    assert result.evidence["rampp_tags"] == ["person"]


def test_worker_does_not_implicitly_queue_historical_gallery_assets(tmp_path: Path) -> None:
    vault, asset = gallery_asset(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()

    # An idle worker has no moved incoming item and must not sweep the existing
    # Gallery catalogue. Historical processing is explicit admin backfill.
    assert not queue_gallery_intelligence_for_published_asset(
        vault, intelligence, uuid4(), "owner"
    )
    assert not intelligence.jobs
    assert vault.get_catalogued_asset_by_id(asset.id) is not None
