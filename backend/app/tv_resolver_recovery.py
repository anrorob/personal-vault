"""Explicit, auditable recovery of an approved TV batch projection.

This module has no HTTP route, scheduler, filesystem access, or raw repair
interface.  It is for an operator who has already reviewed an exact inventory
after an older Development fixture lost only its derived item projection.
"""
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.tv_publication_authority import approved_projection


def restore_approved_batch_projection(store, batch_id, owner_user_id, expected_states):
    """Restore episode projections after an exact, owner-scoped state review.

    ``expected_states`` must enumerate every episode in the approved unfinished
    batch.  The function derives destination and authority only from durable
    batch/track approval evidence; it never updates extras or touches files.
    Repeating a successful call is idempotent.
    """
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE",
            (batch_id, owner_user_id),
        )
        batch = cursor.fetchone()
        if batch is None or batch["status"] not in {"publishing", "failed"}:
            raise ValueError("Recovery requires an approved unfinished batch")
        cursor.execute(
            """SELECT item.* FROM vault_master_items item
               JOIN vault_tv_resolver_tracks track ON track.arrival_item_id=item.id
               WHERE track.batch_id=%s AND track.classification='likely_episode'
               ORDER BY item.id FOR UPDATE OF item""",
            (batch_id,),
        )
        items = [store._to_item(row) for row in cursor.fetchall()]
        if {item.id: item.state for item in items} != expected_states:
            raise ValueError("Recovery inventory differs from reviewed episode states")
        projected = []
        for item in items:
            if item.state not in {"moved", "theatre_promotion_pending", "move_queued"}:
                raise ValueError("Recovery episode state is unsupported")
            projection = approved_projection(cursor, item)
            if projection is None or projection["metadata"]["tv_resolver_batch_id"] != str(batch_id):
                raise ValueError("Recovery approval is missing")
            metadata = projection["metadata"]
            metadata.setdefault(
                "tv_resolver_recovery_original",
                {
                    "state": item.state,
                    "category": item.proposed_category,
                    "destination": item.proposed_destination,
                },
            )
            projected.append((item, projection))
        for item, projection in projected:
            cursor.execute(
                """UPDATE vault_master_items SET proposed_category='TV Shows',
                   proposed_destination=%s, publication_audience=%s, metadata=%s,
                   updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                (
                    projection["proposed_destination"],
                    projection["publication_audience"],
                    Jsonb(projection["metadata"]),
                    item.id,
                ),
            )
            cursor.execute(
                """INSERT INTO vault_master_activity(id,batch_id,item_id,action,username,detail,succeeded)
                   SELECT %s,%s,%s,'tv_resolver_authority_restored','TV resolver recovery',%s,TRUE
                   WHERE NOT EXISTS (
                       SELECT 1 FROM vault_master_activity
                       WHERE item_id=%s AND action='tv_resolver_authority_restored'
                   )""",
                (
                    uuid4(), item.batch_id, item.id,
                    f"Restored approved mapping from resolver batch {batch_id}", item.id,
                ),
            )
