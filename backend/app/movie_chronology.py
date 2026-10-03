"""Reviewed public chronology references, independent of provider availability.

These are explicit publisher viewing chronologies, NOT inferred story dates.
Membership is never derived here. Only a user-selected member's provider ID is
matched. New/disputed/unlisted films remain unresolved until a reviewed source
update; a refresh copies this version into the user's durable franchise record.
"""
from copy import deepcopy


REFERENCE_VERSION = "2026-09-29"

# TMDB movie identities, in the publisher's explicit chronological viewing order.
# Deliberately bounded coverage: no speculative placement of later releases,
# alternate-universe films, or titles absent from the cited guide.
REFERENCES = (
    {
        "key": "mcu",
        "label": "Disney+ MCU chronology",
        "url": "https://www.disneyplus.com/ja-jp/explore/articles/mcu-series",
        "version": REFERENCE_VERSION,
        "positions": {
            str(movie_id): index * 10
            for index, movie_id in enumerate((
                1771, 299537, 1726, 10138, 1724, 10195, 24428, 76338,
                68721, 100402, 118340, 283995, 99861, 102899, 271110,
                497698, 284054, 315635, 284052, 284053, 363088, 299536,
                299534, 566525, 429617, 524434, 453395, 505642, 616037,
                640146, 447365, 609681,
            ), start=1)
        },
    },
    {
        "key": "star-wars",
        "label": "Lucasfilm chronological viewing guide",
        "url": "https://www.starwars.com/news/star-wars-movies-and-series-guide",
        "version": REFERENCE_VERSION,
        "positions": {
            str(movie_id): index * 10
            for index, movie_id in enumerate((
                1893, 1894, 12180, 1895, 348350, 330459, 11, 1891,
                1892, 140607, 181808, 181812,
            ), start=1)
        },
    },
)


def tmdb_movie_id(metadata: dict) -> str | None:
    providers = metadata.get("provider_ids")
    if not isinstance(providers, dict):
        return None
    values = {str(value).strip() for key, value in providers.items()
              if str(key).casefold() == "tmdb" and str(value).strip().isdigit()}
    return next(iter(values)) if len(values) == 1 else None


def resolve_reference(metadata: list[dict]) -> dict:
    """Select one supported chronology by immutable provider identity.

    The franchise name/title/filename/release year are never chronology inputs.
    Mixed supported universes cannot accidentally receive a fabricated timeline.
    """
    ids = {tmdb_movie_id(item) for item in metadata} - {None}
    matches = [reference for reference in REFERENCES
               if ids.intersection(reference["positions"])]
    return deepcopy(matches[0]) if len(matches) == 1 else {}
