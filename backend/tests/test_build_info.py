from pathlib import Path

import pytest

from app.build_info import build_info, get_build_commit, get_environment, load_project_version


def test_version_loads_from_the_canonical_file(tmp_path: Path) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n", encoding="utf-8")

    assert load_project_version(version_file) == "1.2.3"


@pytest.mark.parametrize("value", ("", "1.2", "v1.2.3", "1.2.3-dev"))
def test_version_rejects_invalid_canonical_values(tmp_path: Path, value: str) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text(value, encoding="utf-8")

    with pytest.raises(RuntimeError, match="MAJOR.MINOR.PATCH"):
        load_project_version(version_file)


def test_build_info_reports_safe_development_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_ENVIRONMENT", "development")
    monkeypatch.setenv("PV_COMMIT", "abc1234")

    reported = build_info()

    assert reported == {"version": "1.0.0", "commit": "abc1234", "environment": "development"}


def test_production_requires_an_immutable_commit_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_ENVIRONMENT", "production")
    monkeypatch.setenv("PV_COMMIT", "unknown")

    with pytest.raises(RuntimeError, match="immutable Git commit"):
        get_build_commit()


@pytest.mark.parametrize("value", ("preview", "", "Production "))
def test_environment_rejects_unrecognised_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PV_ENVIRONMENT", value)

    with pytest.raises(RuntimeError, match="PV_ENVIRONMENT"):
        get_environment()
