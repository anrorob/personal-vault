"""Synthetic-only native preparation, persistence, cleanup and integrity checks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import ken_service as lab
from app import ken_native as native
from app.ken_config import PROMPT, PROMPT_VERSION
from tests.test_ken_service import MemoryKen, FakeAdapter


@pytest.fixture
def video(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, 'ADAPTER', FakeAdapter())
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required for synthetic video fixtures")
    source = tmp_path / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=800x480:rate=5:duration=3", "-an", str(source)],
                   check=True, capture_output=True)
    monkeypatch.setenv("PV_HOME_VIDEOS_PATH", str(tmp_path))
    asset = SimpleNamespace(id=uuid4(), owner_user_id=uuid4(), asset_type="Home Videos",
                            vault_path="/vault/Home Videos/synthetic.mp4",
                            sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    return source, asset


def prepare(root, asset, ordinal):
    work = root / f"pv-ken-{uuid4()}-{ordinal:08d}"
    work.mkdir()
    config = lab.configuration()
    payload = lab.prepare_input(asset, config, work)
    lab.verify_input(asset.id, config, payload)
    return work, config, payload


def test_native_is_continuous_bounded_and_never_uses_jpeg_sampler(video, tmp_path, monkeypatch):
    source, asset = video
    def forbidden(*args, **kwargs):
        pytest.fail("Native input entered the PV JPEG path")
    monkeypatch.setattr(lab, "extract_frame", forbidden, raising=False)
    monkeypatch.setattr(lab, "select_deterministic_frame_positions", forbidden, raising=False)
    work, config, payload = prepare(tmp_path, asset, 1)
    params = config["runtime_video"]
    assert params["prepared_kind"] == "prepared_native_video"
    assert params["prepared_stream"]["duration_ms"] == 3000
    assert params["prepared_stream"]["frame_rate"] == "5/1"
    assert max(params["prepared_stream"]["width"], params["prepared_stream"]["height"]) <= 384
    assert params["max_decoded_frames"] == 602
    assert params["fps"] * params["prepared_stream"]["duration_ms"] <= 126000
    assert "frames" not in payload and not list(work.glob("*.jpg"))
    assert config["prompt"] == PROMPT and config["prompt_version"] == PROMPT_VERSION
    assert config["prompt_sha256"] == hashlib.sha256(payload["prompt"].encode()).hexdigest()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == asset.sha256
    assert json.loads((work / "native.json").read_text())["input_fingerprint"] == payload["input_fingerprint"]
    assert set(lab.ENGINE.supported_input_modes) == {"native_video"}


def test_native_repeat_and_changed_source_fingerprints(video, tmp_path):
    source, asset = video
    _, first, one = prepare(tmp_path, asset, 1)
    _, _, two = prepare(tmp_path, asset, 2)
    assert one["input_fingerprint"] == two["input_fingerprint"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=blue:size=800x480:rate=5:duration=3", "-an", str(source)],
                   check=True, capture_output=True)
    with pytest.raises(ValueError, match="catalogue"):
        prepare(tmp_path, asset, 3)
    asset.sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    _, _, three = prepare(tmp_path, asset, 4)
    assert three["input_fingerprint"] != one["input_fingerprint"]
    changed = deepcopy(first)
    changed["runtime_video"]["fps"] /= 2
    with pytest.raises(ValueError, match="fingerprint"):
        lab.verify_input(asset.id, changed, one)
    with pytest.raises(ValueError, match="different asset"):
        lab.verify_input(uuid4(), first, one)


def test_canonical_video_used_when_decodable_and_readable(video, tmp_path):
    source, asset = video
    tmp_path.chmod(0o755)
    smaller = tmp_path / "smaller.mp4"
    native.prepare_compatible_video(source, smaller, max_dimension=384)
    smaller.chmod(0o644)
    asset.vault_path = "/vault/Home Videos/smaller.mp4"
    asset.sha256 = hashlib.sha256(smaller.read_bytes()).hexdigest()
    work, config, _ = prepare(tmp_path, asset, 1)
    assert config["runtime_video"]["prepared_kind"] == "canonical_video"
    assert not (work / "prepared.mp4").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_native_worker_persists_before_inference_and_cleans_derivative(video, tmp_path, monkeypatch, failure):
    source, asset = video
    store = MemoryKen()
    run = store.queue(asset.id, asset.owner_user_id, lab.configuration())
    assert store.queue(asset.id, asset.owner_user_id, lab.configuration())["id"] == run["id"]
    work_root = tmp_path / "inputs"
    monkeypatch.setenv("PV_KEN_WORK_ROOT", str(work_root))
    class Adapter(FakeAdapter):
        def analyse(self, payload):
            assert run["status"] == "analysing"
            assert run["configuration"]["input_integrity"]["input_fingerprint"] == payload["input_fingerprint"]
            assert run["configuration"]["input_mode"] == "native_video"
            assert run["configuration"]["parameters"]["max_tokens"] == 384
            assert run["configuration"]["runtime_video"]["max_decoded_frames"] == 602
            assert (work_root / payload["native_job"] / "prepared.mp4").is_file()
            record = json.loads((work_root / payload["native_job"] / "native.json").read_text())
            assert record["run_id"] == str(run["id"]) == payload["run_id"]
            assert record["asset_id"] == str(asset.id)
            if failure:
                raise RuntimeError("Synthetic service failure")
            return super().analyse(payload)
    monkeypatch.setattr(lab, 'ADAPTER', Adapter())
    vault = SimpleNamespace(get_catalogued_asset_by_id=lambda identifier: asset)
    assert lab.process_next(store, vault) == run["id"]
    assert run["status"] == ("failed" if failure else "completed")
    assert not list(work_root.iterdir())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == asset.sha256


def test_abandoned_input_cleanup_waits_for_idle_service(tmp_path, monkeypatch):
    abandoned = tmp_path / f"pv-ken-{uuid4()}-abcdefgh"
    abandoned.mkdir()
    (abandoned / "prepared.mp4").write_bytes(b"synthetic")
    unrelated = tmp_path / "keep"
    unrelated.mkdir()
    monkeypatch.setenv("PV_KEN_WORK_ROOT", str(tmp_path))
    adapter = FakeAdapter()
    monkeypatch.setattr(lab, 'ADAPTER', adapter)
    monkeypatch.setattr(adapter, "health", lambda: {"status": "busy"})
    assert lab.process_next(MemoryKen(), None) is None
    assert abandoned.exists()
    monkeypatch.setattr(adapter, "health", lambda: {"status": "available"})
    assert lab.process_next(MemoryKen(), None) is None
    assert not abandoned.exists() and unrelated.exists()
