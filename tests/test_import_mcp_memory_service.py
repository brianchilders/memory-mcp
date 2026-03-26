"""
Tests for importers/mcp_memory_service.py — import_mcp_memory_service()
and POST /import/mcp-memory-service.

Uses a real temp SQLite file to simulate the source database.
"""

import sqlite3
import struct
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server as mem
from importers.mcp_memory_service import (
    import_mcp_memory_service,
    _validate_db_path,
    _discover_content_column,
    _SQLITE_MAGIC,
)

_TEST_TOKEN = "test-mcp-ms-token-abc77"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


@pytest.fixture
def src_db(tmp_path) -> Path:
    """Create a minimal mcp-memory-service-like SQLite database."""
    path = tmp_path / "memories.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, metadata TEXT)"
    )
    conn.execute("INSERT INTO memories(content, metadata) VALUES ('Prefers Python', '{}')")
    conn.execute("INSERT INTO memories(content, metadata) VALUES ('Works remotely', '{}')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def empty_src_db(tmp_path) -> Path:
    """SQLite DB with the memories table but no rows."""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    conn.commit()
    conn.close()
    return path


# ── _validate_db_path() tests (sync) ─────────────────────────────────────────

def test_validate_rejects_nonexistent_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        _validate_db_path(str(tmp_path / "nope.db"))


def test_validate_rejects_directory(tmp_path):
    with pytest.raises(ValueError, match="Not a regular file"):
        _validate_db_path(str(tmp_path))


def test_validate_rejects_non_sqlite_file(tmp_path):
    fake = tmp_path / "fake.db"
    # Write > 100 bytes of non-SQLite content so size check passes, magic check fails
    fake.write_bytes(b"This is not a SQLite file at all. " + b"x" * 100)
    with pytest.raises(ValueError, match="not appear to be a SQLite"):
        _validate_db_path(str(fake))


def test_validate_accepts_valid_sqlite(src_db):
    # Should not raise
    path = _validate_db_path(str(src_db))
    assert path == src_db


def test_validate_rejects_tiny_file(tmp_path):
    tiny = tmp_path / "tiny.db"
    tiny.write_bytes(b"x" * 10)
    with pytest.raises(ValueError, match="too small"):
        _validate_db_path(str(tiny))


# ── _discover_content_column() tests ─────────────────────────────────────────

def test_discover_content_column_finds_content(src_db):
    conn   = sqlite3.connect(str(src_db))
    result = _discover_content_column(conn.cursor(), "memories")
    conn.close()
    assert result == "content"


def test_discover_content_column_finds_memory_column(tmp_path):
    path = tmp_path / "alt.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE memories (id INTEGER, memory TEXT)")
    conn.commit()
    result = _discover_content_column(conn.cursor(), "memories")
    conn.close()
    assert result == "memory"


def test_discover_content_column_returns_none_for_unknown_schema(tmp_path):
    path = tmp_path / "unknown.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE data (id INTEGER, blob_col BLOB)")
    conn.commit()
    result = _discover_content_column(conn.cursor(), "data")
    conn.close()
    assert result is None


# ── import_mcp_memory_service() tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_basic_import(src_db):
    result = await import_mcp_memory_service(
        db_path=str(src_db), entity_name="imported"
    )
    assert result.added   == 2
    assert result.skipped == 0
    assert result.errors  == []


@pytest.mark.asyncio
async def test_memories_stored_in_db(src_db):
    await import_mcp_memory_service(db_path=str(src_db), entity_name="imported")
    db  = mem.get_db()
    e   = db.execute("SELECT id FROM entities WHERE name='imported'").fetchone()
    mems = db.execute(
        "SELECT fact FROM memories WHERE entity_id=?", (e["id"],)
    ).fetchall()
    db.close()
    facts = {m["fact"] for m in mems}
    assert "Prefers Python" in facts
    assert "Works remotely" in facts


@pytest.mark.asyncio
async def test_entity_created_with_correct_type(src_db):
    await import_mcp_memory_service(
        db_path=str(src_db), entity_name="mcp_user", entity_type="device"
    )
    db  = mem.get_db()
    row = db.execute("SELECT type FROM entities WHERE name='mcp_user'").fetchone()
    db.close()
    assert row["type"] == "device"


@pytest.mark.asyncio
async def test_deduplication_on_reimport(src_db):
    await import_mcp_memory_service(db_path=str(src_db), entity_name="imported")
    result = await import_mcp_memory_service(db_path=str(src_db), entity_name="imported")
    assert result.added   == 0
    assert result.skipped == 2


@pytest.mark.asyncio
async def test_empty_db_returns_zero(empty_src_db):
    result = await import_mcp_memory_service(
        db_path=str(empty_src_db), entity_name="imported"
    )
    assert result.added   == 0
    assert result.skipped == 0
    assert result.errors  == []


@pytest.mark.asyncio
async def test_nonexistent_path_raises_value_error():
    with pytest.raises(ValueError, match="not found"):
        await import_mcp_memory_service("/tmp/no_such_file.db")


@pytest.mark.asyncio
async def test_invalid_entity_name_raises_value_error(src_db):
    with pytest.raises(ValueError, match="entity_name"):
        await import_mcp_memory_service(db_path=str(src_db), entity_name="")


@pytest.mark.asyncio
async def test_source_tag_stored(src_db):
    await import_mcp_memory_service(db_path=str(src_db), entity_name="imported")
    db  = mem.get_db()
    row = db.execute(
        "SELECT source FROM memories WHERE fact='Prefers Python'"
    ).fetchone()
    db.close()
    assert row["source"] == "import:mcp-memory-service"


@pytest.mark.asyncio
async def test_no_table_raises_runtime_error(tmp_path):
    path = tmp_path / "no_table.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="No recognised memory table"):
        await import_mcp_memory_service(db_path=str(path))


# ── POST /import/mcp-memory-service HTTP tests ────────────────────────────────

def test_http_rejects_nonexistent_path(client):
    r = client.post("/import/mcp-memory-service", json={
        "db_path": "/tmp/does_not_exist_xyz.db",
        "entity_name": "imported",
    })
    assert r.status_code == 400
    assert "not found" in r.json()["detail"].lower()


def test_http_import_requires_auth(api_auth):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.post("/import/mcp-memory-service", json={
            "db_path": "/tmp/test.db",
            "entity_name": "imported",
        })
        assert r.status_code == 401


def test_http_import_valid_db(client, src_db):
    r = client.post("/import/mcp-memory-service", json={
        "db_path": str(src_db),
        "entity_name": "http_imported",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]    is True
    assert body["added"] == 2
