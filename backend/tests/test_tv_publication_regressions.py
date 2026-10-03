"""Synthetic authority regressions; PostgreSQL must be an isolated test database."""
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import os
from uuid import UUID, uuid4

import psycopg
import pytest

from app.arrival_managed_publisher import ArrivalManagedPublicationRequest
from app.auth_store import PostgresAuthenticationStore
from app.tv_disc_resolver import resolve_tv_disc_batch
from app.tv_resolver_publication import PostgresTvResolverStore
from app.tv_shows import PostgresTvShowStore
from app.vault_master import (
    INCOMING_SOURCE, MemoryVaultMasterStore, PostgresVaultMasterStore,
    ScannedFile, process_next_move, safely_move_approved_file, scan_root,
)


@pytest.mark.parametrize("title", ["Example Show", "Another Example Show"])
@pytest.mark.parametrize("category", ["Home Videos", "TV Shows"])
@pytest.mark.parametrize("approved_evidence", [False, True])
def test_direct_mover_denies_resolver_items_before_touching_bytes(
    tmp_path, title, category, approved_evidence,
):
    incoming, target = tmp_path / "incoming", tmp_path / "target"
    incoming.mkdir()
    target.mkdir()
    source = incoming / "Example Episode.mkv"
    source.write_bytes(b"synthetic episode")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.record_decision(store.list_items()[0].id, "approved", "owner")
    destination = f"/vault/Theatre/TV Shows/{title}/Season 01/{title} - S01E01.mkv"
    metadata = {"tv_resolver_batch_id": str(UUID(int=4))}
    if approved_evidence:
        metadata["tv_resolver_publication"] = {
            "track_id": str(UUID(int=5)), "item_id": str(item.id),
            "owner_user_id": str(item.owner_user_id), "sha256": item.sha256,
            "canonical_destination": destination,
        }
    item = replace(item, proposed_category=category, proposed_destination=destination, metadata=metadata)
    with pytest.raises(ValueError, match="managed.*canonical TV"):
        safely_move_approved_file(item, incoming, target)
    assert source.read_bytes() == b"synthetic episode"
    assert list(target.iterdir()) == []


def test_non_tv_direct_mover_preserves_home_video(tmp_path):
    incoming, target = tmp_path / "incoming", tmp_path / "target"
    incoming.mkdir()
    target.mkdir()
    source = incoming / "Example Home Video.mkv"
    source.write_bytes(b"synthetic home video")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.record_decision(store.list_items()[0].id, "approved", "owner")
    assert item.proposed_category == "Home Videos"
    destination = safely_move_approved_file(item, incoming, target)
    assert destination.read_bytes() == b"synthetic home video"
    assert not source.exists()


@pytest.fixture
def approved_tv(tmp_path, monkeypatch):
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    monkeypatch.setenv("PV_METADATA_STORAGE_PATH", str(tmp_path / "metadata"))
    auth = PostgresAuthenticationStore(conninfo)
    auth.initialize()
    auth.ensure_initial_administrator("example-tv-owner", "test")
    owner = auth.get_account("example-tv-owner").user_id
    vault = PostgresVaultMasterStore(conninfo, sidecar_root=tmp_path / "metadata")
    vault.initialize()
    resolver = PostgresTvResolverStore(conninfo)
    resolver.initialize()
    tv = PostgresTvShowStore(conninfo)
    tv.initialize()

    def reset():
        with psycopg.connect(conninfo) as connection:
            connection.execute("DELETE FROM vault_tv_resolver_tracks")
            connection.execute("DELETE FROM vault_tv_resolver_seasons")
            connection.execute("DELETE FROM vault_tv_resolver_batches")
            connection.execute("DELETE FROM vault_tv_extras")
        vault.reset()

    reset()
    batch = vault.create_batch(INCOMING_SOURCE, str(tmp_path))
    sources = []
    for number in range(4):
        name = f"Example Show Season 1 - Disc 1_t{number + 1:02d}.mkv"
        source = tmp_path / name
        source.write_bytes(f"synthetic episode {number}".encode())
        sources.append(source)
        vault.record_file(batch, INCOMING_SOURCE, ScannedFile(
            str(source), name, name, source.stat().st_size, "video/x-matroska",
            datetime.now(UTC), hashlib.sha256(source.read_bytes()).hexdigest(),
            {"duration_seconds": 3500 if number < 3 else 200}, owner_user_id=owner,
        ))
    proposal = resolver.sync_proposal(owner, vault.list_items(), resolve_tv_disc_batch(vault.list_items()))
    resolver.approve(proposal.id, owner, "example-tv-owner")
    try:
        yield vault, resolver, owner, proposal.id, sources
    finally:
        reset()
        for source in sources:
            source.unlink(missing_ok=True)
        assert all(not source.exists() for source in sources)
        with psycopg.connect(conninfo) as connection:
            assert connection.execute("SELECT count(*) FROM vault_tv_resolver_tracks").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM vault_arrival_managed_publications").fetchone()[0] == 0


def publish_episodes(approved_tv, tmp_path):
    vault, resolver, owner, batch_id, _ = approved_tv
    items = []
    while process_next_move(vault, tmp_path, {}, theatre_queue=items.append):
        pass
    assert len(items) == 3
    for index, item in enumerate(items):
        request = ArrivalManagedPublicationRequest.create(item=item)
        receipt = dict(
            request_id=str(request.request_id), item_id=str(item.id), owner_user_id=str(owner),
            logical_destination=request.logical_destination, logical_area="Theatre / TV Shows",
            slot_id="PV-DISK-001", relative_path=request.logical_destination.removeprefix("/vault/"),
            expected_sha256=item.sha256, expected_size_bytes=item.size_bytes,
            verified_at=datetime.now(UTC).isoformat(),
        )
        assert vault.publish_arrival_managed_receipt(item.id, receipt) is not None
        assert resolver.reconcile() == ([batch_id] if index == 2 else [])
    return items


def test_managed_publication_valid_evidence_and_one_handoff(approved_tv, tmp_path):
    _, resolver, owner, batch_id, _ = approved_tv
    publish_episodes(approved_tv, tmp_path)
    assert resolver.reconcile() == []
    batch = resolver.get_for_owner(batch_id, owner)
    assert batch["status"] == "published"
    assert {track["publication_state"] for track in batch["tracks"] if track["classification"] == "likely_episode"} == {"published"}


@pytest.mark.parametrize("item_state", ["moved", "move_queued"])
@pytest.mark.parametrize("batch_status", ["publishing", "published", "complete"])
def test_stale_published_flag_requires_evidence(approved_tv, item_state, batch_status):
    vault, resolver, owner, batch_id, sources = approved_tv
    with psycopg.connect(resolver.conninfo) as connection:
        connection.execute("UPDATE vault_master_items SET state=%s WHERE id IN (SELECT arrival_item_id FROM vault_tv_resolver_tracks WHERE batch_id=%s AND classification='likely_episode')", (item_state, batch_id))
        connection.execute("UPDATE vault_tv_resolver_tracks SET publication_state='published' WHERE batch_id=%s AND classification='likely_episode'", (batch_id,))
        connection.execute("UPDATE vault_tv_resolver_batches SET status=%s WHERE id=%s", (batch_status, batch_id))
    for _ in range(2):
        assert resolver.reconcile() == []
        batch = resolver.get_for_owner(batch_id, owner)
        assert batch["status"] == "publishing"
        episodes = [track for track in batch["tracks"] if track["classification"] == "likely_episode"]
        assert {track["publication_state"] for track in episodes} == {"queued"}
        assert all("evidence" in track["failure_detail"] for track in episodes)
    assert all(source.is_file() for source in sources)
    with psycopg.connect(resolver.conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM vault_arrival_managed_publications").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM vault_files").fetchone()[0] == 0
    assert {item.state for item in vault.list_items() if item.metadata.get("tv_resolver_batch_id")} == {item_state}


@pytest.mark.parametrize("missing", ["receipt", "placement", "episode", "checksum"])
def test_published_requires_each_canonical_relationship(approved_tv, tmp_path, missing):
    _, resolver, owner, batch_id, _ = approved_tv
    items = publish_episodes(approved_tv, tmp_path)
    with psycopg.connect(resolver.conninfo) as connection:
        file_id = connection.execute("SELECT file_id FROM vault_arrival_managed_publications WHERE item_id=%s", (items[0].id,)).fetchone()[0]
        if missing == "receipt":
            connection.execute("DELETE FROM vault_arrival_managed_publications WHERE item_id=%s", (items[0].id,))
        elif missing == "placement":
            connection.execute("DELETE FROM vault_file_storage_placements WHERE file_id=%s", (file_id,))
        elif missing == "episode":
            connection.execute("DELETE FROM vault_tv_episodes WHERE asset_id=(SELECT asset_id FROM vault_files WHERE id=%s)", (file_id,))
        else:
            connection.execute("UPDATE vault_files SET sha256=%s WHERE id=%s", ("f" * 64, file_id))
    assert resolver.reconcile() == []
    batch = resolver.get_for_owner(batch_id, owner)
    assert batch["status"] == "publishing"
    track = next(track for track in batch["tracks"] if track["arrival_item_id"] == items[0].id)
    assert track["publication_state"] == "queued" and "evidence" in track["failure_detail"]
    assert sum(track["publication_state"] == "published" for track in batch["tracks"]) == 2
    if missing == "checksum":
        with psycopg.connect(resolver.conninfo) as connection:
            connection.execute("UPDATE vault_files SET sha256=%s WHERE id=%s", (items[0].sha256, file_id))
        assert resolver.reconcile() == []
        track = next(track for track in resolver.get_for_owner(batch_id, owner)["tracks"] if track["arrival_item_id"] == items[0].id)
        assert track["publication_state"] == "published" and track["failure_detail"] is None


def test_managed_extras_complete_batch_stays_verified(approved_tv, tmp_path):
    vault, resolver, owner, batch_id, _ = approved_tv
    publish_episodes(approved_tv, tmp_path)
    resolver.publish_extras(batch_id, owner, "example-tv-owner")
    requests = []
    assert process_next_move(vault, tmp_path, {}, theatre_queue=requests.append)
    assert len(requests) == 1
    item = requests[0]
    request = ArrivalManagedPublicationRequest.create(item=item)
    receipt = dict(
        request_id=str(request.request_id), item_id=str(item.id), owner_user_id=str(owner),
        logical_destination=request.logical_destination, logical_area="Theatre / TV Shows",
        slot_id="PV-DISK-001", relative_path=request.logical_destination.removeprefix("/vault/"),
        expected_sha256=item.sha256, expected_size_bytes=item.size_bytes,
        verified_at=datetime.now(UTC).isoformat(),
    )
    assert vault.publish_arrival_managed_receipt(item.id, receipt) is not None
    assert resolver.reconcile() == [batch_id]
    assert resolver.reconcile() == []
    batch = resolver.get_for_owner(batch_id, owner)
    assert batch["status"] == "complete"
    assert all(track["publication_state"] == "published" and track["failure_detail"] is None for track in batch["tracks"])
