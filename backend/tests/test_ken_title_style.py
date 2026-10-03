"""Title-only contract and synthetic runtime checks; no real descriptions."""
from copy import deepcopy
from uuid import uuid4
import pytest
from app import ken_config as ken
from app.ken_attestation import digest_json


def test_specific_direct_and_context_supported_creative_title_rules():
    prompt=ken.TITLE_PROMPT
    assert ken.TITLE_VERSION=='ken-title-locality-v4'
    assert 'DIRECT / FACTUAL' in prompt and 'CREATIVE / EVENT-STYLE' in prompt
    for phrase in ('intimate moment','intimate scene','private moment','private scene','adult moment','adult scene',
                   'romantic moment','romantic scene','sexual activity','physical activity','personal moment','special moment'):
        assert phrase in prompt
    for rule in ('Name the supported activity specifically','same direct factual vocabulary',
                 'name a clearly identified sexual activity directly','do not sanitize or euphemize',
                 'Do not make the title gratuitously anatomical','preserve that uncertainty',
                 'Do not force creativity or humour','never invent names or name-to-person mappings',
                 'do not force location into every title','Normally 3-8 words','at most 12 words and 120 characters'):
        assert rule in prompt
    assert 'expose sensitive details unnecessarily' not in prompt


@pytest.mark.parametrize('description,title',[
    ('Two identified adults, Synthetic Ada and Synthetic Ben, are having sex.','Synthetic Ada and Ben Having Sex'),
    ('Synthetic Ada holds a coconut in the trusted location.','Synthetic Ada Holding a Coconut'),
    ('Synthetic Ada and Synthetic Ben race pool inflatables playfully.','The Synthetic Inflatable Showdown'),
])
def test_title_runtime_receives_context_and_only_removes_control_markers(monkeypatch,description,title):
    pytest.importorskip("resource", reason="Title runtime is Linux-only")
    import ken_infer
    context={'accepted_description':description,'trusted_people':[{'name':'Synthetic Ada'},{'name':'Synthetic Ben'}],
             'trusted_location':{'name':'Synthetic Island','source':'user_override'},
             'corrections':[{'text':'Keep the title concise.'}]}
    body={k:str(uuid4()) for k in ('asset_id','run_id','request_id')}
    body.update(context=context,context_fingerprint=digest_json(context),prompt_version=ken.TITLE_VERSION)
    before=deepcopy(body);calls=[]
    def generate(prompt,max_tokens):
        calls.append(prompt)
        assert max_tokens==64
        assert description in prompt and 'Synthetic Island' in prompt and 'Synthetic Ben' in prompt
        assert 'Keep the title concise.' in prompt and 'authoritative' in prompt
        return title+' [end of text]'
    monkeypatch.setattr(ken_infer,'synthesize',generate)
    assert ken_infer.execute_title(body)['title']==title
    assert body==before and len(calls)==1
    assert ken.validate_title('Synthetic [Pool]')=='Synthetic [Pool]'
    old={**body,'prompt_version':'ken-title-context-v2'}
    with pytest.raises(ValueError,match='context mismatch'):ken_infer.execute_title(old)
    assert len(calls)==1


@pytest.mark.parametrize('title',['word '*13,'x'*121,'[end of text]','Synthetic\nTitle'])
def test_title_bounds_remain_enforced(title):
    with pytest.raises(ValueError):ken.validate_title(title)

from tests.test_postgres_ken import pg_ken
from tests.test_ken_titles import titled_ken,TitleAdapter
from tests.test_ken_config import complete,config


def test_new_title_version_does_not_rewrite_historical_title(titled_ken):
    store,a,o=titled_ken
    run=complete(store,store.queue(a,o,config()))
    old=store.generate_title(a,o,run['id'],TitleAdapter())
    with store.connect() as c:
        c.execute("UPDATE vault_ken_titles SET prompt_version='ken-title-context-v2' WHERE id=%s",(old['id'],))
        before=c.execute('SELECT * FROM vault_ken_titles WHERE id=%s',(old['id'],)).fetchone()
    new=store.generate_title(a,o,run['id'],TitleAdapter())
    assert new['prompt_version']==ken.TITLE_VERSION and new['id']!=old['id']
    with store.connect() as c:
        assert c.execute('SELECT * FROM vault_ken_titles WHERE id=%s',(old['id'],)).fetchone()==before
