"""Operator-configured source identity admission for environment-local workers."""
import os
import re


KEY = "PV_ALLOWED_SOURCE_REPOSITORIES"
REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")


def valid_repository(value: str) -> bool:
    if not REPOSITORY.fullmatch(value):
        return False
    owner, name = value.split("/")
    return "--" not in owner and name not in {".", ".."} and not name.endswith(".git")


def source_repository_allowed() -> bool:
    """Case-sensitive exact identities; invalid configuration denies all entries."""
    configured = os.getenv(KEY, "")
    if not configured or len(configured) > 16384:
        return False
    entries = configured.split(",")
    if len(entries) > 64:
        return False
    allowed = {entry.strip(" \t") for entry in entries}
    if not all(valid_repository(entry) for entry in allowed):
        return False
    identity = os.getenv("PV_REPOSITORY", "")
    return valid_repository(identity) and identity in allowed
