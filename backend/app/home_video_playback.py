"""Disposable playback derivatives; never catalogue or intelligence authority."""

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import BinaryIO
from uuid import UUID

logger = logging.getLogger(__name__)
VERSION = "home-video-playback-v1"
MAX_PROBE_BYTES = 64 * 1024
CONVERSION_TIMEOUT = 6 * 60 * 60
# Exclude playlists/concat inputs which could reference unrelated local media.
DEMUXERS = "mov,matroska,webm,avi,flv,mpegts,mpeg,asf"


def get_home_video_playback_cache_path() -> Path:
    return Path(os.getenv("PV_HOME_VIDEO_PLAYBACK_CACHE_PATH",
                          "/var/cache/personal-vault/home-video-playback"))


@dataclass(frozen=True)
class PlaybackPlan:
    mode: str
    video_index: int
    audio_index: int | None
    copy_video: bool
    copy_audio: bool


def playback_plan(probe: dict) -> PlaybackPlan:
    streams = probe.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    duration = float(probe.get("format", {}).get("duration", 0))
    if not videos or not math.isfinite(duration) or duration <= 0:
        raise ValueError("No finite video stream")
    video, audio = videos[0], audios[0] if audios else None
    copy_video = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and video.get("profile") in {"Constrained Baseline", "Baseline", "Main", "High"}
        and 0 < int(video.get("level", 0)) <= 51
        and 0 < int(video.get("width", 0)) <= 3840
        and 0 < int(video.get("height", 0)) <= 2160
        and video.get("field_order") in {None, "unknown", "progressive"}
    )
    copy_audio = audio is None or (
        audio.get("codec_name") == "aac" and audio.get("profile") == "LC"
        and 0 < int(audio.get("channels", 0)) <= 2
        and 0 < int(audio.get("sample_rate", 0)) <= 48000
    )
    container = probe.get("format", {})
    brand = container.get("tags", {}).get("major_brand", "").strip()
    direct = (
        copy_video and copy_audio
        and "mp4" in container.get("format_name", "").split(",")
        and brand in {"isom", "iso2", "mp41", "mp42", "avc1", "M4V"}
        and video.get("codec_tag_string") == "avc1"
        and len(streams) == 1 + len(audios) and len(audios) <= 1
    )
    mode = "direct" if direct else "remux" if copy_video and copy_audio else "transcode"
    return PlaybackPlan(mode, int(video["index"]),
                        int(audio["index"]) if audio else None, copy_video, copy_audio)


def _subprocess_options(locks: tuple[BinaryIO, ...]) -> dict:
    # On Linux the child retains the claims if the web worker dies mid-encode.
    # Kernel locks release when the last descriptor closes, without stale PIDs.
    return {"pass_fds": tuple(lock.fileno() for lock in locks)} if os.name != "nt" else {}


def probe_media(source: Path, locks: tuple[BinaryIO, ...] = ()) -> dict:
    command = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
               "-format_whitelist", DEMUXERS, "-show_entries",
               "format=format_name,duration:format_tags=major_brand:stream=index,codec_type,codec_name,pix_fmt,profile,level,channels,sample_rate,codec_tag_string,width,height,field_order:stream_disposition=attached_pic",
               "-of", "json", "--", str(source.resolve())]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, **_subprocess_options(locks)) as process:
        data = bytearray()

        def read_output() -> None:
            assert process.stdout is not None
            while chunk := process.stdout.read(4096):
                data.extend(chunk[:max(0, MAX_PROBE_BYTES + 1 - len(data))])
                if len(data) > MAX_PROBE_BYTES:
                    process.kill()
                    break

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join()
        if process.returncode or len(data) > MAX_PROBE_BYTES:
            raise ValueError("Media inspection failed")
        return json.loads(data)


def conversion_command(source: Path, output: Path, plan: PlaybackPlan) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
               "-threads", "2", "-i", str(source.resolve()),
               "-map", f"0:{plan.video_index}"]
    if plan.audio_index is not None:
        command += ["-map", f"0:{plan.audio_index}"]
    command += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
    if plan.copy_video:
        command += ["-c:v", "copy"]
    else:
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.1",
                    "-vf", "bwdif=mode=send_frame:deint=interlaced,scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                    "-fpsmax", "30", "-threads", "2"]
    if plan.audio_index is not None:
        command += (["-c:a", "copy"] if plan.copy_audio else
                    ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"])
    else:
        command += ["-an"]
    return command + ["-tag:v", "avc1", "-movflags", "+faststart", "-f", "mp4", str(output)]


def source_key(source: Path, asset_id: UUID, checksum: str) -> str:
    stat = source.stat()
    identity = [VERSION, str(asset_id), checksum, str(source.resolve()), stat.st_dev,
                stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _claim(path: Path) -> BinaryIO | None:
    lock = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except OSError:
        lock.close()
        return None


def _state(root: Path, key: str) -> dict:
    try:
        path = root / f"{key}.json"
        if path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text())
        if value.get("status") == "ready":
            stat = (root / f"{key}.mp4").stat()
            if stat.st_size != value["size"] or stat.st_mtime_ns != value["mtime_ns"]:
                return {}
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _write_state(root: Path, key: str, state: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as temporary:
        path = Path(temporary.name)
        json.dump(state, temporary)
    try:
        # Windows readers may briefly hold a handle without delete sharing.
        # Retry the atomic rename, never fall back to an in-place JSON write.
        for attempt in range(20):
            try:
                path.replace(root / f"{key}.json")
                break
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.01)
    finally:
        path.unlink(missing_ok=True)


def _prepare(source: Path, asset_id: UUID, checksum: str, root: Path, key: str,
             locks: tuple[BinaryIO, ...]) -> None:
    try:
        plan = playback_plan(probe_media(source, locks))
        if plan.mode == "direct":
            state = {"status": "direct"}
        else:
            # Remove only this key's abandoned partial outputs after obtaining
            # its exclusive claim. No incomplete file is ever a serving target.
            for stale in root.glob(f"{key}.*.partial.mp4"):
                stale.unlink(missing_ok=True)
            with tempfile.NamedTemporaryFile(prefix=f"{key}.", suffix=".partial.mp4",
                                             dir=root, delete=False) as temporary:
                partial = Path(temporary.name)
            try:
                subprocess.run(conversion_command(source, partial, plan),
                               check=True, timeout=CONVERSION_TIMEOUT,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **_subprocess_options(locks))
                if playback_plan(probe_media(partial, locks)).mode != "direct":
                    raise ValueError("Proxy validation failed")
                if source_key(source, asset_id, checksum) != key:
                    raise ValueError("Source changed during preparation")
                output = root / f"{key}.mp4"
                partial.replace(output)
                stat = output.stat()
                state = {"status": "ready", "size": stat.st_size,
                         "mtime_ns": stat.st_mtime_ns}
            finally:
                partial.unlink(missing_ok=True)
        if source_key(source, asset_id, checksum) != key:
            raise ValueError("Source changed during preparation")
        _write_state(root, key, state)
    except Exception as error:
        # Do not log raw subprocess commands, source paths or media metadata.
        logger.warning("Home Video playback preparation failed (%s)", type(error).__name__)
        try:
            _write_state(root, key, {"status": "failed", "at": time.time()})
        except OSError:
            pass
    finally:
        for lock in locks:
            lock.close()


def playback_status(source: Path, asset_id: UUID, checksum: str, root: Path,
                    *, retry: bool = False) -> str:
    """Nonblocking polling/claiming. Caller authorizes before each invocation."""
    try:
        key = source_key(source, asset_id, checksum)
        root.mkdir(parents=True, exist_ok=True)
        state = _state(root, key)
        if state.get("status") in {"direct", "ready"}:
            return state["status"]
        if state.get("status") == "failed" and not retry:
            return "failed"
        claim = _claim(root / f"{key}.lock")
        if claim is None:
            return "preparing"
        handed_off = False
        slot = None
        try:
            # Recheck after acquisition: another process may have completed.
            state = _state(root, key)
            if state.get("status") in {"direct", "ready"}:
                return state["status"]
            for index in range(2):
                slot = _claim(root / f"worker-{index}.lock")
                if slot is not None:
                    break
            if slot is None:
                return "preparing"  # Polling retries admission; no unbounded queue.
            _write_state(root, key, {"status": "preparing"})
            threading.Thread(target=_prepare, args=(source, asset_id, checksum, root, key,
                                                    (claim, slot)), daemon=True).start()
            handed_off = True
            return "preparing"
        finally:
            if not handed_off:
                claim.close()
                if slot is not None:
                    slot.close()
    except (OSError, RuntimeError):
        return "failed"



def playback_file(source: Path, asset_id: UUID, checksum: str, root: Path) -> Path | None:
    """Read-only serving resolution; never starts preparation."""
    try:
        key = source_key(source, asset_id, checksum)
        state = _state(root, key).get("status")
        if state == "direct":
            return source
        if state == "ready":
            return root / f"{key}.mp4"
    except OSError:
        pass
    return None
