import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from app import home_video_playback as playback
from app.main import app
from app.video_thumbnails import get_video_thumbnail_cache_path
from tests.test_video_thumbnails import publish
from tests.test_vault_libraries import configure_libraries, authenticate


def facts(container="mov,mp4,m4a,3gp,3g2,mj2", video="h264", audio="aac"):
    streams = [{"index": 0, "codec_type": "video", "codec_name": video,
                "pix_fmt": "yuv420p", "profile": "High", "level": 31,
                "width": 160, "height": 90, "codec_tag_string": "avc1",
                "field_order": "progressive"}]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": audio,
                        "profile": "LC", "channels": 2, "sample_rate": "48000"})
    return {"streams": streams, "format": {"format_name": container, "duration": "0.5",
                                          "tags": {"major_brand": "isom"}}}


@pytest.mark.parametrize("container,video,audio,mode,copy_video,copy_audio", [
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac", "direct", True, True),
    ("mpegts", "h264", "aac", "remux", True, True),
    ("matroska,webm", "h264", "ac3", "transcode", True, False),
    ("avi", "mpeg4", "ac3", "transcode", False, False),
    ("asf", "wmv2", None, "transcode", False, True),
    ("mpegts", "h264", None, "remux", True, True),
    ("mov,mp4,m4a,3gp,3g2,mj2", "hevc", "aac", "transcode", False, True),
])
def test_playback_decision_and_command(tmp_path, container, video, audio, mode, copy_video, copy_audio):
    plan = playback.playback_plan(facts(container, video, audio))
    assert (plan.mode, plan.copy_video, plan.copy_audio) == (mode, copy_video, copy_audio)
    source = tmp_path / "family holiday unicode-Łódź.mts"
    command = playback.conversion_command(source, tmp_path / "partial.mp4", plan)
    assert command[command.index("-i") + 1] == str(source.resolve())
    assert command[command.index("-c:v") + 1] == ("copy" if copy_video else "libx264")
    if audio:
        assert command[command.index("-c:a") + 1] == ("copy" if copy_audio else "aac")
    else:
        assert "-an" in command
    assert "+faststart" in command


@pytest.mark.parametrize("mutation", ["10bit", "he-aac", "quicktime", "multichannel", "extra-stream"])
def test_direct_decision_is_conservative(mutation):
    probe = facts()
    if mutation == "10bit":
        probe["streams"][0]["pix_fmt"] = "yuv420p10le"
    elif mutation == "he-aac":
        probe["streams"][1]["profile"] = "HE-AAC"
    elif mutation == "quicktime":
        probe["format"]["tags"]["major_brand"] = "qt  "
    elif mutation == "multichannel":
        probe["streams"][1]["channels"] = 6
    else:
        probe["streams"].append({"index": 2, "codec_type": "subtitle"})
    assert playback.playback_plan(probe).mode != "direct"


@pytest.mark.parametrize("probe", [{}, {"streams": [], "format": {"duration": "1"}},
                                   {"streams": [], "format": {"duration": "nan"}}])
def test_malformed_probe_fails(probe):
    with pytest.raises(ValueError):
        playback.playback_plan(probe)


def await_status(source, asset, root, wanted="ready"):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = playback.playback_status(source, asset, "checksum", root)
        if state != "preparing":
            assert state == wanted
            return
        time.sleep(0.02)
    pytest.fail("Playback worker did not complete")


def fake_media(monkeypatch):
    calls = []
    def probe(source, locks=()):
        return facts() if source.suffix == ".mp4" else facts("mpegts")
    def convert(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == playback.CONVERSION_TIMEOUT
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert not kwargs.get("shell")
        Path(command[-1]).write_bytes(b"synthetic-proxy-bytes")
    monkeypatch.setattr(playback, "probe_media", probe)
    monkeypatch.setattr(playback.subprocess, "run", convert)
    return calls


def test_async_deduplication_reuse_invalidation_and_partial(tmp_path, monkeypatch):
    source = tmp_path / "recording.mts"
    source.write_bytes(b"original")
    asset = uuid4()
    root = tmp_path / "cache"
    calls = fake_media(monkeypatch)
    started, release = threading.Event(), threading.Event()
    original_probe = playback.probe_media
    def blocked_probe(source, locks=()):
        started.set()
        assert release.wait(5)
        return original_probe(source, locks)
    monkeypatch.setattr(playback, "probe_media", blocked_probe)
    assert playback.playback_status(source, asset, "checksum", root) == "preparing"
    assert started.wait(2)
    try:
        states = []
        threads = [threading.Thread(target=lambda: states.append(
            playback.playback_status(source, asset, "checksum", root))) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        assert states == ["preparing"] * 12
        assert calls == []
        assert playback.playback_file(source, asset, "checksum", root) is None
    finally:
        release.set()
    await_status(source, asset, root)
    assert len(calls) == 1
    cached = playback.playback_file(source, asset, "checksum", root)
    assert cached is not None
    assert playback.playback_status(source, asset, "checksum", root) == "ready"
    assert len(calls) == 1
    source.write_bytes(b"replacement with a different size")
    assert playback.playback_file(source, asset, "checksum", root) is None
    key = playback.source_key(source, asset, "checksum")
    partial = root / f"{key}.orphan.partial.mp4"
    partial.write_bytes(b"incomplete")
    playback._write_state(root, key, {"status": "preparing"})
    await_status(source, asset, root)
    assert len(calls) == 2
    assert not partial.exists()
    assert playback.playback_file(source, asset, "checksum", root) != cached
    assert source.read_bytes() == b"replacement with a different size"


def test_kernel_claim_blocks_another_process_and_recovers(tmp_path):
    path = tmp_path / "claim.lock"
    claim = playback._claim(path)
    assert claim is not None
    script = "from pathlib import Path; from app.home_video_playback import _claim; import sys; lock=_claim(Path(sys.argv[1])); print('busy' if lock is None else 'acquired')"
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    try:
        result = subprocess.run([sys.executable, "-c", script, str(path)], env=environment,
                                capture_output=True, text=True, check=True, timeout=10)
        assert result.stdout.strip() == "busy"
    finally:
        claim.close()
    result = subprocess.run([sys.executable, "-c", script, str(path)], env=environment,
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == "acquired"


def test_global_slots_bound_admission(tmp_path, monkeypatch):
    source = tmp_path / "clip.mts"
    source.write_bytes(b"original")
    root = tmp_path / "cache"
    root.mkdir()
    slots = [playback._claim(root / f"worker-{i}.lock") for i in range(2)]
    calls = fake_media(monkeypatch)
    asset = uuid4()
    try:
        assert playback.playback_status(source, asset, "checksum", root) == "preparing"
        assert not calls
    finally:
        for slot in slots:
            slot.close()
    await_status(source, asset, root)
    assert len(calls) == 1


def configure_playback(client, tmp_path, suffix=".mts"):
    videos, _, _, store = configure_libraries(tmp_path)
    source = videos / ("historical" + suffix)
    source.write_bytes(b"canonical-original")
    asset = publish(store, videos, source)
    cache = tmp_path / "playback"
    app.dependency_overrides[playback.get_home_video_playback_cache_path] = lambda: cache
    authenticate(client)
    row = client.get("/api/personal-videos").json()[0]
    return source, asset, store, cache, row


@pytest.mark.parametrize("suffix,wanted", [(".mts", "ready"), (".mp4", "direct")])
def test_status_content_authorization_and_ranges(client, tmp_path, monkeypatch, suffix, wanted):
    calls = fake_media(monkeypatch)
    source, asset, store, cache, row = configure_playback(client, tmp_path, suffix)
    status_url = f"/api/personal-videos/{row['id']}/playback"
    content_url = status_url + "/content"
    assert client.get(content_url).status_code == 409
    assert client.get(status_url).json()["status"] == "preparing"
    # The route uses the authoritative checksum, not the test helper checksum.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(status_url)
        if response.json()["status"] != "preparing":
            break
        time.sleep(0.01)
    assert response.json()["status"] == wanted
    assert response.json()["playback_url"] == content_url
    assert str(tmp_path) not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    ranged = client.get(content_url, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    expected = b"canonical-original" if wanted == "direct" else b"synthetic-proxy-bytes"
    assert ranged.content == expected[:4]
    assert ranged.headers["content-type"] == "video/mp4"
    assert ranged.headers["content-range"] == f"bytes 0-3/{len(expected)}"
    assert ranged.headers["cache-control"] == "private, no-store"
    assert client.get(content_url, headers={"Range": "bytes=999999-"}).status_code == 416
    assert client.get(row["open_url"]).content == b"canonical-original"
    del store.catalogued_assets[asset.vault_path]
    for url in (status_url, content_url, row["open_url"]):
        assert client.get(url).status_code == 404
    publish(store, source.parent, source, owner_username="other")
    for url in (status_url, content_url):
        assert client.get(url).status_code == 404
    client.cookies.clear()
    assert client.get(status_url).status_code == 401
    assert client.get(content_url).status_code == 401
    assert len(calls) == (0 if wanted == "direct" else 1)


def test_failed_encode_is_controlled_retryable_and_preserves_library(client, tmp_path, monkeypatch):
    calls = fake_media(monkeypatch)
    source, asset, store, cache, row = configure_playback(client, tmp_path)
    original = source.read_bytes()
    original_converter = playback.subprocess.run
    def fail(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(playback.subprocess, "run", fail)
    status_url = f"/api/personal-videos/{row['id']}/playback"
    assert client.get(status_url).json()["status"] == "preparing"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(status_url)
        if response.json()["status"] == "failed":
            break
        time.sleep(0.01)
    assert response.json() == {"status": "failed", "playback_url": None}
    assert not list(cache.glob("*.partial.mp4"))
    assert client.get(status_url + "/content").status_code == 409
    assert source.read_bytes() == original
    assert client.get("/api/personal-videos").json()[0]["id"] == row["id"]
    monkeypatch.setattr(playback.subprocess, "run", original_converter)
    assert client.get(status_url + "?retry=true").json()["status"] == "preparing"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(status_url).json()["status"] == "ready":
            break
        time.sleep(0.01)
    assert client.get(status_url).json()["status"] == "ready"


REAL_MEDIA = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not REAL_MEDIA, reason="FFmpeg/ffprobe required")
@pytest.mark.parametrize("suffix,container,video,audio,wanted", [
    (".mp4", "mp4", "libx264", "aac", "direct"),
    (".mts", "mpegts", "libx264", "aac", "ready"),
    (".mkv", "matroska", "libx264", "ac3", "ready"),
    (".avi", "avi", "mpeg4", "pcm_s16le", "ready"),
    (".wmv", "asf", "wmv2", None, "ready"),
    (".mov", "mov", "libx265", "aac", "ready"),
    (".vob", "vob", "mpeg2video", "ac3", "ready"),
])
def test_real_media_end_to_end(client, tmp_path, monkeypatch, suffix, container, video, audio, wanted):
    source, asset, store, cache, row = configure_playback(client, tmp_path, suffix)
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:a", audio]
    command += ["-t", "0.5", "-c:v", video, "-pix_fmt", "yuv420p", "-f", container, str(source)]
    subprocess.run(command, check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    original = source.read_bytes()
    app.dependency_overrides[get_video_thumbnail_cache_path] = lambda: tmp_path / "thumbnails"
    assert client.get(row["thumbnail_url"]).status_code == 200
    status_url = f"/api/personal-videos/{row['id']}/playback"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.get(status_url).json()
        if result["status"] != "preparing":
            break
        time.sleep(0.05)
    assert result["status"] == wanted
    output = playback.playback_file(source, asset.id, asset.sha256, cache)
    assert output is not None
    assert playback.playback_plan(playback.probe_media(output)).mode == "direct"
    before = output.stat().st_mtime_ns
    assert client.get(status_url).json()["status"] == wanted
    assert output.stat().st_mtime_ns == before
    assert client.get(result["playback_url"], headers={"Range": "bytes=0-31"}).status_code == 206
    assert source.read_bytes() == original
    assert client.get("/api/personal-videos").json()[0]["id"] == row["id"]


@pytest.mark.skipif(not REAL_MEDIA, reason="FFmpeg/ffprobe required")
def test_real_malformed_input_fails_cleanly(tmp_path):
    source = tmp_path / "invalid.mts"
    source.write_bytes(b"not a video")
    await_status(source, uuid4(), tmp_path / "cache", "failed")
    assert source.read_bytes() == b"not a video"


def test_two_processes_share_one_preparation(tmp_path):
    source = tmp_path / "clip.mts"
    source.write_bytes(b"original")
    root = tmp_path / "cache"
    audit = tmp_path / "conversions.txt"
    asset = uuid4()
    script = r"""
import sys, time
from pathlib import Path
from uuid import UUID
from app import home_video_playback as p
from tests.test_home_video_playback import facts
source, root, audit = map(Path, sys.argv[1:4])
asset = UUID(sys.argv[4])
def probe(source, locks=()):
    time.sleep(0.1)
    return facts() if source.suffix == '.mp4' else facts('mpegts')
def convert(command, **kwargs):
    with audit.open('a') as f:
        f.write('conversion\n')
    time.sleep(0.2)
    Path(command[-1]).write_bytes(b'proxy')
p.probe_media = probe
p.subprocess.run = convert
for _ in range(300):
    state = p.playback_status(source, asset, 'checksum', root)
    if state != 'preparing':
        print(state)
        break
    time.sleep(0.02)
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    command = [sys.executable, "-c", script, str(source), str(root), str(audit), str(asset)]
    processes = [subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            assert stdout.strip() == "ready"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert audit.read_text().splitlines() == ["conversion"]


def test_replacement_during_encode_is_not_published(tmp_path, monkeypatch):
    source = tmp_path / "clip.mts"
    source.write_bytes(b"original")
    asset, root = uuid4(), tmp_path / "cache"
    fake_media(monkeypatch)
    original_converter = playback.subprocess.run
    def replace(command, **kwargs):
        original_converter(command, **kwargs)
        source.write_bytes(b"replacement source during encoding")
    monkeypatch.setattr(playback.subprocess, "run", replace)
    key = playback.source_key(source, asset, "checksum")
    playback.playback_status(source, asset, "checksum", root)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and playback._state(root, key).get("status") != "failed":
        time.sleep(0.01)
    assert playback._state(root, key)["status"] == "failed"
    assert not (root / f"{key}.mp4").exists()
    assert playback.playback_file(source, asset, "checksum", root) is None


def test_probe_output_is_bounded(tmp_path, monkeypatch):
    original_popen = subprocess.Popen
    def noisy_probe(command, **kwargs):
        return original_popen([sys.executable, "-c", "print('x' * 1000000)"], **kwargs)
    monkeypatch.setattr(playback.subprocess, "Popen", noisy_probe)
    with pytest.raises(ValueError, match="inspection failed"):
        playback.probe_media(tmp_path / "clip.mts")


def test_admission_failure_releases_claim(tmp_path, monkeypatch):
    source = tmp_path / "clip.mts"
    source.write_bytes(b"source")
    asset, root = uuid4(), tmp_path / "cache"
    fake_media(monkeypatch)
    original_claim = playback._claim
    def fail_slot(path):
        if path.name.startswith("worker-"):
            raise OSError("unavailable")
        return original_claim(path)
    monkeypatch.setattr(playback, "_claim", fail_slot)
    assert playback.playback_status(source, asset, "checksum", root) == "failed"
    monkeypatch.setattr(playback, "_claim", original_claim)
    await_status(source, asset, root)
