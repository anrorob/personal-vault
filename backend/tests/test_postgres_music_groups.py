from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
import psycopg

from app.music_groups import migrate_music_local_visibility
from app.vault_master import PostgresVaultMasterStore
from tests.test_postgres_vault_master import postgres_store, postgres_conninfo, _catalogued_asset, _arrival_hall_owner_user_id
from tests.test_music_groups import intent


@pytest.mark.parametrize("ordered", [False, True])
def test_release_approval_persists_album_information_without_changing_order(postgres_store, postgres_conninfo, tmp_path, ordered):
    from app.vault_master_music import approve_music_album, AlbumSelectionRequest
    from tests.test_vault_master_music import FakeProvider, selected_release
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    assets = []
    for number in range(1, 3):
        metadata = {"display_title": f"Example Song {number}"}
        if ordered:
            metadata.update(disc_number=1, track_number=number)
        asset = replace(_catalogued_asset(uuid4(), f"/vault/Music/{number:02d} Example.wav", "owner"),
                        asset_type="Music", mime_type="audio/wav", owner_user_id=owner,
                        detected_metadata=metadata, effective_metadata=metadata)
        assets.append(postgres_store.restore_catalogued_asset(asset, "owner"))
    album = postgres_store.declare_music_album(owner, intent())
    postgres_store.bind_music_album(album.id, owner, [asset.id for asset in assets])
    postgres_store.update_catalogued_asset_metadata(assets[0].id, {"display_title": "My title"}, "owner")
    before = postgres_store.get_music_album_order(album.id)
    approve_music_album(AlbumSelectionRequest(folder=".", album_group_id=album.id,
                        release_id=selected_release().release_id),
                        SimpleNamespace(user_id=owner), postgres_store, FakeProvider(), tmp_path)
    restarted = PostgresVaultMasterStore(postgres_conninfo)
    assert restarted.get_music_album_order(album.id) == before
    assert before["state"] == ("ready" if ordered else "unresolved")
    assert restarted.get_music_album(album.id) == album
    assert restarted.get_catalogued_asset_by_id(assets[0].id).display_title == "My title"
    for original in assets:
        asset = restarted.get_catalogued_asset_by_id(original.id)
        assert asset.imported_metadata["musicbrainz"]["release_id"] == selected_release().release_id
        assert asset.imported_metadata["artwork"]["owned"]["primary"]["storage_key"] == f"artwork/{asset.id}/primary"
        if not ordered:
            assert not asset.effective_metadata.get("track_number")
        assert (asset.sha256, asset.vault_path, asset.owner_user_id) == (original.sha256, original.vault_path, owner)


def test_postgres_manual_import_queues_whole_group_atomically(postgres_store, postgres_conninfo, tmp_path):
    from app.music_imports import approve_import
    from tests.test_music_imports import seed
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    album, items = seed(postgres_store, owner, tmp_path)
    ids = [item.id for item in items]
    with pytest.raises(ValueError):
        approve_import(postgres_store, owner, album.id, ids[:-1])
    assert all(postgres_store.get_item(item.id).state == "needs_review" for item in items)
    result = approve_import(postgres_store, owner, album.id, ids)
    assert {item.state for item in result} == {"move_queued"}
    assert {item.id for item in result} == set(ids)
    approve_import(postgres_store, owner, album.id, ids)
    with psycopg.connect(postgres_conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM vault_master_decisions WHERE item_id=ANY(%s)", (ids,)).fetchone()[0] == 13
        assert connection.execute("SELECT count(*) FROM vault_music_album_history WHERE album_id=%s AND action='import_approved'", (album.id,)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM vault_files").fetchone()[0] == 0


def test_postgres_manual_import_reconciliation_preserves_staging_and_is_idempotent(postgres_store, postgres_conninfo, tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from app.music_imports import reconcile_manual_import
    from app.vault_supplier import PostgresVaultSupplierStore, SupplierInstallation
    from app.vault_supplier_transfer import PostgresTransferStore, TransferSession
    from app.incoming import record_arrival_hall_file_owner, get_arrival_hall_file_source_context
    from app.vault_master import scan_file
    from tests.test_arrival_recovery import configure_recovery
    from tests.test_music_imports import context
    configure_recovery(tmp_path, monkeypatch)
    incoming = tmp_path / 'incoming'; incoming.mkdir()
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    installation, source_id = uuid4(), uuid4()
    supplier = PostgresVaultSupplierStore(postgres_conninfo); supplier.initialize()
    now = datetime.now(timezone.utc)
    supplier.register_installation(SupplierInstallation(installation, postgres_store.get_local_vault_id(), b'synthetic', 'ECDSA_P256', 1, 'synthetic', now), owner)
    transfers = PostgresTransferStore(postgres_conninfo); transfers.initialize()
    ids, paths = [], []
    try:
        batch = postgres_store.create_batch('incoming', str(incoming))
        for number in range(2):
            path = incoming / f'synthetic-{number}.wma'; path.write_bytes(f'synthetic-{number}'.encode()); paths.append(path)
            record_arrival_hall_file_owner(incoming, path, SimpleNamespace(user_id=owner))
            origin = {**context(source_id), 'relative_path': path.name, 'original_filename': path.name}
            scan = scan_file(path, incoming, 'owner', owner)
            item = postgres_store.record_file(batch, 'incoming', replace(scan, metadata={**scan.metadata, 'source_context': origin}))
            ids.append(item.id)
            session = transfers.create(TransferSession(uuid4(), installation, owner, postgres_store.get_local_vault_id(), path.name, path.name, path.stat().st_size,
                item.sha256, 'audio/x-ms-wma', origin, 1, 'created', path.stat().st_size, now, now, now+timedelta(days=1)))
            assert transfers.begin_verification(session.transfer_id, path.name) is not None
            assert transfers.finalize(session.transfer_id) is not None
        with pytest.raises(ValueError):
            reconcile_manual_import(postgres_store, owner, installation, source_id, ids[:1], incoming)
        result = reconcile_manual_import(postgres_store, owner, installation, source_id, ids, incoming)
        assert result['item_count'] == 2 and not result['published']
        assert reconcile_manual_import(postgres_store, owner, installation, source_id, ids, incoming) == result
        for item_id, path in zip(ids, paths):
            item = postgres_store.get_item(item_id)
            assert item.state == 'needs_review' and item.owner_user_id == owner
            assert result['album_group_id'] in item.proposed_destination
            assert get_arrival_hall_file_source_context(incoming, path)['music_album'] == item.metadata['source_context']['music_album']
            assert path.read_bytes().startswith(b'synthetic-')
        with psycopg.connect(postgres_conninfo) as connection:
            assert connection.execute("SELECT count(*) FROM vault_master_activity WHERE item_id=ANY(%s) AND action='music_import_reconciled'", (ids,)).fetchone()[0] == 2
            assert connection.execute('SELECT count(*) FROM vault_files').fetchone()[0] == 0
    finally:
        with psycopg.connect(postgres_conninfo) as connection:
            connection.execute('DELETE FROM vault_supplier_transfer_sessions WHERE installation_id=%s', (installation,))
            connection.execute('DELETE FROM vault_supplier_authorized_users WHERE installation_id=%s', (installation,))
            connection.execute('DELETE FROM vault_supplier_installations WHERE installation_id=%s', (installation,))


def test_postgres_music_publication_receipt_atomically_binds_group_and_slot(postgres_store, postgres_conninfo):
    from datetime import datetime, timezone
    from app.vault_master import ScannedFile
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    declaration = intent()
    batch = postgres_store.create_batch("incoming", "/synthetic/Arrival Hall")
    item = postgres_store.record_file(batch, "incoming", ScannedFile(
        source_path="/synthetic/Arrival Hall/01.flac", relative_path="01.flac", filename="01.flac", size_bytes=10,
        mime_type="audio/flac", modified_at=datetime.now(timezone.utc), sha256="1" * 64,
        metadata={"source_context": {"music_album": declaration, "original_filename": "01.flac"}}, owner_username="owner", owner_user_id=owner))
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("UPDATE vault_master_items SET state='theatre_promotion_pending' WHERE id=%s", (item.id,))
    receipt = {"request_id": str(uuid4()), "item_id": str(item.id), "owner_user_id": str(owner), "logical_destination": item.proposed_destination, "logical_area": "Music", "slot_id": "PV-DEV-DISK-001", "relative_path": item.proposed_destination.removeprefix("/vault/"), "expected_sha256": item.sha256, "expected_size_bytes": item.size_bytes}
    assert postgres_store.publish_arrival_managed_receipt(item.id, {**receipt, "owner_user_id": str(uuid4())}) is None
    asset = postgres_store.publish_arrival_managed_receipt(item.id, receipt)
    assert asset is not None and asset.visibility == "vault-wide"
    assert asset.owner_user_id == owner and asset.sha256 == item.sha256
    album = postgres_store.get_asset_music_album(asset.id)
    assert album.album_title == "Example Release"
    assert postgres_store.music_album_asset_ids(album.id) == [asset.id]
    assert asset.effective_metadata["storage_placement"]["slot_id"] == receipt["slot_id"]
    assert asset.detected_metadata["music_album_import"]["album_group_id"] == str(album.id)
    assert postgres_store.publish_arrival_managed_receipt(item.id, receipt) is None
    with psycopg.connect(postgres_conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM vault_share_grants WHERE asset_id=%s", (asset.id,)).fetchone()[0] == 0


def test_postgres_group_persistence_atomic_membership_and_local_visibility(postgres_store, postgres_conninfo):
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    other = _arrival_hall_owner_user_id(postgres_conninfo, "son")
    assets = []
    for number in range(12):
        asset = replace(_catalogued_asset(uuid4(), f"/vault/Music/{number}.flac", "owner"), asset_type="Music", mime_type="audio/flac", owner_user_id=owner, origin_vault_id=postgres_store.get_local_vault_id())
        assets.append(postgres_store.restore_catalogued_asset(asset, "owner"))
    declaration = intent()
    album = postgres_store.declare_music_album(owner, declaration)
    postgres_store.bind_music_album(album.id, owner, [asset.id for asset in assets])
    restarted = PostgresVaultMasterStore(postgres_conninfo)
    assert restarted.get_music_album(album.id) == album
    assert len(restarted.music_album_asset_ids(album.id)) == 12
    second = restarted.declare_music_album(owner, intent("Origins"))
    fresh = replace(_catalogued_asset(uuid4(), "/vault/Music/fresh.flac", "owner"), asset_type="Music", mime_type="audio/flac", owner_user_id=owner)
    fresh = postgres_store.restore_catalogued_asset(fresh, "owner")
    with pytest.raises(ValueError):
        restarted.bind_music_album(second.id, owner, [fresh.id, assets[0].id])
    assert restarted.music_album_asset_ids(second.id) == []  # Entire operation rolled back.
    assert restarted.get_asset_music_album(fresh.id) is None
    assert restarted.declare_music_album(other, declaration).id != album.id
    corrected = restarted.correct_music_album(album.id, owner, "Example Artist", "Example Release edition")
    assert corrected.id == album.id
    assert restarted.declare_music_album(owner, declaration) == corrected
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as connection:
        cursor = connection.cursor()
        # Reset only this migration marker in the disposable CI database to test its first-run path.
        cursor.execute("DELETE FROM vault_music_migrations WHERE version='local-default-v1'")
        migrate_music_local_visibility(cursor, postgres_store.get_local_vault_id())
        migrate_music_local_visibility(cursor, postgres_store.get_local_vault_id())
        cursor.execute("SELECT count(*) AS total FROM vault_asset_history WHERE action='music_local_default_migrated' AND asset_id=ANY(%s)", ([asset.id for asset in assets],))
        assert cursor.fetchone()["total"] == 12
    visible = restarted.get_visible_catalogued_assets([asset.vault_path for asset in assets], SimpleNamespace(user_id=other))
    assert len(visible) == 12
    assert all(asset.owner_user_id == owner for asset in visible.values())
    assert all(asset.sha256 == original.sha256 for asset, original in zip(sorted(visible.values(), key=lambda a: a.vault_path), sorted(assets, key=lambda a: a.vault_path)))
    current_rows = restarted.get_catalogued_assets(list(visible))
    assert restarted.filter_visible_catalogued_assets(
        current_rows, SimpleNamespace(user_id=other)
    ) == visible


def test_durable_membership_sequence_migration_and_explicit_correction(postgres_store, postgres_conninfo):
    from psycopg.types.json import Jsonb
    from app.music_order import initialize_music_order
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    assets = []
    for n, coordinate in enumerate([(2, 1), (1, 2), (1, 1)]):
        metadata = {'disc_number': coordinate[0], 'track_number': coordinate[1]}
        asset = replace(_catalogued_asset(uuid4(), f'/vault/Music/order-{n}.flac', 'owner'), asset_type='Music', mime_type='audio/flac', owner_user_id=owner, metadata=metadata, detected_metadata=metadata, effective_metadata=metadata)
        assets.append(postgres_store.restore_catalogued_asset(asset, 'owner'))
    album = postgres_store.declare_music_album(owner, intent())
    postgres_store.bind_music_album(album.id, owner, [a.id for a in assets])
    expected = [assets[2].id, assets[1].id, assets[0].id]
    assert postgres_store.get_music_album_order(album.id)['asset_ids'] == expected
    # Simulate pre-sequence membership in the isolated test database.
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as c:
        c.execute('UPDATE vault_music_album_members SET playback_position=NULL WHERE album_id=%s', (album.id,))
        c.execute("UPDATE vault_music_albums SET order_source='unresolved' WHERE id=%s", (album.id,))
        c.execute("DELETE FROM vault_music_migrations WHERE version='membership-order-v1'")
        initialize_music_order(c.cursor())
        initialize_music_order(c.cursor())
    assert postgres_store.get_music_album_order(album.id)['asset_ids'] == expected
    explicit = [a.id for a in assets]
    postgres_store.set_music_album_order(album.id, owner, explicit)
    with pytest.raises(ValueError):
        postgres_store.set_music_album_order(album.id, uuid4(), explicit)
    with pytest.raises(ValueError):
        postgres_store.set_music_album_order(album.id, owner, explicit[:-1])
    with psycopg.connect(postgres_conninfo) as c:
        c.execute('UPDATE vault_assets SET effective_metadata=%s WHERE id=%s', (Jsonb({'track_number': 99}), assets[0].id))
    restarted = PostgresVaultMasterStore(postgres_conninfo)
    assert restarted.get_music_album_order(album.id)['asset_ids'] == explicit
    fresh = replace(_catalogued_asset(uuid4(), '/vault/Music/order-new.flac', 'owner'), asset_type='Music', mime_type='audio/flac', owner_user_id=owner)
    fresh = postgres_store.restore_catalogued_asset(fresh, 'owner')
    restarted.bind_music_album(album.id, owner, [fresh.id])
    assert restarted.get_music_album_order(album.id) == {'state': 'unresolved', 'asset_ids': [], 'member_count': 4}
    restarted.set_music_album_order(album.id, owner, explicit + [fresh.id])
    assert restarted.get_music_album_order(album.id)['asset_ids'] == explicit + [fresh.id]


def test_local_default_preserves_explicit_private_policy_without_grants(postgres_store, postgres_conninfo):
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    local = postgres_store.get_local_vault_id()
    assets = [postgres_store.restore_catalogued_asset(replace(
        _catalogued_asset(uuid4(), f'/vault/Music/synthetic-policy-{n}.flac', 'owner'),
        asset_type='Music', mime_type='audio/flac', owner_user_id=owner,
        origin_vault_id=local), 'owner') for n in range(2)]
    explicit, legacy = assets
    # An owner can confirm private while already private, without creating a grant.
    assert postgres_store.update_catalogued_asset_access(explicit.id, 'private', (), 'owner')
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as c:
        assert c.execute('SELECT count(*) n FROM vault_share_grants WHERE asset_id=%s', (explicit.id,)).fetchone()['n'] == 0
        c.execute("DELETE FROM vault_music_migrations WHERE version='local-default-v1'")
        migrate_music_local_visibility(c.cursor(), local)
        rows = c.execute('SELECT id,visibility FROM vault_assets WHERE id=ANY(%s)', ([a.id for a in assets],)).fetchall()
        assert {r['id']:r['visibility'] for r in rows} == {explicit.id:'private', legacy.id:'vault-wide'}
    # Reopening the application must not undo an owner policy after the one-time run.
    assert postgres_store.update_catalogued_asset_access(legacy.id, 'private', (), 'owner')
    PostgresVaultMasterStore(postgres_conninfo).initialize()
    with psycopg.connect(postgres_conninfo) as c:
        assert c.execute('SELECT visibility FROM vault_assets WHERE id=%s', (legacy.id,)).fetchone()[0] == 'private'
        assert c.execute("SELECT count(*) FROM vault_asset_history WHERE asset_id=ANY(%s) AND action='music_local_default_migrated'", ([a.id for a in assets],)).fetchone()[0] == 1


def test_local_default_excludes_noncanonical_deleted_hidden_and_foreign_rows(postgres_store, postgres_conninfo):
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    local = postgres_store.get_local_vault_id()
    names = ['legacy', 'fileless', 'deleted', 'secondary-only', 'foreign', 'hidden', 'video']
    assets = {name:postgres_store.restore_catalogued_asset(replace(
        _catalogued_asset(uuid4(), f'/vault/Music/synthetic-{name}.flac', 'owner'),
        asset_type='Music', mime_type='audio/flac', owner_user_id=owner,
        origin_vault_id=local), 'owner') for name in names}
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as c:
        c.execute('DELETE FROM vault_files WHERE asset_id=%s', (assets['fileless'].id,))
        c.execute("UPDATE vault_files SET file_role='secondary' WHERE asset_id=%s", (assets['secondary-only'].id,))
        c.execute("INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES(%s,%s,'permanently_deleted','synthetic','{}','{}')", (uuid4(),assets['deleted'].id))
        foreign = uuid4()
        c.execute('INSERT INTO vaults(vault_id,is_local) VALUES(%s,FALSE)', (foreign,))
        c.execute('UPDATE vault_assets SET origin_vault_id=%s WHERE id=%s', (foreign,assets['foreign'].id))
        c.execute("UPDATE vault_assets SET lifecycle_state='hidden' WHERE id=%s", (assets['hidden'].id,))
        c.execute("UPDATE vault_assets SET asset_type='Music Videos' WHERE id=%s", (assets['video'].id,))
        file_before = c.execute('SELECT * FROM vault_files ORDER BY id').fetchall()
        c.execute("DELETE FROM vault_music_migrations WHERE version='local-default-v1'")
        migrate_music_local_visibility(c.cursor(), local)
        migrate_music_local_visibility(c.cursor(), local)
        rows = c.execute('SELECT id,visibility,owner_user_id FROM vault_assets WHERE id=ANY(%s)', ([a.id for a in assets.values()],)).fetchall()
        assert {r['id']:r['visibility'] for r in rows} == {a.id:('vault-wide' if name=='legacy' else 'private') for name,a in assets.items()}
        assert all(r['owner_user_id']==owner for r in rows)
        assert c.execute('SELECT * FROM vault_files ORDER BY id').fetchall() == file_before
        assert c.execute("SELECT asset_id FROM vault_asset_history WHERE action='music_local_default_migrated'").fetchall() == [{'asset_id':assets['legacy'].id}]
        assert c.execute('SELECT count(*) n FROM vault_music_album_members').fetchone()['n'] == 0


def test_reviewed_mapping_atomic_persistence_reidentification_and_stale_guard(postgres_store, postgres_conninfo, tmp_path):
    from fastapi import HTTPException
    from app.vault_master_music import approve_music_album, preview_music_album, AlbumSelectionRequest, ReviewedTrack
    from tests.test_music_matching import fixture
    _, examples, release=fixture(['Bright Sky.wav','Deep Sea.wav'],['Bright Sky','Deep Sea'])
    owner=_arrival_hall_owner_user_id(postgres_conninfo); actor=SimpleNamespace(user_id=owner)
    assets=[]
    for sample in examples:
        assets.append(postgres_store.restore_catalogued_asset(replace(sample,owner_user_id=owner),'owner'))
    album=postgres_store.declare_music_album(owner,intent())
    postgres_store.bind_music_album(album.id,owner,[a.id for a in assets])
    postgres_store.update_catalogued_asset_metadata(assets[0].id,{'display_title':'My manual title'},'owner')
    provider=SimpleNamespace(get_release=lambda _:release)
    request=AlbumSelectionRequest(folder='.',album_group_id=album.id,release_id=release.release_id)
    preview=preview_music_album(request,actor,postgres_store,provider)
    body=request.model_copy(update={'review_revision':preview.review_revision,'mapping':[ReviewedTrack(asset_id=row['asset_id'],provider_track=row['proposed_track']) for row in preview.local_matches],'apply_order':True})
    result=approve_music_album(body,actor,postgres_store,provider,tmp_path)
    assert result.order_state=='ready'
    restarted=PostgresVaultMasterStore(postgres_conninfo)
    assert restarted.get_catalogued_asset_by_id(assets[0].id).display_title=='My manual title'
    assert restarted.get_music_album_order(album.id)['state']=='ready'
    for a in assets:
        stored=restarted.get_catalogued_asset_by_id(a.id)
        assert stored.imported_metadata['music_match']['release_id']==release.release_id
        assert stored.sha256==a.sha256 and stored.vault_path==a.vault_path
    # A fresh re-review is idempotent in audit and membership.
    preview=preview_music_album(request,actor,restarted,provider)
    body=body.model_copy(update={'review_revision':preview.review_revision})
    approve_music_album(body,actor,restarted,provider,tmp_path)
    with psycopg.connect(postgres_conninfo) as c:
        assert c.execute("SELECT count(*) FROM vault_music_album_history WHERE album_id=%s AND action='mapping_approved'",(album.id,)).fetchone()[0]==1
    # Stale metadata must leave all other assets and positions unchanged.
    preview=preview_music_album(request,actor,restarted,provider)
    stale=body.model_copy(update={'review_revision':preview.review_revision})
    restarted.update_catalogued_asset_metadata(assets[0].id,{'display_title':'New correction'},'owner')
    with pytest.raises(HTTPException):approve_music_album(stale,actor,restarted,provider,tmp_path)
    assert restarted.get_music_album_order(album.id)['state']=='ready'
    # Explicitly leaving a file unmatched removes old importer coordinates and order.
    preview=preview_music_album(request,actor,restarted,provider)
    partial=body.model_copy(update={'review_revision':preview.review_revision,'mapping':[ReviewedTrack(asset_id=a.id) for a in assets]})
    approve_music_album(partial,actor,restarted,provider,tmp_path)
    assert restarted.get_music_album_order(album.id)['state']=='unresolved'
    assert len(restarted.music_album_asset_ids(album.id))==2
    assert all('track_number' not in restarted.get_catalogued_asset_by_id(a.id).imported_metadata for a in assets)
