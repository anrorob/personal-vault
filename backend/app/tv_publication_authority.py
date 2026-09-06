"""Resolve approved TV identity from durable review, never intake filenames."""
from pathlib import PurePosixPath
from uuid import UUID


def season_marker(batch_id, title, season, tracks):
    return {
        "schema": "personal-vault.tv-publication-set.v1",
        "source_directory": f"tv-resolver/{batch_id}/season-{season:02d}",
        "show_title": title,
        "season_number": season,
        "members": [
            {"item_id": str(t["arrival_item_id"]), "episode_number": t["proposed_episode_number"]}
            for t in sorted(tracks, key=lambda t: t["proposed_episode_number"])
        ],
    }


def approved_projection(cursor, item):
    """Return an owner/checksum-bound projection, including erased-marker cases.

    No writes or filesystem access. An inconsistent approval fails closed.
    Bootstrap and older installations can have no resolver tables yet.
    """
    cursor.execute("SELECT to_regclass('vault_tv_resolver_tracks') AS relation")
    if cursor.fetchone()["relation"] is None:
        return None
    cursor.execute(
        """SELECT t.*, b.owner_user_id, b.proposed_show_title, b.review_metadata
           FROM vault_tv_resolver_tracks t JOIN vault_tv_resolver_batches b ON b.id=t.batch_id
           WHERE t.arrival_item_id=%s AND (t.classification='likely_episode'
             OR (t.classification='likely_extra' AND b.review_metadata ? 'extras_approved_at'))
             AND b.status IN ('approved','publishing','published','failed','complete')""", (item.id,),
    )
    rows = cursor.fetchall()
    if not rows:
        if item.metadata.get("tv_resolver_batch_id"):
            raise ValueError("TV resolver approval is missing")
        return None
    if len(rows) != 1:
        raise ValueError("TV resolver approval is ambiguous")
    track = rows[0]
    review = track["review_metadata"]
    title = track["proposed_show_title"]
    season, episode = track["proposed_season_number"], track["proposed_episode_number"]
    if (item.source_kind != "incoming" or track["owner_user_id"] != item.owner_user_id or track["checksum"] != item.sha256
        or review.get("approved_by") != str(item.owner_user_id)
        or not review.get("approved_at") or review.get("audience") not in {"private", "vault-wide"}
        or not title or not isinstance(season, int) or season <= 0):
        raise ValueError("TV resolver approved evidence no longer agrees")
    if track['classification'] == 'likely_extra':
        from app.tv_extras import extra_projection
        return extra_projection(cursor, item, track)
    if not isinstance(episode, int) or episode <= 0:
        raise ValueError("TV resolver approved episode is invalid")
    destination = f"/vault/Theatre/TV Shows/{title}/Season {season:02d}/{title} - S{season:02d}E{episode:02d}{PurePosixPath(track['original_filename']).suffix.lower()}"
    if track["canonical_destination"] != destination or ".." in PurePosixPath(destination).parts:
        raise ValueError("TV resolver canonical mapping is invalid")
    cursor.execute(
        """SELECT * FROM vault_tv_resolver_tracks WHERE batch_id=%s
           AND proposed_season_number=%s AND classification='likely_episode'""",
        (track["batch_id"], season),
    )
    members = cursor.fetchall()
    numbers = [m["proposed_episode_number"] for m in members]
    if any(not isinstance(n, int) or n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("TV resolver season mapping is invalid")
    return {
        "proposed_category": "TV Shows", "proposed_destination": destination,
        "publication_audience": review["audience"],
        "metadata": {
            **item.metadata,
            "tv_resolver_batch_id": str(track["batch_id"]),
            "tv_publication_set": season_marker(track["batch_id"], title, season, members),
            "tv_resolver_publication": {
                "track_id": str(track["id"]), "item_id": str(item.id),
                "owner_user_id": str(item.owner_user_id), "sha256": item.sha256,
                "canonical_destination": destination,
            },
        },
    }


def validate_request_authority(item):
    """Defence at request creation, including the in-memory test double."""
    if not item.metadata.get("tv_resolver_batch_id"):
        return
    evidence = item.metadata.get("tv_resolver_publication")
    if (not isinstance(evidence, dict) or item.proposed_category != "TV Shows"
        or not (item.proposed_destination or "").startswith("/vault/Theatre/TV Shows/")
        or evidence.get("canonical_destination") != item.proposed_destination
        or evidence.get("item_id") != str(item.id)
        or evidence.get("owner_user_id") != str(item.owner_user_id)
        or evidence.get("sha256") != item.sha256):
        raise ValueError("TV resolver episodes require their approved canonical TV destination")
    UUID(str(evidence.get("track_id")))
