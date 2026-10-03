"""Canonical Home Videos runtime-path configuration."""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CANONICAL_HOME_VIDEOS_PATH = Path("/vault/Home Videos")
LEGACY_HOME_VIDEOS_ENV = "PV_PERSONAL_VIDEOS_PATH"


def get_home_videos_path() -> Path:
    """Resolve Home Videos without reviving the obsolete media fallback."""
    configured = os.getenv("PV_HOME_VIDEOS_PATH")
    if configured:
        return Path(configured)
    legacy = os.getenv(LEGACY_HOME_VIDEOS_ENV)
    if legacy:
        logger.warning(
            "%s is deprecated; configure PV_HOME_VIDEOS_PATH instead",
            LEGACY_HOME_VIDEOS_ENV,
        )
        return Path(legacy)
    return CANONICAL_HOME_VIDEOS_PATH
