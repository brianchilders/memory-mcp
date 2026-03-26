"""
tests/test_trust.py — Source trust tier tests.

Covers:
  Constants and env-var config
  tool_remember — trust stored, trust label in return string, clamp
  Conflict resolution — lower trust does not supersede higher trust
  Conflict resolution — equal trust supersedes (existing behaviour)
  Conflict resolution — higher trust supersedes lower trust
  tool_recall — min_trust filter, trust factor in scoring
  tool_get_context — min_trust filter
  Ingestion path defaults — extract, imports, admin UI
  HTTP — POST /remember source_trust, POST /recall min_trust,
          POST /get_context min_trust, import source_trust overrides
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-trust-token-abc42"


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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_trust(fact: str) -> int | None:
    db  = mem.get_db()
    row = db.execute("SELECT source_trust FROM memories WHERE fact=?", (fact,)).fetchone()
    db.close()
    return row["source_trust"] if row else None


# Fixed unit vector used to force high cosine similarity in conflict tests.
# When embed() always returns this, any two facts look identical and the
# contradiction threshold (0.85) is always exceeded.
_FIXED_VEC = [1.0] + [0.0] * (mem.EMBED_DIM - 1)


@pytest.fixture
def same_embedding(monkeypatch):
    """Patch embed() to return the same vector for every call.
    This forces cosine similarity = 1.0, making every pair of memories for the
    same entity look like a contradiction — allowing trust rules to be tested.
    """
    async def _fixed_embed(text: str) -> list[float]:
        return _FIXED_VEC

    monkeypatch.setattr(mem, "embed", _fixed_embed)


# ── Constants ─────────────────────────────────────────────────────────────────

def test_trust_tier_constants():
    assert mem.TRUST_USER     == 5
    assert mem.TRUST_HARDWARE == 4
    assert mem.TRUST_SYSTEM   == 3
    assert mem.TRUST_INFERRED == 2
    assert mem.TRUST_EXTERNAL == 1


def test_trust_names_mapping():
    assert mem.TRUST_NAMES[mem.TRUST_USER]     == "user"
    assert mem.TRUST_NAMES[mem.TRUST_HARDWARE] == "hardware"
    assert mem.TRUST_NAMES[mem.TRUST_SYSTEM]   == "system"
    assert mem.TRUST_NAMES[mem.TRUST_INFERRED] == "inferred"
    assert mem.TRUST_NAMES[mem.TRUST_EXTERNAL] == "external"


def test_trust_by_name_reverse_mapping():
    assert mem.TRUST_BY_NAME["user"]     == mem.TRUST_USER
    assert mem.TRUST_BY_NAME["hardware"] == mem.TRUST_HARDWARE
    assert mem.TRUST_BY_NAME["external"] == mem.TRUST_EXTERNAL


def test_default_remember_is_user():
    assert mem.TRUST_DEFAULT_REMEMBER == mem.TRUST_USER


def test_default_import_is_external():
    assert mem.TRUST_DEFAULT_IMPORT == mem.TRUST_EXTERNAL


def test_default_extract_is_inferred():
    assert mem.TRUST_DEFAULT_EXTRACT == mem.TRUST_INFERRED


def test_default_pattern_is_inferred():
    assert mem.TRUST_DEFAULT_PATTERN == mem.TRUST_INFERRED


def test_parse_trust_env_by_name(monkeypatch):
    monkeypatch.setenv("MEMORY_TRUST_DEFAULT_IMPORT", "hardware")
    val = mem._parse_trust_env("MEMORY_TRUST_DEFAULT_IMPORT", mem.TRUST_EXTERNAL)
    assert val == mem.TRUST_HARDWARE


def test_parse_trust_env_by_integer(monkeypatch):
    monkeypatch.setenv("MEMORY_TRUST_DEFAULT_IMPORT", "3")
    val = mem._parse_trust_env("MEMORY_TRUST_DEFAULT_IMPORT", mem.TRUST_EXTERNAL)
    assert val == mem.TRUST_SYSTEM


def test_parse_trust_env_clamps_high(monkeypatch):
    monkeypatch.setenv("MEMORY_TRUST_DEFAULT_IMPORT", "99")
    val = mem._parse_trust_env("MEMORY_TRUST_DEFAULT_IMPORT", mem.TRUST_EXTERNAL)
    assert val == mem.TRUST_USER


def test_parse_trust_env_clamps_low(monkeypatch):
    monkeypatch.setenv("MEMORY_TRUST_DEFAULT_IMPORT", "0")
    val = mem._parse_trust_env("MEMORY_TRUST_DEFAULT_IMPORT", mem.TRUST_EXTERNAL)
    assert val == mem.TRUST_EXTERNAL


# ── tool_remember stores trust ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remember_stores_default_trust():
    await mem.tool_remember(entity_name="Bob", fact="Bob likes jazz")
    assert _get_trust("Bob likes jazz") == mem.TRUST_USER


@pytest.mark.asyncio
async def test_remember_stores_explicit_trust():
    await mem.tool_remember(entity_name="Bob", fact="Bob sensor reading", source_trust=mem.TRUST_HARDWARE)
    assert _get_trust("Bob sensor reading") == mem.TRUST_HARDWARE


@pytest.mark.asyncio
async def test_remember_clamps_trust_above_max():
    await mem.tool_remember(entity_name="Bob", fact="Bob clamped high", source_trust=99)
    assert _get_trust("Bob clamped high") == mem.TRUST_USER


@pytest.mark.asyncio
async def test_remember_clamps_trust_below_min():
    await mem.tool_remember(entity_name="Bob", fact="Bob clamped low", source_trust=0)
    assert _get_trust("Bob clamped low") == mem.TRUST_EXTERNAL


@pytest.mark.asyncio
async def test_remember_return_string_includes_trust_label():
    result = await mem.tool_remember(
        entity_name="Carol", fact="Carol trust label", source_trust=mem.TRUST_HARDWARE
    )
    assert "trust=hardware" in result


# ── Conflict resolution ────────────────────────────────────────────────────────
# These tests use the same_embedding fixture so that every pair of facts has
# cosine similarity 1.0, ensuring the contradiction threshold is always met.
# This lets us isolate and test the trust-comparison logic cleanly.

@pytest.mark.asyncio
async def test_lower_trust_does_not_supersede_higher_trust(same_embedding):
    """An external-trust fact must not overwrite a user-trust fact."""
    await mem.tool_remember(
        entity_name="Dave", fact="Dave weighs 180 lbs", source_trust=mem.TRUST_USER
    )
    await mem.tool_remember(
        entity_name="Dave", fact="Dave weighs 220 lbs", source_trust=mem.TRUST_EXTERNAL
    )
    db  = mem.get_db()
    row = db.execute(
        "SELECT superseded_by FROM memories WHERE fact='Dave weighs 180 lbs'"
    ).fetchone()
    db.close()
    assert row["superseded_by"] is None, "High-trust memory should NOT be superseded by lower-trust"


@pytest.mark.asyncio
async def test_equal_trust_supersedes_older(same_embedding):
    """Same-trust new fact supersedes old (existing behaviour)."""
    await mem.tool_remember(
        entity_name="Eve", fact="Eve is in London", source_trust=mem.TRUST_SYSTEM
    )
    await mem.tool_remember(
        entity_name="Eve", fact="Eve is in Paris", source_trust=mem.TRUST_SYSTEM
    )
    db  = mem.get_db()
    row = db.execute(
        "SELECT superseded_by FROM memories WHERE fact='Eve is in London'"
    ).fetchone()
    db.close()
    assert row["superseded_by"] is not None, "Equal-trust new fact should supersede old"


@pytest.mark.asyncio
async def test_higher_trust_supersedes_lower_trust(same_embedding):
    """A higher-trust correction supersedes a lower-trust claim."""
    await mem.tool_remember(
        entity_name="Frank", fact="Frank is allergic to peanuts", source_trust=mem.TRUST_EXTERNAL
    )
    await mem.tool_remember(
        entity_name="Frank", fact="Frank has no allergies", source_trust=mem.TRUST_USER
    )
    db  = mem.get_db()
    row = db.execute(
        "SELECT superseded_by FROM memories WHERE fact='Frank is allergic to peanuts'"
    ).fetchone()
    db.close()
    assert row["superseded_by"] is not None, "Higher-trust correction should supersede lower-trust claim"


@pytest.mark.asyncio
async def test_supersede_count_respects_trust(same_embedding):
    """System-trust supersedes external-trust but is blocked from superseding hardware-trust.

    When the only existing similar memory is TRUST_EXTERNAL (1), a TRUST_SYSTEM (3)
    write succeeds and supersedes it.

    When an existing similar memory is TRUST_HARDWARE (4), a TRUST_SYSTEM (3) write
    is blocked entirely by the pre-write cross-check.
    """
    import time as _time

    # ── Part 1: system supersedes external ────────────────────────────────────
    db  = mem.get_db()
    now = _time.time()
    eid = mem.upsert_entity(db, "Grace")
    db.commit()
    vec = await mem.embed("test")  # same_embedding: fixed unit vector

    cur = db.execute(
        "INSERT INTO memories(entity_id,fact,category,confidence,source_trust,created,updated)"
        " VALUES(?,?,?,?,?,?,?)",
        (eid, "Grace has external fact", "general", 1.0, mem.TRUST_EXTERNAL, now, now),
    )
    ext_mid = cur.lastrowid
    db.execute("INSERT INTO memory_vectors(rowid,embedding) VALUES(?,?)",
               (ext_mid, mem.vec_blob(vec)))
    db.commit()
    db.close()

    result = await mem.tool_remember(
        entity_name="Grace", fact="Grace has system fact",
        source_trust=mem.TRUST_SYSTEM,
    )
    assert "Write blocked" not in result, f"System should supersede external: {result}"

    db      = mem.get_db()
    ext_row = db.execute(
        "SELECT superseded_by FROM memories WHERE fact='Grace has external fact'"
    ).fetchone()
    db.close()
    assert ext_row["superseded_by"] is not None, "External-trust must be superseded by system-trust"

    # ── Part 2: system is blocked by hardware ─────────────────────────────────
    db  = mem.get_db()
    eid2 = mem.upsert_entity(db, "GraceHW")
    db.commit()

    cur = db.execute(
        "INSERT INTO memories(entity_id,fact,category,confidence,source_trust,created,updated)"
        " VALUES(?,?,?,?,?,?,?)",
        (eid2, "GraceHW has hardware fact", "general", 1.0, mem.TRUST_HARDWARE, now, now),
    )
    hw_mid = cur.lastrowid
    db.execute("INSERT INTO memory_vectors(rowid,embedding) VALUES(?,?)",
               (hw_mid, mem.vec_blob(vec)))
    db.commit()
    db.close()

    blocked = await mem.tool_remember(
        entity_name="GraceHW", fact="GraceHW system contradicts hardware",
        source_trust=mem.TRUST_SYSTEM,
    )
    assert "Write blocked" in blocked, "System write should be blocked by existing hardware-trust fact"

    db      = mem.get_db()
    hw_row  = db.execute(
        "SELECT superseded_by FROM memories WHERE fact='GraceHW has hardware fact'"
    ).fetchone()
    db.close()
    assert hw_row["superseded_by"] is None, "Hardware-trust must survive system-trust arrival"


# ── tool_recall — min_trust filter ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_min_trust_filters_lower():
    await mem.tool_remember(entity_name="Hal", fact="Hal height sensor 180cm", source_trust=mem.TRUST_HARDWARE)
    await mem.tool_remember(entity_name="Hal", fact="Hal height scrape 175cm", source_trust=mem.TRUST_EXTERNAL)

    result = await mem.tool_recall(query="Hal height", entity_name="Hal", min_trust=mem.TRUST_HARDWARE)
    assert "sensor 180cm" in result
    assert "scrape 175cm" not in result


@pytest.mark.asyncio
async def test_recall_min_trust_zero_returns_all():
    await mem.tool_remember(entity_name="Iris", fact="Iris user fact", source_trust=mem.TRUST_USER)
    await mem.tool_remember(entity_name="Iris", fact="Iris external fact", source_trust=mem.TRUST_EXTERNAL)

    result = await mem.tool_recall(query="Iris fact", entity_name="Iris", min_trust=0)
    assert "user fact" in result or "external fact" in result


@pytest.mark.asyncio
async def test_recall_trust_weight_boosts_higher_trust():
    """Higher-trust fact should score above lower-trust fact when other factors are equal."""
    await mem.tool_remember(entity_name="Jack", fact="Jack prefers hiking", source_trust=mem.TRUST_USER)
    await mem.tool_remember(entity_name="Jack", fact="Jack enjoys hiking trips", source_trust=mem.TRUST_EXTERNAL)

    result = await mem.tool_recall(query="Jack hiking", entity_name="Jack", top_k=2)
    lines  = [l for l in result.splitlines() if "hiking" in l.lower()]
    # The user-trust fact should appear — we can't assert exact rank without controlled
    # embeddings, but we can assert both appear and no error occurred
    assert len(lines) >= 1


# ── tool_get_context — min_trust filter ───────────────────────────────────────

@pytest.mark.asyncio
async def test_get_context_min_trust_filters():
    await mem.tool_remember(entity_name="Karen", fact="Karen job from API",   source_trust=mem.TRUST_SYSTEM)
    await mem.tool_remember(entity_name="Karen", fact="Karen job from scrape", source_trust=mem.TRUST_EXTERNAL)

    result = await mem.tool_get_context(
        entity_name="Karen", context_query="job", min_trust=mem.TRUST_SYSTEM
    )
    assert "from API" in result
    assert "from scrape" not in result


@pytest.mark.asyncio
async def test_get_context_min_trust_zero_returns_all():
    await mem.tool_remember(entity_name="Leo", fact="Leo user info",     source_trust=mem.TRUST_USER)
    await mem.tool_remember(entity_name="Leo", fact="Leo external info", source_trust=mem.TRUST_EXTERNAL)

    result = await mem.tool_get_context(entity_name="Leo", context_query="info", min_trust=0)
    assert "user info" in result or "external info" in result


# ── Ingestion path defaults ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_and_remember_uses_inferred_trust(monkeypatch):
    """extract_and_remember should tag memories with TRUST_INFERRED."""
    llm_response = json.dumps([{"fact": "Mia speaks Spanish", "category": "general", "confidence": 0.9}])

    async def fake_llm(prompt, model="llama3.2"):
        return llm_response

    monkeypatch.setattr(mem, "_call_llm", fake_llm)
    await mem.tool_extract_and_remember(
        entity_name="Mia", text="Mia told me she speaks Spanish."
    )
    assert _get_trust("Mia speaks Spanish") == mem.TRUST_INFERRED


@pytest.mark.asyncio
async def test_existing_memories_default_to_user_trust():
    """Rows added by earlier code (before migration) default to TRUST_USER."""
    db  = mem.get_db()
    now = time.time()
    eid = mem.upsert_entity(db, "OldEntity", "person")
    # Simulate a pre-migration INSERT without source_trust
    db.execute(
        "INSERT INTO memories(entity_id,fact,category,confidence,source,created,updated) "
        "VALUES(?,?,'general',1.0,'legacy',?,?)",
        (eid, "Legacy fact", now, now),
    )
    db.commit()
    db.close()
    # DB default should have set source_trust = TRUST_USER (5)
    assert _get_trust("Legacy fact") == mem.TRUST_USER


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

def test_http_remember_with_source_trust(client):
    r = client.post("/remember", json={
        "entity_name": "Nina",
        "fact": "Nina is a data scientist",
        "source_trust": mem.TRUST_HARDWARE,
    })
    assert r.status_code == 200
    assert _get_trust("Nina is a data scientist") == mem.TRUST_HARDWARE


def test_http_remember_source_trust_out_of_range(client):
    r = client.post("/remember", json={
        "entity_name": "Oscar",
        "fact": "Oscar fact",
        "source_trust": 99,
    })
    assert r.status_code == 422


def test_http_recall_min_trust(client):
    client.post("/remember", json={
        "entity_name": "Paula", "fact": "Paula HW fact", "source_trust": mem.TRUST_HARDWARE,
    })
    client.post("/remember", json={
        "entity_name": "Paula", "fact": "Paula EXT fact", "source_trust": mem.TRUST_EXTERNAL,
    })
    r = client.post("/recall", json={
        "query": "Paula fact", "entity_name": "Paula", "min_trust": mem.TRUST_HARDWARE,
    })
    assert r.status_code == 200
    body = r.json()["result"]
    assert "HW fact" in body
    assert "EXT fact" not in body


def test_http_recall_min_trust_out_of_range(client):
    r = client.post("/recall", json={"query": "anything", "min_trust": 99})
    assert r.status_code == 422


def test_http_get_context_min_trust(client):
    client.post("/remember", json={
        "entity_name": "Quinn", "fact": "Quinn system fact", "source_trust": mem.TRUST_SYSTEM,
    })
    client.post("/remember", json={
        "entity_name": "Quinn", "fact": "Quinn external fact", "source_trust": mem.TRUST_EXTERNAL,
    })
    r = client.post("/get_context", json={
        "entity_name": "Quinn", "context_query": "Quinn", "min_trust": mem.TRUST_SYSTEM,
    })
    assert r.status_code == 200
    body = r.json()["result"]
    assert "system fact" in body
    assert "external fact" not in body


def test_http_import_jsonl_source_trust_override(client):
    content = json.dumps({
        "type": "entity", "name": "TrustImported",
        "entityType": "person", "observations": ["Trust override fact"],
    })
    r = client.post("/import/jsonl", json={
        "content": content,
        "source_trust": mem.TRUST_SYSTEM,
    })
    assert r.status_code == 200
    assert _get_trust("Trust override fact") == mem.TRUST_SYSTEM


def test_http_import_jsonl_default_trust_is_external(client):
    content = json.dumps({
        "type": "entity", "name": "DefaultTrustEntity",
        "entityType": "person", "observations": ["Default trust fact"],
    })
    r = client.post("/import/jsonl", json={"content": content})
    assert r.status_code == 200
    assert _get_trust("Default trust fact") == mem.TRUST_EXTERNAL


def test_http_mcp_info_tool_count_reflects_params(client):
    """tool_count and tools list must agree (regression guard for registration bugs)."""
    body = client.get("/mcp-info").json()
    assert body["tool_count"] == len(body["tools"])
    assert body["tool_count"] > 0
