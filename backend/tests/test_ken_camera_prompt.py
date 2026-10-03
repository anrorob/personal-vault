"""Prompt-only KEN regressions; no database, media or model inference."""
from copy import deepcopy
import hashlib
from uuid import UUID
from app import ken_service as lab, ken_config as ken
from app.ken_repetition import repeated_tail, warn_on_repetition


def test_camera_commentary_requires_a_meaningful_consequence():
    assert 'Do not describe camera behaviour unless it materially changes what the viewer sees' in ken.PROMPT
    assert 'static, close-up, handheld, steady, slightly shaky, or unchanged unless that fact is genuinely important' in ken.PROMPT
    assert 'reveals, follows, introduces, hides, or materially reframes a relevant participant/action' in ken.PROMPT
    assert 'self-facing shot that introduces the filmer' in ken.PROMPT
    assert 'Omit minor angle shifts and lack of panning or zooming' in ken.PROMPT


def test_camera_subject_distinction_and_stop_rule_are_explicit():
    assert 'Distinguish camera movement from movement by people or objects' in ken.PROMPT
    assert 'a pan is not walking' in ken.PROMPT
    assert 'turning the camera toward the filmer is not the filmer entering' in ken.PROMPT
    assert 'If the main activity and relevant participant actions have already been described completely, stop.' in ken.PROMPT
    assert 'Do not pad the ending with static camera position, lack of movement, lack of scene changes, or absence of other people/objects' in ken.PROMPT


def test_regeneration_keeps_camera_rule_and_authoritative_context_without_rewriting_it():
    people=[{'person_id':str(UUID(int=1)),'name':'Synthetic Ada','source':'accepted_people_association'}]
    corrections=[{'id':UUID(int=2),'sequence':1,'text':'There are exactly three people. The camera turns to reveal the filmer.'}]
    context=ken.correction_context('Synthetic old draft. The camera remains stationary.',corrections,people)
    before=deepcopy(context)
    prompt=ken.grounded_prompt(context)
    assert prompt.startswith(ken.PROMPT)
    assert 'Do not carry unnecessary camera commentary or ending filler forward merely because it appeared in the previous draft.' in prompt
    assert 'Explicit user corrections are authoritative, including participant count' in prompt
    assert 'latest explicit change wins' in prompt and 'Use the video again as grounding' in prompt
    assert 'Synthetic Ada' in prompt and corrections[0]['text'] in prompt
    assert context==before  # Guidance changes, not raw history or previous prose.


def test_new_configuration_and_chunks_record_new_prompt_version():
    config=lab.configuration()
    assert config['prompt_version']==ken.PROMPT_VERSION=='ken-language-context-v4'
    assert config['prompt']==ken.PROMPT
    assert config['prompt_sha256']==hashlib.sha256(ken.PROMPT.encode()).hexdigest()
    assert ken.policy(46000)['prompt_version']==config['prompt_version']
    assert config['correction_version']=='ken-corrections-v1'
    assert config['model_revision']=='f982a07559d4a2f6c8744d840bf6fccab30eea96'


def test_generation_bounds_and_repetition_behaviour_are_unchanged():
    assert ken.PARAMETERS=={'temperature':0,'seed':1,'max_tokens':384,'context_size':16384,'threads':18,'quantisation':'Q8_0'}
    normal={'description':'Synthetic activity. Yes, yes. The filmer turns toward a participant.','warnings':[]}
    assert warn_on_repetition(normal) is normal
    block=[f'word{i}' for i in range(48)]
    assert repeated_tail(block*3)
    repeated={'description':' '.join(block*3),'warnings':[]}
    flagged=warn_on_repetition(repeated)
    assert flagged['repetition_guard']=='repeated-block-v1'
    assert flagged['description']==repeated['description'] and repeated['warnings']==[]
