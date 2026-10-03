import hashlib
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from app import music_playback_cache as cache
from app.main import app
from tests.test_music import authenticate, catalogue_track, configure


AUDIO = {"codec_name": "wmalossless", "sample_fmt": "s16p", "sample_rate": "44100", "channels": 2}


def fake_encoder(monkeypatch):
    calls = []
    def probe(path, locks=()):
        if path.suffix in {".partial", ".flac"}:
            return ["flac"], {**AUDIO, "codec_name": "flac", "bits_per_raw_sample": "16"}
        if path.suffix == ".mp3":
            return ["mp3"], {**AUDIO, "codec_name": "mp3"}
        return ["asf"], AUDIO
    def run(command, **kwargs):
        calls.append(command)
        assert command[command.index("-c:a") + 1] == "flac"
        assert "libmp3lame" not in command and "aac" not in command
        assert "-ar" not in command and "-ac" not in command
        assert kwargs["timeout"] == 120
        Path(command[-1]).write_bytes(b"fLaC-synthetic-derivative")
    monkeypatch.setattr(cache, "probe", probe)
    monkeypatch.setattr(cache.subprocess, "run", run)
    return calls


def test_native_mp3_route_authorizes_source_and_preserves_range(client, tmp_path, monkeypatch):
    fake_encoder(monkeypatch)
    root, store = configure(tmp_path)
    path = root / "synthetic.mp3"
    path.write_bytes(b"synthetic-mp3-bytes")
    catalogue_track(store, root, path.name)
    authenticate(client)
    url = client.get("/api/music").json()[0]["playback_url"]
    response = client.get(url, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"synt"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-range"] == "bytes 0-3/19"
    assert path.read_bytes() == b"synthetic-mp3-bytes"


def test_lossless_route_cache_and_unauthorized_access(client, tmp_path, monkeypatch):
    calls = fake_encoder(monkeypatch)
    root, store = configure(tmp_path)
    app.dependency_overrides[cache.get_cache_root] = lambda: tmp_path / "cache"
    path = root / "synthetic.wma"
    path.write_bytes(b"canonical-synthetic")
    asset = catalogue_track(store, root, path.name)
    authenticate(client)
    url = client.get("/api/music").json()[0]["playback_url"]
    first = client.get(url)
    assert first.status_code == 200
    assert first.content.startswith(b"fLaC")
    assert first.headers["content-type"] == "audio/flac"
    assert first.headers["cache-control"] == "private, no-store"
    assert client.get(url, headers={"Range": "bytes=0-3"}).content == b"fLaC"
    assert len(calls) == 1
    assert len(store.catalogued_assets) == 1
    assert path.read_bytes() == b"canonical-synthetic"
    assert not list((tmp_path / "cache").glob("*.mp3"))
    # Existing cache cannot bypass current visibility or authentication.
    store.catalogued_assets.clear()
    assert client.get(url).status_code == 404
    assert len(calls) == 1
    client.cookies.clear()
    assert client.get(url).status_code == 401


def test_cache_reuses_then_invalidates_checksum_and_source(tmp_path, monkeypatch):
    calls = fake_encoder(monkeypatch)
    source = tmp_path / "sources" / "synthetic.wma"
    source.parent.mkdir()
    source.write_bytes(b"original")
    asset = uuid4()
    root = tmp_path / "cache"
    def prepare(checksum):
        path, mime, claim = cache.playback_file(source, asset, checksum, root)
        claim.close()
        assert mime == "audio/flac"
        return path
    first = prepare("a" * 64)
    assert prepare("a" * 64) == first
    assert len(calls) == 1
    assert prepare("b" * 64) != first
    source.write_bytes(b"changed-source")
    assert prepare("b" * 64) != first
    assert len(calls) == 3
    assert not list(root.glob("*.partial"))


def test_cache_evicts_old_entries_and_bounds_count(tmp_path, monkeypatch):
    fake_encoder(monkeypatch)
    monkeypatch.setattr(cache, "MAX_ITEMS", 2)
    source = tmp_path / "sources" / "synthetic.wma"
    source.parent.mkdir()
    source.write_bytes(b"original")
    root = tmp_path / "cache"
    for _ in range(4):
        _, _, claim = cache.playback_file(source, uuid4(), "a" * 64, root)
        claim.close()
    assert len(list(root.glob("*.flac"))) <= 2
    monkeypatch.setattr(cache, "MAX_AGE_SECONDS", -1)
    _, _, claim = cache.playback_file(source, uuid4(), "a" * 64, root)
    claim.close()
    assert len(list(root.glob("*.flac"))) == 1


def test_conversion_failure_removes_partial_and_preserves_source(tmp_path, monkeypatch):
    fake_encoder(monkeypatch)
    def fail(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(cache.subprocess, "run", fail)
    source = tmp_path / "sources" / "synthetic.wma"
    source.parent.mkdir()
    source.write_bytes(b"original")
    with pytest.raises(ValueError):
        cache.playback_file(source, uuid4(), "a" * 64, tmp_path / "cache")
    assert not list((tmp_path / "cache").glob("*.partial"))
    assert not list((tmp_path / "cache").glob("*.flac"))
    assert source.read_bytes() == b"original"


def test_lossless_precision_is_never_silently_reduced(tmp_path, monkeypatch):
    calls = fake_encoder(monkeypatch)
    monkeypatch.setattr(cache, "probe", lambda *args: (["asf"], {
        **AUDIO, "sample_fmt": "s32p", "bits_per_raw_sample": "32"}))
    source = tmp_path / "sources" / "synthetic.wma"
    source.parent.mkdir()
    source.write_bytes(b"original")
    with pytest.raises(ValueError):
        cache.playback_file(source, uuid4(), "a" * 64, tmp_path / "cache")
    assert not calls


def test_native_flac_is_not_reencoded(tmp_path, monkeypatch):
    calls = fake_encoder(monkeypatch)
    source = tmp_path / "synthetic.flac"
    source.write_bytes(b"fLaC-synthetic")
    path, mime, claim = cache.playback_file(source, uuid4(), "a" * 64, tmp_path / "cache")
    assert (path, mime, claim) == (source, "audio/flac", None)
    assert not calls
    assert not (tmp_path / "cache").exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg installed in required CI")
def test_actual_wma_decode_is_sample_identical_after_flac_preparation(tmp_path):
    source = tmp_path / "sources" / "synthetic.wma"
    source.parent.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-c:a", "wmav2", str(source)], check=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    output, mime, claim = cache.playback_file(source, uuid4(), original, tmp_path / "cache")
    try:
        def pcm(path):
            return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                            "-map", "0:a:0", "-c:a", "pcm_s24le", "-f", "s24le", "-"])
        # The fixture encoder is WMA2; decoded samples must survive FLAC intact.
        assert pcm(source) == pcm(output)
        assert mime == "audio/flac"
        assert output.read_bytes().startswith(b"fLaC")
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    finally:
        claim.close()
