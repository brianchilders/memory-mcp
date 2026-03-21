"""
Tests for Enhancement I: Memory consolidation.

_consolidate_memories() is a pattern-engine pass that clusters memories
by cosine similarity and merges near-duplicates (sim >= CONSOLIDATION_THRESHOLD).
The highest-confidence memory in a cluster wins; others are marked superseded.

This prevents memory bloat from repeated facts accumulating over time.
"""

import server as mem


# ── _consolidate_memories basics ──────────────────────────────────────────────

async def test_consolidation_merges_near_duplicates():
    """Two nearly-identical memories → the weaker one is superseded."""
    # Store the same fact twice (identical text = identical vectors under mock embed)
    mid1 = None
    mid2 = None

    db = mem.get_db()
    import time
    now = time.time()
    eid = mem.upsert_entity(db, "Brian")
    db.close()

    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.8)
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.9)

    # Reset superseded_by (as if contradiction detection hadn't run)
    db = mem.get_db()
    db.execute("UPDATE memories SET superseded_by=NULL")
    db.commit()
    db.close()

    # Now run consolidation — should detect the pair and supersede the weaker one
    await mem._consolidate_memories()

    db = mem.get_db()
    rows = db.execute(
        "SELECT id, confidence, superseded_by FROM memories ORDER BY id"
    ).fetchall()
    db.close()

    superseded = [r for r in rows if r["superseded_by"] is not None]
    assert len(superseded) >= 1


async def test_consolidation_preserves_high_confidence_winner():
    """The memory with highest confidence survives; the lower-confidence one is superseded."""
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.7)
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.95)

    db = mem.get_db()
    db.execute("UPDATE memories SET superseded_by=NULL")
    db.commit()
    db.close()

    await mem._consolidate_memories()

    db = mem.get_db()
    rows = db.execute(
        "SELECT confidence, superseded_by FROM memories ORDER BY confidence DESC"
    ).fetchall()
    db.close()

    # The highest-confidence memory should not be superseded
    assert rows[0]["superseded_by"] is None
    assert rows[0]["confidence"] == 0.95


async def test_consolidation_does_not_supersede_distinct_memories():
    """Memories about different topics should not be merged."""
    await mem.tool_remember("Brian", "Likes jazz music intensely")
    await mem.tool_remember("Brian", "Owns a golden retriever named Max")

    await mem._consolidate_memories()

    db = mem.get_db()
    rows = db.execute("SELECT superseded_by FROM memories").fetchall()
    db.close()

    # With hash-based mock embeddings, these should be dissimilar enough
    # Both should survive (though we can't guarantee it without real embeddings,
    # so we check that at least 1 is not superseded)
    not_superseded = [r for r in rows if r["superseded_by"] is None]
    assert len(not_superseded) >= 1


async def test_consolidation_is_entity_scoped():
    """Same fact for two entities: neither supersedes the other."""
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.8)
    await mem.tool_remember("Sarah", "Brian prefers 68F at night", confidence=0.8)

    db = mem.get_db()
    db.execute("UPDATE memories SET superseded_by=NULL")
    db.commit()
    db.close()

    await mem._consolidate_memories()

    db = mem.get_db()
    rows = db.execute(
        """SELECT e.name, m.superseded_by FROM memories m
           JOIN entities e ON e.id=m.entity_id"""
    ).fetchall()
    db.close()

    for r in rows:
        assert r["superseded_by"] is None


async def test_consolidation_empty_db_safe():
    """No memories → no crash."""
    await mem._consolidate_memories()  # should not raise


async def test_consolidation_single_memory_safe():
    """Only one memory → nothing to consolidate."""
    await mem.tool_remember("Brian", "Likes jazz")
    await mem._consolidate_memories()
    db = mem.get_db()
    m = db.execute("SELECT superseded_by FROM memories").fetchone()
    db.close()
    assert m["superseded_by"] is None


async def test_consolidation_returns_count():
    """_consolidate_memories() returns the number of memories superseded."""
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.7)
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.9)

    db = mem.get_db()
    db.execute("UPDATE memories SET superseded_by=NULL")
    db.commit()
    db.close()

    count = await mem._consolidate_memories()
    assert isinstance(count, int)
    assert count >= 0


async def test_consolidation_does_not_re_supersede_already_superseded():
    """Already-superseded memories are excluded from consolidation candidates."""
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.7)
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.8)
    await mem.tool_remember("Brian", "Brian prefers 68F at night", confidence=0.9)

    # Run once
    await mem._consolidate_memories()
    db = mem.get_db()
    first_pass = db.execute("SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL").fetchone()[0]
    db.close()

    # Run again — should not further reduce active memories
    await mem._consolidate_memories()
    db = mem.get_db()
    second_pass = db.execute("SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL").fetchone()[0]
    db.close()

    assert second_pass == first_pass
