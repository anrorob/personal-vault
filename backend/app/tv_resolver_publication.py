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


def _source_identity(items: Iterable[ImportItem]) -> str:
    identities: set[str] = set()
    for item in items:
        context = item.metadata.get("source_context")
        if not isinstance(context, dict):
            continue
        source_id = context.get("source_id")
        label = context.get("source_label")
        value = source_id if isinstance(source_id, str) and source_id else label
        if isinstance(value, str) and value:
            identities.add(value.casefold())
    if len(identities) == 1:
        return f"supplier:{next(iter(identities))}"
    if identities:
        raise ValueError("TV resolver batch source provenance is conflicting")
    # Non-Supplier imports retain a stable Arrival Hall grouping identity.  It
    # is advisory batch evidence only, never a filesystem authority.
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
                    status TEXT NOT NULL CHECK (status IN ('proposed','needs_review','approved','publishing','published','failed','superseded')),
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

    def sync_proposal(self, owner_user_id: UUID, items: Iterable[ImportItem], proposal: TvBatchProposal) -> DurableTvResolverBatch:
        members = tuple(items)
        source_identity = _source_identity(members)
        fingerprint = _fingerprint(proposal, source_identity)
        conflicts = ["show identity is missing or conflicting"] if proposal.show_title is None else []
        status = "needs_review" if proposal.needs_review or conflicts else "proposed"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE owner_user_id=%s AND proposal_fingerprint=%s FOR UPDATE", (owner_user_id, fingerprint))
            row = cursor.fetchone()
            if row:
                return DurableTvResolverBatch(row["id"], row["owner_user_id"], row["status"], row["proposed_show_title"], row["confidence"], row["source_identity"], row["resolver_version"], row["proposal_fingerprint"])
            cursor.execute("""UPDATE vault_tv_resolver_batches SET status='superseded', updated_at=CURRENT_TIMESTAMP
                              WHERE owner_user_id=%s AND source_identity=%s
                                AND status IN ('proposed','needs_review')""", (owner_user_id, source_identity))
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

    def list_for_owner(self, owner_user_id: UUID) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE owner_user_id=%s AND status <> 'superseded' ORDER BY created_at DESC", (owner_user_id,))
            batches = [dict(row) for row in cursor.fetchall()]
            for batch in batches:
                cursor.execute("SELECT * FROM vault_tv_resolver_seasons WHERE batch_id=%s ORDER BY season_number", (batch["id"],)); batch["seasons"] = [dict(row) for row in cursor.fetchall()]
                cursor.execute("SELECT * FROM vault_tv_resolver_tracks WHERE batch_id=%s ORDER BY proposed_season_number NULLS LAST, disc_number NULLS LAST, track_number NULLS LAST, original_filename", (batch["id"],)); batch["tracks"] = [dict(row) for row in cursor.fetchall()]
            return batches

    def get_for_owner(self, batch_id: UUID, owner_user_id: UUID) -> dict[str, object] | None:
        return next((batch for batch in self.list_for_owner(owner_user_id) if batch["id"] == batch_id), None)

    def approve(self, batch_id: UUID, owner_user_id: UUID, username: str, audience: str = "vault-wide") -> dict[str, object]:
        if audience not in {"private", "vault-wide"}:
            raise ValueError("TV publication audience is invalid")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE", (batch_id, owner_user_id))
            batch = cursor.fetchone()
            if batch is None:
                raise LookupError("TV resolver batch not found")
            if batch["status"] in {"approved", "publishing", "published"}:
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
                    cursor.execute("""UPDATE vault_master_items SET state='move_queued', proposed_category='TV Shows', proposed_destination=%s, publication_audience=%s, metadata=metadata || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s""", (track["canonical_destination"], audience, Jsonb({"tv_publication_set": marker, "tv_resolver_batch_id": str(batch_id), "routing_superseded_reason": "TV batch approved by user"}), track["arrival_item_id"]))
                    cursor.execute("INSERT INTO vault_master_decisions (id,item_id,decision,username) VALUES (%s,%s,'approved',%s)", (uuid4(), track["arrival_item_id"], username))
                    cursor.execute("INSERT INTO vault_master_activity (id,batch_id,item_id,action,username,detail,succeeded) VALUES (%s,%s,%s,'tv_resolver_batch_approved',%s,%s,TRUE)", (uuid4(), items[track["arrival_item_id"]]["batch_id"], track["arrival_item_id"], username, f"TV resolver batch {batch_id} approved"))
                    cursor.execute("UPDATE vault_tv_resolver_tracks SET publication_state='queued' WHERE batch_id=%s AND arrival_item_id=%s", (batch_id, track["arrival_item_id"]))
                cursor.execute("UPDATE vault_tv_resolver_seasons SET approval_state='approved' WHERE batch_id=%s AND season_number=%s", (batch_id, season))
            cursor.execute("UPDATE vault_tv_resolver_batches SET status='publishing', review_metadata=review_metadata || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (Jsonb({"approved_at": datetime.now(UTC).isoformat(), "approved_by": str(owner_user_id), "audience": audience}), batch_id))
            cursor.execute("INSERT INTO vault_master_activity (id,action,username,detail,succeeded) VALUES (%s,'tv_resolver_publication_started',%s,%s,TRUE)", (uuid4(), username, f"TV resolver batch {batch_id} queued"))
        return {"id": batch_id, "status": "publishing"}

    def reconcile(self) -> list[UUID]:
        """Mirror durable Arrival Hall progress; return batches newly ready for one Jellyfin handoff."""
        ready: list[UUID] = []
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT DISTINCT batch_id FROM vault_tv_resolver_tracks WHERE publication_state IN ('queued','failed')""")
            for row in cursor.fetchall():
                batch_id = row["batch_id"]
                cursor.execute("""UPDATE vault_tv_resolver_tracks track SET publication_state=CASE item.state WHEN 'moved' THEN 'published' WHEN 'move_failed' THEN 'failed' ELSE track.publication_state END, failure_detail=CASE WHEN item.state='move_failed' THEN 'Arrival Hall managed publication failed' ELSE track.failure_detail END FROM vault_master_items item WHERE track.batch_id=%s AND item.id=track.arrival_item_id""", (batch_id,))
                cursor.execute("SELECT publication_state FROM vault_tv_resolver_tracks WHERE batch_id=%s AND classification=%s", (batch_id, EPISODE_CLASSIFICATION)); states = {entry["publication_state"] for entry in cursor.fetchall()}
                if states == {"published"}:
                    cursor.execute("UPDATE vault_tv_resolver_batches SET status='published', updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status <> 'published' AND jellyfin_handoff_requested_at IS NULL RETURNING id", (batch_id,))
                    if cursor.fetchone():
                        cursor.execute("UPDATE vault_tv_resolver_batches SET jellyfin_handoff_requested_at=CURRENT_TIMESTAMP WHERE id=%s", (batch_id,)); ready.append(batch_id)
                elif "failed" in states:
                    cursor.execute("UPDATE vault_tv_resolver_batches SET status='failed', updated_at=CURRENT_TIMESTAMP WHERE id=%s", (batch_id,))
        return ready

    def retry(self, batch_id: UUID, owner_user_id: UUID, username: str) -> dict[str, object]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE", (batch_id, owner_user_id)); row = cursor.fetchone()
            if row is None: raise LookupError("TV resolver batch not found")
            if row["status"] == "published": return {"id": batch_id, "status": "published"}
            if row["status"] != "failed": raise ValueError("TV resolver batch is not eligible for retry")
            cursor.execute("""UPDATE vault_master_items item SET state='move_queued', updated_at=CURRENT_TIMESTAMP FROM vault_tv_resolver_tracks track WHERE track.batch_id=%s AND track.arrival_item_id=item.id AND track.publication_state='failed' AND item.state='move_failed'""", (batch_id,))
            cursor.execute("UPDATE vault_tv_resolver_tracks SET publication_state='queued', failure_detail=NULL WHERE batch_id=%s AND publication_state='failed'", (batch_id,))
            cursor.execute("UPDATE vault_tv_resolver_batches SET status='publishing', updated_at=CURRENT_TIMESTAMP WHERE id=%s", (batch_id,))
        return {"id": batch_id, "status": "publishing"}
