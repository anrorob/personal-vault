from datetime import datetime, timezone
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import json
import threading
from urllib.parse import unquote
from uuid import UUID, uuid4

import anyio
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import AuthenticatedUsername, get_authentication_store
from app.auth_store import AuthenticationStore
from app.config import get_upload_max_bytes


router = APIRouter(tags=["arrival-hall"])
logger = logging.getLogger("pv.incoming")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
MAX_FILENAME_BYTES = 255
MAX_ORIGINAL_MODIFIED_MILLISECONDS = 4_102_444_800_000
OWNER_MANIFEST_FILENAME = ".pv-arrival-hall-owners.json"
_owner_manifest_lock = threading.RLock()


class ArrivalHallFile(BaseModel):
    name: str
    relative_path: str
    folder: str | None
    size: int
    uploaded_at: datetime


class ArrivalHallListing(BaseModel):
    files: list[ArrivalHallFile]
    max_upload_bytes: int


class UploadResult(BaseModel):
    status: str
    original_name: str
    stored_name: str
    size: int


PREVIEW_MIME_PREFIXES = ("image/", "video/", "application/pdf")


def require_intake_admission(request: Request | None = None) -> None:
    """Reject a new arrival-hall transfer when the persistent gate is not Open."""
    # Delayed import avoids a module cycle: the existing Intake store imports
    # Arrival Hall helpers for its authenticated source endpoint.
    from app.vault_master_intake import get_intake_store

    try:
        provider = (
            request.app.dependency_overrides.get(get_intake_store, get_intake_store)
            if request is not None
            else get_intake_store
        )
        provider().begin_transfer()
    except Exception as error:
        from app.vault_master_intake import IntakeRejected
        if isinstance(error, IntakeRejected):
            raise HTTPException(status_code=error.code, detail=error.reason, headers={"Retry-After": str(error.retry_after or 60)}) from error
        raise


def _validated_relative_parts(raw_path: str) -> tuple[str, ...]:
    try:
        decoded_path = unquote(raw_path, errors="strict")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Arrival Hall path is invalid",
        ) from error

    parts = tuple(decoded_path.split("/"))
    if (
        not decoded_path
        or decoded_path.startswith("/")
        or "\\" in decoded_path
        or any(
            not part
            or part in {".", ".."}
            or part != part.strip()
            or CONTROL_CHARACTERS.search(part)
            or part.startswith(".pv-upload-")
            or len(part.encode("utf-8")) > MAX_FILENAME_BYTES
            for part in parts
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Arrival Hall path is invalid",
        )
    return parts


def resolve_arrival_hall_file(
    incoming_path: Path,
    raw_path: str,
) -> Path:
    parts = _validated_relative_parts(raw_path)
    candidate = incoming_path.joinpath(*parts)
    current = incoming_path
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Arrival Hall preview was not found",
            )

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Arrival Hall preview was not found",
        ) from error
    if not resolved.is_relative_to(incoming_path) or not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Arrival Hall preview was not found",
        )
    return resolved


def get_arrival_hall_path() -> Path:
    configured_path = os.getenv("PV_ARRIVAL_HALL_PATH")
    if configured_path:
        return Path(configured_path)

    return Path(os.getenv("PV_INCOMING_PATH", "/vault/Arrival Hall"))


def get_incoming_path() -> Path:
    """Compatibility dependency for callers using the previous name."""
    return get_arrival_hall_path()


def require_incoming_path(
    incoming_path: Path = Depends(get_incoming_path),
) -> Path:
    try:
        resolved_path = incoming_path.resolve(strict=True)
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrival Hall storage is unavailable",
        ) from error

    if not resolved_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrival Hall storage is unavailable",
        )

    if not os.access(resolved_path, os.W_OK):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrival Hall storage is not writable",
        )

    return resolved_path


def validate_filename(raw_filename: str | None) -> str:
    if not raw_filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload filename is required",
        )

    try:
        filename = unquote(raw_filename, errors="strict")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload filename is invalid",
        ) from error

    if (
        not filename
        or filename in {".", ".."}
        or filename != filename.strip()
        or "/" in filename
        or "\\" in filename
        or CONTROL_CHARACTERS.search(filename)
        or filename.startswith(".pv-upload-")
        or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload filename is invalid",
        )

    return filename


def parse_original_modified(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    try:
        milliseconds = float(raw_value)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Original file timestamp is invalid",
        ) from error
    if (
        not math.isfinite(milliseconds)
        or milliseconds < 0
        or milliseconds > MAX_ORIGINAL_MODIFIED_MILLISECONDS
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Original file timestamp is invalid",
        )
    return milliseconds / 1000


def get_available_name(
    filename: str,
    attempt: int,
) -> str:
    if attempt == 0:
        return filename

    path = Path(filename)
    return f"{path.stem} ({attempt}){path.suffix}"


def publish_without_overwriting(
    temporary_path: Path,
    incoming_path: Path,
    filename: str,
) -> Path:
    for attempt in range(10_000):
        destination = incoming_path / get_available_name(
            filename,
            attempt,
        )

        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            continue

        temporary_path.unlink()
        return destination

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A safe destination filename could not be allocated",
    )


def _owner_manifest_path(incoming_path: Path) -> Path:
    return incoming_path / OWNER_MANIFEST_FILENAME


def _read_owner_manifest(incoming_path: Path) -> dict[str, object]:
    manifest_path = _owner_manifest_path(incoming_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Arrival Hall owner manifest could not be read")
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        relative_path: owner
        for relative_path, owner in raw.items()
        if isinstance(relative_path, str)
        and (
            (isinstance(owner, str) and owner)
            or isinstance(owner, dict)
        )
    }


def record_arrival_hall_file_owner(
    incoming_path: Path,
    destination: Path,
    username: object,
) -> None:
    """Persist the authenticated uploader for a newly published file."""
    relative_path = destination.relative_to(incoming_path).as_posix()
    manifest_path = _owner_manifest_path(incoming_path)
    temporary_path = manifest_path.with_name(
        f"{manifest_path.name}.{uuid4().hex}.tmp"
    )
    with _owner_manifest_lock:
        owners = _read_owner_manifest(incoming_path)
        owner_user_id = getattr(username, "user_id", None)
        if not isinstance(owner_user_id, UUID):
            raise ValueError("Arrival Hall uploader identity is unavailable")
        owners[relative_path] = {
            "owner_user_id": str(owner_user_id),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary_path.write_text(
            json.dumps(owners, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary_path, manifest_path)


def get_arrival_hall_file_owner(
    incoming_path: Path,
    path: Path,
) -> str | None:
    try:
        relative_path = path.resolve(strict=True).relative_to(
            incoming_path.resolve(strict=True)
        ).as_posix()
    except (OSError, ValueError):
        return None
    with _owner_manifest_lock:
        entry = _read_owner_manifest(incoming_path).get(relative_path)
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        owner_user_id = entry.get("owner_user_id")
        return owner_user_id if isinstance(owner_user_id, str) else None
    return None


def get_arrival_hall_file_uploaded_at(
    incoming_path: Path,
    path: Path,
) -> datetime:
    """Return when a file entered Arrival Hall, never its original mtime."""
    try:
        relative_path = path.resolve(strict=True).relative_to(
            incoming_path.resolve(strict=True)
        ).as_posix()
    except (OSError, ValueError):
        return datetime.fromtimestamp(path.stat().st_ctime, tz=timezone.utc)
    with _owner_manifest_lock:
        entry = _read_owner_manifest(incoming_path).get(relative_path)
    if isinstance(entry, dict):
        raw_uploaded_at = entry.get("uploaded_at")
        if isinstance(raw_uploaded_at, str):
            try:
                value = datetime.fromisoformat(raw_uploaded_at.replace("Z", "+00:00"))
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    # Legacy manifests contain the immutable owner but predate a persisted
    # intake timestamp.  Upload changes ctime after applying client mtime, so
    # it is the best preserved arrival boundary for those existing files.
    return datetime.fromtimestamp(path.stat().st_ctime, tz=timezone.utc)


def arrival_hall_owner_matches(
    owner: str | None,
    user: object,
    authentication: AuthenticationStore,
) -> bool:
    """Match UUID manifest ownership, with one fail-closed legacy bridge."""
    user_id = getattr(user, "user_id", None)
    if not isinstance(user_id, UUID) or not owner:
        return False
    try:
        return UUID(owner) == user_id
    except ValueError:
        account = authentication.get_account(owner)
        return bool(account and account.active and account.user_id == user_id)


@router.get("", response_model=ArrivalHallListing)
def list_arrival_hall_files(
    response: Response,
    username: AuthenticatedUsername,
    authentication: AuthenticationStore = Depends(get_authentication_store),
    incoming_path: Path = Depends(require_incoming_path),
    max_upload_bytes: int = Depends(get_upload_max_bytes),
) -> ArrivalHallListing:
    response.headers["Cache-Control"] = "private, no-store"
    files: list[ArrivalHallFile] = []

    try:
        for entry in incoming_path.rglob("*"):
            if (
                entry.name.startswith(".pv-")
                or entry.is_symlink()
                or not entry.is_file()
            ):
                continue

            resolved_entry = entry.resolve(strict=True)
            if not resolved_entry.is_relative_to(incoming_path):
                continue

            relative_path = entry.relative_to(incoming_path).as_posix()
            owner = get_arrival_hall_file_owner(incoming_path, entry)
            if not arrival_hall_owner_matches(owner, username, authentication):
                continue
            parts = tuple(relative_path.split("/"))
            current = incoming_path
            if any(
                (current := current / part).is_symlink()
                for part in parts
            ):
                continue

            file_stat = resolved_entry.stat()
            parent = Path(relative_path).parent.as_posix()
            files.append(
                ArrivalHallFile(
                    name=entry.name,
                    relative_path=relative_path,
                    folder=None if parent == "." else parent,
                    size=file_stat.st_size,
                    uploaded_at=get_arrival_hall_file_uploaded_at(
                        incoming_path, resolved_entry
                    ),
                )
            )
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrival Hall storage is unavailable",
        ) from error

    files.sort(
        key=lambda incoming_file: incoming_file.uploaded_at,
        reverse=True,
    )

    return ArrivalHallListing(
        files=files,
        max_upload_bytes=max_upload_bytes,
    )


@router.get("/{relative_path:path}/preview", response_class=FileResponse)
def preview_incoming_file(
    relative_path: str,
    username: AuthenticatedUsername,
    authentication: AuthenticationStore = Depends(get_authentication_store),
    incoming_path: Path = Depends(require_incoming_path),
) -> FileResponse:
    candidate = resolve_arrival_hall_file(incoming_path, relative_path)
    owner = get_arrival_hall_file_owner(incoming_path, candidate)
    if not arrival_hall_owner_matches(owner, username, authentication):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Arrival Hall preview was not found",
        )
    media_type = (
        mimetypes.guess_type(candidate.name)[0]
        or "application/octet-stream"
    )
    if not media_type.startswith(PREVIEW_MIME_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="This file type cannot be previewed",
        )
    return FileResponse(
        path=candidate,
        media_type=media_type,
        filename=candidate.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("", response_model=UploadResult)
async def upload_to_incoming(
    request: Request,
    response: Response,
    username: AuthenticatedUsername,
    incoming_path: Path = Depends(require_incoming_path),
    max_upload_bytes: int = Depends(get_upload_max_bytes),
) -> UploadResult:
    response.headers["Cache-Control"] = "private, no-store"

    if request.headers.get("X-PV-Upload") != "1":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload request marker is required",
        )

    filename = validate_filename(request.headers.get("X-PV-Filename"))
    original_modified = parse_original_modified(
        request.headers.get("X-PV-Last-Modified")
    )
    content_length = request.headers.get("Content-Length")

    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Upload size is invalid",
            ) from error

        if declared_size > max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File exceeds the upload size limit",
            )

    temporary_path = (
        incoming_path / f".pv-upload-{uuid4().hex}.part"
    )
    destination: Path | None = None
    received_bytes = 0
    require_intake_admission(request)

    try:
        async with await anyio.open_file(temporary_path, "xb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue

                received_bytes += len(chunk)
                if received_bytes > max_upload_bytes:
                    raise HTTPException(
                        status_code=(
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                        ),
                        detail="File exceeds the upload size limit",
                    )

                await output.write(chunk)

        if original_modified is not None:
            os.utime(
                temporary_path,
                (original_modified, original_modified),
            )
        destination = publish_without_overwriting(
            temporary_path,
            incoming_path,
            filename,
        )
        record_arrival_hall_file_owner(incoming_path, destination, username)
    except HTTPException:
        temporary_path.unlink(missing_ok=True)
        raise
    except (OSError, anyio.BrokenResourceError) as error:
        temporary_path.unlink(missing_ok=True)
        if destination is not None:
            destination.unlink(missing_ok=True)
        logger.exception(
            "Upload failed for username=%r filename=%r",
            username,
            filename,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrival Hall storage could not accept the upload",
        ) from error
    finally:
        # Admission is counted per file. A pause waits for this safe boundary.
        from app.vault_master_intake import get_intake_store
        provider = request.app.dependency_overrides.get(get_intake_store, get_intake_store)
        provider().finish_transfer()

    logger.info(
        "Completed upload for username=%r filename=%r stored_name=%r "
        "size=%s",
        username,
        filename,
        destination.name,
        received_bytes,
    )

    return UploadResult(
        status="uploaded",
        original_name=filename,
        stored_name=destination.name,
        size=received_bytes,
    )
