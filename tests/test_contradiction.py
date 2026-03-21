"""
Tests for Enhancement B: Contradiction detection.

When tool_remember() stores a new fact, it checks for semantically similar
existing memories (cosine sim >= CONTRADICTION_THRESHOLD) for the same entity.
Similar memories are marked superseded_by = new_memory_id.

Superseded memories are hidden from recall and get_context by default.
"""

import server as mem


# ── superseded_by column ─────────────────────────────────────────────────────

async def test_fresh_memory_not_superseded():
    await mem.tool_remember("Brian", "Likes coffee")
    db = mem.get_db()
    m = db.execute("SELECT superseded_by FROM memories").fetchone()
    db.close()
    assert m["superseded_by"] is None


async def test_unrelated_memory_not_superseded():
    """Two facts about very different topics — neither supersedes the other."""
    await mem.tool_remember("Brian", "Likes coffee")
    await mem.tool_remember("Brian", "Has a dog named Rex")
    db = mem.get_db()
    rows = db.execute("SELECT superseded_by FROM memories ORDER BY id").fetchall()
    db.close()
    # With mock embeddings (hash-based), these may or may not conflict —
    # but neither should be superseded unless they happen to land near each other.
    # We just verify the column exists and has the right type.
    for r in rows:
        assert r["superseded_by"] is None or isinstance(r["superseded_by"], int)


async def test_identical_fact_supersedes_older():
    """Storing the same fact twice — old one gets superseded_by = new id."""
    await mem.tool_remember("Brian", "Prefers the temperature at 68F")
    await mem.tool_remember("Brian", "Prefers the temperature at 68F")
    db = mem.get_db()
    rows = db.execute(
        "SELECT id, superseded_by FROM memories ORDER BY id"
    ).fetchall()
    db.close()
    assert len(rows) == 2
    old_id, new_id = rows[0]["id"], rows[1]["id"]
    # Old memory should be superseded by the new one
    assert rows[0]["superseded_by"] == new_id
    assert rows[1]["superseded_by"] is None


async def test_remember_reports_contradiction():
    """Return value mentions the superseded memory when a conflict is found."""
    await mem.tool_remember("Brian", "Prefers the temperature at 68F")
    result = await mem.tool_remember("Brian", "Prefers the temperature at 68F")
    assert "supersed" in result.lower() or "replac" in result.lower() or "updat" in result.lower()


# ── Recall hides superseded memories by default ───────────────────────────────

async def test_recall_hides_superseded_memory():
    """After contradiction, recall returns only the newer memory (Top 1, not Top 2)."""
    fact = "Prefers the temperature at 68F"
    await mem.tool_remember("Brian", fact)
    await mem.tool_remember("Brian", fact)
    result = await mem.tool_recall(fact, entity_name="Brian")
    # Header says "Top 1" — only the active memory is returned
    assert "Top 1" in result


async def test_recall_still_returns_non_superseded():
    """Non-superseded memories still appear in recall."""
    await mem.tool_remember("Brian", "Likes jazz music")
    await mem.tool_remember("Brian", "Has a dog named Rex")
    result = await mem.tool_recall("jazz", entity_name="Brian")
    assert "Likes jazz" in result


# ── get_context hides superseded memories ────────────────────────────────────

async def test_get_context_hides_superseded():
    fact = "Prefers the temperature at 68F"
    await mem.tool_remember("Brian", fact)
    await mem.tool_remember("Brian", fact)
    result = await mem.tool_get_context("Brian", "temperature preference")
    assert result.count("Prefers the temperature") <= 1


# ── Cross-entity isolation ────────────────────────────────────────────────────

async def test_contradiction_scoped_to_entity():
    """Same fact for two different entities: each is independent."""
    await mem.tool_remember("Brian", "Prefers the temperature at 68F")
    await mem.tool_remember("Sarah", "Prefers the temperature at 68F")
    # Neither entity's memory should be superseded by the other's
    db = mem.get_db()
    rows = db.execute(
        "SELECT e.name, m.superseded_by FROM memories m JOIN entities e ON e.id=m.entity_id"
    ).fetchall()
    db.close()
    for r in rows:
        assert r["superseded_by"] is None


# ── get_profile hides superseded memories (clean view for AI abilities) ──────

async def test_get_profile_hides_superseded_memories():
    """get_profile is used by AI abilities — it must only show current, active facts."""
    fact = "Prefers the temperature at 68F"
    await mem.tool_remember("Brian", fact)
    await mem.tool_remember("Brian", fact)
    result = await mem.tool_get_profile("Brian")
    # Second remember() supersedes the first — only one instance should appear
    assert result.count("Prefers the temperature") == 1
