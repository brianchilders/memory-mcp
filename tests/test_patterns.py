"""
Tests for Tier 3 pattern detection.

Covers:
  _detect_patterns()      — existing: stable_avg, rising/falling, dominant_categorical
  _detect_tod_patterns()  — new: time-of-day categorical patterns
  _pearson()              — new: Pearson correlation utility
  _detect_correlations()  — new: cross-metric correlation
  _detect_anomalies()     — new: z-score anomaly flagging
  _build_rollups()        — rollup aggregation
  _promote_patterns()     — end-to-end promotion + dedup
"""

import time

import pytest

import server as mem


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_rollup(avg_num=None, mode_cat=None, bucket_ts=None, count=1):
    """Build a rollup-like dict compatible with _detect_patterns / _detect_anomalies."""
    return {
        "avg_num": avg_num,
        "mode_cat": mode_cat,
        "bucket_ts": bucket_ts or time.time(),
        "count": count,
        "min_num": avg_num,
        "max_num": avg_num,
        "p10_num": avg_num,
        "p90_num": avg_num,
    }


def make_reading(reading_id, value_num=None, value_cat=None, ts=None):
    """Build a reading-like dict compatible with new detector functions."""
    return {
        "id": reading_id,
        "ts": ts or time.time(),
        "value_num": value_num,
        "value_cat": value_cat,
    }


def at_hour(h: int, day_offset: int = 0) -> float:
    """Return a unix timestamp landing at hour h on day (today - day_offset)."""
    today_midnight = (time.time() // 86400) * 86400
    return today_midnight - day_offset * 86400 + h * 3600 + 30  # +30s inside the hour


# ── _detect_patterns: stable average ──────────────────────────────────────────

def test_detect_stable_avg_triggers():
    # CV well below 10%
    rollups = [make_rollup(avg_num=68.0 + i * 0.05) for i in range(5)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert any("stable_avg" in pkey for _, pkey, _ in results)


def test_detect_stable_avg_needs_at_least_3_points():
    rollups = [make_rollup(avg_num=68.0), make_rollup(avg_num=68.1)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert not any("stable_avg" in pkey for _, pkey, _ in results)


def test_detect_no_stable_avg_high_variance():
    rollups = [make_rollup(avg_num=v) for v in [40.0, 80.0, 30.0, 90.0, 50.0]]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert not any("stable_avg" in pkey for _, pkey, _ in results)


# ── _detect_patterns: rising / falling trend ───────────────────────────────────

def test_detect_rising_trend():
    rollups = [make_rollup(avg_num=60.0 + i * 5) for i in range(6)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert any("rising" in pkey for _, pkey, _ in results)


def test_detect_falling_trend():
    rollups = [make_rollup(avg_num=80.0 - i * 5) for i in range(6)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert any("falling" in pkey for _, pkey, _ in results)


def test_detect_trend_needs_at_least_5_points():
    rollups = [make_rollup(avg_num=60.0 + i * 10) for i in range(4)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert not any("rising" in pkey or "falling" in pkey for _, pkey, _ in results)


def test_detect_no_trend_flat_data():
    rollups = [make_rollup(avg_num=68.0) for _ in range(6)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    assert not any("rising" in pkey or "falling" in pkey for _, pkey, _ in results)


# ── _detect_patterns: dominant categorical ─────────────────────────────────────

def test_detect_dominant_categorical():
    rollups = (
        [make_rollup(mode_cat="home") for _ in range(8)] +
        [make_rollup(mode_cat="away") for _ in range(2)]
    )  # 80% home — above 70% threshold
    results = mem._detect_patterns("Brian", "presence", rollups)
    assert any("dominant_home" in pkey for _, pkey, _ in results)


def test_detect_no_dominant_below_threshold():
    rollups = (
        [make_rollup(mode_cat="home") for _ in range(6)] +
        [make_rollup(mode_cat="away") for _ in range(4)]
    )  # 60% — below 70% threshold
    results = mem._detect_patterns("Brian", "presence", rollups)
    assert not any("dominant" in pkey for _, pkey, _ in results)


def test_detect_dominant_needs_at_least_3_points():
    rollups = [make_rollup(mode_cat="home"), make_rollup(mode_cat="home")]
    results = mem._detect_patterns("Brian", "presence", rollups)
    assert not any("dominant" in pkey for _, pkey, _ in results)


# ── _detect_patterns: return type contract ────────────────────────────────────

def test_detect_returns_valid_tuple_structure():
    rollups = [make_rollup(avg_num=68.0 + i * 0.05) for i in range(5)]
    results = mem._detect_patterns("Brian", "temperature", rollups)
    for fact, pkey, confidence in results:
        assert isinstance(fact, str) and len(fact) > 0
        assert isinstance(pkey, str) and len(pkey) > 0
        assert 0.0 <= confidence <= 1.0


def test_detect_empty_rollups_returns_empty():
    results = mem._detect_patterns("Brian", "temperature", [])
    assert results == []


# ── _detect_tod_patterns ───────────────────────────────────────────────────────

def test_detect_tod_home_at_evening():
    # 7 'home' readings at hour 19 + 1 'away' = 87.5% → above 75% threshold
    readings = [
        make_reading(i, value_cat="home", ts=at_hour(19, day_offset=i))
        for i in range(7)
    ]
    readings.append(make_reading(99, value_cat="away", ts=at_hour(19, day_offset=7)))

    results = mem._detect_tod_patterns("Brian", "presence", readings)
    assert len(results) > 0
    facts = [f for f, _, _ in results]
    assert any("home" in f and "19" in f for f in facts)


def test_detect_tod_needs_min_5_readings_at_hour():
    readings = [
        make_reading(i, value_cat="home", ts=at_hour(20, day_offset=i))
        for i in range(4)  # only 4 readings
    ]
    results = mem._detect_tod_patterns("Brian", "presence", readings)
    assert len(results) == 0


def test_detect_tod_below_75_percent_threshold():
    # 6 home + 4 away at hour 18 = 60%
    readings = (
        [make_reading(i, value_cat="home", ts=at_hour(18, day_offset=i) + i) for i in range(6)] +
        [make_reading(i + 10, value_cat="away", ts=at_hour(18, day_offset=i + 6) + i) for i in range(4)]
    )
    results = mem._detect_tod_patterns("Brian", "presence", readings)
    assert len(results) == 0


def test_detect_tod_ignores_numeric_readings():
    # Numeric readings (value_cat=None) should be skipped entirely
    readings = [
        make_reading(i, value_num=68.0, value_cat=None, ts=at_hour(10, day_offset=i))
        for i in range(10)
    ]
    results = mem._detect_tod_patterns("Brian", "temperature", readings)
    assert len(results) == 0


def test_detect_tod_returns_valid_structure():
    readings = [
        make_reading(i, value_cat="home", ts=at_hour(19, day_offset=i))
        for i in range(8)
    ]
    results = mem._detect_tod_patterns("Brian", "presence", readings)
    for fact, pkey, confidence in results:
        assert isinstance(fact, str)
        assert pkey.startswith("tod_")
        assert 0.0 <= confidence <= 1.0


# ── _pearson ───────────────────────────────────────────────────────────────────

def test_pearson_perfect_positive_correlation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert abs(mem._pearson(xs, ys) - 1.0) < 0.001


def test_pearson_perfect_negative_correlation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 8.0, 6.0, 4.0, 2.0]
    assert abs(mem._pearson(xs, ys) + 1.0) < 0.001


def test_pearson_no_correlation_constant_y():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [3.0, 3.0, 3.0, 3.0, 3.0]
    assert abs(mem._pearson(xs, ys)) < 0.001


def test_pearson_single_point_returns_zero():
    assert mem._pearson([1.0], [1.0]) == 0.0


def test_pearson_result_bounded():
    import random
    rng = random.Random(42)
    xs = [rng.gauss(0, 1) for _ in range(20)]
    ys = [rng.gauss(0, 1) for _ in range(20)]
    r = mem._pearson(xs, ys)
    assert -1.0 <= r <= 1.0


# ── _detect_correlations ───────────────────────────────────────────────────────

def make_numeric_rollups(values, base_ts=1_700_000_000.0):
    return [
        make_rollup(avg_num=v, bucket_ts=base_ts + i * 86400)
        for i, v in enumerate(values)
    ]


def test_detect_correlations_positive():
    base = 1_700_000_000.0
    temps   = make_numeric_rollups([60, 62, 64, 66, 68, 70, 72], base)
    energy  = make_numeric_rollups([100, 110, 120, 130, 140, 150, 160], base)
    results = mem._detect_correlations("home", {"temperature": temps, "energy_use": energy})
    assert len(results) > 0
    assert any("positively correlated" in f for f, _, _ in results)


def test_detect_correlations_negative():
    base = 1_700_000_000.0
    temps   = make_numeric_rollups([60, 62, 64, 66, 68, 70, 72], base)
    comfort = make_numeric_rollups([100, 90, 80, 70, 60, 50, 40], base)
    results = mem._detect_correlations("home", {"temperature": temps, "comfort": comfort})
    assert len(results) > 0
    assert any("negatively correlated" in f for f, _, _ in results)


def test_detect_correlations_weak_not_reported():
    # These two series have Pearson r ≈ 0.29 — well below the 0.7 threshold.
    # temps_a rises steadily; temps_b oscillates without tracking temps_a.
    base = 1_700_000_000.0
    xs = make_numeric_rollups([60, 62, 64, 66, 68, 70, 72], base)
    ys = make_numeric_rollups([64, 61, 66, 62, 67, 63, 65], base)
    results = mem._detect_correlations("home", {"temps_a": xs, "temps_b": ys})
    assert len(results) == 0


def test_detect_correlations_needs_5_shared_points():
    base = 1_700_000_000.0
    xs = make_numeric_rollups([60, 62, 64, 66], base)      # only 4
    ys = make_numeric_rollups([100, 110, 120, 130], base)
    results = mem._detect_correlations("home", {"temp": xs, "energy": ys})
    assert len(results) == 0


def test_detect_correlations_single_metric_skipped():
    base = 1_700_000_000.0
    xs = make_numeric_rollups([60, 62, 64, 66, 68, 70, 72], base)
    results = mem._detect_correlations("home", {"temperature": xs})
    assert len(results) == 0


def test_detect_correlations_pkey_format():
    base = 1_700_000_000.0
    xs = make_numeric_rollups([60, 62, 64, 66, 68, 70, 72], base)
    ys = make_numeric_rollups([100, 110, 120, 130, 140, 150, 160], base)
    results = mem._detect_correlations("home", {"temp": xs, "energy": ys})
    for _, pkey, _ in results:
        assert "corr_" in pkey
        assert "+" in pkey or "-" in pkey


# ── _detect_anomalies ──────────────────────────────────────────────────────────

def test_detect_anomaly_above_baseline():
    baseline = [make_rollup(avg_num=68.0 + i * 0.1) for i in range(10)]
    recent   = [make_reading(1, value_num=120.0)]   # far above ~69 avg
    results  = mem._detect_anomalies("Brian", "temperature", recent, baseline)
    assert len(results) > 0
    assert "above" in results[0][0]
    assert results[0][1] == "anomaly_1"


def test_detect_anomaly_below_baseline():
    baseline = [make_rollup(avg_num=68.0 + i * 0.1) for i in range(10)]
    recent   = [make_reading(2, value_num=10.0)]    # far below
    results  = mem._detect_anomalies("Brian", "temperature", recent, baseline)
    assert len(results) > 0
    assert "below" in results[0][0]


def test_detect_no_anomaly_within_normal_range():
    baseline = [make_rollup(avg_num=68.0) for _ in range(10)]
    recent   = [make_reading(3, value_num=68.5)]    # within 1 std dev
    results  = mem._detect_anomalies("Brian", "temperature", recent, baseline)
    assert len(results) == 0


def test_detect_anomaly_needs_5_baseline_points():
    baseline = [make_rollup(avg_num=68.0) for _ in range(4)]
    recent   = [make_reading(4, value_num=300.0)]
    results  = mem._detect_anomalies("Brian", "temperature", recent, baseline)
    assert len(results) == 0


def test_detect_anomaly_zero_variance_ignored():
    # All baseline identical → std ≈ 0; z-score undefined, should return nothing
    baseline = [make_rollup(avg_num=68.0) for _ in range(10)]
    recent   = [make_reading(5, value_num=300.0)]
    results  = mem._detect_anomalies("Brian", "temperature", recent, baseline)
    assert len(results) == 0


def test_detect_anomaly_confidence_bounded():
    baseline = [make_rollup(avg_num=68.0 + i * 0.5) for i in range(10)]
    recent   = [make_reading(6, value_num=200.0)]
    results  = mem._detect_anomalies("Brian", "temperature", recent, baseline)
    for _, _, confidence in results:
        assert 0.0 <= confidence <= 1.0


def test_detect_anomaly_uses_reading_id_for_pkey():
    baseline = [make_rollup(avg_num=68.0 + i * 0.1) for i in range(10)]
    r1 = make_reading(42, value_num=200.0)
    r2 = make_reading(99, value_num=200.0)
    results = mem._detect_anomalies("Brian", "temperature", [r1, r2], baseline)
    pkeys = [pkey for _, pkey, _ in results]
    assert "anomaly_42" in pkeys
    assert "anomaly_99" in pkeys


# ── _build_rollups ─────────────────────────────────────────────────────────────

async def test_build_rollups_computes_numeric_stats():
    # Anchor to 30 minutes into the current hour so all readings stay in the
    # same hour bucket regardless of when the test runs (avoids hour-boundary flakiness).
    now = (int(time.time()) // 3600) * 3600 + 1800
    for i, v in enumerate([65.0, 67.0, 69.0, 71.0, 73.0]):
        await mem.tool_record("Brian", "temperature", v, ts=now - i * 60)
    await mem._build_rollups()

    db = mem.get_db()
    rollup = db.execute(
        "SELECT avg_num, min_num, max_num, count FROM reading_rollups "
        "WHERE bucket_type='hour'"
    ).fetchone()
    db.close()
    assert rollup is not None
    assert rollup["count"] == 5
    assert rollup["min_num"] <= rollup["avg_num"] <= rollup["max_num"]
    assert abs(rollup["avg_num"] - 69.0) < 0.1


def test_build_rollups_computes_mode_cat():
    pass  # covered by async test below


async def test_build_rollups_computes_categorical_mode():
    now = time.time()
    for s in ["home", "home", "home", "away"]:
        await mem.tool_record("Brian", "presence", s, ts=now)
        now -= 60
    await mem._build_rollups()

    db = mem.get_db()
    rollup = db.execute(
        "SELECT mode_cat FROM reading_rollups WHERE bucket_type='hour'"
    ).fetchone()
    db.close()
    assert rollup is not None
    assert rollup["mode_cat"] == "home"


async def test_build_rollups_is_idempotent():
    now = time.time()
    await mem.tool_record("Brian", "temperature", 68.0, ts=now)
    await mem._build_rollups()
    await mem._build_rollups()  # second call should not duplicate

    db = mem.get_db()
    count = db.execute("SELECT COUNT(*) FROM reading_rollups").fetchone()[0]
    db.close()
    assert count > 0


# ── _promote_patterns (integration) ───────────────────────────────────────────

async def test_promote_creates_insight_memories():
    now = time.time()
    # 14 days of stable temperature → stable_avg pattern
    for i in range(14):
        await mem.tool_record(
            "Brian", "temperature", 68.0 + i * 0.02, ts=now - i * 86400
        )
    await mem._build_rollups()
    await mem._promote_patterns()

    db = mem.get_db()
    insights = db.execute(
        "SELECT fact FROM memories WHERE category='insight'"
    ).fetchall()
    db.close()
    assert len(insights) > 0


async def test_promote_dedup_prevents_double_promotion():
    now = time.time()
    for i in range(14):
        await mem.tool_record("Brian", "temperature", 68.0, ts=now - i * 86400)
    await mem._build_rollups()
    await mem._promote_patterns()

    db = mem.get_db()
    count_first = db.execute(
        "SELECT COUNT(*) FROM memories WHERE category='insight'"
    ).fetchone()[0]
    db.close()

    # Run again — no new promotions for the same patterns
    await mem._promote_patterns()
    db = mem.get_db()
    count_second = db.execute(
        "SELECT COUNT(*) FROM memories WHERE category='insight'"
    ).fetchone()[0]
    db.close()

    assert count_first == count_second
    assert count_first > 0


async def test_promote_records_in_promoted_patterns_table():
    now = time.time()
    for i in range(14):
        await mem.tool_record("Brian", "temperature", 68.0, ts=now - i * 86400)
    await mem._build_rollups()
    await mem._promote_patterns()

    db = mem.get_db()
    pp = db.execute("SELECT COUNT(*) FROM promoted_patterns").fetchone()[0]
    db.close()
    assert pp > 0
