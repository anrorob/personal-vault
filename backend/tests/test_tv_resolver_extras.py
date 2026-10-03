import pytest

from app.tv_extras import extra_destination


def test_extra_destination_is_canonical_tv_season_extras_not_home_videos() -> None:
    destination = extra_destination("Synthetic Show", 2, "Original featurette.mkv")
    assert destination == "/vault/Theatre/TV Shows/Synthetic Show/Season 02/Extras/Original featurette.mkv"
    assert "Home Videos" not in destination


@pytest.mark.parametrize("filename", ["../unsafe.mkv", "nested/unsafe.mkv", "nested\\unsafe.mkv", ".."])
def test_extra_destination_rejects_unsafe_filenames(filename: str) -> None:
    with pytest.raises(ValueError):
        extra_destination("Synthetic Show", 1, filename)
