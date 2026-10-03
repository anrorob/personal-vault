"""Rebuildable Home Video stills, independent of intelligence jobs."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from uuid import UUID

_LOCKS = tuple(threading.Lock() for _ in range(32))
_GENERATORS = threading.BoundedSemaphore(2)
MAX_JPEG_BYTES = 2 * 1024 * 1024
FAILURE_RETRY_SECONDS = 60


def get_video_thumbnail_cache_path() -> Path:
    return Path(os.getenv(
        "PV_VIDEO_THUMBNAIL_CACHE_PATH",
        "/var/cache/personal-vault/video-thumbnails",
    ))


def _source_identity(source: Path) -> str:
    stat = source.stat()
    return f"{source.resolve()}:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def _valid_jpeg(path: Path) -> bool:
    if not path.is_file() or not 4 <= path.stat().st_size <= MAX_JPEG_BYTES:
        return False
    # FFmpeg is the producer. Reject empty/truncated cache entries without
    # introducing an image metadata dependency into library presentation.
    with path.open("rb") as frame:
        start = frame.read(2)
        frame.seek(-2, os.SEEK_END)
        return start == b"\xff\xd8" and frame.read(2) == b"\xff\xd9"


def video_thumbnail(source: Path, asset_id: UUID, cache_root: Path) -> Path | None:
    """Caller must authorize the source before even checking this cache."""
    try:
        identity = _source_identity(source)
        key = hashlib.sha256(f"v1:{asset_id}:{identity}".encode()).hexdigest()
        cache_root.mkdir(parents=True, exist_ok=True)
        cached = cache_root / f"{key}.jpg"
        failed = cache_root / f"{key}.failed"
        with _LOCKS[int(key[:8], 16) % len(_LOCKS)]:
            if _valid_jpeg(cached):
                return cached
            if failed.exists() and time.time() - failed.stat().st_mtime < FAILURE_RETRY_SECONDS:
                return None
            # Unique temporary output and atomic publication also protect
            # readers when separate backend processes generate the same still.
            with _GENERATORS, tempfile.TemporaryDirectory(dir=cache_root) as temporary:
                output = Path(temporary) / "frame.jpg"
                for seconds in (1, 0):
                    output.unlink(missing_ok=True)
                    try:
                        subprocess.run(
                            ["ffmpeg", "-hide_banner", "-loglevel", "error",
                             "-nostdin", "-y", "-protocol_whitelist", "file,pipe",
                             "-threads", "1", "-ss", str(seconds), "-i", str(source.resolve()),
                             "-map", "0:v:0", "-an", "-sn", "-dn",
                             "-vf", "scale=640:360:force_original_aspect_ratio=decrease",
                             "-frames:v", "1", "-threads", "1", "-q:v", "3",
                             "-fs", str(MAX_JPEG_BYTES), str(output)],
                            check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=15,
                        )
                        if _valid_jpeg(output) and _source_identity(source) == identity:
                            output.replace(cached)
                            failed.unlink(missing_ok=True)
                            return cached
                    except (OSError, subprocess.SubprocessError):
                        continue
            failed.touch()
    except OSError:
        return None
    return None
