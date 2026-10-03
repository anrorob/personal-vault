"""Video recognition is independent of browser container/codec support."""

VIDEO_MIME_TYPES = {
    ".3gp": "video/3gpp", ".3g2": "video/3gpp2",
    ".avi": "video/x-msvideo", ".flv": "video/x-flv",
    ".m2ts": "video/mp2t", ".m4v": "video/mp4",
    ".mkv": "video/x-matroska", ".mov": "video/quicktime",
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".mts": "video/mp2t", ".ts": "video/mp2t", ".vob": "video/mpeg",
    ".webm": "video/webm", ".wmv": "video/x-ms-wmv",
}
VIDEO_EXTENSIONS = frozenset(VIDEO_MIME_TYPES)

# Eligible for an inline attempt; actual playback still depends on the codec
# and browser. Other recognized containers remain downloadable/processable.
BROWSER_INLINE_VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".webm"})
