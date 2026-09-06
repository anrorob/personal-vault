"""Durable, owner-scoped review state for TV disc resolver proposals.

This module deliberately records review evidence and drives the existing
Arrival Hall managed-publication state machine.  It has no filesystem access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import PurePosixPath
from typing import Iterable
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.tv_disc_resolver import RESOLVER_VERSION, TvBatchProposal
from app.vault_master import INCOMING_SOURCE, ImportItem


EPISODE_CLASSIFICATION = "likely_episode"
ACTIVE_STATES = {"proposed", "needs_review", "approved", "publishing", "failed"}


@dataclass(frozen=True)
class DurableTvResolverBatch:
    id: UUID
    owner_user_id: UUID
    status: str
    show_title: str | None
    confidence: str
    source_identity: str
    resolver_version: str
    proposal_fingerprint: str


def _source_identity(items: Iterable[ImportItem], show_title: str | None) -> str:
    if show_title:
        return f"show:{' '.join(show_title.casefold().split())}"
    # Supplier provenance remains per-track evidence, not batch identity.
    parents = {
        PurePosixPath(item.relative_path.replace("\\", "/")).parent.as_posix()
        for item in items
    }
    return "arrival:" + "|".join(sorted(parents))


def _fingerprint(proposal: TvBatchProposal, source_identity: str) -> str:
    evidence = {
        "resolver": RESOLVER_VERSION,
        "source": source_identity,
        "show": proposal.show_title,
        "tracks": [
            {
                "item_id": str(track.item_id), "season": track.season_number,
                "episode": track.episode_number, "classification": track.classification,
                "destination": track.destination,
            }
            for track in proposal.tracks
        ],
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PostgresTvResolverStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self.conninfo, row_factory=psycopg.rows.dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_resolver_batches (
                    id UUID PRIMARY KEY,
                    owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                    resolver_version TEXT NOT NULL,
                    source_identity TEXT NOT NULL,
                    proposed_show_title TEXT,
                    status TEXT NOT NULL CHECK (status IN ('proposed','needs_review','approved','publishing','published','failed','complete','superseded')),
                    confidence TEXT NOT NULL,
                    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
                    conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
                    review_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    proposal_fingerprint TEXT NOT NULL,
                    jellyfin_handoff_requested_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, proposal_fingerprint)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_resolver_seasons (
                    id UUID PRIMARY KEY,
                    batch_id UUID NOT NULL REFERENCES vault_tv_resolver_batches(id) ON DELETE RESTRICT,
                    season_number INTEGER NOT NULL CHECK (season_number > 0),
                    episode_candidate_count INTEGER NOT NULL DEFAULT 0,
                    extra_count INTEGER NOT NULL DEFAULT 0,
                    unresolved_count INTEGER NOT NULL DEFAULT 0,
                    confidence TEXT NOT NULL,
                    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
                    approval_state TEXT NOT NULL DEFAULT 'proposed',
                    UNIQUE(batch_id, season_number)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_resolver_tracks (
                    id UUID PRIMARY KEY,
                    batch_id UUID NOT NULL REFERENCES vault_tv_resolver_batches(id) ON DELETE RESTRICT,
                    arrival_item_id UUID NOT NULL REFERENCES vault_master_items(id) ON DELETE RESTRICT,
                    source_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                    original_filename TEXT NOT NULL,
                    runtime_seconds DOUBLE PRECISION,
                    checksum TEXT NOT NULL,
                    disc_number INTEGER,
                    track_number INTEGER,
                    classification TEXT NOT NULL,
                    proposed_season_number INTEGER,
                    proposed_episode_number INTEGER,
                    canonical_destination TEXT,
                    confidence TEXT NOT NULL,
                    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
                    publication_state TEXT NOT NULL DEFAULT 'proposed',
                    failure_detail TEXT,
                    UNIQUE(batch_id, arrival_item_id)
                )
            """)

            cursor.execute("ALTER TABLE vault_tv_resolver_batches DROP CONSTRAINT IF EXISTS vault_tv_resolver_batches_status_check")
            cursor.execute("ALTER TABLE vault_tv_resolver_batches ADD CONSTRAINT vault_tv_resolver_batches_status_check CHECK (status IN ('proposed','needs_review','approved','publishing','published','failed','superseded','complete'))")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_tv_resolver_tracks_arrival_item_idx ON vault_tv_resolver_tracks (arrival_item_id, batch_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_tv_resolver_batches_owner_status_idx ON vault_tv_resolver_batches (owner_user_id, status)")

    def sync_proposal(self, owner_user_id: UUID, items: Iterable[ImportItem], proposal: TvBatchProposal) -> DurableTvResolverBatch:
        members = tuple(items)
        source_identity = _source_identity(members, proposal.show_title)
        fingerprint = _fingerprint(proposal, source_identity)
        conflicts = ["show identity is missing or conflicting"] if proposal.show_title is None else []
        status = "needs_review" if proposal.needs_review or conflicts else "proposed"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE owner_user_id=%s AND proposal_fingerprint=%s FOR UPDATE", (owner_user_id, fingerprint))
            row = cursor.fetchone()
            if row:
                return DurableTvResolverBatch(row["id"], row["owner_user_id"], row["status"], row["proposed_show_title"], row["confidence"], row["source_identity"], row["resolver_version"], row["proposal_fingerprint"])
            member_ids = [item.id for item in members]
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE id IN (SELECT track.batch_id FROM vault_tv_resolver_tracks track JOIN vault_tv_resolver_batches batch ON batch.id=track.batch_id WHERE batch.owner_user_id=%s AND track.arrival_item_id=ANY(%s) AND batch.status IN ('proposed','needs_review','approved','publishing','published','failed','complete')) FOR UPDATE", (owner_user_id, member_ids))
            supersede: list[UUID] = []
            for existing in [dict(entry) for entry in cursor.fetchall()]:
                cursor.execute("SELECT arrival_item_id FROM vault_tv_resolver_tracks WHERE batch_id=%s", (existing["id"],))
                old_members = {entry["arrival_item_id"] for entry in cursor.fetchall()}
                if existing["status"] in {"approved", "publishing", "published", "failed", "complete"}:
                    raise ValueError("TV resolver proposal overlaps a protected publication batch; review is required")
                if set(member_ids) < old_members:
                    return DurableTvResolverBatch(existing["id"], existing["owner_user_id"], existing["status"], existing["proposed_show_title"], existing["confidence"], existing["source_identity"], existing["resolver_version"], existing["proposal_fingerprint"])
                if old_members <= set(member_ids):
                    supersede.append(existing["id"])
                else:
                    raise ValueError("TV resolver proposal partially overlaps an active review batch; review is required")
            if supersede:
                cursor.execute("UPDATE vault_tv_resolver_batches SET status='superseded', updated_at=CURRENT_TIMESTAMP WHERE id=ANY(%s)", (supersede,))
            batch_id = uuid4()
            cursor.execute("""INSERT INTO vault_tv_resolver_batches
                (id,owner_user_id,resolver_version,source_identity,proposed_show_title,status,confidence,evidence,conflicts,proposal_fingerprint)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (batch_id, owner_user_id, RESOLVER_VERSION, source_identity, proposal.show_title, status, proposal.confidence, Jsonb(list(proposal.evidence)), Jsonb(conflicts), fingerprint))
            for season in sorted({track.season_number for track in proposal.tracks if track.season_number}):
                tracks = [track for track in proposal.tracks if track.season_number == season]
                cursor.execute("""INSERT INTO vault_tv_resolver_seasons (id,batch_id,season_number,episode_candidate_count,extra_count,unresolved_count,confidence,evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (uuid4(), batch_id, season, sum(t.classification == EPISODE_CLASSIFICATION for t in tracks), sum(t.classification == "likely_extra" for t in tracks), sum(t.classification == "unresolved" for t in tracks), "high" if tracks and all(t.confidence == "high" or t.classification == "likely_extra" for t in tracks) else "low", Jsonb([])))
            by_id = {item.id: item for item in members}
            for track in proposal.tracks:
                item = by_id[track.item_id]
                cursor.execute("""INSERT INTO vault_tv_resolver_tracks
                    (id,batch_id,arrival_item_id,source_provenance,original_filename,runtime_seconds,checksum,disc_number,track_number,classification,proposed_season_number,proposed_episode_number,canonical_destination,confidence,evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (uuid4(), batch_id, item.id, Jsonb(dict(item.metadata.get("source_context") or {})), item.filename, track.duration_seconds, item.sha256, track.disc_number, track.track_number, track.classification, track.season_number, track.episode_number, track.destination, track.confidence, Jsonb(list(track.evidence))))
        return DurableTvResolverBatch(batch_id, owner_user_id, status, proposal.show_title, proposal.confidence, source_identity, RESOLVER_VERSION, fingerprint)

    def list_for_owner(self, owner_user_id: UUID, *, include_complete: bool = False) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE owner_user_id=%s AND status <> 'superseded' AND (%s OR status <> 'complete') ORDER BY created_at DESC", (owner_user_id, include_complete))
            batches = [dict(row) for row in cursor.fetchall()]
            for batch in batches:
                cursor.execute("SELECT * FROM vault_tv_resolver_seasons WHERE batch_id=%s ORDER BY season_number", (batch["id"],)); batch["seasons"] = [dict(row) for row in cursor.fetchall()]
                cursor.execute("SELECT * FROM vault_tv_resolver_tracks WHERE batch_id=%s ORDER BY proposed_season_number NULLS LAST, disc_number NULLS LAST, track_number NULLS LAST, original_filename", (batch["id"],)); batch["tracks"] = [dict(row) for row in cursor.fetchall()]
            return batches

    def get_for_owner(self, batch_id: UUID, owner_user_id: UUID) -> dict[str, object] | None:
        return next((batch for batch in self.list_for_owner(owner_user_id, include_complete=True) if batch["id"] == batch_id), None)

    def approve(self, batch_id: UUID, owner_user_id: UUID, username: str, audience: str = "vault-wide") -> dict[str, object]:
        if audience not in {"private", "vault-wide"}:
            raise ValueError("TV publication audience is invalid")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE", (batch_id, owner_user_id))
            batch = cursor.fetchone()
            if batch is None:
                raise LookupError("TV resolver batch not found")
            if batch["status"] in {"approved", "publishing", "published", "complete"}:
                return {"id": batch_id, "status": batch["status"]}
            if batch["status"] not in {"proposed", "needs_review", "failed"} or not batch["proposed_show_title"]:
                raise ValueError("TV resolver batch is not eligible for approval")
            cursor.execute("SELECT * FROM vault_tv_resolver_tracks WHERE batch_id=%s FOR UPDATE", (batch_id,))
            tracks = [dict(row) for row in cursor.fetchall()]
            episodes = [track for track in tracks if track["classification"] == EPISODE_CLASSIFICATION]
            if not episodes or any(track["proposed_season_number"] is None or track["proposed_episode_number"] is None or not track["canonical_destination"] for track in episodes):
                raise ValueError("TV resolver batch has no complete episode mapping")
            if len({(track["proposed_season_number"], track["proposed_episode_number"]) for track in episodes}) != len(episodes):
                raise ValueError("TV resolver batch has duplicate SxxExx assignments")
            item_ids = [track["arrival_item_id"] for track in episodes]
            cursor.execute("SELECT * FROM vault_master_items WHERE id=ANY(%s) FOR UPDATE", (item_ids,))
            items = {row["id"]: row for row in cursor.fetchall()}
            if len(items) != len(item_ids):
                raise ValueError("TV resolver staged item is unavailable")
            for track in episodes:
                item = items[track["arrival_item_id"]]
                if item["source_kind"] != INCOMING_SOURCE or item["owner_user_id"] != owner_user_id or item["state"] not in {"needs_review", "approved", "move_failed"} or item["sha256"] != track["checksum"]:
                    raise ValueError("TV resolver staged evidence is no longer eligible")
                cursor.execute("SELECT sha256,size_bytes FROM vault_files WHERE vault_path=%s FOR UPDATE", (track["canonical_destination"],))
                collision = cursor.fetchone()
                if collision is not None:
                    if collision["sha256"] == track["checksum"]:
                        raise ValueError("TV resolver destination is already published; review exact duplicate")
                    raise ValueError("TV resolver destination collision requires review")
            per_season: dict[int, list[dict[str, object]]] = {}
            for track in episodes: per_season.setdefault(int(track["proposed_season_number"]), []).append(track)
            for season, members in per_season.items():
                marker = {"schema": "personal-vault.tv-publication-set.v1", "source_directory": f"tv-resolver/{batch_id}/season-{season:02d}", "show_title": batch["proposed_show_title"], "season_number": season, "members": [{"item_id": str(track["arrival_item_id"]), "episode_number": int(track["proposed_episode_number"])} for track in sorted(members, key=lambda row: int(row["proposed_episode_number"]))]}
                for track in members:
                    original = items[track["arrival_item_id"]]
                    authority = {
                        "track_id": str(track["id"]), "item_id": str(track["arrival_item_id"]),
                        "owner_user_id": str(owner_user_id), "sha256": track["checksum"],
                        "canonical_destination": track["canonical_destination"],
                    }
                    cursor.execute("UPDATE vault_master_items SET metadata=metadata || %s WHERE id=%s", (
                        Jsonb({"tv_resolver_publication": authority, "tv_resolver_original_proposal": {
                            "category": original["proposed_category"], "destination": original["proposed_destination"],
                            "reason": original["proposal_reason"],
                        }}), track["arrival_item_id"],
                    ))
                    cursor.execute("""UPDATE vault_master_items SET state='move_queued', proposed_category='TV Shows', proposed_destination=%s, publication_audience=%s, metadata=metadata || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s""", (track["canonical_destination"], audience, Jsonb({"tv_publication_set": marker, "tv_resolver_batch_id": str(batch_id), "routing_superseded_reason": "TV batch approved by user"}), track["arrival_item_id"]))
                    cursor.execute("INSERT INTO vault_master_decisions (id,item_id,decision,username) VALUES (%s,%s,'approved',%s)", (uuid4(), track["arrival_item_id"], username))
                    cursor.execute("INSERT INTO vault_master_activity (id,batch_id,item_id,action,username,detail,succeeded) VALUES (%s,%s,%s,'tv_resolver_batch_approved',%s,%s,TRUE)", (uuid4(), items[track["arrival_item_id"]]["batch_id"], track["arrival_item_id"], username, f"TV resolver batch {batch_id} approved"))
                    cursor.execute("UPDATE vault_tv_resolver_tracks SET publication_state='queued' WHERE batch_id=%s AND arrival_item_id=%s", (batch_id, track["arrival_item_id"]))
                cursor.execute("UPDATE vault_tv_resolver_seasons SET approval_state='approved' WHERE batch_id=%s AND season_number=%s", (batch_id, season))
            cursor.execute("UPDATE vault_tv_resolver_batches SET status='publishing', review_metadata=review_metadata || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (Jsonb({"approved_at": datetime.now(UTC).isoformat(), "approved_by": str(owner_user_id), "audience": audience}), batch_id))
            cursor.execute("INSERT INTO vault_master_activity (id,action,username,detail,succeeded) VALUES (%s,'tv_resolver_publication_started',%s,%s,TRUE)", (uuid4(), username, f"TV resolver batch {batch_id} queued"))
        return {"id": batch_id, "status": "publishing"}

    def publish_extras(self, batch_id: UUID, owner_user_id: UUID, username: str) -> dict[str, object]:
        from app.tv_extras import extra_destination
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE", (batch_id, owner_user_id))
            batch = cursor.fetchone()
            if batch is None:
                raise LookupError("TV resolver batch not found")
            if batch['review_metadata'].get('extras_approved_at') or batch['status'] == 'complete':
                return {'id': batch_id, 'status': batch['status']}
            cursor.execute("SELECT * FROM vault_tv_resolver_tracks WHERE batch_id=%s FOR UPDATE", (batch_id,))
            tracks = cursor.fetchall()
            episodes = [t for t in tracks if t['classification'] == 'likely_episode']
            if batch['status'] != 'published' or not episodes or any(t['publication_state'] != 'published' for t in episodes):
                raise ValueError('Publish episodes before publishing extras')
            extras = [t for t in tracks if t['classification'] == 'likely_extra']
            if not extras:
                raise ValueError('No classified extras remain')
            destinations = [extra_destination(batch['proposed_show_title'], t['proposed_season_number'], t['original_filename']) for t in extras]
            if len(set(destinations)) != len(destinations):
                raise ValueError('TV extra destination collision requires review')
            audience = batch['review_metadata'].get('audience')
            if audience not in {'private', 'vault-wide'}:
                raise ValueError('TV batch approved audience is unavailable')
            for track, destination in zip(extras, destinations):
                cursor.execute("SELECT * FROM vault_master_items WHERE id=%s FOR UPDATE", (track['arrival_item_id'],))
                item = cursor.fetchone()
                if (item is None or item['owner_user_id'] != owner_user_id or item['source_kind'] != INCOMING_SOURCE
                    or item['state'] != 'needs_review' or item['sha256'] != track['checksum']
                    or track['proposed_episode_number'] is not None):
                    raise ValueError('TV extra staged evidence is no longer eligible')
                cursor.execute("SELECT 1 FROM vault_tv_seasons s JOIN vault_tv_shows show ON show.id=s.show_id WHERE show.title=%s AND show.owner_user_id=%s AND show.visibility=%s AND s.season_number=%s", (batch['proposed_show_title'], owner_user_id, audience, track['proposed_season_number']))
                if cursor.fetchone() is None:
                    raise ValueError('TV extra published season is unavailable')
                cursor.execute("SELECT 1 FROM vault_files WHERE vault_path=%s", (destination,))
                if cursor.fetchone() is not None:
                    raise ValueError('TV extra destination collision requires review')
                cursor.execute("UPDATE vault_tv_resolver_tracks SET canonical_destination=%s, publication_state='queued', failure_detail=NULL WHERE id=%s", (destination, track['id']))
                cursor.execute("UPDATE vault_master_items SET state='move_queued', proposed_category='TV Shows', proposed_destination=%s, publication_audience=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (destination, audience, item['id']))
                cursor.execute("INSERT INTO vault_master_decisions(id,item_id,decision,username) VALUES (%s,%s,'approved',%s)", (uuid4(), item['id'], username))
            cursor.execute("UPDATE vault_tv_resolver_batches SET status='publishing', review_metadata=review_metadata || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (Jsonb({'extras_approved_at': datetime.now(UTC).isoformat(), 'extras_approved_by': str(owner_user_id)}), batch_id))
            cursor.execute("INSERT INTO vault_master_activity(id,action,username,detail,succeeded) VALUES (%s,'tv_resolver_extras_approved',%s,%s,TRUE)", (uuid4(), username, f'TV resolver extras approved for batch {batch_id}'))
        return {'id': batch_id, 'status': 'publishing'}

    def reconcile(self) -> list[UUID]:
        """Mirror durable Arrival Hall progress; return batches newly ready for one Jellyfin handoff."""
        ready: list[UUID] = []
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('vault_tv_episodes') AS relation")
            if cursor.fetchone()["relation"] is None:
                return ready
            cursor.execute("""SELECT id AS batch_id FROM vault_tv_resolver_batches WHERE status IN ('publishing','failed','published') FOR UPDATE SKIP LOCKED""")
            for row in cursor.fetchall():
                batch_id = row["batch_id"]
                cursor.execute("""UPDATE vault_tv_resolver_tracks track SET
                    publication_state=CASE WHEN item.state='moved' AND EXISTS (
                        SELECT 1 FROM vault_arrival_managed_publications publication
                        JOIN vault_files file ON file.id=publication.file_id AND file.asset_id=publication.asset_id
                        JOIN vault_assets asset ON asset.id=file.asset_id
                        JOIN vault_file_storage_placements placement ON placement.file_id=file.id
                        LEFT JOIN vault_tv_episodes episode ON episode.asset_id=asset.id
                        LEFT JOIN vault_tv_extras extra ON extra.asset_id=asset.id
                        JOIN vault_tv_seasons season ON season.id=COALESCE(episode.season_id,extra.season_id)
                        JOIN vault_tv_shows show ON show.id=season.show_id
                        JOIN vault_tv_resolver_batches batch ON batch.id=track.batch_id
                        WHERE publication.item_id=item.id AND publication.owner_user_id=batch.owner_user_id
                          AND asset.owner_user_id=batch.owner_user_id AND show.owner_user_id=batch.owner_user_id
                          AND asset.lifecycle_state='active' AND asset.asset_type='TV Shows'
                          AND file.vault_path=track.canonical_destination AND file.sha256=track.checksum
                          AND file.size_bytes=item.size_bytes AND publication.logical_destination=file.vault_path
                          AND publication.logical_area='Theatre / TV Shows'
                          AND placement.slot_id=publication.slot_id AND placement.relative_path=publication.relative_path
                          AND show.title=batch.proposed_show_title
                          AND season.season_number=track.proposed_season_number
                          AND ((track.classification='likely_episode' AND episode.episode_number=track.proposed_episode_number)
                            OR (track.classification='likely_extra' AND extra.asset_id IS NOT NULL AND episode.id IS NULL))
                    ) THEN 'published' WHEN item.state='move_failed' THEN 'failed' ELSE 'queued' END,
                    failure_detail=CASE WHEN item.state='move_failed' THEN 'Arrival Hall managed publication failed' ELSE NULL END
                    FROM vault_master_items item WHERE track.batch_id=%s AND item.id=track.arrival_item_id
                    AND (track.classification='likely_episode' OR (track.classification='likely_extra' AND track.publication_state IN ('queued','failed','published')))""", (batch_id,))
                cursor.execute("SELECT classification,publication_state FROM vault_tv_resolver_tracks WHERE batch_id=%s", (batch_id,))
                tracks = cursor.fetchall()
                episode_states = {t['publication_state'] for t in tracks if t['classification'] == EPISODE_CLASSIFICATION}
                if episode_states == {'published'}:
                    cursor.execute("UPDATE vault_tv_resolver_batches SET jellyfin_handoff_requested_at=CURRENT_TIMESTAMP WHERE id=%s AND jellyfin_handoff_requested_at IS NULL RETURNING id", (batch_id,))
                    if cursor.fetchone():
                        ready.append(batch_id)
                states = {t['publication_state'] for t in tracks}
                if states == {'published'} and all(t['classification'] in {'likely_episode','likely_extra'} for t in tracks):
                    next_status = 'complete'
                    if any(t['classification'] == 'likely_extra' for t in tracks):
                        cursor.execute("UPDATE vault_tv_resolver_batches SET review_metadata=review_metadata || %s WHERE id=%s AND NOT (review_metadata ? 'extras_handoff_requested_at') RETURNING id", (Jsonb({'extras_handoff_requested_at': datetime.now(UTC).isoformat()}), batch_id))
                        if cursor.fetchone():
                            ready.append(batch_id)
                elif 'failed' in states:
                    next_status = 'failed'
                elif episode_states == {'published'} and not any(t['classification']=='likely_extra' and t['publication_state']=='queued' for t in tracks):
                    next_status = 'published'
                else:
                    next_status = 'publishing'
                cursor.execute("UPDATE vault_tv_resolver_batches SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status<>%s", (next_status, batch_id, next_status))
        return ready

    def retry(self, batch_id: UUID, owner_user_id: UUID, username: str) -> dict[str, object]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE", (batch_id, owner_user_id)); row = cursor.fetchone()
            if row is None: raise LookupError("TV resolver batch not found")
            if row["status"] in {"published", "complete", "publishing"}: return {"id": batch_id, "status": row["status"]}
            if row["status"] != "failed": raise ValueError("TV resolver batch is not eligible for retry")
            cursor.execute("""UPDATE vault_master_items item SET state='move_queued', updated_at=CURRENT_TIMESTAMP FROM vault_tv_resolver_tracks track WHERE track.batch_id=%s AND track.arrival_item_id=item.id AND track.publication_state='failed' AND item.state='move_failed'""", (batch_id,))
            cursor.execute("UPDATE vault_tv_resolver_tracks SET publication_state='queued', failure_detail=NULL WHERE batch_id=%s AND publication_state='failed'", (batch_id,))
            cursor.execute("UPDATE vault_tv_resolver_batches SET status='publishing', updated_at=CURRENT_TIMESTAMP WHERE id=%s", (batch_id,))
        return {"id": batch_id, "status": "publishing"}
