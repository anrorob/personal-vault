"""Resolve approved TV identity from durable review, never intake proposals."""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID


def season_marker(batch_id, title, season, tracks):
    return {
        "schema": "personal-vault.tv-publication-set.v1",
        "source_directory": f"tv-resolver/{batch_id}/season-{season:02d}",
        "show_title": title,
        "season_number": season,
        "members": [
            {"item_id": str(track["arrival_item_id"]), "episode_number": track["proposed_episode_number"]}
            for track in sorted(tracks, key=lambda value: value["proposed_episode_number"])
        ],
    }


def approved_projection(cursor, item):
    """Return the owner/checksum-bound TV projection, failing closed if inconsistent."""
    cursor.execute("SELECT to_regclass('vault_tv_resolver_tracks') AS relation")
    if cursor.fetchone()["relation"] is None:
        return None
    cursor.execute(
        """SELECT track.*, batch.owner_user_id, batch.proposed_show_title, batch.review_metadata
           FROM vault_tv_resolver_tracks track
           JOIN vault_tv_resolver_batches batch ON batch.id=track.batch_id
           WHERE track.arrival_item_id=%s AND (track.classification='likely_episode'
             OR (track.classification='likely_extra' AND batch.review_metadata ? 'extras_approved_at'))
             AND batch.status IN ('approved','publishing','published','failed','complete')""",
        (item.id,),
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
    title, season, episode = (
        track["proposed_show_title"],
        track["proposed_season_number"],
        track["proposed_episode_number"],
    )
    if (
        item.source_kind != "incoming"
        or track["owner_user_id"] != item.owner_user_id
        or track["checksum"] != item.sha256
        or review.get("approved_by") != str(item.owner_user_id)
        or not review.get("approved_at")
        or review.get("audience") not in {"private", "vault-wide"}
        or not title
        or not isinstance(season, int)
        or season <= 0
    ):
        raise ValueError("TV resolver approved evidence no longer agrees")
    if track["classification"] == "likely_extra":
        from app.tv_extras import extra_projection

        return extra_projection(cursor, item, track)
    if not isinstance(episode, int) or episode <= 0:
        raise ValueError("TV resolver approved episode is invalid")
    destination = (
        f"/vault/Theatre/TV Shows/{title}/Season {season:02d}/"
        f"{title} - S{season:02d}E{episode:02d}{PurePosixPath(track['original_filename']).suffix.lower()}"
    )
    if track["canonical_destination"] != destination or ".." in PurePosixPath(destination).parts:
        raise ValueError("TV resolver canonical mapping is invalid")
    cursor.execute(
        """SELECT * FROM vault_tv_resolver_tracks WHERE batch_id=%s
           AND proposed_season_number=%s AND classification='likely_episode'""",
        (track["batch_id"], season),
    )
    members = cursor.fetchall()
    numbers = [member["proposed_episode_number"] for member in members]
    if any(not isinstance(number, int) or number <= 0 for number in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("TV resolver season mapping is invalid")
    return {
        "proposed_category": "TV Shows",
        "proposed_destination": destination,
        "publication_audience": review["audience"],
        "metadata": {
            **item.metadata,
            "tv_resolver_batch_id": str(track["batch_id"]),
            "tv_publication_set": season_marker(track["batch_id"], title, season, members),
            "tv_resolver_publication": {
                "track_id": str(track["id"]),
                "item_id": str(item.id),
                "owner_user_id": str(item.owner_user_id),
                "sha256": item.sha256,
                "canonical_destination": destination,
            },
        },
    }


def validate_request_authority(item) -> None:
    if not item.metadata.get("tv_resolver_batch_id"):
        return
    evidence = item.metadata.get("tv_resolver_publication")
    if (
        not isinstance(evidence, dict)
        or item.proposed_category != "TV Shows"
        or not (item.proposed_destination or "").startswith("/vault/Theatre/TV Shows/")
        or evidence.get("canonical_destination") != item.proposed_destination
        or evidence.get("item_id") != str(item.id)
        or evidence.get("owner_user_id") != str(item.owner_user_id)
        or evidence.get("sha256") != item.sha256
    ):
        raise ValueError("TV resolver items require their approved canonical TV destination")
    UUID(str(evidence.get("track_id")))
