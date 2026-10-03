"""Real PostgreSQL navigation proof against the disposable test database."""
from datetime import date
import time
from uuid import UUID

import psycopg
import pytest

from app.auth_store import PostgresAuthenticationStore
from tests.test_postgres_vault_master import postgres_conninfo, postgres_store  # noqa: F401


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_postgres_chronology_and_bidirectional_seek_at_scale(postgres_store, postgres_conninfo, sort):
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    other = PostgresAuthenticationStore(postgres_conninfo).get_account("son")
    assert owner and other
    vault_id = postgres_store.get_local_vault_id()
    assets, files = [], []
    for i in range(3000):
        asset_id = UUID(int=i + 1)
        name = f"Synthetic-Example-{i:04d}.jpg"
        captured = date(2018 + i % 8, i % 12 + 1, 1) if i < 2998 else None
        assets.append((asset_id, captured, owner.user_id if i < 2999 else other.user_id, vault_id))
        files.append((UUID(int=10000 + i), asset_id, "/vault/Gallery/" + name, name))
    with psycopg.connect(postgres_conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO vault_assets(id,asset_type,display_title,owner_username,metadata_provenance,captured_on,owner_user_id,origin_vault_id) VALUES (%s,'Gallery','Synthetic Example','synthetic',jsonb_build_object('captured_on','embedded'),%s,%s,%s)", assets)
            cursor.executemany("INSERT INTO vault_files(id,asset_id,vault_path,filename,size_bytes,mime_type,sha256) VALUES (%s,%s,%s,%s,8,'image/jpeg','synthetic')", files)
    scope = (owner.user_id, [], "active", sort, None, None, None)
    started = time.perf_counter()
    periods = postgres_store.gallery_chronology(*scope)
    elapsed = time.perf_counter() - started
    print(f"Synthetic 3000-row chronology ({sort}): {elapsed * 1000:.1f} ms, {len(periods)} summary rows")
    assert elapsed < 2  # broad ceiling, includes SSH/connection setup in local proof
    assert sum(period[3] for period in periods) == 2999
    assert periods[0][0].year == (2025 if sort == "newest" else 2018)
    assert periods[-1][0] is None
    all_paths = postgres_store.gallery_page_paths(*scope, None, 3000)
    for index in (0, 75, 2400, 2998):
        asset = postgres_store.get_catalogued_asset(all_paths[index])
        position = (asset.captured_on, asset.filename.casefold(), asset.vault_path)
        forward = postgres_store.gallery_page_paths(*scope, position, 61, inclusive=True)
        backward = postgres_store.gallery_page_paths(*scope, position, 61, reverse=True)
        assert forward == all_paths[index:index + 61]
        assert backward == list(reversed(all_paths[max(0, index - 61):index]))
    for captured, name, path, count in periods:
        assert count > 0
        assert postgres_store.gallery_page_paths(*scope, (captured, name, path), 1, inclusive=True) == [path]
    matching = [UUID(int=1), UUID(int=2)]
    assert sum(period[3] for period in postgres_store.gallery_chronology(*scope[:-1], matching)) == 2
    assert postgres_store.gallery_chronology(*scope[:-1], []) == []
    hidden_id = UUID(int=1)
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("UPDATE vault_assets SET lifecycle_state='hidden' WHERE id=%s", (hidden_id,))
    assert sum(p[3] for p in postgres_store.gallery_chronology(*scope)) == 2998
    assert sum(p[3] for p in postgres_store.gallery_chronology(owner.user_id, [], "hidden", sort, None, None, None)) == 1
    assert sum(p[3] for p in postgres_store.gallery_chronology(owner.user_id, [UUID(int=3000)], "active", sort, None, None, None)) == 2999
