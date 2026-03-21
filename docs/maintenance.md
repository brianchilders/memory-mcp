# Maintenance & Operations

How to keep memory-mcp healthy over time — what runs automatically, what needs
occasional attention, and how to handle the common operational events like
model swaps and upgrades.

---

## The short version

Most maintenance is automatic. The pattern engine prunes old readings and
checkpoints the database on its own schedule. Under normal operation there are
only four things you ever need to do manually:

| Task | Frequency | Effort |
|---|---|---|
| Back up `memory.db` | Weekly (or daily if you push a lot of data) | One cron line |
| Upgrade the codebase | When you want new features or fixes | `git pull` + restart |
| Rotate the API token | If you suspect it has been exposed | Admin settings page |
| Re-embed memories | Only when changing the embedding model | `python reembed.py` |

If you are using Docker Compose, backups are the only thing that isn't handled
for you by the container lifecycle.

---

## What runs automatically

### Pattern engine (hourly)

Every `PATTERN_INTERVAL` seconds (default: 3600), the pattern engine:

1. Builds or updates rollup aggregates (hour/day/week buckets) for any metrics
   that have new readings since the last run
2. Runs four detectors against the rollup data — stable average, trend,
   time-of-day, anomaly, dominant categorical, and correlation
3. Promotes any newly detected stable patterns as `insight` memories into Tier 1
4. Prunes raw readings older than `RETENTION_DAYS` (default: 30 days)
5. Runs a WAL checkpoint to keep the database file size under control

You don't need to trigger any of this. As long as `api.py` is running, it
happens in the background.

### Contradiction detection (on every `remember` call)

When a new memory is stored, it is automatically compared against existing
memories for the same entity. If a semantically similar memory is found (cosine
similarity ≥ 0.85), the older one is marked `superseded_by` the new one.
Superseded memories are hidden from all query results but kept in the database
for audit purposes.

This means you don't need to manually delete outdated facts — storing a new
fact about the same topic replaces the old one automatically.

---

## Backups

The entire state of memory-mcp lives in a single SQLite file — `memory.db` (or
wherever `MEMORY_DB_PATH` points). Backing up is a file copy, but you must use
SQLite's online backup mechanism to get a consistent snapshot while the server
is running:

```bash
sqlite3 /var/lib/memory-mcp/memory.db \
  ".backup '/var/backups/memory-mcp/memory-$(date +%Y%m%d).db'"
```

This uses SQLite's built-in hot backup, which is safe to run while the server
is actively writing. A plain `cp` is not safe on a live database.

**Automate with cron** — daily backup, 14-day retention:

```bash
# /etc/cron.d/memory-mcp-backup
0 3 * * * your-username \
  sqlite3 /var/lib/memory-mcp/memory.db \
  ".backup '/var/backups/memory-mcp/memory-$(date +\%Y\%m\%d).db'" && \
  find /var/backups/memory-mcp -name "memory-*.db" -mtime +14 -delete
```

**Verify a backup is valid:**

```bash
sqlite3 /var/backups/memory-mcp/memory-20260321.db "PRAGMA integrity_check;"
# Should print: ok
```

**Restore from backup:**

```bash
# Stop the server first
sudo systemctl stop memory-mcp

# Replace the live database
cp /var/backups/memory-mcp/memory-20260321.db /var/lib/memory-mcp/memory.db

# Restart
sudo systemctl start memory-mcp
```

---

## Upgrading

```bash
# 1. Pull the latest code
git pull

# 2. Run the test suite to catch anything unexpected
python -m pytest --tb=short -q

# 3. Restart the service
sudo systemctl restart memory-mcp
# or: docker compose pull && docker compose up -d
```

**Schema migrations happen automatically.** The server calls `_apply_migrations()`
on every startup, which adds new columns and tables idempotently. You never need
to run manual SQL between versions.

**The one exception:** if an upgrade changes the embedding model or dimension
defaults, you will need to re-embed. The upgrade notes will say so explicitly.
See [Swapping embedding models](#swapping-embedding-models) below.

---

## Swapping embedding models

This is the most involved maintenance operation. It's also entirely optional
unless you want to change which embedding model memory-mcp uses.

### When you'd actually do this

- You started with `nomic-embed-text` (768-dim) and want richer embeddings —
  for example `mxbai-embed-large` (1024-dim) or `text-embedding-3-small` (1536-dim)
- You're moving from a local Ollama model to an OpenAI model, or vice versa
- A new Ollama model is released that significantly outperforms the current one
- You're switching AI providers entirely (e.g. from Ollama to OpenAI)

### Why you can't just change the env var and restart

The embedding dimension is baked into the `memory_vectors` SQLite virtual table
at creation time. Every row in that table is a fixed-width vector blob. If you
change to a model that outputs 1536 dimensions but the table was built for 768,
every query will fail with a dimension mismatch error.

There's also a semantic reason: embeddings from different models are not
comparable. A cosine similarity score between a `nomic-embed-text` vector and a
`text-embedding-3-small` vector is meaningless. All memories must be in the same
embedding space for recall to work correctly.

`reembed.py` handles both problems — it drops and rebuilds `memory_vectors` with
the new dimension and re-embeds every memory from scratch using the new model.

### What reembed.py does

1. Connects to the database and reads every row in `memories`
2. Drops the existing `memory_vectors` virtual table
3. Recreates it with the new `EMBED_DIM`
4. Calls the embedding API for each memory fact in batches
5. Writes the new vectors back into `memory_vectors`

**What it does not touch:** entities, memories, readings, rollups, relations,
sessions, promoted patterns, schedule events. Only the vector index is rebuilt.
Your data is safe.

### Full procedure

```bash
# 1. Stop the server (prevents writes during re-embed)
sudo systemctl stop memory-mcp

# 2. Update the model and dimension (in .env or /etc/memory-mcp/env)
MEMORY_EMBED_MODEL=mxbai-embed-large
MEMORY_EMBED_DIM=1024

# 3. Pull the new model if using Ollama
ollama pull mxbai-embed-large

# 4. Dry run — validates the dimension and shows what will happen
python reembed.py --dry-run

# 5. Run the re-embed (takes ~1 second per 100 memories on local Ollama)
python reembed.py

# 6. Restart the server with the new model
sudo systemctl start memory-mcp
```

**How long does it take?** With a local Ollama instance, expect roughly 1–2
seconds per 100 memories. A database with 500 memories takes about 5–10 minutes.
Cloud providers (OpenAI) are faster due to batching but cost API credits.

### Dry run output

The `--dry-run` flag validates the setup without writing anything:

```
Dry run — no changes will be made.
Model    : mxbai-embed-large
Dimension: 1024
Memories : 312
Estimated API calls: 312
Would drop and rebuild memory_vectors (dim=1024)
```

If the dimension doesn't match the model's actual output, the first embedding
call will fail and tell you the correct dimension before anything is written.

### If something goes wrong mid-run

If `reembed.py` is interrupted partway through, the `memory_vectors` table will
be partially populated. Run `python reembed.py` again — it rebuilds from scratch,
so a partial run is safe to retry.

---

## WAL checkpoint

SQLite runs in WAL (Write-Ahead Log) mode, which improves concurrent write
performance. The pattern engine runs a checkpoint automatically after each
hourly pass. Under normal sensor ingestion loads this is sufficient.

For high-frequency sensors (readings every few seconds across many metrics),
the WAL file can grow faster than the hourly checkpoint clears it. Add a weekly
forced checkpoint as a safety net:

```bash
# /etc/cron.d/memory-mcp-wal
0 4 * * 0 your-username \
  sqlite3 /var/lib/memory-mcp/memory.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

You can check the current WAL size any time:

```bash
ls -lh /var/lib/memory-mcp/memory.db-wal
# Anything under ~50 MB is fine. Over 200 MB, run a manual checkpoint.
```

---

## Token rotation

The API bearer token protects all HTTP endpoints. Rotate it if:

- You suspect it has been exposed (e.g. committed to a public repo by mistake)
- You're decommissioning a caller that had access
- It's part of your regular security rotation schedule

**Via the admin UI** (token stored in database):

1. Go to `http://localhost:8900/admin/settings`
2. Click "Regenerate token"
3. Copy the new token — it is shown in full only once
4. Update all callers (HA rest_commands, OpenHome ability config, scripts)

**Via environment variable** (token set via `MEMORY_API_TOKEN`):

Update the value in `/etc/memory-mcp/env` (or `.env`) and restart the service.
The admin UI regenerate button is disabled when the token comes from an env var.

---

## Database health check

Quick verification that the database is intact:

```bash
sqlite3 /var/lib/memory-mcp/memory.db "PRAGMA integrity_check;"
# ok

sqlite3 /var/lib/memory-mcp/memory.db "PRAGMA foreign_key_check;"
# (no output = no violations)
```

Check row counts to confirm data is accumulating as expected:

```bash
sqlite3 /var/lib/memory-mcp/memory.db "
  SELECT 'entities',  COUNT(*) FROM entities  UNION ALL
  SELECT 'memories',  COUNT(*) FROM memories   UNION ALL
  SELECT 'readings',  COUNT(*) FROM readings   UNION ALL
  SELECT 'rollups',   COUNT(*) FROM reading_rollups UNION ALL
  SELECT 'patterns',  COUNT(*) FROM promoted_patterns;
"
```

Or use the HTTP health endpoint:

```bash
curl -s http://localhost:8900/health | python -m json.tool
```

---

## Retention tuning

If disk usage is growing faster than expected, two settings control it
(both in `server.py` — restart required after changing):

```python
RETENTION_DAYS = 30   # raw readings older than this are pruned
```

Decreasing `RETENTION_DAYS` reduces disk use but shortens the window available
for anomaly detection and correlation analysis. The pattern engine needs at least
5 days of readings to detect most patterns — don't go below 7.

Rollup aggregates (hour/day/week buckets) are never pruned automatically. They
are compact (one row per metric per bucket) and grow slowly — a year of daily
rollups for 10 metrics is about 3,600 rows.

See `docs/retention.md` for storage estimates by sensor frequency.

---

## Maintenance calendar

A practical schedule for a home server running memory-mcp continuously:

| When | Task |
|---|---|
| On deploy | Verify `/health` returns `ok`, confirm token printed to logs |
| Weekly | Verify backup cron ran (`ls -lh /var/backups/memory-mcp/`) |
| Monthly | `git pull` + test suite + restart if updates available |
| When prompted | Re-embed after any model swap |
| If disk grows unexpectedly | Check WAL size; reduce `RETENTION_DAYS` if readings table is large |
| If a token is exposed | Regenerate immediately via admin settings or env var |
