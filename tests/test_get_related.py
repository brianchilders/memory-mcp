"""
Tests for tool_get_related() and GET /related/{entity_name}.

Covers:
  tool_get_related() — basic traversal, depth, isolated entity, bidirectional,
                       multi-hop, depth clamping, missing entity
  GET /related/{name} — HTTP round-trip, auth, query params
"""

import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-related-token-abc55"


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

def _upsert(name: str, entity_type: str = "person") -> int:
    db  = mem.get_db()
    eid = mem.upsert_entity(db, name, entity_type)
    db.commit()
    db.close()
    return eid


def _relate(a: str, b: str, rel_type: str) -> None:
    db  = mem.get_db()
    now = time.time()
    a_id = db.execute("SELECT id FROM entities WHERE name=?", (a,)).fetchone()["id"]
    b_id = db.execute("SELECT id FROM entities WHERE name=?", (b,)).fetchone()["id"]
    db.execute(
        """INSERT INTO relations(entity_a, entity_b, rel_type, meta, created, valid_from)
           VALUES (?, ?, ?, '{}', ?, ?)""",
        (a_id, b_id, rel_type, now, now),
    )
    db.commit()
    db.close()


# ── tool_get_related() tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direct_neighbor_found():
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "friend")

    result = await mem.tool_get_related("Alice", depth=1)
    assert "Bob" in result


@pytest.mark.asyncio
async def test_no_entity_returns_message():
    result = await mem.tool_get_related("Nobody")
    assert "No entity" in result


@pytest.mark.asyncio
async def test_isolated_entity_returns_empty_message():
    _upsert("Isolated")
    result = await mem.tool_get_related("Isolated", depth=2)
    assert "No related entities" in result


@pytest.mark.asyncio
async def test_bidirectional_traversal_outgoing():
    """Alice → Bob should make Bob related to Alice."""
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "knows")

    result = await mem.tool_get_related("Alice", depth=1)
    assert "Bob" in result


@pytest.mark.asyncio
async def test_bidirectional_traversal_incoming():
    """Alice → Bob: Bob's related list should include Alice."""
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "knows")

    result = await mem.tool_get_related("Bob", depth=1)
    assert "Alice" in result


@pytest.mark.asyncio
async def test_two_hop_traversal():
    """Alice — Bob — Carol: Carol is reachable from Alice at depth 2."""
    _upsert("Alice")
    _upsert("Bob")
    _upsert("Carol")
    _relate("Alice", "Bob",   "friend")
    _relate("Bob",   "Carol", "colleague")

    result = await mem.tool_get_related("Alice", depth=2)
    assert "Carol" in result


@pytest.mark.asyncio
async def test_two_hop_not_found_at_depth_one():
    """Carol should NOT appear when depth=1 (she is 2 hops away)."""
    _upsert("Alice")
    _upsert("Bob")
    _upsert("Carol")
    _relate("Alice", "Bob",   "friend")
    _relate("Bob",   "Carol", "colleague")

    result = await mem.tool_get_related("Alice", depth=1)
    assert "Carol" not in result


@pytest.mark.asyncio
async def test_starting_entity_not_in_results():
    """The starting entity must not appear in its own related list."""
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "friend")

    result = await mem.tool_get_related("Alice", depth=2)
    # Alice should not appear in the related entities
    lines = [l for l in result.splitlines() if l.strip().startswith("[")]
    names = [l for l in lines if "Alice" in l]
    assert names == []


@pytest.mark.asyncio
async def test_inactive_relation_excluded():
    """Relations with valid_until set (soft-deleted) must not be traversed."""
    _upsert("Alice")
    _upsert("Bob")
    db  = mem.get_db()
    now = time.time()
    a_id = db.execute("SELECT id FROM entities WHERE name='Alice'").fetchone()["id"]
    b_id = db.execute("SELECT id FROM entities WHERE name='Bob'").fetchone()["id"]
    db.execute(
        """INSERT INTO relations(entity_a, entity_b, rel_type, meta,
                                 created, valid_from, valid_until)
           VALUES (?, ?, 'ex_friend', '{}', ?, ?, ?)""",
        (a_id, b_id, now, now, now),   # valid_until = now → inactive
    )
    db.commit()
    db.close()

    result = await mem.tool_get_related("Alice", depth=2)
    assert "Bob" not in result


@pytest.mark.asyncio
async def test_depth_clamped_to_max_five():
    """Passing depth=99 should be silently clamped to 5."""
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "friend")
    # Should not raise or hang — just clamps
    result = await mem.tool_get_related("Alice", depth=99)
    assert "Bob" in result


@pytest.mark.asyncio
async def test_depth_clamped_to_min_one():
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "friend")
    result = await mem.tool_get_related("Alice", depth=0)
    assert "Bob" in result   # depth 0 → clamped to 1


@pytest.mark.asyncio
async def test_hop_count_in_output():
    """Hop count labels must appear in the output."""
    _upsert("Alice")
    _upsert("Bob")
    _relate("Alice", "Bob", "friend")
    result = await mem.tool_get_related("Alice", depth=1)
    assert "1 hop" in result


@pytest.mark.asyncio
async def test_max_results_limit():
    _upsert("Hub")
    for i in range(10):
        _upsert(f"Spoke{i}")
        _relate("Hub", f"Spoke{i}", "connected")

    result = await mem.tool_get_related("Hub", depth=1, max_results=3)
    lines  = [l for l in result.splitlines() if l.strip().startswith("[")]
    assert len(lines) == 3


# ── GET /related/{entity_name} HTTP tests ─────────────────────────────────────

def test_http_related_returns_200(client):
    client.post("/remember", json={
        "entity_name": "Alice", "fact": "A fact", "entity_type": "person",
    })
    r = client.get("/related/Alice")
    assert r.status_code == 200


def test_http_related_requires_auth(api_auth):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.get("/related/Alice")
        assert r.status_code == 401


def test_http_related_depth_param(client):
    r = client.get("/related/Alice", params={"depth": 3})
    assert r.status_code == 200


def test_http_related_unknown_entity(client):
    r = client.get("/related/NoSuchEntity")
    assert r.status_code == 200
    assert "No entity" in r.json()["result"]


def test_http_related_max_results_over_limit_rejected(client):
    """max_results > 500 should return 422 (FastAPI query param validation)."""
    r = client.get("/related/Alice", params={"max_results": 99999})
    assert r.status_code == 422


def test_http_related_max_results_zero_rejected(client):
    """max_results < 1 should return 422."""
    r = client.get("/related/Alice", params={"max_results": 0})
    assert r.status_code == 422
