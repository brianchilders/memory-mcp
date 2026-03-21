"""
Tests for Enhancement E: Session / episodic memory.

Sessions capture raw conversation turns so abilities can reconstruct
what was said and when — episodic memory distinct from semantic facts.

Tools:
  tool_open_session(entity_name)         → session_id
  tool_log_turn(session_id, role, text)  → confirmation
  tool_close_session(session_id, summary?) → confirmation
  tool_get_session(session_id)           → formatted transcript
"""

import time

import server as mem


# ── tool_open_session ─────────────────────────────────────────────────────────

async def test_open_session_returns_id():
    sid = await mem.tool_open_session("Brian")
    assert isinstance(sid, int)
    assert sid > 0


async def test_open_session_creates_db_row():
    sid = await mem.tool_open_session("Brian")
    db = mem.get_db()
    row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    db.close()
    assert row is not None
    assert row["ended_at"] is None


async def test_open_session_sets_started_at():
    before = time.time()
    sid = await mem.tool_open_session("Brian")
    after = time.time()
    db = mem.get_db()
    row = db.execute("SELECT started_at FROM sessions WHERE id=?", (sid,)).fetchone()
    db.close()
    assert before <= row["started_at"] <= after


async def test_open_session_creates_entity_implicitly():
    await mem.tool_open_session("NewPerson")
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='NewPerson'").fetchone()
    db.close()
    assert e is not None


async def test_multiple_sessions_for_same_entity():
    s1 = await mem.tool_open_session("Brian")
    s2 = await mem.tool_open_session("Brian")
    assert s1 != s2


# ── tool_log_turn ─────────────────────────────────────────────────────────────

async def test_log_turn_returns_confirmation():
    sid = await mem.tool_open_session("Brian")
    result = await mem.tool_log_turn(sid, "user", "Hello, how are you?")
    assert isinstance(result, str)
    assert len(result) > 0


async def test_log_turn_creates_db_row():
    sid = await mem.tool_open_session("Brian")
    await mem.tool_log_turn(sid, "user", "Hello there")
    db = mem.get_db()
    row = db.execute(
        "SELECT * FROM session_turns WHERE session_id=?", (sid,)
    ).fetchone()
    db.close()
    assert row is not None
    assert row["role"] == "user"
    assert row["content"] == "Hello there"


async def test_log_multiple_turns_ordered():
    sid = await mem.tool_open_session("Brian")
    await mem.tool_log_turn(sid, "user", "First message")
    await mem.tool_log_turn(sid, "assistant", "Response message")
    db = mem.get_db()
    rows = db.execute(
        "SELECT role, content FROM session_turns WHERE session_id=? ORDER BY ts",
        (sid,)
    ).fetchall()
    db.close()
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[1]["role"] == "assistant"


async def test_log_turn_unknown_session_returns_error():
    result = await mem.tool_log_turn(99999, "user", "Hello")
    assert "No session" in result or "not found" in result.lower()


# ── tool_close_session ────────────────────────────────────────────────────────

async def test_close_session_sets_ended_at():
    sid = await mem.tool_open_session("Brian")
    before = time.time()
    await mem.tool_close_session(sid)
    after = time.time()
    db = mem.get_db()
    row = db.execute("SELECT ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
    db.close()
    assert row["ended_at"] is not None
    assert before <= row["ended_at"] <= after


async def test_close_session_saves_summary():
    sid = await mem.tool_open_session("Brian")
    await mem.tool_close_session(sid, summary="Brian asked about the weather.")
    db = mem.get_db()
    row = db.execute("SELECT summary FROM sessions WHERE id=?", (sid,)).fetchone()
    db.close()
    assert row["summary"] == "Brian asked about the weather."


async def test_close_session_returns_confirmation():
    sid = await mem.tool_open_session("Brian")
    result = await mem.tool_close_session(sid)
    assert isinstance(result, str)
    assert len(result) > 0


async def test_close_unknown_session_returns_error():
    result = await mem.tool_close_session(99999)
    assert "No session" in result or "not found" in result.lower()


# ── tool_get_session ──────────────────────────────────────────────────────────

async def test_get_session_includes_entity_name():
    sid = await mem.tool_open_session("Brian")
    result = await mem.tool_get_session(sid)
    assert "Brian" in result


async def test_get_session_includes_turns():
    sid = await mem.tool_open_session("Brian")
    await mem.tool_log_turn(sid, "user", "What is my schedule?")
    await mem.tool_log_turn(sid, "assistant", "You have a meeting at 3pm.")
    result = await mem.tool_get_session(sid)
    assert "What is my schedule?" in result
    assert "You have a meeting" in result


async def test_get_session_includes_summary_when_closed():
    sid = await mem.tool_open_session("Brian")
    await mem.tool_close_session(sid, summary="Session about scheduling.")
    result = await mem.tool_get_session(sid)
    assert "Session about scheduling." in result


async def test_get_session_unknown_returns_error():
    result = await mem.tool_get_session(99999)
    assert "No session" in result or "not found" in result.lower()


async def test_get_session_empty_turns_no_crash():
    sid = await mem.tool_open_session("Brian")
    result = await mem.tool_get_session(sid)
    assert "Brian" in result
    assert isinstance(result, str)
