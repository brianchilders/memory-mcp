"""
Tests for spatial / location memory (Tier 5).

Covers:
  - tool_locate  — create, refresh same spot, move to new spot
  - tool_find    — current location, previous location, unknown object
  - tool_seen_at — confidence boost, redirect when location differs
  - tool_location_history — full trail in reverse-chronological order
  - _decay_locations — exponential decay, floor, halflife=0 skip
  - _format_age  — human-readable age helper
  - HTTP endpoints — POST /locate, /find, /seen_at, GET /location_history/{name}
"""

import time

import pytest

import server as mem
from tests.conftest import _FIXED_VEC


# ── _format_age helper ─────────────────────────────────────────────────────────

def test_format_age_seconds():
    assert mem._format_age(45) == "45 seconds"

def test_format_age_minutes():
    assert mem._format_age(300) == "5 minutes"

def test_format_age_hours():
    assert mem._format_age(7200) == "2 hours"

def test_format_age_days():
    assert mem._format_age(172800) == "2 days"


# ── tool_locate ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_locate_new_object():
    result = await mem.tool_locate("keys", "entryway table")
    assert "Located" in result
    assert "keys" in result
    assert "entryway table" in result


@pytest.mark.asyncio
async def test_locate_creates_entities():
    await mem.tool_locate("passport", "filing cabinet", entity_type="document", container_type="furniture")
    db = mem.get_db()
    obj = db.execute("SELECT type FROM entities WHERE name='passport'").fetchone()
    container = db.execute("SELECT type FROM entities WHERE name='filing cabinet'").fetchone()
    db.close()
    assert obj["type"] == "document"
    assert container["type"] == "furniture"


@pytest.mark.asyncio
async def test_locate_creates_location_row():
    await mem.tool_locate("remote", "couch")
    db = mem.get_db()
    row = db.execute(
        "SELECT l.active, l.confidence FROM locations l "
        "JOIN entities e ON e.id=l.entity_id WHERE e.name='remote'"
    ).fetchone()
    db.close()
    assert row is not None
    assert row["active"] == 1
    assert row["confidence"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_locate_same_container_refreshes():
    await mem.tool_locate("keys", "entryway table")
    result = await mem.tool_locate("keys", "entryway table")
    assert "Confirmed" in result
    # Should still be only one active row
    db = mem.get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys' AND l.active=1"
    ).fetchone()[0]
    db.close()
    assert count == 1


@pytest.mark.asyncio
async def test_locate_different_container_archives_old():
    await mem.tool_locate("keys", "entryway table")
    result = await mem.tool_locate("keys", "kitchen counter")
    assert "Located" in result
    db = mem.get_db()
    rows = db.execute(
        "SELECT l.active, l.container_name FROM locations l "
        "JOIN entities e ON e.id=l.entity_id WHERE e.name='keys' "
        "ORDER BY l.created"
    ).fetchall()
    db.close()
    assert len(rows) == 2
    assert rows[0]["active"] == 0          # old location archived
    assert rows[0]["container_name"] == "entryway table"
    assert rows[1]["active"] == 1          # new location active
    assert rows[1]["container_name"] == "kitchen counter"


@pytest.mark.asyncio
async def test_locate_with_note():
    await mem.tool_locate("book", "bookshelf", note="third shelf from top")
    db = mem.get_db()
    row = db.execute(
        "SELECT l.note FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='book'"
    ).fetchone()
    db.close()
    assert row["note"] == "third shelf from top"


@pytest.mark.asyncio
async def test_locate_confidence_capped_at_one():
    await mem.tool_locate("keys", "entryway", confidence=1.5)
    db = mem.get_db()
    row = db.execute(
        "SELECT l.confidence FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys'"
    ).fetchone()
    db.close()
    assert row["confidence"] == pytest.approx(1.0)


# ── tool_find ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_returns_current_location():
    await mem.tool_locate("keys", "entryway table")
    result = await mem.tool_find("keys")
    assert "entryway table" in result
    assert "confidence" in result.lower()


@pytest.mark.asyncio
async def test_find_unknown_object_returns_no_location():
    result = await mem.tool_find("magic wand")
    assert "No location recorded" in result


@pytest.mark.asyncio
async def test_find_shows_previous_location_after_move():
    await mem.tool_locate("remote", "couch")
    await mem.tool_locate("remote", "bedroom dresser")
    result = await mem.tool_find("remote")
    assert "bedroom dresser" in result   # current
    assert "couch" in result             # previous


@pytest.mark.asyncio
async def test_find_historical_only_when_no_active():
    await mem.tool_locate("phone", "desk")
    # Manually deactivate the location
    db = mem.get_db()
    db.execute(
        "UPDATE locations SET active=0 WHERE entity_id="
        "(SELECT id FROM entities WHERE name='phone')"
    )
    db.commit()
    db.close()
    result = await mem.tool_find("phone")
    assert "Previously seen" in result
    assert "desk" in result


@pytest.mark.asyncio
async def test_find_includes_note_in_output():
    await mem.tool_locate("passport", "filing cabinet", note="blue folder")
    result = await mem.tool_find("passport")
    assert "blue folder" in result


# ── tool_seen_at ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_seen_at_same_location_boosts_confidence():
    await mem.tool_locate("keys", "entryway", confidence=0.7)
    result = await mem.tool_seen_at("keys", "entryway")
    assert "Confirmed" in result
    db = mem.get_db()
    row = db.execute(
        "SELECT l.confidence FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys' AND l.active=1"
    ).fetchone()
    db.close()
    assert row["confidence"] > 0.7


@pytest.mark.asyncio
async def test_seen_at_confidence_capped_at_one():
    await mem.tool_locate("keys", "entryway", confidence=1.0)
    await mem.tool_seen_at("keys", "entryway")
    db = mem.get_db()
    row = db.execute(
        "SELECT l.confidence FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys' AND l.active=1"
    ).fetchone()
    db.close()
    assert row["confidence"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_seen_at_different_location_creates_new():
    await mem.tool_locate("remote", "couch")
    result = await mem.tool_seen_at("remote", "kitchen")
    # Different location — should behave like tool_locate (creates new active row)
    assert "kitchen" in result
    db = mem.get_db()
    active = db.execute(
        "SELECT l.container_name FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='remote' AND l.active=1"
    ).fetchone()
    db.close()
    assert active["container_name"] == "kitchen"


@pytest.mark.asyncio
async def test_seen_at_case_insensitive_match():
    await mem.tool_locate("glasses", "Entryway Table")
    result = await mem.tool_seen_at("glasses", "entryway table")
    assert "Confirmed" in result


# ── tool_location_history ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_location_history_no_records():
    result = await mem.tool_location_history("ghost object")
    assert "No location history" in result


@pytest.mark.asyncio
async def test_location_history_single_entry():
    await mem.tool_locate("keys", "entryway table")
    result = await mem.tool_location_history("keys")
    assert "entryway table" in result
    assert "current" in result


@pytest.mark.asyncio
async def test_location_history_multiple_entries():
    await mem.tool_locate("keys", "entryway")
    await mem.tool_locate("keys", "kitchen")
    await mem.tool_locate("keys", "bedroom")
    result = await mem.tool_location_history("keys")
    # All three locations should appear
    assert "entryway" in result
    assert "kitchen" in result
    assert "bedroom" in result
    assert "current" in result
    assert "previous" in result


@pytest.mark.asyncio
async def test_location_history_limit():
    for i in range(5):
        await mem.tool_locate("book", f"shelf {i}")
    result = await mem.tool_location_history("book", limit=2)
    # Should show at most 2 entries
    assert result.count("[") <= 2


@pytest.mark.asyncio
async def test_location_history_reverse_chronological():
    await mem.tool_locate("remote", "couch")
    await mem.tool_locate("remote", "desk")
    result = await mem.tool_location_history("remote")
    # Most recent (desk / current) should appear before older (couch / previous)
    assert result.index("desk") < result.index("couch")


# ── _decay_locations ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decay_reduces_confidence(monkeypatch):
    await mem.tool_locate("keys", "entryway")
    # Backdate last_confirmed_ts by 48 hours → should drop to ~25%
    db = mem.get_db()
    db.execute(
        "UPDATE locations SET last_confirmed_ts=last_confirmed_ts - 172800"
    )
    db.commit()
    db.close()

    await mem._decay_locations()

    db = mem.get_db()
    row = db.execute(
        "SELECT l.confidence FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys'"
    ).fetchone()
    db.close()
    assert row["confidence"] < 0.5    # well below original 1.0


@pytest.mark.asyncio
async def test_decay_respects_floor():
    await mem.tool_locate("keys", "entryway")
    # Backdate by 3 weeks — should hit the floor, not go to zero
    db = mem.get_db()
    db.execute(
        "UPDATE locations SET last_confirmed_ts=last_confirmed_ts - 1814400"  # 21 days
    )
    db.commit()
    db.close()

    await mem._decay_locations()

    db = mem.get_db()
    row = db.execute(
        "SELECT l.confidence FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys'"
    ).fetchone()
    db.close()
    assert row["confidence"] == pytest.approx(mem.LOCATION_DECAY_FLOOR, abs=0.01)


@pytest.mark.asyncio
async def test_decay_skipped_when_halflife_zero(monkeypatch):
    monkeypatch.setattr(mem, "LOCATION_DECAY_HALFLIFE_HOURS", 0)
    await mem.tool_locate("keys", "entryway")
    db = mem.get_db()
    db.execute("UPDATE locations SET last_confirmed_ts=last_confirmed_ts - 172800")
    db.commit()
    db.close()

    n = await mem._decay_locations()
    assert n == 0

    db = mem.get_db()
    row = db.execute(
        "SELECT l.confidence FROM locations l JOIN entities e ON e.id=l.entity_id "
        "WHERE e.name='keys'"
    ).fetchone()
    db.close()
    assert row["confidence"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_decay_skips_inactive_rows():
    await mem.tool_locate("keys", "entryway")
    await mem.tool_locate("keys", "kitchen")    # archives old → active=0
    # Backdate all rows
    db = mem.get_db()
    db.execute("UPDATE locations SET last_confirmed_ts=last_confirmed_ts - 172800")
    db.commit()
    old_conf_inactive = db.execute(
        "SELECT confidence FROM locations WHERE active=0"
    ).fetchone()["confidence"]
    db.close()

    await mem._decay_locations()

    db = mem.get_db()
    new_conf_inactive = db.execute(
        "SELECT confidence FROM locations WHERE active=0"
    ).fetchone()["confidence"]
    db.close()
    # Inactive rows should not be touched by decay
    assert new_conf_inactive == pytest.approx(old_conf_inactive)


# ── HTTP endpoints ────────────────────────────────────────────────────────────

_TEST_TOKEN = "spatial-test-token-xyz"


@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    """Return a TestClient for api.py with auth pre-set."""
    import api
    from fastapi.testclient import TestClient
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


def test_http_locate(client):
    r = client.post("/locate", json={"entity_name": "keys", "container_name": "entryway"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "Located" in r.json()["result"]


def test_http_locate_refresh(client):
    client.post("/locate", json={"entity_name": "keys", "container_name": "entryway"})
    r = client.post("/locate", json={"entity_name": "keys", "container_name": "entryway"})
    assert r.status_code == 200
    assert "Confirmed" in r.json()["result"]


def test_http_locate_confidence_out_of_range_rejected(client):
    r = client.post("/locate", json={
        "entity_name": "keys", "container_name": "entryway", "confidence": 1.5
    })
    assert r.status_code == 422


def test_http_find(client):
    client.post("/locate", json={"entity_name": "remote", "container_name": "couch"})
    r = client.post("/find", json={"entity_name": "remote"})
    assert r.status_code == 200
    assert "couch" in r.json()["result"]


def test_http_find_unknown(client):
    r = client.post("/find", json={"entity_name": "invisible object"})
    assert r.status_code == 200
    assert "No location" in r.json()["result"]


def test_http_seen_at(client):
    client.post("/locate", json={"entity_name": "keys", "container_name": "entryway",
                                 "confidence": 0.6})
    r = client.post("/seen_at", json={"entity_name": "keys", "container_name": "entryway"})
    assert r.status_code == 200
    assert "Confirmed" in r.json()["result"]


def test_http_location_history(client):
    client.post("/locate", json={"entity_name": "passport", "container_name": "filing cabinet"})
    r = client.get("/location_history/passport")
    assert r.status_code == 200
    assert "filing cabinet" in r.json()["result"]


def test_http_location_history_limit_param(client):
    for i in range(5):
        client.post("/locate", json={"entity_name": "book", "container_name": f"shelf {i}"})
    r = client.get("/location_history/book?limit=2")
    assert r.status_code == 200


def test_http_location_history_limit_out_of_range(client):
    r = client.get("/location_history/book?limit=0")
    assert r.status_code == 422


def test_http_location_history_missing_entity(client):
    r = client.get("/location_history/nonexistent")
    assert r.status_code == 200
    assert "No location history" in r.json()["result"]


def test_http_requires_auth(api_auth):
    """Endpoints return 401 when no Bearer token is provided."""
    import api
    from fastapi.testclient import TestClient
    with TestClient(api.app) as anon:
        assert anon.post("/locate", json={"entity_name": "x", "container_name": "y"}).status_code == 401
        assert anon.post("/find", json={"entity_name": "x"}).status_code == 401
        assert anon.post("/seen_at", json={"entity_name": "x", "container_name": "y"}).status_code == 401
        assert anon.get("/location_history/x").status_code == 401
