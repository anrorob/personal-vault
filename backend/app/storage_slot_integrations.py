"""Fail-closed reconciliation of independently mounted storage slots.

The root storage executor owns the durable manifest.  This module is the only
credential-bearing caller that changes Jellyfin library media paths.  It never
replaces a virtual folder: it uses Jellyfin's supported add/remove media-path
operations and verifies the resulting locations.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import get_metadata_storage_root

# The plural managed record is the only runtime contract.  The VM-060
# predecessor is intentionally understood only by the root reconciliation tool.
MANIFEST_SCHEMA = "personal-vault.slot-managed-manifest.v1"


class SlotIntegrationError(RuntimeError):
    def __init__(self, message: str, *, evidence: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence


def manifest_path() -> Path:
    return get_metadata_storage_root() / "storage-control" / "active-slots.json"


def active_slot_manifest() -> dict[str, object]:
    try:
        document = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SlotIntegrationError("active-slot manifest is unavailable") from error
    if document.get("schema") != MANIFEST_SCHEMA or not isinstance(document.get("slots"), dict):
        raise SlotIntegrationError("active-slot manifest is invalid")
    return document


def _movies_library(client: object) -> dict[str, object]:
    virtual_folders = getattr(client, "virtual_folders")
    folders = virtual_folders()
    matches = [
        item for item in folders if isinstance(item, dict)
        and item.get("Name") == "Movies" and item.get("CollectionType") == "movies"
    ] if isinstance(folders, list) else []
    if len(matches) != 1:
        raise SlotIntegrationError("expected exactly one Movies library")
    return matches[0]


def _locations(folder: dict[str, object]) -> list[str]:
    locations = folder.get("Locations")
    if not isinstance(locations, list) or any(not isinstance(path, str) or not path.startswith("/") for path in locations):
        raise SlotIntegrationError("Movies library locations are invalid")
    return locations


def _canonical_root(path: str) -> str:
    """Return one deterministic POSIX representation for a library root."""
    if not path.startswith("/") or "\0" in path:
        raise SlotIntegrationError("Movies library locations are invalid")
    parts = path.split("/")
    if ".." in parts:
        raise SlotIntegrationError("Movies library locations are invalid")
    canonical = "/" + "/".join(part for part in parts if part not in {"", "."})
    return canonical


def _normalised_roots(paths: object, *, error: str) -> tuple[str, ...]:
    if not isinstance(paths, (list, set, tuple)) or any(not isinstance(path, str) for path in paths):
        raise SlotIntegrationError(error)
    try:
        return tuple(sorted({_canonical_root(path) for path in paths}))
    except SlotIntegrationError as cause:
        raise SlotIntegrationError(error) from cause


def _root_snapshot(
    *, current: list[str], desired: object, managed: object, before: object, preserved: object,
) -> dict[str, object]:
    raw = {
        "current": list(current),
        "desired": sorted(desired) if isinstance(desired, set) else list(desired),
        "managed": sorted(managed) if isinstance(managed, set) else list(managed),
        "before": sorted(before) if isinstance(before, set) else list(before),
        "preserved": list(preserved),
    }
    normalised = {
        name: list(_normalised_roots(paths, error=f"Movies {name} roots are invalid"))
        for name, paths in raw.items()
    }
    current_set = set(normalised["current"])
    managed_set = set(normalised["managed"])
    preserved_set = set(normalised["preserved"])
    before_set = set(normalised["before"]) | preserved_set
    desired_set = set(normalised["desired"]) | preserved_set
    comparisons = {
        "current_subset_of_managed_or_preserved": current_set <= managed_set | preserved_set,
        "current_equals_before": current_set == before_set,
        "current_equals_desired": current_set == desired_set,
    }
    comparisons["accepted"] = bool(
        comparisons["current_subset_of_managed_or_preserved"]
        and (comparisons["current_equals_before"] or comparisons["current_equals_desired"])
    )
    return {"raw": raw, "normalised": normalised, "comparisons": comparisons}


def _movies_binding(slot: dict[str, object]) -> str | None:
    bindings = slot.get("jellyfin_direct_bindings", [])
    if not isinstance(bindings, list):
        raise SlotIntegrationError("slot Jellyfin bindings are invalid")
    mappings = slot.get("logical_mappings")
    if not isinstance(mappings, dict):
        raise SlotIntegrationError("slot logical mappings are invalid")
    movie_logical = mappings.get("Theatre / Movies")
    matches = [
        item.get("container_root") for item in bindings
        if isinstance(item, dict)
        and item.get("logical_root") == movie_logical
        and isinstance(item.get("container_root"), str)
        and str(item["container_root"]).startswith("/")
    ]
    if len(matches) > 1:
        raise SlotIntegrationError("slot has duplicate Movies bindings")
    return _canonical_root(matches[0]) if matches else None


def reconcile_movies_slot(client: object, slot_id: str, *, activate: bool) -> dict[str, object]:
    """Add or remove one manifest-owned Movies root and verify it.

    Current state must be precisely the expected previous set or the desired
    set (retry).  Any other drift is refused before the supported Jellyfin API
    is called, preserving both library identity and all unrelated roots.
    """
    manifest = active_slot_manifest(); slots = manifest["slots"]
    assert isinstance(slots, dict)
    slot = slots.get(slot_id)
    if not isinstance(slot, dict) or slot.get("integration_mode") != "slot_managed":
        raise SlotIntegrationError("slot has no managed Movies root")
    target_root = _movies_binding(slot)
    if target_root is None:
        raise SlotIntegrationError("slot has no managed Movies root")
    managed_roots: set[str] = set()
    active_roots: set[str] = set()
    for candidate_slot in slots.values():
        if not isinstance(candidate_slot, dict):
            continue
        candidate_binding = _movies_binding(candidate_slot)
        if candidate_binding is None:
            continue
        managed_roots.add(candidate_binding)
        if candidate_slot.get("state") == "active":
            active_roots.add(candidate_binding)
    desired_roots = active_roots | ({target_root} if activate else set())
    desired_roots = desired_roots if activate else active_roots - {target_root}
    before_roots = desired_roots - {target_root} if activate else desired_roots | {target_root}
    preserved = manifest.get("movies_preserved_roots", [])
    if not isinstance(preserved, list) or any(not isinstance(path, str) for path in preserved):
        raise SlotIntegrationError("Movies preserved-root manifest is invalid")
    folder = _movies_library(client)
    snapshot = _root_snapshot(current=_locations(folder), desired=desired_roots, managed=managed_roots, before=before_roots, preserved=preserved)
    normalised = snapshot["normalised"]; comparisons = snapshot["comparisons"]
    assert isinstance(normalised, dict) and isinstance(comparisons, dict)
    current_roots = set(normalised["current"])
    if comparisons["current_subset_of_managed_or_preserved"] is not True:
        raise SlotIntegrationError("Movies library contains an unmanaged root", evidence={"stage": "root_set_preflight", "predicate": "current_subset_of_managed_or_preserved", "root_sets": snapshot})
    if comparisons["accepted"] is not True:
        raise SlotIntegrationError("Movies library root set differs from the active-slot manifest", evidence={"stage": "root_set_preflight", "predicate": "current_equals_before_or_desired", "root_sets": snapshot})
    changed = False
    if target_root not in current_roots and activate:
        getattr(client, "add_media_path")("Movies", target_root, refresh_library=True)
        changed = True
    elif target_root in current_roots and not activate:
        getattr(client, "remove_media_path")("Movies", target_root, refresh_library=True)
        changed = True
    if changed:
        post_mutation_raw = _locations(_movies_library(client))
        verified = set(_normalised_roots(post_mutation_raw, error="Movies library locations are invalid"))
        expected = set(normalised["desired"]) | set(normalised["preserved"])
        if verified != expected:
            post_mutation = {"raw": post_mutation_raw, "normalised": sorted(verified), "expected": sorted(expected), "matches": False}
            raise SlotIntegrationError("Jellyfin did not apply the requested slot-root change", evidence={"stage": "root_set_post_mutation", "predicate": "current_equals_desired", "root_sets": snapshot, "post_mutation": post_mutation})
    else:
        verified = current_roots
    getattr(client, "refresh_library")()
    return {"library_id": folder.get("ItemId"), "slot_id": slot_id, "root": target_root, "locations": sorted(verified), "root_set_evidence": snapshot}
