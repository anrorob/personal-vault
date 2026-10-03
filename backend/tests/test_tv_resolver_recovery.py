from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from app.auth_store import PostgresAuthenticationStore
from app.tv_disc_resolver import resolve_tv_disc_batch
from app.tv_resolver_publication import PostgresTvResolverStore
from app.tv_resolver_recovery import restore_approved_batch_projection
from app.vault_master import INCOMING_SOURCE, PostgresVaultMasterStore, ScannedFile


@pytest.fixture
def approved_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    monkeypatch.setenv("PV_METADATA_STORAGE_PATH", str(tmp_path / "metadata"))
    auth = PostgresAuthenticationStore(conninfo)
    auth.initialize()
    auth.ensure_initial_administrator("resolver-recovery-owner", "test")
    owner = auth.get_account("resolver-recovery-owner").user_id
    vault = PostgresVaultMasterStore(conninfo, sidecar_root=tmp_path / "metadata")
    vault.initialize()
    resolver = PostgresTvResolverStore(conninfo)
    resolver.initialize()
    with psycopg.connect(conninfo) as connection:
        connection.execute("DELETE FROM vault_tv_resolver_tracks")
        connection.execute("DELETE FROM vault_tv_resolver_seasons")
        connection.execute("DELETE FROM vault_tv_resolver_batches")
    vault.reset()
    batch = vault.create_batch(INCOMING_SOURCE, str(tmp_path))
    entries = []
    for number in range(23):
        name = f"Synthetic Show Season 1 - Disc {number // 8 + 1}_t{number % 8 + 1:02d}.mkv"
        scan = ScannedFile(
            str(tmp_path / name), name, name, number + 1, "video/x-matroska",
            datetime.now(timezone.utc), f"{uuid4().int:064x}",
            {"duration_seconds": 3500 if number < 10 else 200}, owner_user_id=owner,
        )
        entries.append(vault.record_file(batch, INCOMING_SOURCE, scan))
    proposal = resolver.sync_proposal(owner, entries, resolve_tv_disc_batch(entries))
    resolver.approve(proposal.id, owner, "resolver-recovery-owner")
    yield vault, resolver, owner, proposal.id
    with psycopg.connect(conninfo) as connection:
        connection.execute("DELETE FROM vault_tv_resolver_tracks")
        connection.execute("DELETE FROM vault_tv_resolver_seasons")
        connection.execute("DELETE FROM vault_tv_resolver_batches")
    vault.reset()


def test_restore_requires_exact_inventory_and_is_idempotent(approved_batch):
    vault, resolver, owner, batch_id = approved_batch
    batch = resolver.get_for_owner(batch_id, owner)
    episode_ids = [
        track["arrival_item_id"]
        for track in batch["tracks"]
        if track["classification"] == "likely_episode"
    ]
    with psycopg.connect(resolver.conninfo) as connection:
        connection.execute(
            "UPDATE vault_master_items SET metadata='{}' WHERE id=ANY(%s)",
            (episode_ids,),
        )
    with pytest.raises(ValueError, match="inventory differs"):
        restore_approved_batch_projection(vault, batch_id, owner, {})
    expected = {item_id: "move_queued" for item_id in episode_ids}
    restore_approved_batch_projection(vault, batch_id, owner, expected)
    restore_approved_batch_projection(vault, batch_id, owner, expected)
    with psycopg.connect(resolver.conninfo) as connection:
        restored = connection.execute(
            "SELECT count(*) FROM vault_master_items WHERE id=ANY(%s) "
            "AND metadata ? 'tv_resolver_publication' AND metadata ? 'tv_publication_set'",
            (episode_ids,),
        ).fetchone()[0]
        audit = connection.execute(
            "SELECT count(*) FROM vault_master_activity "
            "WHERE action='tv_resolver_authority_restored' AND item_id=ANY(%s)",
            (episode_ids,),
        ).fetchone()[0]
    assert restored == len(episode_ids)
    assert audit == len(episode_ids)


def test_development_commissioned_slot_identity_is_accepted(approved_batch):
    _, resolver, _, _ = approved_batch
    with psycopg.connect(resolver.conninfo) as connection:
        connection.execute(
            "INSERT INTO vault_storage_slots(slot_id,state,assigned_areas) "
            "VALUES ('PV-DEV-DISK-001','active','[]'::jsonb)"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO vault_storage_slots(slot_id,state,assigned_areas) "
                "VALUES ('unsafe-slot','active','[]'::jsonb)"
            )
