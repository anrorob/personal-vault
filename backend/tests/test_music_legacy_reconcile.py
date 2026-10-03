"""Synthetic maintenance manifests; never import runtime evidence into fixtures."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.music_legacy_reconcile import VERSION, reconcile, connection_from_environment
from app.music_groups import migrate_music_local_visibility, declared_album, supplier_manual_album
from app.vault_supplier import PostgresVaultSupplierStore, SupplierInstallation
from app.vault_supplier_transfer import PostgresTransferStore, TransferSession
from tests.test_postgres_vault_master import (
    postgres_store, postgres_conninfo, _catalogued_asset, _arrival_hall_owner_user_id,
)


@pytest.fixture
def legacy(postgres_store, postgres_conninfo):
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    vault = postgres_store.get_local_vault_id()
    installation = uuid4()
    now = datetime.now(timezone.utc)
    supplier = PostgresVaultSupplierStore(postgres_conninfo)
    supplier.initialize()
    supplier.register_installation(SupplierInstallation(installation, vault, b'synthetic',
        'ECDSA_P256', 1, 'synthetic', now), owner)
    transfers = PostgresTransferStore(postgres_conninfo)
    transfers.initialize()
    manifest = {'version': VERSION, 'environment': 'development', 'vault_id': str(vault), 'groups': []}
    album_ids = []
    try:
        for group_number, ordered in enumerate((True, False)):
            source = uuid4()
            group = {'owner_user_id': str(owner), 'installation_id': str(installation),
                'source_id': str(source), 'source_label': f'Example Artist - Example Album {group_number}',
                'members': [], 'transfer_ids': [], 'expected_order': []}
            declaration = supplier_manual_album({'source_kind': 'manual_upload',
                'source_id': str(source), 'source_label': group['source_label'],
                'relative_path': 'synthetic.flac'}, 'audio/flac', installation)['music_album']
            album_ids.append(declared_album(owner, declaration).id)
            # Deliberately reverse filename and coordinate order.
            for n in (2, 1, 3):
                filename = f'synthetic-{group_number}-{4-n}.flac'
                metadata = {'disc_number': 1, 'track_number': n} if ordered else {}
                asset = postgres_store.restore_catalogued_asset(replace(
                    _catalogued_asset(uuid4(), f'/vault/Music/{filename}', 'owner'),
                    asset_type='Music', mime_type='audio/flac', owner_user_id=owner,
                    origin_vault_id=vault, sha256=uuid4().hex * 2,
                    metadata=metadata, detected_metadata=metadata, effective_metadata=metadata), 'owner')
                context = {'source_kind': 'manual_upload', 'source_id': str(source),
                    'source_label': group['source_label'], 'relative_path': filename,
                    'original_filename': filename}
                session = transfers.create(TransferSession(uuid4(), installation, owner, vault,
                    filename, filename, asset.size_bytes, asset.sha256, 'audio/flac', context,
                    1, 'created', asset.size_bytes, now, now, now+timedelta(days=1)))
                assert transfers.begin_verification(session.transfer_id, filename)
                assert transfers.finalize(session.transfer_id)
                if not ordered:  # Optional MIME absent in older Supplier sessions.
                    with psycopg.connect(postgres_conninfo) as c:
                        c.execute('UPDATE vault_supplier_transfer_sessions SET media_type=NULL WHERE transfer_id=%s', (session.transfer_id,))
                group['transfer_ids'].append(str(session.transfer_id))
                with psycopg.connect(postgres_conninfo, row_factory=dict_row) as c:
                    row = c.execute("""SELECT a.id asset_id,f.id file_id,f.sha256,f.size_bytes,f.vault_path,
                        md5(jsonb_build_array(a.metadata,a.detected_metadata,a.imported_metadata,
                            a.user_overrides,a.effective_metadata)::text) metadata_fingerprint
                        FROM vault_assets a JOIN vault_files f ON f.asset_id=a.id WHERE a.id=%s""", (asset.id,)).fetchone()
                group['members'].append({k: str(v) if isinstance(v, UUID) else v for k,v in row.items()})
            if ordered:
                group['expected_order'] = [group['members'][i]['asset_id'] for i in (1, 0, 2)]
            manifest['groups'].append(group)
        yield manifest
    finally:
        with psycopg.connect(postgres_conninfo) as c:
            # The shared fixture resets assets but intentionally retains album
            # declarations. Remove only this test's independently generated groups.
            c.execute('DELETE FROM vault_music_album_history WHERE album_id=ANY(%s)', (album_ids,))
            c.execute('DELETE FROM vault_music_album_members WHERE album_id=ANY(%s)', (album_ids,))
            c.execute('DELETE FROM vault_music_albums WHERE id=ANY(%s)', (album_ids,))
            c.execute('DELETE FROM vault_supplier_transfer_sessions WHERE installation_id=%s', (installation,))
            c.execute('DELETE FROM vault_supplier_authorized_users WHERE installation_id=%s', (installation,))
            c.execute('DELETE FROM vault_supplier_installations WHERE installation_id=%s', (installation,))
            assert c.execute('SELECT count(*) FROM vault_supplier_transfer_sessions WHERE installation_id=%s', (installation,)).fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM vault_music_albums WHERE id=ANY(%s)', (album_ids,)).fetchone()[0] == 0


def snapshot(conninfo):
    with psycopg.connect(conninfo) as c:
        return [c.execute(f'SELECT to_jsonb(t) FROM {table} t ORDER BY id').fetchall()
                for table in ('vault_assets', 'vault_files', 'vault_asset_history')]


def group_snapshot(conninfo):
    with psycopg.connect(conninfo) as c:
        return [c.execute(f'SELECT to_jsonb(t) FROM {table} t ORDER BY {key}').fetchall()
                for table, key in (('vault_music_albums', 'id'),
                    ('vault_music_album_members', 'asset_id'), ('vault_music_album_history', 'id'))]


def test_reconcile_identity_order_retry_and_owner_corrections(legacy, postgres_store, postgres_conninfo):
    before = snapshot(postgres_conninfo)
    plan = reconcile(postgres_conninfo, legacy)
    assert [g['audited_order'] for g in plan['groups']] == ['ready', 'unresolved']
    assert snapshot(postgres_conninfo) == before
    applied = reconcile(postgres_conninfo, legacy, apply=True)
    assert snapshot(postgres_conninfo) == before  # All columns, not only UUIDs.
    ordered, unresolved = [UUID(g['album_id']) for g in applied['groups']]
    expected = list(map(UUID, legacy['groups'][0]['expected_order']))
    assert postgres_store.get_music_album_order(ordered)['asset_ids'] == expected
    assert postgres_store.get_music_album_order(unresolved) == {'state': 'unresolved', 'asset_ids': [], 'member_count': 3}
    owner = UUID(legacy['groups'][0]['owner_user_id'])
    postgres_store.correct_music_album(ordered, owner, 'Owner Artist', 'Owner Album')
    postgres_store.set_music_album_order(ordered, owner, list(reversed(expected)))
    with psycopg.connect(postgres_conninfo) as c:
        history = c.execute('SELECT count(*) FROM vault_music_album_history').fetchone()[0]
    assert all(g['already_applied'] for g in reconcile(postgres_conninfo, legacy, apply=True)['groups'])
    assert postgres_store.get_music_album(ordered).album_title == 'Owner Album'
    assert postgres_store.get_music_album_order(ordered)['asset_ids'] == list(reversed(expected))
    with psycopg.connect(postgres_conninfo) as c:
        assert c.execute('SELECT count(*) FROM vault_music_album_history').fetchone()[0] == history
        assert c.execute('SELECT count(*) FROM vault_music_albums WHERE id=ANY(%s)', ([ordered,unresolved],)).fetchone()[0] == 2


@pytest.mark.parametrize('fault', ['subset', 'source', 'label', 'order', 'metadata', 'deleted', 'fileless', 'duplicate'])
def test_changed_or_ambiguous_evidence_fails_atomically(legacy, postgres_conninfo, fault):
    manifest = deepcopy(legacy)
    group = manifest['groups'][1]  # Later group: first must not partially commit.
    asset = UUID(group['members'][0]['asset_id'])
    with psycopg.connect(postgres_conninfo) as c:
        if fault == 'subset':
            group['transfer_ids'].pop()
        elif fault == 'source':
            group['source_id'] = str(uuid4())
        elif fault == 'label':
            group['source_label'] = 'Matching filename is not authority'
        elif fault == 'order':
            group['expected_order'] = [m['asset_id'] for m in group['members']]
        elif fault == 'metadata':
            c.execute("UPDATE vault_assets SET user_overrides=%s WHERE id=%s", (Jsonb({'track_number': 9}), asset))
        elif fault == 'deleted':
            c.execute("""INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values)
                VALUES(%s,%s,'permanently_deleted','synthetic','{}','{}')""", (uuid4(),asset))
        elif fault == 'fileless':
            c.execute('DELETE FROM vault_files WHERE asset_id=%s', (asset,))
        elif fault == 'duplicate':
            group['members'].append(group['members'][0])
    before = snapshot(postgres_conninfo)
    groups_before = group_snapshot(postgres_conninfo)
    with pytest.raises(ValueError):
        reconcile(postgres_conninfo, manifest, apply=True)
    assert snapshot(postgres_conninfo) == before
    assert group_snapshot(postgres_conninfo) == groups_before


@pytest.mark.parametrize('visibility_first', [False, True])
def test_visibility_migration_and_deleted_nonmembers_remain_separate(legacy, postgres_store, postgres_conninfo, visibility_first):
    owner = UUID(legacy['groups'][0]['owner_user_id'])
    stale = postgres_store.restore_catalogued_asset(replace(
        _catalogued_asset(uuid4(), '/vault/Music/synthetic-deleted.flac', 'owner'),
        asset_type='Music', mime_type='audio/flac', owner_user_id=owner,
        origin_vault_id=postgres_store.get_local_vault_id()), 'owner')
    with psycopg.connect(postgres_conninfo, row_factory=dict_row) as c:
        c.execute('DELETE FROM vault_files WHERE asset_id=%s', (stale.id,))
        c.execute("""INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values)
            VALUES(%s,%s,'permanently_deleted','synthetic','{}','{}')""", (uuid4(),stale.id))
        c.execute("DELETE FROM vault_music_migrations WHERE version='local-default-v1'")
        if visibility_first:
            migrate_music_local_visibility(c.cursor(), postgres_store.get_local_vault_id())
    result = reconcile(postgres_conninfo, legacy, apply=True)
    with psycopg.connect(postgres_conninfo, row_factory=dict_row) as c:
        migrate_music_local_visibility(c.cursor(), postgres_store.get_local_vault_id())
        assert c.execute('SELECT visibility FROM vault_assets WHERE id=%s', (stale.id,)).fetchone()['visibility'] == 'private'
        assert c.execute("SELECT count(*) n FROM vault_assets WHERE visibility='vault-wide'").fetchone()['n'] == 6
        assert c.execute('SELECT count(*) n FROM vault_music_album_members WHERE asset_id=%s', (stale.id,)).fetchone()['n'] == 0
    assert postgres_store.get_music_album_order(UUID(result['groups'][0]['album_id']))['state'] == 'ready'
    assert postgres_store.get_music_album_order(UUID(result['groups'][1]['album_id']))['state'] == 'unresolved'


def test_cli_refuses_ambiguous_environment_and_build(monkeypatch):
    manifest = {'environment': 'production', 'database': {'host': 'example-db', 'name': 'example'}}
    for key, value in {'PV_ENVIRONMENT':'production', 'PV_COMMIT':'a'*40,
                       'POSTGRES_HOST':'example-db', 'POSTGRES_DB':'example',
                       'POSTGRES_PORT':'5432', 'POSTGRES_USER':'example', 'POSTGRES_PASSWORD':'synthetic'}.items():
        monkeypatch.setenv(key, value)
    assert 'example-db' in connection_from_environment(manifest, 'production', 'a'*40)
    for env, sha in [('development','a'*40), ('production','b'*40), ('unknown','a'*40)]:
        with pytest.raises(ValueError):
            connection_from_environment(manifest, env, sha)
    monkeypatch.setenv('POSTGRES_HOST', 'other-db')
    with pytest.raises(ValueError):
        connection_from_environment(manifest, 'production', 'a'*40)


def test_interrupted_apply_rolls_back_and_retries_whole_plan(legacy, postgres_conninfo, monkeypatch):
    import app.music_legacy_reconcile as module
    original = module.bind_members
    calls = []
    def interrupt(cursor, album, ids):
        calls.append(album.id)
        original(cursor, album, ids)
        if len(calls) == 2:
            raise RuntimeError('synthetic interruption before commit')
    before = snapshot(postgres_conninfo)
    groups_before = group_snapshot(postgres_conninfo)
    monkeypatch.setattr(module, 'bind_members', interrupt)
    with pytest.raises(RuntimeError):
        reconcile(postgres_conninfo, legacy, apply=True)
    assert group_snapshot(postgres_conninfo) == groups_before
    assert snapshot(postgres_conninfo) == before
    monkeypatch.setattr(module, 'bind_members', original)
    assert len(reconcile(postgres_conninfo, legacy, apply=True)['groups']) == 2


def test_matching_names_without_manual_selection_authority_are_rejected(legacy, postgres_conninfo):
    group = legacy['groups'][0]
    with psycopg.connect(postgres_conninfo) as c:
        c.execute("""UPDATE vault_supplier_transfer_sessions
            SET source_context=jsonb_set(source_context,'{source_kind}','\"automatic_source\"')
            WHERE transfer_id=ANY(%s)""", ([UUID(i) for i in group['transfer_ids']],))
    with pytest.raises(ValueError, match='manual audio selection'):
        reconcile(postgres_conninfo, legacy, apply=True)
