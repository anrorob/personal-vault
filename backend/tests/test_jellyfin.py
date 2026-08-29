from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError
from urllib.request import Request

import pytest

from app import jellyfin
from app.jellyfin import (
    HlsResourceStore,
    JellyfinClient,
    JellyfinUnavailableError,
    rewrite_hls_playlist,
)


class FakeResponse(BytesIO):
    status = 200
    headers: dict[str, str] = {
        "Content-Type": "application/vnd.apple.mpegurl"
    }
    response_url = "http://pv-jellyfin:8096/test"

    def geturl(self) -> str:
        return self.response_url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()


def test_client_matches_exact_source_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_body = {
        "Items": [
            {
                "Id": "matrix-item-id",
                "Path": "/media/movies/The Matrix (1999)/matrix.mkv",
                "MediaSources": [
                    {
                        "Id": "matrix-media-source",
                        "Container": "mkv",
                        "MediaStreams": [
                            {"Type": "Video", "Codec": "vc1"},
                            {"Type": "Audio", "Codec": "ac3"},
                            {"Type": "Audio", "Codec": "ac3"},
                            {"Type": "Audio", "Codec": "truehd"},
                            {
                                "Type": "Subtitle",
                                "Index": 8,
                                "Title": "Polish SDH",
                                "DisplayTitle": "Polish SDH - PGSSUB",
                                "Language": "pol",
                                "Codec": "PGSSUB",
                                "IsExternal": False,
                                "IsDefault": True,
                                "IsForced": False,
                                "IsHearingImpaired": True,
                            },
                        ],
                    }
                ],
            }
        ]
    }
    captured_request: Request | None = None

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        assert timeout == 10
        return FakeResponse(json.dumps(response_body).encode("utf-8"))

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096/",
        "secret-api-key",
    )

    movie = client.find_movie_by_path(
        Path("/media/movies/The Matrix (1999)/matrix.mkv")
    )

    assert movie is not None
    assert movie.item_id == "matrix-item-id"
    assert movie.media_source_id == "matrix-media-source"
    assert movie.container == "mkv"
    assert movie.video_codec == "vc1"
    assert movie.audio_codecs == ("ac3", "truehd")
    assert len(movie.subtitle_tracks) == 1
    assert movie.subtitle_tracks[0].index == 8
    assert movie.subtitle_tracks[0].language == "pol"
    assert movie.subtitle_tracks[0].title == "Polish SDH"
    assert movie.subtitle_tracks[0].is_default is True
    assert movie.subtitle_tracks[0].is_hearing_impaired is True
    assert captured_request is not None
    assert captured_request.full_url.startswith(
        "http://pv-jellyfin:8096/Items?"
    )
    assert captured_request.get_header("X-emby-token") == (
        "secret-api-key"
    )


def test_client_returns_none_when_path_is_not_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        return FakeResponse(b'{"Items":[]}')

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )

    movie = client.find_movie_by_path(
        Path("/media/movies/not-indexed.mkv")
    )

    assert movie is None


def test_tv_metadata_uses_jellyfin_10_11_compatible_item_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Request | None = None

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        return FakeResponse(
            b'{"Items":[{"Id":"episode-id","Name":"The First Crisis"}]}'
        )

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient("http://pv-jellyfin:8096", "secret-api-key")

    assert client.get_tv_item_metadata("episode-id") == {
        "Id": "episode-id", "Name": "The First Crisis"
    }
    assert captured_request is not None
    parsed = parse_qs(urlsplit(captured_request.full_url).query)
    assert urlsplit(captured_request.full_url).path == "/Items"
    assert parsed["Ids"] == ["episode-id"]


def test_client_publishes_targeted_media_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Request | None = None

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        assert timeout == 10
        return FakeResponse(b"")

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient("http://pv-jellyfin:8096/", "secret-api-key")

    client.notify_media_updated(
        (
            PurePosixPath("/media/music/Artist/Album/01 Track.wma"),
            PurePosixPath("/media/tv/Series/S01E01.mkv"),
        )
    )

    assert captured_request is not None
    assert captured_request.full_url == (
        "http://pv-jellyfin:8096/Library/Media/Updated"
    )
    assert captured_request.method == "POST"
    assert captured_request.get_header("X-emby-token") == "secret-api-key"
    assert json.loads(captured_request.data or b"") == {
        "Updates": [
            {
                "Path": "/media/music/Artist/Album/01 Track.wma",
                "UpdateType": "Created",
            },
            {
                "Path": "/media/tv/Series/S01E01.mkv",
                "UpdateType": "Created",
            },
        ]
    }


def test_client_rejects_relative_media_update_paths() -> None:
    client = JellyfinClient("http://pv-jellyfin:8096", "secret-api-key")

    with pytest.raises(ValueError, match="must be absolute"):
        client.notify_media_updated((PurePosixPath("relative/track.wma"),))


def test_client_starts_library_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_request: Request | None = None

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        assert timeout == 10
        return FakeResponse(b"")

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient("http://pv-jellyfin:8096/", "secret-api-key")

    client.refresh_library()

    assert captured_request is not None
    assert captured_request.full_url == "http://pv-jellyfin:8096/Library/Refresh"
    assert captured_request.method == "POST"
    assert captured_request.get_header("X-emby-token") == "secret-api-key"


def test_client_wraps_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        raise URLError("test connection failure")

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )

    with pytest.raises(
        JellyfinUnavailableError,
        match="Playback service is unavailable",
    ):
        client.find_movie_by_path(
            Path("/media/movies/The Matrix (1999)/matrix.mkv")
        )


def test_browser_stream_forces_compatible_transcoding_without_key_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Request | None = None

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        assert timeout == 60
        return FakeResponse(b"transcoded-video")

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )
    movie = jellyfin.JellyfinMovie(
        item_id="matrix-item-id",
        media_source_id="matrix-media-source",
        path="/media/movies/The Matrix (1999)/matrix.mkv",
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )

    stream = client.open_browser_stream(
        movie,
        range_header="bytes=0-1023",
    )
    body = b"".join(stream.iter_bytes())

    assert body == b"transcoded-video"
    assert captured_request is not None
    parsed_url = urlsplit(captured_request.full_url)
    query = parse_qs(parsed_url.query)
    assert parsed_url.path == (
        "/Videos/matrix-item-id/stream.mp4"
    )
    assert query["MediaSourceId"] == ["matrix-media-source"]
    assert query["VideoCodec"] == ["h264"]
    assert query["AudioCodec"] == ["aac"]
    assert query["AllowVideoStreamCopy"] == ["false"]
    assert "api_key" not in query
    assert captured_request.get_header("X-emby-token") == (
        "secret-api-key"
    )
    assert captured_request.get_header("Range") == "bytes=0-1023"


def test_hls_master_forces_seekable_browser_transcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Request | None = None

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        response = FakeResponse(b"#EXTM3U\n")
        response.response_url = request.full_url
        return response

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )
    movie = jellyfin.JellyfinMovie(
        item_id="matrix-item-id",
        media_source_id="matrix-media-source",
        path="/media/movies/The Matrix (1999)/matrix.mkv",
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )

    stream = client.open_hls_master(movie)

    assert captured_request is not None
    parsed_url = urlsplit(captured_request.full_url)
    query = parse_qs(parsed_url.query)
    assert parsed_url.path == "/Videos/matrix-item-id/master.m3u8"
    assert query["MediaSourceId"] == ["matrix-media-source"]
    assert query["VideoCodec"] == ["h264"]
    assert query["AudioCodec"] == ["aac"]
    assert query["SegmentContainer"] == ["ts"]
    assert query["AllowVideoStreamCopy"] == ["false"]
    assert len(query["PlaySessionId"][0]) == 32
    assert "SubtitleStreamIndex" not in query
    assert "SubtitleMethod" not in query
    assert captured_request.get_header("X-emby-token") == (
        "secret-api-key"
    )
    assert stream.content_type == "application/vnd.apple.mpegurl"


def test_hls_master_selects_subtitle_without_changing_audio_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Request | None = None

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        nonlocal captured_request
        captured_request = request
        response = FakeResponse(b"#EXTM3U\n")
        response.response_url = request.full_url
        return response

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )
    movie = jellyfin.JellyfinMovie(
        item_id="matrix-item-id",
        media_source_id="matrix-media-source",
        path="/media/movies/The Matrix (1999)/matrix.mkv",
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )

    client.open_hls_master(movie, subtitle_stream_index=8)

    assert captured_request is not None
    query = parse_qs(urlsplit(captured_request.full_url).query)
    assert query["SubtitleStreamIndex"] == ["8"]
    assert query["SubtitleMethod"] == ["Encode"]
    assert len(query["PlaySessionId"][0]) == 32
    assert query["VideoCodec"] == ["h264"]
    assert query["AudioCodec"] == ["aac"]
    assert query["AllowVideoStreamCopy"] == ["false"]
    assert query["AllowAudioStreamCopy"] == ["false"]


def test_hls_master_isolates_each_transcode_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        requested_urls.append(request.full_url)
        response = FakeResponse(b"#EXTM3U\n")
        response.response_url = request.full_url
        return response

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )
    movie = jellyfin.JellyfinMovie(
        item_id="matrix-item-id",
        media_source_id="matrix-media-source",
        path="/media/movies/The Matrix (1999)/matrix.mkv",
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )

    client.open_hls_master(movie).response.close()
    client.open_hls_master(movie, subtitle_stream_index=8).response.close()

    play_session_ids = [
        parse_qs(urlsplit(url).query)["PlaySessionId"][0]
        for url in requested_urls
    ]
    assert len(play_session_ids) == 2
    assert play_session_ids[0] != play_session_ids[1]


def test_hls_playlist_rewrites_lines_and_uri_attributes() -> None:
    issued_urls: list[str] = []

    def proxy_url_for(upstream_url: str) -> str:
        issued_urls.append(upstream_url)
        return f"/proxy/{len(issued_urls)}"

    rewritten = rewrite_hls_playlist(
        (
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,URI="audio/main.m3u8"\n'
            "video/main.m3u8?session=one\n"
        ),
        "http://pv-jellyfin:8096/Videos/item/master.m3u8",
        proxy_url_for,
    )

    assert rewritten == (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,URI="/proxy/1"\n'
        "/proxy/2\n"
    )
    assert issued_urls == [
        "http://pv-jellyfin:8096/Videos/item/audio/main.m3u8",
        (
            "http://pv-jellyfin:8096/Videos/item/"
            "video/main.m3u8?session=one"
        ),
    ]


def test_hls_resources_are_opaque_and_bound_to_user() -> None:
    store = HlsResourceStore()
    upstream_url = "http://pv-jellyfin:8096/Videos/item/segment.ts"
    owner_user_id = uuid4()

    token = store.issue(owner_user_id, upstream_url)

    assert upstream_url not in token
    assert store.resolve(owner_user_id, token) == upstream_url
    assert store.resolve(uuid4(), token) is None


def test_client_loads_rich_movie_details_with_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        requested_urls.append(request.full_url)
        path = urlsplit(request.full_url).path

        if path == "/Users":
            body: object = [{"Id": "private-user-id"}]
        elif path == "/Items/matrix-item-id":
            body = {
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "OfficialRating": "GB-15",
                "CommunityRating": 8.3,
                "RunTimeTicks": 81_779_610_000,
                "Overview": "A hacker discovers the truth.",
                "Taglines": ["Believe the unbelievable."],
                "EditionName": "Remastered",
                "CollectionName": "The Matrix Collection",
                "Genres": ["Action", "Science Fiction"],
                "Studios": [{"Name": "Warner Bros. Pictures"}],
                "ImageTags": {"Primary": "poster-tag"},
                "BackdropImageTags": ["backdrop-tag"],
                "People": [
                    {
                        "Id": "keanu-id",
                        "Name": "Keanu Reeves",
                        "Role": "Neo",
                        "Type": "Actor",
                        "PrimaryImageTag": "person-tag",
                    }
                ],
                "Chapters": [
                    {"Name": "Wake up", "StartPositionTicks": 0},
                    {
                        "Name": "Follow the white rabbit",
                        "StartPositionTicks": 6_000_000_000,
                    },
                ],
                "MediaSources": [
                    {
                        "Id": "matrix-source-id",
                        "MediaStreams": [
                            {
                                "Type": "Subtitle",
                                "Index": 5,
                                "Title": "English SDH",
                                "Language": "eng",
                                "Codec": "subrip",
                                "IsExternal": True,
                            }
                        ],
                    }
                ],
            }
        elif path.endswith("/SpecialFeatures"):
            body = [
                {
                    "Id": "extra-id",
                    "Name": "title_t02",
                    "RunTimeTicks": 25_040_430_000,
                }
            ]
        elif path.endswith("/LocalTrailers"):
            body = []
        else:
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        return FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )
    movie = jellyfin.JellyfinMovie(
        item_id="matrix-item-id",
        media_source_id="matrix-source-id",
        path="/media/movies/The Matrix (1999)/matrix.mkv",
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )

    details = client.get_movie_details(movie)

    assert details.title == "The Matrix"
    assert details.year == 1999
    assert details.community_rating == 8.3
    assert details.tagline == "Believe the unbelievable."
    assert details.genres == ("Action", "Science Fiction")
    assert details.edition == "Remastered"
    assert details.collections == ("The Matrix Collection",)
    assert details.chapters[1].name == "Follow the white rabbit"
    assert details.chapters[1].start_ticks == 6_000_000_000
    assert details.subtitles[0].language == "eng"
    assert details.subtitles[0].is_external is True
    assert details.people[0].name == "Keanu Reeves"
    assert details.people[0].has_image is True
    assert details.extras[0].name == "title_t02"
    assert details.has_primary_image is True
    assert details.has_backdrop_image is True
    assert all(
        parse_qs(urlsplit(url).query).get("userId")
        == ["private-user-id"]
        for url in requested_urls[1:]
    )


def test_client_loads_audio_details_with_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        requested_urls.append(request.full_url)
        path = urlsplit(request.full_url).path

        if path == "/Users":
            body: object = [{"Id": "private-user-id"}]
        elif path == "/Items/audio-item-id":
            body = {
                "Name": "One day",
                "Artists": ["Example Artist"],
                "Album": "Example Album",
                "AlbumArtist": "Example Artist",
                "Genres": ["Rock"],
                "IndexNumber": 13,
                "ParentIndexNumber": 1,
                "ProductionYear": 2007,
                "ProviderIds": {"MusicBrainzTrack": "track-id"},
            }
        else:
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        return FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )
    audio = jellyfin.JellyfinAudio(
        item_id="audio-item-id",
        media_source_id="audio-source-id",
        path="/media/music/Example Album/13 Track13.wma",
        container="asf",
        audio_codec="wmalossless",
    )

    details = client.get_audio_details(audio)

    assert details["display_title"] == "One day"
    assert details["artist"] == "Example Artist"
    assert details["track_number"] == 13
    assert urlsplit(requested_urls[0]).path == "/Users"
    item_url = urlsplit(requested_urls[1])
    assert item_url.path == "/Items/audio-item-id"
    assert parse_qs(item_url.query)["userId"] == ["private-user-id"]
    assert parse_qs(item_url.query)["Fields"] == [
        "MediaSources,Genres,ProviderIds,ImageTags"
    ]


def test_client_uses_album_artwork_for_audio_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        assert urlsplit(request.full_url).path == "/Items"
        return FakeResponse(
            json.dumps(
                {
                    "Items": [
                        {
                            "Id": "audio-id",
                            "Path": "/media/music/Album/track.flac",
                            "AlbumId": "album-id",
                            "AlbumPrimaryImageTag": "album-image-tag",
                            "MediaSources": [
                                {
                                    "Id": "source-id",
                                    "Container": "flac",
                                    "MediaStreams": [
                                        {"Type": "Audio", "Codec": "flac"}
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient("http://pv-jellyfin:8096", "secret-api-key")

    audio = client.find_audio_by_path(
        Path("/media/music/Album/track.flac")
    )

    assert audio is not None
    assert audio.has_primary_image is True
    assert audio.artwork_item_id == "album-id"


def test_client_loads_normalised_audio_lyrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        assert urlsplit(request.full_url).path == "/Audio/audio-item-id/Lyrics"
        assert timeout == 10
        return FakeResponse(
            json.dumps(
                {
                    "Metadata": {"Artist": "Massive Attack"},
                    "Lyrics": [
                        {"Text": "Love, love is a verb", "Start": 0},
                        {"Text": "Love is a doing word", "Start": 42},
                    ],
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient("http://pv-jellyfin:8096", "secret-api-key")
    audio = jellyfin.JellyfinAudio(
        "audio-item-id",
        "audio-source-id",
        "/media/music/track.flac",
        "flac",
        "flac",
    )

    lyrics = client.get_audio_lyrics(audio)

    assert lyrics == {
        "text": "Love, love is a verb\nLove is a doing word",
        "lines": [
            {"text": "Love, love is a verb", "start_ticks": 0},
            {"text": "Love is a doing word", "start_ticks": 42},
        ],
        "metadata": {"Artist": "Massive Attack"},
    }


def test_client_resolves_feature_playback_with_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(
        request: Request,
        timeout: float,
    ) -> FakeResponse:
        requested_urls.append(request.full_url)
        path = urlsplit(request.full_url).path

        if path == "/Users":
            body: object = [{"Id": "private-user-id"}]
        elif path == "/Items/extra-id":
            body = {
                "Id": "extra-id",
                "Path": "/media/movies/The Matrix/extras/title_t02.mkv",
                "MediaSources": [
                    {
                        "Id": "extra-source-id",
                        "Container": "mkv",
                        "MediaStreams": [
                            {"Type": "Video", "Codec": "h264"},
                            {"Type": "Audio", "Codec": "aac"},
                        ],
                    }
                ],
            }
        else:
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        return FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(jellyfin, "urlopen", fake_urlopen)
    client = JellyfinClient(
        "http://pv-jellyfin:8096",
        "secret-api-key",
    )

    feature = client.get_video_by_id("extra-id")

    assert feature is not None
    assert feature.item_id == "extra-id"
    assert feature.media_source_id == "extra-source-id"
    item_query = parse_qs(urlsplit(requested_urls[1]).query)
    assert item_query["userId"] == ["private-user-id"]
    assert item_query["Fields"] == ["Path,MediaSources,ImageTags"]
