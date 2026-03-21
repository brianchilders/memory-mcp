"""
Integration tests for the FastAPI HTTP wrapper (api.py).

Uses Starlette's TestClient (sync), which runs the app in-process.
The isolated_db and mock_embed autouse fixtures from conftest.py are active,
so every test hits a fresh isolated DB with no Ollama dependency.
"""

import time

import pytest
from fastapi.testclient import TestClient

# Known token injected into the server for all API tests
_TEST_TOKEN = "test-api-token-abc123"


@pytest.fixture
def api_auth(monkeypatch):
    """
    Set a known MEMORY_API_TOKEN before init_db() runs so the auth middleware
    requires — and the client provides — a predictable bearer token.
    Must be a fixture dependency of `client` so it runs first.
    """
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    """
    Create a TestClient for api.app with the test bearer token pre-set.

    Import is deferred so that isolated_db and mock_embed fixtures are already
    applied before api.py's startup handler calls mem.init_db().
    """
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── /health ────────────────────────────────────────────────────────────────────

def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "entities"  in body
    assert "memories"  in body
    assert "readings"  in body
    assert "ts"        in body


# ── /entities ──────────────────────────────────────────────────────────────────

def test_entities_empty_on_fresh_db(client):
    r = client.get("/entities")
    assert r.status_code == 200
    assert r.json()["entities"] == []


def test_entities_lists_after_remember(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Likes jazz"})
    r = client.get("/entities")
    names = [e["name"] for e in r.json()["entities"]]
    assert "Brian" in names


# ── /remember ──────────────────────────────────────────────────────────────────

def test_remember_ok(client):
    r = client.post("/remember", json={"entity_name": "Brian", "fact": "Likes jazz"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_remember_with_category_and_confidence(client):
    r = client.post("/remember", json={
        "entity_name": "Brian",
        "fact": "Prefers 68F",
        "category": "preference",
        "confidence": 0.9,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── /recall ────────────────────────────────────────────────────────────────────

def test_recall_empty_db(client):
    r = client.post("/recall", json={"query": "coffee"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_recall_finds_memory(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Loves jazz"})
    r = client.post("/recall", json={"query": "music"})
    assert r.status_code == 200
    assert "Loves jazz" in r.json()["result"]


# ── /profile/{name} ────────────────────────────────────────────────────────────

def test_profile_unknown_entity(client):
    r = client.get("/profile/Nobody")
    assert r.status_code == 200
    assert "No entity" in r.json()["result"]


def test_profile_known_entity(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Likes coffee"})
    r = client.get("/profile/Brian")
    assert r.status_code == 200
    assert "Brian" in r.json()["result"]
    assert "Likes coffee" in r.json()["result"]


# ── /relate ────────────────────────────────────────────────────────────────────

def test_relate_ok(client):
    r = client.post("/relate", json={
        "entity_a": "Brian", "entity_b": "Sarah", "rel_type": "spouse"
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── /forget ────────────────────────────────────────────────────────────────────

def test_forget_entity(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Old fact"})
    r = client.post("/forget", json={"entity_name": "Brian"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_forget_unknown_entity(client):
    r = client.post("/forget", json={"entity_name": "Ghost"})
    assert r.status_code == 200  # not a 404 — tool returns a message
    assert r.json()["ok"] is True


# ── /record ────────────────────────────────────────────────────────────────────

def test_record_numeric(client):
    r = client.post("/record", json={
        "entity_name": "living_room",
        "metric": "temperature",
        "value": 71.4,
        "unit": "F",
        "entity_type": "room",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_record_categorical(client):
    r = client.post("/record", json={
        "entity_name": "Brian", "metric": "presence", "value": "home"
    })
    assert r.status_code == 200


def test_record_composite(client):
    r = client.post("/record", json={
        "entity_name": "Brian",
        "metric": "mood",
        "value": {"mood": "calm", "confidence": 0.91},
    })
    assert r.status_code == 200


# ── /record/bulk ───────────────────────────────────────────────────────────────

def test_record_bulk_all_succeed(client):
    r = client.post("/record/bulk", json={"readings": [
        {"entity_name": "living_room", "metric": "temperature", "value": 71.0},
        {"entity_name": "bedroom",     "metric": "temperature", "value": 68.0},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert all(item["ok"] for item in body["results"])


def test_record_bulk_empty_list(client):
    r = client.post("/record/bulk", json={"readings": []})
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ── /query_stream ──────────────────────────────────────────────────────────────

def test_query_stream_no_data(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "exists"})
    r = client.post("/query_stream", json={
        "entity_name": "Brian", "metric": "temperature"
    })
    assert r.status_code == 200


def test_query_stream_with_data(client):
    client.post("/record", json={
        "entity_name": "Brian", "metric": "temperature", "value": 70.0
    })
    r = client.post("/query_stream", json={
        "entity_name": "Brian", "metric": "temperature", "granularity": "raw"
    })
    assert r.status_code == 200
    assert "temperature" in r.json()["result"]


# ── /get_trends ────────────────────────────────────────────────────────────────

def test_get_trends_no_data(client):
    r = client.post("/get_trends", json={
        "entity_name": "Brian", "metric": "temperature"
    })
    assert r.status_code == 200


def test_get_trends_with_data(client):
    now = time.time()
    for i in range(3):
        client.post("/record", json={
            "entity_name": "Brian", "metric": "temperature",
            "value": 68.0 + i, "ts": now - i * 3600,
        })
    r = client.post("/get_trends", json={
        "entity_name": "Brian", "metric": "temperature", "window": "day"
    })
    assert r.status_code == 200
    assert "Avg" in r.json()["result"]


# ── /schedule ──────────────────────────────────────────────────────────────────

def test_schedule_event(client):
    r = client.post("/schedule", json={
        "entity_name": "Brian",
        "title": "Doctor appointment",
        "start_ts": time.time() + 86400,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── /cross_query ───────────────────────────────────────────────────────────────

def test_cross_query_empty(client):
    r = client.post("/cross_query", json={"query": "anything"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_cross_query_with_memory(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Likes coffee"})
    r = client.post("/cross_query", json={"query": "Brian's preferences"})
    assert r.status_code == 200
    assert "Likes coffee" in r.json()["result"]


# ── /prune ─────────────────────────────────────────────────────────────────────

def test_prune_empty_db(client):
    r = client.post("/prune")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "0" in r.json()["result"]


def test_prune_removes_old_readings(client):
    old_ts = time.time() - (365 * 86400)  # 1 year ago
    client.post("/record", json={
        "entity_name": "Brian", "metric": "temperature",
        "value": 68.0, "ts": old_ts,
    })
    client.post("/record", json={
        "entity_name": "Brian", "metric": "temperature", "value": 70.0,
    })
    r = client.post("/prune")
    assert r.status_code == 200
    assert "1" in r.json()["result"]
