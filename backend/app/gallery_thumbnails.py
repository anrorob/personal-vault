"""Rebuildable Gallery card images, never canonical media."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from app.vault_master import CataloguedAsset
from app.vault_master_ingestion_ai import render_gallery_pdf_preview


THUMBNAIL_VERSION = 1
THUMBNAIL_EDGE = 640
MAX_JPEG_BYTES = 1024 * 1024
FAILURE_RETRY_SECONDS = 60
_LOCKS = tuple(threading.Lock() for _ in range(32))
_GENERATORS = threading.BoundedSemaphore(2)


def get_gallery_thumbnail_cache_path() -> Path:
    return Path(os.getenv(
        "PV_GALLERY_THUMBNAIL_CACHE_PATH", "/vault/.cache/gallery-thumbnails",
    ))


def _source_identity(source: Path, asset: CataloguedAsset) -> str:
    stat = source.stat()
    return (
        f"v{THUMBNAIL_VERSION}:{asset.owner_user_id}:{asset.id}:{asset.sha256}:"
        f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
    )


def _valid_jpeg(path: Path) -> bool:
    if not path.is_file() or not 4 <= path.stat().st_size <= MAX_JPEG_BYTES:
        return False
    with path.open("rb") as image:
        start = image.read(2)
        image.seek(-2, os.SEEK_END)
        return start == b"\xff\xd8" and image.read(2) == b"\xff\xd9"


def _render_pdf(source: Path, output: Path) -> None:
    output.write_bytes(render_gallery_pdf_preview(source, max_edge=THUMBNAIL_EDGE))


def _render_image(source: Path, output: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-protocol_whitelist", "file,pipe", "-threads", "1", "-i", str(source),
         "-map", "0:v:0", "-an", "-sn", "-dn",
         "-vf", f"scale={THUMBNAIL_EDGE}:{THUMBNAIL_EDGE}:force_original_aspect_ratio=decrease",
         "-frames:v", "1", "-threads", "1", "-q:v", "5",
         "-f", "image2", str(output)],
        check=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
    )


def gallery_thumbnail(
    source: Path, asset: CataloguedAsset, cache_root: Path,
) -> tuple[Path, str] | None:
    """Caller must authorize the asset and contain its source before calling."""
    try:
        identity = _source_identity(source, asset)
        key = hashlib.sha256(identity.encode()).hexdigest()
        cache_dir = cache_root / str(asset.owner_user_id) / str(asset.id)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{key}.jpg"
        failed = cache_dir / f"{key}.failed"
        with _LOCKS[int(key[:8], 16) % len(_LOCKS)]:
            if _valid_jpeg(cached):
                return cached, key
            if failed.exists() and time.time() - failed.stat().st_mtime < FAILURE_RETRY_SECONDS:
                return None
            with _GENERATORS, tempfile.TemporaryDirectory(dir=cache_dir) as temporary:
                output = Path(temporary) / "preview.jpg"
                try:
                    if asset.mime_type == "application/pdf":
                        _render_pdf(source, output)
                    elif asset.mime_type.startswith("image/"):
                        _render_image(source, output)
                    else:
                        return None
                    if _valid_jpeg(output) and _source_identity(source, asset) == identity:
                        output.replace(cached)
                        failed.unlink(missing_ok=True)
                        return cached, key
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            failed.touch()
    except OSError:
        return None
    return None
