"""Synthetic resolver fixtures and real PostgreSQL proposal lifecycle coverage."""

from dataclasses import replace
from datetime import datetime, timezone
import os
from uuid import uuid4

import psycopg
import pytest

from app.auth_store import PostgresAuthenticationStore
from app.tv_disc_resolver import discover_tv_disc_batches, resolve_tv_disc_batch
from app.tv_resolver_publication import PostgresTvResolverStore
from app.vault_master import INCOMING_SOURCE, ImportItem, PostgresVaultMasterStore, ScannedFile


def synthetic_items(durations, season=1, owner=None):
    owner = owner or uuid4()
    result = []
    for index, duration in enumerate(durations):
        name = f"Example Series Season {season} - Disc {index // 3 + 1}_t{index % 3 + 1:02d}.mkv"
        relative = f"Example Series/Season {season}/{name}"
        result.append(ImportItem(
            uuid4(), uuid4(), INCOMING_SOURCE, f"/arrival/{relative}", relative,
            name, index + 1, "video/x-matroska", datetime.now(timezone.utc),
            f"{uuid4().int:064x}", "needs_review", None, "Home Videos", None,
            "generic", "low", {"duration_seconds": duration}, {}, owner_user_id=owner,
        ))
    return result


def test_extras_majority_does_not_displace_episodes_or_long_finale():
    entries = synthetic_items([300, 310, 320, 330, 340, 350, 3400, 3500, 3600, 5100, None])
    proposal = resolve_tv_disc_batch(entries)
    assert [track.classification for track in proposal.tracks] == ["likely_extra"] * 6 + ["likely_episode"] * 4 + ["unresolved"]
    assert [track.episode_number for track in proposal.tracks[6:10]] == [1, 2, 3, 4]
    assert all(track.destination is None for track in proposal.tracks if track.classification != "likely_episode")


def test_season_clusters_keep_short_form_separate_and_merge_mixed_provenance():
    owner = uuid4()
    entries = synthetic_items([1400, 1500, 1600, 100], owner=owner)
    entries += synthetic_items([3400, 3500, 3600, 200], season=2, owner=owner)
    entries[-1] = replace(entries[-1], metadata={**entries[-1].metadata, "source_context": {"source_id": "synthetic-import", "source_label": "Example Series", "relative_path": entries[-1].relative_path}})
    groups = discover_tv_disc_batches(entries)
    assert len(groups) == 1
    proposal = resolve_tv_disc_batch(groups[0])
    for season in (1, 2):
        tracks = [track for track in proposal.tracks if track.season_number == season]
        assert [track.episode_number for track in tracks] == [1, 2, 3, None]
        assert tracks[-1].classification == "likely_extra"
    assert resolve_tv_disc_batch(entries) == proposal
    assert len(discover_tv_disc_batches(entries + synthetic_items([3500], owner=uuid4()))) == 2


@pytest.mark.parametrize("state", ["moved", "arrival_removed", "rejected"])
def test_discovery_excludes_terminal_arrival_items(state):
    entries = [replace(entry, state=state) for entry in synthetic_items([3500] * 3)]
    assert discover_tv_disc_batches(entries) == ()


def test_memory_api_reports_current_version_and_keeps_owner_scope(monkeypatch):
    from types import SimpleNamespace
    from fastapi import Response
    from app.vault_master import MemoryVaultMasterStore
    from app.vault_master_api import list_tv_resolver_batches

    owner = uuid4()
    entries = synthetic_items([3500] * 3, owner=owner)
    store = MemoryVaultMasterStore()
    monkeypatch.setattr(store, "list_items", lambda: entries + synthetic_items([3500] * 3))
    response = Response()
    result = list_tv_resolver_batches(response, SimpleNamespace(user_id=owner), store)
    assert len(result["batches"]) == 1
    assert result["batches"][0]["resolver_version"] == "pv-tv-disc-resolver.v3"
    assert len(result["batches"][0]["tracks"]) == 3
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.fixture
def persisted(tmp_path):
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    auth = PostgresAuthenticationStore(conninfo)
    auth.initialize()
    auth.ensure_initial_administrator("resolver-fixture-owner", "test")
    owner = auth.get_account("resolver-fixture-owner").user_id
    vault = PostgresVaultMasterStore(conninfo, sidecar_root=tmp_path / "metadata")
    vault.initialize()
    resolver = PostgresTvResolverStore(conninfo)
    resolver.initialize()
    batch = vault.create_batch(INCOMING_SOURCE, str(tmp_path))
    entries = []
    for entry in synthetic_items([3400, 3500, 3600, 3500, 3500, 200], owner=owner):
        entries.append(vault.record_file(batch, INCOMING_SOURCE, ScannedFile(
            str(tmp_path / entry.filename), entry.relative_path, entry.filename,
            entry.size_bytes, entry.mime_type, entry.modified_at, entry.sha256,
            entry.metadata, owner_user_id=owner,
        )))
    yield resolver, owner, entries
    with psycopg.connect(conninfo) as connection:
        connection.execute("DELETE FROM vault_tv_resolver_tracks WHERE batch_id IN (SELECT id FROM vault_tv_resolver_batches WHERE owner_user_id=%s)", (owner,))
        connection.execute("DELETE FROM vault_tv_resolver_seasons WHERE batch_id IN (SELECT id FROM vault_tv_resolver_batches WHERE owner_user_id=%s)", (owner,))
        connection.execute("DELETE FROM vault_tv_resolver_batches WHERE owner_user_id=%s", (owner,))
    vault.reset()


def sync(resolver, owner, entries):
    return resolver.sync_proposal(owner, entries, resolve_tv_disc_batch(entries))


def test_overlap_supersession_repeat_and_current_owner_filter(persisted):
    resolver, owner, entries = persisted
    first = sync(resolver, owner, entries[:3])
    second = sync(resolver, owner, entries[3:])
    with psycopg.connect(resolver.conninfo) as connection:
        connection.execute("UPDATE vault_tv_resolver_batches SET resolver_version='pv-tv-disc-resolver.v1', source_identity='supplier:synthetic-old' WHERE id=ANY(%s)", ([first.id, second.id],))
    current = sync(resolver, owner, entries)
    assert sync(resolver, owner, entries).id == current.id
    assert [batch["id"] for batch in resolver.list_for_owner(owner)] == [current.id]
    assert resolver.list_for_owner(uuid4()) == []
    assert resolver.get_for_owner(first.id, owner) is None
    with pytest.raises(ValueError, match="not eligible"):
        resolver.approve(first.id, owner, "resolver-fixture-owner")
    with psycopg.connect(resolver.conninfo) as connection:
        assert connection.execute("SELECT status FROM vault_tv_resolver_batches WHERE id=ANY(%s)", ([first.id, second.id],)).fetchall() == [("superseded",), ("superseded",)]
        assert connection.execute("SELECT count(*) FROM vault_tv_resolver_tracks WHERE batch_id=%s", (current.id,)).fetchone()[0] == len(entries)
        assert connection.execute("SELECT DISTINCT state FROM vault_master_items WHERE id=ANY(%s)", ([entry.id for entry in entries],)).fetchall() == [("needs_review",)]
        assert connection.execute("SELECT count(*) FROM vault_master_activity WHERE action='tv_resolver_publication_started'").fetchone()[0] == 0


@pytest.mark.parametrize("protected_status", ["approved", "publishing", "published", "failed"])
def test_protected_batch_cannot_be_superseded(persisted, protected_status):
    resolver, owner, entries = persisted
    old = sync(resolver, owner, entries[:3])
    with psycopg.connect(resolver.conninfo) as connection:
        connection.execute("UPDATE vault_tv_resolver_batches SET status=%s WHERE id=%s", (protected_status, old.id))
    with pytest.raises(ValueError, match="protected publication"):
        sync(resolver, owner, entries)
    assert resolver.get_for_owner(old.id, owner)["status"] == protected_status
    assert len(resolver.list_for_owner(owner)) == 1


def test_partial_overlap_fails_and_subset_keeps_current_proposal(persisted):
    resolver, owner, entries = persisted
    old = sync(resolver, owner, entries[:4])
    assert sync(resolver, owner, entries[:3]).id == old.id
    with pytest.raises(ValueError, match="partially overlaps"):
        sync(resolver, owner, entries[2:])
    assert [batch["id"] for batch in resolver.list_for_owner(owner)] == [old.id]


def test_indexes_survive_repeated_bootstrap(persisted):
    resolver, owner, entries = persisted
    old = sync(resolver, owner, entries)
    resolver.initialize()
    resolver.initialize()
    with psycopg.connect(resolver.conninfo) as connection:
        indexes = dict(connection.execute("SELECT indexname,indexdef FROM pg_indexes WHERE tablename IN ('vault_tv_resolver_tracks','vault_tv_resolver_batches')").fetchall())
    assert "(arrival_item_id, batch_id)" in indexes["vault_tv_resolver_tracks_arrival_item_idx"]
    assert "(owner_user_id, status)" in indexes["vault_tv_resolver_batches_owner_status_idx"]
    assert sync(resolver, owner, entries).id == old.id
