from pathlib import Path

import pytest

from app.storage_placement import EligibleSlot, FilePlacement, StorageResolver, resolve_metadata_placement, select_slot


def slot(slot_id: str, free: int, root: Path, *, healthy: bool = True) -> EligibleSlot:
    return EligibleSlot(slot_id, "Theatre / Movies", free, healthy, True, root)


def test_placement_prefers_greatest_free_capacity_then_slot_id(tmp_path: Path) -> None:
    first = slot("PV-DISK-005", 100, tmp_path / "five")
    second = slot("PV-DISK-001", 100, tmp_path / "one")
    assert select_slot([first, second], "Theatre / Movies", 10).slot_id == "PV-DISK-001"


def test_placement_excludes_unhealthy_and_insufficient_slots(tmp_path: Path) -> None:
    assert select_slot([slot("PV-DISK-001", 100, tmp_path / "one", healthy=False), slot("PV-DISK-005", 20, tmp_path / "five")], "Theatre / Movies", 10).slot_id == "PV-DISK-005"


def test_resolver_fails_closed_for_missing_or_escaping_placement(tmp_path: Path) -> None:
    root = tmp_path / "slot"; root.mkdir(); (root / "movie.mkv").write_bytes(b"movie")
    resolver = StorageResolver(); available = slot("PV-DISK-005", 100, root)
    assert resolver.resolve(FilePlacement("PV-DISK-005", "movie.mkv"), [available]) == root / "movie.mkv"
    with pytest.raises(ValueError): resolver.resolve(FilePlacement("PV-DISK-005", "../outside"), [available])
    with pytest.raises(ValueError): resolver.resolve(FilePlacement("PV-DISK-009", "movie.mkv"), [available])


def test_metadata_placement_uses_only_configured_backend_slot_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "slot"; root.mkdir(); (root / "Theatre" / "Movies").mkdir(parents=True); path = root / "Theatre" / "Movies" / "movie.mkv"; path.write_bytes(b"movie")
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", '{"PV-DISK-005":"' + str(root).replace("\\", "\\\\") + '"}')
    assert resolve_metadata_placement({"storage_placement":{"slot_id":"PV-DISK-005","relative_path":"Theatre/Movies/movie.mkv"}}) == path
    with pytest.raises(ValueError): resolve_metadata_placement({"storage_placement":{"slot_id":"PV-DISK-006","relative_path":"Theatre/Movies/movie.mkv"}})


def test_metadata_placement_can_use_additive_root_owned_reconciliation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.storage_placement as placement

    root_parent = tmp_path / "storage-slots"
    root = root_parent / "PV-DISK-001"
    root.mkdir(parents=True)
    mapping = tmp_path / "active-slots.json"
    mapping.write_text('{"schema":"personal-vault.slot-managed-manifest.v1","slots":{"PV-DISK-001":{"state":"active","integration_mode":"slot_managed","resolver_root":"' + str(root).replace("\\", "\\\\") + '"}}}')
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", "{}")
    monkeypatch.setenv("PV_STORAGE_SLOT_MANIFEST_FILE", str(mapping))
    monkeypatch.setattr(placement, "SLOT_PUBLISH_ROOT", root_parent)
    assert placement.configured_slot_roots() == {"PV-DISK-001": root}


def test_final_manifest_rejects_noncanonical_resolver_namespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.storage_placement as placement

    mapping = tmp_path / "active-slots.json"
    mapping.write_text('{"schema":"personal-vault.slot-managed-manifest.v1","slots":{"PV-DISK-001":{"state":"active","integration_mode":"slot_managed","resolver_root":"/vault-storage-slots/PV-DISK-001"}}}')
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", "{}")
    monkeypatch.setenv("PV_STORAGE_SLOT_MANIFEST_FILE", str(mapping))
    monkeypatch.setattr(placement, "SLOT_PUBLISH_ROOT", tmp_path / "storage-slots")
    with pytest.raises(ValueError, match="not canonical"):
        placement.configured_slot_roots()
