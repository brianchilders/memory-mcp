"""
Smoke tests for the admin UI routes.

Verifies routes return 200, correct content-type, and expected HTML fragments.
Does not test visual rendering — just that templates render without errors
and key text is present.
"""

import time

import pytest
from fastapi.testclient import TestClient

_TEST_TOKEN = "test-admin-token-xyz789"


@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── Dashboard ──────────────────────────────────────────────────────────────────

def test_dashboard_returns_200(client):
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_dashboard_shows_stat_cards(client):
    r = client.get("/admin/")
    assert "Entities" in r.text
    assert "Memories" in r.text
    assert "Readings" in r.text


def test_dashboard_shows_prune_button(client):
    r = client.get("/admin/")
    assert "Prune" in r.text


def test_dashboard_no_data_shows_empty_messages(client):
    r = client.get("/admin/")
    assert "No memories stored yet" in r.text
    assert "No patterns promoted yet" in r.text


def test_dashboard_shows_recent_memory(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Likes jazz"})
    r = client.get("/admin/")
    assert "Likes jazz" in r.text


# ── Entity list ────────────────────────────────────────────────────────────────

def test_entities_returns_200(client):
    r = client.get("/admin/entities")
    assert r.status_code == 200


def test_entities_empty_message(client):
    r = client.get("/admin/entities")
    assert "No entities yet" in r.text


def test_entities_lists_entity(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Likes coffee"})
    r = client.get("/admin/entities")
    assert "Brian" in r.text
    assert "person" in r.text


def test_entities_shows_memory_count(client):
    client.post("/remember", json={"entity_name": "Brian", "fact": "Fact 1"})
    client.post("/remember", json={"entity_name": "Brian", "fact": "Fact 2"})
    r = client.get("/admin/entities")
    assert "Brian" in r.text
    # Memory count column should show 2 somewhere near Brian
    assert "2" in r.text


# ── Entity detail ──────────────────────────────────────────────────────────────

def test_entity_detail_404_for_unknown(client):
    r = client.get("/admin/entity/Nobody")
    assert r.status_code == 404


def test_entity_detail_shows_memories(client):
    client.post("/remember", json={
        "entity_name": "Brian",
        "fact": "Loves hiking",
        "category": "habit",
    })
    r = client.get("/admin/entity/Brian")
    assert r.status_code == 200
    assert "Loves hiking" in r.text
    assert "habit" in r.text


def test_entity_detail_shows_readings(client):
    client.post("/record", json={
        "entity_name": "Brian", "metric": "temperature", "value": 72.0, "unit": "F"
    })
    r = client.get("/admin/entity/Brian")
    assert r.status_code == 200
    assert "temperature" in r.text
    assert "72" in r.text


def test_entity_detail_shows_relationships(client):
    client.post("/relate", json={
        "entity_a": "Brian", "entity_b": "Sarah", "rel_type": "spouse"
    })
    r = client.get("/admin/entity/Brian")
    assert "Sarah" in r.text
    assert "spouse" in r.text


def test_entity_detail_shows_schedule(client):
    future_ts = time.time() + 86400
    client.post("/schedule", json={
        "entity_name": "Brian",
        "title": "Annual review",
        "start_ts": future_ts,
    })
    r = client.get("/admin/entity/Brian")
    assert "Annual review" in r.text


# ── Readings stream ────────────────────────────────────────────────────────────

def test_readings_returns_200(client):
    r = client.get("/admin/readings")
    assert r.status_code == 200


def test_readings_empty_message(client):
    r = client.get("/admin/readings")
    assert "No readings recorded yet" in r.text


def test_readings_shows_reading(client):
    client.post("/record", json={
        "entity_name": "living_room",
        "metric": "temperature",
        "value": 70.5,
        "unit": "F",
        "entity_type": "room",
    })
    r = client.get("/admin/readings")
    assert "living_room" in r.text
    assert "temperature" in r.text


def test_readings_limit_param(client):
    for i in range(10):
        client.post("/record", json={
            "entity_name": "Brian", "metric": "steps", "value": i * 100
        })
    r = client.get("/admin/readings?limit=5")
    assert r.status_code == 200
    # The page should still render with a 5-row limit
    assert "steps" in r.text


# ── Prune (HTMX endpoint) ──────────────────────────────────────────────────────

def test_admin_prune_returns_html_fragment(client):
    r = client.post("/admin/prune")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Pruned" in r.text


def test_admin_prune_reports_count(client):
    old_ts = time.time() - 365 * 86400  # 1 year ago
    client.post("/record", json={
        "entity_name": "Brian", "metric": "temperature",
        "value": 68.0, "ts": old_ts,
    })
    client.post("/record", json={
        "entity_name": "Brian", "metric": "temperature", "value": 70.0,
    })
    r = client.post("/admin/prune")
    assert "1" in r.text   # pruned 1 old reading
    assert "remain" in r.text


# ── Settings page ──────────────────────────────────────────────────────────────

def test_settings_returns_200(client):
    r = client.get("/admin/settings")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_settings_shows_auth_enabled(client):
    r = client.get("/admin/settings")
    assert "Enabled" in r.text
    assert "API Authentication" in r.text


def test_settings_shows_env_source(client):
    # Token comes from MEMORY_API_TOKEN env var (set by api_auth fixture)
    r = client.get("/admin/settings")
    assert "env" in r.text or "MEMORY_API_TOKEN" in r.text


def test_settings_regenerate_blocked_for_env_token(client):
    # When token is from env var, regenerate should return a warning
    r = client.post("/admin/token/regenerate")
    assert r.status_code == 200
    assert "environment variable" in r.text
