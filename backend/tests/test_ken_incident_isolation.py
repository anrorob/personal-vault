"""Synthetic context controls: no real descriptions or media."""
from copy import deepcopy
import pytest
from uuid import uuid4
from app import ken_config as ken, ken_style as style, ken_owner_style


def test_exemplar_switch_cannot_transfer_facts_to_effective_prompt():
    current=ken.correction_context('',[],[{'name':'Current Person','person_id':str(uuid4())}],{'name':'Current Town'})
    current['owner_style']={'version':style.VERSION,'preferences':dict(style.DEFAULTS)}
    control=ken.grounded_prompt(current)
    attempted=deepcopy(current)
    attempted['owner_style']['exemplars']=[{'id':str(uuid4()),'text':
        'OldName and seven sailors lie on their backs behind a purple submarine in OldTown while juggling pineapples.'}]
    with pytest.raises(ValueError, match='Only structured'):
        ken.grounded_prompt(attempted)
    for forbidden in ('OldName','seven sailors','on their backs','purple submarine','OldTown','pineapples'):
        assert forbidden not in control
    assert 'Current Person' in control and 'Current Town' in control
    assert 'Name a clearly supported activity directly' in control


def test_disabled_exemplars_are_not_even_loaded(monkeypatch):
    monkeypatch.setattr(ken_owner_style,'preferences',lambda *a: {'preferences':dict(style.DEFAULTS),'revision':1})
    assert not hasattr(ken_owner_style,'examples')
    assert set(ken_owner_style.context(object(),uuid4()))=={'version','preference_revision','preferences'}


def test_current_asset_corrections_stay_in_current_context():
    a=ken.correction_context('Synthetic previous draft',[{'id':uuid4(),'sequence':1,'text':'Exactly seven sailors stand behind the submarine in OldTown.'}])
    b=ken.correction_context('',[],[{'name':'Current Person'}],{'name':'Current Town'})
    assert 'seven sailors' in ken.grounded_prompt(a)
    for fact in ('seven sailors','behind the submarine','OldTown','Synthetic previous draft'):
        assert fact not in ken.grounded_prompt(b)


def test_exemplar_preference_and_arbitrary_facts_rejected():
    for value in ({'use_style_exemplars':True},{'participant_count':7},{'activity_first':'seven sailors'}):
        with pytest.raises(ValueError):style.validate_preferences(value)
