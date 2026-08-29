"""Authoritative, slot-aware storage placement primitives for Vault Master.

Logical Vault paths remain catalogue-facing identity.  This module is the only
place that turns a file's durable placement into a physical slot-root path.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable


@dataclass(frozen=True)
class EligibleSlot:
    slot_id: str
    area: str
    usable_free_bytes: int
    healthy: bool
    available: bool
    managed_root: Path


@dataclass(frozen=True)
class FilePlacement:
    slot_id: str
    relative_path: str


def select_slot(slots: Iterable[EligibleSlot], area: str, required_bytes: int) -> EligibleSlot:
    """Choose the greatest usable eligible capacity, then persistent slot ID."""
    if required_bytes < 0:
        raise ValueError("required capacity cannot be negative")
    candidates = [
        slot for slot in slots
        if slot.area == area and slot.healthy and slot.available and slot.usable_free_bytes >= required_bytes
    ]
    if not candidates:
        raise ValueError("no healthy commissioned storage slot has enough usable capacity")
    return sorted(candidates, key=lambda slot: (-slot.usable_free_bytes, slot.slot_id))[0]


def validate_relative_path(relative_path: str) -> PurePosixPath:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("storage placement relative path is invalid")
    return path


class StorageResolver:
    """Resolve durable file placement without exposing slot paths to callers."""

    def resolve(self, placement: FilePlacement, slots: Iterable[EligibleSlot]) -> Path:
        slot = next((item for item in slots if item.slot_id == placement.slot_id), None)
        if slot is None or not slot.available:
            raise ValueError("authoritative storage slot is unavailable")
        relative = validate_relative_path(placement.relative_path)
        root = slot.managed_root.resolve(strict=True)
        candidate = (root.joinpath(*relative.parts)).resolve(strict=True)
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise ValueError("authoritative storage placement is unavailable")
        return candidate


MANIFEST_SCHEMA = "personal-vault.slot-managed-manifest.v1"
SLOT_PUBLISH_ROOT = Path(os.getenv("PV_STORAGE_SLOT_ROOT", "/vault-storage-slots"))


def configured_slot_roots() -> dict[str, Path]:
    """Read final managed resolver roots, never host paths.

    The environment value is an isolated-test override.  Production reads one
    plural managed manifest; it does not fall back to predecessor schemas.
    """
    raw = os.getenv("PV_STORAGE_SLOT_ROOTS_JSON", "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("configured storage slot roots are invalid") from error
    if not isinstance(value, dict):
        raise ValueError("configured storage slot roots are invalid")
    manifest_backed = False
    if not value:
        manifest_backed = True
        configured_file = Path(os.getenv(
            "PV_STORAGE_SLOT_MANIFEST_FILE",
            "/var/lib/personal-vault/metadata/storage-control/active-slots.json",
        ))
        try:
            document = json.loads(configured_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("configured storage slot manifest is unavailable") from error
        if document.get("schema") != MANIFEST_SCHEMA or not isinstance(document.get("slots"), dict):
            raise ValueError("configured storage slot manifest is invalid")
        value = {
            slot_id: entry.get("resolver_root")
            for slot_id, entry in document["slots"].items()
            if isinstance(slot_id, str) and isinstance(entry, dict)
            and entry.get("state") == "active"
            and entry.get("integration_mode") == "slot_managed"
        }
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(path, str) for key, path in value.items()):
        raise ValueError("configured storage slot roots are invalid")
    if manifest_backed:
        for slot_id, resolver_root in value.items():
            if Path(resolver_root) != SLOT_PUBLISH_ROOT / slot_id:
                raise ValueError("configured storage slot resolver root is not canonical")
    return {slot_id: Path(path) for slot_id, path in value.items()}


def resolve_metadata_placement(metadata: object) -> Path | None:
    """Resolve the published placement shape, or return ``None`` for legacy files."""
    if not isinstance(metadata, dict):
        return None
    placement = metadata.get("storage_placement")
    if placement is None:
        return None
    if not isinstance(placement, dict) or not isinstance(placement.get("slot_id"), str) or not isinstance(placement.get("relative_path"), str):
        raise ValueError("catalogued storage placement is invalid")
    roots = configured_slot_roots()
    root = roots.get(placement["slot_id"])
    if root is None:
        raise ValueError("authoritative storage slot is not mounted for the backend")
    return StorageResolver().resolve(
        FilePlacement(placement["slot_id"], placement["relative_path"]),
        [EligibleSlot(placement["slot_id"], "", 0, True, True, root)],
    )
