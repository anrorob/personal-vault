"""Release and build identity for diagnostics and API metadata."""
from __future__ import annotations

import os
import re
from pathlib import Path


SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")
COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")
KNOWN_ENVIRONMENTS = frozenset({"development", "production", "test"})


def load_project_version(version_file: Path | None = None) -> str:
    """Load the one canonical release version from the packaged VERSION file."""
    candidates = (version_file,) if version_file else (
        Path("/app/VERSION"),
        Path(__file__).resolve().parents[2] / "VERSION",
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if SEMVER_RE.fullmatch(value):
            return value
        raise RuntimeError("VERSION must contain a MAJOR.MINOR.PATCH release version")
    raise RuntimeError("VERSION is unavailable")


def get_environment() -> str:
    value = os.getenv("PV_ENVIRONMENT", "development").casefold()
    if value not in KNOWN_ENVIRONMENTS:
        raise RuntimeError("PV_ENVIRONMENT must be development, production, or test")
    return value


def get_build_commit(environment: str | None = None) -> str:
    value = os.getenv("PV_COMMIT", "unknown").strip().casefold()
    active_environment = environment or get_environment()
    if COMMIT_RE.fullmatch(value):
        return value
    if active_environment == "production":
        raise RuntimeError("PV_COMMIT must be an immutable Git commit identifier in production")
    if value in {"unknown", "development", "test"}:
        return value
    raise RuntimeError("PV_COMMIT must be a Git commit identifier or a permitted non-production marker")


def build_info() -> dict[str, str]:
    environment = get_environment()
    return {
        "version": load_project_version(),
        "commit": get_build_commit(environment),
        "environment": environment,
    }
