"""
Tests for Enhancement J: Auto-extraction via tool_extract_and_remember.

tool_extract_and_remember(entity_name, text) calls the Ollama LLM to extract
facts from conversation text, then stores each as a memory.

In tests, the Ollama LLM call is mocked to return deterministic JSON.
"""

import json
import server as mem


# ── Mock LLM responses ────────────────────────────────────────────────────────

def make_mock_llm(facts: list[dict]):
    """Return a mock _call_llm that always yields the given fact list as JSON."""
    async def mock_llm(prompt: str, model: str) -> str:
        return json.dumps(facts)
    return mock_llm


# ── tool_extract_and_remember ─────────────────────────────────────────────────

async def test_extract_stores_facts(monkeypatch):
    """Extracted facts are stored as memories for the entity."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([
        {"fact": "Likes oat milk in coffee", "category": "preference", "confidence": 0.9},
    ]))
    result = await mem.tool_extract_and_remember(
        "Brian",
        "I always use oat milk in my coffee, never regular milk.",
    )
    assert "Likes oat milk" in result or "1" in result

    db = mem.get_db()
    m = db.execute(
        "SELECT fact, category, confidence FROM memories WHERE fact LIKE '%oat milk%'"
    ).fetchone()
    db.close()
    assert m is not None
    assert m["category"] == "preference"
    assert abs(m["confidence"] - 0.9) < 1e-6


async def test_extract_multiple_facts(monkeypatch):
    """Multiple extracted facts each become a separate memory."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([
        {"fact": "Wakes up at 6am", "category": "habit", "confidence": 0.85},
        {"fact": "Drinks green tea", "category": "preference", "confidence": 0.8},
    ]))
    await mem.tool_extract_and_remember("Brian", "I wake at 6 and start with green tea.")
    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    db.close()
    assert count == 2


async def test_extract_no_facts_returns_gracefully(monkeypatch):
    """Empty LLM response (no extractable facts) doesn't crash."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([]))
    result = await mem.tool_extract_and_remember("Brian", "The weather is nice today.")
    assert isinstance(result, str)
    assert "0" in result or "no" in result.lower() or "nothing" in result.lower()


async def test_extract_uses_entity_name(monkeypatch):
    """Facts are stored under the correct entity."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([
        {"fact": "Allergic to peanuts", "category": "general", "confidence": 1.0},
    ]))
    await mem.tool_extract_and_remember("Sarah", "Sarah has a peanut allergy.")
    db = mem.get_db()
    row = db.execute(
        """SELECT e.name FROM memories m JOIN entities e ON e.id=m.entity_id
           WHERE m.fact LIKE '%peanut%'"""
    ).fetchone()
    db.close()
    assert row["name"] == "Sarah"


async def test_extract_creates_entity_if_not_exists(monkeypatch):
    """Entity is created if it doesn't already exist."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([
        {"fact": "Works in healthcare", "category": "general", "confidence": 0.8},
    ]))
    await mem.tool_extract_and_remember("NewPerson", "They are a nurse.")
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='NewPerson'").fetchone()
    db.close()
    assert e is not None


async def test_extract_malformed_llm_response_handled(monkeypatch):
    """If LLM returns invalid JSON, we get an error string, not a crash."""
    async def bad_llm(prompt: str, model: str) -> str:
        return "not valid json {{"
    monkeypatch.setattr(mem, "_call_llm", bad_llm)
    result = await mem.tool_extract_and_remember("Brian", "Some text.")
    assert isinstance(result, str)


async def test_extract_default_confidence_applied(monkeypatch):
    """Fact without confidence field gets the default (0.75)."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([
        {"fact": "Enjoys hiking"},  # no confidence or category
    ]))
    await mem.tool_extract_and_remember("Brian", "I love hiking trails.")
    db = mem.get_db()
    m = db.execute("SELECT confidence FROM memories").fetchone()
    db.close()
    assert m["confidence"] == 0.75


async def test_extract_returns_count_and_summary(monkeypatch):
    """Return value summarises how many facts were stored."""
    monkeypatch.setattr(mem, "_call_llm", make_mock_llm([
        {"fact": "Drinks coffee", "category": "habit", "confidence": 0.9},
        {"fact": "Works from home", "category": "habit", "confidence": 0.85},
    ]))
    result = await mem.tool_extract_and_remember("Brian", "I work from home and drink coffee.")
    assert "2" in result
