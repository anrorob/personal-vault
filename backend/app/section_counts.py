"""Small, authorized catalogue totals; presentation filters never enter this query."""
from fastapi import APIRouter, Depends, Response
import psycopg
from psycopg.rows import dict_row

from app.auth import AuthenticatedUsername
from app.config import get_database_conninfo
from app.movies import MOVIE_COMPANION_DIRECTORY_NAMES
from app.share_grants import ASSET_ACCESS_PREDICATE, active_user_id, evaluate_due_share_operations
from app.vault_libraries import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS

router = APIRouter(prefix="/api/section-counts", tags=["sections"])


@router.get("")
def section_counts(response: Response, username: AuthenticatedUsername,
                   conninfo: str = Depends(get_database_conninfo)) -> dict[str, int]:
    response.headers["Cache-Control"] = "private, no-store"
    counts = dict(gallery=0, music=0, home_videos=0, movies=0)
    with psycopg.connect(conninfo, row_factory=dict_row) as connection, connection.cursor() as cursor:
        user_id = active_user_id(cursor, username)
        if user_id is None:
            return counts
        evaluate_due_share_operations(cursor)
        # One row per canonical asset, irrespective of auxiliary files or grants.
        # Owned hidden Gallery photos belong to the overall total, but hidden
        # assets never contribute through a recipient's grant.
        cursor.execute(f"""
            WITH eligible AS (
                SELECT asset.id, lower(asset.asset_type) AS kind, file.vault_path,
                       lower(substring(file.filename from '\\.[^.]+$')) AS extension
                FROM vault_assets asset
                JOIN LATERAL (
                    SELECT vault_path, filename FROM vault_files WHERE asset_id=asset.id
                    ORDER BY (file_role='primary') DESC, created_at LIMIT 1
                ) file ON TRUE
                WHERE {ASSET_ACCESS_PREDICATE}
                  AND (asset.lifecycle_state='active'
                       OR (asset.owner_user_id=%s AND lower(asset.asset_type)='gallery'))
            )
            SELECT
                count(*) FILTER (WHERE kind='gallery' AND starts_with(vault_path, '/vault/Gallery/')) AS gallery,
                count(*) FILTER (WHERE starts_with(vault_path, '/vault/Music/')
                    AND extension=ANY(%s)) AS music,
                count(*) FILTER (WHERE starts_with(vault_path, '/vault/Home Videos/')
                    AND extension=ANY(%s)) AS home_videos,
                count(*) FILTER (WHERE kind IN ('movie','movies')
                    AND starts_with(vault_path, '/vault/Theatre/Movies/')
                    AND NOT (string_to_array(lower(regexp_replace(vault_path, '/[^/]+$', '')), '/') && %s)) AS movies
            FROM eligible
        """, (user_id, user_id, user_id, user_id,
              sorted(AUDIO_EXTENSIONS), sorted(VIDEO_EXTENSIONS), sorted(MOVIE_COMPANION_DIRECTORY_NAMES)))
        return dict(cursor.fetchone())
