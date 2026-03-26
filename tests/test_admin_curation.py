"""
Tests for admin curation routes:
  POST /admin/memory/{id}/delete      — delete a single memory (HTMX)
  POST /admin/entity/{name}/remember  — add an observation (HTMX)

Uses api_auth + client fixture pattern.  Auth middleware protects /admin/*
when a token is configured.
"""

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-curation-token-abc33"


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

def _remember(client, name, fact, category="general"):
    r = client.post("/remember", json={
        "entity_name": name, "fact": fact,
        "entity_type": "person", "category": category,
    })
    assert r.status_code == 200, r.text


def _get_memory_id(name, fact):
    db  = mem.get_db()
    e   = db.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
    row = db.execute(
        "SELECT id FROM memories WHERE entity_id=? AND fact=?", (e["id"], fact)
    ).fetchone()
    db.close()
    return row["id"] if row else None


def _memory_exists(mid):
    db  = mem.get_db()
    row = db.execute("SELECT id FROM memories WHERE id=?", (mid,)).fetchone()
    db.close()
    return row is not None


def _count_memories(name):
    db  = mem.get_db()
    e   = db.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
    if not e:
        db.close()
        return 0
    count = db.execute(
        "SELECT COUNT(*) FROM memories WHERE entity_id=?", (e["id"],)
    ).fetchone()[0]
    db.close()
    return count


# ── POST /admin/memory/{id}/delete ────────────────────────────────────────────

def test_delete_memory_returns_200(client):
    _remember(client, "Alice", "To be deleted")
    mid = _get_memory_id("Alice", "To be deleted")
    r   = client.post(f"/admin/memory/{mid}/delete")
    assert r.status_code == 200


def test_delete_memory_removes_from_db(client):
    _remember(client, "Alice", "Ephemeral fact")
    mid = _get_memory_id("Alice", "Ephemeral fact")
    assert _memory_exists(mid)
    client.post(f"/admin/memory/{mid}/delete")
    assert not _memory_exists(mid)


def test_delete_memory_response_is_empty(client):
    _remember(client, "Alice", "Gone fact")
    mid = _get_memory_id("Alice", "Gone fact")
    r   = client.post(f"/admin/memory/{mid}/delete")
    # HTMX outerHTML swap to empty removes the <li>
    assert r.text == ""


def test_delete_nonexistent_memory_returns_404(client):
    r = client.post("/admin/memory/999999/delete")
    assert r.status_code == 404


def test_delete_memory_also_removes_vector(client):
    _remember(client, "Alice", "Vector fact")
    mid = _get_memory_id("Alice", "Vector fact")
    client.post(f"/admin/memory/{mid}/delete")
    db  = mem.get_db()
    vec = db.execute("SELECT rowid FROM memory_vectors WHERE rowid=?", (mid,)).fetchone()
    db.close()
    assert vec is None


def test_delete_other_memories_untouched(client):
    _remember(client, "Alice", "Keep this")
    _remember(client, "Alice", "Delete this")
    mid_del = _get_memory_id("Alice", "Delete this")
    mid_keep = _get_memory_id("Alice", "Keep this")
    client.post(f"/admin/memory/{mid_del}/delete")
    assert _memory_exists(mid_keep)


# ── POST /admin/entity/{name}/remember ────────────────────────────────────────

def test_add_observation_returns_200(client):
    _remember(client, "Bob", "Existing fact")
    r = client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "New observation", "category": "general"},
    )
    assert r.status_code == 200


def test_add_observation_stored_in_db(client):
    _remember(client, "Bob", "Seed fact")
    client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "Brand new fact", "category": "preference"},
    )
    mid = _get_memory_id("Bob", "Brand new fact")
    assert mid is not None


def test_add_observation_response_contains_fact(client):
    _remember(client, "Bob", "Existing")
    r = client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "Returned fact text", "category": "general"},
    )
    assert "Returned fact text" in r.text


def test_add_observation_response_contains_category(client):
    _remember(client, "Bob", "Existing")
    r = client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "Category fact", "category": "habit"},
    )
    assert "habit" in r.text


def test_add_observation_invalid_category_defaults_to_general(client):
    _remember(client, "Bob", "Existing")
    r = client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "Invalid cat fact", "category": "nonsense"},
    )
    assert r.status_code == 200
    db  = mem.get_db()
    e   = db.execute("SELECT id FROM entities WHERE name='Bob'").fetchone()
    row = db.execute(
        "SELECT category FROM memories WHERE entity_id=? AND fact='Invalid cat fact'",
        (e["id"],),
    ).fetchone()
    db.close()
    assert row["category"] == "general"


def test_add_observation_empty_fact_returns_400(client):
    _remember(client, "Bob", "Existing")
    r = client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "   ", "category": "general"},
    )
    assert r.status_code == 400


def test_add_observation_to_nonexistent_entity_returns_404(client):
    r = client.post(
        "/admin/entity/NoSuchEntity/remember",
        data={"fact": "Fact for ghost", "category": "general"},
    )
    assert r.status_code == 404


def test_add_observation_source_tagged_as_admin_ui(client):
    _remember(client, "Bob", "Seed")
    client.post(
        "/admin/entity/Bob/remember",
        data={"fact": "Admin-sourced fact", "category": "general"},
    )
    db  = mem.get_db()
    e   = db.execute("SELECT id FROM entities WHERE name='Bob'").fetchone()
    row = db.execute(
        "SELECT source FROM memories WHERE entity_id=? AND fact='Admin-sourced fact'",
        (e["id"],),
    ).fetchone()
    db.close()
    assert row["source"] == "admin_ui"


def test_add_observation_increments_memory_count(client):
    _remember(client, "Charlie", "First")
    before = _count_memories("Charlie")
    client.post(
        "/admin/entity/Charlie/remember",
        data={"fact": "Second", "category": "general"},
    )
    after = _count_memories("Charlie")
    assert after == before + 1
