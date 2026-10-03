"""Explicit Music Video admission; neither filenames nor MIME establish type."""
from pathlib import PurePosixPath

ASSET_TYPE = "Music Videos"
CONTENT_TYPE = "music_video"
ROOT = "/vault/Music/Music Videos"


def validate_media(mime_type: str) -> None:
    if not mime_type.startswith("video/"):
        raise ValueError("Music Videos requires a video file")


def declared_intent(context: object, mime_type: str) -> dict | None:
    """PV-side contract for authenticated owner-declared source context."""
    if not isinstance(context, dict) or "content_type" not in context:
        return None
    if context["content_type"] != CONTENT_TYPE:
        raise ValueError("Unsupported declared content type")
    validate_media(mime_type)
    if context.get("music_album") is not None:
        raise ValueError("Music Videos cannot declare audio album membership")
    result = {}
    for key in ("artist", "title"):
        value = context.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 240):
            raise ValueError("Invalid Music Video metadata")
        result[key] = value.strip() if value else None
    return result


def validate_publication(item) -> None:
    if item.proposed_category != ASSET_TYPE:
        return
    validate_media(item.mime_type)
    if not item.owner_user_id:
        raise ValueError("Music Video publication requires an immutable owner")
    declared = declared_intent(item.metadata.get("source_context"), item.mime_type)
    if item.proposal_reason != "Category selected by the user." and declared is None:
        raise ValueError("Music Video publication requires explicit owner classification")
    destination = PurePosixPath(item.proposed_destination or "")
    if not destination.is_relative_to(PurePosixPath(ROOT)) or ".." in destination.parts:
        raise ValueError("Invalid Music Video destination")
