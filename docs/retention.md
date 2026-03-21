# Retention Policy

Raw sensor readings accumulate indefinitely without a retention policy. The `_prune_readings()` function implements a configurable cutoff that runs automatically with the pattern engine.

## What gets deleted

**Deleted:** Rows in the `readings` table older than `RETENTION_DAYS`.

**Never deleted:**
- `reading_rollups` — pre-aggregated stats. These are permanent historical summaries.
- `memories` — semantic facts and promoted insight memories.
- `promoted_patterns` — dedup records for the pattern engine.
- `entities`, `relations`, `schedule_events` — identity and relationship data.

The principle: lose the raw samples, keep the statistics and the learned knowledge.

## Configuration

In `server.py`:

```python
RETENTION_DAYS = 30   # change to suit your storage budget
```

Restart the server for the change to take effect. The next pattern engine run will apply the new cutoff.

## Triggering a prune

**Automatically:** The pattern engine calls `_prune_readings()` at the end of each hourly run.

**Via MCP tool:**
```python
result = await tool_prune()
# → "Pruned 142 readings older than 30 days."
```

**Via HTTP API:**
```bash
curl -X POST http://localhost:8900/prune
# → {"result": "Pruned 142 readings older than 30 days.", "ok": true}
```

**Via Admin UI:** Dashboard and Readings pages both have a "Prune old readings" button that POSTs to `/admin/prune` via HTMX and shows an inline result.

## Storage estimates

A numeric reading row is approximately 100–150 bytes in SQLite. At one reading per minute per metric:

| Retention | Readings/metric | Storage/metric |
|---|---|---|
| 7 days | ~10,000 | ~1.5 MB |
| 30 days | ~43,000 | ~6 MB |
| 90 days | ~130,000 | ~20 MB |

Rollups are far smaller — one row per hour/day/week bucket per metric, regardless of how many raw readings were in that bucket.

## Rolling back retention changes

If you increase `RETENTION_DAYS` after data has already been pruned, the deleted readings cannot be recovered. If you decrease it, future prune runs will remove more data but existing data is untouched until the next run.
