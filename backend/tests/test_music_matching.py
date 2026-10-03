"""Generic synthetic album matching and owner approval contract."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.vault_master import MemoryVaultMasterStore
from app.vault_master_music import (ProviderTrack, AlbumSelectionRequest, ReviewedTrack,
    preview_music_album, approve_music_album)
from app.vault_master_music_matching import normalized_title, propose, assignment
from tests.test_music_groups import root_tracks, intent
from tests.test_vault_master_music import selected_release


def fixture(names, titles=None, metadata=None, durations=None):
    store=MemoryVaultMasterStore(); assets=[]
    for i,a in enumerate(root_tracks(store,len(names))):
        data=(metadata or [{} for _ in names])[i]
        a=replace(a,filename=names[i],detected_metadata=data,effective_metadata=data)
        store.catalogued_assets[a.vault_path]=a;assets.append(a)
    tracks=tuple(ProviderTrack(1,i+1,str(i+1),t,'Example Artist',None,(durations or [180]*len(titles or names))[i]) for i,t in enumerate(titles or names))
    release=replace(selected_release(),tracks=tracks,cover_art_available=False)
    return store,assets,release


@pytest.mark.parametrize('filename,title',[
    ('01 - Bright Sky.wav','Bright Sky'),('01. BRIGHT SKY.wav','Bright Sky'),
    ('1_Bright Sky.wav','Bright Sky'),('(01) Bright Sky.wav','Bright Sky'),
    ('Don’t Stop.wav',"Don't Stop"),('Light & Shade.wav','Light and Shade'),
    ('Bright Sky (Live).wav','Bright Sky (Live)'),('  Café   song.wav','Café song')])
def test_filename_normalization(filename,title):
    _,a,r=fixture([filename],[title]); result=propose(a,r)[0]
    assert result['proposed_track']=='1:1' and result['status']=='confident'


def test_numbers_and_embedded_title_and_fuzzy():
    _,a,r=fixture(['opaque.wav'],['Bright Skyline'],[{'disc_number':'1/2','track_number':'1/3'}])
    assert propose(a,r)[0]['proposed_track']=='1:1'
    _,a,r=fixture(['opaque.wav'],['Bright Skyline'],[{'display_title':'BRIGHT SKYLINE'}])
    assert propose(a,r)[0]['reason'].startswith('Exact normalized embedded')
    _,a,r=fixture(['Bright Skylinne.wav'],['Bright Skyline'])
    assert propose(a,r)[0]['reason'].startswith('High-confidence')


@pytest.mark.parametrize('drift',[.01,.5,2,3])
def test_small_duration_drift(drift):
    _,a,r=fixture(['Bright Sky.wav'],['Bright Sky'],[{'duration_seconds':180+drift}])
    assert propose(a,r)[0]['proposed_track']=='1:1'


def test_duration_and_version_conflicts_require_review():
    _,a,r=fixture(['Bright Sky.wav'],['Bright Sky'],[{'duration_seconds':350}])
    result=propose(a,r)[0];assert result['status']=='review' and result['proposed_track'] is None
    _,a,r=fixture(['Bright Sky (Live).wav'],['Bright Sky'])
    assert propose(a,r)[0]['proposed_track'] is None
    _,a,r=fixture(['Different Song.wav'],['Bright Sky'],[{'duration_seconds':180}])
    assert propose(a,r)[0]['proposed_track'] is None


def test_global_assignment_is_not_greedy():
    result,total=assignment([[100,99,0,0],[98,0,0,0]])
    assert result==[1,0] and total==197


def test_duplicates_and_duration_tie_breaking():
    _,a,r=fixture(['Bright Sky.wav','Bright Sky.wav'],['Bright Sky','Bright Sky'])
    assert all(x['status']=='ambiguous' and x['proposed_track'] is None for x in propose(a,r))
    _,a,r=fixture(['Bright Sky.wav','Bright Sky.wav'],['Bright Sky','Bright Sky'],[{'duration_seconds':180},{'duration_seconds':220}],[180,220])
    assert [x['proposed_track'] for x in propose(a,r)]==['1:1','1:2']


def test_ambiguous_fuzzy_and_multidisc():
    _,a,r=fixture(['The Bright Skylinne Above.wav'],['The Bright Skyline Above','The Bright Skylines Above'])
    assert propose(a,r)[0]['proposed_track'] is None
    _,a,r=fixture(['Bright Sky.wav','Deep Sea.wav'],['Bright Sky','Deep Sea'])
    r=replace(r,tracks=(r.tracks[0],replace(r.tracks[1],disc_number=2,track_number=1)))
    assert [x['proposed_track'] for x in propose(a,r)]==['1:1','2:1']


def workflow(tmp_path,names=('Bright Sky.wav','Deep Sea.wav'),titles=('Bright Sky','Deep Sea')):
    store,assets,release=fixture(list(names),list(titles))
    owner=assets[0].owner_user_id; album=store.declare_music_album(owner,intent())
    store.bind_music_album(album.id,owner,[a.id for a in assets])
    provider=SimpleNamespace(get_release=lambda _:release,get_front_cover=lambda *a:None)
    actor=SimpleNamespace(user_id=owner)
    request=AlbumSelectionRequest(folder='.',album_group_id=album.id,release_id=release.release_id)
    def approve(mapping=None,apply_order=True):
        preview=preview_music_album(request,actor,store,provider)
        body=request.model_copy(update={'review_revision':preview.review_revision,'mapping':mapping if mapping is not None else [ReviewedTrack(asset_id=x['asset_id'],provider_track=x['proposed_track']) for x in preview.local_matches],'apply_order':apply_order})
        return approve_music_album(body,actor,store,provider,tmp_path)
    return store,assets,release,album,actor,provider,request,approve


def test_approval_reidentification_manual_precedence_and_no_file_mutation(tmp_path):
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path)
    for x in a:(tmp_path/x.filename).write_bytes(b'synthetic audio')
    store.update_catalogued_asset_metadata(a[0].id,{'display_title':'Owner title'},'owner')
    result=approve()
    assert result.order_state=='ready'
    assert store.get_catalogued_asset_by_id(a[0].id).display_title=='Owner title'
    assert len(store._music_albums)==1 and len(store.music_album_asset_ids(album.id))==2
    for x in a:
        updated=store.get_catalogued_asset_by_id(x.id)
        assert (updated.sha256,updated.vault_path)==(x.sha256,x.vault_path)
        assert (tmp_path/x.filename).read_bytes()==b'synthetic audio'
    approve()
    assert len([h for h in store.music_album_history if h[2]=='mapping_approved'])==1
    # Different release and explicitly unmatched file clear old derived coordinates.
    new=replace(r,release_id=str(uuid4()),tracks=r.tracks[:1])
    provider.get_release=lambda _:new
    approve([ReviewedTrack(asset_id=a[0].id,provider_track='1:1'),ReviewedTrack(asset_id=a[1].id)])
    assert store.get_music_album_order(album.id)['state']=='unresolved'
    assert 'track_number' not in store.get_catalogued_asset_by_id(a[1].id).imported_metadata
    assert store.get_catalogued_asset_by_id(a[0].id).display_title=='Owner title'


def test_local_extras_missing_tracks_and_order_preservation(tmp_path):
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path,titles=('Bright Sky',))
    assert approve().order_state=='unresolved'
    assert len(store.music_album_asset_ids(album.id))==2
    store.set_music_album_order(album.id,actor.user_id,[x.id for x in reversed(a)])
    assert approve(apply_order=False).order_state=='ready'
    assert store.get_music_album_order(album.id)['asset_ids']==[x.id for x in reversed(a)]
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path,names=('Bright Sky.wav',))
    assert approve().order_state=='unresolved'


def test_override_unmatched_preserved_and_reject_invalid_mapping(tmp_path):
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path)
    approve([ReviewedTrack(asset_id=x.id) for x in a])
    assert all(x['proposed_track'] is None for x in preview_music_album(request,actor,store,provider).local_matches)
    with pytest.raises(HTTPException):approve([ReviewedTrack(asset_id=x.id,provider_track='1:1') for x in a])
    with pytest.raises(HTTPException):approve([ReviewedTrack(asset_id=uuid4())])


def test_stale_preview_and_owner_scope(tmp_path):
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path)
    preview=preview_music_album(request,actor,store,provider)
    body=request.model_copy(update={'review_revision':preview.review_revision,'mapping':[ReviewedTrack(asset_id=x.id) for x in a]})
    store.update_catalogued_asset_metadata(a[0].id,{'display_title':'Changed'},'owner')
    with pytest.raises(HTTPException):approve_music_album(body,actor,store,provider,tmp_path)
    with pytest.raises(HTTPException):preview_music_album(request,SimpleNamespace(user_id=uuid4()),store,provider)


def test_manual_mapping_follows_unique_title_across_release_positions(tmp_path):
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path)
    approve([ReviewedTrack(asset_id=a[0].id,provider_track='1:2'),ReviewedTrack(asset_id=a[1].id,provider_track='1:1')])
    replacement=replace(r,release_id=str(uuid4()),tracks=(replace(r.tracks[1],disc_number=2,track_number=1),replace(r.tracks[0],disc_number=2,track_number=2)))
    proposals=propose([store.get_catalogued_asset_by_id(x.id) for x in a],replacement)
    assert [x['proposed_track'] for x in proposals]==['2:1','2:2']
    assert all(x['status']=='owner_override' for x in proposals)


def test_14_filename_tracks_map_without_position_or_file_order():
    titles=['Amber Dawn','Blue Horizon','Copper Moon','Deep River','Emerald Sky','Falling Leaves','Golden Light','Hidden Valley','Indigo Night','Jade Mountain','Quiet Harbour','Silver Rain','Violet Path','Winter Sun']
    _,assets,release=fixture([title.upper()+'.wav' for title in reversed(titles)],titles)
    results=propose(assets,release)
    assert [r['proposed_track'] for r in results]==[f'1:{i}' for i in range(14,0,-1)]
    assert all(r['status']=='confident' for r in results)


def test_provider_duplicate_positions_and_matching_bound_fail_explicitly():
    _,a,r=fixture(['Bright Sky.wav'],['Bright Sky'])
    with pytest.raises(ValueError,match='positions'):propose(a,replace(r,tracks=(r.tracks[0],r.tracks[0])))
    with pytest.raises(ValueError,match='200'):propose(a*201,r)


def test_mapping_exported_in_portable_sidecar(tmp_path):
    import json
    store,a,r,album,actor,provider,request,approve=workflow(tmp_path)
    store._sidecar_root=tmp_path/'sidecars'
    result=approve()
    assert result.sidecars_exported
    documents=[json.loads(p.read_text()) for p in store._sidecar_root.rglob('*.json')]
    assert len(documents)==2
    assert all('music_match' in json.dumps(document) for document in documents)
