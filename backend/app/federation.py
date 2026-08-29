"""Stage 6 Vault-to-Vault sharing records and signed protocol helpers.

Federation deliberately has its own records: a remote Vault is an authority,
not a local recipient, and an incoming remote asset is never a ``vault_asset``.
The module contains no request-time schema work; ``initialize_federation`` is
called only by the controlled catalogue bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


FEDERATION_PROTOCOL_VERSION = "pv-federation.v1"
MAX_EVENT_AGE_SECONDS = 300
EventType = Literal[
    "share_created", "share_activated", "metadata_snapshot", "metadata_updated", "share_revoked",
    "collection_snapshot", "collection_revoked", "collection_archived",
]
FEDERATED_METADATA_SCHEMA_VERSION = 1
MAX_METADATA_TEXT = 16_000
MAX_METADATA_TAGS = 64


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_envelope(envelope: dict[str, object], pairing_key: str) -> str:
    """Sign the complete public envelope with a pairing-scoped HMAC key.

    The key is established explicitly while pairing and is never included in a
    transport body, API response, or audit record.  HMAC-SHA-256 is a standard
    symmetric authenticated-message primitive and is sufficient for the small
    already-paired Stage 6 trust boundary.
    """
    return hmac.new(pairing_key.encode("utf-8"), canonical_json(envelope), hashlib.sha256).hexdigest()


def verify_envelope(envelope: dict[str, object], signature: str, pairing_key: str) -> bool:
    return hmac.compare_digest(sign_envelope(envelope, pairing_key), signature)


@dataclass(frozen=True)
class PairedVault:
    remote_vault_id: UUID
    display_label: str
    endpoint: str
    trust_state: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IncomingFederatedShare:
    incoming_share_id: UUID
    origin_vault_id: UUID
    origin_asset_id: UUID
    origin_share_id: UUID
    owner_label: str
    asset_type: str
    display_title: str
    captured_on: object | None
    state: str
    created_at: datetime
    updated_at: datetime
    metadata_revision: int | None = None
    origin_metadata: dict[str, object] | None = None
    download_allowed: bool = False


@dataclass(frozen=True)
class IncomingFederatedCollection:
    """A remote logical collection.  Its origin identity is never local ownership."""
    incoming_collection_id: UUID
    origin_vault_id: UUID
    origin_collection_id: UUID
    origin_collection_share_id: UUID
    owner_label: str
    name: str
    description: str | None
    category: str
    state: str
    lifecycle_revision: int
    membership_revision: int
    member_count: int
    updated_at: datetime


@dataclass(frozen=True)
class OutgoingFederatedCollection:
    federation_collection_share_id: UUID
    origin_collection_id: UUID
    target_vault_id: UUID
    target_label: str
    name: str
    member_count: int
    share_mode: str
    state: str
    release_at: datetime | None


@dataclass(frozen=True)
class FederatedCacheEntry:
    origin_vault_id: UUID
    origin_asset_id: UUID
    state: str
    size_bytes: int | None
    sha256: str | None
    updated_at: datetime


@dataclass(frozen=True)
class OutgoingFederatedShare:
    federation_share_id: UUID
    origin_asset_id: UUID
    target_vault_id: UUID
    target_label: str
    display_title: str
    asset_type: str
    share_mode: str
    state: str
    release_at: datetime | None
    download_allowed: bool = False


@dataclass(frozen=True)
class FederatedDownloadOperation:
    operation_id: UUID
    incoming_share_id: UUID
    recipient_user_id: UUID
    origin_vault_id: UUID
    origin_asset_id: UUID
    state: str
    idempotency_key: UUID
    local_asset_id: UUID | None
    expected_size_bytes: int
    expected_sha256: str
    staging_name: str | None
    failure_reason: str | None


def initialize_federation(cursor: psycopg.Cursor) -> None:
    """Install additive Stage 6 state in the existing bootstrap transaction."""
    cursor.execute("ALTER TABLE vaults ADD COLUMN IF NOT EXISTS display_label TEXT")
    cursor.execute("ALTER TABLE vaults ADD COLUMN IF NOT EXISTS endpoint TEXT")
    cursor.execute("ALTER TABLE vaults ADD COLUMN IF NOT EXISTS pairing_key TEXT")
    cursor.execute("ALTER TABLE vaults ADD COLUMN IF NOT EXISTS trust_state TEXT")
    cursor.execute("ALTER TABLE vaults ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_outgoing_shares (
        federation_share_id UUID PRIMARY KEY, origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
        origin_asset_id UUID NOT NULL REFERENCES vault_assets(id), owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
        target_vault_id UUID NOT NULL REFERENCES vaults(vault_id), share_mode TEXT NOT NULL CHECK (share_mode IN ('quick','standard')),
        state TEXT NOT NULL CHECK (state IN ('pending','active','revoked')), release_at TIMESTAMPTZ,
        activated_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_deliveries (
        delivery_id UUID PRIMARY KEY, federation_share_id UUID NOT NULL REFERENCES vault_federation_outgoing_shares(federation_share_id),
        event_id UUID NOT NULL UNIQUE, event_type TEXT NOT NULL, payload JSONB NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        acknowledged_at TIMESTAMPTZ, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_incoming_shares (
        incoming_share_id UUID PRIMARY KEY, origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
        origin_asset_id UUID NOT NULL, origin_share_id UUID NOT NULL, owner_label TEXT NOT NULL, asset_type TEXT NOT NULL,
        display_title TEXT NOT NULL, captured_on DATE, state TEXT NOT NULL CHECK (state IN ('active','revoked')),
        origin_endpoint TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (origin_vault_id, origin_share_id, origin_asset_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_distribution (
        incoming_share_id UUID NOT NULL REFERENCES vault_federation_incoming_shares(incoming_share_id),
        target_type TEXT NOT NULL CHECK (target_type IN ('local_all','local_user')), target_user_id UUID REFERENCES auth_accounts(user_id),
        created_by UUID NOT NULL REFERENCES auth_accounts(user_id), revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK ((target_type='local_all' AND target_user_id IS NULL) OR (target_type='local_user' AND target_user_id IS NOT NULL)),
        PRIMARY KEY (incoming_share_id, target_type, target_user_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_receipts (
        origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id), event_id UUID NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (origin_vault_id, event_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_audit (
        audit_id UUID PRIMARY KEY, event_type TEXT NOT NULL, origin_vault_id UUID, target_vault_id UUID,
        federation_share_id UUID, detail TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_origin_metadata (
        origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id), origin_asset_id UUID NOT NULL,
        schema_version INTEGER NOT NULL, revision BIGINT NOT NULL, snapshot JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(origin_vault_id, origin_asset_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_local_annotations (
        origin_vault_id UUID NOT NULL, origin_asset_id UUID NOT NULL,
        recipient_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
        note TEXT, alias TEXT, tags JSONB NOT NULL DEFAULT '[]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(origin_vault_id, origin_asset_id, recipient_user_id)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_deliveries_due_idx ON vault_federation_deliveries(next_attempt_at) WHERE state='pending'")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS vault_federation_outgoing_open_idx ON vault_federation_outgoing_shares(origin_asset_id,target_vault_id) WHERE state IN ('pending','active')")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_incoming_visible_idx ON vault_federation_incoming_shares(state, asset_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_metadata_origin_idx ON vault_federation_origin_metadata(origin_vault_id, origin_asset_id)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_cache_entries (
        origin_vault_id UUID NOT NULL, origin_asset_id UUID NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('incomplete','complete','invalidated')),
        size_bytes BIGINT, sha256 CHAR(64), updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(origin_vault_id,origin_asset_id))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_viewer_progress (
        origin_vault_id UUID NOT NULL, origin_asset_id UUID NOT NULL, recipient_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
        position_seconds DOUBLE PRECISION NOT NULL CHECK(position_seconds>=0), duration_seconds DOUBLE PRECISION,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(origin_vault_id,origin_asset_id,recipient_user_id))""")
    cursor.execute("ALTER TABLE vault_federation_outgoing_shares ADD COLUMN IF NOT EXISTS download_allowed BOOLEAN NOT NULL DEFAULT FALSE")
    # Lifecycle ordering is deliberately separate from metadata revisions.  It
    # prevents a delayed activation/update from ever resurrecting a revoked
    # remote share, including rows created by Stages 6-9 before this column.
    cursor.execute("ALTER TABLE vault_federation_outgoing_shares ADD COLUMN IF NOT EXISTS lifecycle_revision BIGINT NOT NULL DEFAULT 0")
    cursor.execute("ALTER TABLE vault_federation_incoming_shares ADD COLUMN IF NOT EXISTS lifecycle_revision BIGINT NOT NULL DEFAULT 0")
    cursor.execute("ALTER TABLE vault_federation_deliveries ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 0")
    cursor.execute("ALTER TABLE vault_federation_incoming_shares ADD COLUMN IF NOT EXISTS download_allowed BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_download_operations (
        operation_id UUID PRIMARY KEY, incoming_share_id UUID NOT NULL REFERENCES vault_federation_incoming_shares(incoming_share_id),
        recipient_user_id UUID NOT NULL REFERENCES auth_accounts(user_id), origin_vault_id UUID NOT NULL, origin_asset_id UUID NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','transferring','verified','promotion_requested','completed','failed','cancelled')),
        idempotency_key UUID NOT NULL, local_asset_id UUID, expected_size_bytes BIGINT, expected_sha256 CHAR(64), staging_name TEXT, metadata_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        failure_reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
         UNIQUE(recipient_user_id,idempotency_key))""")
    cursor.execute("ALTER TABLE vault_federation_download_operations ADD COLUMN IF NOT EXISTS metadata_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_download_provenance (
        local_asset_id UUID PRIMARY KEY REFERENCES vault_assets(id), operation_id UUID NOT NULL UNIQUE REFERENCES vault_federation_download_operations(operation_id),
        origin_vault_id UUID NOT NULL, origin_asset_id UUID NOT NULL, origin_share_id UUID NOT NULL,
        origin_owner_label TEXT NOT NULL, source_sha256 CHAR(64) NOT NULL, metadata_snapshot JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_download_operations_open_idx ON vault_federation_download_operations(recipient_user_id, state) WHERE state IN ('reserved','transferring','verified','promotion_requested')")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_deliveries_priority_due_idx ON vault_federation_deliveries(priority DESC,next_attempt_at) WHERE state='pending'")
    # Remediation Pack 2: remote collections remain logical origin entities.
    # Their deliveries are deliberately separate from asset deliveries because
    # a collection share is not a synthetic file or a local asset record.
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_outgoing_collection_shares (
        federation_collection_share_id UUID PRIMARY KEY,
        origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
        origin_collection_id UUID NOT NULL REFERENCES vault_shared_collections(collection_id),
        owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
        target_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
        share_mode TEXT NOT NULL CHECK (share_mode IN ('quick','standard')),
        state TEXT NOT NULL CHECK (state IN ('pending','active','revoked','archived')),
        release_at TIMESTAMPTZ, activated_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ,
        lifecycle_revision BIGINT NOT NULL DEFAULT 0,
        membership_revision BIGINT NOT NULL DEFAULT 0,
        snapshot_hash CHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(origin_collection_id,target_vault_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_collection_deliveries (
        delivery_id UUID PRIMARY KEY,
        federation_collection_share_id UUID NOT NULL REFERENCES vault_federation_outgoing_collection_shares(federation_collection_share_id),
        event_id UUID NOT NULL UNIQUE, event_type TEXT NOT NULL, payload JSONB NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        acknowledged_at TIMESTAMPTZ, last_error TEXT,
        priority SMALLINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_incoming_collections (
        incoming_collection_id UUID PRIMARY KEY,
        origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
        origin_collection_id UUID NOT NULL,
        origin_collection_share_id UUID NOT NULL,
        owner_label TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
        category TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('active','revoked','archived')),
        origin_endpoint TEXT NOT NULL, lifecycle_revision BIGINT NOT NULL DEFAULT 0,
        membership_revision BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(origin_vault_id,origin_collection_id,origin_collection_share_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_collection_memberships (
        incoming_collection_id UUID NOT NULL REFERENCES vault_federation_incoming_collections(incoming_collection_id),
        origin_vault_id UUID NOT NULL, origin_collection_id UUID NOT NULL,
        origin_asset_id UUID NOT NULL, member_revision BIGINT NOT NULL,
        added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(origin_vault_id,origin_collection_id,origin_asset_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_federation_collection_distribution (
        incoming_collection_id UUID NOT NULL REFERENCES vault_federation_incoming_collections(incoming_collection_id),
        target_type TEXT NOT NULL CHECK (target_type IN ('local_all','local_user')),
        target_user_id UUID REFERENCES auth_accounts(user_id),
        target_key TEXT NOT NULL,
        created_by UUID NOT NULL REFERENCES auth_accounts(user_id), revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK ((target_type='local_all' AND target_user_id IS NULL AND target_key='*') OR (target_type='local_user' AND target_user_id IS NOT NULL AND target_key=target_user_id::text)),
        PRIMARY KEY(incoming_collection_id,target_type,target_key)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_collection_delivery_due_idx ON vault_federation_collection_deliveries(priority DESC,next_attempt_at) WHERE state='pending'")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_incoming_collection_visible_idx ON vault_federation_incoming_collections(state,category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS vault_federation_collection_member_asset_idx ON vault_federation_collection_memberships(origin_vault_id,origin_asset_id)")


class FederationStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def local_vault_id(self) -> UUID:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id FROM vaults WHERE is_local=TRUE")
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise RuntimeError("Federation requires exactly one local Vault identity")
            return UUID(str(rows[0]["vault_id"]))

    def list_paired_vaults(self) -> list[PairedVault]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id, display_label, endpoint, trust_state, created_at, updated_at FROM vaults WHERE is_local=FALSE AND trust_state='trusted' ORDER BY display_label, vault_id")
            return [PairedVault(UUID(str(row["vault_id"])), str(row["display_label"]), str(row["endpoint"]), str(row["trust_state"]), row["created_at"], row["updated_at"]) for row in cursor.fetchall()]

    def pair_vault(self, remote_vault_id: UUID, label: str, endpoint: str, pairing_key: str) -> PairedVault:
        if not label.strip() or not endpoint.startswith("https://") or len(pairing_key) < 32:
            raise ValueError("A label, HTTPS endpoint, and pairing key of at least 32 characters are required")
        if remote_vault_id == self.local_vault_id():
            raise ValueError("A Vault cannot pair with itself")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vaults(vault_id,is_local,display_label,endpoint,pairing_key,trust_state)
                VALUES (%s,FALSE,%s,%s,%s,'trusted') ON CONFLICT(vault_id) DO UPDATE SET
                display_label=EXCLUDED.display_label, endpoint=EXCLUDED.endpoint, pairing_key=EXCLUDED.pairing_key,
                trust_state='trusted', updated_at=CURRENT_TIMESTAMP RETURNING vault_id,display_label,endpoint,trust_state,created_at,updated_at""", (remote_vault_id,label.strip(),endpoint.rstrip('/'),pairing_key))
            row=cursor.fetchone()
            return PairedVault(UUID(str(row['vault_id'])),str(row['display_label']),str(row['endpoint']),str(row['trust_state']),row['created_at'],row['updated_at'])

    def unpair_vault(self, remote_vault_id: UUID) -> None:
        """Fail closed locally while retaining provenance and durable audit facts."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vaults SET trust_state='disabled',updated_at=CURRENT_TIMESTAMP WHERE vault_id=%s AND is_local=FALSE AND trust_state='trusted' RETURNING vault_id", (remote_vault_id,))
            if cursor.fetchone() is None:
                raise ValueError("Trusted paired Vault is unavailable")
            cursor.execute("UPDATE vault_federation_incoming_shares SET state='revoked',updated_at=CURRENT_TIMESTAMP WHERE origin_vault_id=%s AND state='active'", (remote_vault_id,))
            cursor.execute("UPDATE vault_federation_incoming_collections SET state='revoked',updated_at=CURRENT_TIMESTAMP WHERE origin_vault_id=%s AND state='active'", (remote_vault_id,))
            cursor.execute("UPDATE vault_federation_collection_distribution distribution SET revoked_at=CURRENT_TIMESTAMP FROM vault_federation_incoming_collections collection WHERE collection.incoming_collection_id=distribution.incoming_collection_id AND collection.origin_vault_id=%s AND distribution.revoked_at IS NULL", (remote_vault_id,))
            cursor.execute("SELECT origin_asset_id FROM vault_federation_cache_entries WHERE origin_vault_id=%s AND state!='invalidated'", (remote_vault_id,))
            cached_assets = [UUID(str(row['origin_asset_id'])) for row in cursor.fetchall()]
            cursor.execute("UPDATE vault_federation_cache_entries SET state='invalidated',updated_at=CURRENT_TIMESTAMP WHERE origin_vault_id=%s AND state!='invalidated'", (remote_vault_id,))
            cursor.execute("DELETE FROM vault_federation_viewer_progress WHERE origin_vault_id=%s", (remote_vault_id,))
            cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,detail) VALUES(%s,'peer_unpaired',%s,%s,'effective remote access disabled')", (uuid4(),remote_vault_id,self.local_vault_id()))
        root = Path(os.getenv('PV_FEDERATION_CACHE_ROOT', '/vault/.cache/federation')).resolve()
        for asset_id in cached_assets:
            candidate = (root / str(remote_vault_id) / f'{asset_id}.cache').resolve()
            if root in candidate.parents:
                candidate.unlink(missing_ok=True)

    def diagnostics(self) -> dict[str, int]:
        """Small operational signal for health/UI; no endpoint, key, or media facts."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FILTER (WHERE state='pending'),count(*) FILTER (WHERE state='failed'),count(*) FILTER (WHERE state='pending' AND updated_at<CURRENT_TIMESTAMP-INTERVAL '10 minutes') FROM vault_federation_deliveries")
            pending, failed, stuck = cursor.fetchone().values()
            cursor.execute("SELECT count(*) FILTER (WHERE state='pending'),count(*) FILTER (WHERE state='failed'),count(*) FILTER (WHERE state='pending' AND updated_at<CURRENT_TIMESTAMP-INTERVAL '10 minutes') FROM vault_federation_collection_deliveries")
            collection_pending, collection_failed, collection_stuck = cursor.fetchone().values()
            cursor.execute("SELECT count(*) FILTER (WHERE state='incomplete'),count(*) FILTER (WHERE state='invalidated') FROM vault_federation_cache_entries")
            incomplete_cache, invalidated_cache = cursor.fetchone().values()
            cursor.execute("SELECT count(*) FILTER (WHERE state IN ('reserved','transferring','verified','promotion_requested')) FROM vault_federation_download_operations")
            open_downloads = next(iter(cursor.fetchone().values()))
            return {"pending_deliveries": int(pending) + int(collection_pending), "failed_deliveries": int(failed) + int(collection_failed), "stuck_deliveries": int(stuck) + int(collection_stuck), "pending_collection_deliveries": int(collection_pending), "failed_collection_deliveries": int(collection_failed), "incomplete_cache": int(incomplete_cache), "invalidated_cache": int(invalidated_cache), "open_downloads": int(open_downloads)}

    def recent_audit(self, limit: int = 100) -> list[dict[str, object]]:
        """Bounded operational audit; intentionally omits message bodies and secrets."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT event_type,origin_vault_id,target_vault_id,federation_share_id,detail,created_at
                FROM vault_federation_audit ORDER BY created_at DESC LIMIT %s""", (max(1, min(limit, 200)),))
            return [dict(row) for row in cursor.fetchall()]

    def cleanup_stale_cache(self, older_than_seconds: int = 900) -> int:
        """Remove only incomplete/invalidated managed cache artifacts, never owned copies."""
        threshold = max(60, min(older_than_seconds, 86_400))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT origin_vault_id,origin_asset_id FROM vault_federation_cache_entries
                WHERE state IN ('incomplete','invalidated') AND updated_at<CURRENT_TIMESTAMP-(%s * INTERVAL '1 second')
                FOR UPDATE SKIP LOCKED""", (threshold,))
            entries = [(UUID(str(row['origin_vault_id'])), UUID(str(row['origin_asset_id']))) for row in cursor.fetchall()]
            cursor.execute("DELETE FROM vault_federation_cache_entries WHERE state IN ('incomplete','invalidated') AND updated_at<CURRENT_TIMESTAMP-(%s * INTERVAL '1 second')", (threshold,))
            if entries:
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,detail) VALUES(%s,'cache_orphans_cleaned',%s)", (uuid4(), f"count={len(entries)}"))
        root = Path(os.getenv('PV_FEDERATION_CACHE_ROOT', '/vault/.cache/federation')).resolve()
        for origin_vault_id, asset_id in entries:
            candidate = (root / str(origin_vault_id) / f'{asset_id}.cache').resolve()
            if root in candidate.parents:
                candidate.unlink(missing_ok=True)
        return len(entries)

    def recover_stale_download_operations(self, older_than_seconds: int = 3_600) -> int:
        """Fail safely closed on interrupted pre-promotion downloads.

        No canonical owned asset exists before completion, so marking a stale
        transfer failed cannot remove recipient-owned content or manufacture a
        database claim for bytes that were never promoted.
        """
        threshold = max(300, min(older_than_seconds, 86_400))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_federation_download_operations
                SET state='failed',failure_reason='interrupted before canonical promotion; retry required',updated_at=CURRENT_TIMESTAMP
                WHERE state IN ('reserved','transferring','verified')
                  AND updated_at<CURRENT_TIMESTAMP-(%s * INTERVAL '1 second') RETURNING operation_id""", (threshold,))
            count = len(cursor.fetchall())
            if count:
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,detail) VALUES(%s,'download_recovery_failed_closed',%s)", (uuid4(), f"count={count}"))
            return count

    @staticmethod
    def _bounded_text(value: object, limit: int = MAX_METADATA_TEXT) -> str | None:
        if not isinstance(value, str):
            return None
        value=value.strip()
        return value[:limit] if value else None

    @classmethod
    def _metadata_snapshot(cls, cursor: psycopg.Cursor, asset_id: UUID) -> dict[str, object]:
        """Build the small, typed, publishable view; never export model evidence."""
        cursor.execute("SELECT display_title,captured_on,location,effective_metadata,mime_type,file.filename,file.size_bytes,file.sha256 FROM vault_assets asset JOIN vault_files file ON file.asset_id=asset.id WHERE asset.id=%s ORDER BY file.file_role='primary' DESC,file.created_at LIMIT 1", (asset_id,))
        row=cursor.fetchone()
        if row is None: raise ValueError("Origin asset is unavailable")
        effective=dict(row['effective_metadata'] or {})
        def text(*names: str) -> str | None:
            for name in names:
                if (value:=cls._bounded_text(effective.get(name))) is not None: return value
            return None
        tags=effective.get('tags', effective.get('content_tags', []))
        safe_tags=[value.strip()[:160] for value in tags if isinstance(value,str) and value.strip()][:MAX_METADATA_TAGS] if isinstance(tags,list) else []
        cursor.execute("SELECT to_regclass('vault_asset_people') AS people_table")
        if cursor.fetchone()['people_table']:
            cursor.execute("""SELECT DISTINCT people.display_name FROM vault_asset_people associations
                JOIN vault_people people ON people.id=associations.person_id
                WHERE associations.asset_id=%s AND associations.active AND people.active ORDER BY people.display_name LIMIT 64""", (asset_id,))
            people=[str(item['display_name'])[:160] for item in cursor.fetchall()]
        else:
            people=[]
        snapshot={
            "schema_version": FEDERATED_METADATA_SCHEMA_VERSION,
            "title": str(row['display_title'])[:500],
            "captured_on": row['captured_on'].isoformat() if row['captured_on'] else None,
            "media_type": str(row['mime_type'])[:160],
            "filename": str(row['filename'])[:500],
            "description": text('florence_description','description','caption'),
            "tags": safe_tags,
            # These sensitive families stay absent unless an origin policy
            # explicitly permits them; absence is distinct from an empty value.
            "location": cls._bounded_text(row['location'], 500) if effective.get('federation_share_location') is True else None,
            "ocr_text": text('ocr_text') if effective.get('federation_share_ocr') is True else None,
            "collection_context": text('collection_context','event_context'),
            "people": people if effective.get('federation_share_people') is True else [],
            "size_bytes": int(row['size_bytes']),
            "sha256": str(row['sha256']),
        }
        # Full snapshots deliberately retain null/[]: their arrival removes stale fields.
        return snapshot

    def _materialize_origin_metadata(self, cursor: psycopg.Cursor, origin_vault_id: UUID, asset_id: UUID) -> tuple[int, dict[str, object], bool]:
        snapshot=self._metadata_snapshot(cursor,asset_id)
        cursor.execute("SELECT revision,snapshot FROM vault_federation_origin_metadata WHERE origin_vault_id=%s AND origin_asset_id=%s FOR UPDATE", (origin_vault_id,asset_id))
        previous=cursor.fetchone()
        if previous and dict(previous['snapshot']) == snapshot:
            return int(previous['revision']), snapshot, False
        revision=(int(previous['revision'])+1) if previous else 1
        cursor.execute("""INSERT INTO vault_federation_origin_metadata(origin_vault_id,origin_asset_id,schema_version,revision,snapshot)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(origin_vault_id,origin_asset_id) DO UPDATE SET
            schema_version=EXCLUDED.schema_version,revision=EXCLUDED.revision,snapshot=EXCLUDED.snapshot,updated_at=CURRENT_TIMESTAMP""", (origin_vault_id,asset_id,FEDERATED_METADATA_SCHEMA_VERSION,revision,json.dumps(snapshot)))
        return revision,snapshot,True

    def _queue_metadata_event(self, cursor: psycopg.Cursor, share_id: UUID, *, force: bool = False) -> bool:
        cursor.execute("SELECT origin_vault_id,origin_asset_id,state FROM vault_federation_outgoing_shares WHERE federation_share_id=%s", (share_id,))
        share=cursor.fetchone()
        if share is None or share['state']!='active': return False
        revision,snapshot,changed=self._materialize_origin_metadata(cursor,UUID(str(share['origin_vault_id'])),UUID(str(share['origin_asset_id'])))
        if not (force or changed): return False
        cursor.execute("SELECT 1 FROM vault_federation_deliveries WHERE federation_share_id=%s AND event_type='metadata_snapshot' AND state='pending'", (share_id,))
        if cursor.fetchone() is not None: return False
        payload={"share_id":str(share_id),"asset_id":str(share['origin_asset_id']),"metadata_schema_version":FEDERATED_METADATA_SCHEMA_VERSION,"metadata_revision":revision,"origin_metadata":snapshot}
        cursor.execute("INSERT INTO vault_federation_deliveries(delivery_id,federation_share_id,event_id,event_type,payload) VALUES(%s,%s,%s,'metadata_snapshot',%s)", (uuid4(),share_id,uuid4(),json.dumps(payload)))
        cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,federation_share_id,detail) VALUES(%s,'metadata_snapshot_queued',%s,%s,%s)", (uuid4(),share['origin_vault_id'],share_id,f"revision={revision}"))
        return True

    def backfill_active_metadata(self, limit: int = 100) -> int:
        """Restart-safe Stage 6-to-7 backfill; only currently active origin shares qualify."""
        queued=0
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT federation_share_id FROM vault_federation_outgoing_shares WHERE state='active' ORDER BY updated_at LIMIT %s FOR UPDATE SKIP LOCKED", (limit,))
            for row in cursor.fetchall():
                if self._queue_metadata_event(cursor,UUID(str(row['federation_share_id']))): queued += 1
            if queued:
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,detail) VALUES(%s,'metadata_backfill_completed',%s)", (uuid4(),f"queued={queued}"))
        return queued

    @staticmethod
    def _collection_snapshot(cursor: psycopg.Cursor, collection_share_id: UUID) -> dict[str, object]:
        """Build a versioned logical-collection snapshot from authoritative rows."""
        cursor.execute("""SELECT share.federation_collection_share_id,share.origin_vault_id,
                share.origin_collection_id,share.target_vault_id,share.state,share.lifecycle_revision,
                share.membership_revision,collection.name,collection.description,collection.archived_at,
                owner.display_name
            FROM vault_federation_outgoing_collection_shares share
            JOIN vault_shared_collections collection ON collection.collection_id=share.origin_collection_id
            JOIN auth_accounts owner ON owner.user_id=share.owner_user_id
            WHERE share.federation_collection_share_id=%s FOR UPDATE""", (collection_share_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Unknown federated collection share")
        cursor.execute("""SELECT asset.id,asset.asset_type,asset.display_title,asset.captured_on
            FROM vault_shared_collection_members member
            JOIN vault_assets asset ON asset.id=member.asset_id
            WHERE member.collection_id=%s ORDER BY asset.id""", (row["origin_collection_id"],))
        members = [
            {"asset_id": str(member["id"]), "asset_type": str(member["asset_type"]),
             "display_title": str(member["display_title"]),
             "captured_on": member["captured_on"].isoformat() if member["captured_on"] else None}
            for member in cursor.fetchall()
        ]
        return {
            "collection_share_id": str(row["federation_collection_share_id"]),
            "collection_id": str(row["origin_collection_id"]),
            "state": "archived" if row["archived_at"] is not None else str(row["state"]),
            "name": str(row["name"]), "description": row["description"], "category": "Gallery",
            "owner_label": str(row["display_name"]), "members": members,
            "lifecycle_revision": int(row["lifecycle_revision"]),
            "membership_revision": int(row["membership_revision"]),
        }

    def _queue_collection_snapshot(self, cursor: psycopg.Cursor, collection_share_id: UUID, *, force: bool = False) -> bool:
        snapshot = self._collection_snapshot(cursor, collection_share_id)
        origin_endpoint = os.getenv("PV_FEDERATION_ENDPOINT", "").rstrip("/")
        if not origin_endpoint.startswith("https://"):
            raise ValueError("Federation requires configured HTTPS origin endpoint")
        snapshot["origin_endpoint"] = origin_endpoint
        # Hash the authoritative logical content, not delivery revisions.  This
        # makes periodic reconciliation idempotent while allowing it to replay
        # the exact current revision after an offline receiver reconnects.
        fingerprint = dict(snapshot)
        fingerprint.pop("lifecycle_revision", None)
        fingerprint.pop("membership_revision", None)
        digest = hashlib.sha256(canonical_json(fingerprint)).hexdigest()
        cursor.execute("SELECT snapshot_hash,state,lifecycle_revision,membership_revision FROM vault_federation_outgoing_collection_shares WHERE federation_collection_share_id=%s FOR UPDATE", (collection_share_id,))
        current = cursor.fetchone()
        if current is None:
            return False
        state = str(snapshot["state"])
        event_type = "collection_revoked" if state == "revoked" else "collection_archived" if state == "archived" else "collection_snapshot"
        if not force and current["snapshot_hash"] == digest:
            return False
        changed = current["snapshot_hash"] != digest
        if state == "archived" and current["state"] != "archived":
            cursor.execute("UPDATE vault_federation_outgoing_collection_shares SET state='archived',lifecycle_revision=lifecycle_revision+1,snapshot_hash=%s,updated_at=CURRENT_TIMESTAMP WHERE federation_collection_share_id=%s RETURNING lifecycle_revision,membership_revision", (digest, collection_share_id))
            revisions = cursor.fetchone()
        elif event_type == "collection_snapshot" and changed:
            cursor.execute("""UPDATE vault_federation_outgoing_collection_shares
                SET membership_revision=membership_revision+1,snapshot_hash=%s,updated_at=CURRENT_TIMESTAMP
                WHERE federation_collection_share_id=%s RETURNING membership_revision,lifecycle_revision""", (digest, collection_share_id))
            revisions = cursor.fetchone()
        else:
            cursor.execute("UPDATE vault_federation_outgoing_collection_shares SET snapshot_hash=%s,updated_at=CURRENT_TIMESTAMP WHERE federation_collection_share_id=%s RETURNING membership_revision,lifecycle_revision", (digest, collection_share_id))
            revisions = cursor.fetchone()
        snapshot["membership_revision"] = int(revisions["membership_revision"])
        snapshot["lifecycle_revision"] = int(revisions["lifecycle_revision"])
        if not force and not changed:
            return False
        cursor.execute("""INSERT INTO vault_federation_collection_deliveries(
                delivery_id,federation_collection_share_id,event_id,event_type,payload,priority)
            VALUES(%s,%s,%s,%s,%s,%s)""", (
                uuid4(), collection_share_id, uuid4(), event_type, json.dumps(snapshot),
                100 if event_type in {"collection_revoked", "collection_archived"} else 0,
            ))
        cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,federation_share_id,detail) VALUES(%s,%s,%s,%s)", (uuid4(), event_type, collection_share_id, "collection snapshot queued"))
        return True

    def create_outgoing_collection_share(self, owner_user_id: UUID, collection_id: UUID, target_vault_id: UUID, share_mode: Literal["quick", "standard"]) -> UUID:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id FROM vaults WHERE is_local=TRUE")
            local = cursor.fetchone()
            cursor.execute("SELECT 1 FROM vaults WHERE vault_id=%s AND is_local=FALSE AND trust_state='trusted'", (target_vault_id,))
            if local is None or cursor.fetchone() is None:
                raise ValueError("Select a trusted paired Vault")
            cursor.execute("""SELECT 1 FROM vault_shared_collections
                WHERE collection_id=%s AND owner_user_id=%s AND origin_vault_id=%s AND archived_at IS NULL""", (collection_id, owner_user_id, local["vault_id"]))
            if cursor.fetchone() is None:
                raise ValueError("Federated collection sharing requires an owned active local collection")
            cursor.execute("""SELECT 1 FROM vault_shared_collection_members member
                JOIN vault_assets asset ON asset.id=member.asset_id
                WHERE member.collection_id=%s AND (asset.owner_user_id<>%s OR asset.origin_vault_id<>%s) LIMIT 1""", (collection_id, owner_user_id, local["vault_id"]))
            if cursor.fetchone() is not None:
                raise ValueError("Federated collections may contain only owned local assets")
            quick = share_mode == "quick"
            cursor.execute("""SELECT federation_collection_share_id,state FROM vault_federation_outgoing_collection_shares
                WHERE origin_collection_id=%s AND target_vault_id=%s FOR UPDATE""", (collection_id, target_vault_id))
            previous = cursor.fetchone()
            if previous is not None and str(previous["state"]) in {"pending", "active"}:
                raise ValueError("This collection is already shared with that Vault")
            share_id = UUID(str(previous["federation_collection_share_id"])) if previous is not None else uuid4()
            if previous is None:
                cursor.execute("""INSERT INTO vault_federation_outgoing_collection_shares(
                    federation_collection_share_id,origin_vault_id,origin_collection_id,owner_user_id,target_vault_id,
                    share_mode,state,release_at,activated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP+INTERVAL '180 seconds' END,
                    CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)""", (share_id, local["vault_id"], collection_id, owner_user_id, target_vault_id, share_mode, "active" if quick else "pending", quick, quick))
            else:
                # A later explicit re-share retains the same immutable origin
                # identity but advances lifecycle state so a revoked receiver
                # can converge without a duplicate logical collection.
                cursor.execute("""UPDATE vault_federation_outgoing_collection_shares SET
                    share_mode=%s,state=%s,release_at=CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP+INTERVAL '180 seconds' END,
                    activated_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,revoked_at=NULL,
                    lifecycle_revision=lifecycle_revision+1,snapshot_hash=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE federation_collection_share_id=%s""", (share_mode, "active" if quick else "pending", quick, quick, share_id))
            if quick:
                self._queue_collection_snapshot(cursor, share_id, force=True)
            return share_id

    def transition_outgoing_collection(self, collection_share_id: UUID, owner_user_id: UUID, action: Literal["activate", "revoke"]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state FROM vault_federation_outgoing_collection_shares WHERE federation_collection_share_id=%s AND owner_user_id=%s FOR UPDATE", (collection_share_id, owner_user_id))
            row = cursor.fetchone()
            if row is None or row["state"] not in {"pending", "active"} or (action == "activate" and row["state"] != "pending"):
                raise ValueError("Federated collection share is unavailable")
            cursor.execute("""UPDATE vault_federation_outgoing_collection_shares SET
                state=%s,lifecycle_revision=lifecycle_revision+1,activated_at=CASE WHEN %s='activate' THEN CURRENT_TIMESTAMP ELSE activated_at END,
                revoked_at=CASE WHEN %s='revoke' THEN CURRENT_TIMESTAMP ELSE NULL END,updated_at=CURRENT_TIMESTAMP
                WHERE federation_collection_share_id=%s""", ("active" if action == "activate" else "revoked", action, action, collection_share_id))
            self._queue_collection_snapshot(cursor, collection_share_id, force=True)

    def reconcile_collections(self, target_vault_id: UUID | None = None, limit: int = 100) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT federation_collection_share_id FROM vault_federation_outgoing_collection_shares share
                JOIN vaults peer ON peer.vault_id=share.target_vault_id AND peer.trust_state='trusted'
                WHERE share.state IN ('active','revoked','archived') AND (%s::uuid IS NULL OR share.target_vault_id=%s)
                ORDER BY share.updated_at LIMIT %s FOR UPDATE SKIP LOCKED""", (target_vault_id, target_vault_id, limit))
            ids = [UUID(str(row["federation_collection_share_id"])) for row in cursor.fetchall()]
            for share_id in ids:
                self._queue_collection_snapshot(cursor, share_id, force=True)
            return len(ids)

    def _receive_collection_event(self, origin: UUID, target: UUID, event_id: UUID, event_type: str, share: dict[str, object], envelope: dict[str, object], signature: str) -> bool:
        """Apply a full, signed collection snapshot with monotonic revisions."""
        try:
            collection_share_id=UUID(str(share["collection_share_id"])); collection_id=UUID(str(share["collection_id"]))
            lifecycle_revision=int(share.get("lifecycle_revision", 0)); membership_revision=int(share.get("membership_revision", 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Malformed federated collection identity") from None
        if lifecycle_revision < 0 or membership_revision < 0:
            raise ValueError("Malformed federated collection revision")
        members=share.get("members", [])
        if not isinstance(members,list) or len(members)>10_000:
            raise ValueError("Malformed federated collection members")
        parsed_members=[]
        for member in members:
            if not isinstance(member,dict): raise ValueError("Malformed federated collection member")
            try: asset_id=UUID(str(member["asset_id"]))
            except (KeyError,ValueError): raise ValueError("Malformed federated collection member") from None
            parsed_members.append((asset_id,str(member.get("asset_type","other"))[:160],str(member.get("display_title","Untitled"))[:500],member.get("captured_on")))
        if len({asset_id for asset_id,*_ in parsed_members}) != len(parsed_members):
            raise ValueError("Duplicate federated collection member")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pairing_key,trust_state FROM vaults WHERE vault_id=%s AND is_local=FALSE",(origin,)); peer=cursor.fetchone()
            if peer is None or peer['trust_state']!='trusted' or not peer['pairing_key'] or not verify_envelope(envelope,signature,str(peer['pairing_key'])):
                raise ValueError("Untrusted or unauthenticated federation event")
            if self.local_vault_id()!=target: raise ValueError("Federation event targets another Vault")
            cursor.execute("INSERT INTO vault_federation_receipts(origin_vault_id,event_id) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event_id",(origin,event_id))
            if cursor.fetchone() is None: return False
            cursor.execute("""SELECT * FROM vault_federation_incoming_collections
                WHERE origin_vault_id=%s AND origin_collection_id=%s AND origin_collection_share_id=%s FOR UPDATE""",(origin,collection_id,collection_share_id))
            existing=cursor.fetchone()
            if existing is not None and (
                int(existing['lifecycle_revision']) > lifecycle_revision
                or (
                    event_type == 'collection_snapshot'
                    and int(existing['lifecycle_revision']) == lifecycle_revision
                    and int(existing['membership_revision']) > membership_revision
                )
                or (str(existing['state']) in {'revoked','archived'} and int(existing['lifecycle_revision']) >= lifecycle_revision and event_type=='collection_snapshot')
            ):
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,detail) VALUES(%s,'collection_stale_ignored',%s,%s,%s)",(uuid4(),origin,target,f"collection={collection_id};revision={lifecycle_revision}/{membership_revision}"))
                return True
            state='revoked' if event_type=='collection_revoked' else 'archived' if event_type=='collection_archived' else str(share.get('state','active'))
            if state not in {'active','revoked','archived'}: raise ValueError("Malformed federated collection state")
            if existing is None:
                incoming_id=uuid4()
                cursor.execute("""INSERT INTO vault_federation_incoming_collections(incoming_collection_id,origin_vault_id,origin_collection_id,origin_collection_share_id,owner_label,name,description,category,state,origin_endpoint,lifecycle_revision,membership_revision)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(incoming_id,origin,collection_id,collection_share_id,str(share.get('owner_label','Origin Vault'))[:500],str(share.get('name','Untitled'))[:500],self._bounded_text(share.get('description'),2000),str(share.get('category','Gallery'))[:160],state,str(share.get('origin_endpoint','')),lifecycle_revision,membership_revision))
            else:
                incoming_id=UUID(str(existing['incoming_collection_id']))
                cursor.execute("""UPDATE vault_federation_incoming_collections SET owner_label=%s,name=%s,description=%s,category=%s,state=%s,origin_endpoint=%s,lifecycle_revision=GREATEST(lifecycle_revision,%s),membership_revision=GREATEST(membership_revision,%s),updated_at=CURRENT_TIMESTAMP WHERE incoming_collection_id=%s""",(str(share.get('owner_label','Origin Vault'))[:500],str(share.get('name','Untitled'))[:500],self._bounded_text(share.get('description'),2000),str(share.get('category','Gallery'))[:160],state,str(share.get('origin_endpoint','')),lifecycle_revision,membership_revision,incoming_id))
            if state=='active' and (existing is None or membership_revision>=int(existing['membership_revision'])):
                cursor.execute("DELETE FROM vault_federation_collection_memberships WHERE incoming_collection_id=%s",(incoming_id,))
                for asset_id,asset_type,title,captured_on in parsed_members:
                    cursor.execute("INSERT INTO vault_federation_collection_memberships(incoming_collection_id,origin_vault_id,origin_collection_id,origin_asset_id,member_revision) VALUES(%s,%s,%s,%s,%s)",(incoming_id,origin,collection_id,asset_id,membership_revision))
                    # An incoming asset row is an access path only.  Its canonical
                    # remote identity stays (origin_vault_id, origin_asset_id).
                    cursor.execute("""INSERT INTO vault_federation_incoming_shares(incoming_share_id,origin_vault_id,origin_asset_id,origin_share_id,owner_label,asset_type,display_title,captured_on,state,origin_endpoint,lifecycle_revision)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                        ON CONFLICT(origin_vault_id,origin_share_id,origin_asset_id) DO UPDATE SET owner_label=EXCLUDED.owner_label,asset_type=EXCLUDED.asset_type,display_title=EXCLUDED.display_title,captured_on=EXCLUDED.captured_on,state='active',origin_endpoint=EXCLUDED.origin_endpoint,lifecycle_revision=EXCLUDED.lifecycle_revision,updated_at=CURRENT_TIMESTAMP""",(uuid4(),origin,asset_id,collection_share_id,str(share.get('owner_label','Origin Vault'))[:500],asset_type,title,captured_on,str(share.get('origin_endpoint','')),lifecycle_revision))
                cursor.execute("""UPDATE vault_federation_incoming_shares SET state='revoked',updated_at=CURRENT_TIMESTAMP
                    WHERE origin_vault_id=%s AND origin_share_id=%s AND NOT (origin_asset_id=ANY(%s)) AND state='active'""",(origin,collection_share_id,[asset_id for asset_id,*_ in parsed_members]))
            elif state in {'revoked','archived'}:
                cursor.execute("UPDATE vault_federation_incoming_shares SET state='revoked',updated_at=CURRENT_TIMESTAMP WHERE origin_vault_id=%s AND origin_share_id=%s AND state='active'",(origin,collection_share_id))
            cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,detail) VALUES(%s,%s,%s,%s,%s)",(uuid4(),event_type,origin,target,f"collection={collection_id};members={len(parsed_members)}"))
            return True

    def receive_event(self, envelope: dict[str, object], signature: str) -> bool:
        required={"protocol_version","event_id","origin_vault_id","target_vault_id","event_type","timestamp","share"}
        if set(envelope) != required or envelope.get("protocol_version") != FEDERATION_PROTOCOL_VERSION:
            raise ValueError("Unsupported or malformed federation envelope")
        try:
            origin=UUID(str(envelope["origin_vault_id"])); target=UUID(str(envelope["target_vault_id"])); event_id=UUID(str(envelope["event_id"]))
            timestamp=datetime.fromisoformat(str(envelope["timestamp"]).replace("Z","+00:00"))
        except (ValueError, TypeError):
            raise ValueError("Malformed federation identity") from None
        if timestamp.tzinfo is None or abs((datetime.now(UTC)-timestamp).total_seconds()) > MAX_EVENT_AGE_SECONDS:
            raise ValueError("Stale federation event")
        share=envelope['share']
        if not isinstance(share,dict):
            raise ValueError("Malformed federation share")
        event_type=str(envelope['event_type'])
        if event_type in {'collection_snapshot','collection_revoked','collection_archived'}:
            return self._receive_collection_event(origin,target,event_id,event_type,share,envelope,signature)
        try:
            origin_share=UUID(str(share['share_id'])); asset=UUID(str(share['asset_id']))
        except (KeyError,ValueError):
            raise ValueError("Malformed federation share identity") from None
        if event_type not in {'share_created','share_activated','share_revoked','metadata_snapshot','metadata_updated'}:
            raise ValueError("Unsupported federation event type")
        try:
            lifecycle_revision=int(share.get('lifecycle_revision', 0))
        except (TypeError, ValueError):
            raise ValueError("Malformed federation lifecycle revision") from None
        if lifecycle_revision < 0:
            raise ValueError("Malformed federation lifecycle revision")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id,pairing_key,trust_state FROM vaults WHERE vault_id=%s AND is_local=FALSE",(origin,)); peer=cursor.fetchone()
            if peer is None or peer['trust_state']!='trusted' or not peer['pairing_key'] or not verify_envelope(envelope,signature,str(peer['pairing_key'])):
                raise ValueError("Untrusted or unauthenticated federation event")
            cursor.execute("SELECT vault_id FROM vaults WHERE is_local=TRUE"); local=cursor.fetchone()
            if local is None or UUID(str(local['vault_id'])) != target: raise ValueError("Federation event targets another Vault")
            cursor.execute("INSERT INTO vault_federation_receipts(origin_vault_id,event_id) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING event_id",(origin,event_id))
            if cursor.fetchone() is None: return False
            cursor.execute("SELECT state,lifecycle_revision FROM vault_federation_incoming_shares WHERE origin_vault_id=%s AND origin_share_id=%s AND origin_asset_id=%s FOR UPDATE", (origin,origin_share,asset))
            lifecycle = cursor.fetchone()
            # Equal revisions are idempotent.  A legacy (revision 0) revoke is
            # still authoritative and pins the row so another legacy activate
            # cannot restore access by arrival order alone.
            stale_lifecycle = lifecycle is not None and (
                int(lifecycle['lifecycle_revision']) > lifecycle_revision
                or (lifecycle['state'] == 'revoked' and int(lifecycle['lifecycle_revision']) >= lifecycle_revision and event_type != 'share_revoked')
            )
            if stale_lifecycle:
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,federation_share_id,detail) VALUES(%s,'lifecycle_stale_ignored',%s,%s,%s,%s)", (uuid4(),origin,target,origin_share,f"event={event_type};revision={lifecycle_revision}"))
                return True
            if event_type == 'share_revoked':
                cursor.execute("UPDATE vault_federation_incoming_shares SET state='revoked',lifecycle_revision=GREATEST(lifecycle_revision,%s),updated_at=CURRENT_TIMESTAMP WHERE origin_vault_id=%s AND origin_share_id=%s AND origin_asset_id=%s",(lifecycle_revision,origin,origin_share,asset))
                cursor.execute("UPDATE vault_federation_cache_entries SET state='invalidated',updated_at=CURRENT_TIMESTAMP WHERE origin_vault_id=%s AND origin_asset_id=%s",(origin,asset))
                cursor.execute("DELETE FROM vault_federation_viewer_progress WHERE origin_vault_id=%s AND origin_asset_id=%s",(origin,asset))
                root=Path(os.getenv('PV_FEDERATION_CACHE_ROOT','/vault/.cache/federation')).resolve()
                candidate=(root/str(origin)/f'{asset}.cache').resolve()
                if root in candidate.parents: candidate.unlink(missing_ok=True)
                cursor.execute("""DELETE FROM vault_federation_origin_metadata metadata WHERE metadata.origin_vault_id=%s AND metadata.origin_asset_id=%s
                    AND NOT EXISTS (SELECT 1 FROM vault_federation_incoming_shares incoming WHERE incoming.origin_vault_id=%s AND incoming.origin_asset_id=%s AND incoming.state='active')""", (origin,asset,origin,asset))
            elif event_type in {'share_created','share_activated'}:
                if not (event_type=='share_created' and share.get('state')!='active'):
                    cursor.execute("""INSERT INTO vault_federation_incoming_shares(incoming_share_id,origin_vault_id,origin_asset_id,origin_share_id,owner_label,asset_type,display_title,captured_on,state,origin_endpoint,lifecycle_revision)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s) ON CONFLICT(origin_vault_id,origin_share_id,origin_asset_id) DO UPDATE SET
                        owner_label=EXCLUDED.owner_label,asset_type=EXCLUDED.asset_type,display_title=EXCLUDED.display_title,captured_on=EXCLUDED.captured_on,state='active',origin_endpoint=EXCLUDED.origin_endpoint,lifecycle_revision=EXCLUDED.lifecycle_revision,updated_at=CURRENT_TIMESTAMP""",
                        (uuid4(),origin,asset,origin_share,str(share.get('owner_label','Origin Vault')),str(share.get('asset_type','other')),str(share.get('display_title','Untitled')),share.get('captured_on'),str(share.get('origin_endpoint','')),lifecycle_revision))
                    cursor.execute("UPDATE vault_federation_incoming_shares SET download_allowed=%s WHERE origin_vault_id=%s AND origin_share_id=%s AND origin_asset_id=%s",(share.get('download_allowed') is True,origin,origin_share,asset))
            elif event_type in {'metadata_snapshot','metadata_updated'}:
                try:
                    schema_version=int(share['metadata_schema_version']); revision=int(share['metadata_revision']); snapshot=share['origin_metadata']
                except (KeyError, TypeError, ValueError):
                    raise ValueError("Malformed federated metadata") from None
                if schema_version != FEDERATED_METADATA_SCHEMA_VERSION or revision < 1 or not isinstance(snapshot,dict) or snapshot.get('schema_version') != schema_version:
                    raise ValueError("Unsupported federated metadata")
                if len(canonical_json(snapshot)) > 64_000:
                    raise ValueError("Federated metadata is too large")
                cursor.execute("SELECT state FROM vault_federation_incoming_shares WHERE origin_vault_id=%s AND origin_share_id=%s AND origin_asset_id=%s", (origin,origin_share,asset))
                incoming=cursor.fetchone()
                if incoming is None or incoming['state']!='active':
                    raise ValueError("Federated metadata has no active share")
                cursor.execute("SELECT revision FROM vault_federation_origin_metadata WHERE origin_vault_id=%s AND origin_asset_id=%s FOR UPDATE", (origin,asset))
                existing=cursor.fetchone()
                if existing is not None and int(existing['revision']) >= revision:
                    cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,federation_share_id,detail) VALUES(%s,'metadata_stale_ignored',%s,%s,%s,%s)", (uuid4(),origin,target,origin_share,f"revision={revision}"))
                    return True
                cursor.execute("""INSERT INTO vault_federation_origin_metadata(origin_vault_id,origin_asset_id,schema_version,revision,snapshot)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(origin_vault_id,origin_asset_id) DO UPDATE SET
                    schema_version=EXCLUDED.schema_version,revision=EXCLUDED.revision,snapshot=EXCLUDED.snapshot,updated_at=CURRENT_TIMESTAMP""", (origin,asset,schema_version,revision,json.dumps(snapshot)))
            cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,federation_share_id,detail) VALUES(%s,%s,%s,%s,%s,%s)",(uuid4(),event_type,origin,target,origin_share,'received'))
            return True

    def create_outgoing_shares(self, owner_user_id: UUID, asset_ids: list[UUID], target_vault_id: UUID, share_mode: Literal["quick", "standard"]) -> list[UUID]:
        if not asset_ids or len(asset_ids) != len(set(asset_ids)):
            raise ValueError("Select one or more unique assets")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id,trust_state FROM vaults WHERE vault_id=%s AND is_local=FALSE", (target_vault_id,))
            peer=cursor.fetchone()
            if peer is None or peer['trust_state'] != 'trusted': raise ValueError("Select a trusted paired Vault")
            cursor.execute("SELECT vault_id FROM vaults WHERE is_local=TRUE"); local=cursor.fetchone()
            if local is None: raise RuntimeError("Missing local Vault identity")
            local_id=UUID(str(local['vault_id'])); quick=share_mode=='quick'; created=[]
            for asset_id in asset_ids:
                cursor.execute("SELECT id FROM vault_assets WHERE id=%s AND owner_user_id=%s AND origin_vault_id=%s",(asset_id,owner_user_id,local_id))
                if cursor.fetchone() is None: raise ValueError("Federation shares require an owned local asset")
                share_id=uuid4()
                cursor.execute("""INSERT INTO vault_federation_outgoing_shares(federation_share_id,origin_vault_id,origin_asset_id,owner_user_id,target_vault_id,share_mode,state,release_at,activated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP + INTERVAL '180 seconds' END,CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)""",
                    (share_id,local_id,asset_id,owner_user_id,target_vault_id,share_mode,'active' if quick else 'pending',quick,quick))
                if quick:
                    self._queue_event(cursor,share_id,'share_activated')
                    self._queue_metadata_event(cursor,share_id,force=True)
                created.append(share_id)
            return created

    @staticmethod
    def _queue_event(cursor: psycopg.Cursor, share_id: UUID, event_type: EventType) -> None:
        cursor.execute("""SELECT share.federation_share_id,share.origin_vault_id,share.origin_asset_id,share.target_vault_id,share.state,share.download_allowed,share.lifecycle_revision,
               asset.asset_type,asset.display_title,asset.captured_on,owner.display_name,peer.endpoint
               FROM vault_federation_outgoing_shares share JOIN vault_assets asset ON asset.id=share.origin_asset_id
               JOIN auth_accounts owner ON owner.user_id=share.owner_user_id JOIN vaults peer ON peer.vault_id=share.target_vault_id
               WHERE share.federation_share_id=%s""",(share_id,))
        row=cursor.fetchone()
        if row is None: raise ValueError("Unknown federation share")
        origin_endpoint=os.getenv("PV_FEDERATION_ENDPOINT", "").rstrip('/')
        if not origin_endpoint.startswith("https://"):
            raise ValueError("Federation requires configured HTTPS origin endpoint")
        payload={"share_id":str(row['federation_share_id']),"asset_id":str(row['origin_asset_id']),"state":str(row['state']),"asset_type":str(row['asset_type']),"display_title":str(row['display_title']),"captured_on":row['captured_on'].isoformat() if row['captured_on'] else None,"owner_label":str(row['display_name']),"origin_endpoint":origin_endpoint,"download_allowed":bool(row['download_allowed']),"lifecycle_revision":int(row['lifecycle_revision'])}
        cursor.execute("INSERT INTO vault_federation_deliveries(delivery_id,federation_share_id,event_id,event_type,payload,priority) VALUES(%s,%s,%s,%s,%s,%s)",(uuid4(),share_id,uuid4(),event_type,json.dumps(payload),100 if event_type=='share_revoked' else 0))

    def transition_outgoing(self, share_ids: list[UUID], owner_user_id: UUID, action: Literal['activate','revoke']) -> None:
        if not share_ids: raise ValueError("No federation shares selected")
        with self._connect() as connection, connection.cursor() as cursor:
            for share_id in share_ids:
                cursor.execute("SELECT state FROM vault_federation_outgoing_shares WHERE federation_share_id=%s AND owner_user_id=%s FOR UPDATE",(share_id,owner_user_id)); row=cursor.fetchone()
                if row is None or row['state'] not in {'pending','active'}: raise ValueError("Federation share is unavailable")
                if action=='activate' and row['state']!='pending': raise ValueError("Only pending federation shares can activate")
                cursor.execute("UPDATE vault_federation_outgoing_shares SET state=%s,activated_at=CASE WHEN %s='activate' THEN CURRENT_TIMESTAMP ELSE activated_at END,revoked_at=CASE WHEN %s='revoke' THEN CURRENT_TIMESTAMP ELSE NULL END,lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP WHERE federation_share_id=%s",('active' if action=='activate' else 'revoked',action,action,share_id))
                self._queue_event(cursor,share_id,'share_activated' if action=='activate' else 'share_revoked')
                if action=='activate': self._queue_metadata_event(cursor,share_id,force=True)

    def set_download_allowed(self, share_id: UUID, owner_user_id: UUID, allowed: bool) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_federation_outgoing_shares SET download_allowed=%s,lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP WHERE federation_share_id=%s AND owner_user_id=%s AND state='active' RETURNING federation_share_id",(allowed,share_id,owner_user_id))
            if cursor.fetchone() is None: raise ValueError('Federation share is unavailable')
            self._queue_event(cursor,share_id,'share_activated')

    @staticmethod
    def _download_operation(row: dict[str, object]) -> FederatedDownloadOperation:
        return FederatedDownloadOperation(
            UUID(str(row['operation_id'])), UUID(str(row['incoming_share_id'])), UUID(str(row['recipient_user_id'])),
            UUID(str(row['origin_vault_id'])), UUID(str(row['origin_asset_id'])), str(row['state']),
            UUID(str(row['idempotency_key'])), UUID(str(row['local_asset_id'])) if row['local_asset_id'] else None,
            int(row['expected_size_bytes']), str(row['expected_sha256']), str(row['staging_name']) if row['staging_name'] else None,
            str(row['failure_reason']) if row['failure_reason'] else None,
        )

    def reserve_download(self, incoming_share_id: UUID, recipient_user_id: UUID, idempotency_key: UUID) -> FederatedDownloadOperation:
        """Reserve one identity-bound local-copy operation, never from UI facts."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT incoming.*, metadata.snapshot FROM vault_federation_incoming_shares incoming
                JOIN vault_federation_distribution distribution ON distribution.incoming_share_id=incoming.incoming_share_id AND distribution.revoked_at IS NULL
                LEFT JOIN vault_federation_origin_metadata metadata ON metadata.origin_vault_id=incoming.origin_vault_id AND metadata.origin_asset_id=incoming.origin_asset_id
                WHERE incoming.incoming_share_id=%s AND incoming.state='active' AND incoming.download_allowed=TRUE
                AND (distribution.target_type='local_all' OR distribution.target_user_id=%s) FOR UPDATE OF incoming""", (incoming_share_id, recipient_user_id))
            share = cursor.fetchone()
            if share is None:
                raise ValueError('Download to My Vault is unavailable')
            metadata = share['snapshot'] if isinstance(share['snapshot'], dict) else json.loads(share['snapshot']) if share['snapshot'] else {}
            size, checksum = metadata.get('size_bytes'), metadata.get('sha256')
            if not isinstance(size, int) or size < 0 or not isinstance(checksum, str) or len(checksum) != 64:
                raise ValueError('Origin content facts are unavailable')
            cursor.execute("SELECT * FROM vault_federation_download_operations WHERE recipient_user_id=%s AND idempotency_key=%s", (recipient_user_id, idempotency_key))
            existing = cursor.fetchone()
            if existing is not None:
                if UUID(str(existing['incoming_share_id'])) != incoming_share_id:
                    raise ValueError('Idempotency key belongs to another download')
                return self._download_operation(existing)
            operation_id, local_asset_id = uuid4(), uuid4()
            staging_name = f'{operation_id}.part'
            cursor.execute("""INSERT INTO vault_federation_download_operations(operation_id,incoming_share_id,recipient_user_id,origin_vault_id,origin_asset_id,state,idempotency_key,local_asset_id,expected_size_bytes,expected_sha256,staging_name,metadata_snapshot)
                VALUES(%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s,%s,%s) RETURNING *""", (operation_id, incoming_share_id, recipient_user_id, share['origin_vault_id'], share['origin_asset_id'], idempotency_key, local_asset_id, size, checksum, staging_name, json.dumps(metadata)))
            return self._download_operation(cursor.fetchone())

    def set_download_operation_state(self, operation_id: UUID, recipient_user_id: UUID, state: str, *, failure_reason: str | None = None) -> FederatedDownloadOperation:
        if state not in {'reserved', 'transferring', 'verified', 'promotion_requested', 'completed', 'failed', 'cancelled'}:
            raise ValueError('Invalid federated download state')
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_federation_download_operations SET state=%s,failure_reason=%s,updated_at=CURRENT_TIMESTAMP WHERE operation_id=%s AND recipient_user_id=%s RETURNING *", (state, failure_reason, operation_id, recipient_user_id))
            row = cursor.fetchone()
            if row is None:
                raise ValueError('Federated download is unavailable')
            return self._download_operation(row)

    def complete_download(self, operation_id: UUID, recipient_user_id: UUID, local_asset_id: UUID) -> FederatedDownloadOperation:
        """Persist provenance only after the local catalogue has the promoted bytes."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT operation.*, incoming.origin_share_id,incoming.owner_label
                FROM vault_federation_download_operations operation
                JOIN vault_federation_incoming_shares incoming ON incoming.incoming_share_id=operation.incoming_share_id
                WHERE operation.operation_id=%s AND operation.recipient_user_id=%s FOR UPDATE""", (operation_id, recipient_user_id))
            row = cursor.fetchone()
            if row is None or UUID(str(row['local_asset_id'])) != local_asset_id or row['state'] not in {'verified', 'promotion_requested', 'completed'}:
                raise ValueError('Federated download cannot be completed')
            snapshot = row['metadata_snapshot'] if isinstance(row['metadata_snapshot'], dict) else json.loads(row['metadata_snapshot']) if row['metadata_snapshot'] else {}
            if not snapshot:
                raise ValueError('Federated download provenance is unavailable')
            cursor.execute("""INSERT INTO vault_federation_download_provenance(local_asset_id,operation_id,origin_vault_id,origin_asset_id,origin_share_id,origin_owner_label,source_sha256,metadata_snapshot)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(local_asset_id) DO NOTHING""", (local_asset_id, operation_id, row['origin_vault_id'], row['origin_asset_id'], row['origin_share_id'], row['owner_label'], row['expected_sha256'], json.dumps(snapshot)))
            cursor.execute("UPDATE vault_federation_download_operations SET state='completed',failure_reason=NULL,updated_at=CURRENT_TIMESTAMP WHERE operation_id=%s RETURNING *", (operation_id,))
            return self._download_operation(cursor.fetchone())

    def theatre_download_context(self, operation_id: UUID) -> tuple[FederatedDownloadOperation, IncomingFederatedShare, str] | None:
        """Worker-only context for a verified executor receipt; not an API lookup."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT operation.*, recipient.username AS recipient_username, incoming.origin_share_id,incoming.owner_label,incoming.asset_type,incoming.display_title,incoming.captured_on,
                incoming.state AS incoming_state,incoming.created_at AS incoming_created_at,incoming.updated_at AS incoming_updated_at,incoming.download_allowed,
                NULL::INTEGER AS metadata_revision,operation.metadata_snapshot AS origin_metadata
                FROM vault_federation_download_operations operation
                JOIN auth_accounts recipient ON recipient.user_id=operation.recipient_user_id
                JOIN vault_federation_incoming_shares incoming ON incoming.incoming_share_id=operation.incoming_share_id
                WHERE operation.operation_id=%s AND operation.state='promotion_requested'""", (operation_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            operation = self._download_operation(row)
            share = IncomingFederatedShare(UUID(str(row['incoming_share_id'])),UUID(str(row['origin_vault_id'])),UUID(str(row['origin_asset_id'])),UUID(str(row['origin_share_id'])),str(row['owner_label']),str(row['asset_type']),str(row['display_title']),row['captured_on'],str(row['incoming_state']),row['incoming_created_at'],row['incoming_updated_at'],int(row['metadata_revision']) if row['metadata_revision'] is not None else None,dict(row['origin_metadata']) if row['origin_metadata'] else None,bool(row['download_allowed']))
            return operation, share, str(row['recipient_username'])

    def list_outgoing(self, owner_user_id: UUID) -> list[OutgoingFederatedShare]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT share.federation_share_id,share.origin_asset_id,share.target_vault_id,peer.display_label,asset.display_title,asset.asset_type,share.share_mode,share.state,share.release_at,share.download_allowed
                FROM vault_federation_outgoing_shares share JOIN vault_assets asset ON asset.id=share.origin_asset_id JOIN vaults peer ON peer.vault_id=share.target_vault_id
                WHERE share.owner_user_id=%s ORDER BY share.created_at DESC""",(owner_user_id,))
            return [OutgoingFederatedShare(UUID(str(r['federation_share_id'])),UUID(str(r['origin_asset_id'])),UUID(str(r['target_vault_id'])),str(r['display_label']),str(r['display_title']),str(r['asset_type']),str(r['share_mode']),str(r['state']),r['release_at'],bool(r['download_allowed'])) for r in cursor.fetchall()]

    def list_outgoing_collections(self, owner_user_id: UUID) -> list[OutgoingFederatedCollection]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT share.federation_collection_share_id,share.origin_collection_id,share.target_vault_id,
                    peer.display_label,collection.name,share.share_mode,share.state,share.release_at,count(member.asset_id) AS member_count
                FROM vault_federation_outgoing_collection_shares share
                JOIN vault_shared_collections collection ON collection.collection_id=share.origin_collection_id
                JOIN vaults peer ON peer.vault_id=share.target_vault_id
                LEFT JOIN vault_shared_collection_members member ON member.collection_id=collection.collection_id
                WHERE share.owner_user_id=%s
                GROUP BY share.federation_collection_share_id,peer.display_label,collection.name
                ORDER BY share.created_at DESC""", (owner_user_id,))
            return [OutgoingFederatedCollection(
                UUID(str(row['federation_collection_share_id'])), UUID(str(row['origin_collection_id'])),
                UUID(str(row['target_vault_id'])), str(row['display_label']), str(row['name']),
                int(row['member_count']), str(row['share_mode']), str(row['state']), row['release_at'],
            ) for row in cursor.fetchall()]

    def release_due(self) -> list[UUID]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT federation_share_id FROM vault_federation_outgoing_shares WHERE state='pending' AND release_at<=CURRENT_TIMESTAMP FOR UPDATE")
            shares=[UUID(str(row['federation_share_id'])) for row in cursor.fetchall()]
            for share_id in shares:
                cursor.execute("UPDATE vault_federation_outgoing_shares SET state='active',activated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE federation_share_id=%s",(share_id,)); self._queue_event(cursor,share_id,'share_activated'); self._queue_metadata_event(cursor,share_id,force=True)
            cursor.execute("SELECT federation_collection_share_id FROM vault_federation_outgoing_collection_shares WHERE state='pending' AND release_at<=CURRENT_TIMESTAMP FOR UPDATE")
            collection_shares=[UUID(str(row['federation_collection_share_id'])) for row in cursor.fetchall()]
            for share_id in collection_shares:
                cursor.execute("UPDATE vault_federation_outgoing_collection_shares SET state='active',lifecycle_revision=lifecycle_revision+1,activated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE federation_collection_share_id=%s", (share_id,))
                self._queue_collection_snapshot(cursor, share_id, force=True)
            return shares + collection_shares

    def list_incoming_for_user(self, user_id: UUID, asset_type: str | None = None) -> list[IncomingFederatedShare]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT DISTINCT ON (incoming.origin_vault_id,incoming.origin_asset_id)
                incoming.*,metadata.revision AS metadata_revision,metadata.snapshot AS origin_metadata
                FROM vault_federation_incoming_shares incoming
                JOIN vaults peer ON peer.vault_id=incoming.origin_vault_id AND peer.is_local=FALSE AND peer.trust_state='trusted'
                LEFT JOIN vault_federation_origin_metadata metadata ON metadata.origin_vault_id=incoming.origin_vault_id AND metadata.origin_asset_id=incoming.origin_asset_id
                WHERE incoming.state='active' AND (%s::text IS NULL OR lower(incoming.asset_type)=lower(%s::text)) AND (
                  EXISTS (SELECT 1 FROM vault_federation_distribution distribution WHERE distribution.incoming_share_id=incoming.incoming_share_id AND distribution.revoked_at IS NULL AND (distribution.target_type='local_all' OR distribution.target_user_id=%s))
                  OR EXISTS (SELECT 1 FROM vault_federation_incoming_collections collection
                    JOIN vault_federation_collection_memberships member ON member.incoming_collection_id=collection.incoming_collection_id AND member.origin_asset_id=incoming.origin_asset_id
                    JOIN vault_federation_collection_distribution distribution ON distribution.incoming_collection_id=collection.incoming_collection_id AND distribution.revoked_at IS NULL
                    WHERE collection.origin_vault_id=incoming.origin_vault_id AND collection.origin_collection_share_id=incoming.origin_share_id AND collection.state='active' AND (distribution.target_type='local_all' OR distribution.target_user_id=%s))
                ) ORDER BY incoming.origin_vault_id,incoming.origin_asset_id,incoming.download_allowed DESC,incoming.updated_at DESC""",(asset_type,asset_type,user_id,user_id))
            return [IncomingFederatedShare(UUID(str(r['incoming_share_id'])),UUID(str(r['origin_vault_id'])),UUID(str(r['origin_asset_id'])),UUID(str(r['origin_share_id'])),str(r['owner_label']),str(r['asset_type']),str(r['display_title']),r['captured_on'],str(r['state']),r['created_at'],r['updated_at'],int(r['metadata_revision']) if r['metadata_revision'] is not None else None,dict(r['origin_metadata']) if r['origin_metadata'] else None,bool(r['download_allowed'])) for r in cursor.fetchall()]

    def list_incoming_admin(self) -> list[IncomingFederatedShare]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT incoming.*,metadata.revision AS metadata_revision,metadata.snapshot AS origin_metadata FROM vault_federation_incoming_shares incoming
                LEFT JOIN vault_federation_origin_metadata metadata ON metadata.origin_vault_id=incoming.origin_vault_id AND metadata.origin_asset_id=incoming.origin_asset_id ORDER BY incoming.updated_at DESC""")
            return [IncomingFederatedShare(UUID(str(r['incoming_share_id'])),UUID(str(r['origin_vault_id'])),UUID(str(r['origin_asset_id'])),UUID(str(r['origin_share_id'])),str(r['owner_label']),str(r['asset_type']),str(r['display_title']),r['captured_on'],str(r['state']),r['created_at'],r['updated_at'],int(r['metadata_revision']) if r['metadata_revision'] is not None else None,dict(r['origin_metadata']) if r['origin_metadata'] else None,bool(r['download_allowed'])) for r in cursor.fetchall()]

    def set_local_annotation(self, origin_vault_id: UUID, origin_asset_id: UUID, user_id: UUID, *, note: str | None, alias: str | None, tags: list[str]) -> None:
        if len(tags)>MAX_METADATA_TAGS or any(not isinstance(tag,str) or len(tag)>160 for tag in tags): raise ValueError("Invalid local annotation tags")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT 1 FROM vault_federation_incoming_shares incoming JOIN vaults peer ON peer.vault_id=incoming.origin_vault_id AND peer.is_local=FALSE AND peer.trust_state='trusted' JOIN vault_federation_distribution distribution ON distribution.incoming_share_id=incoming.incoming_share_id AND distribution.revoked_at IS NULL
                WHERE incoming.origin_vault_id=%s AND incoming.origin_asset_id=%s AND incoming.state='active' AND (distribution.target_type='local_all' OR distribution.target_user_id=%s)""", (origin_vault_id,origin_asset_id,user_id))
            if cursor.fetchone() is None: raise ValueError("Incoming federation asset is unavailable")
            cursor.execute("""INSERT INTO vault_federation_local_annotations(origin_vault_id,origin_asset_id,recipient_user_id,note,alias,tags)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(origin_vault_id,origin_asset_id,recipient_user_id) DO UPDATE SET note=EXCLUDED.note,alias=EXCLUDED.alias,tags=EXCLUDED.tags,updated_at=CURRENT_TIMESTAMP""", (origin_vault_id,origin_asset_id,user_id,self._bounded_text(note,2000),self._bounded_text(alias,500),json.dumps(tags)))

    def local_annotation(self, origin_vault_id: UUID, origin_asset_id: UUID, user_id: UUID) -> dict[str, object] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT note,alias,tags FROM vault_federation_local_annotations WHERE origin_vault_id=%s AND origin_asset_id=%s AND recipient_user_id=%s", (origin_vault_id,origin_asset_id,user_id)); row=cursor.fetchone()
            return {"note":row['note'],"alias":row['alias'],"tags":list(row['tags'] or [])} if row else None

    @staticmethod
    def _incoming_collection(row: dict[str, object]) -> IncomingFederatedCollection:
        return IncomingFederatedCollection(UUID(str(row['incoming_collection_id'])),UUID(str(row['origin_vault_id'])),UUID(str(row['origin_collection_id'])),UUID(str(row['origin_collection_share_id'])),str(row['owner_label']),str(row['name']),str(row['description']) if row['description'] else None,str(row['category']),str(row['state']),int(row['lifecycle_revision']),int(row['membership_revision']),int(row['member_count']),row['updated_at'])

    def list_incoming_collections_for_user(self, user_id: UUID) -> list[IncomingFederatedCollection]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT collection.*,count(member.origin_asset_id) AS member_count FROM vault_federation_incoming_collections collection
                JOIN vaults peer ON peer.vault_id=collection.origin_vault_id AND peer.trust_state='trusted'
                LEFT JOIN vault_federation_collection_memberships member ON member.incoming_collection_id=collection.incoming_collection_id
                WHERE collection.state='active' AND EXISTS (SELECT 1 FROM vault_federation_collection_distribution distribution WHERE distribution.incoming_collection_id=collection.incoming_collection_id AND distribution.revoked_at IS NULL AND (distribution.target_type='local_all' OR distribution.target_user_id=%s))
                GROUP BY collection.incoming_collection_id ORDER BY collection.updated_at DESC""",(user_id,))
            return [self._incoming_collection(row) for row in cursor.fetchall()]

    def list_incoming_collections_admin(self) -> list[IncomingFederatedCollection]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT collection.*,count(member.origin_asset_id) AS member_count FROM vault_federation_incoming_collections collection
                LEFT JOIN vault_federation_collection_memberships member ON member.incoming_collection_id=collection.incoming_collection_id
                GROUP BY collection.incoming_collection_id ORDER BY collection.updated_at DESC""")
            return [self._incoming_collection(row) for row in cursor.fetchall()]

    def list_incoming_collection_members(self, incoming_collection_id: UUID, user_id: UUID) -> list[IncomingFederatedShare]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT incoming.*,metadata.revision AS metadata_revision,metadata.snapshot AS origin_metadata
                FROM vault_federation_incoming_collections collection
                JOIN vault_federation_collection_memberships member ON member.incoming_collection_id=collection.incoming_collection_id
                JOIN vault_federation_incoming_shares incoming ON incoming.origin_vault_id=member.origin_vault_id AND incoming.origin_asset_id=member.origin_asset_id AND incoming.origin_share_id=collection.origin_collection_share_id
                JOIN vaults peer ON peer.vault_id=collection.origin_vault_id AND peer.trust_state='trusted'
                LEFT JOIN vault_federation_origin_metadata metadata ON metadata.origin_vault_id=incoming.origin_vault_id AND metadata.origin_asset_id=incoming.origin_asset_id
                WHERE collection.incoming_collection_id=%s AND collection.state='active' AND incoming.state='active'
                  AND EXISTS (SELECT 1 FROM vault_federation_collection_distribution distribution WHERE distribution.incoming_collection_id=collection.incoming_collection_id AND distribution.revoked_at IS NULL AND (distribution.target_type='local_all' OR distribution.target_user_id=%s))
                ORDER BY incoming.display_title,incoming.origin_asset_id""",(incoming_collection_id,user_id))
            return [IncomingFederatedShare(UUID(str(r['incoming_share_id'])),UUID(str(r['origin_vault_id'])),UUID(str(r['origin_asset_id'])),UUID(str(r['origin_share_id'])),str(r['owner_label']),str(r['asset_type']),str(r['display_title']),r['captured_on'],str(r['state']),r['created_at'],r['updated_at'],int(r['metadata_revision']) if r['metadata_revision'] is not None else None,dict(r['origin_metadata']) if r['origin_metadata'] else None,bool(r['download_allowed'])) for r in cursor.fetchall()]

    def set_collection_distribution(self, incoming_collection_id: UUID, admin_user_id: UUID, everyone: bool, user_ids: list[UUID]) -> None:
        if everyone and user_ids or not everyone and not user_ids: raise ValueError("Select Everyone or at least one local user")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM vault_federation_incoming_collections WHERE incoming_collection_id=%s",(incoming_collection_id,))
            if cursor.fetchone() is None: raise ValueError("Incoming federated collection is unavailable")
            cursor.execute("UPDATE vault_federation_collection_distribution SET revoked_at=CURRENT_TIMESTAMP WHERE incoming_collection_id=%s AND revoked_at IS NULL",(incoming_collection_id,))
            for target in ([None] if everyone else user_ids):
                if target is not None:
                    cursor.execute("SELECT 1 FROM auth_accounts WHERE user_id=%s AND active=TRUE",(target,))
                    if cursor.fetchone() is None: raise ValueError("Distribution requires active local users")
                cursor.execute("""INSERT INTO vault_federation_collection_distribution(incoming_collection_id,target_type,target_user_id,target_key,created_by)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(incoming_collection_id,target_type,target_key) DO UPDATE SET revoked_at=NULL,created_by=EXCLUDED.created_by,created_at=CURRENT_TIMESTAMP""",(incoming_collection_id,'local_all' if everyone else 'local_user',target,'*' if target is None else str(target),admin_user_id))
            cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,detail) VALUES(%s,'collection_local_distribution_changed',%s)",(uuid4(),f"collection={incoming_collection_id}"))

    def clear_collection_distribution(self, incoming_collection_id: UUID, admin_user_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_federation_collection_distribution SET revoked_at=CURRENT_TIMESTAMP WHERE incoming_collection_id=%s AND revoked_at IS NULL RETURNING incoming_collection_id",(incoming_collection_id,))
            if cursor.fetchone() is None: raise ValueError("Incoming federated collection is unavailable")
            cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,detail) VALUES(%s,'collection_local_distribution_removed',%s)",(uuid4(),f"collection={incoming_collection_id};admin={admin_user_id}"))

    def set_distribution(self, incoming_share_id: UUID, admin_user_id: UUID, everyone: bool, user_ids: list[UUID]) -> None:
        if everyone and user_ids: raise ValueError("Everyone distribution cannot name users")
        if not everyone and not user_ids: raise ValueError("Select Everyone or at least one local user")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM vault_federation_incoming_shares WHERE incoming_share_id=%s",(incoming_share_id,))
            if cursor.fetchone() is None: raise ValueError("Incoming federation share is unavailable")
            cursor.execute("UPDATE vault_federation_distribution SET revoked_at=CURRENT_TIMESTAMP WHERE incoming_share_id=%s AND revoked_at IS NULL",(incoming_share_id,))
            targets=[None] if everyone else user_ids
            for target in targets:
                if target is not None:
                    cursor.execute("SELECT 1 FROM auth_accounts WHERE user_id=%s AND active=TRUE",(target,))
                    if cursor.fetchone() is None: raise ValueError("Distribution requires active local users")
                cursor.execute("INSERT INTO vault_federation_distribution(incoming_share_id,target_type,target_user_id,created_by) VALUES(%s,%s,%s,%s) ON CONFLICT(incoming_share_id,target_type,target_user_id) DO UPDATE SET revoked_at=NULL,created_by=EXCLUDED.created_by,created_at=CURRENT_TIMESTAMP",(incoming_share_id,'local_all' if everyone else 'local_user',target,admin_user_id))

    def clear_distribution(self, incoming_share_id: UUID, admin_user_id: UUID) -> None:
        """Remove only this Vault's local visibility; the origin share remains intact."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM vault_federation_incoming_shares WHERE incoming_share_id=%s", (incoming_share_id,))
            if cursor.fetchone() is None:
                raise ValueError("Incoming federation share is unavailable")
            cursor.execute("UPDATE vault_federation_distribution SET revoked_at=CURRENT_TIMESTAMP WHERE incoming_share_id=%s AND revoked_at IS NULL", (incoming_share_id,))
            cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,origin_vault_id,target_vault_id,federation_share_id,detail) SELECT %s,'local_distribution_removed',origin_vault_id,%s,origin_share_id,%s FROM vault_federation_incoming_shares WHERE incoming_share_id=%s", (uuid4(),self.local_vault_id(),str(admin_user_id),incoming_share_id))

    def deliver_due(self, limit: int = 20) -> int:
        """Deliver bounded durable events. Failures remain retryable with backoff."""
        self.release_due()
        delivered = 0
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT delivery.delivery_id,delivery.event_id,delivery.event_type,delivery.payload,share.origin_vault_id,share.target_vault_id,peer.endpoint,peer.pairing_key
                FROM vault_federation_deliveries delivery JOIN vault_federation_outgoing_shares share ON share.federation_share_id=delivery.federation_share_id
                JOIN vaults peer ON peer.vault_id=share.target_vault_id WHERE delivery.state='pending' AND delivery.next_attempt_at<=CURRENT_TIMESTAMP
                AND peer.trust_state='trusted' ORDER BY delivery.priority DESC,delivery.created_at LIMIT %s FOR UPDATE SKIP LOCKED""", (limit,))
            rows=cursor.fetchall()
            for row in rows:
                envelope={"protocol_version":FEDERATION_PROTOCOL_VERSION,"event_id":str(row['event_id']),"origin_vault_id":str(row['origin_vault_id']),"target_vault_id":str(row['target_vault_id']),"event_type":str(row['event_type']),"timestamp":datetime.now(UTC).isoformat(),"share":row['payload'] if isinstance(row['payload'],dict) else json.loads(row['payload'])}
                try:
                    request=Request(str(row['endpoint']).rstrip('/')+'/api/vault-master/federation/events',data=canonical_json(envelope),method='POST',headers={'Content-Type':'application/json','X-PV-Federation-Signature':sign_envelope(envelope,str(row['pairing_key']))})
                    with urlopen(request,timeout=10) as response:
                        if response.status < 200 or response.status >= 300: raise ValueError('remote acknowledgement failed')
                    cursor.execute("UPDATE vault_federation_deliveries SET state='acknowledged',acknowledged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=%s",(row['delivery_id'],)); delivered += 1
                except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                    permanent = isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429
                    cursor.execute("UPDATE vault_federation_deliveries SET state=CASE WHEN %s THEN 'failed' ELSE state END,attempts=attempts+1,next_attempt_at=CURRENT_TIMESTAMP + (LEAST(300, 5 * power(2, attempts)::integer) * INTERVAL '1 second'),last_error=%s,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=%s",(permanent,type(error).__name__,row['delivery_id']))
            cursor.execute("""SELECT delivery.delivery_id,delivery.event_id,delivery.event_type,delivery.payload,
                share.origin_vault_id,share.target_vault_id,peer.endpoint,peer.pairing_key
                FROM vault_federation_collection_deliveries delivery
                JOIN vault_federation_outgoing_collection_shares share ON share.federation_collection_share_id=delivery.federation_collection_share_id
                JOIN vaults peer ON peer.vault_id=share.target_vault_id
                WHERE delivery.state='pending' AND delivery.next_attempt_at<=CURRENT_TIMESTAMP AND peer.trust_state='trusted'
                ORDER BY delivery.priority DESC,delivery.created_at LIMIT %s FOR UPDATE SKIP LOCKED""", (limit,))
            for row in cursor.fetchall():
                envelope={"protocol_version":FEDERATION_PROTOCOL_VERSION,"event_id":str(row['event_id']),"origin_vault_id":str(row['origin_vault_id']),"target_vault_id":str(row['target_vault_id']),"event_type":str(row['event_type']),"timestamp":datetime.now(UTC).isoformat(),"share":row['payload'] if isinstance(row['payload'],dict) else json.loads(row['payload'])}
                try:
                    request=Request(str(row['endpoint']).rstrip('/')+'/api/vault-master/federation/events',data=canonical_json(envelope),method='POST',headers={'Content-Type':'application/json','X-PV-Federation-Signature':sign_envelope(envelope,str(row['pairing_key']))})
                    with urlopen(request,timeout=10) as response:
                        if response.status < 200 or response.status >= 300: raise ValueError('remote acknowledgement failed')
                    cursor.execute("UPDATE vault_federation_collection_deliveries SET state='acknowledged',acknowledged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=%s",(row['delivery_id'],)); delivered += 1
                except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                    permanent=isinstance(error,HTTPError) and 400<=error.code<500 and error.code!=429
                    cursor.execute("UPDATE vault_federation_collection_deliveries SET state=CASE WHEN %s THEN 'failed' ELSE state END,attempts=attempts+1,next_attempt_at=CURRENT_TIMESTAMP+(LEAST(300,5*power(2,attempts)::integer)*INTERVAL '1 second'),last_error=%s,updated_at=CURRENT_TIMESTAMP WHERE delivery_id=%s",(permanent,type(error).__name__,row['delivery_id']))
            return delivered

    def retry_stuck_deliveries(self, older_than_seconds: int = 900) -> int:
        """Make durable, retryable transport work eligible again; terminal failures stay terminal."""
        threshold = max(60, min(older_than_seconds, 86_400))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_federation_deliveries SET next_attempt_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE state='pending' AND updated_at<CURRENT_TIMESTAMP-(%s * INTERVAL '1 second') RETURNING delivery_id""", (threshold,))
            count = len(cursor.fetchall())
            cursor.execute("""UPDATE vault_federation_collection_deliveries SET next_attempt_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE state='pending' AND updated_at<CURRENT_TIMESTAMP-(%s * INTERVAL '1 second') RETURNING delivery_id""", (threshold,))
            count += len(cursor.fetchall())
            if count:
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,detail) VALUES(%s,'delivery_retry_recovered',%s)", (uuid4(), f"count={count}"))
            return count

    def reconcile_authoritative_state(self, target_vault_id: UUID | None = None, limit: int = 200) -> int:
        """Re-emit current authoritative lifecycle state for trusted peers.

        This is deliberately a narrow event repair mechanism, not database or
        file synchronization.  Existing receiver rows converge through the
        lifecycle revision checks in ``receive_event``.
        """
        with self._connect() as connection, connection.cursor() as cursor:
            if target_vault_id is not None:
                cursor.execute("SELECT 1 FROM vaults WHERE vault_id=%s AND is_local=FALSE AND trust_state='trusted'", (target_vault_id,))
                if cursor.fetchone() is None:
                    raise ValueError("Trusted paired Vault is unavailable")
            cursor.execute("""SELECT share.federation_share_id FROM vault_federation_outgoing_shares share
                JOIN vaults peer ON peer.vault_id=share.target_vault_id AND peer.trust_state='trusted'
                WHERE share.state IN ('active','revoked') AND (%s::uuid IS NULL OR share.target_vault_id=%s)
                ORDER BY share.updated_at LIMIT %s FOR UPDATE SKIP LOCKED""", (target_vault_id, target_vault_id, limit))
            share_ids = [UUID(str(row['federation_share_id'])) for row in cursor.fetchall()]
            for share_id in share_ids:
                cursor.execute("SELECT state FROM vault_federation_outgoing_shares WHERE federation_share_id=%s", (share_id,))
                state = cursor.fetchone()['state']
                self._queue_event(cursor, share_id, 'share_activated' if state == 'active' else 'share_revoked')
                if state == 'active':
                    self._queue_metadata_event(cursor, share_id, force=True)
            if share_ids:
                cursor.execute("INSERT INTO vault_federation_audit(audit_id,event_type,target_vault_id,detail) VALUES(%s,'reconciliation_queued',%s,%s)", (uuid4(), target_vault_id, f"shares={len(share_ids)}"))
        return len(share_ids) + self.reconcile_collections(target_vault_id, max(1, limit - len(share_ids)))

    def incoming_for_preview(self, incoming_share_id: UUID, user_id: UUID) -> tuple[IncomingFederatedShare, str, str]:
        shares={share.incoming_share_id: share for share in self.list_incoming_for_user(user_id)}
        share=shares.get(incoming_share_id)
        if share is None: raise ValueError('Incoming federation share is unavailable')
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT endpoint,pairing_key FROM vaults WHERE vault_id=%s AND trust_state='trusted'",(share.origin_vault_id,)); peer=cursor.fetchone()
            if peer is None or not peer['endpoint'] or not peer['pairing_key']: raise ValueError('Origin Vault is unavailable')
            return share,str(peer['endpoint']),str(peer['pairing_key'])

    def authorizes_origin_preview(self, origin_share_id: UUID, origin_asset_id: UUID, requester_vault_id: UUID, timestamp: str, signature: str) -> bool:
        try: parsed=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        except ValueError: return False
        if parsed.tzinfo is None or abs((datetime.now(UTC)-parsed).total_seconds()) > MAX_EVENT_AGE_SECONDS: return False
        request={"share_id":str(origin_share_id),"asset_id":str(origin_asset_id),"requester_vault_id":str(requester_vault_id),"timestamp":timestamp}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pairing_key,trust_state FROM vaults WHERE vault_id=%s AND is_local=FALSE",(requester_vault_id,)); peer=cursor.fetchone()
            if peer is None or peer['trust_state']!='trusted' or not peer['pairing_key'] or not verify_envelope(request,signature,str(peer['pairing_key'])): return False
            cursor.execute("""SELECT 1 WHERE EXISTS (
                SELECT 1 FROM vault_federation_outgoing_shares
                WHERE federation_share_id=%s AND origin_asset_id=%s AND target_vault_id=%s AND state='active'
            ) OR EXISTS (
                SELECT 1 FROM vault_federation_outgoing_collection_shares collection_share
                JOIN vault_shared_collections collection ON collection.collection_id=collection_share.origin_collection_id AND collection.archived_at IS NULL
                JOIN vault_shared_collection_members member ON member.collection_id=collection.collection_id
                WHERE collection_share.federation_collection_share_id=%s AND member.asset_id=%s
                  AND collection_share.target_vault_id=%s AND collection_share.state='active'
            )""",(origin_share_id,origin_asset_id,requester_vault_id,origin_share_id,origin_asset_id,requester_vault_id))
            return cursor.fetchone() is not None

    # Content has exactly the same paired-Vault, signed request and active-share
    # boundary as previews.  Identity never grants recipient ownership.
    authorizes_origin_content = authorizes_origin_preview
    incoming_for_content = incoming_for_preview

    def authorizes_origin_download(self, origin_share_id: UUID, origin_asset_id: UUID, requester_vault_id: UUID, timestamp: str, signature: str) -> bool:
        """A download has the normal signed-share boundary plus owner consent."""
        if not self.authorizes_origin_content(origin_share_id, origin_asset_id, requester_vault_id, timestamp, signature):
            return False
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM vault_federation_outgoing_shares WHERE federation_share_id=%s AND origin_asset_id=%s AND target_vault_id=%s AND state='active' AND download_allowed=TRUE",
                (origin_share_id, origin_asset_id, requester_vault_id),
            )
            return cursor.fetchone() is not None

    def cache_entry(self, origin_vault_id: UUID, origin_asset_id: UUID) -> FederatedCacheEntry | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT origin_vault_id,origin_asset_id,state,size_bytes,sha256,updated_at FROM vault_federation_cache_entries WHERE origin_vault_id=%s AND origin_asset_id=%s",(origin_vault_id,origin_asset_id)); row=cursor.fetchone()
            return FederatedCacheEntry(UUID(str(row['origin_vault_id'])),UUID(str(row['origin_asset_id'])),str(row['state']),int(row['size_bytes']) if row['size_bytes'] is not None else None,str(row['sha256']) if row['sha256'] else None,row['updated_at']) if row else None

    def set_cache_state(self, share: IncomingFederatedShare, state: str, size_bytes: int | None = None, sha256: str | None = None) -> None:
        if state not in {'incomplete','complete','invalidated'}: raise ValueError('Invalid cache state')
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_federation_cache_entries(origin_vault_id,origin_asset_id,state,size_bytes,sha256) VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(origin_vault_id,origin_asset_id) DO UPDATE SET state=EXCLUDED.state,size_bytes=EXCLUDED.size_bytes,sha256=EXCLUDED.sha256,updated_at=CURRENT_TIMESTAMP""",(share.origin_vault_id,share.origin_asset_id,state,size_bytes,sha256))

    def begin_cache(self, share: IncomingFederatedShare, size_bytes: int, sha256: str) -> bool:
        """Claim one cache transfer per federated identity; no duplicate files."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_federation_cache_entries(origin_vault_id,origin_asset_id,state,size_bytes,sha256)
              VALUES(%s,%s,'incomplete',%s,%s)
              ON CONFLICT(origin_vault_id,origin_asset_id) DO UPDATE SET state='incomplete',size_bytes=EXCLUDED.size_bytes,sha256=EXCLUDED.sha256,updated_at=CURRENT_TIMESTAMP
              WHERE vault_federation_cache_entries.state='invalidated' RETURNING state""",(share.origin_vault_id,share.origin_asset_id,size_bytes,sha256))
            return cursor.fetchone() is not None

    def set_progress(self, share: IncomingFederatedShare, user_id: UUID, position: float, duration: float | None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_federation_viewer_progress(origin_vault_id,origin_asset_id,recipient_user_id,position_seconds,duration_seconds) VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(origin_vault_id,origin_asset_id,recipient_user_id) DO UPDATE SET position_seconds=EXCLUDED.position_seconds,duration_seconds=EXCLUDED.duration_seconds,updated_at=CURRENT_TIMESTAMP""",(share.origin_vault_id,share.origin_asset_id,user_id,position,duration))
