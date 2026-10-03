"""Real recovery routes and host executor, using only disposable synthetic bytes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import json
import multiprocessing
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from app import arrival_recovery as recovery
from app.arrival_managed_publisher import queue_item, reissue_item
from app.arrival_publication_coordination import VERSION, publication_lock
from app.vault_master import MemoryVaultMasterStore, process_next_batch, scan_file
from tests.test_vault_master_api import configure, authenticate
from tests.managed_publisher_fixture import publisher, load_publisher
from tests.test_arrival_removal import synthetic_resolver


def configure_recovery(tmp_path, monkeypatch):
    queue, receipts, slot = tmp_path / "requests", tmp_path / "receipts", tmp_path / "slots" / "PV-DISK-001"
    for path in (queue, receipts, slot):
        path.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "key"
    key.write_bytes(b"synthetic-recovery-key")
    (queue / ".coordination-version").write_text(VERSION)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": "personal-vault.slot-managed-manifest.v1", "slots": {"PV-DISK-001": {
        "state": "active", "integration_mode": "slot_managed", "resolver_root": str(slot),
        "hardware_id": "synthetic", "filesystem_uuid": "synthetic", "areas": ["Music", "Theatre / Movies"],
        "logical_mappings": {"Music": "/vault/Music", "Theatre / Movies": "/vault/Theatre/Movies"}}}}))
    for name, value in {"PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE": queue, "PV_ARRIVAL_MANAGED_PUBLISHER_RECEIPTS": receipts,
        "PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH": key, "PV_SECTION_MOVE_MANIFEST": manifest,
        "PV_STORAGE_SLOT_ROOTS_JSON": json.dumps({"PV-DISK-001": str(slot)})}.items():
        monkeypatch.setenv(name, str(value))
    return {"queue": queue, "receipts": receipts, "key": key, "manifest": manifest, "slot_root": slot.parent}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    values = configure_recovery(tmp_path, monkeypatch)
    incoming = tmp_path / "incoming"; incoming.mkdir()
    values["arrival"] = incoming
    store = MemoryVaultMasterStore()
    source = incoming / "synthetic.wma"; source.write_bytes(b"synthetic music only")
    owner = uuid4()
    batch = store.create_batch("incoming", str(incoming))
    item = store.record_file(batch, "incoming", scan_file(source, incoming, "Synthetic owner", owner))
    item = replace(item, state="theatre_promotion_pending", proposed_category="Music", proposed_destination="/vault/Music/synthetic.wma")
    store.items[item.source_path] = item
    request_id = queue_item(item)
    return store, item, values, values["queue"] / f"{request_id}.json"


def test_queued_music_abandon_is_signed_idempotent_and_publisher_ignores_it(staged, publisher):
    store, item, values, request = staged
    original = request.read_bytes()
    assert recovery.inspect(item, store, values["arrival"])["can_remove"]
    for _ in range(2):
        assert recovery.remove(store, item.id, item.owner_user_id, values["arrival"]).state == "arrival_removed"
    assert not Path(item.source_path).exists()
    assert request.with_suffix(".cancelled.request").exists()
    # Replay of the previously valid signed request cannot resurrect publication.
    request.write_bytes(original)
    publisher.process(values, request)
    assert not list(values["receipts"].glob("*.json"))
    assert not (values["slot_root"] / "PV-DISK-001/Music/synthetic.wma").exists()
    with pytest.raises(ValueError, match="cancelled"):
        queue_item(item)
    assert store.record_decision(item.id, "approved", "Synthetic owner") is None
    assert len([a for a in store.activity if a.action == "arrival_removed"]) == 1


def test_receipt_before_abandon_reconciles_without_touching_canonical_bytes(staged, publisher):
    store, item, values, request = staged
    publisher.process(values, request)
    target = values["slot_root"] / "PV-DISK-001/Music/synthetic.wma"
    before = target.read_bytes()
    with pytest.raises(ValueError):
        recovery.remove(store, item.id, item.owner_user_id, values["arrival"])
    assert recovery.recheck(store, item.id, item.owner_user_id, values["arrival"])["status"] == "complete"
    asset = store.get_catalogued_asset(item.proposed_destination)
    assert asset and asset.owner_user_id == item.owner_user_id
    identity = asset.id
    # A stale intake with an already-consumed receipt repairs only intake state.
    store.items[item.source_path] = item
    assert recovery.recheck(store, item.id, item.owner_user_id, values["arrival"])["status"] == "complete"
    assert store.get_catalogued_asset(item.proposed_destination).id == identity
    assert target.read_bytes() == before


@pytest.mark.parametrize("winner", ["publisher", "abandon"])
def test_racing_publisher_and_abandon_have_one_terminal_outcome(staged, publisher, winner):
    store, item, values, request = staged
    entered, proceed = threading.Event(), threading.Event()
    def first():
        with publication_lock(values["queue"]):
            entered.set(); assert proceed.wait(5)
            if winner == "publisher": publisher.process(values, request)
            else: recovery.remove(store, item.id, item.owner_user_id, values["arrival"])
    def second():
        if winner == "publisher":
            with pytest.raises(ValueError): recovery.remove(store, item.id, item.owner_user_id, values["arrival"])
        else: publisher.process(values, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first); assert entered.wait(5)
        b = pool.submit(second); proceed.set(); a.result(10); b.result(10)
    published = (values["slot_root"] / "PV-DISK-001/Music/synthetic.wma").exists()
    abandoned = store.get_item(item.id).state == "arrival_removed"
    assert published != abandoned


def _child_publish(values, request, ready):
    path = Path(__file__).parents[2] / "ops/storage/arrival-managed-publisher.py"
    module = load_publisher()
    ready.set()
    module.process(values, request)


def test_shared_os_lock_prevents_publish_after_abandon_across_processes(staged):
    store, item, values, request = staged
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(target=_child_publish, args=(values, request, ready))
    with publication_lock(values["queue"]):
        child.start(); assert ready.wait(15)
        recovery.remove(store, item.id, item.owner_user_id, values["arrival"])
    child.join(15)
    assert child.exitcode == 0
    assert not list(values["receipts"].glob("*.json"))


def test_music_retry_reuses_live_request_and_rejects_cancellation(staged):
    store, item, values, request = staged
    assert reissue_item(item, values["arrival"]) == request
    for _ in range(2):
        assert recovery.recheck(store, item.id, item.owner_user_id, values["arrival"], retry=True)["status"] == "unpublished"
    assert list(values["queue"].glob("*.json")) == [request]
    assert store.get_item(item.id).metadata["managed_request_id"] == request.stem


def test_interrupted_staging_cleanup_retains_cancellation_and_can_finish(staged, monkeypatch, publisher):
    store, item, values, request = staged
    original_request = request.read_bytes()
    primitive = recovery.safely_remove_rejected_arrival_item
    def interrupted(*args): raise OSError("synthetic unlink interruption")
    monkeypatch.setattr(recovery, "safely_remove_rejected_arrival_item", interrupted)
    with pytest.raises(OSError): recovery.remove(store, item.id, item.owner_user_id, values["arrival"])
    assert Path(item.source_path).exists()
    request.write_bytes(original_request)
    publisher.process(values, request)
    assert not list(values["receipts"].glob("*.json"))
    monkeypatch.setattr(recovery, "safely_remove_rejected_arrival_item", primitive)
    assert recovery.remove(store, item.id, item.owner_user_id, values["arrival"]).state == "arrival_removed"


def test_expired_music_retry_keeps_history_and_enqueues_once(staged):
    from datetime import UTC, datetime, timedelta
    from app.arrival_managed_publisher import ArrivalManagedPublicationRequest, queue_request, _key
    store, item, values, request = staged
    request.unlink()  # Replace only this synthetic queue fixture with an expired request.
    old = replace(ArrivalManagedPublicationRequest.create(item=item), created_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    old_path = queue_request(old, queue_root=values['queue'], key=_key())
    for _ in range(2): recovery.recheck(store, item.id, item.owner_user_id, values['arrival'], retry=True)
    assert old_path.with_suffix('.superseded.request').exists()
    assert len(list(values['queue'].glob('*.json'))) == 1


def test_owner_uuid_is_required_even_if_display_name_matches(staged):
    store, item, values, request = staged
    with pytest.raises(LookupError): recovery.remove(store, item.id, uuid4(), values['arrival'])
    assert request.exists() and Path(item.source_path).exists()


def test_interrupted_cancellation_write_is_archived_before_safe_retry(staged):
    store, item, values, _ = staged
    (values['queue'] / f'{item.id}.cancellation-writing').write_bytes(b'partial synthetic control record')
    assert recovery.remove(store, item.id, item.owner_user_id, values['arrival']).state == 'arrival_removed'
    assert len(list(values['queue'].glob('*.interrupted-cancellation'))) == 1


@pytest.mark.parametrize("problem", ["destination", "receipt", "unmounted", "source_changed", "unsigned_queue", "old_executor"])
def test_ambiguous_evidence_is_non_destructive_and_recheckable(staged, problem):
    store, item, values, request = staged
    if problem == "destination":
        target = values["slot_root"] / "PV-DISK-001/Music/synthetic.wma"; target.parent.mkdir(); target.write_bytes(b"canonical")
    elif problem == "receipt": (values["receipts"] / "bad.json").write_text('{}')
    elif problem == "unmounted": (values["slot_root"] / "PV-DISK-001").rmdir()
    elif problem == "source_changed": Path(item.source_path).write_bytes(b"changed")
    elif problem == "unsigned_queue": request.write_text('{}')
    elif problem == "old_executor": (values["queue"] / ".coordination-version").unlink()
    before = Path(item.source_path).read_bytes()
    assert recovery.inspect(item, store, values["arrival"])["status"] == "needs_recovery"
    with pytest.raises((ValueError, OSError)):
        recovery.remove(store, item.id, item.owner_user_id, values["arrival"])
    assert Path(item.source_path).read_bytes() == before
    assert recovery.recheck(store, item.id, item.owner_user_id, values["arrival"])["status"] == "needs_recovery"


def test_real_routes_classify_mixed_bulk_and_use_owner_uuid(client, tmp_path, monkeypatch):
    store = MemoryVaultMasterStore(); incoming, _ = configure(tmp_path, store)
    values = configure_recovery(tmp_path, monkeypatch)
    for name in ("safe.wma", "changed.wma"):
        (incoming / name).write_bytes(name.encode())
    authenticate(client); client.post('/api/vault-master/scan/incoming'); process_next_batch(store)
    items = store.list_items()
    (incoming / "changed.wma").write_bytes(b"changed after inventory")
    listing = client.get('/api/vault-master/recovery')
    assert listing.status_code == 200 and len(listing.json()['items']) == 2
    response = client.post('/api/vault-master/recovery', json={"action": "remove", "item_ids": [str(i.id) for i in items], "confirmation": "REMOVE FROM ARRIVAL HALL"})
    assert response.status_code == 200
    assert sorted(o['processed'] for o in response.json()['outcomes']) == [False, True]
    assert (incoming / "changed.wma").exists() and not (incoming / "safe.wma").exists()
    denied = client.post('/api/vault-master/recovery', json={"action": "remove", "item_ids": [str(uuid4())], "confirmation": "REMOVE FROM ARRIVAL HALL"})
    assert not denied.json()['outcomes'][0]['processed']


@pytest.mark.parametrize("state", sorted(recovery.STATES - recovery.TERMINAL))
def test_every_nonterminal_state_has_safe_web_recheck_and_unpublished_exit(staged, state):
    store, item, values, _ = staged
    item = replace(item, state=state); store.items[item.source_path] = item
    assert recovery.inspect(item, store, values["arrival"])["can_remove"]
    assert recovery.recheck(store, item.id, item.owner_user_id, values["arrival"])["status"] == "unpublished"


def test_postgres_music_cancel_and_published_reconciliation_preserve_catalogue(synthetic_resolver, publisher):
    import os
    from app import arrival_managed_publisher as managed
    vault, _, owner, principal, _, _, incoming = synthetic_resolver
    values = {"queue": managed._queue_root(), "receipts": managed._receipt_root(),
        "key": Path(os.environ["PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH"]), "arrival": incoming,
        "manifest": Path(os.environ["PV_SECTION_MOVE_MANIFEST"]),
        "slot_root": Path(json.loads(os.environ["PV_STORAGE_SLOT_ROOTS_JSON"])["PV-DISK-001"]).parent}
    for name, publish in [("cancel.wma", False), ("publish.wma", True)]:
        source = incoming / name; source.write_bytes(name.encode())
        batch = vault.create_batch("incoming", str(incoming))
        item = vault.record_file(batch, "incoming", scan_file(source, incoming, str(principal), owner))
        with vault._connect() as conn:
            conn.execute("UPDATE vault_master_items SET proposed_category='Music', proposed_destination=%s,state='theatre_promotion_pending' WHERE id=%s", (f"/vault/Music/{name}", item.id))
        item = vault.get_item(item.id); request_id = queue_item(item)
        if not publish:
            assert recovery.remove(vault, item.id, owner, incoming).state == "arrival_removed"
            assert recovery.remove(vault, item.id, owner, incoming).state == "arrival_removed"
            with vault._connect() as conn:
                assert conn.execute("SELECT count(*) AS n FROM vault_master_decisions WHERE item_id=%s AND decision='arrival_removed'", (item.id,)).fetchone()['n'] == 1
        else:
            publisher.process(values, values['queue'] / f'{request_id}.json')
            assert recovery.recheck(vault, item.id, owner, incoming)['status'] == 'complete'
            with vault._connect() as conn:
                tables = ['vault_assets','vault_files','vault_file_storage_placements','vault_arrival_managed_publications']
                before = {table: conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
                conn.execute("UPDATE vault_master_items SET state='theatre_promotion_pending' WHERE id=%s", (item.id,))
            with pytest.raises(ValueError): recovery.remove(vault, item.id, owner, incoming)
            assert recovery.recheck(vault, item.id, owner, incoming)['status'] == 'complete'
            with vault._connect() as conn:
                assert before == {table: conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
            assert (values['slot_root'] / 'PV-DISK-001/Music' / name).read_bytes() == name.encode()
