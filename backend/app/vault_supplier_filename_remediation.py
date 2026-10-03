"""Evidence-only planning plus recoverable execution for Supplier filename repair.

The executor accepts only an automatic planner result, persists its snapshot
before touching a slot, and uses the catalogue placement as physical authority.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.supplier_filename_remediation_storage import (
    key as managed_storage_key,
    queue_request,
    request_from_snapshot,
    receipt_root,
    queue_root,
    verify_request,
    verify_receipt,
)

VERSION = "pv-vs-filename-remediation-v1"
POLLUTED = re.compile(r" \(Vault Supplier (?:[0-9a-f]{8}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\)(?=\.[^.]+$|$)", re.I)
GENERIC_THEATRE_TRACK = re.compile(r"^(?:[a-z0-9]+)_t\d+(?:_[a-z0-9]+)*\.[^.]+$", re.I)


class RemediationRefused(RuntimeError):
    """The action is stale, unsafe, or needs human recovery/review."""


@dataclass(frozen=True)
class Candidate:
    record_id: str; owner_user_id: str; transfer_id: str | None; current_filename: str
    original_filename: str | None; checksum_matches: bool; size_matches: bool; owner_matches: bool
    current_title: str | None = None; title_provenance: str | None = None; has_user_override: bool = False
    target_exists: bool = False; theatre: bool = False; duration_seconds: float | None = None
    planned_at: datetime | None = None
    current_vault_path: str | None = None
    current_relative_path: str | None = None


@dataclass(frozen=True)
class PlannedChange:
    candidate: Candidate
    classification: Literal["arrival_hall_only", "published_recoverable", "filename_title_recoverable", "manual_metadata_protected", "possible_theatre_extra", "collision", "ambiguous_provenance"]
    automatic: bool; proposed_filename: str | None; proposed_title: str | None; reason: str


def plan(candidate: Candidate) -> PlannedChange:
    if candidate.planned_at is None:
        candidate = replace(candidate, planned_at=datetime.now(UTC))
    if not candidate.transfer_id or not candidate.original_filename or not POLLUTED.search(candidate.current_filename):
        return PlannedChange(candidate, "ambiguous_provenance", False, None, None, "No exact Supplier provenance and polluted filename evidence agree.")
    if not (candidate.checksum_matches and candidate.size_matches and candidate.owner_matches):
        return PlannedChange(candidate, "ambiguous_provenance", False, None, None, "Supplier provenance does not match owner, checksum, and size evidence.")
    if candidate.target_exists: return PlannedChange(candidate, "collision", False, None, None, "The recovered target filename already exists.")
    if candidate.has_user_override: return PlannedChange(candidate, "manual_metadata_protected", False, candidate.original_filename, None, "User metadata is authoritative and will not be overwritten.")
    if candidate.theatre and (GENERIC_THEATRE_TRACK.fullmatch(candidate.original_filename) or (candidate.duration_seconds is not None and candidate.duration_seconds < 600)):
        return PlannedChange(candidate, "possible_theatre_extra", False, candidate.original_filename, None, "Short Theatre media requires review; its role is not inferred.")
    fallback, title = Path(candidate.current_filename).stem.replace("_", " "), Path(candidate.original_filename).stem.replace("_", " ")
    if candidate.current_title == fallback and candidate.title_provenance == "filename":
        return PlannedChange(candidate, "filename_title_recoverable", True, candidate.original_filename, title, "Exact Supplier provenance supports filename and filename-derived title recovery.")
    return PlannedChange(candidate, "published_recoverable", True, candidate.original_filename, None, "Exact Supplier provenance supports filename recovery; title is not proven fallback.")


def dry_run(candidates: list[Candidate]) -> dict[str, object]:
    changes = [plan(candidate) for candidate in candidates]
    return {"version": VERSION, "dry_run": True, "files_changed": 0, "automatic": sum(c.automatic for c in changes), "review_required": sum(not c.automatic for c in changes), "changes": changes}


def apply(candidates: list[Candidate], mutate: Callable[[PlannedChange], None]) -> list[PlannedChange]:
    applied = []
    for change in map(plan, candidates):
        if change.automatic: mutate(change); applied.append(change)
    return applied


class PostgresSupplierFilenameRemediationExecutor:
    """Catalogue-side executor; physical mutation is root-worker-only."""
    def __init__(self, conninfo: str): self.conninfo = conninfo

    def initialize(self) -> None:
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_filename_remediation_actions (
                id UUID PRIMARY KEY, version TEXT NOT NULL, file_id UUID NOT NULL UNIQUE REFERENCES vault_files(id), asset_id UUID NOT NULL REFERENCES vault_assets(id), transfer_id UUID NOT NULL, owner_user_id UUID NOT NULL, status TEXT NOT NULL CHECK(status IN ('applying','file_moved','completed','failed','recovery_required')), snapshot JSONB NOT NULL, failure TEXT, started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMPTZ)""")

    def apply(self, change: PlannedChange) -> dict[str, object]:
        if not change.automatic or change.classification not in {"published_recoverable", "filename_title_recoverable"}: raise RemediationRefused("only an automatic published plan may execute")
        action = self._prepare(change)
        if action["status"] == "completed": return {"status": "already_completed", "action_id": str(action["id"])}
        snapshot = dict(action["snapshot"])
        if action["status"] == "applying":
            try:
                queue_request(request_from_snapshot(action["id"], snapshot))
            except (OSError, ValueError) as error:
                self._mark_recovery(action["id"], f"managed storage request could not be persisted: {error}")
                raise RemediationRefused("managed storage request could not be persisted") from error
            return {"status": "requested", "action_id": str(action["id"])}
        elif action["status"] == "file_moved":
            self._commit_metadata(action["id"])
            return {"status": "completed", "action_id": str(action["id"])}
        else: raise RemediationRefused(f"action requires manual recovery ({action['status']})")

    def reconcile_next_receipt(self) -> UUID | None:
        """Advance exactly one action after a signed root-worker receipt."""
        root = receipt_root()
        if root.is_symlink() or not root.is_dir():
            return None
        try:
            signing_key = managed_storage_key()
        except OSError:
            return None
        for path in sorted(root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                import json
                receipt = verify_receipt(json.loads(path.read_text(encoding="utf-8")), signing_key)
                if receipt is None:
                    continue
                action_id = UUID(str(receipt["action_id"]))
                if self._accept_receipt(action_id, receipt):
                    return action_id
            except (OSError, ValueError, TypeError, json.JSONDecodeError, RemediationRefused):
                continue
        return None

    def reconcile_next_rejected_request(self) -> UUID | None:
        """A root-worker rejection is evidence that retrying the action is unsafe."""
        root = queue_root()
        if root.is_symlink() or not root.is_dir(): return None
        try: signing_key = managed_storage_key()
        except OSError: return None
        for path in sorted(root.glob("*.rejected.request")):
            if path.is_symlink() or not path.is_file(): continue
            try:
                import json
                request = verify_request(json.loads(path.read_text(encoding="utf-8")), signing_key)
                if request is None: continue
                action_id = UUID(str(request["action_id"]))
                with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
                    cursor.execute("UPDATE vault_supplier_filename_remediation_actions SET status='recovery_required',failure='managed storage worker rejected the signed request; inspect durable request/receipt and old/new slot paths' WHERE id=%s AND status='applying'", (action_id,))
                    if cursor.rowcount: return action_id
            except (OSError, ValueError, TypeError, json.JSONDecodeError): continue
        return None

    def _accept_receipt(self, action_id: UUID, receipt: dict[str, object]) -> bool:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_supplier_filename_remediation_actions WHERE id=%s FOR UPDATE", (action_id,))
            action = cursor.fetchone()
            if not action:
                return False
            if action["status"] == "completed":
                return False
            if action["status"] != "applying":
                raise RemediationRefused("receipt conflicts with persisted remediation state")
            snapshot = dict(action["snapshot"])
            expected = {"slot_id": snapshot["slot_id"], "old_relative_path": snapshot["old_relative_path"], "new_relative_path": snapshot["new_relative_path"], "expected_sha256": snapshot["sha256"], "expected_size_bytes": snapshot["size_bytes"], "post_sha256": snapshot["sha256"], "post_size_bytes": snapshot["size_bytes"]}
            if any(str(receipt[key]) != str(value) for key, value in expected.items()):
                raise RemediationRefused("managed storage receipt does not match action evidence")
            cursor.execute("UPDATE vault_supplier_filename_remediation_actions SET status='file_moved' WHERE id=%s AND status='applying'", (action_id,))
        self._commit_metadata(action_id)
        return True

    def _prepare(self, change: PlannedChange) -> dict[str, object]:
        try: file_id = UUID(change.candidate.record_id)
        except ValueError as error: raise RemediationRefused("candidate file id is invalid") from error
        with psycopg.connect(self.conninfo, row_factory=dict_row) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_supplier_filename_remediation_actions WHERE file_id=%s FOR UPDATE", (file_id,)); existing = cursor.fetchone()
            if existing: return dict(existing)
            cursor.execute("""SELECT f.id file_id,f.asset_id,f.filename,f.vault_path,f.size_bytes,f.sha256,p.slot_id,p.relative_path,a.owner_user_id,a.display_title,a.metadata,a.metadata_provenance,a.user_overrides,a.asset_type FROM vault_files f JOIN vault_assets a ON a.id=f.asset_id JOIN vault_file_storage_placements p ON p.file_id=f.id WHERE f.id=%s FOR UPDATE OF f,a,p""", (file_id,)); row = cursor.fetchone()
            snapshot = self._validate_current(dict(row) if row else None, change, cursor); action_id = uuid4()
            cursor.execute("INSERT INTO vault_supplier_filename_remediation_actions(id,version,file_id,asset_id,transfer_id,owner_user_id,status,snapshot) VALUES (%s,%s,%s,%s,%s,%s,'applying',%s)", (action_id, VERSION, file_id, UUID(snapshot["asset_id"]), UUID(snapshot["transfer_id"]), UUID(snapshot["owner_user_id"]), Jsonb(snapshot)))
            return {"id": action_id, "status": "applying", "snapshot": snapshot}

    def _validate_current(self, row: dict[str, object] | None, change: PlannedChange, cursor: object) -> dict[str, object]:
        c = change.candidate
        if c.planned_at is None:
            raise RemediationRefused("an executor plan must carry its dry-run timestamp")
        if not c.current_vault_path or not c.current_relative_path:
            raise RemediationRefused("an executor plan must carry its canonical and placement paths")
        if row is None or row["filename"] != c.current_filename or row["owner_user_id"] != UUID(c.owner_user_id): raise RemediationRefused("file identity changed or is absent")
        if row["vault_path"] != c.current_vault_path or row["relative_path"] != c.current_relative_path: raise RemediationRefused("canonical or physical placement changed since planning")
        metadata, provenance, overrides = dict(row["metadata"] or {}), dict(row["metadata_provenance"] or {}), dict(row["user_overrides"] or {})
        context = metadata.get("source_context"); transfer_id = context.get("transfer_id") if isinstance(context, dict) else None
        if transfer_id != c.transfer_id or not c.original_filename or change.proposed_filename != c.original_filename: raise RemediationRefused("exact transfer provenance is absent")
        if Path(c.original_filename).name != c.original_filename or any(part in c.original_filename for part in ("/", "\\", "\x00")): raise RemediationRefused("proposed filename is unsafe")
        try: transfer_uuid = UUID(transfer_id)
        except (ValueError, TypeError) as error: raise RemediationRefused("transfer provenance id is invalid") from error
        cursor.execute("SELECT user_id,filename,original_filename,total_size,expected_sha256,state FROM vault_supplier_transfer_sessions WHERE transfer_id=%s", (transfer_uuid,)); transfer = cursor.fetchone(); original = transfer and (transfer["original_filename"] or transfer["filename"])
        if not transfer or transfer["state"] != "finalized" or transfer["user_id"] != row["owner_user_id"] or original != c.original_filename or transfer["total_size"] != row["size_bytes"] or transfer["expected_sha256"] != row["sha256"]: raise RemediationRefused("transfer evidence does not agree")
        if row["asset_type"] in {"Movie", "Movies"} and GENERIC_THEATRE_TRACK.fullmatch(c.original_filename): raise RemediationRefused("generic Theatre track always requires review")
        if overrides: raise RemediationRefused("manual metadata is protected")
        if change.proposed_title and (row["display_title"] != c.current_title or provenance.get("display_title") != "filename"): raise RemediationRefused("fallback title changed since planning")
        cursor.execute("SELECT 1 FROM vault_asset_history WHERE asset_id=%s AND created_at > %s AND username <> 'Supplier filename remediation' LIMIT 1", (row["asset_id"], c.planned_at))
        if cursor.fetchone(): raise RemediationRefused("asset history changed since planning")
        target_relative, target_path = str(PurePosixPath(str(row["relative_path"])).with_name(c.original_filename)), str(PurePosixPath(str(row["vault_path"])).with_name(c.original_filename))
        cursor.execute("SELECT 1 FROM vault_files WHERE vault_path=%s", (target_path,))
        if cursor.fetchone(): raise RemediationRefused("target logical path already exists")
        return {"file_id":str(row["file_id"]),"asset_id":str(row["asset_id"]),"transfer_id":transfer_id,"owner_user_id":str(row["owner_user_id"]),"old_filename":c.current_filename,"new_filename":c.original_filename,"old_path":str(row["vault_path"]),"new_path":target_path,"old_relative_path":str(row["relative_path"]),"new_relative_path":target_relative,"slot_id":str(row["slot_id"]),"sha256":str(row["sha256"]),"size_bytes":int(row["size_bytes"]),"old_title":c.current_title,"new_title":change.proposed_title,"classification":change.classification,"planned_at":c.planned_at.astimezone(UTC).isoformat()}

    def _mark_recovery(self, action_id: UUID, failure: str) -> None:
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_supplier_filename_remediation_actions SET status='recovery_required',failure=%s WHERE id=%s AND status IN ('applying','file_moved')", (failure, action_id))

    def _commit_metadata(self, action_id: UUID) -> None:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_supplier_filename_remediation_actions WHERE id=%s FOR UPDATE", (action_id,)); action = cursor.fetchone()
            if not action or action["status"] == "completed": return
            if action["status"] != "file_moved": raise RemediationRefused("action is not ready for catalogue reconciliation")
            s = dict(action["snapshot"])
            cursor.execute("UPDATE vault_files SET vault_path=%s,filename=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND vault_path=%s AND filename=%s AND sha256=%s AND size_bytes=%s", (s["new_path"],s["new_filename"],UUID(s["file_id"]),s["old_path"],s["old_filename"],s["sha256"],s["size_bytes"]))
            if cursor.rowcount != 1: raise RemediationRefused("catalogue file changed after physical rename; recovery required")
            cursor.execute("UPDATE vault_file_storage_placements SET relative_path=%s WHERE file_id=%s AND slot_id=%s AND relative_path=%s", (s["new_relative_path"],UUID(s["file_id"]),s["slot_id"],s["old_relative_path"]))
            if cursor.rowcount != 1: raise RemediationRefused("storage placement changed after physical rename; recovery required")
            if s.get("new_title"):
                cursor.execute("UPDATE vault_assets SET display_title=%s,effective_metadata=jsonb_set(effective_metadata,'{display_title}',to_jsonb(%s::text),true),updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s AND display_title=%s AND user_overrides='{}'::jsonb AND metadata_provenance->>'display_title'='filename'", (s["new_title"],s["new_title"],UUID(s["asset_id"]),UUID(s["owner_user_id"]),s["old_title"]))
                if cursor.rowcount != 1: raise RemediationRefused("title became protected after physical rename; recovery required")
            cursor.execute("INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES (%s,%s,'supplier_filename_remediated','Supplier filename remediation',%s,%s)", (uuid4(),UUID(s["asset_id"]),Jsonb({"filename":s["old_filename"],"vault_path":s["old_path"],"title":s.get("old_title"),"sha256":s["sha256"]}),Jsonb({"filename":s["new_filename"],"vault_path":s["new_path"],"title":s.get("new_title"),"transfer_id":s["transfer_id"],"version":VERSION,"action_id":str(action_id)})))
            cursor.execute("UPDATE vault_supplier_filename_remediation_actions SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE id=%s", (action_id,))
