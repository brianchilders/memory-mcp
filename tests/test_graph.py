"""
Tests for the entity relationship graph endpoints.

  GET /graph      — vis.js SPA (auth-exempt HTML page, like /admin)
  GET /api/graph  — { nodes, edges } JSON

Follows the api_auth + client fixture pattern from test_api.py.
"""

import pytest
from fastapi.testclient import TestClient

_TEST_TOKEN = "test-graph-token-xyz99"


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

def _remember(client, name, fact, entity_type="person", category="general"):
    r = client.post("/remember", json={
        "entity_name": name,
        "fact":        fact,
        "entity_type": entity_type,
        "category":    category,
    })
    assert r.status_code == 200, r.text


def _relate(client, a, b, rel_type):
    r = client.post("/relate", json={"entity_a": a, "entity_b": b, "rel_type": rel_type})
    assert r.status_code == 200, r.text


def _unrelate(client, a, b, rel_type):
    r = client.post("/unrelate", json={"entity_a": a, "entity_b": b, "rel_type": rel_type})
    assert r.status_code == 200, r.text


# ── GET /graph — HTML page ────────────────────────────────────────────────────

def test_graph_page_returns_html(client):
    r = client.get("/graph")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_graph_page_includes_visjs(client):
    r = client.get("/graph")
    assert "vis-network" in r.text


def test_graph_page_fetches_api_graph(client):
    """The SPA must reference the /api/graph data endpoint."""
    r = client.get("/graph")
    assert "/api/graph" in r.text


def test_graph_page_accessible_without_auth(api_auth):
    """HTML page is auth-exempt (like /admin) — browsers load it without headers."""
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.get("/graph")
        assert r.status_code == 200


# ── GET /api/graph — empty database ──────────────────────────────────────────

def test_api_graph_empty_db(client):
    r = client.get("/api/graph")
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"] == []
    assert body["edges"] == []


# ── GET /api/graph — node structure ──────────────────────────────────────────

def test_api_graph_node_required_fields(client):
    _remember(client, "Alice", "Works in tech")
    r = client.get("/api/graph")
    assert r.status_code == 200
    node = r.json()["nodes"][0]
    assert "id"           in node
    assert "name"         in node
    assert "type"         in node
    assert "memory_count" in node
    assert "memories"     in node


def test_api_graph_multiple_nodes(client):
    _remember(client, "Alice", "Fact A")
    _remember(client, "Bob",   "Fact B")
    r = client.get("/api/graph")
    names = {n["name"] for n in r.json()["nodes"]}
    assert names == {"Alice", "Bob"}


def test_api_graph_node_type_preserved(client):
    _remember(client, "living_room", "Main room", "room")
    r = client.get("/api/graph")
    node = next(n for n in r.json()["nodes"] if n["name"] == "living_room")
    assert node["type"] == "room"


def test_api_graph_memory_count(client):
    _remember(client, "Alice", "Fact 1")
    _remember(client, "Alice", "Fact 2")
    _remember(client, "Alice", "Fact 3")
    r = client.get("/api/graph")
    alice = next(n for n in r.json()["nodes"] if n["name"] == "Alice")
    assert alice["memory_count"] == 3


def test_api_graph_memories_inline(client):
    _remember(client, "Alice", "Likes coffee", category="preference")
    r = client.get("/api/graph")
    alice = next(n for n in r.json()["nodes"] if n["name"] == "Alice")
    assert len(alice["memories"]) == 1
    m = alice["memories"][0]
    assert m["fact"]     == "Likes coffee"
    assert m["category"] == "preference"
    assert "confidence"  in m


def test_api_graph_node_with_no_memories(client):
    _remember(client, "Alice", "A fact")
    _remember(client, "Bob",   "Bob fact")
    # Bob has one memory, Alice has one — just verify structure
    r  = client.get("/api/graph")
    bob = next(n for n in r.json()["nodes"] if n["name"] == "Bob")
    assert isinstance(bob["memories"], list)


# ── GET /api/graph — edge structure ──────────────────────────────────────────

def test_api_graph_edge_required_fields(client):
    _remember(client, "Alice", "Fact")
    _remember(client, "Bob",   "Fact")
    _relate(client, "Alice", "Bob", "colleague")
    r = client.get("/api/graph")
    edge = r.json()["edges"][0]
    assert "from"  in edge
    assert "to"    in edge
    assert "label" in edge


def test_api_graph_edge_label(client):
    _remember(client, "Alice", "Fact")
    _remember(client, "Bob",   "Fact")
    _relate(client, "Alice", "Bob", "spouse")
    r = client.get("/api/graph")
    assert r.json()["edges"][0]["label"] == "spouse"


def test_api_graph_inactive_edge_excluded(client):
    """Soft-deleted relations (valid_until set by unrelate) must not appear."""
    _remember(client, "Alice", "Fact")
    _remember(client, "Bob",   "Fact")
    _relate(client,   "Alice", "Bob", "colleague")
    _unrelate(client, "Alice", "Bob", "colleague")
    r = client.get("/api/graph")
    assert r.json()["edges"] == []


def test_api_graph_only_active_edges(client):
    """Active edges appear; unrelated edge is absent."""
    _remember(client, "Alice", "Fact")
    _remember(client, "Bob",   "Fact")
    _remember(client, "Carol", "Fact")
    _relate(client,   "Alice", "Bob",   "friend")
    _relate(client,   "Alice", "Carol", "colleague")
    _unrelate(client, "Alice", "Carol", "colleague")
    edges = client.get("/api/graph").json()["edges"]
    assert len(edges) == 1
    assert edges[0]["label"] == "friend"
