"""
Integration tests for the Tier 1.5 episodic memory HTTP endpoints and
the extract_and_remember endpoint (api.py).

Routes covered:
  POST /open_session
  POST /log_turn
  POST /close_session
  GET  /get_session/{session_id}
  POST /extract_and_remember

Uses the same isolated_db + mock_embed autouse fixtures from conftest.py.
A file-scoped mock_llm fixture patches _call_llm so extract_and_remember
tests are fully offline — no Ollama required.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-sessions-token-abc"


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


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """
    Patch _call_llm with a deterministic offline response.

    Returns a single extracted fact so extract_and_remember tests don't
    require a running Ollama instance. All tests in this file benefit from
    this fixture automatically.
    """
    async def fake_llm(prompt: str, model: str) -> str:
        return json.dumps([
            {"fact": "Prefers dark roast coffee", "category": "preference", "confidence": 0.9}
        ])
    monkeypatch.setattr(mem, "_call_llm", fake_llm)


# ── POST /open_session ─────────────────────────────────────────────────────────

def test_open_session_returns_int_session_id(client):
    r = client.post("/open_session", json={"entity_name": "Brian"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["result"], int)
    assert body["result"] > 0


def test_open_session_creates_entity_if_missing(client):
    client.post("/open_session", json={"entity_name": "NewPerson"})
    r = client.get("/profile/NewPerson")
    assert r.status_code == 200
    assert "NewPerson" in r.json()["result"]


def test_open_session_default_entity_type_is_person(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    db = mem.get_db()
    row = db.execute(
        "SELECT e.type FROM sessions s JOIN entities e ON e.id = s.entity_id WHERE s.id = ?",
        (sid,),
    ).fetchone()
    db.close()
    assert row["type"] == "person"


def test_open_session_custom_entity_type(client):
    r = client.post("/open_session", json={"entity_name": "kitchen", "entity_type": "room"})
    assert r.status_code == 200
    assert isinstance(r.json()["result"], int)


def test_open_session_multiple_sessions_per_entity(client):
    sid1 = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    sid2 = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    assert sid1 != sid2


# ── POST /log_turn ─────────────────────────────────────────────────────────────

def test_log_turn_user_role(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.post("/log_turn", json={
        "session_id": sid, "role": "user", "content": "I need groceries tomorrow",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_log_turn_assistant_role(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.post("/log_turn", json={
        "session_id": sid, "role": "assistant", "content": "I'll remind you tomorrow.",
    })
    assert r.status_code == 200


def test_log_turn_system_role(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.post("/log_turn", json={
        "session_id": sid, "role": "system", "content": "Room: kitchen.",
    })
    assert r.status_code == 200


def test_log_turn_invalid_role_rejected(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.post("/log_turn", json={
        "session_id": sid, "role": "unknown", "content": "some text",
    })
    assert r.status_code == 422


def test_log_turn_multiple_turns_stored(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    for i in range(3):
        client.post("/log_turn", json={"session_id": sid, "role": "user", "content": f"turn {i}"})

    db = mem.get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM session_turns WHERE session_id = ?", (sid,)
    ).fetchone()[0]
    db.close()
    assert count == 3


def test_log_turn_unknown_session_returns_200_with_error_string(client):
    """Tool-wrapping routes surface errors as result strings, not HTTP 4xx."""
    r = client.post("/log_turn", json={"session_id": 99999, "role": "user", "content": "hello"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "99999" in r.json()["result"]


# ── POST /close_session ────────────────────────────────────────────────────────

def test_close_session_without_summary(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.post("/close_session", json={"session_id": sid})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = mem.get_db()
    row = db.execute("SELECT ended_at FROM sessions WHERE id = ?", (sid,)).fetchone()
    db.close()
    assert row["ended_at"] is not None


def test_close_session_with_summary(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    client.post("/close_session", json={
        "session_id": sid, "summary": "Brian discussed grocery shopping.",
    })

    db = mem.get_db()
    row = db.execute("SELECT summary FROM sessions WHERE id = ?", (sid,)).fetchone()
    db.close()
    assert row["summary"] == "Brian discussed grocery shopping."


def test_close_session_sets_ended_at_within_test_window(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    before = time.time()
    client.post("/close_session", json={"session_id": sid})
    after = time.time()

    db = mem.get_db()
    row = db.execute("SELECT ended_at FROM sessions WHERE id = ?", (sid,)).fetchone()
    db.close()
    assert before <= row["ended_at"] <= after


def test_close_session_unknown_session_returns_200_with_error_string(client):
    r = client.post("/close_session", json={"session_id": 99999})
    assert r.status_code == 200
    assert "99999" in r.json()["result"]


# ── GET /get_session/{session_id} ─────────────────────────────────────────────

def test_get_session_open_session(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.get(f"/get_session/{sid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    result = r.json()["result"]
    assert "Brian" in result
    assert str(sid) in result


def test_get_session_includes_turns(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    client.post("/log_turn", json={"session_id": sid, "role": "user", "content": "hello there"})
    client.post("/log_turn", json={"session_id": sid, "role": "assistant", "content": "hi Brian"})

    r = client.get(f"/get_session/{sid}")
    result = r.json()["result"]
    assert "hello there" in result
    assert "hi Brian" in result
    assert "user" in result
    assert "assistant" in result


def test_get_session_includes_summary_after_close(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    client.post("/close_session", json={"session_id": sid, "summary": "Discussed meal planning."})

    r = client.get(f"/get_session/{sid}")
    assert "Discussed meal planning." in r.json()["result"]


def test_get_session_shows_open_status_before_close(client):
    sid = client.post("/open_session", json={"entity_name": "Brian"}).json()["result"]
    r = client.get(f"/get_session/{sid}")
    assert "open" in r.json()["result"]


def test_get_session_unknown_id_returns_200_with_error_string(client):
    r = client.get("/get_session/99999")
    assert r.status_code == 200
    assert "99999" in r.json()["result"]


# ── POST /extract_and_remember ─────────────────────────────────────────────────

def test_extract_and_remember_returns_ok(client):
    r = client.post("/extract_and_remember", json={
        "entity_name": "Brian",
        "text": "I prefer dark roast coffee and wake up at 6am.",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_extract_and_remember_stores_fact(client):
    """mock_llm returns one fact — verify it ends up in the memories table."""
    client.post("/extract_and_remember", json={
        "entity_name": "Brian",
        "text": "I prefer dark roast coffee.",
    })
    db = mem.get_db()
    row = db.execute(
        "SELECT fact FROM memories WHERE fact LIKE '%dark roast%'"
    ).fetchone()
    db.close()
    assert row is not None


def test_extract_and_remember_custom_model(client):
    r = client.post("/extract_and_remember", json={
        "entity_name": "Brian",
        "text": "some text",
        "model": "llama3.2",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_extract_and_remember_missing_entity_name_rejected(client):
    r = client.post("/extract_and_remember", json={"text": "some text"})
    assert r.status_code == 422


def test_extract_and_remember_missing_text_rejected(client):
    r = client.post("/extract_and_remember", json={"entity_name": "Brian"})
    assert r.status_code == 422
