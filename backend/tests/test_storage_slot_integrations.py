import json
from pathlib import Path
import pytest

from app import storage_slot_integrations as integrations


class Jellyfin:
    def __init__(self, roots: list[str]): self.roots = roots; self.added = []; self.removed = []; self.refreshes = 0
    def virtual_folders(self): return [{"Name":"Movies", "CollectionType":"movies", "ItemId":"movies", "Locations":list(self.roots), "LibraryOptions":{"Enabled":True}}]
    def add_media_path(self, name, path, *, refresh_library=True): assert name == "Movies"; self.added.append(path); self.roots.append(path)
    def remove_media_path(self, name, path, *, refresh_library=True): assert name == "Movies"; self.removed.append(path); self.roots.remove(path)
    def refresh_library(self): self.refreshes += 1

def manifest(tmp_path: Path, states: dict[str, str]) -> Path:
    path = tmp_path / "active-slots.json"
    path.write_text(json.dumps({"schema": integrations.MANIFEST_SCHEMA, "movies_preserved_roots": [], "slots": {
        slot: {
            "state": state,
            "integration_mode": "slot_managed",
            "logical_mappings": {"Theatre / Movies": f"/vault/Theatre/Movies/{slot}"},
            "jellyfin_direct_bindings": [{
                "logical_root": f"/vault/Theatre/Movies/{slot}",
                "container_root": f"/media/storage-slots/{slot}/Theatre/Movies",
            }],
        } for slot, state in states.items()
    }}))
    return path

def test_add_and_retire_only_change_the_target_slot_root(monkeypatch, tmp_path):
    path = manifest(tmp_path, {"PV-DISK-001":"active", "PV-DISK-005":"active"}); monkeypatch.setattr(integrations, "manifest_path", lambda: path)
    client = Jellyfin(["/media/storage-slots/PV-DISK-001/Theatre/Movies"])
    added = integrations.reconcile_movies_slot(client, "PV-DISK-005", activate=True)
    assert client.roots == ["/media/storage-slots/PV-DISK-001/Theatre/Movies", "/media/storage-slots/PV-DISK-005/Theatre/Movies"]
    assert added["library_id"] == "movies"
    integrations.reconcile_movies_slot(client, "PV-DISK-005", activate=False)
    assert client.roots == ["/media/storage-slots/PV-DISK-001/Theatre/Movies"]
    assert client.removed == ["/media/storage-slots/PV-DISK-005/Theatre/Movies"]

def test_unexpected_or_missing_root_fails_closed(monkeypatch, tmp_path):
    path = manifest(tmp_path, {"PV-DISK-001":"active", "PV-DISK-005":"active"}); monkeypatch.setattr(integrations, "manifest_path", lambda: path)
    with pytest.raises(integrations.SlotIntegrationError): integrations.reconcile_movies_slot(Jellyfin(["/wrong"]), "PV-DISK-005", activate=True)
    data = json.loads(path.read_text()); data["slots"]["PV-DISK-005"]["state"] = "retired"; path.write_text(json.dumps(data))
    with pytest.raises(integrations.SlotIntegrationError): integrations.reconcile_movies_slot(Jellyfin(["/media/storage-slots/PV-DISK-001/Theatre/Movies", "/unexpected"]), "PV-DISK-005", activate=False)


def test_reconciliation_uses_one_normalised_root_snapshot(monkeypatch, tmp_path):
    path = manifest(tmp_path, {"PV-DISK-001": "active"}); monkeypatch.setattr(integrations, "manifest_path", lambda: path)

    class TransientJellyfin(Jellyfin):
        def __init__(self): super().__init__([]); self.reads = 0
        def virtual_folders(self):
            self.reads += 1
            roots = [
                "/media/storage-slots/PV-DISK-001/Theatre/Movies/",
                "/media//storage-slots/PV-DISK-001/Theatre/./Movies",
            ] if self.reads == 1 else ["/transiently/different"]
            return [{"Name":"Movies", "CollectionType":"movies", "ItemId":"movies", "Locations":roots, "LibraryOptions":{"Enabled":True}}]

    client = TransientJellyfin()
    result = integrations.reconcile_movies_slot(client, "PV-DISK-001", activate=True)

    assert client.reads == 1
    assert result["locations"] == ["/media/storage-slots/PV-DISK-001/Theatre/Movies"]
    assert result["root_set_evidence"]["comparisons"] == {
        "current_subset_of_managed_or_preserved": True,
        "current_equals_before": False,
        "current_equals_desired": True,
        "accepted": True,
    }


def test_root_set_rejection_carries_exact_snapshot_evidence(monkeypatch, tmp_path):
    path = manifest(tmp_path, {"PV-DISK-001": "active"}); monkeypatch.setattr(integrations, "manifest_path", lambda: path)

    with pytest.raises(integrations.SlotIntegrationError) as raised:
        integrations.reconcile_movies_slot(Jellyfin(["/unexpected/"]), "PV-DISK-001", activate=True)

    evidence = raised.value.evidence
    assert evidence["stage"] == "root_set_preflight"
    assert evidence["predicate"] == "current_subset_of_managed_or_preserved"
    assert evidence["root_sets"]["raw"]["current"] == ["/unexpected/"]
    assert evidence["root_sets"]["normalised"]["current"] == ["/unexpected"]
    assert evidence["root_sets"]["comparisons"]["accepted"] is False


def test_bindingless_active_slot_cannot_shadow_the_target_root(monkeypatch, tmp_path):
    path = tmp_path / "active-slots.json"
    path.write_text(json.dumps({
        "schema": integrations.MANIFEST_SCHEMA,
        "movies_preserved_roots": [],
        "slots": {
            "PV-DISK-001": {
                "state": "migrating",
                "integration_mode": "slot_managed",
                "logical_mappings": {"Theatre / Movies": "/vault/Theatre/Movies"},
                "jellyfin_direct_bindings": [{
                    "logical_root": "/vault/Theatre/Movies",
                    "container_root": "/media/movies",
                }],
            },
            "PV-DISK-002": {
                "state": "active",
                "integration_mode": "slot_managed",
                "logical_mappings": {},
                "jellyfin_direct_bindings": [],
            },
        },
    }))
    monkeypatch.setattr(integrations, "manifest_path", lambda: path)

    result = integrations.reconcile_movies_slot(Jellyfin(["/media/movies"]), "PV-DISK-001", activate=True)

    desired = result["root_set_evidence"]["normalised"]["desired"]
    assert set(desired) == {"/media/movies"}
    assert None not in desired
    assert result["root"] == "/media/movies"


def test_none_root_remains_invalid() -> None:
    with pytest.raises(integrations.SlotIntegrationError, match="Movies desired roots are invalid"):
        integrations._root_snapshot(
            current=["/media/movies"],
            desired={None},
            managed={"/media/movies"},
            before=set(),
            preserved=[],
        )
