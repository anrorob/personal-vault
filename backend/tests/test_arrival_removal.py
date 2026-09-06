"""Synthetic regressions for explicit staged-file removal, never live inventories."""
from dataclasses import replace
from datetime import datetime, timezone
import os
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg import sql
import pytest

from app.auth_store import PostgresAuthenticationStore
from app.tv_disc_resolver import TvBatchProposal, TvTrackProposal
from app.tv_resolver_publication import PostgresTvResolverStore
from app.tv_shows import PostgresTvShowStore
from app.vault_master import PostgresVaultMasterStore, scan_file, safely_remove_rejected_arrival_item, process_next_move
from app.arrival_managed_publisher import ArrivalManagedPublicationRequest
from app import vault_master_api as api
from tests.test_vault_master_api import configure, authenticate
from app.vault_master import MemoryVaultMasterStore


def test_missing_staged_source_removal_is_idempotent_and_bounded(client, tmp_path):
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / 'missing-file.mov'
    source.write_bytes(b'synthetic staging')
    authenticate(client)
    client.post('/api/vault-master/scan/incoming')
    from app.vault_master import process_next_batch
    process_next_batch(store)
    item = next(i for i in store.list_items() if i.filename == source.name)
    assert client.post(f'/api/vault-master/items/{item.id}/reject').status_code == 200
    rejected = store.get_item(item.id)
    source.unlink()  # Simulated interruption after the physical removal.
    with pytest.raises(ValueError, match='outside'):
        safely_remove_rejected_arrival_item(replace(rejected, source_path=str(tmp_path / 'outside.mov')), incoming)
    for _ in range(2):
        result = client.post(f'/api/vault-master/items/{item.id}/rejected/remove', json={'confirmation': 'REMOVE FROM ARRIVAL HALL'})
        assert result.status_code == 200
        assert result.json()['state'] == 'arrival_removed'
    assert not source.exists()


@pytest.fixture
def synthetic_resolver(tmp_path, monkeypatch):
    base = os.getenv('PV_TEST_DATABASE_URL')
    if not base:
        pytest.skip('PV_TEST_DATABASE_URL is not configured')
    schema = 'removal_' + uuid4().hex
    with psycopg.connect(base) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    conninfo = make_conninfo(base, options=f'-c search_path={schema}')
    monkeypatch.setenv('PV_METADATA_STORAGE_PATH', str(tmp_path / 'metadata'))
    monkeypatch.setattr(api, 'get_database_conninfo', lambda: conninfo)
    try:
        auth = PostgresAuthenticationStore(conninfo); auth.initialize()
        auth.ensure_initial_administrator('test-user-a', 'synthetic')
        owner = auth.get_account('test-user-a').user_id
        class Principal(str):
            user_id = owner
        principal = Principal('test-user-a')
        vault = PostgresVaultMasterStore(conninfo, sidecar_root=tmp_path / 'metadata'); vault.initialize()
        # Public catalogue publication reads the separately bootstrapped AI evidence store.
        from app.vault_master_ingestion_ai import PostgresIngestionAiStore
        PostgresIngestionAiStore(conninfo).initialize()
        tv = PostgresTvShowStore(conninfo); tv.initialize()
        resolver = PostgresTvResolverStore(conninfo); resolver.initialize()
        incoming = tmp_path / 'incoming'; incoming.mkdir()
        batch = vault.create_batch('incoming', str(incoming))
        entries = []
        for name in ['sample-episode-01.mkv', 'sample-extra-01.mkv', 'sample-extra-02.mkv']:
            path = incoming / name; path.write_bytes(name.encode())
            entries.append(vault.record_file(batch, 'incoming', scan_file(path, incoming, str(principal), owner)))
        tracks = tuple(TvTrackProposal(item.id, item.filename, 1, 1, index, 1800 if index == 0 else 60,
            'likely_episode' if index == 0 else 'likely_extra', 1 if index == 0 else None, 'high', (),
            '/vault/Theatre/TV Shows/Test Series Alpha/Season 01/Test Series Alpha - S01E01.mkv' if index == 0 else None)
            for index, item in enumerate(entries))
        batch_id = resolver.sync_proposal(owner, entries, TvBatchProposal('synthetic', 'Test Series Alpha', 'high', False, (), tracks)).id
        yield vault, resolver, owner, principal, batch_id, entries, incoming
    finally:
        with psycopg.connect(base) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def remove(vault, item, principal, incoming):
    vault.record_decision(item.id, 'rejected', principal)
    return api.remove_rejected_arrival_item(item.id, api.RejectedArrivalRemovalConfirmation(confirmation='REMOVE FROM ARRIVAL HALL'), principal, vault, incoming)


def test_removed_extras_retire_work_preserve_published_episode_and_history(synthetic_resolver):
    vault, resolver, owner, principal, batch_id, entries, incoming = synthetic_resolver
    resolver.approve(batch_id, owner, principal)
    requests = []
    assert process_next_move(vault, incoming, {}, theatre_queue=requests.append)
    item = requests[0]
    request = ArrivalManagedPublicationRequest.create(item=item)
    receipt = dict(request_id=str(request.request_id), item_id=str(item.id), owner_user_id=str(owner),
        logical_destination=request.logical_destination, logical_area='Theatre / TV Shows', slot_id='PV-DISK-003',
        relative_path=request.logical_destination.removeprefix('/vault/'), expected_sha256=item.sha256,
        expected_size_bytes=item.size_bytes, verified_at=datetime.now(timezone.utc).isoformat())
    assert vault.publish_arrival_managed_receipt(item.id, receipt)
    resolver.reconcile()
    with psycopg.connect(resolver.conninfo) as conn:
        before = {t: conn.execute(f'SELECT * FROM {t}').fetchall() for t in ['vault_assets','vault_files','vault_tv_episodes','vault_file_storage_placements']}
    assert remove(vault, entries[1], principal, incoming).state == 'arrival_removed'
    active = resolver.list_for_owner(owner)
    assert len(active) == 1 and len(active[0]['tracks']) == 2
    resolver.reconcile()
    assert remove(vault, entries[2], principal, incoming).state == 'arrival_removed'
    assert resolver.reconcile() == []
    assert resolver.list_for_owner(owner) == []
    history = resolver.get_for_owner(batch_id, owner)
    assert history['status'] == 'complete'
    assert sorted(t['publication_state'] for t in history['tracks']) == ['cancelled','cancelled','published']
    resolver.retire_removed()
    assert resolver.publish_extras(batch_id, owner, principal)['status'] == 'complete'
    with psycopg.connect(resolver.conninfo) as conn:
        assert before == {t: conn.execute(f'SELECT * FROM {t}').fetchall() for t in before}
        assert conn.execute('SELECT count(*) FROM vault_tv_extras').fetchone()[0] == 0
    assert vault.get_item(entries[0].id).state == 'moved'


def test_all_removed_proposal_closes_and_retry_heals_missing_db_transition(synthetic_resolver):
    vault, resolver, owner, principal, batch_id, entries, incoming = synthetic_resolver
    for item in entries:
        vault.record_decision(item.id, 'rejected', principal)
        source = incoming / item.filename
        source.unlink()
        assert api.remove_rejected_arrival_item(item.id, api.RejectedArrivalRemovalConfirmation(confirmation='REMOVE FROM ARRIVAL HALL'), principal, vault, incoming).state == 'arrival_removed'
    assert resolver.list_for_owner(owner) == []
    assert all(t['publication_state'] == 'cancelled' for t in resolver.get_for_owner(batch_id, owner)['tracks'])
    assert resolver.reconcile() == []