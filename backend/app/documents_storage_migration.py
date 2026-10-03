"""Explicit manifest-bound Documents migration; never imported by startup.

Copy and catalogue cutover are separate maintenance phases. Original bytes are
retained for rollback; this tool has no source-deletion command. Host mapping
changes require the reviewed registration documents and a separate admin step.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from uuid import UUID, uuid4

from app.storage_placement import validate_relative_path, MANIFEST_SCHEMA, configured_slot_roots

VERSION = "documents-slot-adoption-v1"
ACTION = "documents_storage_migrated"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(manifest):
    if manifest.get("version") != VERSION or manifest.get("environment") not in ("production", "development"):
        raise ValueError("Explicit version/environment required")
    UUID(manifest["vault_id"])
    pattern = r"PV-DISK-[0-9]{3,}" if manifest["environment"] == "production" else r"PV-DEV-DISK-[0-9]{3,}"
    if not re.fullmatch(pattern, manifest["slot_id"]):
        raise ValueError("Slot environment mismatch")
    files = manifest["files"]
    if not files or len({r["file_id"] for r in files}) != len(files) or len({r["asset_id"] for r in files}) != len(files):
        raise ValueError("Nonempty unique canonical manifest required")
    paths = set()
    for row in files:
        for key in ("file_id", "asset_id", "owner_user_id"):
            UUID(row[key])
        if not row["vault_path"].startswith("/vault/Documents/"):
            raise ValueError("Only Documents are in scope")
        relative = row["vault_path"].removeprefix("/vault/Documents/")
        if str(validate_relative_path(relative)) != relative or relative in paths:
            raise ValueError("Ambiguous Documents path")
        paths.add(relative)
        if Path(relative).name != row["filename"] or row["size_bytes"] < 0 or not re.fullmatch("[0-9a-f]{64}", row["sha256"]):
            raise ValueError("Invalid canonical file evidence")


def safe_file(root, relative):
    root = Path(root).resolve(strict=True)
    candidate = root.joinpath(*validate_relative_path(relative).parts)
    if any(p.is_symlink() for p in [candidate, *candidate.parents] if p != root and p.is_relative_to(root)):
        raise ValueError("Symlink inside migration path")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Migration path escapes root")
    return candidate


def verified(path, row):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOATIME", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        hashed = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size != row["size_bytes"] or hashed != row["sha256"] or
            (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size)):
        raise ValueError("File integrity changed")


def inspect_files(manifest, source_root, target_root, *, require_copies=False):
    validate(manifest)
    stamp = Path(target_root) / (".documents-adoption-" + digest(manifest) + ".json")
    ours = stamp.is_file() and not stamp.is_symlink() and json.loads(stamp.read_text()) == {"manifest_sha256": digest(manifest)}
    if (Path(target_root) / "Documents").exists() and not ours:
        raise ValueError("Existing destination directory requires separate review")
    for row in manifest["files"]:
        relative = row["vault_path"].removeprefix("/vault/Documents/")
        verified(safe_file(source_root, relative), row)
        target = safe_file(target_root, "Documents/" + relative)
        if target.exists():
            if not ours:
                raise ValueError("Target collision without migration ownership")
            verified(target, row)
        elif require_copies:
            raise ValueError("Verified migration copy is missing")
    if shutil.disk_usage(target_root).free < sum(r["size_bytes"] for r in manifest["files"]):
        raise ValueError("Insufficient commissioned capacity")


def copy_files(manifest, source_root, target_root):
    inspect_files(manifest, source_root, target_root)
    stamp = Path(target_root) / (".documents-adoption-" + digest(manifest) + ".json")
    if not stamp.exists():
        with stamp.open("x") as stream:
            json.dump({"manifest_sha256": digest(manifest)}, stream)
            stream.flush(); os.fsync(stream.fileno())
    for row in manifest["files"]:
        relative = row["vault_path"].removeprefix("/vault/Documents/")
        source = safe_file(source_root, relative)
        target = safe_file(target_root, "Documents/" + relative)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(".adoption-" + row["file_id"] + ".part")
        if partial.is_symlink():
            raise ValueError("Unsafe partial copy")
        # Only our manifest-owned incomplete temporary copy is discarded on retry.
        if partial.exists():
            partial.unlink()
        with source.open("rb") as incoming, partial.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush(); os.fsync(outgoing.fileno())
        shutil.copystat(source, partial)
        verified(partial, row); verified(source, row)
        os.link(partial, target)  # Never overwrite a canonical or unrelated file.
        partial.unlink()
        if os.name == "posix":
            fd = os.open(target.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    inspect_files(manifest, source_root, target_root, require_copies=True)


def registration_documents(config, authority, manifest):
    """Prepare changes to the existing registry, not a second registry or apply."""
    validate(manifest)
    if authority.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("Managed authority required")
    config, authority = deepcopy(config), deepcopy(authority)
    slots = [d for d in config["disks"] if d["id"] == manifest["slot_id"]]
    slot = authority["slots"].get(manifest["slot_id"])
    if len(slots) != 1 or not slot or slot.get("state") != "active" or slot.get("integration_mode") != "slot_managed":
        raise ValueError("An existing active commissioned slot is required")
    configured = slots[0]
    if (configured.get("state") != "Active" or configured.get("integration_mode") != "slot_managed"
            or configured.get("uuid") != manifest["filesystem_uuid"] or slot.get("filesystem_uuid") != manifest["filesystem_uuid"]
            or configured.get("hardware_id") != slot.get("hardware_id")):
        raise ValueError("Configured hardware/manifest authority mismatch")
    for entry in authority["slots"].values():
        mapping = entry.get("logical_mappings", {})
        if "Documents" in mapping and entry is not slot:
            raise ValueError("Documents already has another authority; review required")
    for mapping in (configured["areas"], slot["logical_mappings"]):
        if mapping.get("Documents", "/vault/Documents") != "/vault/Documents":
            raise ValueError("Conflicting Documents logical mapping")
        mapping["Documents"] = "/vault/Documents"
    slot["areas"] = sorted(set(slot["areas"]) | {"Documents"})
    return config, authority


def catalogue(conninfo, manifest, source_root, target_root, *, phase="preflight"):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    validate(manifest)
    if phase not in ("preflight", "place", "rollback"):
        raise ValueError("Unknown catalogue phase")
    mutate = phase != "preflight"
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        if mutate:
            conn.execute("SET LOCAL lock_timeout='10s'")
            conn.execute("SET LOCAL statement_timeout='60s'")
            conn.execute("LOCK TABLE vault_assets,vault_files,vault_file_storage_placements,vault_storage_slots,vault_asset_history IN SHARE ROW EXCLUSIVE MODE")
        else:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        local = conn.execute("SELECT vault_id FROM vaults WHERE is_local").fetchall()
        if len(local) != 1 or str(local[0]["vault_id"]) != manifest["vault_id"]:
            raise ValueError("Wrong sovereign Vault")
        slot = conn.execute("SELECT * FROM vault_storage_slots WHERE slot_id=%s", (manifest["slot_id"],)).fetchone()
        if not slot or slot["state"] != "active" or slot["hardware"].get("filesystem_uuid") != manifest["filesystem_uuid"]:
            raise ValueError("Commissioned catalogue slot changed")
        rows = conn.execute("""SELECT a.id asset_id,f.id file_id,a.owner_user_id,a.origin_vault_id,
            a.asset_type,a.lifecycle_state,f.vault_path,f.filename,f.size_bytes,f.sha256,
            a.metadata->'storage_placement' metadata_placement,p.slot_id,p.relative_path,
            md5(jsonb_build_array(a.metadata-'storage_placement',a.detected_metadata,a.imported_metadata,
                a.user_overrides,a.effective_metadata,a.metadata_provenance,a.visibility,a.shared_with)::text) state_fingerprint
            FROM vault_files f JOIN vault_assets a ON a.id=f.asset_id
            LEFT JOIN vault_file_storage_placements p ON p.file_id=f.id
            WHERE f.vault_path LIKE '/vault/Documents/%%' AND f.file_role='primary'""").fetchall()
        expected = {r["file_id"]: r for r in manifest["files"]}
        if {str(r["file_id"]) for r in rows} != set(expected):
            raise ValueError("Documents file set changed; no partial adoption")
        states = []
        for row in rows:
            wanted = expected[str(row["file_id"])]
            if any(str(row[k]) != str(wanted[k]) for k in ("asset_id", "file_id", "owner_user_id", "vault_path", "filename", "size_bytes", "sha256", "state_fingerprint")):
                raise ValueError("Canonical identity or metadata changed")
            if row["asset_type"] != "Documents" or row["lifecycle_state"] != "active" or str(row["origin_vault_id"]) != manifest["vault_id"]:
                raise ValueError("Foreign or inactive canonical asset")
            placement = {"slot_id": manifest["slot_id"], "relative_path": row["vault_path"].removeprefix("/vault/")}
            migrated = row["slot_id"] == placement["slot_id"] and row["relative_path"] == placement["relative_path"] and row["metadata_placement"] == placement
            if not migrated and (row["slot_id"] is not None or row["metadata_placement"] is not None):
                raise ValueError("Conflicting storage placement")
            if migrated and not conn.execute("SELECT 1 FROM vault_asset_history WHERE asset_id=%s AND action=%s AND current_values->>'manifest_sha256'=%s", (row["asset_id"], ACTION, digest(manifest))).fetchone():
                raise ValueError("Placement lacks this migration's audit authority")
            states.append(migrated)
        if any(states) and not all(states):
            raise ValueError("Unexpected partial catalogue cutover")
        expected_areas = set(manifest["previous_assigned_areas"]) | ({"Documents"} if all(states) else set())
        if set(slot["assigned_areas"]) != expected_areas:
            raise ValueError("Slot area assignments changed; review required")
        inspect_files(manifest, source_root, target_root, require_copies=phase == "place" or any(states))
        if phase == "place" and not all(states):
            for row in rows:
                placement = {"slot_id": manifest["slot_id"], "relative_path": row["vault_path"].removeprefix("/vault/")}
                conn.execute("INSERT INTO vault_file_storage_placements(file_id,slot_id,relative_path,assigned_by,placement_reason) VALUES(%s,%s,%s,%s,%s)",
                             (row["file_id"], placement["slot_id"], placement["relative_path"], "documents-migration", VERSION))
                conn.execute("UPDATE vault_assets SET metadata=metadata || %s WHERE id=%s", (Jsonb({"storage_placement": placement}), row["asset_id"]))
                conn.execute("INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES(%s,%s,%s,%s,%s,%s)",
                             (uuid4(), row["asset_id"], ACTION, "documents-migration", Jsonb({"storage_placement": None}), Jsonb({"manifest_sha256": digest(manifest), "storage_placement": placement})))
            conn.execute("UPDATE vault_storage_slots SET assigned_areas=%s WHERE slot_id=%s", (Jsonb(sorted(set(slot["assigned_areas"]) | {"Documents"})), manifest["slot_id"]))
        if phase == "rollback" and all(states):
            # Only this exact file set can be rolled back. New files already fail
            # the complete-set guard; user metadata changes fail the fingerprint.
            for row in rows:
                conn.execute("DELETE FROM vault_file_storage_placements WHERE file_id=%s", (row["file_id"],))
                conn.execute("UPDATE vault_assets SET metadata=metadata-'storage_placement' WHERE id=%s", (row["asset_id"],))
                conn.execute("INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES(%s,%s,'documents_storage_rollback',%s,%s,%s)",
                             (uuid4(), row["asset_id"], "documents-migration", Jsonb({"manifest_sha256": digest(manifest)}), Jsonb({"storage_placement": None})))
            conn.execute("UPDATE vault_storage_slots SET assigned_areas=%s WHERE slot_id=%s", (Jsonb(manifest["previous_assigned_areas"]), manifest["slot_id"]))
        return {"environment": manifest["environment"], "files": len(rows), "phase": phase, "already_placed": all(states), "manifest_sha256": digest(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source-root", required=True, help="Read-only legacy Documents root or retained recovery bind")
    parser.add_argument("--expect-sha", required=True)
    parser.add_argument("--phase", choices=("preflight", "copy", "place", "rollback"), default="preflight")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
    if args.phase != "preflight" and not args.maintenance_confirmed:
        raise ValueError("Explicit maintenance confirmation required")
    # Same explicit environment/database/build contract as other audited CLI work.
    from app.music_legacy_reconcile import connection_from_environment
    conninfo = connection_from_environment(manifest, manifest["environment"], args.expect_sha)
    authority = json.loads(Path(os.environ["PV_STORAGE_SLOT_MANIFEST_FILE"]).read_text())
    slot = authority["slots"].get(manifest["slot_id"], {})
    if slot.get("state") != "active" or slot.get("integration_mode") != "slot_managed" or slot.get("filesystem_uuid") != manifest["filesystem_uuid"]:
        raise ValueError("Host storage authority changed")
    root = configured_slot_roots().get(manifest["slot_id"])
    if root is None or not root.is_dir() or root.is_symlink() or not root.is_mount():
        raise ValueError("Commissioned migration mount unavailable")
    if root.stat().st_dev != manifest["target_device"] or Path(args.source_root).stat().st_dev != manifest["source_device"]:
        raise ValueError("Audited source/target filesystem changed")
    if args.phase == "copy":
        catalogue(conninfo, manifest, args.source_root, root)
        copy_files(manifest, args.source_root, root)
        result = {"phase": "copy", "files": len(manifest["files"]), "sources_retained": True}
    else:
        result = catalogue(conninfo, manifest, args.source_root, root, phase=args.phase)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
