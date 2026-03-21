# Troubleshooting

Common errors and how to resolve them.

---

## sqlite-vec won't load

**Symptom:**
```
ImportError: No module named 'sqlite_vec'
```
or
```
AttributeError: module 'sqlite3' has no attribute 'load_extension'
```

**Cause:** `sqlite-vec` is not installed, or your Python build has SQLite
extension loading disabled (common on some Linux distributions).

**Fix:**
```bash
pip install sqlite-vec
python -c "import sqlite_vec; print(sqlite_vec.__version__)"
```

If Python was built without extension support (e.g. Ubuntu's `python3-dev` package):
```bash
# Install Python from deadsnakes or pyenv instead of the system package
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt install python3.12 python3.12-dev python3.12-venv
```

---

## Embedding model not found

**Symptom:**
```
httpx.HTTPStatusError: 404 Not Found
```
or API log shows `model "nomic-embed-text" not found`

**Cause:** The embedding model hasn't been pulled into Ollama yet.

**Fix:**
```bash
ollama pull nomic-embed-text
```

For other providers, verify the model name matches what the provider expects.
See `docs/ai-backend.md` for provider-specific model names.

---

## AI backend unreachable

**Symptom:**
```
httpx.ConnectError: [Errno 111] Connection refused
```
or
```
httpx.ConnectTimeout
```

**Cause:** The AI backend (Ollama or other provider) is not running or the
`MEMORY_AI_BASE_URL` is wrong.

**Fix:**
```bash
# Test Ollama directly
curl http://localhost:11434/v1/models

# Check what base URL the server is using
python -c "import server; print(server.AI_BASE_URL)"

# If using a remote host, verify it's reachable
ping your-ollama-host
```

If Ollama is running but on a different host, update the URL:
```bash
export MEMORY_AI_BASE_URL=http://your-server:11434/v1
```

---

## Embedding dimension mismatch

**Symptom:**
```
sqlite3.OperationalError: vector dimension mismatch
```

**Cause:** The database was created with one embedding model (e.g. 768-dim
`nomic-embed-text`), then you switched to a model with a different dimension
(e.g. 1536-dim `text-embedding-3-small`) without re-embedding.

**Fix — Option A:** Re-embed all existing memories (non-destructive):
```bash
export MEMORY_EMBED_MODEL=text-embedding-3-small
export MEMORY_EMBED_DIM=1536
python reembed.py --dry-run   # preview
python reembed.py             # run
```

**Fix — Option B:** Start fresh (loses existing memories):
```bash
rm memory.db
python server.py   # creates a new database with the new dimension
```

See `docs/ai-backend.md` for the full model swap guide.

---

## Port already in use

**Symptom:**
```
OSError: [Errno 98] Address already in use
```

**Cause:** Something else is already listening on port 8900, or a previous
`api.py` process didn't shut down cleanly.

**Fix:**
```bash
# Find what's using the port
lsof -i :8900
# or
ss -tlnp | grep 8900

# Kill the old process (replace <PID> with the actual PID)
kill <PID>

# Or use a different port
uvicorn api:app --host 0.0.0.0 --port 8901
```

---

## MCP server produces no output

**Symptom:** Running `python server.py` shows nothing and appears to hang.

**Cause:** This is normal. The MCP server uses stdio transport — it reads
JSON-RPC requests from stdin and writes responses to stdout. It won't produce
visible output until it receives a tool call from a client.

To verify it's working, send a test request:
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python server.py
```

---

## recall returns no results

**Symptom:** `tool_recall` or `POST /recall` returns an empty list even though
memories exist.

**Cause A:** No memories exist for that entity yet. Check with:
```bash
curl http://localhost:8900/profile/{entity_name}
```

**Cause B:** The query embedding returned by the AI backend is zero or near-zero,
making cosine similarity undefined. This can happen if the embedding model is
returning malformed output.

**Cause C:** All memories for that entity have been superseded (contradiction
detection marked them with `superseded_by`). Superseded memories are hidden from
recall. Check the admin UI entity detail page — superseded memories show with a
strikethrough.

**Fix:**
```bash
# Verify embeddings are working
python -c "
import asyncio, server
vec = asyncio.run(server.embed('test'))
print('Embedding length:', len(vec))
print('First values:', vec[:3])
"
```

---

## Pattern engine not running

**Symptom:** No promoted patterns appear after hours of readings. No memories
of type "insight" are being created.

**Cause A:** The pattern engine only runs via `api.py` (as a background asyncio
task). It does not run when using `server.py` via MCP alone.

**Cause B:** Insufficient data. Most detectors require ≥3–5 days of readings
before they trigger.

**Cause C:** An unhandled exception in the engine loop. Check the process logs.

**Fix:** Run the HTTP API to get the pattern engine:
```bash
python api.py   # pattern engine starts automatically
```

To check when the pattern engine last ran, watch the server logs for:
```
INFO: Pattern engine: built rollups, promoted patterns, pruned readings
```

---

## Admin UI shows no data

**Symptom:** Admin dashboard shows all zeros, or entity pages are empty.

**Cause A:** You're looking at the wrong server — check the URL and port.

**Cause B:** The database file doesn't exist or is in a different directory than
where the server is running.

**Fix:**
```bash
# Check where the database is
python -c "import server; print(server.DB_PATH)"

# Verify it exists and has data
ls -lh memory.db
sqlite3 memory.db "SELECT count(*) FROM memories;"
```

---

## Database locked

**Symptom:**
```
sqlite3.OperationalError: database is locked
```

**Cause:** SQLite only allows one writer at a time. This can happen if multiple
processes try to write simultaneously, or if a previous process crashed while
holding a write lock.

**Fix:**
```bash
# Check for stale lock files
ls -la memory.db-wal memory.db-shm

# If the server is not running and lock files exist, remove them
rm -f memory.db-wal memory.db-shm
```

The database uses WAL (Write-Ahead Logging) mode, which allows concurrent
readers alongside one writer. Concurrent reads should never see this error.

---

## Memory consolidation removed a fact I wanted to keep

**Cause:** Consolidation clusters memories with cosine similarity ≥ 0.92 and
keeps the highest-confidence one. If two very similar facts existed, the
lower-confidence one was superseded.

**Fix — Option A:** Increase confidence on the fact you want to keep:
```bash
# Use the HTTP API to store it again with higher confidence
curl -X POST http://localhost:8900/remember \
  -H "Content-Type: application/json" \
  -d '{"entity_name": "Brian", "fact": "your fact here", "confidence": 0.99}'
```

**Fix — Option B:** Increase `CONSOLIDATION_THRESHOLD` in `server.py` to make
it less aggressive (e.g. `0.95` instead of `0.92`).

---

## Logs show repeated "Unexpected disconnect" from MQTT bridge

**Symptom** (in `mqtt_bridge.py` output):
```
WARNING   Unexpected disconnect (rc=7) — reconnect in progress
```

**Cause:** The MQTT broker closed the connection — usually due to keepalive
timeout, broker restart, or network interruption.

**Fix:** This is handled automatically. paho-mqtt reconnects with exponential
backoff (1s → 60s). No action needed unless disconnects are persistent.

For persistent disconnects, verify the broker is running:
```bash
systemctl status mosquitto
# or
mosquitto_sub -h your-broker-host -t test/ping -v &
mosquitto_pub -h your-broker-host -t test/ping -m hello
```

---

## Getting more information

**Enable debug logging** for the HTTP API:
```bash
uvicorn api:app --host 0.0.0.0 --port 8900 --log-level debug
```

**Inspect the database directly:**
```bash
sqlite3 memory.db
.tables
SELECT * FROM memories ORDER BY created DESC LIMIT 10;
SELECT * FROM readings ORDER BY ts DESC LIMIT 10;
SELECT count(*), entity_id FROM readings GROUP BY entity_id;
.quit
```

**Check server startup:**
```bash
python -c "import server; print('server.py loads OK')"
python -c "import api;    print('api.py loads OK')"
```
