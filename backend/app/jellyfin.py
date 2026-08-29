from dataclasses import dataclass, field
from collections.abc import Iterator
from http.client import HTTPResponse
import json
import os
from pathlib import Path
import re
import secrets
from threading import Lock
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from uuid import UUID


class JellyfinUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class JellyfinSubtitleTrack:
    index: int
    title: str | None
    display_title: str | None
    language: str | None
    codec: str | None
    is_external: bool
    is_default: bool
    is_forced: bool
    is_hearing_impaired: bool


def _parse_playback_subtitle_tracks(
    streams: list[dict[str, object]],
) -> tuple[JellyfinSubtitleTrack, ...]:
    return tuple(
        JellyfinSubtitleTrack(
            index=stream["Index"],
            title=(
                stream.get("Title")
                if isinstance(stream.get("Title"), str)
                else None
            ),
            display_title=(
                stream.get("DisplayTitle")
                if isinstance(stream.get("DisplayTitle"), str)
                else None
            ),
            language=(
                stream.get("Language")
                if isinstance(stream.get("Language"), str)
                else None
            ),
            codec=(
                stream.get("Codec")
                if isinstance(stream.get("Codec"), str)
                else None
            ),
            is_external=bool(stream.get("IsExternal")),
            is_default=bool(stream.get("IsDefault")),
            is_forced=bool(stream.get("IsForced")),
            is_hearing_impaired=bool(stream.get("IsHearingImpaired")),
        )
        for stream in streams
        if (
            stream.get("Type") == "Subtitle"
            and isinstance(stream.get("Index"), int)
        )
    )


def _parse_playback_item(item: dict[str, object]) -> "JellyfinMovie | None":
    item_id = item.get("Id")
    item_path = item.get("Path")
    media_sources = item.get("MediaSources")
    first_source = (
        media_sources[0]
        if isinstance(media_sources, list) and media_sources
        else None
    )
    source = first_source if isinstance(first_source, dict) else {}
    media_source_id = source.get("Id")
    if (
        not isinstance(item_id, str)
        or not item_id
        or not isinstance(item_path, str)
        or not isinstance(media_source_id, str)
        or not media_source_id
    ):
        return None
    streams = [
        stream
        for stream in source.get("MediaStreams", [])
        if isinstance(stream, dict)
    ]
    video_codec = next(
        (
            stream.get("Codec")
            for stream in streams
            if stream.get("Type") == "Video"
        ),
        None,
    )
    audio_codecs = tuple(
        dict.fromkeys(
            stream.get("Codec")
            for stream in streams
            if (
                stream.get("Type") == "Audio"
                and isinstance(stream.get("Codec"), str)
            )
        )
    )
    return JellyfinMovie(
        item_id=item_id,
        media_source_id=media_source_id,
        path=item_path,
        container=(
            source.get("Container")
            if isinstance(source.get("Container"), str)
            else None
        ),
        video_codec=video_codec if isinstance(video_codec, str) else None,
        audio_codecs=audio_codecs,
        has_primary_image=(
            isinstance(item.get("ImageTags"), dict)
            and isinstance(item.get("ImageTags", {}).get("Primary"), str)
        ),
        subtitle_tracks=_parse_playback_subtitle_tracks(streams),
    )


@dataclass(frozen=True)
class JellyfinMovie:
    item_id: str
    media_source_id: str
    path: str
    container: str | None
    video_codec: str | None
    audio_codecs: tuple[str, ...]
    has_primary_image: bool = False
    subtitle_tracks: tuple[JellyfinSubtitleTrack, ...] = ()


@dataclass(frozen=True)
class JellyfinAudio:
    item_id: str
    media_source_id: str
    path: str
    container: str | None
    audio_codec: str | None
    has_primary_image: bool = False
    artwork_item_id: str | None = None


@dataclass(frozen=True)
class JellyfinPerson:
    item_id: str
    name: str
    role: str | None
    person_type: str | None
    has_image: bool


@dataclass(frozen=True)
class JellyfinExtra:
    item_id: str
    name: str
    runtime_ticks: int | None
    has_image: bool = False


@dataclass(frozen=True)
class JellyfinChapter:
    name: str
    start_ticks: int


@dataclass(frozen=True)
class JellyfinSubtitle:
    index: int
    title: str | None
    language: str | None
    codec: str | None
    is_external: bool


@dataclass(frozen=True)
class JellyfinMovieDetails:
    title: str
    year: int | None
    official_rating: str | None
    community_rating: float | None
    runtime_ticks: int | None
    overview: str | None
    tagline: str | None
    genres: tuple[str, ...]
    studios: tuple[str, ...]
    people: tuple[JellyfinPerson, ...]
    extras: tuple[JellyfinExtra, ...]
    trailers: tuple[JellyfinExtra, ...]
    has_primary_image: bool
    has_backdrop_image: bool
    provider_ids: dict[str, str] = field(default_factory=dict)
    edition: str | None = None
    collections: tuple[str, ...] = ()
    chapters: tuple[JellyfinChapter, ...] = ()
    subtitles: tuple[JellyfinSubtitle, ...] = ()


@dataclass
class JellyfinStream:
    response: HTTPResponse
    status_code: int
    headers: dict[str, str]
    url: str
    content_type: str | None

    def iter_bytes(
        self,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        try:
            while chunk := self.response.read(chunk_size):
                yield chunk
        finally:
            self.response.close()


@dataclass(frozen=True)
class HlsResource:
    user_id: UUID
    url: str
    expires_at: float


class HlsResourceStore:
    def __init__(
        self,
        ttl_seconds: int = 6 * 60 * 60,
        max_resources: int = 20_000,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_resources = max_resources
        self._resources: dict[str, HlsResource] = {}
        self._lock = Lock()

    def issue(self, user_id: UUID, url: str) -> str:
        now = time.monotonic()

        with self._lock:
            self._purge_expired(now)
            while len(self._resources) >= self._max_resources:
                oldest_token = min(
                    self._resources,
                    key=lambda token: self._resources[token].expires_at,
                )
                self._resources.pop(oldest_token)

            token = secrets.token_urlsafe(32)
            self._resources[token] = HlsResource(
                user_id=user_id,
                url=url,
                expires_at=now + self._ttl_seconds,
            )

        return token

    def resolve(self, user_id: UUID, token: str) -> str | None:
        now = time.monotonic()

        with self._lock:
            self._purge_expired(now)
            resource = self._resources.get(token)

            if resource is None or resource.user_id != user_id:
                return None

            return resource.url

    def _purge_expired(self, now: float) -> None:
        expired_tokens = [
            token
            for token, resource in self._resources.items()
            if resource.expires_at <= now
        ]
        for token in expired_tokens:
            self._resources.pop(token)


PLAYLIST_URI_PATTERN = re.compile(r'URI="(?P<uri>[^"]+)"')


def rewrite_hls_playlist(
    playlist: str,
    playlist_url: str,
    proxy_url_for: Callable[[str], str],
) -> str:
    def replace_uri_attribute(match: re.Match[str]) -> str:
        upstream_url = urljoin(playlist_url, match.group("uri"))
        return f'URI="{proxy_url_for(upstream_url)}"'

    rewritten_lines: list[str] = []

    for line in playlist.splitlines():
        if line.startswith("#"):
            rewritten_lines.append(
                PLAYLIST_URI_PATTERN.sub(replace_uri_attribute, line)
            )
        elif line.strip():
            upstream_url = urljoin(playlist_url, line.strip())
            rewritten_lines.append(proxy_url_for(upstream_url))
        else:
            rewritten_lines.append(line)

    return "\n".join(rewritten_lines) + "\n"


def _normalise_media_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def _get_json(
        self,
        path: str,
        query_parameters: dict[str, str] | None = None,
    ) -> object:
        query = (
            f"?{urlencode(query_parameters)}"
            if query_parameters
            else ""
        )
        request = Request(
            f"{self._base_url}{path}{query}",
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self._api_key,
            },
        )

        try:
            with urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                return json.load(response)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            raise JellyfinUnavailableError(
                "Playback service is unavailable"
            ) from error

    def _path_mutation(self, method: str, name: str, path: str, *, refresh_library: bool) -> None:
        request = Request(
            f"{self._base_url}/Library/VirtualFolders/MediaPaths?{urlencode({'name': name, 'path': path, 'refreshLibrary': str(refresh_library).lower()})}",
            data=b"" if method == "POST" else None,
            method=method,
            headers={"Accept": "application/json", "X-Emby-Token": self._api_key},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds):
                return
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise JellyfinUnavailableError("Jellyfin media-path update failed") from error

    def virtual_folders(self) -> list[dict[str, object]]:
        result = self._get_json("/Library/VirtualFolders")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise JellyfinUnavailableError("Playback service returned invalid virtual folders")
        return result

    def add_media_path(self, library_name: str, path: str, *, refresh_library: bool = True) -> None:
        self._path_mutation("POST", library_name, path, refresh_library=refresh_library)

    def remove_media_path(self, library_name: str, path: str, *, refresh_library: bool = True) -> None:
        self._path_mutation("DELETE", library_name, path, refresh_library=refresh_library)

    def notify_media_updated(
        self,
        paths: tuple[Path, ...],
        update_type: str = "Created",
    ) -> None:
        if not paths:
            return
        updates = []
        for path in paths:
            if not path.is_absolute():
                raise ValueError("Jellyfin media update paths must be absolute")
            updates.append(
                {
                    "Path": str(path),
                    "UpdateType": update_type,
                }
            )
        request = Request(
            f"{self._base_url}/Library/Media/Updated",
            data=json.dumps({"Updates": updates}).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Emby-Token": self._api_key,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds):
                return
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise JellyfinUnavailableError(
                "Playback library update could not be published"
            ) from error

    def refresh_library(self) -> None:
        request = Request(
            f"{self._base_url}/Library/Refresh",
            data=b"",
            method="POST",
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self._api_key,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds):
                return
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise JellyfinUnavailableError(
                "Playback library scan could not be started"
            ) from error

    def service_status(self) -> dict[str, object]:
        """Return only operational facts; no URL, token, or library paths."""
        info = self._get_json("/System/Info")
        sessions = self._get_json("/Sessions")
        if not isinstance(info, dict) or not isinstance(sessions, list):
            raise JellyfinUnavailableError("Playback service returned an invalid status")
        active_streams = sum(
            1
            for session in sessions
            if isinstance(session, dict) and isinstance(session.get("NowPlayingItem"), dict)
        )
        return {
            "version": info.get("Version") if isinstance(info.get("Version"), str) else None,
            "active_streams": active_streams,
            "scan_state": "Unavailable",
            "last_completed_scan": None,
        }

    def find_movie_by_path(
        self,
        source_path: Path,
    ) -> JellyfinMovie | None:
        query = urlencode(
            {
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path,MediaSources,ImageTags",
            }
        )
        request = Request(
            f"{self._base_url}/Items?{query}",
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self._api_key,
            },
        )

        try:
            with urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                response_body = json.load(response)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            raise JellyfinUnavailableError(
                "Playback service is unavailable"
            ) from error

        items = response_body.get("Items")
        if not isinstance(items, list):
            raise JellyfinUnavailableError(
                "Playback service returned an invalid response"
            )

        target_path = _normalise_media_path(source_path)

        for item in items:
            if not isinstance(item, dict):
                continue

            item_path = item.get("Path")
            if (
                not isinstance(item_path, str)
                or _normalise_media_path(item_path) != target_path
            ):
                continue

            item_id = item.get("Id")
            if not isinstance(item_id, str) or not item_id:
                raise JellyfinUnavailableError(
                    "Playback service returned an invalid movie"
                )

            media_sources = item.get("MediaSources")
            first_source = (
                media_sources[0]
                if isinstance(media_sources, list) and media_sources
                else None
            )
            source = (
                first_source
                if isinstance(first_source, dict)
                else {}
            )
            media_source_id = source.get("Id")
            if (
                not isinstance(media_source_id, str)
                or not media_source_id
            ):
                raise JellyfinUnavailableError(
                    "Playback service returned an invalid media source"
                )

            raw_streams = source.get("MediaStreams", [])
            streams = [
                stream
                for stream in raw_streams
                if isinstance(stream, dict)
            ]
            video_codec = next(
                (
                    stream.get("Codec")
                    for stream in streams
                    if stream.get("Type") == "Video"
                ),
                None,
            )
            audio_codecs = tuple(
                dict.fromkeys(
                    stream.get("Codec")
                    for stream in streams
                    if (
                        stream.get("Type") == "Audio"
                        and isinstance(stream.get("Codec"), str)
                    )
                )
            )

            return JellyfinMovie(
                item_id=item_id,
                media_source_id=media_source_id,
                path=item_path,
                container=source.get("Container"),
                video_codec=video_codec,
                audio_codecs=audio_codecs,
                has_primary_image=(
                    isinstance(item.get("ImageTags"), dict)
                    and isinstance(
                        item.get("ImageTags", {}).get("Primary"),
                        str,
                    )
                ),
                subtitle_tracks=_parse_playback_subtitle_tracks(streams),
            )

        return None

    def find_episode_by_path(self, source_path: Path) -> JellyfinMovie | None:
        """Return an indexed Jellyfin Episode by exact managed filesystem path."""
        response = self._get_json(
            "/Items",
            {"Recursive": "true", "IncludeItemTypes": "Episode", "Fields": "Path,MediaSources,ImageTags"},
        )
        items = response.get("Items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise JellyfinUnavailableError("Playback service returned an invalid response")
        target_path = _normalise_media_path(source_path)
        for item in items:
            if not isinstance(item, dict) or _normalise_media_path(item.get("Path", "")) != target_path:
                continue
            parsed = _parse_playback_item(item)
            if parsed is None:
                raise JellyfinUnavailableError("Playback service returned an invalid episode")
            return parsed
        return None

    def get_tv_item_metadata(self, item_id: str) -> dict[str, object]:
        """Read provider metadata only after a managed episode is indexed.

        Jellyfin 10.11 serves indexed Episodes through ``/Items?Ids=...``;
        its item-detail route returns HTTP 400 for these media-source IDs.
        """
        response = self._get_json(
            "/Items",
            {
                "Ids": item_id,
                "Fields": "ProviderIds,Overview,RunTimeTicks,ImageTags,SeriesId,SeasonId,IndexNumber,ParentIndexNumber",
            },
        )
        items = response.get("Items") if isinstance(response, dict) else None
        item = items[0] if isinstance(items, list) and len(items) == 1 else None
        if not isinstance(item, dict) or item.get("Id") != item_id:
            raise JellyfinUnavailableError("Playback service returned invalid TV metadata")
        return item

    def find_audio_by_path(self, source_path: Path) -> JellyfinAudio | None:
        response = self._get_json(
            "/Items",
            {
                "Recursive": "true",
                "IncludeItemTypes": "Audio",
                "Fields": (
                    "Path,MediaSources,ImageTags,AlbumId,"
                    "AlbumPrimaryImageTag"
                ),
            },
        )
        items = response.get("Items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise JellyfinUnavailableError("Playback service returned invalid audio items")
        target = _normalise_media_path(source_path)
        for item in items:
            if not isinstance(item, dict) or _normalise_media_path(item.get("Path", "")) != target:
                continue
            item_id = item.get("Id")
            sources = item.get("MediaSources")
            source = sources[0] if isinstance(sources, list) and sources else None
            if not isinstance(item_id, str) or not isinstance(source, dict):
                raise JellyfinUnavailableError("Playback service returned invalid audio")
            source_id = source.get("Id")
            if not isinstance(source_id, str):
                raise JellyfinUnavailableError("Playback service returned invalid audio source")
            streams = source.get("MediaStreams")
            codec = next(
                (
                    stream.get("Codec")
                    for stream in streams
                    if isinstance(stream, dict) and stream.get("Type") == "Audio"
                ),
                None,
            ) if isinstance(streams, list) else None
            return JellyfinAudio(
                item_id=item_id,
                media_source_id=source_id,
                path=str(item.get("Path")),
                container=source.get("Container") if isinstance(source.get("Container"), str) else None,
                audio_codec=codec if isinstance(codec, str) else None,
                has_primary_image=(
                    (
                        isinstance(item.get("ImageTags"), dict)
                        and isinstance(item["ImageTags"].get("Primary"), str)
                    )
                    or isinstance(item.get("AlbumPrimaryImageTag"), str)
                ),
                artwork_item_id=(
                    item.get("AlbumId")
                    if isinstance(item.get("AlbumPrimaryImageTag"), str)
                    and isinstance(item.get("AlbumId"), str)
                    else item_id
                ),
            )
        return None

    def get_audio_details(self, audio: JellyfinAudio) -> dict[str, object]:
        users = self._get_json("/Users")
        first_user = users[0] if isinstance(users, list) and users else None
        user_id = first_user.get("Id") if isinstance(first_user, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise JellyfinUnavailableError(
                "Playback service has no usable user context"
            )
        item = self._get_json(
            f"/Items/{audio.item_id}",
            {
                "userId": user_id,
                "Fields": "MediaSources,Genres,ProviderIds,ImageTags",
            },
        )
        if not isinstance(item, dict):
            raise JellyfinUnavailableError("Playback service returned invalid audio details")
        return {
            "display_title": item.get("Name"),
            "artist": item.get("Artists", [None])[0] if isinstance(item.get("Artists"), list) and item.get("Artists") else None,
            "artists": item.get("Artists") if isinstance(item.get("Artists"), list) else [],
            "album": item.get("Album"),
            "album_artist": item.get("AlbumArtist"),
            "genres": item.get("Genres") if isinstance(item.get("Genres"), list) else [],
            "track_number": item.get("IndexNumber"),
            "disc_number": item.get("ParentIndexNumber"),
            "release_year": item.get("ProductionYear"),
            "runtime_ticks": item.get("RunTimeTicks"),
            "provider_ids": item.get("ProviderIds") if isinstance(item.get("ProviderIds"), dict) else {},
            "overview": item.get("Overview"),
        }

    def get_audio_lyrics(self, audio: JellyfinAudio) -> dict[str, object] | None:
        request = Request(
            f"{self._base_url}/Audio/{audio.item_id}/Lyrics",
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self._api_key,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise JellyfinUnavailableError(
                "Music lyrics are unavailable"
            ) from error
        except (
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            raise JellyfinUnavailableError(
                "Music lyrics are unavailable"
            ) from error
        if not isinstance(payload, dict):
            raise JellyfinUnavailableError(
                "Playback service returned invalid lyrics"
            )
        raw_lines = payload.get("Lyrics")
        lines = []
        if isinstance(raw_lines, list):
            for raw_line in raw_lines:
                if not isinstance(raw_line, dict):
                    continue
                text = raw_line.get("Text")
                if not isinstance(text, str) or not text.strip():
                    continue
                line: dict[str, object] = {"text": text.strip()}
                start = raw_line.get("Start")
                if isinstance(start, int):
                    line["start_ticks"] = start
                lines.append(line)
        if not lines:
            return None
        metadata = payload.get("Metadata")
        return {
            "text": "\n".join(str(line["text"]) for line in lines),
            "lines": lines,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    def open_audio_stream(
        self,
        audio: JellyfinAudio,
        range_header: str | None = None,
    ) -> JellyfinStream:
        users = self._get_json("/Users")
        first_user = users[0] if isinstance(users, list) and users else None
        user_id = first_user.get("Id") if isinstance(first_user, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise JellyfinUnavailableError("Playback service has no usable user context")
        query = urlencode(
            {
                "UserId": user_id,
                "DeviceId": "personal-vault",
                "MediaSourceId": audio.media_source_id,
                "MaxStreamingBitrate": "320000",
                "Container": "mp3",
                "TranscodingContainer": "mp3",
                "TranscodingProtocol": "http",
                "AudioCodec": "mp3",
                "EnableRedirection": "false",
            }
        )
        headers = {"Accept": "audio/mpeg", "X-Emby-Token": self._api_key}
        if range_header:
            headers["Range"] = range_header
        request = Request(
            f"{self._base_url}/Audio/{audio.item_id}/universal?{query}",
            headers=headers,
        )
        try:
            response = urlopen(request, timeout=60)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise JellyfinUnavailableError("Music playback is unavailable") from error
        response_headers = {
            name: value
            for name in ("Accept-Ranges", "Content-Length", "Content-Range")
            if (value := response.headers.get(name))
        }
        return JellyfinStream(
            response=response,
            status_code=response.status,
            headers=response_headers,
            url=response.geturl(),
            content_type=response.headers.get("Content-Type"),
        )

    def get_video_by_id(self, item_id: str) -> JellyfinMovie | None:
        users = self._get_json("/Users")
        first_user = users[0] if isinstance(users, list) and users else None
        user_id = (
            first_user.get("Id")
            if isinstance(first_user, dict)
            else None
        )
        if not isinstance(user_id, str) or not user_id:
            raise JellyfinUnavailableError(
                "Playback service has no usable user context"
            )
        item = self._get_json(
            f"/Items/{item_id}",
            {
                "userId": user_id,
                "Fields": "Path,MediaSources,ImageTags",
            },
        )
        if not isinstance(item, dict):
            return None
        return _parse_playback_item(item)

    def get_movie_details(
        self,
        movie: JellyfinMovie,
    ) -> JellyfinMovieDetails:
        users = self._get_json("/Users")
        first_user = users[0] if isinstance(users, list) and users else None
        user_id = (
            first_user.get("Id")
            if isinstance(first_user, dict)
            else None
        )
        if not isinstance(user_id, str) or not user_id:
            raise JellyfinUnavailableError(
                "Playback service has no usable user context"
            )

        user_query = {
            "userId": user_id,
            "Fields": "Chapters,MediaSources",
        }
        item = self._get_json(
            f"/Items/{movie.item_id}",
            user_query,
        )
        raw_extras = self._get_json(
            f"/Items/{movie.item_id}/SpecialFeatures",
            user_query,
        )
        raw_trailers = self._get_json(
            f"/Items/{movie.item_id}/LocalTrailers",
            user_query,
        )

        if (
            not isinstance(item, dict)
            or not isinstance(raw_extras, list)
            or not isinstance(raw_trailers, list)
        ):
            raise JellyfinUnavailableError(
                "Playback service returned invalid movie details"
            )

        people = tuple(
            JellyfinPerson(
                item_id=person["Id"],
                name=person["Name"],
                role=(
                    person.get("Role")
                    if isinstance(person.get("Role"), str)
                    else None
                ),
                person_type=(
                    person.get("Type")
                    if isinstance(person.get("Type"), str)
                    else None
                ),
                has_image=bool(person.get("PrimaryImageTag")),
            )
            for person in item.get("People", [])
            if (
                isinstance(person, dict)
                and isinstance(person.get("Id"), str)
                and isinstance(person.get("Name"), str)
            )
        )

        def parse_extras(entries: list[object]) -> tuple[JellyfinExtra, ...]:
            return tuple(
                JellyfinExtra(
                    item_id=entry["Id"],
                    name=entry["Name"],
                    runtime_ticks=(
                        entry.get("RunTimeTicks")
                        if isinstance(entry.get("RunTimeTicks"), int)
                        else None
                    ),
                    has_image=(
                        isinstance(entry.get("ImageTags"), dict)
                        and isinstance(
                            entry.get("ImageTags", {}).get("Primary"),
                            str,
                        )
                    ),
                )
                for entry in entries
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("Id"), str)
                    and isinstance(entry.get("Name"), str)
                )
            )

        image_tags = item.get("ImageTags")
        backdrop_tags = item.get("BackdropImageTags")
        raw_taglines = item.get("Taglines")
        tagline = next(
            (
                value
                for value in raw_taglines
                if isinstance(value, str) and value
            ),
            None,
        ) if isinstance(raw_taglines, list) else None
        raw_collections = item.get("Collections")
        collection_names: list[object] = [item.get("CollectionName")]
        if isinstance(raw_collections, list):
            collection_names.extend(
                collection.get("Name")
                for collection in raw_collections
                if isinstance(collection, dict)
            )
        collections = tuple(
            dict.fromkeys(
                value
                for value in collection_names
                if isinstance(value, str) and value.strip()
            )
        )
        chapters = tuple(
            JellyfinChapter(
                name=(
                    chapter.get("Name")
                    if isinstance(chapter.get("Name"), str)
                    else f"Chapter {index + 1}"
                ),
                start_ticks=chapter["StartPositionTicks"],
            )
            for index, chapter in enumerate(item.get("Chapters", []))
            if (
                isinstance(chapter, dict)
                and isinstance(chapter.get("StartPositionTicks"), int)
            )
        )
        media_sources = item.get("MediaSources")
        source = next(
            (
                candidate
                for candidate in media_sources
                if (
                    isinstance(candidate, dict)
                    and candidate.get("Id") == movie.media_source_id
                )
            ),
            None,
        ) if isinstance(media_sources, list) else None
        subtitles = tuple(
            JellyfinSubtitle(
                index=stream["Index"],
                title=(
                    stream.get("Title")
                    if isinstance(stream.get("Title"), str)
                    else None
                ),
                language=(
                    stream.get("Language")
                    if isinstance(stream.get("Language"), str)
                    else None
                ),
                codec=(
                    stream.get("Codec")
                    if isinstance(stream.get("Codec"), str)
                    else None
                ),
                is_external=bool(stream.get("IsExternal")),
            )
            for stream in (
                source.get("MediaStreams", [])
                if isinstance(source, dict)
                else []
            )
            if (
                isinstance(stream, dict)
                and stream.get("Type") == "Subtitle"
                and isinstance(stream.get("Index"), int)
            )
        )

        return JellyfinMovieDetails(
            title=(
                item.get("Name")
                if isinstance(item.get("Name"), str)
                else "Untitled movie"
            ),
            year=(
                item.get("ProductionYear")
                if isinstance(item.get("ProductionYear"), int)
                else None
            ),
            official_rating=(
                item.get("OfficialRating")
                if isinstance(item.get("OfficialRating"), str)
                else None
            ),
            community_rating=(
                float(item["CommunityRating"])
                if isinstance(item.get("CommunityRating"), (int, float))
                else None
            ),
            runtime_ticks=(
                item.get("RunTimeTicks")
                if isinstance(item.get("RunTimeTicks"), int)
                else None
            ),
            overview=(
                item.get("Overview")
                if isinstance(item.get("Overview"), str)
                else None
            ),
            tagline=tagline,
            genres=tuple(
                value
                for value in item.get("Genres", [])
                if isinstance(value, str)
            ),
            studios=tuple(
                studio["Name"]
                for studio in item.get("Studios", [])
                if (
                    isinstance(studio, dict)
                    and isinstance(studio.get("Name"), str)
                )
            ),
            people=people,
            extras=parse_extras(raw_extras),
            trailers=parse_extras(raw_trailers),
            has_primary_image=(
                isinstance(image_tags, dict)
                and isinstance(image_tags.get("Primary"), str)
            ),
            has_backdrop_image=(
                isinstance(backdrop_tags, list)
                and any(isinstance(tag, str) for tag in backdrop_tags)
            ),
            provider_ids={
                str(name): str(value)
                for name, value in (
                    item.get("ProviderIds", {}).items()
                    if isinstance(item.get("ProviderIds"), dict)
                    else ()
                )
                if value
            },
            edition=(
                item.get("EditionName")
                if isinstance(item.get("EditionName"), str)
                else None
            ),
            collections=collections,
            chapters=chapters,
            subtitles=subtitles,
        )

    def get_image_url(
        self,
        item_id: str,
        image_type: str,
        *,
        image_index: int | None = None,
        max_width: int,
    ) -> str:
        index = f"/{image_index}" if image_index is not None else ""
        query = urlencode(
            {
                "maxWidth": str(max_width),
                "quality": "88",
            }
        )
        return (
            f"{self._base_url}/Items/{item_id}/Images/"
            f"{image_type}{index}?{query}"
        )

    def open_browser_stream(
        self,
        movie: JellyfinMovie,
        range_header: str | None = None,
    ) -> JellyfinStream:
        query = urlencode(
            {
                "Static": "false",
                "MediaSourceId": movie.media_source_id,
                "Container": "mp4",
                "VideoCodec": "h264",
                "AudioCodec": "aac",
                "AudioChannels": "2",
                "AudioBitRate": "192000",
                "VideoBitRate": "12000000",
                "MaxWidth": "1920",
                "MaxHeight": "1080",
                "AllowVideoStreamCopy": "false",
                "AllowAudioStreamCopy": "false",
                "RequireAvc": "true",
                "Context": "Streaming",
            }
        )
        headers = {
            "Accept": "video/mp4",
            "X-Emby-Token": self._api_key,
        }
        if range_header:
            headers["Range"] = range_header

        request = Request(
            (
                f"{self._base_url}/Videos/{movie.item_id}"
                f"/stream.mp4?{query}"
            ),
            headers=headers,
        )

        try:
            response = urlopen(
                request,
                timeout=60,
            )
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
        ) as error:
            raise JellyfinUnavailableError(
                "Playback stream is unavailable"
            ) from error

        response_headers = {
            name: value
            for name in (
                "Accept-Ranges",
                "Content-Length",
                "Content-Range",
            )
            if (value := response.headers.get(name))
        }

        return JellyfinStream(
            response=response,
            status_code=response.status,
            headers=response_headers,
            url=response.geturl(),
            content_type=response.headers.get("Content-Type"),
        )

    def open_hls_master(
        self,
        movie: JellyfinMovie,
        subtitle_stream_index: int | None = None,
    ) -> JellyfinStream:
        parameters = {
            "MediaSourceId": movie.media_source_id,
            "VideoCodec": "h264",
            "AudioCodec": "aac",
            "AudioChannels": "2",
            "AudioBitRate": "192000",
            "VideoBitRate": "12000000",
            "MaxWidth": "1920",
            "MaxHeight": "1080",
            "AllowVideoStreamCopy": "false",
            "AllowAudioStreamCopy": "false",
            "RequireAvc": "true",
            "SegmentContainer": "ts",
            "MinSegments": "1",
            "BreakOnNonKeyFrames": "false",
            "Context": "Streaming",
            "PlaySessionId": secrets.token_hex(16),
        }
        if subtitle_stream_index is not None:
            parameters["SubtitleStreamIndex"] = str(
                subtitle_stream_index
            )
            parameters["SubtitleMethod"] = "Encode"
        query = urlencode(parameters)
        return self.open_resource(
            (
                f"{self._base_url}/Videos/{movie.item_id}"
                f"/master.m3u8?{query}"
            )
        )

    def open_resource(self, url: str) -> JellyfinStream:
        if not url.startswith(f"{self._base_url}/"):
            raise JellyfinUnavailableError(
                "Playback resource is invalid"
            )

        request = Request(
            url,
            headers={
                "Accept": "*/*",
                "X-Emby-Token": self._api_key,
            },
        )

        try:
            response = urlopen(
                request,
                timeout=60,
            )
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
        ) as error:
            raise JellyfinUnavailableError(
                "Playback resource is unavailable"
            ) from error

        response_headers = {
            name: value
            for name in (
                "Content-Length",
                "Content-Range",
            )
            if (value := response.headers.get(name))
        }

        return JellyfinStream(
            response=response,
            status_code=response.status,
            headers=response_headers,
            url=response.geturl(),
            content_type=response.headers.get("Content-Type"),
        )

    def open_hls_resource(self, url: str) -> JellyfinStream:
        return self.open_resource(url)
