from dataclasses import dataclass
from datetime import datetime
import json
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


LOCAL_ALL_TARGET = "local_all"
LOCAL_USER_TARGET = "local_user"
REMOTE_VAULT_TARGET = "remote_vault"
PENDING_GRANT_STATE = "pending"
ACTIVE_GRANT_STATE = "active"
REVOKED_GRANT_STATE = "revoked"
QUICK_SHARE_MODE = "quick"
STANDARD_SHARE_MODE = "standard"
STANDARD_SHARE_DELAY_SECONDS = 180
STANDARD_SHARE_ITEM_THRESHOLD = 10

ShareGrantTargetType = Literal["local_all", "local_user", "remote_vault"]
ShareGrantState = Literal["pending", "active", "revoked"]


@dataclass(frozen=True)
class ShareGrant:
    grant_id: UUID
    asset_id: UUID
    grantor_user_id: UUID
    origin_vault_id: UUID
    target_type: ShareGrantTargetType
    target_local_user_id: UUID | None
    target_vault_id: UUID | None
    state: ShareGrantState
    allow_download: bool
    created_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class ShareOperation:
    operation_id: UUID
    grantor_user_id: UUID
    share_mode: Literal["quick", "standard"]
    state: ShareGrantState
    created_at: datetime
    release_at: datetime | None
    activated_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class SharedWithMeAsset:
    asset_id: UUID
    asset_type: str
    display_title: str
    captured_on: object | None
    owner_user_id: UUID
    owner_display_name: str
    origin_vault_id: UUID


@dataclass(frozen=True)
class GallerySharedCollection:
    collection_id: UUID
    name: str
    owner_display_name: str
    included: bool


@dataclass(frozen=True)
class SharedCollection:
    collection_id: UUID
    owner_user_id: UUID
    origin_vault_id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    member_count: int


@dataclass(frozen=True)
class CollectionShareGrant:
    grant_id: UUID
    collection_id: UUID
    operation_id: UUID | None
    grantor_user_id: UUID
    origin_vault_id: UUID
    target_type: ShareGrantTargetType
    target_local_user_id: UUID | None
    state: ShareGrantState
    allow_download: bool
    created_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None


def initialize_share_grants(cursor: psycopg.Cursor) -> None:
    """Install the inert Stage 1D share-grant schema in the current transaction."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_share_grants (
            grant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            asset_id UUID NOT NULL REFERENCES vault_assets(id),
            grantor_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
            target_type TEXT NOT NULL,
            target_local_user_id UUID REFERENCES auth_accounts(user_id),
            target_vault_id UUID REFERENCES vaults(vault_id),
            state TEXT NOT NULL DEFAULT 'pending',
            allow_download BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT vault_share_grants_target_check CHECK (
                (target_type = 'local_all'
                    AND target_local_user_id IS NULL
                    AND target_vault_id IS NULL)
                OR (target_type = 'local_user'
                    AND target_local_user_id IS NOT NULL
                    AND target_vault_id IS NULL)
                OR (target_type = 'remote_vault'
                    AND target_local_user_id IS NULL
                    AND target_vault_id IS NOT NULL)
            ),
            CONSTRAINT vault_share_grants_state_check CHECK (
                (state = 'pending'
                    AND activated_at IS NULL
                    AND revoked_at IS NULL)
                OR (state = 'active'
                    AND activated_at IS NOT NULL
                    AND revoked_at IS NULL)
                OR (state = 'revoked' AND revoked_at IS NOT NULL)
            )
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_share_operations (
            operation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            grantor_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            share_mode TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            release_at TIMESTAMPTZ,
            activated_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT vault_share_operations_mode_check
                CHECK (share_mode IN ('quick', 'standard')),
            CONSTRAINT vault_share_operations_state_check CHECK (
                (state = 'pending' AND activated_at IS NULL AND revoked_at IS NULL)
                OR (state = 'active' AND activated_at IS NOT NULL AND revoked_at IS NULL)
                OR (state = 'revoked' AND revoked_at IS NOT NULL)
            )
        )
        """
    )
    cursor.execute(
        "ALTER TABLE vault_share_grants ADD COLUMN IF NOT EXISTS operation_id UUID "
        "REFERENCES vault_share_operations(operation_id)"
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS vault_share_grants_open_target_idx
        ON vault_share_grants (
            asset_id,
            grantor_user_id,
            target_type,
            COALESCE(target_local_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
            COALESCE(target_vault_id, '00000000-0000-0000-0000-000000000000'::uuid)
        )
        WHERE state IN ('pending', 'active')
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_share_operations_grantor_idx "
        "ON vault_share_operations(grantor_user_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_share_operations_due_idx "
        "ON vault_share_operations(release_at) WHERE state = 'pending'"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_share_grants_operation_idx "
        "ON vault_share_grants(operation_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_share_grants_asset_id_idx "
        "ON vault_share_grants(asset_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_share_grants_grantor_user_id_idx "
        "ON vault_share_grants(grantor_user_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_share_grants_origin_vault_id_idx "
        "ON vault_share_grants(origin_vault_id)"
    )
    migrate_legacy_local_shares(cursor)
    initialize_shared_collections(cursor)


def initialize_shared_collections(cursor: psycopg.Cursor) -> None:
    """Install additive logical collection storage; no Vault file is involved."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_shared_collections (
            collection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
            name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMPTZ
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_shared_collection_members (
            collection_id UUID NOT NULL REFERENCES vault_shared_collections(collection_id),
            asset_id UUID NOT NULL REFERENCES vault_assets(id),
            added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (collection_id, asset_id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_collection_share_grants (
            grant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            collection_id UUID NOT NULL REFERENCES vault_shared_collections(collection_id),
            operation_id UUID REFERENCES vault_share_operations(operation_id),
            grantor_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            origin_vault_id UUID NOT NULL REFERENCES vaults(vault_id),
            target_type TEXT NOT NULL CHECK (target_type IN ('local_all', 'local_user')),
            target_local_user_id UUID REFERENCES auth_accounts(user_id),
            state TEXT NOT NULL DEFAULT 'pending',
            allow_download BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT vault_collection_share_grants_target_check CHECK (
                (target_type = 'local_all' AND target_local_user_id IS NULL)
                OR (target_type = 'local_user' AND target_local_user_id IS NOT NULL)
            ),
            CONSTRAINT vault_collection_share_grants_state_check CHECK (
                (state = 'pending' AND activated_at IS NULL AND revoked_at IS NULL)
                OR (state = 'active' AND activated_at IS NOT NULL AND revoked_at IS NULL)
                OR (state = 'revoked' AND revoked_at IS NOT NULL)
            )
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_shared_collections_owner_idx "
        "ON vault_shared_collections(owner_user_id, created_at DESC)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_gallery_shared_preferences (
            user_id UUID PRIMARY KEY REFERENCES auth_accounts(user_id),
            include_shared_photos BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_gallery_collection_preferences (
            user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            collection_id UUID NOT NULL REFERENCES vault_shared_collections(collection_id),
            included BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, collection_id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_shared_collection_members_asset_idx "
        "ON vault_shared_collection_members(asset_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_collection_share_grants_operation_idx "
        "ON vault_collection_share_grants(operation_id)"
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS vault_collection_share_grants_open_target_idx
        ON vault_collection_share_grants (
            collection_id, grantor_user_id, target_type,
            COALESCE(target_local_user_id, '00000000-0000-0000-0000-000000000000'::uuid)
        ) WHERE state IN ('pending', 'active')
        """
    )
    # Recipient annotations are a separate, local presentation layer over an
    # active local share. They do not copy or modify origin metadata.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_local_gallery_annotations (
            asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE,
            recipient_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            note TEXT,
            tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (asset_id, recipient_user_id),
            CONSTRAINT vault_local_gallery_annotations_note_check
                CHECK (note IS NULL OR length(note) <= 2000),
            CONSTRAINT vault_local_gallery_annotations_tags_check
                CHECK (jsonb_typeof(tags) = 'array')
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_local_gallery_annotation_people (
            asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE,
            recipient_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
            person_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (asset_id, recipient_user_id, person_id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS vault_local_gallery_annotation_people_recipient_idx "
        "ON vault_local_gallery_annotation_people(recipient_user_id, person_id)"
    )


def migrate_legacy_local_shares(cursor: psycopg.Cursor) -> None:
    """Backfill exact legacy recipients as active UUID grants, or fail closed."""
    cursor.execute(
        """
        SELECT asset.id
        FROM vault_assets AS asset
        WHERE asset.visibility = 'shared'
          AND jsonb_typeof(asset.shared_with) <> 'array'
        LIMIT 1
        """
    )
    if cursor.fetchone() is not None:
        raise RuntimeError("Legacy sharing migration found a malformed recipient list")
    cursor.execute(
        """
        SELECT asset.id, recipient.username
        FROM vault_assets AS asset
        CROSS JOIN LATERAL jsonb_array_elements_text(asset.shared_with)
            AS recipient(username)
        LEFT JOIN auth_accounts AS account
            ON account.username = recipient.username AND account.active = TRUE
        WHERE asset.visibility = 'shared'
          AND account.user_id IS NULL
        LIMIT 1
        """
    )
    unresolved = cursor.fetchone()
    if unresolved is not None:
        raise RuntimeError(
            "Legacy sharing migration could not resolve active recipient "
            f"{unresolved['username']!r} for asset {unresolved['id']}"
        )
    cursor.execute(
        """
        INSERT INTO vault_share_grants (
            grant_id, asset_id, grantor_user_id, origin_vault_id,
            target_type, target_local_user_id, state, activated_at
        )
        SELECT
            gen_random_uuid(), asset.id, asset.owner_user_id,
            asset.origin_vault_id, 'local_user', account.user_id,
            'active', CURRENT_TIMESTAMP
        FROM vault_assets AS asset
        CROSS JOIN LATERAL jsonb_array_elements_text(asset.shared_with)
            AS recipient(username)
        JOIN auth_accounts AS account
            ON account.username = recipient.username AND account.active = TRUE
        WHERE asset.visibility = 'shared'
          AND account.user_id <> asset.owner_user_id
          AND NOT EXISTS (
              SELECT 1
              FROM vault_share_grants AS share_grant
              WHERE share_grant.asset_id = asset.id
                AND share_grant.grantor_user_id = asset.owner_user_id
                AND share_grant.target_type = 'local_user'
                AND share_grant.target_local_user_id = account.user_id
                AND share_grant.state IN ('pending', 'active')
          )
        """
    )


def active_user_id(cursor: psycopg.Cursor, username: str) -> UUID | None:
    cursor.execute(
        "SELECT user_id FROM auth_accounts WHERE username = %s AND active = TRUE",
        (username,),
    )
    row = cursor.fetchone()
    return UUID(str(row["user_id"])) if row is not None else None


def visible_asset_ids(
    cursor: psycopg.Cursor,
    user_id: UUID,
    asset_ids: list[UUID],
) -> set[UUID]:
    """Return asset UUIDs authorized by current owner/grant state only."""
    # This request-time evaluator is the Stage 2C background mechanism: a
    # browser need not remain open for a Standard Share to release.
    evaluate_due_share_operations(cursor)
    if not asset_ids:
        return set()
    cursor.execute(
        """
        SELECT asset.id
        FROM vault_assets AS asset
        WHERE asset.id = ANY(%s)
          AND (
              asset.owner_user_id = %s
              OR asset.visibility = 'vault-wide'
              OR EXISTS (
                  SELECT 1
                  FROM vault_share_grants AS share_grant
                  WHERE share_grant.asset_id = asset.id
                    AND share_grant.state = 'active'
                    AND (
                        share_grant.target_type = 'local_all'
                        OR (
                            share_grant.target_type = 'local_user'
                            AND share_grant.target_local_user_id = %s
                      )
                  )
              )
              OR EXISTS (
                  SELECT 1
                  FROM vault_shared_collection_members AS member
                  JOIN vault_collection_share_grants AS collection_grant
                    ON collection_grant.collection_id = member.collection_id
                  WHERE member.asset_id = asset.id
                    AND collection_grant.state = 'active'
                    AND (
                        collection_grant.target_type = 'local_all'
                        OR (
                            collection_grant.target_type = 'local_user'
                            AND collection_grant.target_local_user_id = %s
                        )
                    )
              )
          )
        """,
        (asset_ids, user_id, user_id, user_id),
    )
    return {UUID(str(row["id"])) for row in cursor.fetchall()}


def evaluate_due_share_operations(cursor: psycopg.Cursor) -> None:
    """Activate due Standard Shares from server time; safe to call on every read."""
    cursor.execute(
        """
        UPDATE vault_share_operations
        SET state = 'active', activated_at = CURRENT_TIMESTAMP
        WHERE state = 'pending' AND release_at <= CURRENT_TIMESTAMP
        RETURNING operation_id
        """
    )
    operation_ids = [row["operation_id"] for row in cursor.fetchall()]
    if operation_ids:
        cursor.execute(
            """
            UPDATE vault_share_grants
            SET state = 'active', activated_at = CURRENT_TIMESTAMP
            WHERE operation_id = ANY(%s) AND state = 'pending'
            """,
            (operation_ids,),
        )
        cursor.execute(
            """
            UPDATE vault_collection_share_grants
            SET state = 'active', activated_at = CURRENT_TIMESTAMP
            WHERE operation_id = ANY(%s) AND state = 'pending'
            """,
            (operation_ids,),
        )


def sync_stage2c_local_share_grants(
    cursor: psycopg.Cursor,
    asset_id: UUID,
    grantor_user_id: UUID,
    origin_vault_id: UUID,
    visibility: str,
    shared_with: tuple[str, ...],
    *,
    local_all: bool,
    share_mode: Literal["quick", "standard"],
) -> None:
    """Replace an asset's open local policy through one durable operation.

    Legacy fields remain an auditable compatibility snapshot.  Pending grants
    never authorize access; private revokes every open grant immediately.
    """
    evaluate_due_share_operations(cursor)
    recipients: list[UUID] = []
    if visibility == "shared" and not local_all:
        for username in shared_with:
            recipient = active_user_id(cursor, username)
            if recipient is None or recipient == grantor_user_id:
                raise ValueError("Share recipients must resolve to active non-owner accounts")
            recipients.append(recipient)
    cursor.execute(
        """
        UPDATE vault_share_grants SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP
        WHERE asset_id = %s AND grantor_user_id = %s AND state IN ('pending', 'active')
        RETURNING operation_id
        """,
        (asset_id, grantor_user_id),
    )
    old_operations = {row["operation_id"] for row in cursor.fetchall() if row["operation_id"]}
    if old_operations:
        cursor.execute(
            """
            UPDATE vault_share_operations SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP
            WHERE operation_id = ANY(%s) AND state IN ('pending', 'active')
            """,
            (list(old_operations),),
        )
    if visibility == "private":
        return
    operation_id = uuid4()
    is_quick = share_mode == QUICK_SHARE_MODE
    cursor.execute(
        """
        INSERT INTO vault_share_operations (
            operation_id, grantor_user_id, share_mode, state, release_at, activated_at
        ) VALUES (%s, %s, %s, %s,
            CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') END,
            CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        (operation_id, grantor_user_id, share_mode,
         ACTIVE_GRANT_STATE if is_quick else PENDING_GRANT_STATE, is_quick,
         STANDARD_SHARE_DELAY_SECONDS, is_quick),
    )
    targets = [(LOCAL_ALL_TARGET, None)] if local_all else [(LOCAL_USER_TARGET, user_id) for user_id in recipients]
    for target_type, target_user_id in targets:
        cursor.execute(
            """
            INSERT INTO vault_share_grants (
                grant_id, operation_id, asset_id, grantor_user_id, origin_vault_id,
                target_type, target_local_user_id, state, activated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s = 'active' THEN CURRENT_TIMESTAMP ELSE NULL END)
            """,
            (uuid4(), operation_id, asset_id, grantor_user_id, origin_vault_id,
             target_type, target_user_id,
             ACTIVE_GRANT_STATE if is_quick else PENDING_GRANT_STATE,
             ACTIVE_GRANT_STATE if is_quick else PENDING_GRANT_STATE),
        )


def sync_legacy_local_share_grants(
    cursor: psycopg.Cursor,
    asset_id: UUID,
    grantor_user_id: UUID,
    origin_vault_id: UUID,
    visibility: str,
    shared_with: tuple[str, ...],
    *,
    local_all: bool = False,
) -> None:
    """Keep the temporary legacy editor and live local grants in exact parity."""
    recipients: list[UUID] = []
    if visibility == "shared":
        for username in shared_with:
            recipient = active_user_id(cursor, username)
            if recipient is None:
                raise ValueError(
                    f"Legacy sharing recipient {username!r} is not an active account"
                )
            if recipient == grantor_user_id:
                raise ValueError("The asset owner cannot receive a share grant")
            recipients.append(recipient)
    cursor.execute(
        """
        UPDATE vault_share_grants
        SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP
        WHERE asset_id = %s
          AND grantor_user_id = %s
          AND target_type = 'local_all'
          AND state IN ('pending', 'active')
          AND %s = FALSE
        """,
        (asset_id, grantor_user_id, local_all),
    )
    cursor.execute(
        """
        UPDATE vault_share_grants
        SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP
        WHERE asset_id = %s
          AND grantor_user_id = %s
          AND target_type = 'local_user'
          AND state IN ('pending', 'active')
          AND (%s OR NOT (target_local_user_id = ANY(%s)))
        """,
        (asset_id, grantor_user_id, local_all, recipients),
    )
    if local_all:
        cursor.execute(
            """
            INSERT INTO vault_share_grants (
                grant_id, asset_id, grantor_user_id, origin_vault_id,
                target_type, state, activated_at
            )
            SELECT %s, %s, %s, %s, 'local_all', 'active', CURRENT_TIMESTAMP
            WHERE NOT EXISTS (
                SELECT 1 FROM vault_share_grants
                WHERE asset_id = %s
                  AND grantor_user_id = %s
                  AND target_type = 'local_all'
                  AND state IN ('pending', 'active')
            )
            """,
            (uuid4(), asset_id, grantor_user_id, origin_vault_id, asset_id, grantor_user_id),
        )
    if local_all:
        return
    for recipient in recipients:
        cursor.execute(
            """
            INSERT INTO vault_share_grants (
                grant_id, asset_id, grantor_user_id, origin_vault_id,
                target_type, target_local_user_id, state, activated_at
            )
            SELECT %s, %s, %s, %s, 'local_user', %s, 'active', CURRENT_TIMESTAMP
            WHERE NOT EXISTS (
                SELECT 1 FROM vault_share_grants
                WHERE asset_id = %s
                  AND grantor_user_id = %s
                  AND target_type = 'local_user'
                  AND target_local_user_id = %s
                  AND state IN ('pending', 'active')
            )
            """,
            (
                uuid4(), asset_id, grantor_user_id, origin_vault_id, recipient,
                asset_id, grantor_user_id, recipient,
            ),
        )


def _share_grant_from_row(row: dict[str, object]) -> ShareGrant:
    return ShareGrant(
        grant_id=UUID(str(row["grant_id"])),
        asset_id=UUID(str(row["asset_id"])),
        grantor_user_id=UUID(str(row["grantor_user_id"])),
        origin_vault_id=UUID(str(row["origin_vault_id"])),
        target_type=str(row["target_type"]),  # type: ignore[arg-type]
        target_local_user_id=(
            UUID(str(row["target_local_user_id"]))
            if row["target_local_user_id"] is not None
            else None
        ),
        target_vault_id=(
            UUID(str(row["target_vault_id"]))
            if row["target_vault_id"] is not None
            else None
        ),
        state=str(row["state"]),  # type: ignore[arg-type]
        allow_download=bool(row["allow_download"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
        activated_at=row["activated_at"],  # type: ignore[arg-type]
        revoked_at=row["revoked_at"],  # type: ignore[arg-type]
    )


def _share_operation_from_row(row: dict[str, object]) -> ShareOperation:
    return ShareOperation(
        operation_id=UUID(str(row["operation_id"])),
        grantor_user_id=UUID(str(row["grantor_user_id"])),
        share_mode=str(row["share_mode"]),  # type: ignore[arg-type]
        state=str(row["state"]),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        release_at=row["release_at"],  # type: ignore[arg-type]
        activated_at=row["activated_at"],  # type: ignore[arg-type]
        revoked_at=row["revoked_at"],  # type: ignore[arg-type]
    )


def _collection_from_row(row: dict[str, object]) -> SharedCollection:
    return SharedCollection(
        collection_id=UUID(str(row["collection_id"])),
        owner_user_id=UUID(str(row["owner_user_id"])),
        origin_vault_id=UUID(str(row["origin_vault_id"])),
        name=str(row["name"]),
        description=str(row["description"]) if row["description"] is not None else None,
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        member_count=int(row.get("member_count", 0)),
    )


def _collection_grant_from_row(row: dict[str, object]) -> CollectionShareGrant:
    return CollectionShareGrant(
        grant_id=UUID(str(row["grant_id"])),
        collection_id=UUID(str(row["collection_id"])),
        operation_id=UUID(str(row["operation_id"])) if row["operation_id"] else None,
        grantor_user_id=UUID(str(row["grantor_user_id"])),
        origin_vault_id=UUID(str(row["origin_vault_id"])),
        target_type=str(row["target_type"]),  # type: ignore[arg-type]
        target_local_user_id=(UUID(str(row["target_local_user_id"])) if row["target_local_user_id"] else None),
        state=str(row["state"]),  # type: ignore[arg-type]
        allow_download=bool(row["allow_download"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
        activated_at=row["activated_at"],  # type: ignore[arg-type]
        revoked_at=row["revoked_at"],  # type: ignore[arg-type]
    )


class PostgresShareGrantStore:
    """Durable Stage 1D grant records and request-time access evaluator.

    The additive schema is established by ``PostgresVaultMasterStore`` during
    controlled database bootstrap.  Request handlers construct this evaluator
    for every authoritative access decision, so construction must stay
    read-only: repeating DDL here can deadlock concurrent preview requests.
    """

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    @staticmethod
    def _asset_origin_for_grant(
        cursor: psycopg.Cursor,
        asset_id: UUID,
        grantor_user_id: UUID,
    ) -> UUID:
        cursor.execute(
            """
            SELECT owner_user_id, origin_vault_id
            FROM vault_assets
            WHERE id = %s
            """,
            (asset_id,),
        )
        asset = cursor.fetchone()
        if (
            asset is None
            or asset["owner_user_id"] is None
            or asset["origin_vault_id"] is None
            or UUID(str(asset["owner_user_id"])) != grantor_user_id
        ):
            raise ValueError("Share grants require the asset's immutable owner")
        return UUID(str(asset["origin_vault_id"]))

    @staticmethod
    def _validate_target(
        cursor: psycopg.Cursor,
        target_type: ShareGrantTargetType,
        target_local_user_id: UUID | None,
        target_vault_id: UUID | None,
    ) -> None:
        if target_type == LOCAL_ALL_TARGET:
            if target_local_user_id is not None or target_vault_id is not None:
                raise ValueError("local_all grants cannot name a recipient")
            return
        if target_type == LOCAL_USER_TARGET:
            if target_local_user_id is None or target_vault_id is not None:
                raise ValueError("local_user grants require exactly one local user")
            cursor.execute(
                "SELECT active FROM auth_accounts WHERE user_id = %s",
                (target_local_user_id,),
            )
            account = cursor.fetchone()
            if account is None or not account["active"]:
                raise ValueError("local_user grants require an active local user")
            return
        if target_type == REMOTE_VAULT_TARGET:
            if target_local_user_id is not None or target_vault_id is None:
                raise ValueError("remote_vault grants require exactly one remote Vault")
            cursor.execute(
                "SELECT is_local FROM vaults WHERE vault_id = %s",
                (target_vault_id,),
            )
            vault = cursor.fetchone()
            if vault is None or vault["is_local"]:
                raise ValueError("remote_vault grants require a known remote Vault")
            return
        raise ValueError("Share grant target type is invalid")

    @staticmethod
    def _collection_for_owner(
        cursor: psycopg.Cursor, collection_id: UUID, owner_user_id: UUID
    ) -> dict[str, object]:
        cursor.execute(
            """
            SELECT collection_id, owner_user_id, origin_vault_id, name, description,
                   created_at, updated_at
            FROM vault_shared_collections
            WHERE collection_id = %s AND owner_user_id = %s AND archived_at IS NULL
            FOR UPDATE
            """,
            (collection_id, owner_user_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Only the collection owner can change this collection")
        return row

    @staticmethod
    def _validate_owned_members(
        cursor: psycopg.Cursor,
        asset_ids: list[UUID],
        owner_user_id: UUID,
        origin_vault_id: UUID,
    ) -> None:
        if not asset_ids or len(asset_ids) != len(set(asset_ids)):
            raise ValueError("A collection needs distinct owned assets")
        cursor.execute(
            """
            SELECT id FROM vault_assets
            WHERE id = ANY(%s) AND owner_user_id = %s AND origin_vault_id = %s
            """,
            (asset_ids, owner_user_id, origin_vault_id),
        )
        if {UUID(str(row["id"])) for row in cursor.fetchall()} != set(asset_ids):
            raise ValueError("Collections can contain only the owner's local assets")

    def create_collection(
        self, owner_user_id: UUID, name: str, asset_ids: list[UUID], *, description: str | None = None
    ) -> SharedCollection:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("A collection name is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if not asset_ids or len(asset_ids) != len(set(asset_ids)):
                    raise ValueError("A collection needs distinct owned assets")
                cursor.execute(
                    """
                    SELECT asset.origin_vault_id
                    FROM vault_assets AS asset
                    JOIN vaults AS origin ON origin.vault_id = asset.origin_vault_id
                    WHERE asset.id = ANY(%s) AND asset.owner_user_id = %s AND origin.is_local = TRUE
                    GROUP BY asset.origin_vault_id
                    """,
                    (asset_ids, owner_user_id),
                )
                origins = [row["origin_vault_id"] for row in cursor.fetchall()]
                if len(origins) != 1 or origins[0] is None:
                    raise ValueError("Collections can contain only the owner's local assets")
                origin_vault_id = UUID(str(origins[0]))
                self._validate_owned_members(cursor, asset_ids, owner_user_id, origin_vault_id)
                collection_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_shared_collections (
                        collection_id, owner_user_id, origin_vault_id, name, description
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING collection_id, owner_user_id, origin_vault_id, name, description,
                              created_at, updated_at
                    """,
                    (collection_id, owner_user_id, origin_vault_id, cleaned_name, description.strip() if description else None),
                )
                row = cursor.fetchone()
                cursor.executemany(
                    "INSERT INTO vault_shared_collection_members (collection_id, asset_id) VALUES (%s, %s)",
                    [(collection_id, asset_id) for asset_id in asset_ids],
                )
        return _collection_from_row({**row, "member_count": len(asset_ids)})

    def list_collection_members(self, collection_id: UUID, viewer_user_id: UUID) -> list[UUID]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute(
                    """
                    SELECT member.asset_id
                    FROM vault_shared_collection_members AS member
                    JOIN vault_shared_collections AS collection ON collection.collection_id = member.collection_id
                    WHERE member.collection_id = %s AND collection.archived_at IS NULL
                      AND (collection.owner_user_id = %s OR EXISTS (
                          SELECT 1 FROM vault_collection_share_grants AS collection_grant
                          WHERE collection_grant.collection_id = collection.collection_id AND collection_grant.state = 'active'
                            AND (collection_grant.target_type = 'local_all' OR collection_grant.target_local_user_id = %s)
                      ))
                    ORDER BY member.added_at, member.asset_id
                    """,
                    (collection_id, viewer_user_id, viewer_user_id),
                )
                return [UUID(str(row["asset_id"])) for row in cursor.fetchall()]

    def add_collection_members(
        self, collection_id: UUID, owner_user_id: UUID, asset_ids: list[UUID], *, confirm_live_share: bool = False
    ) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                collection = self._collection_for_owner(cursor, collection_id, owner_user_id)
                self._validate_owned_members(cursor, asset_ids, owner_user_id, UUID(str(collection["origin_vault_id"])))
                cursor.execute(
                    "SELECT 1 FROM vault_collection_share_grants WHERE collection_id = %s AND state = 'active' LIMIT 1",
                    (collection_id,),
                )
                if cursor.fetchone() is not None and not confirm_live_share:
                    raise ValueError("Confirm that adding these assets shares them with active collection recipients")
                cursor.executemany(
                    "INSERT INTO vault_shared_collection_members (collection_id, asset_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    [(collection_id, asset_id) for asset_id in asset_ids],
                )
                cursor.execute("UPDATE vault_shared_collections SET updated_at = CURRENT_TIMESTAMP WHERE collection_id = %s", (collection_id,))

    def remove_collection_member(self, collection_id: UUID, owner_user_id: UUID, asset_id: UUID) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._collection_for_owner(cursor, collection_id, owner_user_id)
                cursor.execute("DELETE FROM vault_shared_collection_members WHERE collection_id = %s AND asset_id = %s", (collection_id, asset_id))
                cursor.execute("UPDATE vault_shared_collections SET updated_at = CURRENT_TIMESTAMP WHERE collection_id = %s", (collection_id,))

    def update_collection(self, collection_id: UUID, owner_user_id: UUID, name: str, description: str | None) -> SharedCollection:
        if not name.strip():
            raise ValueError("A collection name is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._collection_for_owner(cursor, collection_id, owner_user_id)
                cursor.execute(
                    """UPDATE vault_shared_collections SET name = %s, description = %s, updated_at = CURRENT_TIMESTAMP
                       WHERE collection_id = %s RETURNING collection_id, owner_user_id, origin_vault_id, name, description, created_at, updated_at""",
                    (name.strip(), description.strip() if description else None, collection_id),
                )
                row = cursor.fetchone()
                cursor.execute("SELECT COUNT(*)::integer AS member_count FROM vault_shared_collection_members WHERE collection_id = %s", (collection_id,))
                row["member_count"] = cursor.fetchone()["member_count"]
        return _collection_from_row(row)

    def archive_collection(self, collection_id: UUID, owner_user_id: UUID) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._collection_for_owner(cursor, collection_id, owner_user_id)
                cursor.execute("UPDATE vault_collection_share_grants SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP WHERE collection_id = %s AND state IN ('pending', 'active')", (collection_id,))
                cursor.execute("UPDATE vault_shared_collections SET archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE collection_id = %s", (collection_id,))

    def share_collection(
        self, collection_id: UUID, owner_user_id: UUID, target_type: Literal["local_all", "local_user"],
        *, target_local_user_ids: list[UUID] = (), share_mode: Literal["quick", "standard"] = QUICK_SHARE_MODE,
    ) -> ShareOperation:
        if target_type == LOCAL_ALL_TARGET and target_local_user_ids:
            raise ValueError("Everyone sharing cannot name recipients")
        if target_type == LOCAL_USER_TARGET and not target_local_user_ids:
            raise ValueError("Select at least one person")
        if len(target_local_user_ids) != len(set(target_local_user_ids)):
            raise ValueError("Share recipients must be unique")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                collection = self._collection_for_owner(cursor, collection_id, owner_user_id)
                cursor.execute("SELECT 1 FROM vault_shared_collection_members WHERE collection_id = %s LIMIT 1", (collection_id,))
                if cursor.fetchone() is None:
                    raise ValueError("An empty collection cannot be shared")
                if target_type == LOCAL_USER_TARGET:
                    self._validate_target(cursor, LOCAL_USER_TARGET, target_local_user_ids[0], None)
                    for recipient in target_local_user_ids:
                        self._validate_target(cursor, LOCAL_USER_TARGET, recipient, None)
                        if recipient == owner_user_id:
                            raise ValueError("The collection owner cannot receive a share grant")
                is_quick = share_mode == QUICK_SHARE_MODE
                operation_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_share_operations (operation_id, grantor_user_id, share_mode, state, release_at, activated_at)
                    VALUES (%s, %s, %s, %s,
                        CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') END,
                        CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)
                    RETURNING *
                    """,
                    (operation_id, owner_user_id, share_mode, ACTIVE_GRANT_STATE if is_quick else PENDING_GRANT_STATE,
                     is_quick, STANDARD_SHARE_DELAY_SECONDS, is_quick),
                )
                operation = cursor.fetchone()
                targets: list[UUID | None] = [None] if target_type == LOCAL_ALL_TARGET else target_local_user_ids
                for recipient in targets:
                    cursor.execute(
                        """
                        INSERT INTO vault_collection_share_grants (
                            grant_id, collection_id, operation_id, grantor_user_id, origin_vault_id,
                            target_type, target_local_user_id, state, activated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s = 'active' THEN CURRENT_TIMESTAMP ELSE NULL END)
                        """,
                        (uuid4(), collection_id, operation_id, owner_user_id, collection["origin_vault_id"],
                         target_type, recipient, ACTIVE_GRANT_STATE if is_quick else PENDING_GRANT_STATE,
                         ACTIVE_GRANT_STATE if is_quick else PENDING_GRANT_STATE),
                    )
        return _share_operation_from_row(operation)

    def list_shared_collections_for_user(self, user_id: UUID) -> list[SharedCollection]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute(
                    """
                    SELECT collection.collection_id, collection.owner_user_id, collection.origin_vault_id,
                           collection.name, collection.description, collection.created_at, collection.updated_at,
                           COUNT(member.asset_id)::integer AS member_count
                    FROM vault_shared_collections AS collection
                    JOIN vault_collection_share_grants AS collection_grant ON collection_grant.collection_id = collection.collection_id
                    LEFT JOIN vault_shared_collection_members AS member ON member.collection_id = collection.collection_id
                    WHERE collection.archived_at IS NULL AND collection.owner_user_id <> %s
                      AND collection_grant.state = 'active'
                      AND (collection_grant.target_type = 'local_all' OR collection_grant.target_local_user_id = %s)
                    GROUP BY collection.collection_id
                    ORDER BY collection.updated_at DESC, collection.collection_id
                    """,
                    (user_id, user_id),
                )
                return [_collection_from_row(row) for row in cursor.fetchall()]

    def gallery_shared_preference(self, user_id: UUID) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT include_shared_photos FROM user_gallery_shared_preferences WHERE user_id=%s", (user_id,))
                row = cursor.fetchone()
                return bool(row["include_shared_photos"]) if row else False

    def set_gallery_shared_preference(self, user_id: UUID, enabled: bool) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO user_gallery_shared_preferences (user_id, include_shared_photos)
                    VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET include_shared_photos=EXCLUDED.include_shared_photos, updated_at=CURRENT_TIMESTAMP
                    RETURNING include_shared_photos""", (user_id, enabled))
                return bool(cursor.fetchone()["include_shared_photos"])

    def set_gallery_collection_preference(self, user_id: UUID, collection_id: UUID, enabled: bool) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute("""SELECT 1 FROM vault_shared_collections c JOIN vault_collection_share_grants g ON g.collection_id=c.collection_id
                    WHERE c.collection_id=%s AND c.archived_at IS NULL AND c.owner_user_id<>%s AND g.state='active'
                    AND (g.target_type='local_all' OR g.target_local_user_id=%s) LIMIT 1""", (collection_id,user_id,user_id))
                if cursor.fetchone() is None: raise ValueError("Shared collection is unavailable")
                cursor.execute("""INSERT INTO user_gallery_collection_preferences (user_id,collection_id,included) VALUES (%s,%s,%s)
                    ON CONFLICT (user_id,collection_id) DO UPDATE SET included=EXCLUDED.included,updated_at=CURRENT_TIMESTAMP RETURNING included""", (user_id,collection_id,enabled))
                return bool(cursor.fetchone()["included"])

    def included_gallery_assets(self, user_id: UUID) -> dict[UUID, str]:
        """Return only the recipient's opted-in, currently authorized Gallery assets.

        This is deliberately evaluated at read time.  A preference never grants
        access by itself: pending, revoked, archived, and no-longer-member paths
        disappear before the Gallery can present a card or serve its file.
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute("""WITH direct_assets AS (
                    SELECT a.id, owner.display_name AS owner_display_name
                    FROM vault_assets a JOIN vault_share_grants g ON g.asset_id=a.id
                    JOIN auth_accounts owner ON owner.user_id=a.owner_user_id
                    JOIN user_gallery_shared_preferences p ON p.user_id=%s AND p.include_shared_photos=TRUE
                    WHERE lower(a.asset_type)='gallery' AND a.owner_user_id<>%s AND g.state='active'
                      AND (g.target_type='local_all' OR (g.target_type='local_user' AND g.target_local_user_id=%s))
                ), collection_assets AS (
                    SELECT m.asset_id AS id, owner.display_name AS owner_display_name
                    FROM user_gallery_collection_preferences p
                    JOIN vault_shared_collections c ON c.collection_id=p.collection_id AND c.archived_at IS NULL
                    JOIN vault_collection_share_grants g ON g.collection_id=c.collection_id AND g.state='active'
                    JOIN vault_shared_collection_members m ON m.collection_id=c.collection_id
                    JOIN vault_assets a ON a.id=m.asset_id AND lower(a.asset_type)='gallery'
                    JOIN auth_accounts owner ON owner.user_id=a.owner_user_id
                    WHERE p.user_id=%s AND p.included=TRUE AND c.owner_user_id<>%s
                      AND (g.target_type='local_all' OR (g.target_type='local_user' AND g.target_local_user_id=%s))
                ) SELECT DISTINCT ON (id) id, owner_display_name
                  FROM (SELECT * FROM direct_assets UNION ALL SELECT * FROM collection_assets) AS eligible
                  ORDER BY id, owner_display_name""", (user_id,user_id,user_id,user_id,user_id,user_id))
                return {
                    UUID(str(row["id"])): str(row["owner_display_name"])
                    for row in cursor.fetchall()
                }

    def included_gallery_asset_ids(self, user_id: UUID) -> set[UUID]:
        return set(self.included_gallery_assets(user_id))

    def included_gallery_assets_for_local_people(
        self, recipient_user_id: UUID, person_ids: tuple[UUID, ...]
    ) -> set[UUID]:
        """Return opted-in shared Gallery assets explicitly linked to local People.

        Local annotations are not grants.  Their Person links become visible only
        after the ordinary current-share and Gallery inclusion evaluation has
        succeeded, so revocation immediately removes them from People results.
        """
        if not person_ids:
            return set()
        included = self.included_gallery_asset_ids(recipient_user_id)
        if not included:
            return set()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT annotation.asset_id
                    FROM vault_local_gallery_annotation_people AS annotation
                    JOIN vault_people AS person ON person.id = annotation.person_id
                    WHERE annotation.recipient_user_id = %s
                      AND annotation.person_id = ANY(%s)
                      AND person.owner_user_id = %s
                      AND person.active
                    """,
                    (recipient_user_id, list(person_ids), recipient_user_id),
                )
                annotated = {UUID(str(row["asset_id"])) for row in cursor.fetchall()}
        return included & annotated

    @staticmethod
    def _has_active_local_gallery_access(
        cursor: psycopg.Cursor, asset_id: UUID, recipient_user_id: UUID
    ) -> bool:
        """Evaluate current local share access without treating annotations as grants."""
        evaluate_due_share_operations(cursor)
        cursor.execute(
            """
            SELECT 1
            FROM vault_assets AS asset
            WHERE asset.id = %s
              AND lower(asset.asset_type) = 'gallery'
              AND asset.owner_user_id <> %s
              AND (
                  EXISTS (
                      SELECT 1 FROM vault_share_grants AS share_grant
                      WHERE share_grant.asset_id = asset.id AND share_grant.state = 'active'
                        AND (share_grant.target_type = 'local_all'
                             OR (share_grant.target_type = 'local_user'
                                 AND share_grant.target_local_user_id = %s))
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM vault_shared_collection_members AS member
                      JOIN vault_shared_collections AS collection
                        ON collection.collection_id = member.collection_id
                       AND collection.archived_at IS NULL
                      JOIN vault_collection_share_grants AS collection_grant
                        ON collection_grant.collection_id = collection.collection_id
                       AND collection_grant.state = 'active'
                      WHERE member.asset_id = asset.id
                        AND (collection_grant.target_type = 'local_all'
                             OR (collection_grant.target_type = 'local_user'
                                 AND collection_grant.target_local_user_id = %s))
                  )
              )
            """,
            (asset_id, recipient_user_id, recipient_user_id, recipient_user_id),
        )
        return cursor.fetchone() is not None

    def get_local_gallery_annotation(
        self, asset_id: UUID, recipient_user_id: UUID
    ) -> dict[str, object] | None:
        """Return only a recipient's own annotation while the share remains active."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if not self._has_active_local_gallery_access(cursor, asset_id, recipient_user_id):
                    return None
                cursor.execute(
                    """
                    SELECT note, tags FROM vault_local_gallery_annotations
                    WHERE asset_id = %s AND recipient_user_id = %s
                    """,
                    (asset_id, recipient_user_id),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT person.id, person.display_name
                    FROM vault_local_gallery_annotation_people AS annotation
                    JOIN vault_people AS person ON person.id = annotation.person_id
                    WHERE annotation.asset_id = %s
                      AND annotation.recipient_user_id = %s
                      AND person.owner_user_id = %s AND person.active
                    ORDER BY lower(person.display_name), person.id
                    """,
                    (asset_id, recipient_user_id, recipient_user_id),
                )
                people = [
                    {"id": str(person["id"]), "display_name": str(person["display_name"])}
                    for person in cursor.fetchall()
                ]
        return {
            "note": str(row["note"]) if row and row["note"] is not None else None,
            "tags": list(row["tags"] or []) if row else [],
            "people": people,
        }

    def set_local_gallery_annotation(
        self,
        asset_id: UUID,
        recipient_user_id: UUID,
        *,
        note: str | None,
        tags: list[str],
        person_ids: list[UUID],
    ) -> dict[str, object]:
        """Replace one recipient layer after current grant and Person checks."""
        cleaned_note = note.strip() if isinstance(note, str) else None
        cleaned_note = cleaned_note or None
        cleaned_tags = [tag.strip() for tag in tags]
        if (
            len(cleaned_tags) > 64
            or any(not tag or len(tag) > 160 for tag in cleaned_tags)
            or len(set(cleaned_tags)) != len(cleaned_tags)
            or len(person_ids) != len(set(person_ids))
        ):
            raise ValueError("Local Gallery annotation is invalid")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if not self._has_active_local_gallery_access(cursor, asset_id, recipient_user_id):
                    raise ValueError("Shared Gallery photo is unavailable")
                if person_ids:
                    cursor.execute(
                        "SELECT id FROM vault_people WHERE owner_user_id = %s AND active AND id = ANY(%s)",
                        (recipient_user_id, person_ids),
                    )
                    if {UUID(str(row["id"])) for row in cursor.fetchall()} != set(person_ids):
                        raise ValueError("Local Person was not found")
                cursor.execute(
                    """
                    INSERT INTO vault_local_gallery_annotations
                        (asset_id, recipient_user_id, note, tags)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (asset_id, recipient_user_id) DO UPDATE
                    SET note = EXCLUDED.note, tags = EXCLUDED.tags,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (asset_id, recipient_user_id, cleaned_note, json.dumps(cleaned_tags)),
                )
                cursor.execute(
                    "DELETE FROM vault_local_gallery_annotation_people "
                    "WHERE asset_id = %s AND recipient_user_id = %s",
                    (asset_id, recipient_user_id),
                )
                if person_ids:
                    cursor.executemany(
                        """INSERT INTO vault_local_gallery_annotation_people
                            (asset_id, recipient_user_id, person_id)
                            VALUES (%s, %s, %s)""",
                        [(asset_id, recipient_user_id, person_id) for person_id in person_ids],
                    )
        value = self.get_local_gallery_annotation(asset_id, recipient_user_id)
        if value is None:
            raise ValueError("Shared Gallery photo is unavailable")
        return value

    def list_gallery_shared_collections(self, user_id: UUID) -> list[GallerySharedCollection]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute("""
                    SELECT DISTINCT ON (collection.collection_id)
                        collection.collection_id, collection.name,
                        owner.display_name AS owner_display_name,
                        COALESCE(preference.included, FALSE) AS included
                    FROM vault_shared_collections AS collection
                    JOIN vault_collection_share_grants AS collection_grant
                      ON collection_grant.collection_id = collection.collection_id AND collection_grant.state = 'active'
                    JOIN vault_shared_collection_members AS member
                      ON member.collection_id = collection.collection_id
                    JOIN vault_assets AS asset
                      ON asset.id = member.asset_id AND lower(asset.asset_type) = 'gallery'
                    JOIN auth_accounts AS owner ON owner.user_id = collection.owner_user_id
                    LEFT JOIN user_gallery_collection_preferences AS preference
                      ON preference.collection_id = collection.collection_id AND preference.user_id = %s
                    WHERE collection.archived_at IS NULL
                      AND collection.owner_user_id <> %s
                      AND (collection_grant.target_type = 'local_all'
                           OR (collection_grant.target_type = 'local_user' AND collection_grant.target_local_user_id = %s))
                    ORDER BY collection.collection_id, collection.created_at DESC
                """, (user_id, user_id, user_id))
                return [
                    GallerySharedCollection(
                        collection_id=UUID(str(row["collection_id"])),
                        name=str(row["name"]),
                        owner_display_name=str(row["owner_display_name"]),
                        included=bool(row["included"]),
                    )
                    for row in cursor.fetchall()
                ]

    def list_outgoing_collection_operations(
        self, owner_user_id: UUID
    ) -> list[tuple[ShareOperation, SharedCollection, list[CollectionShareGrant]]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute(
                    """
                    SELECT operation.*, collection.collection_id, collection.owner_user_id,
                           collection.origin_vault_id, collection.name, collection.description,
                           collection.created_at AS collection_created_at,
                           collection.updated_at, COUNT(member.asset_id)::integer AS member_count
                    FROM vault_share_operations AS operation
                    JOIN vault_collection_share_grants AS collection_grant ON collection_grant.operation_id = operation.operation_id
                    JOIN vault_shared_collections AS collection ON collection.collection_id = collection_grant.collection_id
                    LEFT JOIN vault_shared_collection_members AS member ON member.collection_id = collection.collection_id
                    WHERE operation.grantor_user_id = %s
                    GROUP BY operation.operation_id, collection.collection_id
                    ORDER BY operation.created_at DESC, operation.operation_id DESC
                    """,
                    (owner_user_id,),
                )
                rows = cursor.fetchall()
                if not rows:
                    return []
                operation_ids = [row["operation_id"] for row in rows]
                cursor.execute(
                    "SELECT * FROM vault_collection_share_grants WHERE operation_id = ANY(%s) ORDER BY created_at, grant_id",
                    (operation_ids,),
                )
                grants_by_operation: dict[UUID, list[CollectionShareGrant]] = {UUID(str(row["operation_id"])): [] for row in rows}
                for grant_row in cursor.fetchall():
                    grant = _collection_grant_from_row(grant_row)
                    if grant.operation_id is not None:
                        grants_by_operation[grant.operation_id].append(grant)
                result: list[tuple[ShareOperation, SharedCollection, list[CollectionShareGrant]]] = []
                for row in rows:
                    collection_row = {
                        **row,
                        "created_at": row["collection_created_at"],
                    }
                    operation = _share_operation_from_row(row)
                    result.append((operation, _collection_from_row(collection_row), grants_by_operation[operation.operation_id]))
                return result

    def create_grant(
        self,
        asset_id: UUID,
        grantor_user_id: UUID,
        target_type: ShareGrantTargetType,
        *,
        target_local_user_id: UUID | None = None,
        target_vault_id: UUID | None = None,
        state: Literal["pending", "active"] = PENDING_GRANT_STATE,
        allow_download: bool = False,
    ) -> ShareGrant:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    origin_vault_id = self._asset_origin_for_grant(
                        cursor, asset_id, grantor_user_id
                    )
                    self._validate_target(
                        cursor, target_type, target_local_user_id, target_vault_id
                    )
                    if target_local_user_id == grantor_user_id:
                        raise ValueError(
                            "The asset owner cannot receive a share grant"
                        )
                    grant_id = uuid4()
                    cursor.execute(
                        """
                        INSERT INTO vault_share_grants (
                            grant_id, asset_id, grantor_user_id, origin_vault_id,
                            target_type, target_local_user_id, target_vault_id,
                            state, allow_download, activated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s = 'active'
                                THEN CURRENT_TIMESTAMP ELSE NULL END)
                        RETURNING *
                        """,
                        (
                            grant_id,
                            asset_id,
                            grantor_user_id,
                            origin_vault_id,
                            target_type,
                            target_local_user_id,
                            target_vault_id,
                            state,
                            allow_download,
                            state,
                        ),
                    )
                    return _share_grant_from_row(cursor.fetchone())
        except psycopg.errors.UniqueViolation as error:
            raise ValueError(
                "An open share grant already exists for this target"
            ) from error

    def get_grant(self, grant_id: UUID) -> ShareGrant | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM vault_share_grants WHERE grant_id = %s",
                    (grant_id,),
                )
                row = cursor.fetchone()
        return _share_grant_from_row(row) if row is not None else None

    def list_outgoing_grants(self, grantor_user_id: UUID) -> list[ShareGrant]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM vault_share_grants
                    WHERE grantor_user_id = %s
                    ORDER BY created_at, grant_id
                    """,
                    (grantor_user_id,),
                )
                return [_share_grant_from_row(row) for row in cursor.fetchall()]

    def list_assets_shared_with_user(
        self, user_id: UUID, asset_types: tuple[str, ...] = ()
    ) -> list[SharedWithMeAsset]:
        """List active direct or collection access for a non-owner recipient.

        Vault Commons is the authoritative shared-content catalogue.  Its
        membership must therefore use the same active local access paths as
        product views, without depending on a Gallery inclusion preference.
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute(
                    """
                    SELECT DISTINCT ON (asset.id)
                        asset.id AS asset_id, asset.asset_type, asset.display_title,
                        asset.captured_on, asset.owner_user_id, owner.display_name AS owner_display_name,
                        asset.origin_vault_id
                    FROM vault_assets AS asset
                    JOIN auth_accounts AS owner ON owner.user_id = asset.owner_user_id
                    WHERE asset.owner_user_id <> %s
                      AND (
                          EXISTS (
                              SELECT 1 FROM vault_share_grants AS share_grant
                              WHERE share_grant.asset_id = asset.id
                                AND share_grant.state = 'active'
                                AND (share_grant.target_type = 'local_all'
                                     OR (share_grant.target_type = 'local_user' AND share_grant.target_local_user_id = %s))
                          )
                          OR EXISTS (
                              SELECT 1 FROM vault_shared_collection_members AS member
                              JOIN vault_shared_collections AS collection
                                ON collection.collection_id = member.collection_id
                               AND collection.archived_at IS NULL
                              JOIN vault_collection_share_grants AS collection_grant
                                ON collection_grant.collection_id = collection.collection_id
                              WHERE member.asset_id = asset.id
                                AND collection_grant.state = 'active'
                                AND (collection_grant.target_type = 'local_all'
                                     OR (collection_grant.target_type = 'local_user' AND collection_grant.target_local_user_id = %s))
                          )
                      )
                      AND (%s = '{}'::text[] OR lower(asset.asset_type) = ANY(%s))
                    ORDER BY asset.id, asset.captured_on DESC NULLS LAST, asset.display_title
                    """,
                    (user_id, user_id, user_id, list(asset_types), list(asset_types)),
                )
                return [
                    SharedWithMeAsset(
                        asset_id=UUID(str(row["asset_id"])),
                        asset_type=str(row["asset_type"]),
                        display_title=str(row["display_title"]),
                        captured_on=row["captured_on"],
                        owner_user_id=UUID(str(row["owner_user_id"])),
                        owner_display_name=str(row["owner_display_name"]),
                        origin_vault_id=UUID(str(row["origin_vault_id"])),
                    )
                    for row in cursor.fetchall()
                ]

    def list_outgoing_operations(self, grantor_user_id: UUID) -> list[tuple[ShareOperation, list[ShareGrant]]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                cursor.execute(
                    "SELECT * FROM vault_share_operations WHERE grantor_user_id = %s ORDER BY created_at DESC, operation_id DESC",
                    (grantor_user_id,),
                )
                operations = [_share_operation_from_row(row) for row in cursor.fetchall()]
                if not operations:
                    return []
                cursor.execute(
                    "SELECT * FROM vault_share_grants WHERE operation_id = ANY(%s) ORDER BY created_at, grant_id",
                    ([operation.operation_id for operation in operations],),
                )
                grouped: dict[UUID, list[ShareGrant]] = {operation.operation_id: [] for operation in operations}
                for row in cursor.fetchall():
                    grant = _share_grant_from_row(row)
                    if row["operation_id"] is not None:
                        grouped[UUID(str(row["operation_id"]))].append(grant)
                return [(operation, grouped[operation.operation_id]) for operation in operations]

    def transition_operation(self, operation_id: UUID, grantor_user_id: UUID, action: Literal["activate", "revoke"]) -> ShareOperation:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                evaluate_due_share_operations(cursor)
                if action == "activate":
                    cursor.execute(
                        """
                        UPDATE vault_share_operations SET state = 'active', activated_at = CURRENT_TIMESTAMP
                        WHERE operation_id = %s AND grantor_user_id = %s AND state = 'pending'
                        RETURNING *
                        """,
                        (operation_id, grantor_user_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("Only the owner can release a pending share once")
                    cursor.execute(
                        "UPDATE vault_share_grants SET state = 'active', activated_at = CURRENT_TIMESTAMP WHERE operation_id = %s AND state = 'pending'",
                        (operation_id,),
                    )
                    cursor.execute(
                        "UPDATE vault_collection_share_grants SET state = 'active', activated_at = CURRENT_TIMESTAMP WHERE operation_id = %s AND state = 'pending'",
                        (operation_id,),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE vault_share_operations SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP
                        WHERE operation_id = %s AND grantor_user_id = %s AND state IN ('pending', 'active')
                        RETURNING *
                        """,
                        (operation_id, grantor_user_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("Only the owner can cancel or revoke an open share once")
                    cursor.execute(
                        "UPDATE vault_share_grants SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP WHERE operation_id = %s AND state IN ('pending', 'active')",
                        (operation_id,),
                    )
                    cursor.execute(
                        "UPDATE vault_collection_share_grants SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP WHERE operation_id = %s AND state IN ('pending', 'active')",
                        (operation_id,),
                    )
        return _share_operation_from_row(row)

    def activate_grant(self, grant_id: UUID, grantor_user_id: UUID) -> ShareGrant:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_share_grants
                    SET state = 'active', activated_at = CURRENT_TIMESTAMP
                    WHERE grant_id = %s
                      AND grantor_user_id = %s
                      AND state = 'pending'
                    RETURNING *
                    """,
                    (grant_id, grantor_user_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError(
                "Only the grantor can activate a pending share grant"
            )
        return _share_grant_from_row(row)

    def revoke_grant(self, grant_id: UUID, grantor_user_id: UUID) -> ShareGrant:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_share_grants
                    SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP
                    WHERE grant_id = %s
                      AND grantor_user_id = %s
                      AND state IN ('pending', 'active')
                    RETURNING *
                    """,
                    (grant_id, grantor_user_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError("Only the grantor can revoke an open share grant")
        return _share_grant_from_row(row)
