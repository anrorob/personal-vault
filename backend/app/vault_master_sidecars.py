from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Callable, TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from app.vault_master import CataloguedAsset


SIDECAR_SCHEMA = "personal-vault.metadata"
SIDECAR_VERSION = 2
LEGACY_SIDECAR_VERSION = 1
SIDECAR_DIRECTORY = "sidecars"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ValidatedCanonicalSidecar:
    path: Path
    asset_id: UUID
    exported_at: datetime
    document: dict[str, object]


@dataclass(frozen=True)
class SidecarRecoveryAssessment:
    discovered: int
    valid: int
    invalid: int
    unsupported: int
    current: int = 0
    hidden: int = 0
    recoverable: int = 0
    intentionally_deleted: int = 0
    media_missing: int = 0
    restorable: int = 0
    conflicting: int = 0
    path_conflicts: int = 0
    candidates: tuple[SidecarRecoveryCandidate, ...] = ()


@dataclass(frozen=True)
class SidecarRecoveryCandidate:
    sidecar_name: str
    status: str
    detail: str
    asset_id: UUID | None = None
    display_title: str | None = None
    vault_path: str | None = None
    filename: str | None = None


class UnsupportedSidecarVersion(ValueError):
    pass


def _require_mapping(
    value: object,
    field_name: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def read_canonical_sidecar(path: Path) -> ValidatedCanonicalSidecar:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Sidecar must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Sidecar is not valid UTF-8 JSON") from error
    document = _require_mapping(document, "Sidecar")
    if document.get("schema") != SIDECAR_SCHEMA:
        raise ValueError("Sidecar schema is not recognised")
    version = document.get("version")
    if version not in {LEGACY_SIDECAR_VERSION, SIDECAR_VERSION}:
        raise UnsupportedSidecarVersion("Sidecar version is not supported")
    required_fields = {"schema", "version", "exported_at", "asset", "metadata"}
    if version == SIDECAR_VERSION:
        required_fields.add("access")
    if set(document) != required_fields:
        raise ValueError("Sidecar fields do not match the canonical schema")

    exported_at = document.get("exported_at")
    if not isinstance(exported_at, str):
        raise ValueError("Sidecar export timestamp is missing")
    try:
        timestamp = datetime.fromisoformat(exported_at)
    except ValueError as error:
        raise ValueError("Sidecar export timestamp is invalid") from error
    if timestamp.tzinfo is None:
        raise ValueError("Sidecar export timestamp must be timezone-aware")

    asset = _require_mapping(document.get("asset"), "asset")
    if set(asset) != {
        "id",
        "asset_type",
        "vault_path",
        "filename",
        "size_bytes",
        "mime_type",
        "sha256",
    }:
        raise ValueError("Sidecar asset fields do not match the canonical schema")
    try:
        asset_id = UUID(str(asset.get("id")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Sidecar asset id is invalid") from error
    if path.name != f"{asset_id}.json":
        raise ValueError("Sidecar filename does not match its asset id")

    for field_name in ("asset_type", "vault_path", "filename", "mime_type"):
        if not isinstance(asset.get(field_name), str) or not asset[field_name]:
            raise ValueError(f"Sidecar asset {field_name} is invalid")
    vault_path = PurePosixPath(str(asset["vault_path"]))
    if (
        not vault_path.is_absolute()
        or len(vault_path.parts) < 3
        or vault_path.parts[1] != "vault"
        or ".." in vault_path.parts
        or vault_path.name != asset["filename"]
    ):
        raise ValueError("Sidecar Vault path is invalid")
    size_bytes = asset.get("size_bytes")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
    ):
        raise ValueError("Sidecar asset size is invalid")
    sha256 = asset.get("sha256")
    if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
        raise ValueError("Sidecar asset checksum is invalid")

    metadata = _require_mapping(document.get("metadata"), "metadata")
    if set(metadata) != {
        "detected",
        "imported",
        "user_overrides",
        "effective",
        "provenance",
    }:
        raise ValueError("Sidecar metadata layers do not match the canonical schema")
    for layer in (
        "detected",
        "imported",
        "user_overrides",
        "effective",
        "provenance",
    ):
        _require_mapping(metadata.get(layer), f"metadata.{layer}")

    if version == SIDECAR_VERSION:
        access = _require_mapping(document.get("access"), "access")
        if set(access) != {"owner_username", "visibility", "shared_with"}:
            raise ValueError("Sidecar access fields do not match the canonical schema")
        owner_username = access.get("owner_username")
        if not isinstance(owner_username, str) or not owner_username.strip():
            raise ValueError("Sidecar access owner is invalid")
        if access.get("visibility") not in {"private", "shared", "vault-wide"}:
            raise ValueError("Sidecar access visibility is invalid")
        shared_with = access.get("shared_with")
        if (
            not isinstance(shared_with, list)
            or not all(
                isinstance(username, str) and username.strip()
                for username in shared_with
            )
            or len(set(shared_with)) != len(shared_with)
        ):
            raise ValueError("Sidecar access sharing scope is invalid")

    return ValidatedCanonicalSidecar(
        path=path,
        asset_id=asset_id,
        exported_at=timestamp.astimezone(timezone.utc),
        document=document,
    )


def _read_sidecar_directory(
    storage_root: Path,
) -> tuple[
    list[ValidatedCanonicalSidecar],
    list[SidecarRecoveryCandidate],
    int,
    int,
]:
    sidecar_root = storage_root / SIDECAR_DIRECTORY
    try:
        paths = sorted(sidecar_root.glob("*.json"))
    except OSError:
        paths = []
    valid: list[ValidatedCanonicalSidecar] = []
    failures: list[SidecarRecoveryCandidate] = []
    for path in paths:
        try:
            sidecar = read_canonical_sidecar(path)
        except UnsupportedSidecarVersion as error:
            failures.append(
                SidecarRecoveryCandidate(
                    sidecar_name=path.name,
                    status="unsupported",
                    detail=str(error),
                )
            )
        except ValueError as error:
            failures.append(
                SidecarRecoveryCandidate(
                    sidecar_name=path.name,
                    status="invalid",
                    detail=str(error),
                )
            )
        else:
            valid.append(sidecar)
    return (
        valid,
        failures,
        sum(item.status == "invalid" for item in failures),
        sum(item.status == "unsupported" for item in failures),
    )


def assess_sidecar_recovery(storage_root: Path) -> SidecarRecoveryAssessment:
    valid, failures, invalid, unsupported = _read_sidecar_directory(storage_root)
    return SidecarRecoveryAssessment(
        discovered=len(valid) + len(failures),
        valid=len(valid),
        invalid=invalid,
        unsupported=unsupported,
        candidates=tuple(failures),
    )


def compare_sidecar_recovery(
    storage_root: Path,
    get_asset_by_id: Callable[[UUID], CataloguedAsset | None],
    get_asset_by_path: Callable[[str], CataloguedAsset | None],
    has_deletion: Callable[[UUID], bool] | None = None,
    file_is_recoverable: Callable[[CataloguedAsset], bool] | None = None,
) -> SidecarRecoveryAssessment:
    valid, failures, invalid, unsupported = _read_sidecar_directory(storage_root)
    current = 0
    hidden = 0
    recoverable = 0
    intentionally_deleted = 0
    media_missing = 0
    restorable = 0
    conflicting = 0
    path_conflicts = 0
    candidates = list(failures)
    for sidecar in valid:
        document_asset = _require_mapping(
            sidecar.document["asset"],
            "asset",
        )
        vault_path = str(document_asset["vault_path"])
        metadata = _require_mapping(sidecar.document["metadata"], "metadata")
        effective = _require_mapping(metadata["effective"], "metadata.effective")
        candidate_values = {
            "sidecar_name": sidecar.path.name,
            "asset_id": sidecar.asset_id,
            "display_title": str(
                effective.get("display_title") or document_asset["filename"]
            ),
            "vault_path": vault_path,
            "filename": str(document_asset["filename"]),
        }
        existing = get_asset_by_id(sidecar.asset_id)
        if existing is not None:
            if existing.lifecycle_state == "hidden":
                hidden += 1
                candidates.append(
                    SidecarRecoveryCandidate(
                        **candidate_values,
                        status="hidden",
                        detail="The canonical asset is intentionally hidden.",
                    )
                )
                continue
            expected = canonical_sidecar_document(existing)
            expected.pop("exported_at")
            actual = dict(sidecar.document)
            actual.pop("exported_at")
            if actual == expected:
                current += 1
                candidates.append(
                    SidecarRecoveryCandidate(
                        **candidate_values,
                        status="current",
                        detail="The catalogue already matches this sidecar.",
                    )
                )
            else:
                conflicting += 1
                candidates.append(
                    SidecarRecoveryCandidate(
                        **candidate_values,
                        status="conflict",
                        detail="This asset id already exists with different metadata.",
                    )
                )
            continue

        if has_deletion is not None and has_deletion(sidecar.asset_id):
            intentionally_deleted += 1
            candidates.append(
                SidecarRecoveryCandidate(
                    **candidate_values,
                    status="intentionally_deleted",
                    detail="Permanent-deletion evidence prevents catalogue recovery.",
                )
            )
            continue

        existing_at_path = get_asset_by_path(vault_path)
        if existing_at_path is not None:
            path_conflicts += 1
            candidates.append(
                SidecarRecoveryCandidate(
                    **candidate_values,
                    status="path_conflict",
                    detail="A different catalogue asset already uses this Vault path.",
                )
            )
        else:
            candidate_asset = catalogued_asset_from_sidecar(
                sidecar, legacy_owner_username="recovery"
            )
            if file_is_recoverable is not None and not file_is_recoverable(candidate_asset):
                media_missing += 1
                candidates.append(
                    SidecarRecoveryCandidate(
                        **candidate_values,
                        status="media_missing",
                        detail="The canonical media file is missing or no longer verifies.",
                    )
                )
                continue
            recoverable += 1
            restorable += 1
            candidates.append(
                SidecarRecoveryCandidate(
                    **candidate_values,
                    status="recoverable",
                    detail="The verified canonical media can be restored to the catalogue.",
                )
            )

    return SidecarRecoveryAssessment(
        discovered=len(valid) + len(failures),
        valid=len(valid),
        invalid=invalid,
        unsupported=unsupported,
        current=current,
        hidden=hidden,
        recoverable=recoverable,
        intentionally_deleted=intentionally_deleted,
        media_missing=media_missing,
        restorable=restorable,
        conflicting=conflicting,
        path_conflicts=path_conflicts,
        candidates=tuple(candidates),
    )


def catalogued_asset_from_sidecar(
    sidecar: ValidatedCanonicalSidecar,
    *,
    legacy_owner_username: str | None = None,
) -> CataloguedAsset:
    from app.vault_master import CataloguedAsset

    asset = _require_mapping(sidecar.document["asset"], "asset")
    metadata = _require_mapping(sidecar.document["metadata"], "metadata")
    detected = _require_mapping(metadata["detected"], "metadata.detected")
    imported = _require_mapping(metadata["imported"], "metadata.imported")
    overrides = _require_mapping(
        metadata["user_overrides"],
        "metadata.user_overrides",
    )
    effective = _require_mapping(metadata["effective"], "metadata.effective")
    provenance = _require_mapping(
        metadata["provenance"],
        "metadata.provenance",
    )
    access = sidecar.document.get("access")
    if access is None:
        if legacy_owner_username is None:
            raise ValueError("Sidecar owner identity is unavailable")
        owner_username = legacy_owner_username
        owner_user_id = None
        visibility = "private"
        shared_with: tuple[str, ...] = ()
    else:
        access_values = _require_mapping(access, "access")
        owner_username = str(access_values["owner_username"])
        owner_user_id = None
        visibility = str(access_values["visibility"])
        shared_with = tuple(str(username) for username in access_values["shared_with"])

    display_title = effective.get("display_title")
    if not isinstance(display_title, str) or not display_title.strip():
        raise ValueError("Sidecar effective display title is invalid")
    captured_value = effective.get("captured_on")
    if captured_value is None:
        captured_on = None
    elif isinstance(captured_value, str):
        try:
            captured_on = date.fromisoformat(captured_value)
        except ValueError as error:
            raise ValueError("Sidecar effective capture date is invalid") from error
    else:
        raise ValueError("Sidecar effective capture date is invalid")
    location = effective.get("location")
    if location is not None and not isinstance(location, str):
        raise ValueError("Sidecar effective location is invalid")
    if not all(isinstance(value, str) for value in provenance.values()):
        raise ValueError("Sidecar metadata provenance is invalid")

    return CataloguedAsset(
        id=sidecar.asset_id,
        asset_type=str(asset["asset_type"]),
        display_title=display_title.strip(),
        captured_on=captured_on,
        location=location,
        vault_path=str(asset["vault_path"]),
        filename=str(asset["filename"]),
        size_bytes=int(asset["size_bytes"]),
        mime_type=str(asset["mime_type"]),
        sha256=str(asset["sha256"]),
        metadata=dict(detected),
        metadata_provenance={
            key: str(value) for key, value in provenance.items()
        },
        detected_metadata=dict(detected),
        imported_metadata=dict(imported),
        user_overrides=dict(overrides),
        effective_metadata=dict(effective),
        owner_username=owner_username,
        owner_user_id=owner_user_id,
        visibility=visibility,
        shared_with=shared_with,
    )


def read_restorable_sidecar(
    storage_root: Path,
    asset_id: UUID,
    get_asset_by_id: Callable[[UUID], CataloguedAsset | None],
    get_asset_by_path: Callable[[str], CataloguedAsset | None],
    has_deletion: Callable[[UUID], bool] | None = None,
    *,
    legacy_owner_username: str | None = None,
) -> CataloguedAsset:
    path = storage_root / SIDECAR_DIRECTORY / f"{asset_id}.json"
    sidecar = read_canonical_sidecar(path)
    asset = catalogued_asset_from_sidecar(
        sidecar,
        legacy_owner_username=legacy_owner_username,
    )
    if get_asset_by_id(asset.id) is not None:
        raise ValueError("The asset already exists in the catalogue")
    if get_asset_by_path(asset.vault_path) is not None:
        raise ValueError("The Vault path is already assigned")
    if has_deletion is not None and has_deletion(asset.id):
        raise ValueError("Permanent-deletion evidence prevents catalogue recovery")
    return asset


def canonical_sidecar_document(
    asset: CataloguedAsset,
    *,
    exported_at: datetime | None = None,
) -> dict[str, object]:
    timestamp = exported_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("Sidecar export timestamps must be timezone-aware")

    return {
        "schema": SIDECAR_SCHEMA,
        "version": SIDECAR_VERSION,
        "exported_at": timestamp.astimezone(timezone.utc).isoformat(),
        "asset": {
            "id": str(asset.id),
            "asset_type": asset.asset_type,
            "vault_path": asset.vault_path,
            "filename": asset.filename,
            "size_bytes": asset.size_bytes,
            "mime_type": asset.mime_type,
            "sha256": asset.sha256,
        },
        "access": {
            "owner_username": asset.owner_username,
            "visibility": asset.visibility,
            "shared_with": list(asset.shared_with),
        },
        "metadata": {
            "detected": asset.detected_metadata,
            "imported": asset.imported_metadata,
            "user_overrides": asset.user_overrides,
            "effective": asset.effective_metadata,
            "provenance": asset.metadata_provenance,
        },
    }


def write_canonical_sidecar(
    asset: CataloguedAsset,
    storage_root: Path,
    *,
    exported_at: datetime | None = None,
) -> Path:
    sidecar_root = storage_root / SIDECAR_DIRECTORY
    sidecar_root.mkdir(parents=True, exist_ok=True)
    destination = sidecar_root / f"{asset.id}.json"
    temporary = sidecar_root / f".{asset.id}.{uuid4().hex}.json.part"
    payload = canonical_sidecar_document(asset, exported_at=exported_at)

    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return destination


def canonical_sidecar_path(
    asset: CataloguedAsset,
    storage_root: Path,
) -> Path:
    return storage_root / SIDECAR_DIRECTORY / f"{asset.id}.json"


def canonical_sidecar_is_current(
    asset: CataloguedAsset,
    storage_root: Path,
) -> bool:
    destination = canonical_sidecar_path(asset, storage_root)
    try:
        document = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    exported_at = document.get("exported_at")
    if not isinstance(exported_at, str):
        return False
    try:
        timestamp = datetime.fromisoformat(exported_at)
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        return False

    expected = canonical_sidecar_document(asset)
    document.pop("exported_at", None)
    expected.pop("exported_at", None)
    return document == expected
