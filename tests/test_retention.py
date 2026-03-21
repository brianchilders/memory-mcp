"""
Tests for the retention policy (item 6).

_prune_readings() deletes raw readings older than RETENTION_DAYS.
Rollups are never deleted — they represent permanent aggregate history.
"""

import time

import server as mem


async def test_prune_deletes_old_readings():
    old_ts    = time.time() - (mem.RETENTION_DAYS + 5) * 86400
    recent_ts = time.time() - 3600

    await mem.tool_record("Brian", "temperature", 68.0, ts=old_ts)
    await mem.tool_record("Brian", "temperature", 70.0, ts=recent_ts)

    result = await mem.tool_prune()
    assert "1" in result  # reported 1 pruned

    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    assert count == 1  # only the recent one remains


async def test_prune_keeps_rollups():
    old_ts = time.time() - (mem.RETENTION_DAYS + 5) * 86400
    await mem.tool_record("Brian", "temperature", 68.0, ts=old_ts)
    await mem._build_rollups()

    await mem.tool_prune()

    db = mem.get_db()
    rollup_count = db.execute("SELECT COUNT(*) FROM reading_rollups").fetchone()[0]
    db.close()
    assert rollup_count > 0


async def test_prune_keeps_recent_readings():
    recent_ts = time.time() - 3600
    await mem.tool_record("Brian", "temperature", 70.0, ts=recent_ts)

    await mem.tool_prune()

    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    assert count == 1


async def test_prune_returns_zero_when_nothing_to_delete():
    await mem.tool_record("Brian", "temperature", 70.0)  # just now

    result = await mem.tool_prune()
    assert "0" in result


async def test_prune_boundary_old_side_deleted():
    # Exactly 1 second before the cutoff → should be deleted
    cutoff = time.time() - mem.RETENTION_DAYS * 86400
    await mem.tool_record("Brian", "temperature", 68.0, ts=cutoff - 1)

    await mem.tool_prune()

    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    assert count == 0


async def test_prune_boundary_recent_side_kept():
    # Exactly 1 second after the cutoff → should survive
    cutoff = time.time() - mem.RETENTION_DAYS * 86400
    await mem.tool_record("Brian", "temperature", 70.0, ts=cutoff + 1)

    await mem.tool_prune()

    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    assert count == 1


async def test_prune_empty_db_is_safe():
    result = await mem.tool_prune()
    assert isinstance(result, str)
    assert "0" in result


async def test_prune_multiple_entities():
    old_ts = time.time() - (mem.RETENTION_DAYS + 1) * 86400
    await mem.tool_record("Brian",       "temperature", 68.0, ts=old_ts)
    await mem.tool_record("living_room", "temperature", 70.0, ts=old_ts, entity_type="room")
    await mem.tool_record("Brian",       "temperature", 72.0)  # recent

    result = await mem.tool_prune()
    assert "2" in result  # 2 old readings removed

    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    assert count == 1


async def test_prune_does_not_touch_memories():
    old_ts = time.time() - (mem.RETENTION_DAYS + 5) * 86400
    await mem.tool_record("Brian", "temperature", 68.0, ts=old_ts)
    await mem.tool_remember("Brian", "Likes coffee")

    await mem.tool_prune()

    db = mem.get_db()
    mem_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    db.close()
    assert mem_count == 1  # memory survives prune


async def test_prune_does_not_touch_schedule_events():
    old_ts = time.time() - (mem.RETENTION_DAYS + 5) * 86400
    await mem.tool_record("Brian", "temperature", 68.0, ts=old_ts)
    await mem.tool_schedule("Brian", "Annual review", start_ts=time.time() + 86400)

    await mem.tool_prune()

    db = mem.get_db()
    ev_count = db.execute("SELECT COUNT(*) FROM schedule_events").fetchone()[0]
    db.close()
    assert ev_count == 1
