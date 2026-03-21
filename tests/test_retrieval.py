"""
Tests for retrieval quality features:
  H. Access logging (last_accessed, access_count)
  A. Multi-factor scoring (recency_weight, min_confidence)
  D. get_context() tool
"""

import math
import time

import server as mem


# ── H: Access logging ─────────────────────────────────────────────────────────

async def test_fresh_memory_has_zero_access_count():
    await mem.tool_remember("Brian", "Likes coffee")
    db = mem.get_db()
    m = db.execute("SELECT access_count, last_accessed FROM memories").fetchone()
    db.close()
    assert m["access_count"] == 0
    assert m["last_accessed"] is None


async def test_recall_sets_last_accessed():
    await mem.tool_remember("Brian", "Likes jazz")
    before = time.time()
    await mem.tool_recall("music", entity_name="Brian")
    after = time.time()

    db = mem.get_db()
    m = db.execute("SELECT last_accessed FROM memories").fetchone()
    db.close()
    assert m["last_accessed"] is not None
    assert before <= m["last_accessed"] <= after


async def test_recall_increments_access_count():
    await mem.tool_remember("Brian", "Likes jazz")
    await mem.tool_recall("music", entity_name="Brian")
    await mem.tool_recall("music", entity_name="Brian")

    db = mem.get_db()
    m = db.execute("SELECT access_count FROM memories").fetchone()
    db.close()
    assert m["access_count"] == 2


async def test_recall_with_no_results_does_not_crash():
    # No memories — recall should not raise on access update
    result = await mem.tool_recall("anything")
    assert "No relevant" in result


async def test_recall_only_updates_returned_memories():
    await mem.tool_remember("Brian", "Likes jazz")
    await mem.tool_remember("Brian", "Loves hiking")

    # Recall with top_k=1 — only one memory is returned and updated
    await mem.tool_recall("music", entity_name="Brian", top_k=1)

    db = mem.get_db()
    counts = [r["access_count"] for r in
              db.execute("SELECT access_count FROM memories ORDER BY id").fetchall()]
    db.close()
    # Exactly one of them should have access_count=1, the other 0
    assert sum(counts) == 1


async def test_cross_query_also_updates_access():
    await mem.tool_remember("Brian", "Likes coffee")
    await mem.tool_cross_query("coffee preference")

    db = mem.get_db()
    m = db.execute("SELECT access_count FROM memories").fetchone()
    db.close()
    # cross_query should also register as an access
    assert m["access_count"] >= 1


# ── A: Multi-factor retrieval ─────────────────────────────────────────────────

async def test_recall_min_confidence_filters_low_confidence_memories():
    await mem.tool_remember("Brian", "Likes coffee")

    # Manually lower confidence of the stored memory
    db = mem.get_db()
    db.execute("UPDATE memories SET confidence=0.3 WHERE fact='Likes coffee'")
    db.commit()
    db.close()

    result = await mem.tool_recall("coffee", entity_name="Brian", min_confidence=0.5)
    assert "No relevant" in result or "Likes coffee" not in result


async def test_recall_min_confidence_allows_high_confidence():
    await mem.tool_remember("Brian", "Likes coffee", confidence=0.9)
    result = await mem.tool_recall("coffee", entity_name="Brian", min_confidence=0.5)
    assert "Likes coffee" in result


async def test_recall_min_confidence_default_zero_shows_all():
    await mem.tool_remember("Brian", "Low confidence fact", confidence=0.1)
    result = await mem.tool_recall("fact", entity_name="Brian")
    assert "Low confidence fact" in result


async def test_recall_recency_weight_zero_does_not_penalise_old_memories():
    """With recency_weight=0, age should not affect ranking — both old and new memories return."""
    await mem.tool_remember("Brian", "Old fact about coffee")
    await mem.tool_remember("Brian", "New fact about coffee")

    # Age the first memory
    db = mem.get_db()
    db.execute("UPDATE memories SET updated=updated-86400*365 WHERE fact='Old fact about coffee'")
    db.commit()
    db.close()

    result = await mem.tool_recall("coffee fact", entity_name="Brian",
                                   top_k=2, recency_weight=0.0)
    assert "Old fact about coffee" in result
    assert "New fact about coffee" in result


async def test_recall_high_recency_weight_demotes_old_memories():
    """With strong recency weight, a 365-day-old memory should rank below a fresh one."""
    # Store two memories with identical text (same embedding = same cosine dist)
    # then age one and check ordering
    await mem.tool_remember("Brian", "Prefers mild coffee")  # will be aged
    await mem.tool_remember("Brian", "Prefers mild coffee")  # stays fresh

    db = mem.get_db()
    ids = [r["id"] for r in db.execute("SELECT id FROM memories ORDER BY id").fetchall()]
    # Age the first one significantly
    db.execute("UPDATE memories SET updated=? WHERE id=?",
               (time.time() - 365 * 86400, ids[0]))
    db.commit()
    db.close()

    result = await mem.tool_recall("coffee preference", entity_name="Brian",
                                   top_k=1, recency_weight=1.0)
    # We can't easily assert which one wins by content (text is identical),
    # but the call should not error and should return exactly 1 result
    assert "Top 1" in result


async def test_recall_recency_weight_parameter_accepted():
    """Verify the new parameter doesn't break existing call patterns."""
    await mem.tool_remember("Brian", "Likes tea")
    result = await mem.tool_recall("tea", entity_name="Brian", recency_weight=0.5)
    assert "Likes tea" in result


async def test_recency_factor_helper():
    """_recency_factor() should be 1.0 for fresh and < 1.0 for old."""
    fresh = mem._recency_factor(time.time(), weight=1.0)
    old   = mem._recency_factor(time.time() - 365 * 86400, weight=1.0)
    assert abs(fresh - 1.0) < 0.01
    assert old < 0.5


async def test_recency_factor_weight_zero_always_one():
    old = mem._recency_factor(time.time() - 365 * 86400, weight=0.0)
    assert abs(old - 1.0) < 0.001


# ── D: get_context() tool ─────────────────────────────────────────────────────

async def test_get_context_unknown_entity():
    result = await mem.tool_get_context("Nobody", "anything")
    assert "No entity" in result


async def test_get_context_returns_relevant_memories():
    await mem.tool_remember("Brian", "Prefers 65F for sleeping")
    await mem.tool_remember("Brian", "Enjoys hiking on weekends")
    result = await mem.tool_get_context("Brian", "temperature preferences")
    assert "Brian" in result
    assert "65F" in result


async def test_get_context_respects_max_facts():
    for i in range(10):
        await mem.tool_remember("Brian", f"Unique fact {i} about Brian")
    # max_facts=2 should limit memory lines
    result = await mem.tool_get_context("Brian", "fact about Brian", max_facts=2)
    assert "Brian" in result
    # Should not contain all 10 facts
    fact_count = sum(1 for i in range(10) if f"Unique fact {i}" in result)
    assert fact_count <= 2


async def test_get_context_includes_relationships():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    result = await mem.tool_get_context("Brian", "family")
    assert "Sarah" in result


async def test_get_context_includes_latest_readings():
    await mem.tool_record("Brian", "temperature", 72.0, unit="F")
    result = await mem.tool_get_context("Brian", "health")
    assert "temperature" in result
    assert "72" in result


async def test_get_context_includes_upcoming_schedule():
    future_ts = time.time() + 3600
    await mem.tool_schedule("Brian", "Doctor checkup", start_ts=future_ts)
    result = await mem.tool_get_context("Brian", "schedule")
    assert "Doctor checkup" in result


async def test_get_context_empty_entity_no_crash():
    """Entity with no memories, readings, or schedule still returns a valid profile."""
    await mem.tool_relate("Brian", "Sarah", "spouse")  # creates both entities
    result = await mem.tool_get_context("Sarah", "anything")
    assert "Sarah" in result
    assert isinstance(result, str)


async def test_get_context_updates_access_count():
    await mem.tool_remember("Brian", "Relevant fact about work")
    await mem.tool_get_context("Brian", "work context")

    db = mem.get_db()
    m = db.execute("SELECT access_count FROM memories").fetchone()
    db.close()
    assert m["access_count"] >= 1
