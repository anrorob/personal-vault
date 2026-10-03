"""Bounded source-associated FLAC cache. Never canonical/catalogue authority."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time

from app.home_video_playback import _claim, _subprocess_options

VERSION = "music-lossless-flac-v1"
DEMUXERS = "aac,flac,mov,mp3,ogg,wav,asf"
MAX_CACHE_BYTES = 2 * 1024**3
MAX_ITEM_BYTES = 512 * 1024**2
MAX_ITEMS = 64
MAX_AGE_SECONDS = 7 * 24 * 3600


def get_cache_root() -> Path:
    return Path(os.getenv("PV_MUSIC_PLAYBACK_CACHE_PATH", "/var/cache/personal-vault/music-playback"))


def source_key(source, asset_id, checksum):
    stat = source.stat()
    values = [VERSION, str(asset_id), checksum, str(source.resolve()), stat.st_dev,
              stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def probe(source, locks=()):
    # Select one audio stream and fixed scalar facts only: no tags/descriptions.
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
         "-format_whitelist", DEMUXERS, "-select_streams", "a:0", "-show_entries",
         "format=format_name:stream=codec_name,sample_fmt,sample_rate,channels,bits_per_raw_sample",
         "-of", "json", str(source.resolve())],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=30, check=True, **_subprocess_options(locks))
    if len(result.stdout) > 4096:
        raise ValueError("Invalid audio probe")
    data = json.loads(result.stdout)
    audio = data.get("streams", [])
    if len(audio) != 1:
        raise ValueError("Audio stream missing")
    return data["format"]["format_name"].split(","), audio[0]


def direct_mime(formats, audio):
    codec = audio["codec_name"]
    if "mp3" in formats and codec == "mp3":
        return "audio/mpeg"
    if "flac" in formats and codec == "flac":
        return "audio/flac"
    if "wav" in formats and codec in {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"}:
        return "audio/wav"
    if "mov" in formats and codec == "aac":
        return "audio/mp4"
    if "aac" in formats and codec == "aac":
        return "audio/aac"
    return None


def _lock_path(root, key):
    # Fixed lock stripes avoid accumulating per-source lock files. Never unlink
    # a lock inode: another process could already be waiting on that inode.
    return root / f"reader-{int(key[:8], 16) % 64}.lock"


def _wait_claim(path):
    deadline = time.monotonic() + 10
    while True:
        claim = _claim(path)
        if claim is not None:
            return claim
        if time.monotonic() >= deadline:
            raise RuntimeError("Playback cache busy")
        time.sleep(0.05)


def _evict(root, keep, reserve=0):
    entries = sorted((p for p in root.glob("*.flac")
                      if re.fullmatch(r"[0-9a-f]{64}\.flac", p.name)),
                     key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in entries)
    count = len(entries)
    for path in entries:
        stat = path.stat()
        if path.stem == keep:
            continue
        if (time.time() - stat.st_mtime < MAX_AGE_SECONDS
                and total + reserve <= MAX_CACHE_BYTES and count < MAX_ITEMS):
            continue
        lock = _claim(_lock_path(root, path.stem))
        if lock is None:
            continue  # A live response/preparer pins this stripe.
        try:
            path.unlink(missing_ok=True)
            total -= stat.st_size
            count -= 1
        finally:
            lock.close()
    if total + reserve > MAX_CACHE_BYTES or count >= MAX_ITEMS:
        raise RuntimeError("Playback cache capacity busy")


def playback_file(source, asset_id, checksum, root):
    claim = None
    try:
        key = source_key(source, asset_id, checksum)
        formats, audio = probe(source)
        if source_key(source, asset_id, checksum) != key:
            raise ValueError("Source changed during inspection")
        mime = direct_mime(formats, audio)
        if mime:
            return source, mime, None
        # FLAC must not truncate lossless precision, resample or downmix. Reject
        # representations outside the encoder's lossless integer contract.
        bits = int(audio.get("bits_per_raw_sample") or 0)
        if not bits and audio.get("sample_fmt", "").startswith("s16"):
            bits = 16
        if audio["codec_name"] in {"wmalossless", "alac"} and not bits:
            raise ValueError("Unknown lossless source precision")
        if bits > 24 or int(audio["channels"]) > 8:
            raise ValueError("Source precision is not supported losslessly")
        if source.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(source.parent.resolve()):
            raise ValueError("Playback cache must be separate from canonical source storage")
        root.mkdir(parents=True, exist_ok=True)
        claim = _wait_claim(_lock_path(root, key))
        output = root / f"{key}.flac"
        maintenance = _wait_claim(root / "maintenance.lock")
        try:
            _evict(root, key, 0 if output.exists() else MAX_ITEM_BYTES)
            if not output.exists():
                # Serial preparation bounds CPU/disk admission across processes.
                # Children retain kernel locks if their web worker dies.
                for partial in root.glob("pv-music-*.partial"):
                    partial.unlink(missing_ok=True)
                with tempfile.NamedTemporaryFile(dir=root, prefix="pv-music-", suffix=".partial", delete=False) as f:
                    partial = Path(f.name)
                try:
                    subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                         "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
                         "-threads", "2", "-i", str(source.resolve()), "-map", "0:a:0",
                         "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                         "-c:a", "flac", "-threads", "2", "-fs", str(MAX_ITEM_BYTES),
                         "-f", "flac", str(partial)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=120, check=True, **_subprocess_options((claim, maintenance)))
                    _, prepared = probe(partial, (claim, maintenance))
                    if (prepared["codec_name"] != "flac"
                            or prepared["sample_rate"] != audio["sample_rate"]
                            or prepared["channels"] != audio["channels"]
                            or int(prepared.get("bits_per_raw_sample") or 0) < bits
                            or not 0 < partial.stat().st_size < MAX_ITEM_BYTES):
                        raise ValueError("Lossless preparation validation failed")
                    if source_key(source, asset_id, checksum) != key:
                        raise ValueError("Source changed during preparation")
                    partial.replace(output)
                finally:
                    partial.unlink(missing_ok=True)
            output.touch()  # Cache access age only; never touch the canonical file.
        finally:
            maintenance.close()
        return output, "audio/flac", claim
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        if claim is not None:
            claim.close()
        raise ValueError("Lossless playback preparation failed") from None
