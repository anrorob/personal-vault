from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest
from PIL import Image

from app.main import app
from app.media_formats import VIDEO_EXTENSIONS, BROWSER_INLINE_VIDEO_EXTENSIONS
from app.vault_libraries import scan_vault_library
from app.vault_master import detected_mime_type
from app.video_thumbnails import get_video_thumbnail_cache_path, video_thumbnail
from tests.test_vault_libraries import configure_libraries, catalogue_file, authenticate


def publish(store, root, path, **kwargs):
    return catalogue_file(store, root, path, asset_type="Home Videos",
                          vault_root="/vault/Home Videos", mime_type=detected_mime_type(path), **kwargs)


@pytest.mark.parametrize("suffix", sorted(VIDEO_EXTENSIONS) + [".MTS", ".MoV", ".MP4"])
def test_existing_video_formats_are_visible(client, tmp_path, suffix):
    videos, _, _, store = configure_libraries(tmp_path)
    source = videos / ("historical" + suffix)
    source.write_bytes(b"original")
    asset = publish(store, videos, source)
    (videos / "uncatalogued.mts").write_bytes(b"hidden")
    random = videos / "random.xyz"
    random.write_bytes(b"not video")
    publish(store, videos, random)
    private = videos / "private.mts"
    private.write_bytes(b"private")
    publish(store, videos, private, owner_username="other")
    authenticate(client)
    response = client.get("/api/personal-videos")
    assert response.status_code == 200
    assert [row["name"] for row in response.json()] == [source.name]
    row = response.json()[0]
    assert row["opens_inline"] == (suffix.casefold() in BROWSER_INLINE_VIDEO_EXTENSIONS)
    assert row["thumbnail_url"].endswith("/thumbnail")
    assert detected_mime_type(source).startswith("video/")
    assert store.catalogued_assets[asset.vault_path].id == asset.id
    assert str(tmp_path) not in response.text
    content = client.get(row["open_url"])
    assert content.status_code == 200
    disposition = "inline" if row["opens_inline"] else "attachment"
    assert content.headers["content-disposition"].startswith(disposition)


def test_thumbnail_authorization_cache_replacement_and_failure(client, tmp_path, monkeypatch):
    videos, _, _, store = configure_libraries(tmp_path)
    source = videos / "historical.mts"
    source.write_bytes(b"original")
    original_stat = source.stat()
    asset = publish(store, videos, source)
    cache = tmp_path / "derived"
    app.dependency_overrides[get_video_thumbnail_cache_path] = lambda: cache
    calls = []

    def generate(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 15
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert not kwargs.get("shell")
        Image.new("RGB", (320, 180), "blue").save(command[-1], "JPEG")

    monkeypatch.setattr("app.video_thumbnails.subprocess.run", generate)
    entry = scan_vault_library(videos)[0]
    url = f"/api/personal-videos/{entry.id}/thumbnail"
    assert client.get(url).status_code == 401
    authenticate(client)
    assert client.get(url).status_code == 200
    assert client.get(url).headers["cache-control"] == "private, no-store"
    assert len(calls) == 1
    assert source.read_bytes() == b"original"
    assert source.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert list(videos.iterdir()) == [source]
    # Authorization is checked even for a warm cache, after revocation/removal.
    del store.catalogued_assets[asset.vault_path]
    assert client.get(url).status_code == 404
    assert len(calls) == 1
    publish(store, videos, source, owner_username="other")
    assert client.get(url).status_code == 404
    store.catalogued_assets[asset.vault_path] = asset
    source.write_bytes(b"replaced original video")
    assert client.get(url).status_code == 200
    assert len(calls) == 2

    def fail(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("ffmpeg", 15)

    monkeypatch.setattr("app.video_thumbnails.subprocess.run", fail)
    source.write_bytes(b"undecodable replacement with a different size")
    assert client.get(url).status_code == 503
    count = len(calls)
    assert client.get(url).status_code == 503
    assert len(calls) == count  # brief negative cache prevents retry storms
    assert client.get("/api/personal-videos").status_code == 200


def test_short_clip_fallback_and_corrupt_cache(tmp_path, monkeypatch):
    source = tmp_path / "short.mkv"
    source.write_bytes(b"source")
    calls = []
    def generate(command, **kwargs):
        calls.append(command)
        if command[command.index("-ss") + 1] == "0":
            Image.new("RGB", (160, 90), "green").save(command[-1], "JPEG")
    monkeypatch.setattr("app.video_thumbnails.subprocess.run", generate)
    asset_id = uuid4()
    cached = video_thumbnail(source, asset_id, tmp_path / "cache")
    assert cached is not None
    assert len(calls) == 2
    cached.write_bytes(b"corrupt")
    assert video_thumbnail(source, asset_id, tmp_path / "cache") == cached
    assert len(calls) == 4
    # Same filename/stats in a different root cannot alias cached content.
    other = tmp_path / "other" / source.name
    other.parent.mkdir()
    other.write_bytes(source.read_bytes())
    assert video_thumbnail(other, asset_id, tmp_path / "cache") != cached


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed locally")
@pytest.mark.parametrize("suffix,container,codec", [
    (".3gp", "3gp", "mpeg4"), (".3g2", "3g2", "mpeg4"),
    (".avi", "avi", "mpeg4"), (".flv", "flv", "flv"),
    (".m2ts", "mpegts", "mpeg2video"), (".m4v", "mp4", "mpeg4"),
    (".mkv", "matroska", "mpeg4"), (".mov", "mov", "mpeg4"),
    (".mp4", "mp4", "mpeg4"), (".mpeg", "mpeg", "mpeg2video"),
    (".mpg", "mpeg", "mpeg2video"), (".mts", "mpegts", "mpeg2video"),
    (".ts", "mpegts", "mpeg2video"), (".vob", "vob", "mpeg2video"),
    (".webm", "webm", "libvpx"), (".wmv", "asf", "wmv2"),
])
def test_real_ffmpeg_existing_short_clip(tmp_path, suffix, container, codec):
    source = tmp_path / ("generated" + suffix)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=160x90:r=25", "-t", "0.4", "-c:v", codec,
                    "-f", container, str(source)], check=True, timeout=15)
    original = source.read_bytes()
    result = video_thumbnail(source, uuid4(), tmp_path / "cache")
    assert result is not None
    with Image.open(result) as still:
        assert still.format == "JPEG"
    assert source.read_bytes() == original
