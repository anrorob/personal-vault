"""Bounded, durable automatic Arrival discovery; explicit scans remain full scans."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import UUID

from app.vault_master import (
    INCOMING_SOURCE, MemoryVaultMasterStore, scan_file,
    apply_source_timestamp_provenance, supplier_original_filename,
)

SCAN_VERSION = "arrival-technical-v1"
CHECKPOINT_SCHEMA = """CREATE TABLE IF NOT EXISTS vault_arrival_scan_checkpoints (
    source_path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    scan_version TEXT NOT NULL, succeeded BOOLEAN NOT NULL,
    attempts INTEGER NOT NULL, attempted_at TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL
)"""


def _load(store):
    if isinstance(store, MemoryVaultMasterStore):
        return dict(getattr(store, "arrival_scan_checkpoints", {}))
    with store._connect() as connection:
        return {row["source_path"]: dict(row) for row in connection.execute(
            "SELECT * FROM vault_arrival_scan_checkpoints"
        ).fetchall()}


def _save(store, path, fingerprint, version, succeeded, attempts, now):
    row = dict(source_path=str(path), fingerprint=fingerprint, scan_version=version,
               succeeded=succeeded, attempts=attempts, attempted_at=now,
               next_attempt_at=now + timedelta(seconds=0 if succeeded else min(3600, 30 * 2 ** min(attempts, 7))))
    if isinstance(store, MemoryVaultMasterStore):
        if not hasattr(store, "arrival_scan_checkpoints"):
            store.arrival_scan_checkpoints = {}
        store.arrival_scan_checkpoints[str(path)] = row
        return
    with store._connect() as connection:
        connection.execute("""INSERT INTO vault_arrival_scan_checkpoints
            (source_path,fingerprint,scan_version,succeeded,attempts,attempted_at,next_attempt_at)
            VALUES (%(source_path)s,%(fingerprint)s,%(scan_version)s,%(succeeded)s,%(attempts)s,%(attempted_at)s,%(next_attempt_at)s)
            ON CONFLICT(source_path) DO UPDATE SET fingerprint=EXCLUDED.fingerprint,
            scan_version=EXCLUDED.scan_version,succeeded=EXCLUDED.succeeded,
            attempts=EXCLUDED.attempts,attempted_at=EXCLUDED.attempted_at,next_attempt_at=EXCLUDED.next_attempt_at""", row)


def _fingerprint(path, owner, context):
    stat = path.stat()
    value = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, str(owner), context)
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def scan_arrival_incrementally(store, root: Path, owner_lookup=None, context_lookup=None,
                              *, limit=50, version=SCAN_VERSION, now=None):
    """Inspect at most limit changed files; poison files back off independently.

    Checkpoints are written only after record_file succeeds. A changed input or
    task version invalidates success/backoff; absence of a checkpoint is eligible.
    No new batch is created if all staged sources are current.
    """
    if not 1 <= limit <= 500:
        raise ValueError("Automatic scan limit must be between 1 and 500")
    now = now or datetime.now(timezone.utc)
    root = root.resolve(strict=True)
    checkpoints = _load(store)
    busy = {item.source_path for item in store.list_items()
            if item.state in {"move_queued", "moving", "theatre_promotion_pending"}}
    pending = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if str(path) in busy:
            continue
        if any(part.startswith(".pv-") for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            continue
        lookup_failed = False
        try:
            owner = owner_lookup(path) if owner_lookup else None
            context = context_lookup(path) if context_lookup else None
            fingerprint = _fingerprint(path, owner, context)
        except (OSError, ValueError):
            owner, context = None, None
            fingerprint = "unreadable-source-or-provenance"
            lookup_failed = True
        prior = checkpoints.get(str(path))
        same = prior and prior["fingerprint"] == fingerprint and prior["scan_version"] == version
        if same and (prior["succeeded"] or prior["next_attempt_at"] > now):
            continue
        pending.append((prior["attempted_at"] if same else datetime.min.replace(tzinfo=timezone.utc), str(path), owner, context, fingerprint, prior if same else None, lookup_failed))
    pending.sort(key=lambda row: (row[0], row[1]))
    if not pending:
        return None
    batch = store.create_batch(INCOMING_SOURCE, str(root))
    count = 0
    for _, name, owner, context, fingerprint, prior, lookup_failed in pending[:limit]:
        path = Path(name)
        try:
            if lookup_failed:
                raise ValueError("Source or provenance unavailable")
            scanned = scan_file(path, root, owner_username=owner if isinstance(owner, str) else None,
                                owner_user_id=owner if isinstance(owner, UUID) else None)
            if context is not None:
                original = supplier_original_filename(context)
                scanned = replace(scanned, metadata={**apply_source_timestamp_provenance(scanned.metadata, context),
                    "source_context": context, **({"logical_filename": original} if original else {})})
            if _fingerprint(path, owner, context) != fingerprint:
                raise ValueError("Source changed during scan")
            store.record_file(batch, INCOMING_SOURCE, scanned)
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, RuntimeError) and not str(error).startswith((
                "Arrival Hall ownership could not resolve account UUID",
                "Arrival Hall scan refused a file without immutable owner identity",
            )):
                raise
            _save(store, path, fingerprint, version, False, (prior["attempts"] if prior else 0) + 1, now)
            continue
        _save(store, path, fingerprint, version, True, 0, now)
        count += 1
    store.complete_batch(batch, count)
    return batch


def scan_arrival_from_manifest(store, root: Path, **options):
    """Snapshot atomically-written provenance once per sweep, not twice per file."""
    from app.incoming import OWNER_MANIFEST_FILENAME
    root = root.resolve(strict=True)
    manifest = root / OWNER_MANIFEST_FILENAME
    entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    if not isinstance(entries, dict):
        raise ValueError("Arrival ownership manifest is invalid")
    def entry(path):
        return entries.get(path.relative_to(root).as_posix())
    def owner(path):
        value = entry(path)
        reference = value if isinstance(value, str) else value.get("owner_user_id") if isinstance(value, dict) else None
        if reference is None:
            raise ValueError("Arrival owner reference is missing")
        if not isinstance(reference, str):
            raise ValueError("Arrival owner reference is invalid")
        try:
            return UUID(reference)
        except ValueError:
            return reference
    def context(path):
        value = entry(path)
        provenance = value.get("supplier_provenance") if isinstance(value, dict) else None
        if not isinstance(provenance, dict):
            return None
        source = provenance.get("source_context")
        transfer = provenance.get("transfer_id")
        if not isinstance(source, dict) or not isinstance(transfer, str):
            return None
        return {"transfer_id": transfer, **source}
    return scan_arrival_incrementally(store, root, owner, context, **options)
