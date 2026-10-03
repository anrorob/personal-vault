"""Explicit, copy-only reconciliation of legacy terms into UUID-owned private tags.

Dry-run is read-only. Apply requires one immutable owner UUID. Original terms,
decisions, model evidence and assignments are retained as historical evidence.
"""
import argparse
import hashlib
import json
import os
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.gallery_intelligence import custom_tag_identity
from app.gallery_taxonomy import GALLERY_TERM_NAMES

MIGRATION_VERSION = "PV-GALLERY-TAGS-001-v1"


def reconcile_legacy_tags(conninfo: str, *, apply: bool = False, owner_user_id: UUID | None = None) -> dict:
    if apply and owner_user_id is None:
        raise ValueError("Apply requires an explicit immutable owner_user_id")
    report = {"version": MIGRATION_VERSION, "mode": "apply" if apply else "dry-run", "records": []}
    with psycopg.connect(conninfo, row_factory=dict_row) as connection:
        if not apply:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            connection.execute("SELECT pg_advisory_xact_lock(73001001)")
            # Freeze attribution and destination names for this short atomic copy.
            connection.execute("""LOCK TABLE vault_metadata_terms, vault_asset_metadata_decisions,
                vault_asset_metadata_assignments, user_gallery_custom_tags,
                user_gallery_custom_tag_assignments IN SHARE ROW EXCLUSIVE MODE""")
            connection.execute("""CREATE TABLE IF NOT EXISTS user_gallery_legacy_tag_migrations (
                legacy_term_id UUID NOT NULL, owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                private_tag_id UUID NOT NULL, migration_version TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL, evidence JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (legacy_term_id, owner_user_id))""")
        ledger_exists = connection.execute("SELECT to_regclass('user_gallery_legacy_tag_migrations') AS name").fetchone()["name"] is not None
        accounts = {row["user_id"] for row in connection.execute("SELECT user_id FROM auth_accounts")}
        terms = connection.execute("SELECT id,namespace,slug,display_name FROM vault_metadata_terms ORDER BY namespace,slug").fetchall()
        for term in terms:
            if (term["namespace"], term["slug"]) in GALLERY_TERM_NAMES:
                continue
            decisions = connection.execute("""SELECT id,asset_id,decision,decided_by,active FROM vault_asset_metadata_decisions
                WHERE term_id=%s ORDER BY id""", (term["id"],)).fetchall()
            assignments = connection.execute("SELECT id,asset_id,source FROM vault_asset_metadata_assignments WHERE term_id=%s ORDER BY id", (term["id"],)).fetchall()
            record = {"legacy_term_id": str(term["id"]), "name": term["display_name"], "status": "blocked", "owners": []}
            reasons = []
            by_owner = {}
            if term["namespace"] != "content_tag":
                reasons.append("Noncanonical photo type has no supported custom-tag provenance")
            if not decisions:
                reasons.append("No creator/decision UUID evidence; an unassigned term has no attributable owner")
            if assignments:
                reasons.append("Legacy assignment rows lack immutable tag-creator identity; no assignment is guessed")
            for decision in decisions:
                try:
                    user_id = UUID(decision["decided_by"])
                    if user_id not in accounts:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    reasons.append(f"Decision {decision['id']} lacks a verified immutable user UUID")
                    continue
                by_owner.setdefault(user_id, []).append(decision)
            try:
                slug, normalized = custom_tag_identity(term["display_name"])
                if normalized != term["display_name"]:
                    reasons.append("Name requires normalization; preserving exact historical name needs review")
            except ValueError:
                slug = ""
                reasons.append("Historical name does not meet private-tag validation")
            if reasons:
                record["reasons"] = reasons
                report["records"].append(record)
                continue
            record["status"] = "ready"
            for user_id, owned_decisions in sorted(by_owner.items(), key=lambda item: str(item[0])):
                if owner_user_id is not None and user_id != owner_user_id:
                    continue
                evidence = {"term": term, "decisions": owned_decisions, "model_assignments": assignments}
                serialized = json.dumps(evidence, default=str, sort_keys=True)
                fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
                asset_ids = sorted({row["asset_id"] for row in owned_decisions if row["active"] and row["decision"] == "include"}, key=str)
                item = {"owner_user_id": str(user_id), "asset_ids": [str(value) for value in asset_ids], "status": "ready"}
                record["owners"].append(item)
                previous = connection.execute("SELECT * FROM user_gallery_legacy_tag_migrations WHERE legacy_term_id=%s AND owner_user_id=%s", (term["id"], user_id)).fetchone() if ledger_exists else None
                if previous:
                    if previous["source_fingerprint"] != fingerprint:
                        item.update(status="blocked", reason="Legacy evidence changed after migration; explicit reconciliation review required")
                        record["status"] = "blocked"
                    else:
                        item.update(status="already_migrated", private_tag_id=str(previous["private_tag_id"]))
                    continue
                existing = connection.execute("SELECT id,display_name FROM user_gallery_custom_tags WHERE owner_user_id=%s AND slug=%s", (user_id, slug)).fetchone()
                if existing and existing["display_name"] != term["display_name"]:
                    item.update(status="blocked", reason="Private tag slug collides with a differently named user tag")
                    record["status"] = "blocked"
                    continue
                if not apply:
                    continue
                tag_id = existing["id"] if existing else uuid4()
                if not existing:
                    connection.execute("INSERT INTO user_gallery_custom_tags(id,owner_user_id,slug,display_name) VALUES(%s,%s,%s,%s)", (tag_id, user_id, slug, term["display_name"]))
                added_assets = []
                for asset_id in asset_ids:
                    result = connection.execute("""INSERT INTO user_gallery_custom_tag_assignments(tag_id,asset_id,owner_user_id)
                        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""", (tag_id, asset_id, user_id))
                    if result.rowcount:
                        added_assets.append(str(asset_id))
                audit = {**evidence, "created_private_tag": not bool(existing), "added_asset_ids": added_assets}
                connection.execute("""INSERT INTO user_gallery_legacy_tag_migrations
                    (legacy_term_id,owner_user_id,private_tag_id,migration_version,source_fingerprint,evidence)
                    VALUES(%s,%s,%s,%s,%s,%s::jsonb)""", (term["id"], user_id, tag_id, MIGRATION_VERSION, fingerprint, json.dumps(audit, default=str)))
                item.update(status="migrated", private_tag_id=str(tag_id))
            report["records"].append(record)
    report["blocked_records"] = sum(record["status"] == "blocked" for record in report["records"])
    report["migrated_owners"] = sum(owner["status"] == "migrated" for record in report["records"] for owner in record["owners"])
    return report


def main() -> int:
    from app.config import get_database_conninfo
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Copy attributable records; default is read-only dry-run")
    parser.add_argument("--owner-user-id", type=UUID)
    parser.add_argument("--expected-environment", choices=("development", "test", "production"))
    args = parser.parse_args()
    if args.apply and args.owner_user_id is None:
        parser.error("--apply requires --owner-user-id")
    if args.apply and (not args.expected_environment or os.getenv("PV_ENVIRONMENT") != args.expected_environment):
        parser.error("--apply requires --expected-environment matching PV_ENVIRONMENT")
    report = reconcile_legacy_tags(get_database_conninfo(), apply=args.apply, owner_user_id=args.owner_user_id)
    print(json.dumps(report, indent=2))
    return 2 if report["blocked_records"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
