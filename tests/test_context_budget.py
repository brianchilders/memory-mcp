"""
tests/test_context_budget.py — Token-budget context assembly + episodic consolidation
                               + intention memory tests.

Covers:
  _est_tokens()              — character-based token estimator
  tool_get_context_budget    — fills budget greedily; keyword/hybrid/vector modes;
                               budget metadata in output; includes readings; truncation
  _consolidate_episodes()    — marks sessions consolidated; extracts facts (mocked LLM)
  tool_intend                — creates intention, links to entity, optional expiry
  tool_check_intentions      — FTS5 match, fires and increments count, expired skipped
  tool_dismiss_intention     — deactivates, double-dismiss, unknown id
  tool_list_intentions       — filter by entity, active_only
  HTTP endpoints             — /get_context_budget, /intend, /check_intentions,
                               /dismiss_intention, /intentions
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-budget-token-zz88"


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


# ── _est_tokens ────────────────────────────────────────────────────────────────

def test_est_tokens_basic():
    assert mem._est_tokens("hello") == 1      # 5 chars // 4 = 1
    assert mem._est_tokens("hello world") == 2  # 11 // 4 = 2
    assert mem._est_tokens("") == 1            # floor at 1
    assert mem._est_tokens("a" * 400) == 100


def test_est_tokens_never_zero():
    assert mem._est_tokens("x") >= 1


# ── tool_get_context_budget ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_budget_returns_header():
    await mem.tool_remember("Alice", "likes tea")
    result = await mem.tool_get_context_budget("Alice", "morning drink", token_budget=500)
    assert "Alice" in result


@pytest.mark.asyncio
async def test_context_budget_includes_budget_line():
    await mem.tool_remember("Bob", "uses Python")
    result = await mem.tool_get_context_budget("Bob", "programming", token_budget=500)
    assert "Budget:" in result
    assert "/500" in result


@pytest.mark.asyncio
async def test_context_budget_unknown_entity():
    result = await mem.tool_get_context_budget("Nobody99", "test", token_budget=500)
    assert "No entity named" in result


@pytest.mark.asyncio
async def test_context_budget_invalid_mode():
    await mem.tool_remember("Carol", "test fact")
    result = await mem.tool_get_context_budget("Carol", "test", recall_mode="badmode")
    assert "recall_mode must be one of" in result


@pytest.mark.asyncio
async def test_context_budget_keyword_mode_no_embed(monkeypatch):
    """keyword mode should not call embed() at recall time."""
    called = []
    original = mem.embed

    async def _fake(text):
        called.append(text)
        return await original(text)

    monkeypatch.setattr(mem, "embed", _fake)
    await mem.tool_remember("Dave", "drinks espresso daily")
    called.clear()
    await mem.tool_get_context_budget(
        "Dave", "coffee", recall_mode="keyword", token_budget=500
    )
    # embed should NOT have been called during the budget retrieval phase
    assert called == []


@pytest.mark.asyncio
async def test_context_budget_respects_budget():
    """With a very tight budget, only header + budget line should fit."""
    await mem.tool_remember("Eve", "knows a lot of things " * 20)
    result = await mem.tool_get_context_budget("Eve", "things", token_budget=10)
    # Even with a tiny budget, we should get the budget line
    assert "Budget:" in result


@pytest.mark.asyncio
async def test_context_budget_includes_readings():
    await mem.tool_remember("Frank", "lives at home")
    await mem.tool_record("Frank", "temperature", 72.0, unit="F")
    result = await mem.tool_get_context_budget(
        "Frank", "home", include_readings=True, token_budget=2000
    )
    assert "temperature" in result or "72" in result


@pytest.mark.asyncio
async def test_context_budget_excludes_readings_when_disabled():
    await mem.tool_remember("Grace", "lives at home")
    await mem.tool_record("Grace", "temperature", 72.0, unit="F")
    result = await mem.tool_get_context_budget(
        "Grace", "home", include_readings=False, token_budget=2000
    )
    assert "temperature" not in result


@pytest.mark.asyncio
async def test_context_budget_includes_relations():
    await mem.tool_remember("Hank", "is a person")
    await mem.tool_remember("Iris", "is another person")
    await mem.tool_relate("Hank", "Iris", "colleague")
    result = await mem.tool_get_context_budget("Hank", "work", token_budget=2000)
    assert "colleague" in result or "Iris" in result


@pytest.mark.asyncio
async def test_context_budget_truncated_label():
    """When budget runs out, output should say 'truncated'."""
    # Add many memories to exceed a small budget
    for i in range(10):
        await mem.tool_remember("Jack", f"fact number {i}: " + "x" * 100)
    result = await mem.tool_get_context_budget("Jack", "facts", token_budget=50)
    assert "truncated" in result


@pytest.mark.asyncio
async def test_context_budget_hybrid_mode():
    await mem.tool_remember("Kim", "practices yoga")
    result = await mem.tool_get_context_budget(
        "Kim", "yoga morning", recall_mode="hybrid", token_budget=500
    )
    assert "mode=hybrid" in result


# ── episodic consolidation ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consolidation_marks_sessions_consolidated(monkeypatch):
    """Processed sessions should have consolidated=1 after _consolidate_episodes()."""
    async def _fake_llm(prompt, model):
        return "[]"
    monkeypatch.setattr(mem, "_call_llm", _fake_llm)

    sid = await mem.tool_open_session("Leo")
    await mem.tool_log_turn(sid, "user", "Let's discuss memory systems")
    await mem.tool_log_turn(sid, "assistant", "Sure, what aspect?")
    await mem.tool_close_session(sid)

    count = await mem._consolidate_episodes()
    assert count >= 1

    db = mem.get_db()
    row = db.execute(
        "SELECT consolidated FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    db.close()
    assert row["consolidated"] == 1


@pytest.mark.asyncio
async def test_consolidation_skips_already_consolidated(monkeypatch):
    """Sessions with consolidated=1 should not be processed again."""
    async def _fake_llm(prompt, model):
        return "[]"
    monkeypatch.setattr(mem, "_call_llm", _fake_llm)

    sid = await mem.tool_open_session("Mia")
    await mem.tool_log_turn(sid, "user", "Hello")
    await mem.tool_close_session(sid)

    await mem._consolidate_episodes()  # first pass
    count = await mem._consolidate_episodes()  # second pass
    assert count == 0  # nothing new to process


@pytest.mark.asyncio
async def test_consolidation_skips_open_sessions(monkeypatch):
    """Open sessions (ended_at IS NULL) should never be consolidated."""
    async def _fake_llm(prompt, model):
        return "[]"
    monkeypatch.setattr(mem, "_call_llm", _fake_llm)

    sid = await mem.tool_open_session("Ned")
    await mem.tool_log_turn(sid, "user", "Still open session")
    # Do NOT close the session

    count = await mem._consolidate_episodes()
    assert count == 0


@pytest.mark.asyncio
async def test_consolidation_stores_extracted_facts(monkeypatch):
    """LLM-returned facts should be stored as memories at TRUST_INFERRED."""
    async def _fake_llm(prompt, model):
        return json.dumps([
            {"fact": "Olivia prefers morning meetings", "category": "preference"},
            {"fact": "Olivia works in the engineering team", "category": "general"},
        ])
    monkeypatch.setattr(mem, "_call_llm", _fake_llm)

    sid = await mem.tool_open_session("Olivia")
    await mem.tool_log_turn(sid, "user", "I like morning standups in engineering")
    await mem.tool_close_session(sid)

    await mem._consolidate_episodes()

    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Olivia'").fetchone()
    rows = db.execute(
        "SELECT fact, source_trust, source FROM memories WHERE entity_id=?",
        (e["id"],),
    ).fetchall()
    db.close()

    consolidated = [r for r in rows if r["source"] == "episode_consolidation"]
    assert len(consolidated) == 2
    assert all(r["source_trust"] == mem.TRUST_DEFAULT_EXTRACT for r in consolidated)


@pytest.mark.asyncio
async def test_consolidation_handles_llm_error(monkeypatch):
    """LLM errors should be handled gracefully — session still marked consolidated."""
    async def _bad_llm(prompt, model):
        raise RuntimeError("LLM unavailable")
    monkeypatch.setattr(mem, "_call_llm", _bad_llm)

    sid = await mem.tool_open_session("Pete")
    await mem.tool_log_turn(sid, "user", "test turn")
    await mem.tool_close_session(sid)

    count = await mem._consolidate_episodes()
    assert count >= 1

    db = mem.get_db()
    row = db.execute("SELECT consolidated FROM sessions WHERE id=?", (sid,)).fetchone()
    db.close()
    assert row["consolidated"] == 1


# ── intention memory ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_intend_creates_intention():
    result = await mem.tool_intend(
        "Quinn",
        trigger_text="Quinn mentions being tired",
        action_text="suggest a 10-minute break",
    )
    assert "id=" in result
    assert "Quinn" in result


@pytest.mark.asyncio
async def test_intend_with_expiry():
    future_ts = time.time() + 86400
    result = await mem.tool_intend(
        "Rita",
        trigger_text="Rita asks about budget",
        action_text="show financial dashboard",
        expires_ts=future_ts,
    )
    assert "expires" in result


@pytest.mark.asyncio
async def test_intend_stores_in_db():
    await mem.tool_intend(
        "Sam",
        trigger_text="Sam mentions back pain",
        action_text="recommend ergonomic adjustments",
    )
    db = mem.get_db()
    row = db.execute(
        "SELECT trigger_text, action_text, active FROM intentions i"
        " JOIN entities e ON e.id=i.entity_id WHERE e.name='Sam'"
    ).fetchone()
    db.close()
    assert row is not None
    assert row["active"] == 1
    assert "back pain" in row["trigger_text"]


@pytest.mark.asyncio
async def test_check_intentions_matches_keyword():
    await mem.tool_intend("Tina", "Tina is tired", "suggest rest break")
    result = await mem.tool_check_intentions("Tina", "I'm really tired today")
    assert "suggest rest break" in result or "Tina" in result


@pytest.mark.asyncio
async def test_check_intentions_increments_fired_count():
    await mem.tool_intend("Uma", "Uma mentions headache", "offer painkiller reminder")
    await mem.tool_check_intentions("Uma", "I have a headache")
    await mem.tool_check_intentions("Uma", "headache again")

    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Uma'").fetchone()
    row = db.execute(
        "SELECT fired_count FROM intentions WHERE entity_id=?", (e["id"],)
    ).fetchone()
    db.close()
    assert row["fired_count"] >= 2


@pytest.mark.asyncio
async def test_check_intentions_no_match():
    await mem.tool_intend("Victor", "Victor mentions coffee", "offer alternative")
    result = await mem.tool_check_intentions("Victor", "completely unrelated topic xyz")
    assert "No intentions triggered" in result


@pytest.mark.asyncio
async def test_check_intentions_skips_expired():
    past_ts = time.time() - 1
    await mem.tool_intend("Wendy", "Wendy mentions weather", "check forecast",
                          expires_ts=past_ts)
    result = await mem.tool_check_intentions("Wendy", "What's the weather like?")
    assert "No intentions triggered" in result


@pytest.mark.asyncio
async def test_check_intentions_skips_dismissed():
    await mem.tool_intend("Xena", "Xena asks about reports", "open reports dashboard")
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Xena'").fetchone()
    row = db.execute("SELECT id FROM intentions WHERE entity_id=?", (e["id"],)).fetchone()
    db.execute("UPDATE intentions SET active=0 WHERE id=?", (row["id"],))
    db.commit()
    db.close()

    result = await mem.tool_check_intentions("Xena", "Can you show me the reports?")
    assert "No intentions triggered" in result


@pytest.mark.asyncio
async def test_check_intentions_unknown_entity():
    result = await mem.tool_check_intentions("GhostEntity99", "some text")
    assert "No entity named" in result


@pytest.mark.asyncio
async def test_dismiss_intention_deactivates():
    await mem.tool_intend("Yara", "Yara mentions sleep", "suggest sleep hygiene")
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Yara'").fetchone()
    row = db.execute("SELECT id FROM intentions WHERE entity_id=?", (e["id"],)).fetchone()
    iid = row["id"]
    db.close()

    result = await mem.tool_dismiss_intention(iid)
    assert "dismissed" in result.lower()

    db = mem.get_db()
    row = db.execute("SELECT active FROM intentions WHERE id=?", (iid,)).fetchone()
    db.close()
    assert row["active"] == 0


@pytest.mark.asyncio
async def test_dismiss_intention_already_dismissed():
    await mem.tool_intend("Zara", "Zara asks about schedule", "show calendar")
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Zara'").fetchone()
    row = db.execute("SELECT id FROM intentions WHERE entity_id=?", (e["id"],)).fetchone()
    iid = row["id"]
    db.close()

    await mem.tool_dismiss_intention(iid)
    result = await mem.tool_dismiss_intention(iid)
    assert "already dismissed" in result.lower()


@pytest.mark.asyncio
async def test_dismiss_intention_unknown():
    result = await mem.tool_dismiss_intention(99999)
    assert "No intention" in result


@pytest.mark.asyncio
async def test_list_intentions_active_only():
    await mem.tool_intend("Aaron", "Aaron mentions projects", "list open tasks")
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Aaron'").fetchone()
    row = db.execute("SELECT id FROM intentions WHERE entity_id=?", (e["id"],)).fetchone()
    iid = row["id"]
    db.close()

    await mem.tool_dismiss_intention(iid)
    # Dismissed one; add an active one
    await mem.tool_intend("Aaron", "Aaron asks for help", "offer assistance")

    result_active = await mem.tool_list_intentions(entity_name="Aaron", active_only=True)
    result_all    = await mem.tool_list_intentions(entity_name="Aaron", active_only=False)

    assert "ask" in result_active.lower()
    assert "project" not in result_active.lower()  # dismissed one hidden
    assert "project" in result_all.lower()  # dismissed one shown


@pytest.mark.asyncio
async def test_list_intentions_no_entity_filter():
    await mem.tool_intend("Beth", "Beth mentions Python", "show Python docs")
    await mem.tool_intend("Craig", "Craig mentions Java", "show Java docs")
    result = await mem.tool_list_intentions(active_only=True)
    assert "Beth" in result
    assert "Craig" in result


@pytest.mark.asyncio
async def test_list_intentions_unknown_entity():
    result = await mem.tool_list_intentions(entity_name="NoSuchPerson99")
    assert "No entity named" in result


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

def test_http_get_context_budget(client):
    client.post("/remember", json={"entity_name": "HTTP_Alice", "fact": "likes tea"})
    r = client.post("/get_context_budget", json={
        "entity_name": "HTTP_Alice",
        "context_query": "morning routine",
        "token_budget": 500,
    })
    assert r.status_code == 200
    assert "Budget:" in r.json()["result"]


def test_http_get_context_budget_keyword_mode(client):
    client.post("/remember", json={"entity_name": "HTTP_Bob", "fact": "drinks coffee"})
    r = client.post("/get_context_budget", json={
        "entity_name": "HTTP_Bob",
        "context_query": "coffee drinks",
        "token_budget": 500,
        "recall_mode": "keyword",
    })
    assert r.status_code == 200


def test_http_get_context_budget_negative_budget_rejected(client):
    r = client.post("/get_context_budget", json={
        "entity_name": "X",
        "context_query": "y",
        "token_budget": -1,
    })
    assert r.status_code == 422


def test_http_intend(client):
    client.post("/remember", json={"entity_name": "HTTP_C", "fact": "exists"})
    r = client.post("/intend", json={
        "entity_name": "HTTP_C",
        "trigger_text": "C mentions deadlines",
        "action_text": "show project timeline",
    })
    assert r.status_code == 200
    assert "id=" in r.json()["result"]


def test_http_check_intentions(client):
    client.post("/remember", json={"entity_name": "HTTP_D", "fact": "exists"})
    client.post("/intend", json={
        "entity_name": "HTTP_D",
        "trigger_text": "D feels stressed",
        "action_text": "suggest breathing exercise",
    })
    r = client.post("/check_intentions", json={
        "entity_name": "HTTP_D",
        "text": "I am feeling really stressed today",
    })
    assert r.status_code == 200


def test_http_dismiss_intention(client):
    client.post("/remember", json={"entity_name": "HTTP_E", "fact": "exists"})
    r_intend = client.post("/intend", json={
        "entity_name": "HTTP_E",
        "trigger_text": "E mentions budget",
        "action_text": "show financials",
    })
    result_text = r_intend.json()["result"]
    iid = int(result_text.split("id=")[1].split(" ")[0].rstrip("."))
    r = client.post("/dismiss_intention", json={"intention_id": iid})
    assert r.status_code == 200
    assert "dismissed" in r.json()["result"].lower()


def test_http_list_intentions(client):
    client.post("/remember", json={"entity_name": "HTTP_F", "fact": "exists"})
    client.post("/intend", json={
        "entity_name": "HTTP_F",
        "trigger_text": "F asks about status",
        "action_text": "give status report",
    })
    r = client.get("/intentions?entity_name=HTTP_F")
    assert r.status_code == 200
    assert "HTTP_F" in r.json()["result"]


def test_http_intentions_requires_auth():
    import api
    with TestClient(api.app) as c:
        r = c.get("/intentions")
    assert r.status_code == 401
