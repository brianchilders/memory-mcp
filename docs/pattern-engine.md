# Pattern Engine

The pattern engine is a background asyncio task that runs once per hour (configurable via `PATTERN_INTERVAL`). It closes the loop between raw sensor data (Tier 2) and searchable semantic memory (Tier 1):

```
Raw readings → rollups → pattern detection → insight memories
```

## How it runs

`pattern_engine_loop()` in `server.py`:

1. Waits 60 seconds after startup to let the server settle
2. Calls `_build_rollups()` — aggregates raw readings into hour/day/week buckets
3. Calls `_promote_patterns()` — runs all detectors and promotes new patterns as insight memories
4. Calls `_prune_readings()` — deletes raw readings older than `RETENTION_DAYS`
5. Sleeps `PATTERN_INTERVAL` seconds, then repeats

## Detectors

### `_detect_patterns(entity_name, metric, rollups)` — existing

Takes daily rollup rows and returns a list of `(fact, pattern_key, confidence)` tuples.

| Detector | Trigger | Example fact |
|---|---|---|
| Stable average | CV < 10% over ≥3 days | "Brian's temperature is consistently around 68.0 (std=0.1, stable over 7 days)" |
| Rising trend | Second half avg > first half avg by >15%, ≥5 days | "Brian's weight has been rising (180.0 → 185.0, +3% over 6 days)" |
| Falling trend | Same, but declining | "bedroom temperature has been falling (72.0 → 68.0, -6% over 5 days)" |
| Dominant categorical | One value ≥70% of days | "Brian's presence is predominantly 'home' (85% of 7 days)" |

### `_detect_tod_patterns(entity_name, metric, readings)` — new

Groups raw categorical readings by hour-of-day. For each hour with ≥5 readings where one category accounts for ≥75%, emits a pattern.

| Parameter | Value |
|---|---|
| Minimum readings per hour | 5 |
| Dominant threshold | 75% |
| Dedup key format | `tod_{HH}_{value}` |

Example: "Brian's presence is 'home' at 19:00 (87% of 8 readings)"

### `_detect_correlations(entity_name, metrics_rollups)` — new

Computes pairwise Pearson correlation between all numeric metrics for an entity. Requires ≥5 shared day-buckets and `|r| ≥ 0.7`.

| Parameter | Value |
|---|---|
| Minimum shared days | 5 |
| Correlation threshold | |r| ≥ 0.70 |
| Dedup key format | `corr_{metric_a}_{metric_b}_{+/-}` |

Example: "home's temperature and energy_use are positively correlated (r=0.94, n=14 days)"

### `_detect_anomalies(entity_name, metric, recent_readings, baseline_rollups)` — new

Flags individual numeric readings that deviate ≥3 standard deviations from the baseline mean (computed from daily rollups). Each anomalous reading gets a unique dedup key based on its row ID, so it is only promoted once.

Returns nothing if the baseline has <5 points or zero variance.

| Parameter | Value |
|---|---|
| Minimum baseline points | 5 |
| z-score threshold | ≥ 3.0 |
| Dedup key format | `anomaly_{reading_id}` |

Example: "Anomaly: Brian's temperature was 102.3 at 2024-01-15 14:32 (4.2 std devs above normal 68.0)"

## Dedup via `promoted_patterns`

Every promotion is recorded in the `promoted_patterns` table with a deterministic `pattern_key`. Before promoting, the engine checks for an existing row — if found, the pattern is skipped. This means:

- Stable patterns are only written as memories once, no matter how many times the engine runs
- Anomaly memories are keyed by reading ID, so a one-off spike is promoted once and never again
- Correlation and TOD patterns re-check on every run; if the pattern key hasn't changed, no duplicate is written

## Adding a new detector

1. Write a pure function: `def _detect_*(entity_name, metric, data) -> list[tuple]:`
   - Returns `[(fact_str, pattern_key_str, confidence_float), ...]`
   - `pattern_key` must be deterministic and unique per pattern type
   - `confidence` should be in [0.0, 1.0]
2. Add tests in `tests/test_patterns.py`
3. Call it from `_promote_patterns()` via `await _maybe_promote(db, eid, metric, fact, pkey, conf)`

The existing `_maybe_promote()` helper handles dedup, embedding, and DB insertion — your detector only needs to identify the pattern.

## Configuration

| Constant | Default | Effect |
|---|---|---|
| `PATTERN_INTERVAL` | `3600` | Seconds between engine runs |
| `RETENTION_DAYS` | `30` | Raw readings older than this are pruned each run |
