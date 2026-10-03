"""Private per-user asset preferences; never ownership or visibility authority."""
from uuid import UUID
import psycopg
from app.config import get_database_conninfo


class MemoryAssetFavorites:
    def __init__(self):
        self.entries: set[tuple[UUID, UUID]] = set()
    def initialize(self): pass
    def list(self, user_id):
        return {asset for user, asset in self.entries if user == user_id}
    def set(self, user_id, asset_id, favorite):
        if favorite: self.entries.add((user_id, asset_id))
        else: self.entries.discard((user_id, asset_id))


class PostgresAssetFavorites:
    def __init__(self, conninfo): self.conninfo = conninfo
    def initialize(self):
        with psycopg.connect(self.conninfo) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS user_asset_favorites (
                user_id UUID NOT NULL REFERENCES auth_accounts(user_id) ON DELETE CASCADE,
                asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, asset_id))""")
    def list(self, user_id):
        with psycopg.connect(self.conninfo) as connection:
            return {row[0] for row in connection.execute(
                "SELECT asset_id FROM user_asset_favorites WHERE user_id=%s", (user_id,))}
    def set(self, user_id, asset_id, favorite):
        with psycopg.connect(self.conninfo) as connection:
            if favorite:
                connection.execute("INSERT INTO user_asset_favorites(user_id,asset_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (user_id, asset_id))
            else:
                connection.execute("DELETE FROM user_asset_favorites WHERE user_id=%s AND asset_id=%s", (user_id, asset_id))


def get_asset_favorites():
    return PostgresAssetFavorites(get_database_conninfo())
