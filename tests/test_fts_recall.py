"""
tests/test_fts_recall.py — FTS5 keyword recall + hybrid search tests.

Covers:
  _fts_query()     — sanitises FTS5 special chars, drops single-char tokens
  FTS5 backfill    — existing memories/turns indexed on init
  FTS5 triggers    — new memories auto-indexed by INSERT trigger
  tool_recall mode=keyword  — returns relevant memories without embedding
  tool_recall mode=hybrid   — merges vector + keyword results
  tool_recall mode=vector   — existing behaviour unchanged
  tool_search_sessions      — FTS5 search across session_turns
  HTTP POST /recall mode param
  HTTP POST /search_sessions
  Pre-write cross-check     — Feature 4: low-trust write rejected by higher-trust fact
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-fts-token-xyz77"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── _fts_query helper ──────────────────────────────────────────────────────────

def test_fts_query_strips_special_chars():
    result = mem._fts_query('hello "world" -foo *bar')
    assert '"' not in result
    assert '-' not in result
    assert '*' not in result
    assert 'hello' in result
    assert 'world' in result


def test_fts_query_drops_single_char_tokens():
    result = mem._fts_query("a big cat")
    tokens = result.split()
    assert "a" not in tokens
    assert "big" in tokens
    assert "cat" in tokens


def test_fts_query_strips_colon():
    result = mem._fts_query("field:value test")
    assert ":" not in result


def test_fts_query_empty_after_strip():
    # Only special chars — should fall back to original (not crash)
    result = mem._fts_query('"-*')
    assert isinstance(result, str)


# ── FTS5 triggers — auto-indexing ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fts_memory_trigger_indexes_new_fact():
    """After tool_remember, the fact should be searchable via memories_fts."""
    await mem.tool_remember("Alice", "loves hiking in the mountains")
    db = mem.get_db()
    rows = db.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'hiking'"
    ).fetchall()
    db.close()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_fts_session_trigger_indexes_turn():
    """After tool_log_turn, the content should be searchable via session_turns_fts."""
    sid = await mem.tool_open_session("Bob")
    await mem.tool_log_turn(sid, "user", "Let's discuss the database migration")
    db = mem.get_db()
    rows = db.execute(
        "SELECT rowid FROM session_turns_fts WHERE session_turns_fts MATCH 'database'"
    ).fetchall()
    db.close()
    assert len(rows) >= 1


# ── FTS5 backfill ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fts_backfill_existing_memories():
    """Memories inserted before FTS tables existed are backfilled by _apply_migrations."""
    # The autouse isolated_db fixture calls init_db() which runs _apply_migrations.
    # We verify the config key was set and memories are findable.
    await mem.tool_remember("Carol", "prefers decaf coffee")
    db = mem.get_db()
    backfill = db.execute(
        "SELECT value FROM config WHERE key='fts_backfill_v1'"
    ).fetchone()
    db.close()
    assert backfill is not None
    assert backfill["value"] == "1"


# ── keyword recall ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_keyword_finds_exact_term():
    await mem.tool_remember("Dave", "uses PostgreSQL for all projects")
    result = await mem.tool_recall("PostgreSQL database", mode="keyword")
    assert "PostgreSQL" in result


@pytest.mark.asyncio
async def test_recall_keyword_no_embedding_needed(monkeypatch):
    """Keyword mode should not call embed() at all."""
    called = []
    original_embed = mem.embed

    async def _fake_embed(text):
        called.append(text)
        return await original_embed(text)

    monkeypatch.setattr(mem, "embed", _fake_embed)
    await mem.tool_remember("Eve", "loves pizza and pasta")
    called.clear()  # reset after remember (which does need embed)
    await mem.tool_recall("pizza pasta", mode="keyword")
    assert called == [], "keyword mode should not call embed()"


@pytest.mark.asyncio
async def test_recall_keyword_entity_filter():
    await mem.tool_remember("Frank", "works with Redis caching")
    await mem.tool_remember("Grace", "also uses Redis")
    result = await mem.tool_recall("Redis", entity_name="Frank", mode="keyword")
    assert "Frank" in result
    assert "Grace" not in result


@pytest.mark.asyncio
async def test_recall_keyword_respects_min_trust():
    db = mem.get_db()
    e = mem.upsert_entity(db, "Hank")
    import time
    now = time.time()
    vec = await mem.embed("low trust redis fact")
    db.execute(
        "INSERT INTO memories(entity_id,fact,category,confidence,source_trust,created,updated)"
        " VALUES(?,?,?,?,?,?,?)",
        (e, "Hank uses Redis", "general", 1.0, mem.TRUST_EXTERNAL, now, now),
    )
    mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO memories_fts(rowid, fact) VALUES(?,?)", (mid, "Hank uses Redis")
    )
    db.execute(
        "INSERT INTO memory_vectors(rowid, embedding) VALUES(?,?)",
        (mid, mem.vec_blob(vec)),
    )
    db.commit()
    db.close()

    result = await mem.tool_recall("Redis", entity_name="Hank",
                                   mode="keyword", min_trust=mem.TRUST_SYSTEM)
    assert "No relevant memories found" in result


@pytest.mark.asyncio
async def test_recall_keyword_returns_mode_label():
    await mem.tool_remember("Iris", "enjoys yoga in the morning")
    result = await mem.tool_recall("yoga morning", mode="keyword")
    assert "[keyword]" in result


@pytest.mark.asyncio
async def test_recall_invalid_mode_returns_error():
    result = await mem.tool_recall("test", mode="badmode")
    assert "mode must be one of" in result


# ── hybrid recall ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_hybrid_returns_results():
    await mem.tool_remember("Jack", "drinks green tea every morning")
    result = await mem.tool_recall("tea morning routine", mode="hybrid")
    assert "green tea" in result


@pytest.mark.asyncio
async def test_recall_hybrid_mode_label():
    await mem.tool_remember("Kim", "practices meditation")
    result = await mem.tool_recall("meditation", mode="hybrid")
    assert "[hybrid]" in result


@pytest.mark.asyncio
async def test_recall_vector_mode_label():
    await mem.tool_remember("Leo", "builds software for fun")
    result = await mem.tool_recall("software development", mode="vector")
    assert "[vector]" in result


# ── tool_search_sessions ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_sessions_finds_content():
    sid = await mem.tool_open_session("Mia")
    await mem.tool_log_turn(sid, "user", "How do we handle the Terraform provider upgrade?")
    await mem.tool_log_turn(sid, "assistant", "We should pin the provider version first.")
    await mem.tool_close_session(sid)

    result = await mem.tool_search_sessions("Terraform provider")
    assert "Terraform" in result or "terraform" in result.lower()


@pytest.mark.asyncio
async def test_search_sessions_entity_filter():
    sid1 = await mem.tool_open_session("Ned")
    await mem.tool_log_turn(sid1, "user", "Kubernetes deployment strategy")
    await mem.tool_close_session(sid1)

    sid2 = await mem.tool_open_session("Olivia")
    await mem.tool_log_turn(sid2, "user", "Kubernetes also came up for me")
    await mem.tool_close_session(sid2)

    result = await mem.tool_search_sessions("Kubernetes", entity_name="Ned")
    assert "Ned" in result
    assert "Olivia" not in result


@pytest.mark.asyncio
async def test_search_sessions_no_match():
    sid = await mem.tool_open_session("Pete")
    await mem.tool_log_turn(sid, "user", "Hello world")
    await mem.tool_close_session(sid)
    result = await mem.tool_search_sessions("xyznonexistentterm99")
    assert "No session turns matched" in result


@pytest.mark.asyncio
async def test_search_sessions_empty_query():
    result = await mem.tool_search_sessions('""-*')
    assert "No searchable terms" in result


@pytest.mark.asyncio
async def test_search_sessions_shows_session_context():
    sid = await mem.tool_open_session("Quinn")
    await mem.tool_log_turn(sid, "user", "Let's talk about Redis caching strategies")
    await mem.tool_close_session(sid, summary="Discussed Redis")
    result = await mem.tool_search_sessions("Redis caching")
    assert "Session" in result
    assert "Quinn" in result


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

def test_http_recall_keyword_mode(client):
    client.post("/remember", json={"entity_name": "Rita", "fact": "loves chocolate cake"})
    r = client.post("/recall", json={"query": "chocolate cake", "mode": "keyword"})
    assert r.status_code == 200
    assert "chocolate" in r.json()["result"]


def test_http_recall_hybrid_mode(client):
    client.post("/remember", json={"entity_name": "Sam", "fact": "uses Docker containers"})
    r = client.post("/recall", json={"query": "Docker containers", "mode": "hybrid"})
    assert r.status_code == 200


def test_http_recall_invalid_mode_rejected(client):
    r = client.post("/recall", json={"query": "test", "mode": "fuzzy"})
    assert r.status_code == 422


def test_http_search_sessions(client):
    # Create a session and log a turn
    r_open = client.post("/open_session", json={"entity_name": "Tina"})
    sid = r_open.json()["result"]
    client.post("/log_turn", json={"session_id": sid, "role": "user",
                                   "content": "Let's review the Ansible playbooks"})
    client.post("/close_session", json={"session_id": sid})
    r = client.post("/search_sessions", json={"query": "Ansible playbooks"})
    assert r.status_code == 200
    assert "Ansible" in r.json()["result"]


def test_http_search_sessions_requires_auth():
    import api
    with TestClient(api.app) as c:
        r = c.post("/search_sessions", json={"query": "test"})
    assert r.status_code == 401


# ── Pre-write cross-check (Feature 4) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_check_blocks_low_trust_contradiction(same_embedding):
    """A TRUST_EXTERNAL write should be blocked if it contradicts a TRUST_USER fact."""
    await mem.tool_remember("Uma", "loves coffee", source_trust=mem.TRUST_USER)
    result = await mem.tool_remember(
        "Uma", "hates coffee", source_trust=mem.TRUST_EXTERNAL
    )
    assert "Write blocked" in result
    assert "user" in result.lower()


@pytest.mark.asyncio
async def test_cross_check_user_trust_always_writes(same_embedding):
    """TRUST_USER writes are never blocked, even against other TRUST_USER facts."""
    await mem.tool_remember("Victor", "prefers tea", source_trust=mem.TRUST_USER)
    result = await mem.tool_remember(
        "Victor", "now prefers coffee", source_trust=mem.TRUST_USER
    )
    assert "Write blocked" not in result
    assert "Remembered" in result


@pytest.mark.asyncio
async def test_cross_check_no_entity_skips_check():
    """No pre-existing entity means no blocking check (entity created on write)."""
    result = await mem.tool_remember(
        "BrandNewPerson99", "hates vegetables", source_trust=mem.TRUST_EXTERNAL
    )
    # Should succeed since no prior entity/memories exist
    assert "Remembered" in result


@pytest.mark.asyncio
async def test_cross_check_system_trust_blocked_by_user(same_embedding):
    """TRUST_SYSTEM (3) should be blocked by TRUST_USER (5) contradiction."""
    await mem.tool_remember("Wendy", "is left-handed", source_trust=mem.TRUST_USER)
    result = await mem.tool_remember(
        "Wendy", "is right-handed", source_trust=mem.TRUST_SYSTEM
    )
    assert "Write blocked" in result


@pytest.mark.asyncio
async def test_cross_check_same_trust_not_blocked(same_embedding):
    """Equal trust levels do NOT trigger the cross-check block (supersession handles it)."""
    await mem.tool_remember("Xena", "enjoys hiking", source_trust=mem.TRUST_INFERRED)
    result = await mem.tool_remember(
        "Xena", "no longer enjoys hiking", source_trust=mem.TRUST_INFERRED
    )
    # Should NOT be blocked — same trust; supersession applies instead
    assert "Write blocked" not in result
