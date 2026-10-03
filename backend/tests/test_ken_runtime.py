"""Real video preparation/decoder, fake prose: never user media."""
from copy import deepcopy
import io
from pathlib import Path
import subprocess
import pytest
import sys
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux analyser runtime")
from types import SimpleNamespace
from uuid import uuid4
from app import ken_service as lab, ken_config as ken
from app.ken_attestation import valid_attestation


@pytest.mark.parametrize('duration', [46, 600, 1092, 1800])
def test_chunks_rewatch_video_and_bind_corrections_and_full_timeline(tmp_path, monkeypatch, duration):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'video_analyser'))
    # parents[2] is backend's parent repository.
    import ken_infer, native_input, video_ffmpeg
    monkeypatch.setattr(native_input,'INPUT_ROOT',tmp_path)
    monkeypatch.setattr(native_input,'VIDEO_ROOT',tmp_path)
    monkeypatch.setenv('PV_KEN_SCRATCH',str(tmp_path))
    monkeypatch.setenv('PV_HOME_VIDEOS_PATH',str(tmp_path))
    source=tmp_path/'synthetic.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'color=blue:s=64x64:r=4:d={duration}',
                    '-c:v','libx264','-threads','1',str(source)],check=True,capture_output=True)
    asset=SimpleNamespace(id=uuid4(),owner_user_id=uuid4(),asset_type='Home Videos',
                          vault_path='/vault/Home Videos/synthetic.mp4',sha256=lab.file_sha256(source))
    calls=[]; synthesis=[]
    def fake_model(body, proof, *, native_override):
        path, params, identity=native_override
        calls.append({'sha':lab.file_sha256(path),'prompt':body['prompt'],'fp':body['input_fingerprint']})
        output=io.BytesIO()
        evidence=video_ffmpeg.attest(['-nostdin','-read_ahead_limit','-1','-i','cache:pipe:0','-vf','fps=1.000000',
                                     '-f','rawvideo','-pix_fmt','rgb24','pipe:1','-loglevel','error'],
            {'width':64,'height':64,'fps':1,'max_decoded_frames':44,'expected_input_size':path.stat().st_size,
             'expected_input_sha256':lab.file_sha256(path),'attestation':proof},io.BytesIO(path.read_bytes()),output)
        assert output.getvalue()
        return {'description':'Synthetic chunk evidence','input_attestation':evidence,'error':None,
                'prompt_eval_tokens':500,'processing_ms':1}
    def fake_synthesis(prompt):
        synthesis.append(prompt)
        return 'Synthetic combined description'
    monkeypatch.setattr(ken_infer,'synthesize',fake_synthesis)
    results=[]
    for corrected in (False,True):
        cfg=lab.configuration(); cfg['run_id']=str(uuid4())
        if corrected:
            cfg['correction_context']=ken.correction_context('Previous synthetic prose',
                [{'id':uuid4(),'sequence':1,'text':'Exactly two people. Camera movement is not walking.'}])
        from app import ken_style as owner_style
        cfg.setdefault('correction_context',ken.correction_context('',[]))['owner_style']={
            'version':owner_style.VERSION,'preference_revision':1,'preferences':dict(owner_style.DEFAULTS)}
        work=tmp_path/f'pv-ken-{uuid4()}-00000000'; work.mkdir()
        body=lab.prepare_input(asset,cfg,work)
        result=ken_infer.execute_ken(body,fake_model)
        assert valid_attestation(body,result['input_attestation'])
        assert result['input_attestation']['decoded_manifest']['frame_count']==duration
        assert [f['timestamp_ms'] for f in result['input_attestation']['decoded_manifest']['frames']]==list(range(0,duration*1000,1000))
        assert len(result['input_attestation']['chunks'])==len(ken.policy(duration*1000)['chunks'])
        assert result['max_output_tokens']==384
        bad=deepcopy(result['input_attestation']); bad['chunks'][1]['start_ms']+=1000
        assert not valid_attestation(body,bad)
        bad=deepcopy(result['input_attestation']); bad['chunks'][0]['prepared_sha256']='0'*64
        assert not valid_attestation(body,bad)
        bad=deepcopy(result['input_attestation']); bad['correction_fingerprint']='0'*64
        assert not valid_attestation(body,bad)
        results.append(result)
    offset=len(calls)//2
    assert offset==len(ken.policy(duration*1000)['chunks']) and calls[0]['sha']==calls[offset]['sha']
    assert calls[0]['fp']!=calls[offset]['fp']
    assert all(call['prompt'].startswith(ken.PROMPT) for call in calls)
    assert all('Name a clearly supported activity directly' in call['prompt'] for call in calls)
    assert all(text.startswith(ken.PROMPT) for text in synthesis)
    assert 'Exactly two people' not in calls[0]['prompt']
    assert 'Exactly two people' in calls[offset]['prompt'] and 'Previous synthetic prose' in calls[offset]['prompt']
    assert 'authoritative' in calls[offset]['prompt'] and 'Synthetic chunk evidence' in synthesis[1]
    assert results[0]['input_fingerprint']!=results[1]['input_fingerprint']
    assert lab.file_sha256(source)==asset.sha256


def test_context_budget_uses_tokenizer_and_reserves_visual_output_and_template(tmp_path, monkeypatch):
    import json
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'video_analyser'))
    import ken_infer
    calls=[]
    def tokenize(command, **kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(stdout=json.dumps([1]*1000).encode())
    monkeypatch.setattr(ken_infer.subprocess,'run',tokenize)
    assert ken_infer.check_context('Synthetic prompt',visual_tokens=4224)==1000
    assert '--stdin' in calls[0][0] and calls[0][1]['input']==b'Synthetic prompt'
    with pytest.raises(ValueError,match='context budget'):
        ken_infer.check_context('Synthetic prompt',visual_tokens=15000)


def test_text_synthesis_is_single_turn_preserves_prose_and_stops_loops(tmp_path, monkeypatch):
    import time
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'video_analyser'))
    import ken_infer, server
    monkeypatch.setattr(server,'BINARY',str(tmp_path/'llama-mtmd-cli'))
    monkeypatch.setattr(ken_infer,'check_context',lambda *args, **kwargs: 100)
    binary=tmp_path/'llama-completion'
    binary.write_text('''#!/usr/bin/env python3
import sys
assert '--single-turn' in sys.argv and '--no-display-prompt' in sys.argv
assert '--conversation' in sys.argv and '--jinja' in sys.argv
assert '--log-disable' not in sys.argv  # This pinned binary uses logging for generated stdout too.
print('A concise synthetic description. Normal repetition: yes, yes. [end of text]')
''')
    binary.chmod(0o755)
    assert ken_infer.synthesize('Synthetic evidence') == 'A concise synthetic description. Normal repetition: yes, yes.'
    block=' '.join(f'word{i}' for i in range(32))+' '
    binary.write_text('#!/usr/bin/env python3\nimport time\nprint('+repr(block*4)+',flush=True)\ntime.sleep(10)\n')
    started=time.monotonic()
    assert ken_infer.synthesize('Synthetic loop evidence') == (block*4).strip()
    assert time.monotonic()-started < 5


def test_service_runtime_matches_registered_ken(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'video_analyser'))
    import server
    assert server.RUNTIME==ken.RUNTIME and server.REVISION==ken.REVISION


def test_title_runtime_binds_context_and_uses_bounded_text_only_generation(monkeypatch):
    from app.ken_attestation import digest_json
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'video_analyser'))
    import ken_infer
    calls=[]
    monkeypatch.setattr(ken_infer,'synthesize',lambda prompt,max_tokens:(calls.append((prompt,max_tokens)) or 'Synthetic Pool Challenge'))
    context={'accepted_description':'Synthetic activity','trusted_people':[{'name':'Synthetic Ada'}]}
    body={'asset_id':str(uuid4()),'run_id':str(uuid4()),'request_id':str(uuid4()),'context':context,
          'context_fingerprint':digest_json(context),'prompt_version':ken.TITLE_VERSION}
    result=ken_infer.execute_title(body)
    assert calls[0][1]==64 and 'Synthetic Ada' in calls[0][0]
    assert result['request_id']==body['request_id'] and result['context_fingerprint']==body['context_fingerprint']
    body['context_fingerprint']='0'*64
    with pytest.raises(ValueError,match='context mismatch'):ken_infer.execute_title(body)
