"""Real PostgreSQL lifecycle and managed receipt regressions for TV extras."""
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest

from app.arrival_managed_publisher import ArrivalManagedPublicationRequest, queue_request, reconcile_rejected_request
from app.tv_extras import extra_destination
from app.vault_master import process_next_move
from tests.test_tv_resolver_authority import setup  # shared 28 episode / 71 extra fixture
from tests.test_tv_resolver_managed_recovery import executor


def receipt(item):
    request = ArrivalManagedPublicationRequest.create(item=item)
    return dict(request_id=str(request.request_id), item_id=str(item.id), owner_user_id=str(item.owner_user_id),
        logical_destination=request.logical_destination, logical_area='Theatre / TV Shows', slot_id='PV-DISK-003',
        relative_path=request.logical_destination.removeprefix('/vault/'), expected_sha256=item.sha256,
        expected_size_bytes=item.size_bytes, verified_at=datetime.now(timezone.utc).isoformat())


def publish_episodes(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    resolver.approve(batch_id, owner, 'test')
    items = []
    while process_next_move(vault, tmp_path, {}, theatre_queue=items.append):
        pass
    assert len(items) == 28
    for item in items:
        assert vault.publish_arrival_managed_receipt(item.id, receipt(item)) is not None
    assert resolver.reconcile() == [batch_id]
    assert resolver.get_for_owner(batch_id, owner)['status'] == 'published'
    assert sum(i.state == 'needs_review' for i in vault.list_items()) == 71
    return items


def test_71_extras_publish_in_seasons_complete_hidden_history_idempotent(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    episodes = publish_episodes(setup, tmp_path)
    resolver.publish_extras(batch_id, owner, 'test')
    assert resolver.publish_extras(batch_id, owner, 'test')['status'] == 'publishing'
    # Automatic rescans cannot restore generic Home Videos routing.
    for scan in scans:
        vault.record_file(uuid4(), 'incoming', scan)
    extras = []
    while process_next_move(vault, tmp_path, {}, theatre_queue=extras.append):
        pass
    assert len(extras) == 71
    for index, item in enumerate(extras):
        marker = item.metadata['tv_extra']
        assert item.proposed_destination == extra_destination('Westworld', marker['season_number'], item.filename)
        assert 'Home Videos' not in item.proposed_destination
        assert 'tv_publication_set' not in item.metadata
        proof = receipt(item)
        assert vault.publish_arrival_managed_receipt(item.id, proof) is not None
        assert vault.publish_arrival_managed_receipt(item.id, proof) is None
        assert resolver.reconcile() == ([batch_id] if index == 70 else [])
    assert resolver.reconcile() == []  # one bounded extras handoff
    assert resolver.get_for_owner(batch_id, owner)['status'] == 'complete'
    assert resolver.list_for_owner(owner) == []
    assert len(resolver.list_for_owner(owner, include_complete=True)) == 1
    assert resolver.publish_extras(batch_id, owner, 'test')['status'] == 'complete'
    assert resolver.retry(batch_id, owner, 'test')['status'] == 'complete'
    assert resolver.approve(batch_id, owner, 'test')['status'] == 'complete'
    with psycopg.connect(resolver.conninfo) as conn:
        assert conn.execute('SELECT count(*) FROM vault_tv_episodes').fetchone()[0] == 28
        assert conn.execute('SELECT s.season_number,count(*) FROM vault_tv_extras e JOIN vault_tv_seasons s ON s.id=e.season_id GROUP BY s.season_number ORDER BY 1').fetchall() == [(1,13),(2,27),(3,31)]
        assert conn.execute('SELECT count(*) FROM vault_tv_resolver_tracks WHERE batch_id=%s', (batch_id,)).fetchone()[0] == 99


def test_failed_extra_keeps_successes_active_and_retry_only_failed(setup, tmp_path, monkeypatch):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    publish_episodes(setup, tmp_path)
    resolver.publish_extras(batch_id, owner, 'test')
    extras = []
    queue = tmp_path / 'queue'; receipts = tmp_path / 'receipts'; receipts.mkdir()
    key_path = tmp_path / 'key'; key_path.write_bytes(b'test-key')
    monkeypatch.setenv('PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE', str(queue))
    monkeypatch.setenv('PV_ARRIVAL_MANAGED_PUBLISHER_RECEIPTS', str(receipts))
    monkeypatch.setenv('PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH', str(key_path))
    def queued(item):
        extras.append(item)
        request = ArrivalManagedPublicationRequest.create(item=item)
        path = queue_request(request, queue_root=queue, key=b'test-key')
        if len(extras) == 1:
            path.rename(path.with_suffix('.rejected.request'))
        return request.request_id
    while process_next_move(vault, tmp_path, {}, theatre_queue=queued):
        pass
    assert reconcile_rejected_request(vault) == extras[0].id
    assert reconcile_rejected_request(vault) is None
    for item in extras[1:]:
        assert vault.publish_arrival_managed_receipt(item.id, receipt(item)) is not None
    resolver.reconcile()
    active = resolver.list_for_owner(owner)[0]
    assert active['status'] == 'failed'
    assert sum(t['publication_state']=='published' for t in active['tracks']) == 98
    assert sum(t['publication_state']=='failed' for t in active['tracks']) == 1
    assert resolver.publish_extras(batch_id, owner, 'test')['status'] == 'failed'
    resolver.retry(batch_id, owner, 'test')
    assert resolver.retry(batch_id, owner, 'test')['status'] == 'publishing'
    retried=[]
    while process_next_move(vault, tmp_path, {}, theatre_queue=lambda i: (retried.append(i), uuid4())[1]):
        pass
    assert [i.id for i in retried] == [extras[0].id]
    assert reconcile_rejected_request(vault) is None  # old rejection cannot fail new attempt
    assert vault.publish_arrival_managed_receipt(retried[0].id, receipt(retried[0])) is not None
    resolver.reconcile()
    assert resolver.list_for_owner(owner) == []


@pytest.mark.parametrize('corruption', ['owner', 'checksum', 'season', 'collision'])
def test_extra_preapproval_evidence_fails_closed(setup, tmp_path, corruption):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    publish_episodes(setup, tmp_path)
    with psycopg.connect(resolver.conninfo) as conn:
        track = conn.execute("SELECT id,arrival_item_id FROM vault_tv_resolver_tracks WHERE batch_id=%s AND classification='likely_extra' LIMIT 1", (batch_id,)).fetchone()
        if corruption == 'owner':
            from app.auth_store import PostgresAuthenticationStore
            auth = PostgresAuthenticationStore(resolver.conninfo)
            auth.ensure_initial_administrator('extra-other-owner', 'test')
            other = auth.get_account('extra-other-owner').user_id
            conn.execute('UPDATE vault_master_items SET owner_user_id=%s WHERE id=%s', (other, track[1]))
        elif corruption == 'checksum':
            conn.execute("UPDATE vault_master_items SET sha256=%s WHERE id=%s", ('f'*64, track[1]))
        elif corruption == 'season':
            conn.execute('UPDATE vault_tv_resolver_tracks SET proposed_season_number=999 WHERE id=%s', (track[0],))
        else:
            conn.execute("UPDATE vault_tv_resolver_tracks SET original_filename='same.mkv',proposed_season_number=1 WHERE batch_id=%s AND classification='likely_extra'", (batch_id,))
    with pytest.raises(ValueError):
        resolver.publish_extras(batch_id, owner, 'test')
    assert sum(i.state == 'needs_review' for i in vault.list_items()) == 71


def test_one_invalid_claim_does_not_block_other_extras(setup, tmp_path):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    publish_episodes(setup, tmp_path)
    resolver.publish_extras(batch_id, owner, 'test')
    with psycopg.connect(resolver.conninfo) as conn:
        conn.execute("UPDATE vault_master_items SET sha256=%s WHERE id=(SELECT arrival_item_id FROM vault_tv_resolver_tracks WHERE batch_id=%s AND classification='likely_extra' LIMIT 1)", ('f'*64, batch_id))
    extras=[]
    while process_next_move(vault, tmp_path, {}, theatre_queue=extras.append):
        pass
    assert len(extras) == 70
    resolver.reconcile()
    active = resolver.list_for_owner(owner)[0]
    assert active['status'] == 'failed'
    assert sum(t['publication_state']=='failed' for t in active['tracks']) == 1


def test_reconcile_defers_a_batch_locked_by_an_action(setup):
    vault, resolver, tv, owner, batch_id, entries, scans = setup
    resolver.approve(batch_id, owner, 'test')
    with psycopg.connect(resolver.conninfo) as conn:
        conn.execute('SELECT id FROM vault_tv_resolver_batches WHERE id=%s FOR UPDATE', (batch_id,))
        assert resolver.reconcile() == []
    assert resolver.get_for_owner(batch_id, owner)['status'] == 'publishing'


@pytest.mark.parametrize('name', ['../bad.mkv', 'a/b.mkv', 'a\\b.mkv', '..'])
def test_extra_unsafe_name_rejected(name):
    with pytest.raises(ValueError):
        extra_destination('Example', 1, name)


@pytest.mark.parametrize('failure', [None, 'missing', 'checksum', 'collision'])
def test_real_managed_executor_extra_source_and_destination_safety(executor, failure):
    from dataclasses import replace
    import hashlib
    module, old, _, _ = executor
    source = module.ARRIVAL / 'Original extra.mkv'
    source.write_bytes(b'extra fixture bytes')
    destination = extra_destination('Example', 2, source.name)
    target = module.SLOT_ROOT / 'PV-DISK-003' / destination.removeprefix('/vault/')
    request = ArrivalManagedPublicationRequest(uuid4(), uuid4(), old.owner_user_id, source.name,
        destination, hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size,
        datetime.now(timezone.utc).isoformat())
    if failure == 'missing':
        source.unlink()
    elif failure == 'checksum':
        request = replace(request, expected_sha256='f'*64)
    elif failure == 'collision':
        target.parent.mkdir(parents=True)
        target.write_bytes(b'untouched existing file')
    queued = queue_request(request, queue_root=module.QUEUE, key=module.KEY.read_bytes())
    if failure:
        with pytest.raises(ValueError):
            module.process_request(queued)
        assert source.exists() == (failure != 'missing')
        if failure == 'collision':
            assert target.read_bytes() == b'untouched existing file'
    else:
        module.process_request(queued)
        assert target.read_bytes() == b'extra fixture bytes'
        assert not source.exists()
        module.process_request(queue_request(request, queue_root=module.QUEUE, key=module.KEY.read_bytes()))
        assert target.read_bytes() == b'extra fixture bytes'
