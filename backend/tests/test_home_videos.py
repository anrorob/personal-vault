from app.home_videos import CANONICAL_HOME_VIDEOS_PATH, get_home_videos_path


def test_home_videos_path_prefers_canonical_environment(monkeypatch, tmp_path):
    canonical = tmp_path / "home-videos"
    legacy = tmp_path / "legacy-videos"
    monkeypatch.setenv("PV_HOME_VIDEOS_PATH", str(canonical))
    monkeypatch.setenv("PV_PERSONAL_VIDEOS_PATH", str(legacy))

    assert get_home_videos_path() == canonical


def test_home_videos_path_accepts_legacy_environment(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy-videos"
    monkeypatch.delenv("PV_HOME_VIDEOS_PATH", raising=False)
    monkeypatch.setenv("PV_PERSONAL_VIDEOS_PATH", str(legacy))

    assert get_home_videos_path() == legacy


def test_home_videos_path_uses_canonical_default(monkeypatch):
    monkeypatch.delenv("PV_HOME_VIDEOS_PATH", raising=False)
    monkeypatch.delenv("PV_PERSONAL_VIDEOS_PATH", raising=False)

    assert get_home_videos_path() == CANONICAL_HOME_VIDEOS_PATH
