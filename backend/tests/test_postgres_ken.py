from concurrent.futures import ThreadPoolExecutor
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg import sql
import pytest

from app import ken_service as lab
from tests.test_ken_service import result_binding


@pytest.fixture
def pg_ken(monkeypatch):
    monkeypatch.setattr(lab, 'ADAPTER', lab.LocalModelAdapter())
    url = os.getenv("PV_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL is not configured")
    schema = "ken_test_" + uuid4().hex
    owner, asset = uuid4(), uuid4()
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(url, options=f"-c search_path={schema}")
    with psycopg.connect(scoped) as conn:
        conn.execute("CREATE TABLE vault_assets(id UUID PRIMARY KEY, owner_user_id UUID, canonical JSONB, location TEXT, metadata_provenance JSONB DEFAULT '{}'::jsonb)")
        conn.execute("INSERT INTO vault_assets(id,owner_user_id,canonical) VALUES(%s,%s,'{\"description\":\"original\",\"people\":[\"existing\"],\"tags\":[\"original\"],\"routing\":\"unchanged\"}')", (asset, owner))
    with psycopg.connect(scoped) as conn:
        conn.execute("CREATE TABLE vault_people(id UUID PRIMARY KEY,owner_user_id UUID,display_name TEXT,active BOOLEAN DEFAULT TRUE)")
        conn.execute("CREATE TABLE vault_asset_people(asset_id UUID,person_id UUID,owner_user_id UUID,source TEXT,active BOOLEAN DEFAULT TRUE)")
        conn.execute("CREATE TABLE vault_asset_people_decisions(asset_id UUID,person_id UUID,owner_user_id UUID,decision TEXT,active BOOLEAN DEFAULT TRUE)")
    store = lab.KenStore(scoped)
    store.initialize()
    store.initialize()
    yield store, asset, owner
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_database_atomic_duplicate_admission_and_history(pg_ken):
    store, asset, owner = pg_ken
    config = lab.configuration()
    config.pop("metadata_destination")
    with ThreadPoolExecutor(max_workers=6) as pool:
        runs = list(pool.map(lambda _: store.queue(asset, owner, config), range(12)))
    assert len({run["id"] for run in runs}) == 1
    run = runs[0]
    from tests.test_ken_config import complete
    complete(store,run,"Synthetic first")
    second = store.queue(asset, owner, config)
    assert second["id"] != run["id"]
    assert lab.KenStore(store.conninfo).get(run["id"])["result"]["description"] == "Synthetic first"
    assert store.history(asset, uuid4()) == []
    with pytest.raises(ValueError):
        store.queue(asset, uuid4(), config)


def test_database_process_lock_excludes_other_workers_and_restart_recovers(pg_ken):
    store, asset, owner = pg_ken
    run = store.queue(asset, owner, lab.configuration())
    with store.worker_lock() as acquired:
        assert acquired
        with lab.KenStore(store.conninfo).worker_lock() as second:
            assert not second
        store.claim()
    with store.worker_lock() as acquired:
        assert acquired
        store.recover()
    saved = store.get(run["id"])
    assert saved["status"] == "failed"
    assert saved["completed_at"] is not None
    assert saved["error"] == "Interrupted by worker restart"










@pytest.mark.parametrize("field", ["asset_id", "run_id", "input_fingerprint"])
def test_database_rejects_cross_run_result_even_if_worker_is_bypassed(pg_ken, field):
    store, asset, owner = pg_ken
    config = lab.configuration()
    config.pop("metadata_destination")
    config["input_integrity"] = {"input_fingerprint": "a" * 64}
    run = store.queue(asset, owner, config)
    good = {"asset_id": str(asset), "run_id": str(run["id"]), "input_fingerprint": "a" * 64,"correction_fingerprint":run["configuration"]["correction_fingerprint"]}
    with pytest.raises(psycopg.errors.CheckViolation):
        store.update(run["id"], "completed", result={**good, field: "stale"})
    assert store.get(run["id"])["result"] is None
    store.update(run["id"], "completed", result=good)
    assert store.bindings(asset, uuid4()) == []
    record = store.bindings(asset, owner)[0]
    assert record["persisted_result_asset_id"] == asset
    assert record["persisted_result_run_id"] == run["id"]
    assert record["embedded_result_run_id"] == str(run["id"])
    assert "result" not in record and "description" not in record


@pytest.mark.parametrize("field", ["resolved_input_sha256", "resolved_input_size", "request_nonce", "runtime_opened_sha256"])
def test_database_cannot_complete_a_run_with_mismatched_service_attestation(pg_ken, field):
    store, asset, owner = pg_ken
    config = lab.configuration()
    config.pop("metadata_destination")
    config["input_integrity"] = {"input_fingerprint": "a" * 64}
    payload = {"asset_id": str(asset), "input_fingerprint": "a" * 64, "input_mode": "native_video"}
    lab.expect_input(config, payload, "b" * 64, 42)
    run = store.queue(asset, owner, config)
    payload["run_id"] = str(run["id"])
    from app import ken_config as ken
    payload.update(native_parameters={**ken.policy(3000),"prepared_stream":{"duration_ms":3000}},correction_fingerprint=run["configuration"]["correction_fingerprint"])
    result = result_binding(payload)
    result["input_attestation"][field] = 43 if field.endswith("size") else "stale"
    with pytest.raises(psycopg.errors.CheckViolation):
        store.update(run["id"], "completed", result=result)
    # Diagnostic mismatch metadata can still be persisted on a failed run.
    store.update(run["id"], "failed", result=result, error="Input integrity verification failed")
    assert store.get(run["id"])["result"]["input_attestation"][field] == result["input_attestation"][field]
    technical = store.bindings(asset, owner)[0]
    assert technical["expected_input_sha256"] == "b" * 64
    assert technical["resolved_input_sha256"] == result["input_attestation"]["resolved_input_sha256"]
    assert "result" not in technical and "description" not in technical and "raw_response" not in technical
