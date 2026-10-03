"""Explicit deletion of three user-authorized obsolete Development metadata terms.

Never runs during startup. No canonical content, private annotation, or generated
term is deleted. Historical migration/deletion audit evidence remains retained.
"""
import argparse
import json
import os
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.gallery_taxonomy import GALLERY_TERM_NAMES

VERSION = "PV-HOME-VIDEOS-CLEANUP-001-delete-legacy-v1"
RELATIONS = ("vault_asset_metadata_assignments", "vault_asset_metadata_decisions")


def cleanup(conninfo, *, targets, copied_term, apply=False):
    # Runtime identifiers are explicit operator inputs, never release constants.
    if (len(targets) != 3 or copied_term not in targets
            or any(not isinstance(key, UUID) or not isinstance(value, str)
                   or not value.strip() for key, value in targets.items())):
        raise ValueError("Exactly three explicit targets and their copied term are required")
    if apply and os.getenv("PV_ENVIRONMENT") not in ("development", "test"):
        raise ValueError("This cleanup may apply only in Development or synthetic tests")
    with psycopg.connect(conninfo, row_factory=dict_row) as c:
        if not apply:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            c.execute("SET LOCAL lock_timeout = '10s'")
            c.execute("SELECT pg_advisory_xact_lock(73001001)")
            c.execute("""LOCK TABLE vault_metadata_terms, vault_asset_metadata_assignments,
                vault_asset_metadata_decisions, vault_gallery_intelligence_concept_terms,
                user_gallery_custom_tags, user_gallery_custom_tag_assignments,
                user_gallery_legacy_tag_migrations IN SHARE ROW EXCLUSIVE MODE""")
        # Never let an unknown cascading relationship broaden this cleanup.
        foreign_keys = c.execute("""SELECT conrelid::regclass::text AS relation,confdeltype
            FROM pg_constraint WHERE confrelid='vault_metadata_terms'::regclass""").fetchall()
        if any(row['relation'] not in (*RELATIONS, 'vault_gallery_intelligence_concept_terms')
               or row['confdeltype'] != 'a' for row in foreign_keys):
            raise ValueError("Unexpected legacy-term relationship; cleanup scope needs review")
        ids = list(targets)
        terms = c.execute("SELECT id,namespace,slug,display_name FROM vault_metadata_terms WHERE id=ANY(%s) ORDER BY id", (ids,)).fetchall()
        for term in terms:
            if (term['namespace'] != 'content_tag' or term['slug'] != targets[term['id']]
                    or (term['namespace'],term['slug']) in GALLERY_TERM_NAMES):
                raise ValueError("Target identity changed or is an official term")
        if c.execute("SELECT 1 FROM vault_gallery_intelligence_concept_terms WHERE term_id=ANY(%s)",(ids,)).fetchone():
            raise ValueError("Target is used by generated/system concept mappings")
        copies = c.execute("""SELECT m.private_tag_id,m.owner_user_id,t.display_name
            FROM user_gallery_legacy_tag_migrations m JOIN user_gallery_custom_tags t
            ON t.id=m.private_tag_id AND t.owner_user_id=m.owner_user_id
            WHERE m.legacy_term_id=%s""",(copied_term,)).fetchall()
        if len(copies) != 1:
            raise ValueError("The existing migrated private copy must remain valid")
        private_assignments = c.execute("""SELECT tag_id,asset_id,owner_user_id
            FROM user_gallery_custom_tag_assignments WHERE tag_id=%s ORDER BY asset_id""",
            (copies[0]['private_tag_id'],)).fetchall()
        if not private_assignments or any(row['owner_user_id'] != copies[0]['owner_user_id'] for row in private_assignments):
            raise ValueError("The migrated private assignments must remain valid")
        relationships = {
            table: c.execute(f"SELECT * FROM {table} WHERE term_id=ANY(%s) ORDER BY id",(ids,)).fetchall()
            for table in RELATIONS  # Fixed identifiers, never caller input.
        }
        report = {'version':VERSION,'mode':'apply' if apply else 'dry-run',
            'terms':len(terms),'relationships':{table:len(rows) for table,rows in relationships.items()},
            'private_copy_preserved':str(copies[0]['private_tag_id']),
            'private_assignments_preserved':len(private_assignments)}
        if apply and (terms or any(relationships.values())):
            c.execute("""CREATE TABLE IF NOT EXISTS home_video_legacy_tag_cleanup_audit (
                version TEXT PRIMARY KEY, evidence JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            evidence = json.loads(json.dumps({'terms':terms,'relationships':relationships,
                'private_copy':copies,'private_assignments':private_assignments},default=str))
            # A changed/recreated legacy source after a completed cleanup needs review.
            c.execute("INSERT INTO home_video_legacy_tag_cleanup_audit(version,evidence) VALUES(%s,%s)",(VERSION,Jsonb(evidence)))
            for table in RELATIONS:
                c.execute(f"DELETE FROM {table} WHERE term_id=ANY(%s)",(ids,))
            c.execute("DELETE FROM vault_metadata_terms WHERE id=ANY(%s)",(ids,))
        report['remaining_terms'] = c.execute("SELECT count(*) AS n FROM vault_metadata_terms WHERE id=ANY(%s)",(ids,)).fetchone()['n']
        return report


def main():
    from app.config import get_database_conninfo
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Delete the exact three authorized legacy records')
    parser.add_argument('--targets-file', required=True, help='External private JSON manifest; never commit it')
    args=parser.parse_args()
    with open(args.targets_file, encoding='utf-8') as source:
        manifest = json.load(source)
    targets = {UUID(key): value for key, value in manifest['targets'].items()}
    copied_term = UUID(manifest['copied_term'])
    print(json.dumps(cleanup(get_database_conninfo(), targets=targets,
                             copied_term=copied_term, apply=args.apply),indent=2))


if __name__ == '__main__':
    main()
