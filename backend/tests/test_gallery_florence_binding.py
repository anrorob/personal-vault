from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from app.auth import AuthenticatedIdentity
from app.gallery_reconciliation import published_source_items, latest_retained_florence_visual_evidence
from app.vault_master_ai import MemoryAiStore
from app.vault_master_api import get_asset_ai_evidence
from tests.test_gallery_intelligence import gallery_asset, retained_florence_description


def fixture(tmp_path):
    vault, asset = gallery_asset(tmp_path)
    evidence = retained_florence_description(vault, asset, 'Synthetic photo evidence.')
    item = next(iter(vault.items.values()))
    renamed = replace(asset, vault_path='/vault/Gallery/published.jpg', filename='published.jpg')
    vault.catalogued_assets = {renamed.vault_path: renamed}
    vault.asset_history.append(dict(id=uuid4(), asset_id=asset.id, action='section_moved',
        previous_values={'vault_path': asset.vault_path}, current_values={'vault_path': renamed.vault_path},
        created_at=datetime.now(timezone.utc)))
    return vault, renamed, item, evidence


def identity(owner):
    return AuthenticatedIdentity(SimpleNamespace(username='owner',user_id=owner,display_name='Owner',role='user',active=True))


def test_existing_renamed_asset_evidence_reaches_api_and_reconciliation(tmp_path):
    vault, asset, item, evidence = fixture(tmp_path)
    assert published_source_items(vault, asset) == [item]
    assert latest_retained_florence_visual_evidence(vault, evidence, asset).description == 'Synthetic photo evidence.'
    result = get_asset_ai_evidence(asset.id, identity(asset.owner_user_id), vault, MemoryAiStore(), evidence, Response())
    assert result.visual_description.caption == 'Synthetic photo evidence.'
    assert len(evidence.evidence) == 1  # Read-only recovery; no duplicate inference/evidence.


def test_multiple_connected_moves_and_unrelated_history(tmp_path):
    vault, asset, item, evidence = fixture(tmp_path)
    moved = replace(asset,vault_path='/vault/Gallery/final.jpg')
    vault.asset_history.append(dict(id=uuid4(),asset_id=asset.id,action='section_moved',
        previous_values={'vault_path':asset.vault_path},current_values={'vault_path':moved.vault_path},
        created_at=datetime.now(timezone.utc)+timedelta(seconds=1)))
    assert published_source_items(vault,moved)==[item]
    vault.asset_history[0]['current_values']['vault_path']='/vault/Gallery/unrelated.jpg'
    assert published_source_items(vault,moved)==[]


@pytest.mark.parametrize('changes',[
    {'owner_user_id':uuid4()}, {'owner_user_id':None}, {'sha256':'b'*64},
    {'size_bytes':999}, {'state':'inventoried'}, {'source_kind':'inventory'},
    {'proposed_destination':'/vault/Gallery/unrelated.jpg'},
])
def test_source_identity_and_publication_are_required(tmp_path,changes):
    vault,asset,item,evidence=fixture(tmp_path)
    vault.items[item.source_path]=replace(item,**changes)
    assert published_source_items(vault,asset)==[]


@pytest.mark.parametrize('field,value', [('asset_id',uuid4()),('action','user_metadata_updated')])
def test_unrelated_history_cannot_authorise_source(tmp_path,field,value):
    vault,asset,item,evidence=fixture(tmp_path)
    vault.asset_history[0][field]=value
    assert published_source_items(vault,asset)==[]


def test_no_uuid_or_history_does_not_fall_back_to_hash_or_username(tmp_path):
    vault,asset,item,evidence=fixture(tmp_path)
    assert published_source_items(vault,replace(asset,owner_user_id=None))==[]
    vault.asset_history.clear()
    assert published_source_items(vault,asset)==[]
    assert published_source_items(vault,replace(asset,vault_path=item.proposed_destination))==[item]


def test_other_owner_cannot_read_evidence(tmp_path):
    vault,asset,item,evidence=fixture(tmp_path)
    with pytest.raises(HTTPException) as exc:
        get_asset_ai_evidence(asset.id,identity(uuid4()),vault,MemoryAiStore(),evidence,Response())
    assert exc.value.status_code==404


@pytest.fixture(autouse=True)
def isolated_canonical_florence(monkeypatch):
    # These tests supply retained ingestion evidence. Canonical fallback is an
    # injected empty store, never a connection to a real Vault database.
    from types import SimpleNamespace
    monkeypatch.setattr('app.gallery_florence.get_gallery_florence_store',
                        lambda: SimpleNamespace(latest_evidence=lambda *_:None))
