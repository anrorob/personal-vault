import json
from pathlib import PurePosixPath

import pytest

from app.jellyfin_paths import CONFIG_KEY, JellyfinPathMappingError, jellyfin_media_path


def configure(monkeypatch, root, section="movies", destination="/media/movies"):
    monkeypatch.setenv(CONFIG_KEY, json.dumps([
        {"section": section, "pv_root": str(root), "jellyfin_root": destination}
    ]))


@pytest.mark.parametrize("section", ["movies", "tv"])
def test_explicit_development_mapping(monkeypatch, tmp_path, section):
    # Provider paths remain POSIX even when a test backend runs on Windows.
    root = tmp_path / section
    destination = "/development/" + section
    configure(monkeypatch, root, section, destination)
    assert jellyfin_media_path(root / "Example.mkv", section) == PurePosixPath(destination) / "Example.mkv"


@pytest.mark.parametrize("section,destination", [("movies", "/media/movies"), ("tv", "/media/tv")])
def test_commissioned_root_to_provider_translation(monkeypatch, tmp_path, section, destination):
    root = tmp_path / "commissioned-slot" / "Theatre" / section
    configure(monkeypatch, root, section, destination)
    relative = "Example Series [2026]/Season 01/Example — é & #1.mkv"
    assert jellyfin_media_path(root / relative, section) == PurePosixPath(destination) / relative


def test_identity_is_explicit_even_when_namespaces_match(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, destination=tmp_path.as_posix())
    if tmp_path.drive:  # The actual deployment's identity namespaces are POSIX.
        pytest.skip("POSIX identity root fixture requires a POSIX host")
    assert jellyfin_media_path(tmp_path / "Example.mkv", "movies") == PurePosixPath(tmp_path.as_posix()) / "Example.mkv"


@pytest.mark.parametrize("config", ["", " ", "[]", "null", "{}", "invalid", '[{"section":"movies"}]'])
def test_invalid_configured_mapping_fails_closed(monkeypatch, tmp_path, config):
    monkeypatch.setenv(CONFIG_KEY, config)
    with pytest.raises(JellyfinPathMappingError, match="mapping is unavailable"):
        jellyfin_media_path(tmp_path / "Example.mkv", "movies")


@pytest.mark.parametrize("section,method", [("movies", "find_movie_by_path"), ("tv", "find_episode_by_path")])
@pytest.mark.parametrize("mode", ["literal", "mapped", "different-unconfigured"])
def test_provider_lookup_compatibility(monkeypatch, tmp_path, section, method, mode):
    from io import BytesIO
    from app import jellyfin
    from app.jellyfin import JellyfinClient

    source = tmp_path / section / "Example.mkv"
    monkeypatch.delenv(CONFIG_KEY, raising=False)
    provider_path = source if mode == "literal" else PurePosixPath("/media") / section / source.name
    if mode == "mapped":
        configure(monkeypatch, source.parent, section, "/media/" + section)
    client = JellyfinClient("http://provider.test", "synthetic-key")
    response = {"Items": [{
        "Id": "example-item", "Path": str(provider_path),
        "MediaSources": [{"Id": "example-source", "Path": str(provider_path), "MediaStreams": []}],
    }]}
    monkeypatch.setattr(client, "_get_json", lambda *args: response)
    monkeypatch.setattr(jellyfin, "urlopen", lambda *args, **kwargs: BytesIO(json.dumps(response).encode()))
    lookup_path = jellyfin_media_path(source, section)
    if mode != "mapped":
        assert lookup_path is source  # No resolution, translation or inferred root.
    item = getattr(client, method)(lookup_path)
    if mode == "different-unconfigured":
        assert item is None
    else:
        assert item is not None
        assert item.media_source_id == "example-source"


def test_section_mismatch_and_prefix_spoof_are_denied(monkeypatch, tmp_path):
    root = tmp_path / "movies"
    configure(monkeypatch, root)
    for source, section in [(tmp_path / "movies-extra" / "Example.mkv", "movies"),
                            (root / "Example.mkv", "tv"), (root, "movies")]:
        with pytest.raises(JellyfinPathMappingError):
            jellyfin_media_path(source, section)


def test_traversal_is_denied_before_normalization(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    with pytest.raises(JellyfinPathMappingError):
        jellyfin_media_path(tmp_path / "nested" / ".." / "Example.mkv", "movies")


@pytest.mark.parametrize("destination", ["relative", "/media/../movies", "//other/movies", "/media\\movies"])
def test_unsafe_provider_root_is_denied(monkeypatch, tmp_path, destination):
    configure(monkeypatch, tmp_path, destination=destination)
    with pytest.raises(JellyfinPathMappingError):
        jellyfin_media_path(tmp_path / "Example.mkv", "movies")


def test_overlapping_mappings_are_ambiguous(monkeypatch, tmp_path):
    monkeypatch.setenv(CONFIG_KEY, json.dumps([
        {"section": "movies", "pv_root": str(tmp_path), "jellyfin_root": "/media/movies"},
        {"section": "movies", "pv_root": str(tmp_path / "nested"), "jellyfin_root": "/other"},
    ]))
    with pytest.raises(JellyfinPathMappingError):
        jellyfin_media_path(tmp_path / "nested" / "Example.mkv", "movies")


def test_symlink_escape_is_denied(monkeypatch, tmp_path):
    root, outside = tmp_path / "allowed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlinks")
    configure(monkeypatch, root)
    with pytest.raises(JellyfinPathMappingError):
        jellyfin_media_path(link / "Example.mkv", "movies")
