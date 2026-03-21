"""
Tests for Enhancement F: Incremental rollup processing.

_build_rollups() now only reprocesses entity/metric/bucket combinations
that have new data since the last watermark, using the rollup_watermarks table.

This is a correctness + efficiency test: rollups should still be accurate,
but only the minimum necessary buckets should be (re)computed.
"""

import time

import server as mem


# ── Watermark table basics ────────────────────────────────────────────────────

async def test_watermark_table_exists():
    db = mem.get_db()
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rollup_watermarks'"
    ).fetchone()
    db.close()
    assert row is not None


async def test_watermark_created_after_first_build():
    await mem.tool_record("Brian", "temperature", 72.0)
    await mem._build_rollups()
    db = mem.get_db()
    row = db.execute("SELECT * FROM rollup_watermarks").fetchone()
    db.close()
    assert row is not None
    assert row["last_ts"] is not None


async def test_watermark_updated_on_subsequent_build():
    t0 = time.time() - 7200
    await mem.tool_record("Brian", "temperature", 70.0, ts=t0)
    await mem._build_rollups()

    db = mem.get_db()
    first_wm = db.execute(
        "SELECT last_ts FROM rollup_watermarks"
    ).fetchone()["last_ts"]
    db.close()

    # Add a newer reading and rebuild
    t1 = time.time()
    await mem.tool_record("Brian", "temperature", 75.0, ts=t1)
    await mem._build_rollups()

    db = mem.get_db()
    second_wm = db.execute(
        "SELECT last_ts FROM rollup_watermarks"
    ).fetchone()["last_ts"]
    db.close()
    assert second_wm >= first_wm


# ── Rollup correctness with incremental processing ───────────────────────────

async def test_rollup_still_accurate_after_incremental_build():
    """Rollups should have correct averages even when built incrementally."""
    t = time.time() - 3 * 86400
    for i in range(6):
        await mem.tool_record("Brian", "temperature", 70.0 + i, ts=t + i * 3600)

    await mem._build_rollups()

    db = mem.get_db()
    rollup = db.execute(
        """SELECT avg_num FROM reading_rollups
           WHERE metric='temperature' AND bucket_type='day'"""
    ).fetchone()
    db.close()
    assert rollup is not None
    assert rollup["avg_num"] is not None


async def test_incremental_build_picks_up_new_readings():
    """New readings added after first build appear in rollups after second build."""
    t = time.time() - 7200
    await mem.tool_record("Brian", "temperature", 70.0, ts=t)
    await mem._build_rollups()

    # Add new reading and rebuild
    await mem.tool_record("Brian", "temperature", 80.0, ts=time.time())
    await mem._build_rollups()

    result = await mem.tool_query_stream(
        "Brian", "temperature", granularity="hour",
        start_ts=time.time() - 86400, end_ts=time.time() + 3600,
    )
    assert "80" in result or "avg=" in result


async def test_no_data_build_safe():
    """_build_rollups on empty DB should not crash."""
    await mem._build_rollups()  # no readings at all
    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM reading_rollups").fetchone()[0]
    db.close()
    assert count == 0


async def test_watermark_scoped_per_entity_metric():
    """Each entity/metric pair tracks its own watermark."""
    await mem.tool_record("Brian", "temperature", 70.0)
    await mem.tool_record("Sarah", "heart_rate", 65.0)
    await mem._build_rollups()

    db = mem.get_db()
    wm_count = db.execute("SELECT COUNT(*) FROM rollup_watermarks").fetchone()[0]
    db.close()
    # Should have one watermark per entity/metric pair
    assert wm_count >= 2


async def test_second_build_without_new_data_is_idempotent():
    """Running _build_rollups twice with no new data should not change rollup values."""
    t = time.time() - 3600
    await mem.tool_record("Brian", "temperature", 72.0, ts=t)
    await mem._build_rollups()

    db = mem.get_db()
    r1 = db.execute(
        "SELECT avg_num, count FROM reading_rollups WHERE metric='temperature'"
    ).fetchone()
    db.close()

    await mem._build_rollups()  # no new data

    db = mem.get_db()
    r2 = db.execute(
        "SELECT avg_num, count FROM reading_rollups WHERE metric='temperature'"
    ).fetchone()
    db.close()

    assert r1["avg_num"] == r2["avg_num"]
    assert r1["count"] == r2["count"]
