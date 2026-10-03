"""Explicit Gallery reclassification through a durable, managed-storage worker.

HTTP accepts a logical section only. The worker owns physical writes; the API
mount stays read-only. A verified migration copy is never authoritative until
the existing file/asset and placement are committed together. Cleanup follows
that commit and can be retried without re-ingestion or a second asset.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import psycopg
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, StrictBool

from app.auth import AuthenticatedUsername
from app.runtime_identity import source_repository_allowed
from app.storage_placement import EligibleSlot, configured_slot_roots, select_slot, validate_relative_path, commissioned_destination_slots
from app.vault_master import CataloguedAsset, PostgresVaultMasterStore, asset_is_editable_by, get_vault_master_store, sha256_file

router = APIRouter(prefix="/api/vault-master/assets/{asset_id}/section-move", tags=["vault-master"])


def runtime_enabled() -> bool:
    if os.getenv("PV_ENVIRONMENT") not in {"development", "production", "test"} or not source_repository_allowed():
        return False
    # Existing Development deployments retain their behavior. Production is
    # explicitly opted in only after its worker and commissioned mounts exist.
    if os.getenv("PV_ENVIRONMENT") == "production":
        return os.getenv("PV_SECTION_MOVE_ENABLED") == "true"
    return os.getenv("PV_SECTION_MOVE_ENABLED", "true") == "true"


def validate_worker_environment() -> None:
    if os.getenv("PV_ENVIRONMENT") not in {"development", "production"} or not runtime_enabled():
        raise RuntimeError("Section move worker requires an explicitly enabled matching environment/repository")


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str
    confirm: StrictBool = False


def commissioned_slots(area: str, size: int) -> list[EligibleSlot]:
    area = "Music" if area == "Music Videos" else area
    manifest = Path(os.getenv("PV_SECTION_MOVE_MANIFEST", "/var/lib/personal-vault/metadata/storage-control/active-slots.json"))
    return commissioned_destination_slots(area, size, manifest)


def eligible_destinations(asset: CataloguedAsset) -> list[str]:
    from app.vault_libraries import DOCUMENT_LIBRARY_EXTENSIONS
    if (asset.asset_type == "Home Videos" and asset.vault_path.startswith("/vault/Home Videos/")
            and asset.owner_user_id and asset.lifecycle_state == "active" and asset.mime_type.startswith("video/")):
        try:
            commissioned_slots("Music", asset.size_bytes)
            return ["Music Videos"]
        except (OSError, ValueError, TypeError):
            return []
    if asset.asset_type != "Gallery" or not asset.vault_path.startswith("/vault/Gallery/") or not asset.owner_user_id or asset.lifecycle_state != "active":
        return []
    # Documents' actual image/document support is independent of Reading Room.
    sections = (["Documents"] if Path(asset.filename).suffix.casefold() in DOCUMENT_LIBRARY_EXTENSIONS else []) + ["Archives"]
    result = []
    for area in sections:
        try:
            commissioned_slots(area, asset.size_bytes)
        except (OSError, ValueError, TypeError):
            continue
        result.append(area)
    return result


def safe_path(root: Path, relative: str, *, exists: bool) -> Path:
    parts = validate_relative_path(relative).parts
    root = root.resolve(strict=True)
    candidate = root.joinpath(*parts)
    # Reject symlink components, including dangling destinations.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Storage path is unsafe.")
    resolved = candidate.resolve(strict=exists)
    if not resolved.is_relative_to(root) or (exists and not resolved.is_file()):
        raise ValueError("Storage path is unavailable.")
    return resolved


def source_path(snapshot: dict) -> Path:
    placement = snapshot["source_placement"]
    if placement:
        roots = configured_slot_roots()
        root = roots.get(placement["slot_id"])
        if root is None:
            raise ValueError("Source storage is unavailable.")
        return safe_path(root, placement["relative_path"], exists=True)
    logical = snapshot["source_path"]
    if logical.startswith("/vault/Home Videos/"):
        from app.home_videos import get_home_videos_path
        return safe_path(get_home_videos_path(), logical.removeprefix("/vault/Home Videos/"), exists=True)
    if not logical.startswith("/vault/Gallery/"):
        raise ValueError("Source section is invalid.")
    return safe_path(Path(os.getenv("PV_GALLERY_PATH", "/vault/Gallery")), logical.removeprefix("/vault/Gallery/"), exists=True)


def verified(path: Path, snapshot: dict) -> bool:
    return path.is_file() and not path.is_symlink() and path.stat().st_size == snapshot["size"] and sha256_file(path) == snapshot["sha256"]


class SectionMoves:
    def __init__(self, store: PostgresVaultMasterStore):
        self.store = store

    def initialize(self):
        with self.store._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS vault_section_moves (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id),
                owner_user_id UUID NOT NULL, destination TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','copying','committed','completed','failed')),
                snapshot JSONB NOT NULL, failure TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS vault_section_moves_one_active ON vault_section_moves(asset_id) WHERE status IN ('queued','copying','committed')")

    def preflight(self, asset: CataloguedAsset, destination: str) -> dict:
        if destination not in eligible_destinations(asset):
            raise ValueError("This section is not an eligible destination. Hidden photos must be restored before moving.")
        if Path(asset.filename).name != asset.filename or "\\" in asset.filename:
            raise ValueError("The filename is unsafe.")
        area = "Music" if destination == "Music Videos" else destination
        slot = select_slot(commissioned_slots(area, asset.size_bytes), area, asset.size_bytes)
        subtree = "Music/Music Videos" if destination == "Music Videos" else destination
        relative = f"{subtree}/{asset.owner_user_id}/{asset.id}/{asset.filename}"
        logical = f"/vault/{relative}"
        with self.store._connect() as connection:
            files = connection.execute("SELECT id FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchall()
            if len(files) != 1:
                raise ValueError("This asset requires a multi-file migration and cannot be moved here.")
            placement = connection.execute("SELECT slot_id,relative_path FROM vault_file_storage_placements WHERE file_id=%s", (files[0]["id"],)).fetchone()
            if (dict(placement) if placement else None) != asset.metadata.get("storage_placement"):
                raise ValueError("Source placement needs reconciliation before moving.")
            if connection.execute("SELECT 1 FROM vault_files WHERE vault_path=%s", (logical,)).fetchone():
                raise ValueError("A file already occupies that destination.")
        snapshot = {"source_type": asset.asset_type, "file_id": str(files[0]["id"]), "source_path": asset.vault_path,
                    "source_placement": dict(placement) if placement else None,
                    "sha256": asset.sha256, "size": asset.size_bytes,
                    "slot_id": slot.slot_id, "relative_path": relative, "destination_path": logical}
        if safe_path(slot.managed_root, relative, exists=False).exists():
            raise ValueError("A file already occupies that destination.")
        source = source_path(snapshot)
        if not verified(source, snapshot):
            raise ValueError("Source checksum verification failed.")
        snapshot["source_inode"] = source.stat().st_ino
        snapshot["source_device"] = source.stat().st_dev
        return snapshot

    def enqueue(self, asset: CataloguedAsset, destination: str) -> dict:
        snapshot = self.preflight(asset, destination)
        with self.store._connect() as connection:
            connection.execute("SELECT id FROM vault_assets WHERE id=%s FOR UPDATE", (asset.id,))
            active = connection.execute("SELECT id,status FROM vault_section_moves WHERE asset_id=%s AND status IN ('queued','copying','committed')", (asset.id,)).fetchone()
            if active:
                return {"operation_id": str(active["id"]), "status": active["status"]}
            operation_id = uuid4()
            connection.execute("INSERT INTO vault_section_moves(id,asset_id,owner_user_id,destination,status,snapshot) VALUES (%s,%s,%s,%s,'queued',%s)",
                               (operation_id, asset.id, asset.owner_user_id, destination, Jsonb(snapshot)))
            connection.execute("INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES (%s,%s,'section_move_requested',%s,%s,%s)",
                               (uuid4(), asset.id, str(asset.owner_user_id), Jsonb({"vault_path": asset.vault_path}), Jsonb({"destination": destination, "operation_id": str(operation_id)})))
        return {"operation_id": str(operation_id), "status": "queued"}

    def status(self, asset_id: UUID, operation_id: UUID, owner: UUID) -> dict:
        with self.store._connect() as connection:
            row = connection.execute("SELECT status FROM vault_section_moves WHERE id=%s AND asset_id=%s AND owner_user_id=%s", (operation_id, asset_id, owner)).fetchone()
        if row is None:
            raise HTTPException(404)
        return {"operation_id": str(operation_id), "status": row["status"], "reason": "Move needs administrator recovery; its evidence and files have been retained." if row["status"] == "failed" else None}

    def process_next(self) -> bool:
        # Session advisory lock survives phase commits, preventing two workers
        # from copying or cleaning the same operation after a restart.
        with self.store._connect() as connection:
            if not connection.execute("SELECT pg_try_advisory_lock(70119001) AS locked").fetchone()["locked"]:
                return False
            try:
                row = connection.execute("SELECT * FROM vault_section_moves WHERE status IN ('queued','copying','committed') ORDER BY created_at LIMIT 1").fetchone()
                if row is None:
                    return False
                connection.commit()
                try:
                    self._process(connection, row)
                except (OSError, ValueError, psycopg.Error) as error:
                    connection.rollback()
                    # Committed authority is never reversed if retirement fails.
                    connection.execute("UPDATE vault_section_moves SET status='failed',failure=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (str(error), row["id"]))
                    connection.commit()
                return True
            finally:
                connection.execute("SELECT pg_advisory_unlock(70119001)")

    def _process(self, connection, row):
        snapshot = row["snapshot"]
        destination = row["destination"]
        slots = commissioned_slots(destination, 0 if row["status"] == "committed" else snapshot["size"])
        slot = next((slot for slot in slots if slot.slot_id == snapshot["slot_id"]), None)
        if slot is None:
            raise ValueError("Reserved commissioned slot is no longer eligible.")
        target = safe_path(slot.managed_root, snapshot["relative_path"], exists=False)
        temporary = target.with_name(f".section-move-{row['id']}.part")
        if row["status"] != "committed":
            # Lock latest authoritative state, preserving concurrent metadata,
            # sharing, People and tag updates rather than replaying old metadata.
            asset_row = connection.execute("SELECT * FROM vault_assets WHERE id=%s FOR UPDATE", (row["asset_id"],)).fetchone()
            file_row = connection.execute("SELECT * FROM vault_files WHERE id=%s FOR UPDATE", (UUID(snapshot["file_id"]),)).fetchone()
            placement = connection.execute("SELECT slot_id,relative_path FROM vault_file_storage_placements WHERE file_id=%s FOR UPDATE", (UUID(snapshot["file_id"]),)).fetchone()
            if not asset_row or not file_row or asset_row["owner_user_id"] != row["owner_user_id"] or asset_row["asset_type"] != snapshot.get("source_type", "Gallery") or file_row["vault_path"] != snapshot["source_path"] or file_row["sha256"] != snapshot["sha256"] or file_row["size_bytes"] != snapshot["size"] or (dict(placement) if placement else None) != snapshot["source_placement"]:
                raise ValueError("The authoritative asset changed before moving.")
            if asset_row["metadata"].get("storage_placement") != snapshot["source_placement"]:
                raise ValueError("Source metadata placement is inconsistent.")
            if asset_row.get("lifecycle_state", "active") != "active":
                raise ValueError("The asset is no longer active.")
            if (snapshot.get("source_type", "Gallery"), destination) not in {
                    ("Gallery", "Documents"), ("Gallery", "Archives"), ("Home Videos", "Music Videos")}:
                raise ValueError("Unsupported content transition.")
            source = source_path(snapshot)
            if not verified(source, snapshot):
                raise ValueError("Source checksum verification failed.")
            # Device ids differ across containers; record identity at execution.
            if row["status"] == "queued":
                if target.exists() or temporary.exists() or temporary.is_symlink():
                    raise ValueError("Destination collision; no file was overwritten.")
                snapshot.update(source_inode=source.stat().st_ino, source_device=source.stat().st_dev)
                connection.execute("UPDATE vault_section_moves SET status='copying',snapshot=%s WHERE id=%s", (Jsonb(snapshot), row["id"]))
                connection.commit()
                # Reacquire authoritative locks after the durable reservation.
                return self._process(connection, {**row, "status": "copying", "snapshot": snapshot})
            target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            safe_path(slot.managed_root, snapshot["relative_path"], exists=False)
            if not temporary.exists():
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                    outgoing.flush(); os.fsync(outgoing.fileno())
                shutil.copystat(source, temporary)
            if not verified(temporary, snapshot) or not verified(source, snapshot):
                raise ValueError("Migration copy verification failed; source retained.")
            if target.exists():
                if not target.samefile(temporary):
                    raise ValueError("Destination collision; no file was overwritten.")
            else:
                os.link(temporary, target)
            # Directory durability before catalogue cutover (Linux worker).
            if os.name != "nt":
                descriptor = os.open(target.parent, os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
            placement = {"slot_id": snapshot["slot_id"], "relative_path": snapshot["relative_path"]}
            connection.execute("INSERT INTO vault_storage_slots(slot_id,state,assigned_areas) VALUES (%s,'active',%s) ON CONFLICT (slot_id) DO NOTHING", (snapshot["slot_id"], Jsonb(["Music" if destination == "Music Videos" else destination])))
            slot_row = connection.execute("SELECT state FROM vault_storage_slots WHERE slot_id=%s FOR UPDATE", (snapshot["slot_id"],)).fetchone()
            if slot_row["state"] != "active":
                raise ValueError("Commissioned slot is not active in the catalogue.")
            connection.execute("UPDATE vault_files SET vault_path=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (snapshot["destination_path"], UUID(snapshot["file_id"])))
            connection.execute("INSERT INTO vault_file_storage_placements(file_id,slot_id,relative_path,assigned_by,placement_reason) VALUES (%s,%s,%s,%s,'explicit_section_move') ON CONFLICT (file_id) DO UPDATE SET slot_id=EXCLUDED.slot_id,relative_path=EXCLUDED.relative_path,assigned_at=CURRENT_TIMESTAMP,assigned_by=EXCLUDED.assigned_by,placement_reason=EXCLUDED.placement_reason", (UUID(snapshot["file_id"]), snapshot["slot_id"], snapshot["relative_path"], str(row["owner_user_id"])))
            connection.execute("UPDATE vault_assets SET asset_type=%s,metadata=metadata || %s,effective_metadata=effective_metadata || %s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (destination, Jsonb({"storage_placement": placement}), Jsonb({"storage_placement": placement}), row["asset_id"]))
            connection.execute("INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES (%s,%s,'asset_section_moved',%s,%s,%s)", (uuid4(), row["asset_id"], str(row["owner_user_id"]), Jsonb({"vault_path": snapshot["source_path"], "storage_placement": snapshot["source_placement"], "asset_type": snapshot.get("source_type", "Gallery")}), Jsonb({"vault_path": snapshot["destination_path"], "storage_placement": placement, "asset_type": destination, "sha256": snapshot["sha256"], "operation_id": str(row["id"])})))
            connection.execute("UPDATE vault_section_moves SET status='committed',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (row["id"],))
            connection.commit()
        if not verified(target, snapshot):
            raise ValueError("Committed destination verification failed; old copy retained.")
        try:
            source = source_path(snapshot)
        except FileNotFoundError:
            source = None  # A previous cleanup completed before its final commit.
        if source is not None:
            if not verified(source, snapshot) or source.stat().st_ino != snapshot["source_inode"] or source.stat().st_dev != snapshot["source_device"]:
                raise ValueError("Source changed before retirement; both copies retained.")
            source.unlink()
        if temporary.exists():
            if not temporary.samefile(target):
                raise ValueError("Migration temporary file changed; retained for recovery.")
            temporary.unlink()
        updated = self.store.get_catalogued_asset_by_id(row["asset_id"])
        if updated:
            self.store._export_sidecar(updated)
        connection.execute("UPDATE vault_section_moves SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (row["id"],))
        connection.commit()


def owner_asset(asset_id, username, store):
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username) or asset.owner_user_id is None:
        raise HTTPException(404)
    return asset


def moves(store):
    if not isinstance(store, PostgresVaultMasterStore):
        raise HTTPException(503, "Durable section moves are unavailable.")
    return SectionMoves(store)


@router.get("/destinations")
def destinations(asset_id: UUID, username: AuthenticatedUsername, store=Depends(get_vault_master_store)):
    asset = owner_asset(asset_id, username, store)
    return {"destinations": eligible_destinations(asset) if runtime_enabled() else []}


@router.post("/preflight")
def preflight(asset_id: UUID, request: MoveRequest, username: AuthenticatedUsername, store=Depends(get_vault_master_store)):
    asset = owner_asset(asset_id, username, store)
    if not runtime_enabled():
        return {"ready": False, "reason": "Section moves are not enabled for this Vault."}
    try:
        moves(store).preflight(asset, request.destination)
    except (OSError, ValueError):
        return {"ready": False, "reason": "This move is unavailable. Check destination availability and the asset's storage state."}
    return {"ready": True}


@router.post("", status_code=202)
def confirm(asset_id: UUID, request: MoveRequest, username: AuthenticatedUsername, store=Depends(get_vault_master_store)):
    asset = owner_asset(asset_id, username, store)
    if request.confirm is not True:
        raise HTTPException(422, "Explicit confirmation is required.")
    if not runtime_enabled():
        raise HTTPException(503, "Section moves are not enabled for this Vault.")
    try:
        return moves(store).enqueue(asset, request.destination)
    except (OSError, ValueError):
        raise HTTPException(409, "The move is no longer valid; no existing file was overwritten.") from None


@router.get("/{operation_id}")
def operation(asset_id: UUID, operation_id: UUID, username: AuthenticatedUsername, store=Depends(get_vault_master_store)):
    asset = owner_asset(asset_id, username, store)
    return moves(store).status(asset_id, operation_id, asset.owner_user_id)


def main():
    import logging
    logging.basicConfig(level=logging.INFO)
    validate_worker_environment()
    from app.config import get_database_conninfo, get_metadata_storage_root
    store = PostgresVaultMasterStore(get_database_conninfo(), sidecar_root=get_metadata_storage_root())
    worker = SectionMoves(store)
    while True:
        try:
            worker.process_next()
            Path("/tmp/section-move-heartbeat").touch()
        except Exception:
            logging.exception("Section move worker failed; durable operation retained")
        time.sleep(2)


if __name__ == "__main__":
    main()
