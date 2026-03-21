"""
Unit tests for all 10 MCP tool functions in server.py.

The isolated_db and mock_embed fixtures from conftest.py are autouse,
so every test runs against a fresh in-memory DB with no Ollama dependency.
"""

import json
import time

import server as mem


# ── remember ──────────────────────────────────────────────────────────────────

async def test_remember_returns_confirmation():
    result = await mem.tool_remember("Brian", "Likes dark chocolate")
    assert "Brian" in result
    assert "Likes dark chocolate" in result


async def test_remember_creates_entity_and_memory():
    await mem.tool_remember("Brian", "Likes coffee", entity_type="person")
    db = mem.get_db()
    entity = db.execute("SELECT * FROM entities WHERE name='Brian'").fetchone()
    assert entity is not None
    assert entity["type"] == "person"
    memories = db.execute(
        "SELECT * FROM memories WHERE entity_id=?", (entity["id"],)
    ).fetchall()
    assert len(memories) == 1
    assert memories[0]["fact"] == "Likes coffee"
    db.close()


async def test_remember_inserts_vector():
    await mem.tool_remember("Brian", "Likes tea")
    db = mem.get_db()
    m = db.execute("SELECT id FROM memories").fetchone()
    v = db.execute(
        "SELECT rowid FROM memory_vectors WHERE rowid=?", (m["id"],)
    ).fetchone()
    assert v is not None
    db.close()


async def test_remember_entity_meta_merged():
    await mem.tool_remember("Alice", "Likes hiking", meta={"age": 30})
    await mem.tool_remember("Alice", "Likes reading", meta={"city": "Portland"})
    db = mem.get_db()
    row = db.execute("SELECT meta FROM entities WHERE name='Alice'").fetchone()
    meta = json.loads(row["meta"])
    assert meta["age"] == 30
    assert meta["city"] == "Portland"
    db.close()


async def test_remember_category_and_confidence():
    await mem.tool_remember(
        "Brian", "Prefers 68F", category="preference", confidence=0.9
    )
    db = mem.get_db()
    m = db.execute("SELECT category, confidence FROM memories").fetchone()
    assert m["category"] == "preference"
    assert abs(m["confidence"] - 0.9) < 0.001
    db.close()


async def test_remember_multiple_facts_same_entity():
    await mem.tool_remember("Brian", "Likes coffee")
    await mem.tool_remember("Brian", "Dislikes loud noise")
    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    entity_count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 2
    assert entity_count == 1  # same entity, not duplicated
    db.close()


# ── recall ─────────────────────────────────────────────────────────────────────

async def test_recall_empty_db():
    result = await mem.tool_recall("anything")
    assert "No relevant" in result


async def test_recall_finds_stored_memory():
    await mem.tool_remember("Brian", "Loves jazz music")
    result = await mem.tool_recall("music preference", entity_name="Brian")
    assert "Loves jazz music" in result


async def test_recall_filters_by_entity():
    await mem.tool_remember("Brian", "Likes coffee")
    await mem.tool_remember("Sarah", "Likes tea")
    result = await mem.tool_recall("beverage", entity_name="Brian")
    assert "Sarah" not in result


async def test_recall_filters_by_category():
    await mem.tool_remember("Brian", "Prefers 68F", category="preference")
    await mem.tool_remember("Brian", "Wakes at 6am", category="routine")
    result = await mem.tool_recall("temperature", entity_name="Brian", category="preference")
    # Only preference-category memories are searched
    assert "Prefers 68F" in result


async def test_recall_respects_top_k():
    for i in range(10):
        await mem.tool_remember("Brian", f"Fact number {i}")
    result = await mem.tool_recall("fact", top_k=3)
    assert "Top 3" in result


async def test_recall_shows_entity_and_category():
    await mem.tool_remember("Brian", "Likes hiking", category="habit")
    result = await mem.tool_recall("outdoor activity")
    assert "Brian" in result
    assert "habit" in result


# ── get_profile ─────────────────────────────────────────────────────────────────

async def test_get_profile_unknown_entity():
    result = await mem.tool_get_profile("Nobody")
    assert "No entity" in result


async def test_get_profile_shows_memories():
    await mem.tool_remember("Brian", "Likes coffee", category="preference")
    result = await mem.tool_get_profile("Brian")
    assert "Likes coffee" in result
    assert "PREFERENCE" in result


async def test_get_profile_shows_entity_type():
    await mem.tool_remember("living_room", "Has hardwood floors", entity_type="room")
    result = await mem.tool_get_profile("living_room")
    assert "room" in result


async def test_get_profile_shows_meta():
    await mem.tool_remember("Brian", "Some fact", meta={"age": 35, "role": "engineer"})
    result = await mem.tool_get_profile("Brian")
    assert "age" in result
    assert "35" in result


async def test_get_profile_shows_relationships():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    result = await mem.tool_get_profile("Brian")
    assert "Sarah" in result
    assert "spouse" in result


async def test_get_profile_shows_latest_readings():
    await mem.tool_record("Brian", "temperature", 72.0, unit="F")
    result = await mem.tool_get_profile("Brian")
    assert "temperature" in result
    assert "72" in result


async def test_get_profile_shows_upcoming_schedule():
    future_ts = time.time() + 3600
    await mem.tool_schedule("Brian", "Team standup", start_ts=future_ts)
    result = await mem.tool_get_profile("Brian")
    assert "Team standup" in result


async def test_get_profile_past_schedule_hidden():
    past_ts = time.time() - 3600
    await mem.tool_schedule("Brian", "Old meeting", start_ts=past_ts)
    result = await mem.tool_get_profile("Brian")
    # Past events should not appear in upcoming schedule
    assert "Old meeting" not in result


# ── relate ─────────────────────────────────────────────────────────────────────

async def test_relate_returns_confirmation():
    result = await mem.tool_relate("Brian", "Sarah", "spouse")
    assert "Brian" in result
    assert "Sarah" in result
    assert "spouse" in result


async def test_relate_creates_db_row():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    db = mem.get_db()
    rel = db.execute("SELECT * FROM relations").fetchone()
    assert rel is not None
    assert rel["rel_type"] == "spouse"
    db.close()


async def test_relate_is_idempotent():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    await mem.tool_relate("Brian", "Sarah", "spouse")
    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    assert count == 1
    db.close()


async def test_relate_creates_both_entities():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 2
    db.close()


async def test_relate_reverse_shows_in_profile():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    result_sarah = await mem.tool_get_profile("Sarah")
    assert "spouse_of" in result_sarah


# ── forget ─────────────────────────────────────────────────────────────────────

async def test_forget_specific_memory():
    await mem.tool_remember("Brian", "Old fact")
    db = mem.get_db()
    mid = db.execute("SELECT id FROM memories").fetchone()["id"]
    db.close()

    result = await mem.tool_forget("Brian", memory_id=mid)
    assert f"#{mid}" in result

    db = mem.get_db()
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0
    db.close()


async def test_forget_entity_cascades_memories():
    await mem.tool_remember("Brian", "Some fact")
    await mem.tool_forget("Brian")
    db = mem.get_db()
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0
    db.close()


async def test_forget_entity_cascades_readings():
    await mem.tool_record("Brian", "temperature", 72.0)
    await mem.tool_forget("Brian")
    db = mem.get_db()
    assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    db.close()


async def test_forget_entity_removes_entity_row():
    await mem.tool_remember("Brian", "Some fact")
    await mem.tool_forget("Brian")
    db = mem.get_db()
    assert db.execute("SELECT COUNT(*) FROM entities WHERE name='Brian'").fetchone()[0] == 0
    db.close()


async def test_forget_unknown_entity():
    result = await mem.tool_forget("Nobody")
    assert "No entity" in result


# ── record ─────────────────────────────────────────────────────────────────────

async def test_record_numeric():
    result = await mem.tool_record(
        "living_room", "temperature", 71.4, unit="F", entity_type="room"
    )
    assert "living_room" in result
    assert "temperature" in result
    db = mem.get_db()
    r = db.execute("SELECT * FROM readings").fetchone()
    assert r["value_type"] == "numeric"
    assert abs(r["value_num"] - 71.4) < 0.01
    assert r["unit"] == "F"
    db.close()


async def test_record_categorical():
    await mem.tool_record("Brian", "presence", "home")
    db = mem.get_db()
    r = db.execute("SELECT * FROM readings").fetchone()
    assert r["value_type"] == "categorical"
    assert r["value_cat"] == "home"
    db.close()


async def test_record_composite():
    await mem.tool_record("Brian", "mood", {"mood": "calm", "confidence": 0.91})
    db = mem.get_db()
    r = db.execute("SELECT * FROM readings").fetchone()
    assert r["value_type"] == "composite"
    data = json.loads(r["value_json"])
    assert data["mood"] == "calm"
    assert abs(data["confidence"] - 0.91) < 0.001
    db.close()


async def test_record_integer_treated_as_numeric():
    await mem.tool_record("Brian", "steps", 8000)
    db = mem.get_db()
    r = db.execute("SELECT * FROM readings").fetchone()
    assert r["value_type"] == "numeric"
    assert r["value_num"] == 8000.0
    db.close()


async def test_record_custom_timestamp():
    ts = time.time() - 3600
    await mem.tool_record("Brian", "temperature", 70.0, ts=ts)
    db = mem.get_db()
    r = db.execute("SELECT ts FROM readings").fetchone()
    assert abs(r["ts"] - ts) < 1.0
    db.close()


async def test_record_creates_entity_implicitly():
    await mem.tool_record("new_room", "temperature", 68.0, entity_type="room")
    db = mem.get_db()
    e = db.execute("SELECT * FROM entities WHERE name='new_room'").fetchone()
    assert e is not None
    assert e["type"] == "room"
    db.close()


async def test_record_with_source():
    await mem.tool_record("Brian", "temperature", 70.0, source="ha")
    db = mem.get_db()
    r = db.execute("SELECT source FROM readings").fetchone()
    assert r["source"] == "ha"
    db.close()


# ── query_stream ───────────────────────────────────────────────────────────────

async def test_query_stream_unknown_entity():
    result = await mem.tool_query_stream("nobody", "temperature")
    assert "No entity" in result


async def test_query_stream_no_data_for_metric():
    # Entity exists but no readings for this metric
    await mem.tool_remember("Brian", "exists")
    result = await mem.tool_query_stream("Brian", "temperature")
    assert "No" in result


async def test_query_stream_raw_returns_readings():
    now = time.time()
    for i in range(3):
        await mem.tool_record("Brian", "temperature", 68.0 + i, ts=now - i * 60)
    result = await mem.tool_query_stream(
        "Brian", "temperature",
        start_ts=now - 3600, end_ts=now + 60,
        granularity="raw",
    )
    assert "temperature" in result
    assert "raw" in result
    assert "n=3" in result


async def test_query_stream_raw_respects_time_window():
    now = time.time()
    await mem.tool_record("Brian", "temperature", 68.0, ts=now - 7200)  # 2h ago, outside window
    await mem.tool_record("Brian", "temperature", 70.0, ts=now - 60)    # recent, inside window
    result = await mem.tool_query_stream(
        "Brian", "temperature",
        start_ts=now - 3600, end_ts=now + 60,
        granularity="raw",
    )
    assert "n=1" in result


async def test_query_stream_rollup_returns_aggregates():
    now = time.time()
    for day in range(3):
        await mem.tool_record(
            "Brian", "temperature", 68.0 + day, ts=now - day * 86400
        )
    await mem._build_rollups()
    result = await mem.tool_query_stream(
        "Brian", "temperature",
        start_ts=now - 4 * 86400, end_ts=now + 86400,
        granularity="day",
    )
    assert "rollup" in result


async def test_query_stream_rollup_no_data_suggests_raw():
    await mem.tool_record("Brian", "temperature", 70.0)
    # No rollups built yet
    result = await mem.tool_query_stream("Brian", "temperature", granularity="hour")
    assert "raw" in result.lower() or "No" in result


# ── get_trends ─────────────────────────────────────────────────────────────────

async def test_get_trends_unknown_entity():
    result = await mem.tool_get_trends("nobody", "temperature")
    assert "No entity" in result


async def test_get_trends_no_data():
    await mem.tool_remember("Brian", "exists")
    result = await mem.tool_get_trends("Brian", "temperature")
    assert "No" in result


async def test_get_trends_numeric_stats():
    now = time.time()
    values = [68.0, 70.0, 72.0, 71.0, 69.0]
    for i, v in enumerate(values):
        await mem.tool_record("Brian", "temperature", v, ts=now - i * 3600)
    result = await mem.tool_get_trends("Brian", "temperature", window="day")
    assert "Samples" in result
    assert "Avg" in result


async def test_get_trends_categorical_mode():
    now = time.time()
    for i, s in enumerate(["home", "home", "home", "away", "home"]):
        await mem.tool_record("Brian", "presence", s, ts=now - i * 3600)
    result = await mem.tool_get_trends("Brian", "presence", window="day")
    assert "home" in result


async def test_get_trends_includes_promoted_insights():
    now = time.time()
    # Add readings and promote a pattern
    for i in range(14):
        await mem.tool_record(
            "Brian", "temperature", 68.0 + i * 0.05, ts=now - i * 86400
        )
    await mem._build_rollups()
    await mem._promote_patterns()
    result = await mem.tool_get_trends("Brian", "temperature")
    assert "patterns" in result.lower() or "insight" in result.lower() or "Learned" in result


# ── schedule ───────────────────────────────────────────────────────────────────

async def test_schedule_creates_event():
    future_ts = time.time() + 86400
    result = await mem.tool_schedule("Brian", "Doctor appointment", start_ts=future_ts)
    assert "Brian" in result
    assert "Doctor appointment" in result


async def test_schedule_recurring():
    future_ts = time.time() + 3600
    await mem.tool_schedule(
        "Brian", "Daily standup", start_ts=future_ts, recurrence="daily"
    )
    db = mem.get_db()
    ev = db.execute("SELECT recurrence FROM schedule_events").fetchone()
    assert ev["recurrence"] == "daily"
    db.close()


async def test_schedule_creates_entity_implicitly():
    future_ts = time.time() + 3600
    await mem.tool_schedule(
        "Alice", "Yoga class", start_ts=future_ts, entity_type="person"
    )
    db = mem.get_db()
    e = db.execute("SELECT name FROM entities WHERE name='Alice'").fetchone()
    assert e is not None
    db.close()


# ── cross_query ─────────────────────────────────────────────────────────────────

async def test_cross_query_finds_semantic_memories():
    await mem.tool_remember("Brian", "Prefers cooler rooms")
    result = await mem.tool_cross_query("temperature preference")
    assert "Semantic memories" in result


async def test_cross_query_finds_live_readings():
    now = time.time()
    await mem.tool_record(
        "living_room", "temperature", 65.0, entity_type="room", ts=now - 60
    )
    result = await mem.tool_cross_query("room temperature")
    assert "Live readings" in result


async def test_cross_query_empty_db_returns_gracefully():
    result = await mem.tool_cross_query("anything at all")
    # Should return something without crashing
    assert isinstance(result, str)
    assert len(result) > 0


# ── Bug fixes — Round 1 ────────────────────────────────────────────────────────

async def test_upsert_entity_preserves_type_on_update():
    """Entity type must not be overwritten when a second remember() call uses the default type."""
    # First call creates entity with type="room"
    await mem.tool_remember("living_room", "Has a couch", entity_type="room")
    # Second call uses default entity_type="person" — should NOT change the stored type
    await mem.tool_remember("living_room", "Has a TV")
    db = mem.get_db()
    row = db.execute("SELECT type FROM entities WHERE name='living_room'").fetchone()
    db.close()
    assert row["type"] == "room", "entity type was silently overwritten to 'person'"


async def test_get_profile_hides_superseded_memories():
    """tool_get_profile must not include superseded memories."""
    await mem.tool_remember("Brian", "Old outdated fact", category="preference")
    await mem.tool_remember("Brian", "New current fact", category="preference")
    # Manually mark the first memory as superseded (mock embeddings won't trigger
    # automatic contradiction detection since they produce unrelated random vectors)
    db = mem.get_db()
    old_id, new_id = [r["id"] for r in db.execute(
        "SELECT id FROM memories ORDER BY created ASC"
    ).fetchall()]
    db.execute("UPDATE memories SET superseded_by=? WHERE id=?", (new_id, old_id))
    db.commit()
    db.close()

    result = await mem.tool_get_profile("Brian")
    assert "New current fact" in result
    assert "Old outdated fact" not in result, "superseded memory must not appear in profile"


async def test_recall_hides_superseded_memories():
    """tool_recall must not return superseded memories."""
    await mem.tool_remember("Brian", "Old morning habit", category="habit")
    await mem.tool_remember("Brian", "New morning habit", category="habit")
    # Manually supersede the first memory
    db = mem.get_db()
    old_id, new_id = [r["id"] for r in db.execute(
        "SELECT id FROM memories ORDER BY created ASC"
    ).fetchall()]
    db.execute("UPDATE memories SET superseded_by=? WHERE id=?", (new_id, old_id))
    db.commit()
    db.close()

    result = await mem.tool_recall("morning habit", entity_name="Brian")
    assert "New morning habit" in result
    assert "Old morning habit" not in result, "superseded memory must not appear in recall"


# ── Temporal context — Round 2 ────────────────────────────────────────────────

async def test_recall_includes_age_label():
    """tool_recall output must include a temporal age label for each memory."""
    await mem.tool_remember("Brian", "Enjoys hiking")
    result = await mem.tool_recall("outdoor activity", entity_name="Brian")
    # Age labels: 'just now', '<N>m ago', '<N>h ago', '<N>d ago', or 'YYYY-MM-DD'
    assert "ago" in result or "just now" in result or any(
        c.isdigit() and "-" in result for c in result
    ), f"expected an age label in recall output, got:\n{result}"


async def test_get_profile_includes_age_label():
    """tool_get_profile memories must include creation age labels."""
    await mem.tool_remember("Brian", "Likes jazz")
    result = await mem.tool_get_profile("Brian")
    assert "ago" in result or "just now" in result, (
        f"expected age label in profile output, got:\n{result}"
    )


async def test_get_profile_readings_include_age_label():
    """tool_get_profile latest readings must include an age label."""
    await mem.tool_record("Brian", "temperature", 71.0)
    result = await mem.tool_get_profile("Brian")
    assert "ago" in result or "just now" in result, (
        f"expected age label on readings in profile, got:\n{result}"
    )


async def test_get_context_includes_age_label():
    """tool_get_context memory bullets must include age labels."""
    await mem.tool_remember("Brian", "Prefers quiet environments")
    result = await mem.tool_get_context("Brian", "noise preference")
    assert "ago" in result or "just now" in result, (
        f"expected age label in context output, got:\n{result}"
    )


async def test_get_context_readings_include_age_label():
    """tool_get_context latest readings must include age labels."""
    await mem.tool_record("Brian", "heart_rate", 62.0)
    result = await mem.tool_get_context("Brian", "health")
    assert "ago" in result or "just now" in result, (
        f"expected age label on readings in context, got:\n{result}"
    )


async def test_get_trends_includes_date_range():
    """tool_get_trends must include the actual date range in its header."""
    now = time.time()
    for i in range(5):
        await mem.tool_record("Brian", "temperature", 68.0 + i, ts=now - i * 3600)
    result = await mem.tool_get_trends("Brian", "temperature", window="day")
    # Date range format: 'YYYY-MM-DD → YYYY-MM-DD'
    assert "→" in result, f"expected date range arrow in trends output, got:\n{result}"
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}", result), (
        f"expected YYYY-MM-DD date in trends output, got:\n{result}"
    )


async def test_cross_query_memories_include_age_label():
    """tool_cross_query semantic memory hits must include age labels."""
    await mem.tool_remember("Brian", "Prefers cooler rooms")
    result = await mem.tool_cross_query("temperature preference")
    assert "Semantic memories" in result
    assert "ago" in result or "just now" in result, (
        f"expected age label in cross_query semantic hits, got:\n{result}"
    )


async def test_cross_query_hides_superseded_memories():
    """tool_cross_query must not surface superseded memories."""
    await mem.tool_remember("Brian", "Old room preference", category="preference")
    await mem.tool_remember("Brian", "New room preference", category="preference")
    # Manually supersede the first memory
    db = mem.get_db()
    old_id, new_id = [r["id"] for r in db.execute(
        "SELECT id FROM memories ORDER BY created ASC"
    ).fetchall()]
    db.execute("UPDATE memories SET superseded_by=? WHERE id=?", (new_id, old_id))
    db.commit()
    db.close()

    result = await mem.tool_cross_query("room preference")
    assert "New room preference" in result
    assert "Old room preference" not in result, (
        "superseded memory must not appear in cross_query results"
    )
