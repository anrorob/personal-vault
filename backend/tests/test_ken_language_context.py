"""Synthetic KEN language/context regressions. No real model descriptions."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app import ken_config as ken
from app.ken_titles import trusted_location, trusted_people
from app.ken_attestation import digest_json


class ContextConnection:
    def __init__(self, asset, owner, name, source):
        self.asset, self.owner, self.name, self.source = asset, owner, name, source
    def execute(self, query, params):
        assert 'owner_user_id=%s' in query and 'id=%s' in query
        row = {'location': self.name, 'metadata_provenance': {'location': self.source}} if params == (self.asset,self.owner) else None
        return SimpleNamespace(fetchone=lambda: row)


@pytest.mark.parametrize('name,source', [('Athens, Greece','user_override'),('Greece','embedded'),('Tulum, Mexico','import:sidecar')])
def test_location_granularity_owner_and_asset_scope(name, source):
    asset,owner=uuid4(),uuid4()
    conn=ContextConnection(asset,owner,name,source)
    assert trusted_location(conn,asset,owner)=={'name':name,'source':source}
    assert trusted_location(conn,asset,uuid4()) is None
    assert trusted_location(conn,uuid4(),owner) is None


@pytest.mark.parametrize('name,source', [(None,'unavailable'),('Guessed place','model'),('Unknown',''),('Place','detected')])
def test_no_fabricated_or_untrusted_location(name,source):
    a,o=uuid4(),uuid4()
    assert trusted_location(ContextConnection(a,o,name,source),a,o) is None


def test_initial_regeneration_and_title_share_trusted_context():
    names=[{'person_id':str(uuid4()),'name':name} for name in ('Synthetic Ada','Synthetic Ben')]
    location={'name':'Greece','source':'embedded'}
    corrections=[{'id':uuid4(),'sequence':1,'text':'There are exactly two people. They are swimming.'}]
    ctx=ken.correction_context('Previous synthetic draft',corrections,names,location)
    before=deepcopy(ctx)
    for prompt in (ken.grounded_prompt(ctx),ken.title_prompt(ctx)):
        assert 'Synthetic Ada' in prompt and 'Synthetic Ben' in prompt and 'Greece' in prompt
        assert 'authoritative' in prompt and corrections[0]['text'] in prompt
    assert ctx==before
    assert digest_json(ctx)!=digest_json(ken.correction_context('Previous synthetic draft',corrections,names,None))
    assert ken.correction_context('',[])['trusted_location'] is None
    assert 'Use trusted names consistently throughout' in ken.PROMPT
    assert 'Unidentified participants remain generic' in ken.PROMPT
    assert 'do not invent name-to-person mappings' in ken.PROMPT
    assert 'Use pronouns only when unambiguous' in ken.PROMPT


def test_activity_ambiguity_and_title_rules():
    assert 'Name the visible activity directly when identifiable' in ken.PROMPT
    for term in ('physical engagement','intimate interaction','genital area','pelvic area','suggestive behaviour'):
        assert term in ken.PROMPT
    assert 'adult or sexual activity is clearly visible' in ken.PROMPT
    assert 'Do not force specificity when the evidence is ambiguous' in ken.PROMPT
    assert 'stop' in ken.PROMPT and 'static' in ken.PROMPT
    assert 'do not force location into every title' in ken.TITLE_PROMPT
    assert 'Name the supported activity specifically' in ken.TITLE_PROMPT
    assert ken.TITLE_TOKENS==64 and ken.PARAMETERS['max_tokens']==384


@pytest.mark.parametrize('marker',ken.CONTROL_MARKERS)
def test_only_known_control_markers_removed_from_descriptions_and_titles(marker):
    assert ken.clean_generated_output('Synthetic swimming [pool]. '+marker)=='Synthetic swimming [pool].'
    assert ken.validate_title('Synthetic Pool [Final] '+marker)=='Synthetic Pool [Final]'
    assert ken.clean_generated_output(marker)==''


def test_normal_prose_and_brackets_are_preserved():
    text='Synthetic [pool] activity. Yes, yes. [ordinary text] <ordinary>.'
    assert ken.clean_generated_output(text)==text
    assert ken.validate_title('Synthetic [Pool]')=='Synthetic [Pool]'
    with pytest.raises(ValueError):ken.validate_title('[end of text]')


def test_people_query_enforces_immutable_owner_and_accepted_associations():
    def execute(query, params):
        assert 'a.owner_user_id=p.owner_user_id' in query
        assert 'p.owner_user_id=%s' in query
        assert 'x.owner_user_id=a.owner_user_id' in query
        assert "d.decision='include'" in query and "<>'exclude'" in query
        assert params==(asset,owner)
        return SimpleNamespace(fetchall=lambda:[])
    asset,owner=uuid4(),uuid4()
    assert trusted_people(SimpleNamespace(execute=execute),asset,owner)==[]


def test_historical_generated_display_cleanup_does_not_rewrite_storage_or_canonical():
    from app.ken_api import public_run
    run={'configuration':{'correction_version':ken.PHASE},'result':{'description':'Synthetic [pool]. [end of text]','input_fingerprint':'same'}}
    before=deepcopy(run)
    assert public_run(run)['result']=={'description':'Synthetic [pool].','input_fingerprint':'same'}
    assert run==before
    canonical={'configuration':{},'result':{'description':'Manual [end of text]'}}
    assert public_run(canonical) is canonical
