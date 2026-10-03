"""Real PostgreSQL regressions for approved disc-track publication authority."""
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import Response

from app.arrival_managed_publisher import ArrivalManagedPublicationRequest
from app.auth_store import PostgresAuthenticationStore
from app.tv_disc_resolver import resolve_tv_disc_batch
from app.tv_resolver_publication import PostgresTvResolverStore
from app.tv_shows import PostgresTvShowStore
from app.vault_master import INCOMING_SOURCE, MemoryVaultMasterStore, PostgresVaultMasterStore, ScannedFile, process_next_move, safely_move_approved_file


@pytest.fixture
def setup(tmp_path, monkeypatch):
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    monkeypatch.setenv("PV_METADATA_STORAGE_PATH", str(tmp_path / "metadata"))
    auth = PostgresAuthenticationStore(conninfo)
    auth.initialize()
    auth.ensure_initial_administrator("resolver-authority-owner", "test")
    owner = auth.get_account("resolver-authority-owner").user_id
    vault = PostgresVaultMasterStore(conninfo, sidecar_root=tmp_path / "metadata")
    vault.initialize()
    resolver = PostgresTvResolverStore(conninfo)
    resolver.initialize()
    tv = PostgresTvShowStore(conninfo)
    tv.initialize()
    def reset():
        with psycopg.connect(conninfo) as conn:
            conn.execute("DELETE FROM vault_tv_resolver_tracks")
            conn.execute("DELETE FROM vault_tv_resolver_seasons")
            conn.execute("DELETE FROM vault_tv_resolver_batches")
        vault.reset()
    reset()
    batch = vault.create_batch(INCOMING_SOURCE, str(tmp_path))
    entries, scans = [], []
    for season, episode_count, extra_count in [(1, 10, 13), (2, 10, 27), (3, 8, 31)]:
        for n in range(episode_count + extra_count):
            name = f"Westworld Season {season} - Disc {n // 8 + 1}_t{n % 8 + 1:02d}.mkv"
            scan = ScannedFile(str(tmp_path / name), name, name, n + 1,
                "video/x-matroska", datetime.now(timezone.utc), f"{uuid4().int:064x}",
                {"duration_seconds": 3500 if n < episode_count else 200}, owner_user_id=owner)
            scans.append(scan)
            entries.append(vault.record_file(batch, INCOMING_SOURCE, scan))
    proposal = resolver.sync_proposal(owner, entries, resolve_tv_disc_batch(entries))
    yield vault, resolver, tv, owner, proposal.id, entries, scans
    reset()


def approve(setup):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    resolver.approve(batch_id, owner, "resolver-authority-owner")
    return resolver.get_for_owner(batch_id, owner)


def test_rescan_then_all_28_requests_use_durable_tv_mapping_and_71_extras_stay(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    assert all(i.proposed_category == "Home Videos" for i in entries)
    batch = approve(setup)
    episodes = [t for t in batch["tracks"] if t["classification"] == "likely_episode"]
    assert len(episodes) == 28
    # Reproduce the production sequence: approval, automatic rescan, worker.
    rescan = vault.create_batch(INCOMING_SOURCE, str(tmp_path))
    for scan in scans:
        vault.record_file(rescan, INCOMING_SOURCE, scan)
    requests = []
    while process_next_move(vault, tmp_path, {}, theatre_queue=lambda item: requests.append(ArrivalManagedPublicationRequest.create(item=item))):
        pass
    assert {r.item_id: r.logical_destination for r in requests} == {t["arrival_item_id"]: t["canonical_destination"] for t in episodes}
    selected = next(t for t in episodes if t["proposed_season_number"] == 2 and t["proposed_episode_number"] == 3)
    request = next(r for r in requests if r.item_id == selected["arrival_item_id"])
    assert request.logical_destination == "/vault/Theatre/TV Shows/Westworld/Season 02/Westworld - S02E03.mkv"
    assert all("Home Videos" not in r.logical_destination for r in requests)
    assert sum(i.state == "needs_review" for i in vault.list_items()) == 71
    assert sum(i.state == "theatre_promotion_pending" for i in vault.list_items()) == 28


def test_erased_markers_and_generic_proposal_cannot_bypass_claim_authority(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    batch = approve(setup)
    track = next(t for t in batch["tracks"] if t["classification"] == "likely_episode")
    with psycopg.connect(resolver.conninfo) as conn:
        conn.execute("UPDATE vault_master_items SET proposed_category='Home Videos', proposed_destination='/vault/Home Videos/wrong.mkv', metadata='{}' WHERE id=%s", (track["arrival_item_id"],))
    requests = []
    for _ in range(28):
        process_next_move(vault, tmp_path, {}, theatre_queue=lambda item: requests.append(ArrivalManagedPublicationRequest.create(item=item)))
    assert next(r for r in requests if r.item_id == track["arrival_item_id"]).logical_destination == track["canonical_destination"]


def test_request_and_legacy_move_reject_resolver_generic_destination(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    approve(setup)
    item = vault.claim_next_move()
    corrupted = replace(item, proposed_category="Home Videos", proposed_destination="/vault/Home Videos/wrong.mkv")
    with pytest.raises(ValueError, match="canonical TV"):
        ArrivalManagedPublicationRequest.create(item=corrupted)
    with pytest.raises(ValueError, match="canonical TV"):
        safely_move_approved_file(replace(corrupted, state="approved"), tmp_path, tmp_path)
    memory = MemoryVaultMasterStore()
    memory.items[item.source_path] = item
    original = next(s for s in scans if s.source_path == item.source_path)
    assert memory.record_file(uuid4(), INCOMING_SOURCE, original) == item
    changed_scan = replace(original, sha256="f" * 64)
    # Queued publication owns an immutable snapshot; rescans cannot rewrite it.
    assert memory.record_file(uuid4(), INCOMING_SOURCE, changed_scan) == item
    rescanned = vault.record_file(uuid4(), INCOMING_SOURCE, changed_scan)
    assert rescanned.id == item.id and rescanned.state == item.state
    assert rescanned.sha256 == original.sha256
    assert rescanned.metadata == item.metadata
    assert rescanned.proposed_destination == item.proposed_destination
    assert ArrivalManagedPublicationRequest.create(item=rescanned).expected_sha256 == original.sha256


def test_disc_filename_receipts_reconcile_three_seasons_before_one_handoff(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    approve(setup)
    items = []
    for _ in range(28):
        process_next_move(vault, tmp_path, {}, theatre_queue=items.append)
    for index, item in enumerate(items):
        request = ArrivalManagedPublicationRequest.create(item=item)
        receipt = dict(request_id=str(request.request_id), item_id=str(item.id), owner_user_id=str(owner),
            logical_destination=request.logical_destination, logical_area="Theatre / TV Shows", slot_id="PV-DISK-003",
            relative_path=request.logical_destination.removeprefix("/vault/"), expected_sha256=item.sha256,
            expected_size_bytes=item.size_bytes, verified_at=datetime.now(timezone.utc).isoformat())
        assert vault.publish_arrival_managed_receipt(item.id, receipt) is not None
        assert resolver.reconcile() == ([batch_id] if index == 27 else [])
    assert resolver.reconcile() == []
    with psycopg.connect(resolver.conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM vault_tv_shows WHERE title='Westworld'").fetchone()[0] == 1
        assert conn.execute("SELECT season_number,count(*) FROM vault_tv_episodes e JOIN vault_tv_seasons s ON s.id=e.season_id GROUP BY season_number ORDER BY season_number").fetchall() == [(1, 10), (2, 10), (3, 8)]
    assert resolver.get_for_owner(batch_id, owner)["status"] == "published"


def test_wrong_moved_state_does_not_count_as_canonical_publication_and_listing_survives(setup, monkeypatch):
    from app import vault_master_api
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    approve(setup)
    with psycopg.connect(resolver.conninfo) as conn:
        conn.execute("UPDATE vault_master_items SET state='moved' WHERE id IN (SELECT arrival_item_id FROM vault_tv_resolver_tracks WHERE batch_id=%s AND classification='likely_episode')", (batch_id,))
        conn.execute("UPDATE vault_tv_resolver_tracks SET publication_state='published' WHERE batch_id=%s AND classification='likely_episode'", (batch_id,))
    assert resolver.reconcile() == []
    batch = resolver.get_for_owner(batch_id, owner)
    assert batch["status"] == "publishing"
    assert {t["publication_state"] for t in batch["tracks"] if t["classification"] == "likely_episode"} == {"queued"}
    monkeypatch.setattr(vault_master_api, "get_database_conninfo", lambda: resolver.conninfo)
    result = vault_master_api.list_tv_resolver_batches(Response(), SimpleNamespace(user_id=owner), vault)
    assert len(result["batches"]) == 1 and len(result["batches"][0]["tracks"]) == 99


def test_reviewed_projection_recovery_is_owner_scoped_idempotent_and_does_not_publish(setup):
    from app.tv_resolver_recovery import restore_approved_batch_projection
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    batch = approve(setup)
    tracks = [t for t in batch["tracks"] if t["classification"] == "likely_episode"]
    expected = {t["arrival_item_id"]: "move_queued" for t in tracks}
    extras_before = [i for i in vault.list_items() if i.id not in expected]
    with psycopg.connect(resolver.conninfo) as conn:
        conn.execute("UPDATE vault_master_items SET metadata='{}', proposed_category='Home Videos', proposed_destination='/vault/Home Videos/Example.mkv' WHERE id=ANY(%s)", (list(expected),))
        receipts_before = conn.execute("SELECT count(*) FROM vault_arrival_managed_publications").fetchone()[0]
    with pytest.raises(ValueError, match="approved unfinished batch"):
        restore_approved_batch_projection(vault, batch_id, uuid4(), expected)
    with pytest.raises(ValueError, match="inventory differs"):
        restore_approved_batch_projection(vault, batch_id, owner, {})
    restore_approved_batch_projection(vault, batch_id, owner, expected)
    restore_approved_batch_projection(vault, batch_id, owner, expected)
    items = {i.id: i for i in vault.list_items()}
    for track in tracks:
        item = items[track["arrival_item_id"]]
        assert item.state == "move_queued"
        assert item.proposed_category == "TV Shows"
        assert item.proposed_destination == track["canonical_destination"]
        assert item.metadata["tv_resolver_batch_id"] == str(batch_id)
    assert [i for i in vault.list_items() if i.id not in expected] == extras_before
    with psycopg.connect(resolver.conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM vault_arrival_managed_publications").fetchone()[0] == receipts_before
        assert conn.execute("SELECT count(*) FROM vault_master_activity WHERE item_id=ANY(%s) AND action='tv_resolver_authority_restored'", (list(expected),)).fetchone()[0] == len(expected)
