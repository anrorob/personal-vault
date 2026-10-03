"""Shared stdlib-only contract installed alongside the privileged publisher.

The lock inode is permanent: never unlink it. Cancellation is durable before
staging cleanup, and every publisher checks it while holding the same lock.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4

VERSION = "arrival-cancellation-v1"
_mutex = threading.RLock()
_local = threading.local()


@contextmanager
def publication_lock(queue: Path | None = None, *, timeout: float | None = 10):
    with _mutex:
        configured = os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE")
        queue = queue or (Path(configured) if configured else None)
        if queue is None:
            default = Path("/var/lib/personal-vault/arrival-managed-requests")
            if (default / ".coordination-version").is_file():
                queue = default
        lock_key = str(queue.absolute()) if queue is not None else None
        previous = getattr(_local, "held_keys", ())
        if lock_key in previous:
            yield
            return
        descriptor = None
        try:
            if queue is not None:
                if not queue.is_symlink():
                    queue.mkdir(mode=0o700, parents=True, exist_ok=True)
                if queue.is_symlink() or not queue.is_dir():
                    raise ValueError("Publication coordination is unavailable. Recheck later.")
                path = queue / ".publication.lock"
                if path.is_symlink():
                    raise ValueError("Unsafe publication coordination file")
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                deadline = time.monotonic() + timeout if timeout is not None else None
                while True:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            os.lseek(descriptor, 0, 0)
                            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise ValueError("Publication is busy. Recheck shortly.") from None
                        time.sleep(0.05)
            _local.held_keys = (*previous, lock_key)
            yield
        finally:
            _local.held_keys = previous
            if descriptor is not None:
                os.close(descriptor)


def serialized_publication(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with publication_lock():
            return function(*args, **kwargs)
    return wrapped


def cancellation_path(queue: Path, item_id: object) -> Path:
    return queue / f"{UUID(str(item_id))}.cancelled"


def cancellation(queue: Path, item_id: object, key: bytes) -> dict | None:
    path = cancellation_path(queue, item_id)
    if path.is_symlink():
        raise ValueError("Unsafe cancellation evidence")
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    value = document.get("cancellation")
    if not isinstance(value, dict) or value.get("version") != VERSION or value.get("item_id") != str(item_id):
        raise ValueError("Invalid cancellation evidence")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = document.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, hmac.new(key, encoded, hashlib.sha256).hexdigest()):
        raise ValueError("Invalid cancellation signature")
    return value


def cancel(queue: Path, value: dict, key: bytes) -> None:
    """Caller holds publication_lock and has proved absence of publication."""
    if cancellation(queue, value["item_id"], key) is not None:
        return
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    document = json.dumps({"cancellation": value, "signature": hmac.new(key, payload, hashlib.sha256).hexdigest()}).encode()
    target = cancellation_path(queue, value["item_id"])
    temporary = target.with_suffix(".cancellation-writing")
    # An interrupted temporary write is not authority. The caller has rechecked
    # non-publication under the lock; archive it before making a fresh attempt.
    if temporary.is_symlink():
        raise ValueError("Unsafe interrupted cancellation evidence")
    if temporary.exists():
        os.replace(temporary, queue / f"{value['item_id']}.{uuid4()}.interrupted-cancellation")
    with temporary.open("xb") as stream:
        stream.write(document)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    if os.name != "nt":
        fd = os.open(queue, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
