"""
Tests for Enhancement C: Composite reading decomposition.

When a composite (dict) value is recorded, each scalar key is also
stored as a child reading with metric name "{parent}.{key}".
"""

import time

import server as mem


# ── Decomposition at ingest ───────────────────────────────────────────────────

async def test_composite_stores_parent_row():
    """The original composite row is still stored."""
    await mem.tool_record("Brian", "mood", {"mood": "calm", "confidence": 0.91})
    db = mem.get_db()
    rows = db.execute(
        "SELECT * FROM readings WHERE metric='mood'"
    ).fetchall()
    db.close()
    assert len(rows) == 1
    assert rows[0]["value_type"] == "composite"


async def test_composite_decomposes_numeric_subfield():
    """Float sub-field becomes a numeric child reading."""
    await mem.tool_record("Brian", "mood", {"mood": "calm", "confidence": 0.91})
    db = mem.get_db()
    r = db.execute(
        "SELECT * FROM readings WHERE metric='mood.confidence'"
    ).fetchone()
    db.close()
    assert r is not None
    assert r["value_type"] == "numeric"
    assert abs(r["value_num"] - 0.91) < 1e-6


async def test_composite_decomposes_categorical_subfield():
    """String sub-field becomes a categorical child reading."""
    await mem.tool_record("Brian", "mood", {"mood": "calm", "confidence": 0.91})
    db = mem.get_db()
    r = db.execute(
        "SELECT * FROM readings WHERE metric='mood.mood'"
    ).fetchone()
    db.close()
    assert r is not None
    assert r["value_type"] == "categorical"
    assert r["value_cat"] == "calm"


async def test_composite_uses_dotted_metric_name():
    """Child metric name is {parent}.{key}."""
    await mem.tool_record("Brian", "vitals", {"heart_rate": 72, "spo2": 98.5})
    db = mem.get_db()
    metrics = {r["metric"] for r in db.execute(
        "SELECT metric FROM readings"
    ).fetchall()}
    db.close()
    assert "vitals.heart_rate" in metrics
    assert "vitals.spo2" in metrics


async def test_composite_non_composite_unchanged():
    """Numeric and categorical readings are not decomposed."""
    await mem.tool_record("Brian", "temperature", 71.4, unit="F")
    db = mem.get_db()
    rows = db.execute("SELECT metric FROM readings").fetchall()
    db.close()
    assert len(rows) == 1
    assert rows[0]["metric"] == "temperature"


async def test_composite_nested_dict_skipped():
    """Nested dict values are skipped — only scalar sub-fields are decomposed."""
    await mem.tool_record("Brian", "data", {"value": 42, "nested": {"a": 1}})
    db = mem.get_db()
    metrics = {r["metric"] for r in db.execute(
        "SELECT metric FROM readings"
    ).fetchall()}
    db.close()
    assert "data.value" in metrics
    assert "data.nested" not in metrics
    assert "data.nested.a" not in metrics


async def test_composite_all_subfields_share_timestamp():
    """Parent and child rows all have the same ts."""
    now = time.time()
    await mem.tool_record("Brian", "mood", {"mood": "happy", "energy": 0.8}, ts=now)
    db = mem.get_db()
    rows = db.execute("SELECT ts FROM readings").fetchall()
    db.close()
    for r in rows:
        assert abs(r["ts"] - now) < 0.01


async def test_composite_source_inherited_by_children():
    """Children inherit the source of the parent recording."""
    await mem.tool_record("Brian", "mood", {"mood": "focused"}, source="avatar")
    db = mem.get_db()
    rows = db.execute(
        "SELECT source FROM readings WHERE metric='mood.mood'"
    ).fetchall()
    db.close()
    assert len(rows) == 1
    assert rows[0]["source"] == "avatar"


async def test_composite_child_queryable_via_query_stream():
    """Decomposed sub-fields can be queried through tool_query_stream."""
    await mem.tool_record("Brian", "mood", {"mood": "calm", "confidence": 0.91})
    result = await mem.tool_query_stream("Brian", "mood.confidence")
    assert "0.91" in result


async def test_composite_child_detectable_in_rollups():
    """After _build_rollups, child metrics appear in reading_rollups."""
    t = time.time()
    for i in range(5):
        await mem.tool_record(
            "Brian", "mood",
            {"mood": "calm", "confidence": 0.8 + i * 0.02},
            ts=t - i * 3600,
        )
    await mem._build_rollups()
    db = mem.get_db()
    rollup = db.execute(
        "SELECT * FROM reading_rollups WHERE metric='mood.confidence'"
    ).fetchone()
    db.close()
    assert rollup is not None
    assert rollup["avg_num"] is not None


async def test_composite_integer_subfield_treated_as_numeric():
    """Integer sub-fields are stored as numeric (not categorical)."""
    await mem.tool_record("Brian", "vitals", {"heart_rate": 72})
    db = mem.get_db()
    r = db.execute(
        "SELECT value_type, value_num FROM readings WHERE metric='vitals.heart_rate'"
    ).fetchone()
    db.close()
    assert r["value_type"] == "numeric"
    assert r["value_num"] == 72.0


async def test_composite_boolean_subfield_treated_as_categorical():
    """Boolean sub-fields become categorical strings."""
    await mem.tool_record("Brian", "status", {"active": True})
    db = mem.get_db()
    r = db.execute(
        "SELECT value_type, value_cat FROM readings WHERE metric='status.active'"
    ).fetchone()
    db.close()
    assert r["value_type"] == "categorical"
    assert r["value_cat"] == "True"
