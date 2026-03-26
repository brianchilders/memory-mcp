"""
Tests for importers/jsonl.py — import_jsonl() and POST /import/jsonl.

Covers:
  import_jsonl()            — entity creation, observation dedup, two-pass relations,
                              malformed lines, name validation, size limit
  POST /import/jsonl        — HTTP round-trip, auth, 400 on invalid content
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import server as mem
from importers.jsonl import import_jsonl

_TEST_TOKEN = "test-jsonl-token-xyz99"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── Helpers ───────────────────────────────────────────────────────────────────

def _line(**kwargs) -> str:
    return json.dumps(kwargs)

def _entity_line(name, entity_type="person", observations=None):
    obj = {"type": "entity", "name": name, "entityType": entity_type}
    if observations is not None:
        obj["observations"] = observations
    return json.dumps(obj)

def _relation_line(from_name, to_name, rel_type):
    return json.dumps({
        "type": "relation",
        "from": from_name,
        "to":   to_name,
        "relationType": rel_type,
    })

def _content(*lines):
    return "\n".join(lines)


# ── import_jsonl() unit tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_basic_entity_and_observations():
    content = _content(
        _entity_line("Alice", observations=["Likes coffee", "Works at CERN"]),
    )
    result = await import_jsonl(content)
    assert result.added   == 2
    assert result.skipped == 0
    assert result.errors  == []

    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name='Alice'").fetchone()
    assert e is not None
    mems = db.execute(
        "SELECT fact FROM memories WHERE entity_id=?", (e["id"],)
    ).fetchall()
    db.close()
    facts = {m["fact"] for m in mems}
    assert "Likes coffee" in facts
    assert "Works at CERN" in facts


@pytest.mark.asyncio
async def test_entity_type_preserved():
    content = _entity_line("Thermostat", entity_type="device", observations=["Controls HVAC"])
    result  = await import_jsonl(content)
    assert result.added == 1

    db  = mem.get_db()
    row = db.execute("SELECT type FROM entities WHERE name='Thermostat'").fetchone()
    db.close()
    assert row["type"] == "device"


@pytest.mark.asyncio
async def test_deduplication_skips_existing_observations():
    content = _entity_line("Bob", observations=["Plays guitar"])
    await import_jsonl(content)          # first import
    result = await import_jsonl(content) # second import — should skip
    assert result.added   == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_partial_deduplication():
    await import_jsonl(_entity_line("Carol", observations=["Fact A"]))
    result = await import_jsonl(
        _entity_line("Carol", observations=["Fact A", "Fact B"])
    )
    assert result.added   == 1
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_empty_observations_list():
    result = await import_jsonl(_entity_line("Dave", observations=[]))
    assert result.added  == 0
    assert result.errors == []
    db = mem.get_db()
    e  = db.execute("SELECT id FROM entities WHERE name='Dave'").fetchone()
    db.close()
    assert e is not None   # entity created even with no observations


@pytest.mark.asyncio
async def test_two_pass_relation_created():
    content = _content(
        _entity_line("Alice", observations=["A fact"]),
        _entity_line("Bob",   observations=["Another fact"]),
        _relation_line("Alice", "Bob", "friend"),
    )
    await import_jsonl(content)

    db = mem.get_db()
    a  = db.execute("SELECT id FROM entities WHERE name='Alice'").fetchone()
    b  = db.execute("SELECT id FROM entities WHERE name='Bob'").fetchone()
    rel = db.execute(
        "SELECT id FROM relations WHERE entity_a=? AND entity_b=? AND rel_type='friend'",
        (a["id"], b["id"]),
    ).fetchone()
    db.close()
    assert rel is not None


@pytest.mark.asyncio
async def test_relation_idempotent():
    content = _content(
        _entity_line("Alice", observations=["Fact"]),
        _entity_line("Bob",   observations=["Fact"]),
        _relation_line("Alice", "Bob", "friend"),
    )
    await import_jsonl(content)
    await import_jsonl(content)   # second import — relation must not be duplicated

    db   = mem.get_db()
    a    = db.execute("SELECT id FROM entities WHERE name='Alice'").fetchone()
    b    = db.execute("SELECT id FROM entities WHERE name='Bob'").fetchone()
    rels = db.execute(
        "SELECT id FROM relations WHERE entity_a=? AND entity_b=? AND rel_type='friend'",
        (a["id"], b["id"]),
    ).fetchall()
    db.close()
    assert len(rels) == 1


@pytest.mark.asyncio
async def test_stub_entity_created_for_unknown_relation_target():
    """Relation referencing an entity not in the file → stub entity created."""
    content = _content(
        _entity_line("Alice", observations=["Fact"]),
        _relation_line("Alice", "GhostCorp", "works_at"),
    )
    await import_jsonl(content)

    db = mem.get_db()
    e  = db.execute("SELECT id FROM entities WHERE name='GhostCorp'").fetchone()
    db.close()
    assert e is not None


@pytest.mark.asyncio
async def test_malformed_json_line_logged_as_error():
    content = _content(
        _entity_line("Alice", observations=["A fact"]),
        "this is not json {{{",
    )
    result = await import_jsonl(content)
    assert result.added  == 1
    assert len(result.errors) == 1
    assert "invalid json" in result.errors[0].lower()


@pytest.mark.asyncio
async def test_non_object_json_line_logged_as_error():
    content = _content(
        "[1, 2, 3]",
    )
    result = await import_jsonl(content)
    assert any("expected a json object" in e.lower() for e in result.errors)


@pytest.mark.asyncio
async def test_empty_entity_name_logged_as_error():
    content = _entity_line("", observations=["A fact"])
    result  = await import_jsonl(content)
    assert result.added == 0
    assert any("entity name" in e.lower() for e in result.errors)


@pytest.mark.asyncio
async def test_blank_lines_skipped():
    content = _content(
        "",
        _entity_line("Alice", observations=["Fact"]),
        "   ",
    )
    result = await import_jsonl(content)
    assert result.added == 1
    assert result.errors == []


@pytest.mark.asyncio
async def test_unknown_type_silently_ignored():
    content = _content(
        json.dumps({"type": "future_type", "data": "ignored"}),
        _entity_line("Alice", observations=["Fact"]),
    )
    result = await import_jsonl(content)
    assert result.added  == 1
    assert result.errors == []


@pytest.mark.asyncio
async def test_multiple_entities():
    content = _content(
        _entity_line("Alice", observations=["Fact A"]),
        _entity_line("Bob",   observations=["Fact B", "Fact C"]),
    )
    result = await import_jsonl(content)
    assert result.added == 3


@pytest.mark.asyncio
async def test_observations_non_list_logged_as_error():
    content = json.dumps({
        "type": "entity", "name": "Alice",
        "entityType": "person", "observations": "not a list",
    })
    result = await import_jsonl(content)
    assert any("must be a json array" in e.lower() for e in result.errors)


@pytest.mark.asyncio
async def test_relation_missing_fields_logged_as_error():
    content = json.dumps({"type": "relation", "from": "Alice"})   # missing to/relationType
    result  = await import_jsonl(content)
    assert any("missing" in e.lower() or "invalid" in e.lower() for e in result.errors)


@pytest.mark.asyncio
async def test_content_size_limit_raises():
    big = "x" * (5 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="5 MB"):
        await import_jsonl(big)


@pytest.mark.asyncio
async def test_source_tag_stored():
    content = _entity_line("Alice", observations=["Tagged fact"])
    await import_jsonl(content)

    db  = mem.get_db()
    row = db.execute("SELECT source FROM memories WHERE fact='Tagged fact'").fetchone()
    db.close()
    assert row["source"] == "import:jsonl"


# ── POST /import/jsonl HTTP tests ─────────────────────────────────────────────

def test_http_import_returns_200(client):
    content = _entity_line("Alice", observations=["HTTP fact"])
    r = client.post("/import/jsonl", json={"content": content})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]    is True
    assert body["added"] == 1


def test_http_import_empty_content(client):
    r = client.post("/import/jsonl", json={"content": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["added"]   == 0
    assert body["skipped"] == 0


def test_http_import_requires_auth(api_auth):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.post("/import/jsonl", json={"content": ""})
        assert r.status_code == 401


def test_http_import_result_shape(client):
    content = _content(
        _entity_line("Alice", observations=["Fact A", "Fact B"]),
        _relation_line("Alice", "Bob", "knows"),
        _entity_line("Bob", observations=["Fact C"]),
    )
    r    = client.post("/import/jsonl", json={"content": content})
    body = r.json()
    assert "added"   in body
    assert "skipped" in body
    assert "errors"  in body
    assert body["added"] == 3
