"""Translate authorized PV files into the provider's configured path namespace."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Literal

CONFIG_KEY = "PV_JELLYFIN_MEDIA_PATH_MAP_JSON"


class JellyfinPathMappingError(ValueError):
    """The explicit namespace contract cannot safely map this PV source."""


def jellyfin_media_path(source: Path, section: Literal["movies", "tv"]) -> Path | PurePosixPath:
    """Use literal legacy lookup unless an explicit namespace map is configured.

    Callers must resolve PV identity, audience and authoritative placement first.
    Source roots use the backend filesystem; destination roots use POSIX provider
    paths. An absent key preserves the source literally; it never guesses a
    translation. A present key, including an empty value, must validate fully.
    """
    if CONFIG_KEY not in os.environ:
        return source
    try:
        entries = json.loads(os.environ[CONFIG_KEY])
        if not isinstance(entries, list) or not entries or len(entries) > 128:
            raise ValueError
        if not source.is_absolute() or ".." in source.parts:
            raise ValueError
        resolved = source.resolve()
        matches: list[PurePosixPath] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"section", "pv_root", "jellyfin_root"}:
                raise ValueError
            if entry["section"] not in ("movies", "tv"):
                raise ValueError
            pv_value, provider_value = entry["pv_root"], entry["jellyfin_root"]
            if not isinstance(pv_value, str) or not isinstance(provider_value, str):
                raise ValueError
            if "\x00" in pv_value or "\x00" in provider_value or "\\" in provider_value:
                raise ValueError
            root = Path(pv_value)
            destination = PurePosixPath(provider_value)
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError
            if not destination.is_absolute() or ".." in destination.parts or provider_value.startswith("//"):
                raise ValueError
            if entry["section"] != section:
                continue
            try:
                relative = resolved.relative_to(root.resolve())
            except ValueError:
                continue
            if not relative.parts or any("\\" in part for part in relative.parts):
                raise ValueError
            matches.append(destination.joinpath(*relative.parts))
        if len(matches) != 1:
            raise ValueError
        return matches[0]
    except (ValueError, TypeError, OSError, RuntimeError):
        raise JellyfinPathMappingError("Jellyfin media path mapping is unavailable") from None
