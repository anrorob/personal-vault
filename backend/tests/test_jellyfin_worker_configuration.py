import pytest

from app.main import jellyfin_enabled


@pytest.mark.parametrize("value", ("0", "false", "no", "off"))
def test_jellyfin_worker_can_be_disabled(monkeypatch, value: str) -> None:
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", value)

    assert not jellyfin_enabled()


def test_jellyfin_worker_is_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PV_JELLYFIN_ENABLED", raising=False)

    assert jellyfin_enabled()
