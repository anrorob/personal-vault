"""Server-only administration command for AR-SE-018 permanent deletion."""

from __future__ import annotations

import argparse
from uuid import UUID

from app.auth import AuthenticatedIdentity
from app.auth_store import PostgresAuthenticationStore
from app.config import get_database_conninfo, get_metadata_storage_root
from app.vault_master import PostgresVaultMasterStore
from app.vault_master_api import (
    get_catalogue_preview_roots,
    get_quarantine_retention_days,
    get_quarantine_root,
    run_server_admin_permanent_deletion,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Permanently delete one canonical Vault asset (server administration only).")
    parser.add_argument("--asset-id", type=UUID, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--administrator-user-id", type=UUID, required=True)
    parser.add_argument("--confirm-permanent-deletion", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_permanent_deletion:
        parser.error("--confirm-permanent-deletion is required; no deletion was attempted")

    auth_store = PostgresAuthenticationStore(get_database_conninfo())
    account = auth_store.get_account_by_user_id(args.administrator_user_id)
    if account is None or not account.active or account.role != "administrator":
        parser.error("--administrator-user-id must identify an active administrator")
    store = PostgresVaultMasterStore(
        get_database_conninfo(), sidecar_root=get_metadata_storage_root()
    )
    entry = run_server_admin_permanent_deletion(
        args.asset_id,
        args.reason,
        AuthenticatedIdentity(account),
        store,
        get_catalogue_preview_roots(),
        get_quarantine_root(),
        get_quarantine_retention_days(),
        get_metadata_storage_root(),
        confirmed=True,
    )
    print(f"permanently deleted {entry.asset_id}; audit entry {entry.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
