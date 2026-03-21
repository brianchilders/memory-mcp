"""
Tests for Enhancement G: Temporal graph edges.

Relations now carry valid_from / valid_until timestamps.
tool_unrelate() soft-deletes by setting valid_until.
Active-only filtering applies to get_profile and get_context.
"""

import time

import server as mem


# ── valid_from / valid_until basics ───────────────────────────────────────────

async def test_relate_sets_valid_from():
    before = time.time()
    await mem.tool_relate("Brian", "Sarah", "spouse")
    after = time.time()
    db = mem.get_db()
    r = db.execute("SELECT valid_from, valid_until FROM relations").fetchone()
    db.close()
    assert r["valid_from"] is not None
    assert before <= r["valid_from"] <= after
    assert r["valid_until"] is None


async def test_relate_valid_until_null_on_creation():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    db = mem.get_db()
    r = db.execute("SELECT valid_until FROM relations").fetchone()
    db.close()
    assert r["valid_until"] is None


# ── tool_unrelate ─────────────────────────────────────────────────────────────

async def test_unrelate_sets_valid_until():
    await mem.tool_relate("Brian", "Sarah", "colleague")
    before = time.time()
    result = await mem.tool_unrelate("Brian", "Sarah", "colleague")
    after = time.time()
    db = mem.get_db()
    r = db.execute("SELECT valid_until FROM relations").fetchone()
    db.close()
    assert "ended" in result.lower() or "unrelated" in result.lower()
    assert r["valid_until"] is not None
    assert before <= r["valid_until"] <= after


async def test_unrelate_unknown_entity_returns_error():
    result = await mem.tool_unrelate("Nobody", "Ghost", "friend")
    assert "No" in result or "not found" in result.lower()


async def test_unrelate_unknown_relation_returns_error():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    result = await mem.tool_unrelate("Brian", "Sarah", "nonexistent_rel")
    assert "No" in result or "not found" in result.lower()


async def test_unrelate_already_ended_is_safe():
    """Calling unrelate twice doesn't crash — second call reports no active relation."""
    await mem.tool_relate("Brian", "Sarah", "colleague")
    await mem.tool_unrelate("Brian", "Sarah", "colleague")
    result = await mem.tool_unrelate("Brian", "Sarah", "colleague")
    # Should not raise, should report nothing found
    assert isinstance(result, str)


# ── Active-only filtering in get_profile and get_context ─────────────────────

async def test_get_profile_hides_expired_relation():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    await mem.tool_unrelate("Brian", "Sarah", "spouse")
    result = await mem.tool_get_profile("Brian")
    assert "Sarah" not in result


async def test_get_profile_shows_active_relation():
    await mem.tool_relate("Brian", "Sarah", "spouse")
    result = await mem.tool_get_profile("Brian")
    assert "Sarah" in result


async def test_get_context_hides_expired_relation():
    await mem.tool_remember("Brian", "Some fact about Brian")
    await mem.tool_relate("Brian", "Sarah", "colleague")
    await mem.tool_unrelate("Brian", "Sarah", "colleague")
    result = await mem.tool_get_context("Brian", "relationships")
    assert "Sarah" not in result


async def test_get_context_shows_active_relation():
    await mem.tool_remember("Brian", "Some fact about Brian")
    await mem.tool_relate("Brian", "Sarah", "colleague")
    result = await mem.tool_get_context("Brian", "relationships")
    assert "Sarah" in result


# ── Reactivation ──────────────────────────────────────────────────────────────

async def test_relate_after_unrelate_reactivates():
    """Re-relating after ending resets valid_until to NULL."""
    await mem.tool_relate("Brian", "Sarah", "colleague")
    await mem.tool_unrelate("Brian", "Sarah", "colleague")
    await mem.tool_relate("Brian", "Sarah", "colleague")
    db = mem.get_db()
    r = db.execute("SELECT valid_until FROM relations").fetchone()
    db.close()
    assert r["valid_until"] is None


async def test_reactivated_relation_visible_in_profile():
    await mem.tool_relate("Brian", "Sarah", "colleague")
    await mem.tool_unrelate("Brian", "Sarah", "colleague")
    await mem.tool_relate("Brian", "Sarah", "colleague")
    result = await mem.tool_get_profile("Brian")
    assert "Sarah" in result


# ── History preservation ──────────────────────────────────────────────────────

async def test_unrelate_preserves_row_in_db():
    """Row is soft-deleted (valid_until set), NOT hard-deleted."""
    await mem.tool_relate("Brian", "Sarah", "spouse")
    await mem.tool_unrelate("Brian", "Sarah", "spouse")
    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    db.close()
    assert count == 1  # row still present


async def test_multiple_relation_types_independent():
    """Ending one rel type does not affect others."""
    await mem.tool_relate("Brian", "Sarah", "spouse")
    await mem.tool_relate("Brian", "Sarah", "colleague")
    await mem.tool_unrelate("Brian", "Sarah", "colleague")
    result = await mem.tool_get_profile("Brian")
    assert "spouse" in result
    assert "colleague" not in result
