"""
tests/test_working_memory.py — Working memory (Tier 1.75) tests.

Covers:
  wm_open   — creates task, optional entity association, optional TTL
  wm_set    — sets slot, overwrites, rejects closed tasks
  wm_get    — reads one slot or all slots with metadata
  wm_list   — filters by status and entity
  wm_close  — closes task, blocks further writes
  promote   — close with promote=True bundles slots into long-term memory
  TTL expiry — _expire_working_memory() marks timed-out tasks as 'expired'
  HTTP      — POST /wm/open, /wm/set, /wm/get, /wm/close; GET /wm/list, /wm/{id}
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-wm-token-abc99"


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


# ── wm_open ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wm_open_basic():
    result = await mem.tool_wm_open("test task")
    assert "id=" in result
    task_id = int(result.split("id=")[1].split(".")[0].split(",")[0])
    assert task_id > 0


@pytest.mark.asyncio
async def test_wm_open_with_entity():
    await mem.tool_remember("Alice", "likes tea")
    result = await mem.tool_wm_open("greet alice", entity_name="Alice")
    assert "id=" in result
    # Verify DB linkage
    task_id = int(result.split("id=")[1].split(".")[0].split(",")[0])
    db = mem.get_db()
    row = db.execute(
        "SELECT entity_id FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    assert row is not None
    assert row["entity_id"] is not None


@pytest.mark.asyncio
async def test_wm_open_with_unknown_entity_does_not_fail():
    # Missing entity just stores entity_id=NULL, no error
    result = await mem.tool_wm_open("orphan task", entity_name="NoSuchEntity")
    assert "id=" in result


@pytest.mark.asyncio
async def test_wm_open_with_ttl():
    result = await mem.tool_wm_open("ttl task", ttl_seconds=3600)
    assert "expires in 3600s" in result


@pytest.mark.asyncio
async def test_wm_open_status_is_open():
    result = await mem.tool_wm_open("check status")
    task_id = int(result.split("id=")[1].split(".")[0].split(",")[0])
    db = mem.get_db()
    row = db.execute(
        "SELECT status FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    assert row["status"] == "open"


# ── wm_set ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wm_set_stores_slot():
    r = await mem.tool_wm_open("task A")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    result = await mem.tool_wm_set(task_id, "step", "fetch data")
    assert "Set working memory" in result
    db = mem.get_db()
    row = db.execute(
        "SELECT value FROM working_memory_slots WHERE task_id=? AND key=?",
        (task_id, "step"),
    ).fetchone()
    db.close()
    assert json.loads(row["value"]) == "fetch data"


@pytest.mark.asyncio
async def test_wm_set_overwrites_existing():
    r = await mem.tool_wm_open("task B")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "count", 1)
    await mem.tool_wm_set(task_id, "count", 2)
    db = mem.get_db()
    row = db.execute(
        "SELECT value FROM working_memory_slots WHERE task_id=? AND key=?",
        (task_id, "count"),
    ).fetchone()
    db.close()
    # Only one row (UNIQUE constraint), value is 2
    assert json.loads(row["value"]) == 2


@pytest.mark.asyncio
async def test_wm_set_stores_dict_value():
    r = await mem.tool_wm_open("task C")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    payload = {"key1": "val1", "num": 42, "flag": True}
    await mem.tool_wm_set(task_id, "data", payload)
    db = mem.get_db()
    row = db.execute(
        "SELECT value FROM working_memory_slots WHERE task_id=? AND key=?",
        (task_id, "data"),
    ).fetchone()
    db.close()
    assert json.loads(row["value"]) == payload


@pytest.mark.asyncio
async def test_wm_set_rejects_unknown_task():
    result = await mem.tool_wm_set(99999, "k", "v")
    assert "No working memory task" in result


@pytest.mark.asyncio
async def test_wm_set_rejects_closed_task():
    r = await mem.tool_wm_open("task D")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_close(task_id)
    result = await mem.tool_wm_set(task_id, "k", "v")
    assert "closed" in result.lower()


# ── wm_get ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wm_get_single_slot():
    r = await mem.tool_wm_open("task E")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "answer", 42)
    result = await mem.tool_wm_get(task_id, key="answer")
    data = json.loads(result)
    assert data == 42


@pytest.mark.asyncio
async def test_wm_get_all_slots():
    r = await mem.tool_wm_open("task F")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "a", 1)
    await mem.tool_wm_set(task_id, "b", "hello")
    result = await mem.tool_wm_get(task_id)
    data = json.loads(result)
    assert data["task_id"] == task_id
    assert data["slots"]["a"] == 1
    assert data["slots"]["b"] == "hello"
    assert data["status"] == "open"


@pytest.mark.asyncio
async def test_wm_get_missing_slot():
    r = await mem.tool_wm_open("task G")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    result = await mem.tool_wm_get(task_id, key="nonexistent")
    assert "No slot" in result


@pytest.mark.asyncio
async def test_wm_get_unknown_task():
    result = await mem.tool_wm_get(99999)
    assert "No working memory task" in result


@pytest.mark.asyncio
async def test_wm_get_includes_entity_name():
    await mem.tool_remember("Bob", "test entity")
    r = await mem.tool_wm_open("linked task", entity_name="Bob")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    result = await mem.tool_wm_get(task_id)
    data = json.loads(result)
    assert data["entity"] == "Bob"


# ── wm_list ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wm_list_shows_open_tasks():
    r = await mem.tool_wm_open("visible task")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    result = await mem.tool_wm_list(status="open")
    assert "visible task" in result
    assert str(task_id) in result


@pytest.mark.asyncio
async def test_wm_list_default_status_is_open():
    await mem.tool_wm_open("open one")
    result = await mem.tool_wm_list()
    assert "open one" in result


@pytest.mark.asyncio
async def test_wm_list_closed_not_in_open():
    r = await mem.tool_wm_open("to close")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_close(task_id)
    result = await mem.tool_wm_list(status="open")
    assert "to close" not in result


@pytest.mark.asyncio
async def test_wm_list_all_includes_closed():
    r = await mem.tool_wm_open("also closing")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_close(task_id)
    result = await mem.tool_wm_list(status="all")
    assert "also closing" in result


@pytest.mark.asyncio
async def test_wm_list_filter_by_entity():
    await mem.tool_remember("Carol", "test entity")
    r = await mem.tool_wm_open("carol task", entity_name="Carol")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    r2 = await mem.tool_wm_open("unrelated task")

    result_carol = await mem.tool_wm_list(entity_name="Carol")
    result_all   = await mem.tool_wm_list(status="all")

    assert "carol task" in result_carol
    assert "unrelated task" not in result_carol
    assert "unrelated task" in result_all


@pytest.mark.asyncio
async def test_wm_list_shows_slot_count():
    r = await mem.tool_wm_open("counting task")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "x", 1)
    await mem.tool_wm_set(task_id, "y", 2)
    result = await mem.tool_wm_list()
    assert "slots=2" in result


@pytest.mark.asyncio
async def test_wm_list_unknown_entity_returns_error():
    result = await mem.tool_wm_list(entity_name="GhostEntity")
    assert "No entity named" in result


@pytest.mark.asyncio
async def test_wm_list_invalid_status_returns_error():
    result = await mem.tool_wm_list(status="bogus")
    assert "status must be one of" in result


# ── wm_close ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wm_close_marks_task_closed():
    r = await mem.tool_wm_open("close me")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    result = await mem.tool_wm_close(task_id)
    assert f"Task {task_id} closed" in result
    db = mem.get_db()
    row = db.execute(
        "SELECT status, closed_at FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


@pytest.mark.asyncio
async def test_wm_close_already_closed():
    r = await mem.tool_wm_open("close twice")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_close(task_id)
    result = await mem.tool_wm_close(task_id)
    assert "already closed" in result.lower()


@pytest.mark.asyncio
async def test_wm_close_unknown_task():
    result = await mem.tool_wm_close(99999)
    assert "No working memory task" in result


@pytest.mark.asyncio
async def test_wm_close_blocks_writes():
    r = await mem.tool_wm_open("write block task")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_close(task_id)
    result = await mem.tool_wm_set(task_id, "k", "v")
    assert "closed" in result.lower()


# ── promote on close ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wm_close_promote_creates_memory():
    await mem.tool_remember("Dave", "is a test person")
    r = await mem.tool_wm_open("meeting", entity_name="Dave")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "agenda", "discuss budget")
    await mem.tool_wm_set(task_id, "outcome", "approved")

    result = await mem.tool_wm_close(task_id, promote=True)
    assert "Promoted" in result
    assert "2 slot(s)" in result

    # Verify the memory was written to the DB for Dave
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Dave'").fetchone()
    facts = db.execute(
        "SELECT fact, source FROM memories WHERE entity_id=? AND source='working_memory'",
        (e["id"],),
    ).fetchall()
    db.close()
    assert len(facts) >= 1
    assert any("meeting" in f["fact"] for f in facts)


@pytest.mark.asyncio
async def test_wm_close_promote_no_entity_skips():
    r = await mem.tool_wm_open("orphan promote task")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "data", "value")
    result = await mem.tool_wm_close(task_id, promote=True)
    assert "Task" in result and "closed" in result
    assert "skipped" in result.lower()


@pytest.mark.asyncio
async def test_wm_close_promote_no_slots():
    await mem.tool_remember("Eve", "is a test person")
    r = await mem.tool_wm_open("empty task", entity_name="Eve")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    result = await mem.tool_wm_close(task_id, promote=True)
    assert "No slots to promote" in result


@pytest.mark.asyncio
async def test_wm_close_promote_uses_inferred_trust():
    await mem.tool_remember("Frank", "is a test person")
    r = await mem.tool_wm_open("trust task", entity_name="Frank")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem.tool_wm_set(task_id, "note", "remembered thing")
    await mem.tool_wm_close(task_id, promote=True)

    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Frank'").fetchone()
    row = db.execute(
        "SELECT source_trust FROM memories WHERE entity_id=? AND source='working_memory'",
        (e["id"],),
    ).fetchone()
    db.close()
    assert row is not None
    assert row["source_trust"] == mem.TRUST_INFERRED


# ── TTL expiry ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_expire_working_memory_marks_expired():
    # Create a task with a TTL in the past
    db = mem.get_db()
    now = time.time()
    cur = db.execute(
        "INSERT INTO working_memory_tasks(name, status, ttl_ts, created)"
        " VALUES (?,?,?,?)",
        ("expired task", "open", now - 1, now - 10),
    )
    task_id = cur.lastrowid
    db.commit()
    db.close()

    count = await mem._expire_working_memory()
    assert count >= 1

    db = mem.get_db()
    row = db.execute(
        "SELECT status FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    assert row["status"] == "expired"


@pytest.mark.asyncio
async def test_expire_working_memory_ignores_future_ttl():
    r = await mem.tool_wm_open("future task", ttl_seconds=9999)
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    count = await mem._expire_working_memory()
    # This task should NOT be expired
    db = mem.get_db()
    row = db.execute(
        "SELECT status FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_expire_working_memory_ignores_no_ttl():
    r = await mem.tool_wm_open("no ttl task")
    task_id = int(r.split("id=")[1].split(".")[0].split(",")[0])
    await mem._expire_working_memory()
    db = mem.get_db()
    row = db.execute(
        "SELECT status FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_expire_working_memory_ignores_already_closed():
    db = mem.get_db()
    now = time.time()
    cur = db.execute(
        "INSERT INTO working_memory_tasks(name, status, ttl_ts, created, closed_at)"
        " VALUES (?,?,?,?,?)",
        ("already closed", "closed", now - 1, now - 10, now - 5),
    )
    task_id = cur.lastrowid
    db.commit()
    db.close()

    await mem._expire_working_memory()

    db = mem.get_db()
    row = db.execute(
        "SELECT status FROM working_memory_tasks WHERE id=?", (task_id,)
    ).fetchone()
    db.close()
    # Should still be 'closed', not changed to 'expired'
    assert row["status"] == "closed"


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

def test_http_wm_open(client):
    r = client.post("/wm/open", json={"task_name": "http task"})
    assert r.status_code == 200
    assert "id=" in r.json()["result"]


def test_http_wm_open_with_entity(client):
    # Create entity first
    client.post("/remember", json={"entity_name": "HTTP_User", "fact": "exists"})
    r = client.post("/wm/open", json={"task_name": "user task", "entity_name": "HTTP_User"})
    assert r.status_code == 200


def test_http_wm_open_negative_ttl_rejected(client):
    r = client.post("/wm/open", json={"task_name": "bad ttl", "ttl_seconds": -1})
    assert r.status_code == 422


def test_http_wm_set(client):
    r = client.post("/wm/open", json={"task_name": "set task"})
    result = r.json()["result"]
    task_id = int(result.split("id=")[1].split(".")[0].split(",")[0])
    r2 = client.post("/wm/set", json={"task_id": task_id, "key": "progress", "value": 50})
    assert r2.status_code == 200


def test_http_wm_get_single(client):
    r = client.post("/wm/open", json={"task_name": "get task"})
    task_id = int(r.json()["result"].split("id=")[1].split(".")[0].split(",")[0])
    client.post("/wm/set", json={"task_id": task_id, "key": "x", "value": 99})
    r2 = client.post("/wm/get", json={"task_id": task_id, "key": "x"})
    assert r2.status_code == 200
    data = json.loads(r2.json()["result"])
    assert data == 99


def test_http_wm_get_all(client):
    r = client.post("/wm/open", json={"task_name": "all slots task"})
    task_id = int(r.json()["result"].split("id=")[1].split(".")[0].split(",")[0])
    client.post("/wm/set", json={"task_id": task_id, "key": "a", "value": 1})
    client.post("/wm/set", json={"task_id": task_id, "key": "b", "value": 2})
    r2 = client.post("/wm/get", json={"task_id": task_id})
    assert r2.status_code == 200
    data = json.loads(r2.json()["result"])
    assert data["slots"] == {"a": 1, "b": 2}


def test_http_wm_get_by_path(client):
    r = client.post("/wm/open", json={"task_name": "path task"})
    task_id = int(r.json()["result"].split("id=")[1].split(".")[0].split(",")[0])
    client.post("/wm/set", json={"task_id": task_id, "key": "y", "value": "hello"})
    r2 = client.get(f"/wm/{task_id}")
    assert r2.status_code == 200
    data = json.loads(r2.json()["result"])
    assert data["slots"]["y"] == "hello"


def test_http_wm_list(client):
    client.post("/wm/open", json={"task_name": "list http task"})
    r = client.get("/wm/list?status=open")
    assert r.status_code == 200
    assert "list http task" in r.json()["result"]


def test_http_wm_close(client):
    r = client.post("/wm/open", json={"task_name": "close http task"})
    task_id = int(r.json()["result"].split("id=")[1].split(".")[0].split(",")[0])
    r2 = client.post("/wm/close", json={"task_id": task_id})
    assert r2.status_code == 200
    assert f"Task {task_id} closed" in r2.json()["result"]


def test_http_wm_close_and_promote(client):
    client.post("/remember", json={"entity_name": "Gina", "fact": "exists"})
    r = client.post("/wm/open", json={"task_name": "promote http", "entity_name": "Gina"})
    task_id = int(r.json()["result"].split("id=")[1].split(".")[0].split(",")[0])
    client.post("/wm/set", json={"task_id": task_id, "key": "note", "value": "important"})
    r2 = client.post("/wm/close", json={"task_id": task_id, "promote": True})
    assert r2.status_code == 200
    assert "Promoted" in r2.json()["result"]


def test_http_wm_requires_auth():
    import api
    with TestClient(api.app) as c:
        r = c.post("/wm/open", json={"task_name": "unauth"})
    assert r.status_code == 401


# ── MCP tool dispatch ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_tool_dispatch_wm_open():
    from mcp.types import TextContent
    # Call via the MCP dispatch
    import server as mem
    # The call_tool handler isn't directly callable as a function here,
    # but we can verify tool names are in TOOLS
    tool_names = {t.name for t in mem.TOOLS}
    for name in ("wm_open", "wm_set", "wm_get", "wm_list", "wm_close"):
        assert name in tool_names, f"Tool '{name}' missing from TOOLS"


@pytest.mark.asyncio
async def test_mcp_tool_count_includes_wm():
    # 20 pre-existing tools + 5 new wm tools = 25
    assert len(mem.TOOLS) >= 25
