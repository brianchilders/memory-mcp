"""
Tests for Feature 5 — Confidence Decay.

Covers:
  _decay_memories()           — exponential decay, floor, per-category halflife,
                                disabled when halflife=0, skips superseded rows
  tool_get_fading_memories()  — threshold filtering, entity scoping, ordering
  tool_recall() boost         — recalled memories get confidence nudge
  GET /fading                 — HTTP endpoint, query params, auth
"""

import math
import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-decay-token-abc42"


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


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _plant(entity_name: str, fact: str, confidence: float,
                 days_old: float = 0.0, category: str = "general") -> int:
    """Insert a memory with configurable confidence and last_accessed age."""
    db = mem.get_db()
    now = time.time()
    ts  = now - days_old * 86400
    vec = await mem.embed(fact)
    cur = db.execute(
        """INSERT INTO memories(entity_id, fact, category, confidence, source,
                                created, updated, last_accessed)
           VALUES(
               (SELECT id FROM entities WHERE name=?),
               ?, ?, ?, 'test', ?, ?, ?
           )""",
        (entity_name, fact, category, confidence, ts, ts, ts),
    )
    mid = cur.lastrowid
    db.execute(
        "INSERT INTO memory_vectors(rowid, embedding) VALUES(?, ?)",
        (mid, mem.vec_blob(vec)),
    )
    db.commit()
    db.close()
    return mid


def _upsert(name: str) -> int:
    db = mem.get_db()
    eid = mem.upsert_entity(db, name, "person")
    db.commit()
    db.close()
    return eid


def _get_conf(mid: int) -> float:
    db = mem.get_db()
    row = db.execute("SELECT confidence FROM memories WHERE id=?", (mid,)).fetchone()
    db.close()
    return row["confidence"]


# ── _decay_memories() ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decay_reduces_confidence_for_old_memory():
    _upsert("Alice")
    # 90-day-old memory; with 90-day halflife it should halve to ~0.5
    mid = await _plant("Alice", "Likes hiking", 1.0, days_old=90.0)

    original_halflife = mem._decay_halflife_global
    mem._decay_halflife_global = 90.0
    try:
        await mem._decay_memories()
    finally:
        mem._decay_halflife_global = original_halflife

    conf = _get_conf(mid)
    # exp(-ln2 * 90/90) = 0.5
    assert conf == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_decay_floor_applied():
    _upsert("Alice")
    # 1000 days old — would decay below floor without clamping
    mid = await _plant("Alice", "Very old fact", 1.0, days_old=1000.0)

    original = mem._decay_halflife_global
    mem._decay_halflife_global = 90.0
    try:
        await mem._decay_memories()
    finally:
        mem._decay_halflife_global = original

    assert _get_conf(mid) == pytest.approx(mem.DECAY_CONFIDENCE_FLOOR, abs=0.001)


@pytest.mark.asyncio
async def test_decay_skips_superseded_memories():
    _upsert("Alice")
    # Insert a newer memory that will serve as the superseding target
    newer_id = await _plant("Alice", "Newer replacement fact", 1.0, days_old=0.0)

    db = mem.get_db()
    now = time.time()
    ts  = now - 180 * 86400  # 180 days old
    cur = db.execute(
        """INSERT INTO memories(entity_id, fact, category, confidence, source,
                                created, updated, last_accessed, superseded_by)
           VALUES(
               (SELECT id FROM entities WHERE name='Alice'),
               'Old superseded fact', 'general', 0.9, 'test', ?, ?, ?, ?
           )""",
        (ts, ts, ts, newer_id),
    )
    mid = cur.lastrowid
    vec = await mem.embed("Old superseded fact")
    db.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES(?, ?)",
               (mid, mem.vec_blob(vec)))
    db.commit()
    db.close()

    original = mem._decay_halflife_global
    mem._decay_halflife_global = 90.0
    try:
        await mem._decay_memories()
    finally:
        mem._decay_halflife_global = original

    # Superseded row must not be touched
    assert _get_conf(mid) == pytest.approx(0.9, abs=0.001)


@pytest.mark.asyncio
async def test_decay_no_change_for_fresh_memory():
    _upsert("Alice")
    mid = await _plant("Alice", "Fresh fact", 0.9, days_old=0.0)

    original = mem._decay_halflife_global
    mem._decay_halflife_global = 90.0
    try:
        updated = await mem._decay_memories()
    finally:
        mem._decay_halflife_global = original

    # A zero-day-old memory should not move enough to trigger a write
    assert _get_conf(mid) == pytest.approx(0.9, abs=0.01)


@pytest.mark.asyncio
async def test_decay_disabled_when_halflife_zero():
    _upsert("Alice")
    mid = await _plant("Alice", "Fact to not decay", 0.8, days_old=365.0)

    original = mem._decay_halflife_global
    mem._decay_halflife_global = 0.0
    try:
        count = await mem._decay_memories()
    finally:
        mem._decay_halflife_global = original

    assert count == 0
    assert _get_conf(mid) == pytest.approx(0.8, abs=0.001)


@pytest.mark.asyncio
async def test_decay_per_category_halflife_override():
    _upsert("Alice")
    # habit with 30-day halflife: 30 days → conf * 0.5
    mid = await _plant("Alice", "Wakes early", 1.0, days_old=30.0, category="habit")

    original_global = mem._decay_halflife_global
    original_cat    = mem._decay_halflife_by_category.copy()
    mem._decay_halflife_global       = 90.0
    mem._decay_halflife_by_category  = {"habit": 30.0}
    try:
        await mem._decay_memories()
    finally:
        mem._decay_halflife_global      = original_global
        mem._decay_halflife_by_category = original_cat

    # 30-day halflife, 30 days old → 0.5
    assert _get_conf(mid) == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_decay_global_halflife_used_when_no_category_override():
    _upsert("Alice")
    mid = await _plant("Alice", "General insight", 1.0, days_old=90.0, category="insight")

    original_global = mem._decay_halflife_global
    original_cat    = mem._decay_halflife_by_category.copy()
    mem._decay_halflife_global       = 90.0
    mem._decay_halflife_by_category  = {"habit": 30.0}  # no override for 'insight'
    try:
        await mem._decay_memories()
    finally:
        mem._decay_halflife_global      = original_global
        mem._decay_halflife_by_category = original_cat

    # Falls back to global 90-day halflife → 0.5
    assert _get_conf(mid) == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_decay_returns_count_of_changed_memories():
    _upsert("Alice")
    await _plant("Alice", "Old fact A", 1.0, days_old=90.0)
    await _plant("Alice", "Old fact B", 1.0, days_old=90.0)
    await _plant("Alice", "Fresh fact", 0.9, days_old=0.0)

    original = mem._decay_halflife_global
    mem._decay_halflife_global = 90.0
    try:
        count = await mem._decay_memories()
    finally:
        mem._decay_halflife_global = original

    assert count >= 2  # at minimum the two 90-day-old memories


# ── tool_recall() confidence boost ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_boosts_confidence():
    _upsert("Alice")
    mid = await _plant("Alice", "Prefers coffee", 0.6, days_old=1.0)

    await mem.tool_recall("coffee", entity_name="Alice", top_k=5)

    conf = _get_conf(mid)
    expected = min(1.0, 0.6 + mem.DECAY_RECALL_BOOST)
    assert conf == pytest.approx(expected, abs=0.001)


@pytest.mark.asyncio
async def test_recall_boost_does_not_exceed_1():
    _upsert("Alice")
    mid = await _plant("Alice", "Prefers tea", 0.99, days_old=1.0)

    await mem.tool_recall("tea", entity_name="Alice", top_k=5)

    assert _get_conf(mid) <= 1.0


# ── tool_get_fading_memories() ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_fading_returns_memories_below_threshold():
    _upsert("Alice")
    await _plant("Alice", "Faded fact", 0.3, days_old=30.0)
    await _plant("Alice", "Strong fact", 0.9, days_old=30.0)

    result = await mem.tool_get_fading_memories(threshold=0.5)
    assert "Faded fact" in result
    assert "Strong fact" not in result


@pytest.mark.asyncio
async def test_get_fading_scoped_to_entity():
    _upsert("Alice")
    _upsert("Bob")
    await _plant("Alice", "Alice faded", 0.2, days_old=10.0)
    await _plant("Bob",   "Bob faded",   0.2, days_old=10.0)

    result = await mem.tool_get_fading_memories(entity_name="Alice", threshold=0.5)
    assert "Alice faded" in result
    assert "Bob faded"   not in result


@pytest.mark.asyncio
async def test_get_fading_empty_result_message():
    _upsert("Alice")
    await _plant("Alice", "Confident fact", 0.9, days_old=0.0)

    result = await mem.tool_get_fading_memories(threshold=0.5)
    assert "No fading memories" in result


@pytest.mark.asyncio
async def test_get_fading_ordered_by_confidence_asc():
    _upsert("Alice")
    await _plant("Alice", "Low conf",  0.1, days_old=5.0)
    await _plant("Alice", "Mid conf",  0.3, days_old=5.0)

    result = await mem.tool_get_fading_memories(threshold=0.5)
    low_pos = result.index("Low conf")
    mid_pos = result.index("Mid conf")
    assert low_pos < mid_pos


@pytest.mark.asyncio
async def test_get_fading_respects_limit():
    _upsert("Alice")
    for i in range(5):
        await _plant("Alice", f"Faded fact {i}", 0.2, days_old=10.0)

    result = await mem.tool_get_fading_memories(threshold=0.5, limit=2)
    # Only 2 entries should appear (each line has "Faded fact N")
    count = result.count("Faded fact")
    assert count == 2


@pytest.mark.asyncio
async def test_get_fading_excludes_superseded():
    _upsert("Alice")
    # Create a newer memory to act as the superseding target
    newer_id = await _plant("Alice", "Newer replacement for faded", 0.9, days_old=0.0)

    db = mem.get_db()
    now = time.time()
    ts  = now - 5 * 86400
    cur = db.execute(
        """INSERT INTO memories(entity_id, fact, category, confidence, source,
                                created, updated, last_accessed, superseded_by)
           VALUES(
               (SELECT id FROM entities WHERE name='Alice'),
               'Superseded faded', 'general', 0.1, 'test', ?, ?, ?, ?
           )""",
        (ts, ts, ts, newer_id),
    )
    mid = cur.lastrowid
    vec = await mem.embed("Superseded faded")
    db.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES(?, ?)",
               (mid, mem.vec_blob(vec)))
    db.commit()
    db.close()

    result = await mem.tool_get_fading_memories(threshold=0.5)
    assert "Superseded faded" not in result


# ── GET /fading HTTP endpoint ─────────────────────────────────────────────────

def test_fading_endpoint_returns_200(client):
    assert client.get("/fading").status_code == 200


def test_fading_endpoint_respects_threshold(client):
    client.post("/remember", json={
        "entity_name": "Alice", "fact": "Faded HTTP fact",
        "entity_type": "person", "category": "general",
    })
    # Manually lower confidence below threshold via direct DB update
    db = mem.get_db()
    db.execute(
        "UPDATE memories SET confidence=0.2 WHERE fact='Faded HTTP fact'"
    )
    db.commit()
    db.close()

    r = client.get("/fading", params={"threshold": 0.5})
    assert r.status_code == 200
    assert "Faded HTTP fact" in r.json()["result"]


def test_fading_endpoint_entity_scope(client):
    client.post("/remember", json={
        "entity_name": "Alice", "fact": "Alice scoped faded",
        "entity_type": "person", "category": "general",
    })
    db = mem.get_db()
    db.execute("UPDATE memories SET confidence=0.1 WHERE fact='Alice scoped faded'")
    db.commit()
    db.close()

    r = client.get("/fading", params={"entity_name": "Alice", "threshold": 0.5})
    assert r.status_code == 200
    assert "Alice scoped faded" in r.json()["result"]


def test_fading_endpoint_requires_auth(api_auth):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.get("/fading")
        assert r.status_code == 401
