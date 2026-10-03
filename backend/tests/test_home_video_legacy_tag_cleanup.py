"""Explicit obsolete metadata deletion, using disposable PostgreSQL only."""
from uuid import UUID, uuid4
import psycopg
import pytest
from app.home_video_legacy_tag_cleanup import cleanup as cleanup_explicit, VERSION

# Deliberately synthetic identities; never import a runtime cleanup target.
TARGETS = {UUID(int=101): "example-legacy-a", UUID(int=102): "example-legacy-b",
           UUID(int=103): "example-private-tag"}
COPIED_TERM = UUID(int=103)


def cleanup(conninfo, *, apply=False):
    return cleanup_explicit(conninfo, targets=TARGETS, copied_term=COPIED_TERM, apply=apply)

from app.gallery_tag_migration import reconcile_legacy_tags
from app.home_video_tags import system_terms, effective_system_tags
from tests.test_gallery_tag_migration import tag_database


@pytest.fixture
def obsolete(tag_database, monkeypatch):
    monkeypatch.setenv('PV_ENVIRONMENT','test')
    conninfo,intelligence,private,owner,second,asset,another=tag_database
    intelligence.create_custom_tag(asset,owner,'Example Private Tag')
    with psycopg.connect(conninfo) as c:
        old=c.execute("SELECT id FROM vault_metadata_terms WHERE slug='example-private-tag'").fetchone()[0]
        # Set audited identity before creating the migration ledger.
        c.execute('DELETE FROM vault_asset_metadata_decisions WHERE term_id=%s',(old,))
        c.execute('UPDATE vault_metadata_terms SET id=%s WHERE id=%s',(COPIED_TERM,old))
    intelligence.create_custom_tag(asset,owner,'Example Private Tag')
    assert reconcile_legacy_tags(conninfo,apply=True,owner_user_id=owner)['migrated_owners']==1
    tag=private.list(owner)[0]
    with psycopg.connect(conninfo) as c:
        for term_id,slug in TARGETS.items():
            if term_id==COPIED_TERM: continue
            c.execute("INSERT INTO vault_metadata_terms(id,namespace,slug,display_name) VALUES(%s,'content_tag',%s,%s)",(term_id,slug,slug))
            c.execute("INSERT INTO vault_asset_metadata_assignments(id,asset_id,term_id,source) VALUES(%s,%s,%s,'user')",(uuid4(),another,term_id))
    intelligence.persist_canonical_assignments(asset,(('content_tag','beach'),),model_id='synthetic',model_revision=None,task_version='test')
    return conninfo,intelligence,private,owner,asset,tag


def test_deletes_rows_relationships_preserves_private_system_and_is_idempotent(obsolete):
    conninfo,intelligence,private,owner,asset,tag=obsolete
    before=private.for_asset(owner,asset)
    assert cleanup(conninfo)['terms']==3
    first=cleanup(conninfo,apply=True)
    assert first['terms']==3 and first['remaining_terms']==0
    with psycopg.connect(conninfo) as c:
        for table,column in [('vault_metadata_terms','id'),('vault_asset_metadata_assignments','term_id'),('vault_asset_metadata_decisions','term_id'),('vault_gallery_intelligence_concept_terms','term_id')]:
            assert c.execute(f'SELECT count(*) FROM {table} WHERE {column}=ANY(%s)',(list(TARGETS),)).fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM vault_assets').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM home_video_legacy_tag_cleanup_audit').fetchone()[0]==1
    assert private.for_asset(owner,asset)==before and private.list(owner)==[tag]
    assert [t['slug'] for t in effective_system_tags(intelligence,asset)]==['beach']
    second=cleanup(conninfo,apply=True)
    assert second['terms']==0 and not any(second['relationships'].values())
    intelligence.initialize()
    assert cleanup(conninfo)['remaining_terms']==0
    assert not set(TARGETS.values()) & {t['slug'] for t in intelligence.list_terms()}
    assert not set(TARGETS.values()) & {t['slug'] for t in system_terms()}
    assert reconcile_legacy_tags(conninfo)['records']==[]


def test_failure_rolls_back_all_deletes_and_audit(obsolete):
    conninfo,*_=obsolete
    with psycopg.connect(conninfo) as c:
        c.execute("""CREATE FUNCTION deny_test_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic failure'; END $$;
            CREATE TRIGGER deny_test_delete BEFORE DELETE ON vault_metadata_terms
            FOR EACH ROW EXECUTE FUNCTION deny_test_delete()""")
    with pytest.raises(psycopg.Error,match='synthetic failure'): cleanup(conninfo,apply=True)
    assert cleanup(conninfo)['terms']==3
    with psycopg.connect(conninfo) as c:
        assert c.execute("SELECT to_regclass('home_video_legacy_tag_cleanup_audit')").fetchone()[0] is None
        assert c.execute('SELECT count(*) FROM vault_asset_metadata_decisions WHERE term_id=%s',(COPIED_TERM,)).fetchone()[0]==1


def test_changed_or_system_target_fails_closed(obsolete):
    conninfo,*_=obsolete
    with psycopg.connect(conninfo) as c:
        c.execute("UPDATE vault_metadata_terms SET slug='changed' WHERE id=%s",(COPIED_TERM,))
    with pytest.raises(ValueError,match='identity changed'): cleanup(conninfo,apply=True)
    with psycopg.connect(conninfo) as c:
        assert c.execute('SELECT count(*) FROM vault_metadata_terms WHERE id=ANY(%s)',(list(TARGETS),)).fetchone()[0]==3


def test_missing_private_copy_and_production_apply_are_rejected(obsolete,monkeypatch):
    conninfo,_,private,owner,_,tag=obsolete
    monkeypatch.setenv('PV_ENVIRONMENT','production')
    with pytest.raises(ValueError,match='only in Development'): cleanup(conninfo,apply=True)
    monkeypatch.setenv('PV_ENVIRONMENT','test')
    private.delete(owner,tag.id)
    with pytest.raises(ValueError,match='private copy'): cleanup(conninfo,apply=True)


def test_generated_mapping_or_unexpected_cascade_is_not_deleted(obsolete):
    conninfo,*_=obsolete
    with psycopg.connect(conninfo) as c:
        concept=c.execute('SELECT id FROM vault_gallery_intelligence_concepts LIMIT 1').fetchone()[0]
        c.execute("INSERT INTO vault_gallery_intelligence_concept_terms(concept_id,term_id,mapping_source,mapping_version) VALUES(%s,%s,'synthetic','test')",(concept,COPIED_TERM))
    with pytest.raises(ValueError,match='generated/system'): cleanup(conninfo,apply=True)
    with psycopg.connect(conninfo) as c:
        c.execute('DELETE FROM vault_gallery_intelligence_concept_terms WHERE term_id=%s',(COPIED_TERM,))
        c.execute('CREATE TABLE synthetic_unknown_reference(term_id UUID REFERENCES vault_metadata_terms(id) ON DELETE CASCADE)')
    with pytest.raises(ValueError,match='Unexpected'): cleanup(conninfo,apply=True)
    with psycopg.connect(conninfo) as c:
        assert c.execute('SELECT count(*) FROM vault_metadata_terms WHERE id=ANY(%s)',(list(TARGETS),)).fetchone()[0]==3
