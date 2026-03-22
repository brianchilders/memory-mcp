"""
Integration tests for the /voices API routes (voice_routes.py).

Uses Starlette's TestClient (sync), running the full app in-process.
The isolated_db and mock_embed autouse fixtures from conftest.py are active,
so every test gets a fresh isolated SQLite DB with no Ollama dependency.

Helper functions insert test entities and readings directly via mem.get_db(),
since voiceprint entities are managed outside the standard /remember flow.
"""

import json
import math
import time

import pytest
from fastapi.testclient import TestClient

import server as mem

_TEST_TOKEN = "test-voice-token-xyz"


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


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _insert_entity(name, status="unenrolled", detection_count=5,
                   first_seen_room="kitchen", voiceprint=None,
                   voiceprint_samples=None, entity_type="person"):
    """Insert a voice entity directly into the test DB. Returns entity id."""
    db = mem.get_db()
    now = time.time()
    meta = {
        "status": status,
        "first_seen": "2026-03-22T10:30:00Z",
        "first_seen_room": first_seen_room,
        "detection_count": detection_count,
    }
    if voiceprint is not None:
        meta["voiceprint"] = voiceprint
        meta["voiceprint_samples"] = voiceprint_samples if voiceprint_samples is not None else 1
    db.execute(
        "INSERT INTO entities(name, type, meta, created, updated) VALUES (?,?,?,?,?)",
        (name, entity_type, json.dumps(meta), now, now),
    )
    db.commit()
    eid = db.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()["id"]
    db.close()
    return eid


def _insert_voice_reading(entity_id, transcript, ts=None):
    """Insert a voice_activity composite reading for an entity."""
    db = mem.get_db()
    db.execute(
        """INSERT INTO readings(entity_id, metric, value_type, value_json, ts)
           VALUES (?, 'voice_activity', 'composite', ?, ?)""",
        (entity_id, json.dumps({"transcript": transcript}), ts or time.time()),
    )
    db.commit()
    db.close()


def _get_entity_meta(name):
    """Fetch the parsed meta dict for an entity by name."""
    db = mem.get_db()
    row = db.execute("SELECT meta FROM entities WHERE name = ?", (name,)).fetchone()
    db.close()
    return json.loads(row["meta"]) if row else None


def _unit_vec(axis, dim=256):
    """Return a unit vector with 1.0 at `axis` and 0.0 elsewhere."""
    vec = [0.0] * dim
    vec[axis] = 1.0
    return vec


# ── GET /voices/unknown ────────────────────────────────────────────────────────

def test_list_unknown_empty(client):
    r = client.get("/voices/unknown")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"] == []


def test_list_unknown_filters_enrolled(client):
    _insert_entity("unknown_voice_aaa", status="unenrolled", detection_count=3)
    _insert_entity("Brian", status="enrolled", detection_count=10)

    r = client.get("/voices/unknown")
    assert r.status_code == 200
    names = [e["entity_name"] for e in r.json()["result"]]
    assert "unknown_voice_aaa" in names
    assert "Brian" not in names


def test_list_unknown_with_transcript(client):
    eid = _insert_entity("unknown_voice_bbb", detection_count=4)
    _insert_voice_reading(eid, "I need to pick up groceries tomorrow")

    r = client.get("/voices/unknown")
    assert r.status_code == 200
    entries = {e["entity_name"]: e for e in r.json()["result"]}
    assert "unknown_voice_bbb" in entries
    entry = entries["unknown_voice_bbb"]
    assert entry["sample_transcript"] == "I need to pick up groceries tomorrow"
    assert entry["last_seen"] is not None


def test_list_unknown_most_recent_transcript(client):
    """last_seen and sample_transcript come from the most recent voice_activity reading."""
    eid = _insert_entity("unknown_voice_ccc", detection_count=2)
    t_old = time.time() - 3600
    t_new = time.time() - 60
    _insert_voice_reading(eid, "older utterance", ts=t_old)
    _insert_voice_reading(eid, "newer utterance", ts=t_new)

    r = client.get("/voices/unknown")
    entry = next(e for e in r.json()["result"] if e["entity_name"] == "unknown_voice_ccc")
    assert entry["sample_transcript"] == "newer utterance"
    assert entry["last_seen"] == pytest.approx(t_new, abs=1.0)


def test_list_unknown_min_detections(client):
    _insert_entity("unknown_voice_low", detection_count=1)
    _insert_entity("unknown_voice_high", detection_count=5)

    r = client.get("/voices/unknown?min_detections=3")
    assert r.status_code == 200
    names = [e["entity_name"] for e in r.json()["result"]]
    assert "unknown_voice_high" in names
    assert "unknown_voice_low" not in names


def test_list_unknown_respects_limit(client):
    for i in range(5):
        _insert_entity(f"unknown_voice_{i:03d}", detection_count=i + 1)

    r = client.get("/voices/unknown?limit=2")
    assert r.status_code == 200
    assert len(r.json()["result"]) == 2


def test_list_unknown_ordered_by_detection_count_desc(client):
    _insert_entity("unknown_voice_x1", detection_count=2)
    _insert_entity("unknown_voice_x2", detection_count=8)
    _insert_entity("unknown_voice_x3", detection_count=5)

    r = client.get("/voices/unknown")
    names = [e["entity_name"] for e in r.json()["result"]]
    assert names.index("unknown_voice_x2") < names.index("unknown_voice_x3")
    assert names.index("unknown_voice_x3") < names.index("unknown_voice_x1")


# ── POST /voices/enroll ────────────────────────────────────────────────────────

def test_enroll_success(client):
    eid = _insert_entity("unknown_voice_enroll1", detection_count=7)
    _insert_voice_reading(eid, "some utterance")

    r = client.post("/voices/enroll", json={
        "entity_name": "unknown_voice_enroll1",
        "new_name": "Brian",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["entity_name"] == "Brian"
    assert result["previous_name"] == "unknown_voice_enroll1"
    assert result["readings_transferred"] == 1

    # Verify in DB: name changed, status enrolled, entity_id unchanged
    meta = _get_entity_meta("Brian")
    assert meta is not None
    assert meta["status"] == "enrolled"
    assert _get_entity_meta("unknown_voice_enroll1") is None


def test_enroll_sets_display_name(client):
    _insert_entity("unknown_voice_enroll2")

    r = client.post("/voices/enroll", json={
        "entity_name": "unknown_voice_enroll2",
        "new_name": "Sarah",
        "display_name": "Sarah Childers",
    })
    assert r.status_code == 200
    meta = _get_entity_meta("Sarah")
    assert meta["display_name"] == "Sarah Childers"
    assert meta["status"] == "enrolled"


def test_enroll_without_display_name_omits_field(client):
    _insert_entity("unknown_voice_enroll3")

    client.post("/voices/enroll", json={
        "entity_name": "unknown_voice_enroll3",
        "new_name": "Emma",
    })
    meta = _get_entity_meta("Emma")
    assert "display_name" not in meta


def test_enroll_conflict(client):
    _insert_entity("unknown_voice_conflict")
    _insert_entity("AlreadyExists", status="enrolled")

    r = client.post("/voices/enroll", json={
        "entity_name": "unknown_voice_conflict",
        "new_name": "AlreadyExists",
    })
    assert r.status_code == 409
    assert "AlreadyExists" in r.json()["detail"]


def test_enroll_not_found(client):
    r = client.post("/voices/enroll", json={
        "entity_name": "unknown_voice_ghost",
        "new_name": "Brian",
    })
    assert r.status_code == 404
    assert "unknown_voice_ghost" in r.json()["detail"]


def test_enroll_preserves_memories(client):
    """Memories stay attached — entity_id doesn't change on rename."""
    _insert_entity("unknown_voice_mem1", detection_count=3)
    # Add a memory via the remember endpoint
    client.post("/remember", json={"entity_name": "unknown_voice_mem1", "fact": "Speaks often"})

    r = client.post("/voices/enroll", json={
        "entity_name": "unknown_voice_mem1",
        "new_name": "Carol",
    })
    assert r.status_code == 200
    assert r.json()["result"]["memories_transferred"] == 1

    # Memory still accessible under new name
    r2 = client.get("/profile/Carol")
    assert "Speaks often" in r2.json()["result"]


# ── POST /voices/merge ─────────────────────────────────────────────────────────

def test_merge_success(client):
    src_id = _insert_entity("unknown_voice_src", detection_count=3)
    tgt_id = _insert_entity("Brian", status="enrolled", detection_count=10)

    # Add a memory and reading to source
    client.post("/remember", json={"entity_name": "unknown_voice_src", "fact": "Heard in garage"})
    _insert_voice_reading(src_id, "some words")

    r = client.post("/voices/merge", json={
        "source_name": "unknown_voice_src",
        "target_name": "Brian",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["target_name"] == "Brian"
    assert result["memories_merged"] == 1
    assert result["readings_merged"] == 1
    assert result["source_deleted"] == "unknown_voice_src"

    # Source entity gone
    assert _get_entity_meta("unknown_voice_src") is None

    # Memory now accessible via target
    r2 = client.get("/profile/Brian")
    assert "Heard in garage" in r2.json()["result"]


def test_merge_accumulates_detection_count(client):
    _insert_entity("unknown_voice_dc_src", detection_count=4)
    _insert_entity("Brian_dc", status="enrolled", detection_count=10)

    client.post("/voices/merge", json={
        "source_name": "unknown_voice_dc_src",
        "target_name": "Brian_dc",
    })

    meta = _get_entity_meta("Brian_dc")
    assert meta["detection_count"] == 14


def test_merge_voiceprint_averaging(client):
    """Voiceprints are averaged weighted by sample count, then re-normalized."""
    src_vp = _unit_vec(0)  # [1, 0, 0, ...]
    tgt_vp = _unit_vec(1)  # [0, 1, 0, ...]
    src_n, tgt_n = 3, 7

    _insert_entity("unknown_voice_vp_src", voiceprint=src_vp, voiceprint_samples=src_n)
    _insert_entity("Brian_vp", status="enrolled", voiceprint=tgt_vp, voiceprint_samples=tgt_n)

    client.post("/voices/merge", json={
        "source_name": "unknown_voice_vp_src",
        "target_name": "Brian_vp",
    })

    meta = _get_entity_meta("Brian_vp")
    assert meta["voiceprint_samples"] == src_n + tgt_n

    merged = meta["voiceprint"]
    # Pre-normalization: [3/10, 7/10, 0, ...]
    # norm = sqrt(0.09 + 0.49) = sqrt(0.58)
    expected_norm = math.sqrt(0.58)
    assert merged[0] == pytest.approx(0.3 / expected_norm, abs=1e-6)
    assert merged[1] == pytest.approx(0.7 / expected_norm, abs=1e-6)
    assert all(abs(v) < 1e-9 for v in merged[2:])

    # Result must be a unit vector
    result_norm = sum(x * x for x in merged) ** 0.5
    assert result_norm == pytest.approx(1.0, abs=1e-6)


def test_merge_source_has_voiceprint_target_does_not(client):
    """When only source has a voiceprint, it is adopted by the target."""
    src_vp = _unit_vec(0)
    _insert_entity("unknown_voice_novp_src", voiceprint=src_vp, voiceprint_samples=5)
    _insert_entity("Brian_novp", status="enrolled")

    client.post("/voices/merge", json={
        "source_name": "unknown_voice_novp_src",
        "target_name": "Brian_novp",
    })

    meta = _get_entity_meta("Brian_novp")
    assert meta["voiceprint"] == src_vp
    assert meta["voiceprint_samples"] == 5


def test_merge_self_merge_rejected(client):
    """Merging an entity with itself must be rejected — it would delete the entity."""
    _insert_entity("Brian_self", status="enrolled")

    r = client.post("/voices/merge", json={
        "source_name": "Brian_self",
        "target_name": "Brian_self",
    })
    assert r.status_code == 400
    assert "different" in r.json()["detail"]

    # Entity must still exist
    assert _get_entity_meta("Brian_self") is not None


def test_merge_source_not_found(client):
    _insert_entity("Brian_msnf", status="enrolled")

    r = client.post("/voices/merge", json={
        "source_name": "unknown_voice_ghost",
        "target_name": "Brian_msnf",
    })
    assert r.status_code == 404
    assert "unknown_voice_ghost" in r.json()["detail"]


def test_merge_target_not_found(client):
    _insert_entity("unknown_voice_mtnf")

    r = client.post("/voices/merge", json={
        "source_name": "unknown_voice_mtnf",
        "target_name": "Nobody",
    })
    assert r.status_code == 404
    assert "Nobody" in r.json()["detail"]


# ── POST /voices/update_print ──────────────────────────────────────────────────

def test_update_print_first_time(client):
    """No existing voiceprint: embedding stored as-is, samples set to 1."""
    _insert_entity("Brian_up1", status="enrolled")
    embedding = _unit_vec(0)

    r = client.post("/voices/update_print", json={
        "entity_name": "Brian_up1",
        "embedding": embedding,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["voiceprint_samples"] == 1
    assert body["result"]["embedding_norm"] == pytest.approx(1.0, abs=1e-4)

    meta = _get_entity_meta("Brian_up1")
    assert meta["voiceprint"] == embedding
    assert meta["voiceprint_samples"] == 1


def test_update_print_running_average(client):
    """Blended embedding is re-normalized; sample count increments."""
    existing_vp = _unit_vec(0)  # [1, 0, 0, ...]
    incoming_vp = _unit_vec(1)  # [0, 1, 0, ...]
    weight = 0.1
    existing_samples = 5

    _insert_entity("Brian_up2", status="enrolled",
                   voiceprint=existing_vp, voiceprint_samples=existing_samples)

    r = client.post("/voices/update_print", json={
        "entity_name": "Brian_up2",
        "embedding": incoming_vp,
        "weight": weight,
    })
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["voiceprint_samples"] == existing_samples + 1
    assert result["embedding_norm"] == pytest.approx(1.0, abs=1e-4)

    meta = _get_entity_meta("Brian_up2")
    merged = meta["voiceprint"]

    # blended (pre-norm): [0.9, 0.1, 0, ...]
    # norm = sqrt(0.81 + 0.01) = sqrt(0.82)
    expected_norm = math.sqrt(0.82)
    assert merged[0] == pytest.approx(0.9 / expected_norm, abs=1e-6)
    assert merged[1] == pytest.approx(0.1 / expected_norm, abs=1e-6)
    assert all(abs(v) < 1e-9 for v in merged[2:])

    result_norm = sum(x * x for x in merged) ** 0.5
    assert result_norm == pytest.approx(1.0, abs=1e-6)


def test_update_print_wrong_shape(client):
    _insert_entity("Brian_up3", status="enrolled")
    bad_embedding = [0.1] * 128  # wrong dimension

    r = client.post("/voices/update_print", json={
        "entity_name": "Brian_up3",
        "embedding": bad_embedding,
    })
    assert r.status_code == 422
    assert "256" in r.json()["detail"]


def test_update_print_rejects_nan():
    """
    NaN in an embedding would corrupt meta JSON — rejected by the Pydantic validator.

    Tested at the model level: standard JSON (httpx, curl, etc.) can't even
    serialise NaN so it never reaches the server via HTTP. The validator guards
    against direct Pydantic model construction from pipeline code.
    """
    from voice_routes import UpdatePrintRequest
    embedding = _unit_vec(0)
    embedding[5] = float("nan")
    with pytest.raises(Exception):  # pydantic.ValidationError
        UpdatePrintRequest(entity_name="Brian", embedding=embedding)


def test_update_print_rejects_infinity():
    """Infinity in an embedding is rejected by the Pydantic validator (same reason as NaN)."""
    from voice_routes import UpdatePrintRequest
    embedding = _unit_vec(0)
    embedding[3] = float("inf")
    with pytest.raises(Exception):  # pydantic.ValidationError
        UpdatePrintRequest(entity_name="Brian", embedding=embedding)


def test_update_print_not_found(client):
    embedding = _unit_vec(0)
    r = client.post("/voices/update_print", json={
        "entity_name": "nobody",
        "embedding": embedding,
    })
    assert r.status_code == 404
    assert "nobody" in r.json()["detail"]
