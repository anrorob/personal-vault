import hashlib
import hmac
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.arrival_managed_publisher import _payload
from app.vault_supplier_filename_remediation import Candidate, PostgresSupplierFilenameRemediationExecutor, RemediationRefused, dry_run, plan


def candidate(**changes):
    values = dict(record_id="file-1", owner_user_id="owner", transfer_id="transfer-1", current_filename="Rocky 2 (Vault Supplier 743c0890).mkv", original_filename="Rocky 2.mkv", checksum_matches=True, size_matches=True, owner_matches=True, current_title="Rocky 2 (Vault Supplier 743c0890)", title_provenance="filename")
    values.update(changes); return Candidate(**values)


def test_planner_is_non_mutating_and_protects_review_cases():
    assert dry_run([candidate()])["files_changed"] == 0
    assert plan(candidate()).proposed_title == "Rocky 2"
    assert plan(candidate(has_user_override=True)).classification == "manual_metadata_protected"


@pytest.fixture
def remediation_postgres(tmp_path, monkeypatch):
    base = os.getenv("PV_TEST_DATABASE_URL")
    if not base: pytest.skip("PV_TEST_DATABASE_URL is not configured")
    schema = f"supplier_remediation_{uuid4().hex}"
    with psycopg.connect(base) as c: c.execute(f"CREATE SCHEMA {schema}")
    conninfo = psycopg.conninfo.make_conninfo(base, options=f"-c search_path={schema},public")
    with psycopg.connect(conninfo) as c:
        c.execute("CREATE TABLE vault_assets (id UUID PRIMARY KEY, owner_user_id UUID NOT NULL, asset_type TEXT NOT NULL, display_title TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, metadata_provenance JSONB NOT NULL DEFAULT '{}'::jsonb, user_overrides JSONB NOT NULL DEFAULT '{}'::jsonb, effective_metadata JSONB NOT NULL DEFAULT '{}'::jsonb, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE vault_files (id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), vault_path TEXT NOT NULL UNIQUE, filename TEXT NOT NULL, size_bytes BIGINT NOT NULL, sha256 TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE vault_file_storage_placements (file_id UUID PRIMARY KEY REFERENCES vault_files(id), slot_id TEXT NOT NULL, relative_path TEXT NOT NULL)")
        c.execute("CREATE TABLE vault_supplier_transfer_sessions (transfer_id UUID PRIMARY KEY, user_id UUID NOT NULL, filename TEXT NOT NULL, original_filename TEXT, total_size BIGINT NOT NULL, expected_sha256 TEXT NOT NULL, state TEXT NOT NULL)")
        c.execute("CREATE TABLE vault_asset_history (id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), action TEXT NOT NULL, username TEXT NOT NULL, previous_values JSONB NOT NULL, current_values JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    old, new, payload = "Rocky 2 (Vault Supplier 743c0890).mkv", "Rocky 2.mkv", b"disposable Rocky"
    digest, owner, asset, file, transfer = hashlib.sha256(payload).hexdigest(), uuid4(), uuid4(), uuid4(), uuid4(); old_path = f"/vault/Theatre/Movies/{old}"
    with psycopg.connect(conninfo) as c:
        c.execute("INSERT INTO vault_assets(id,owner_user_id,asset_type,display_title,metadata,metadata_provenance,user_overrides,effective_metadata) VALUES (%s,%s,'Movies',%s,%s,%s,'{}'::jsonb,%s)", (asset,owner,Path(old).stem,Jsonb({"source_context":{"transfer_id":str(transfer)}}),Jsonb({"display_title":"filename"}),Jsonb({"display_title":Path(old).stem})))
        c.execute("INSERT INTO vault_files VALUES (%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)", (file,asset,old_path,old,len(payload),digest))
        c.execute("INSERT INTO vault_file_storage_placements VALUES (%s,'PV-DEV-DISK-001',%s)", (file,f"Theatre/Movies/{old}"))
        c.execute("INSERT INTO vault_supplier_transfer_sessions VALUES (%s,%s,%s,%s,%s,%s,'finalized')", (transfer,owner,new,new,len(payload),digest))
    runtime, secrets = tmp_path / "example-runtime", tmp_path / "secrets"
    queue, receipts, key = runtime / "operations" / "supplier-remediation-requests", runtime / "operations" / "supplier-remediation-receipts", secrets / "arrival-managed-publisher.key"
    slot = runtime / "storage-slots" / "PV-DEV-DISK-001"; source = slot / "Theatre" / "Movies" / old; source.parent.mkdir(parents=True); source.write_bytes(payload)
    key.parent.mkdir(); key.write_bytes(b"test-key"); key.chmod(0o600)
    monkeypatch.setenv("PV_SUPPLIER_REMEDIATION_QUEUE",str(queue)); monkeypatch.setenv("PV_SUPPLIER_REMEDIATION_RECEIPTS",str(receipts)); monkeypatch.setenv("PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH",str(key))
    executor=PostgresSupplierFilenameRemediationExecutor(conninfo); executor.initialize(); executor.initialize()
    change=plan(Candidate(str(file),str(owner),str(transfer),old,new,True,True,True,Path(old).stem,"filename",theatre=True,duration_seconds=3600,current_vault_path=old_path,current_relative_path=f"Theatre/Movies/{old}"))
    try: yield locals()
    finally:
        with psycopg.connect(base) as c: c.execute(f"DROP SCHEMA {schema} CASCADE")


def write_receipt(f, *, alter=None):
    request=json.loads(next(f["queue"].glob("*.json")).read_text())["request"]
    receipt={"schema":request["schema"],"action_id":request["action_id"],"slot_id":request["slot_id"],"old_relative_path":request["old_relative_path"],"new_relative_path":request["new_relative_path"],"expected_sha256":request["expected_sha256"],"expected_size_bytes":request["expected_size_bytes"],"post_sha256":request["expected_sha256"],"post_size_bytes":request["expected_size_bytes"],"status":"completed","started_at":"2026-01-01T00:00:00+00:00","completed_at":"2026-01-01T00:00:01+00:00"}
    if alter: alter(receipt)
    f["receipts"].mkdir(); f["receipts"].joinpath(f"{receipt['action_id']}.json").write_text(json.dumps({"receipt":receipt,"signature":hmac.new(f["key"].read_bytes(),_payload(receipt),hashlib.sha256).hexdigest()}))


def test_executor_queues_then_only_verified_receipt_completes(remediation_postgres):
    f=remediation_postgres
    assert f["executor"].apply(f["change"])["status"] == "requested"
    with psycopg.connect(f["conninfo"]) as c: assert c.execute("SELECT filename FROM vault_files").fetchone()[0] == f["old"]
    write_receipt(f); assert f["executor"].reconcile_next_receipt() is not None
    with psycopg.connect(f["conninfo"]) as c:
        assert c.execute("SELECT filename FROM vault_files").fetchone() == (f["new"],)
        assert c.execute("SELECT status FROM vault_supplier_filename_remediation_actions").fetchone() == ("completed",)
        assert c.execute("SELECT count(*) FROM vault_asset_history").fetchone() == (1,)
    assert f["executor"].apply(f["change"])["status"] == "already_completed"


def test_mismatched_receipt_is_not_accepted(remediation_postgres):
    f=remediation_postgres; f["executor"].apply(f["change"]); write_receipt(f, alter=lambda r:r.update(slot_id="PV-DEV-DISK-999"))
    assert f["executor"].reconcile_next_receipt() is None
    with psycopg.connect(f["conninfo"]) as c: assert c.execute("SELECT status FROM vault_supplier_filename_remediation_actions").fetchone() == ("applying",)




@pytest.mark.parametrize("mutation",["owner","override","stale","provenance"])
def test_preconditions_fail_closed_before_queue(remediation_postgres,mutation):
    f=remediation_postgres
    with psycopg.connect(f["conninfo"]) as c:
        if mutation=="owner": c.execute("UPDATE vault_assets SET owner_user_id=%s",(uuid4(),))
        elif mutation=="override": c.execute("UPDATE vault_assets SET user_overrides=%s",(Jsonb({"display_title":"manual"}),))
        elif mutation=="stale": c.execute("UPDATE vault_files SET vault_path='/vault/Theatre/Movies/changed.mkv'")
        else: c.execute("UPDATE vault_assets SET metadata='{}'::jsonb")
    with pytest.raises(RemediationRefused): f["executor"].apply(f["change"])
    assert not f["queue"].exists()
