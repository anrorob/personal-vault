"""Synthetic physical relocation using the production managed executor code."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.arrival_managed_publisher import ResolverRelocationRequest, queue_request, verify_receipt, verify_request


@pytest.fixture
def executor(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "ops/storage/arrival-managed-publisher.py"
    spec = importlib.util.spec_from_file_location("resolver_recovery_executor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("QUEUE", "RECEIPTS", "ARRIVAL", "SLOT_ROOT"):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setattr(module, name, root)
    key = tmp_path / "key"
    key.write_bytes(b"synthetic-recovery-test-key")
    monkeypatch.setattr(module, "KEY", key)
    manifest = {"schema": module.SCHEMA, "slots": {}}
    for number, area, logical in [(2, "Home Videos", "/vault/Home Videos"), (3, "Theatre / TV Shows", "/vault/Theatre/TV Shows")]:
        slot = f"PV-DISK-{number:03d}"
        root = module.SLOT_ROOT / slot
        root.mkdir()
        manifest["slots"][slot] = dict(state="active", integration_mode="slot_managed", hardware_id=f"synthetic-{number}", filesystem_uuid=f"synthetic-uuid-{number}", areas=[area], logical_mappings={area: logical}, resolver_root=str(root))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(module, "MANIFEST", manifest_path)
    source = module.SLOT_ROOT / "PV-DISK-002/Home Videos/original-disc-track.mkv"
    source.parent.mkdir()
    source.write_bytes(b"synthetic verified episode")
    target = module.SLOT_ROOT / "PV-DISK-003/Theatre/TV Shows/Example/Season 02/Example - S02E03.mkv"
    request = ResolverRelocationRequest(uuid4(), uuid4(), uuid4(), "Home Videos/original-disc-track.mkv",
        "/vault/Theatre/TV Shows/Example/Season 02/Example - S02E03.mkv", hashlib.sha256(source.read_bytes()).hexdigest(),
        source.stat().st_size, datetime.now(timezone.utc).isoformat(),
        dict(schema="personal-vault.tv-resolver-relocation.v1", source_logical_path="/vault/Home Videos/original-disc-track.mkv",
            asset_id=str(uuid4()), file_id=str(uuid4()), batch_id=str(uuid4()), track_id=str(uuid4())))
    return module, request, source, target


def submit(module, request):
    return queue_request(request, queue_root=module.QUEUE, key=module.KEY.read_bytes())


def test_managed_relocation_preserves_bytes_and_receipt_identity(executor):
    module, request, source, target = executor
    path = submit(module, request)
    assert verify_request(json.loads(path.read_text()), module.KEY.read_bytes()) == request
    module.process_request(path)
    assert not source.exists() and module.digest(target) == request.expected_sha256
    document = json.loads(module.receipt_path(str(request.request_id)).read_text())
    receipt = verify_receipt(document, module.KEY.read_bytes())
    assert receipt["recovery"] == request.recovery
    assert receipt["slot_id"] == "PV-DISK-003" and receipt["logical_area"] == "Theatre / TV Shows"
    assert not list(target.parent.glob("*.partial"))
    # Retry after receipt/source cleanup retains the exact same receipt.
    module.process_request(submit(module, request))
    assert json.loads(module.receipt_path(str(request.request_id)).read_text()) == document


def test_relocation_conflict_never_overwrites_or_removes_source(executor):
    module, request, source, target = executor
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different existing content")
    with pytest.raises(ValueError, match="collision"):
        module.process_request(submit(module, request))
    assert target.read_bytes() == b"different existing content" and source.is_file()
    assert not list(module.RECEIPTS.glob("*.json"))


def test_exact_existing_destination_is_reconciled_without_second_copy(executor):
    module, request, source, target = executor
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    before = target.stat().st_mtime_ns
    module.process_request(submit(module, request))
    assert target.stat().st_mtime_ns == before and not source.exists()
    assert module.digest(target) == request.expected_sha256


def test_changed_source_cannot_create_a_recovery_receipt(executor):
    module, request, source, target = executor
    source.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="verification"):
        module.process_request(submit(module, request))
    assert source.is_file() and not target.exists() and not list(module.RECEIPTS.glob("*.json"))


def test_relocation_is_signed_and_restricted_to_reviewed_logical_areas(executor):
    module, request, source, target = executor
    path = submit(module, request)
    document = json.loads(path.read_text())
    document["request"]["logical_destination"] = "/vault/Home Videos/elsewhere.mkv"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="invalid request"):
        module.process_request(path)
    path.unlink()
    from dataclasses import replace
    with pytest.raises(ValueError, match="areas"):
        module.process_request(submit(module, replace(request, logical_destination="/vault/Home Videos/elsewhere.mkv")))
    assert source.is_file() and not target.exists()


def test_ordinary_arrival_request_contract_is_unchanged(executor):
    from app.arrival_managed_publisher import ArrivalManagedPublicationRequest
    module, recovery, source, target = executor
    ordinary = ArrivalManagedPublicationRequest(**{k: v for k, v in asdict(recovery).items() if k != "recovery"})
    staged = module.ARRIVAL / ordinary.source_relative_path
    staged.parent.mkdir(parents=True)
    staged.write_bytes(source.read_bytes())
    module.process_request(submit(module, ordinary))
    assert source.exists() and not staged.exists() and target.exists()
    receipt = verify_receipt(json.loads(module.receipt_path(str(ordinary.request_id)).read_text()), module.KEY.read_bytes())
    assert "recovery" not in receipt
