# memory-mcp-server

Unified semantic memory + time-series intelligence layer for OpenHome abilities.

## Architecture: four tiers

```
Tier 1   — Semantic memory      entities, memories, relations, vectors
Tier 1.5 — Episodic memory      conversation sessions, turn-by-turn transcripts
Tier 2   — Time-series store    readings (numeric/categorical/composite), rollups, schedule
Tier 3   — Pattern engine       background task: promotes stable trends → Tier 1 memories
```

The pattern engine closes the loop: raw sensor data (Tier 2) automatically becomes
searchable, natural-language memory ("Brian's temperature preference is consistently 68°F")
that any ability can recall semantically (Tier 1).

## Files

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `server.py`       | MCP server (stdio transport, all 11 tools)           |
| `api.py`          | FastAPI HTTP wrapper + admin UI mount                |
| `admin.py`        | Admin UI router (served at `/admin`)                 |
| `voice_routes.py` | Speaker identity API (`/voices/*` — enroll, merge, update voiceprints) |
| `reembed.py`      | Utility to re-embed all memories when swapping models|
| `templates/admin` | Jinja2 HTML templates for the admin UI               |
| `integrations/`   | Standalone tools that connect external systems to memory-mcp via HTTP |

## Documentation

| Doc | Contents |
|---|---|
| `docs/overview.md` | What it is, motivation, integration patterns (OpenHome, HA, MQTT, IoT) |
| `docs/installation.md` | Requirements, step-by-step setup, first run, verification |
| `docs/quickstart.md` | First entity, memory, reading — common operations with curl examples |
| `docs/api-reference.md` | Full HTTP API — every endpoint, request/response shapes, examples |
| `docs/admin-ui.md` | Admin dashboard guide, pages, reading confidence, prune, security |
| `docs/ai-backend.md` | AI backend config, provider examples, model swap guide |
| `docs/pattern-engine.md` | How detectors work, all 5 detector types, how to add new ones |
| `docs/retention.md` | Retention policy config, what gets deleted, storage estimates |
| `docs/deployment.md` | systemd service, Docker Compose, reverse proxy, environment config |
| `docs/maintenance.md` | Keeping it healthy — backups, upgrades, model swaps, reembed.py walkthrough |
| `docs/testing.md` | Running tests, fixture design, what is and isn't covered |
| `docs/troubleshooting.md` | Common errors, what they mean, how to fix them |
| `integrations/README.md` | Integration index — MQTT bridge, HA state poller, OpenHome, Cloudflare |
| `integrations/background_example.py` | Background worker template — health data, environment sensors, weather |
| `integrations/ha_state_poller.py` | Pull-based HA state poller — polls HA REST API, pushes to memory-mcp |
| `integrations/homeassistant/README.md` | HA package setup — rest_commands, automations, scripts |
| `integrations/openhome/README.md` | OpenHome ability setup — background daemon + recall skill |
| `integrations/cloudflare/README.md` | Cloudflare Tunnel setup — safe internet exposure for cloud callers |

## Setup

```bash
# Install deps
pip install mcp sqlite-vec httpx fastapi uvicorn jinja2

# Pull embedding and LLM models (Ollama default)
ollama pull nomic-embed-text
ollama pull llama3.2

# Run MCP server (for OpenHome abilities)
python server.py

# Run HTTP API + admin UI (for HA webhooks, Node-RED, scripts)
python api.py          # listens on :8900
                       # admin UI at http://localhost:8900/admin/
```

## AI Backend

Uses the **OpenAI-compatible API** (`/v1/embeddings` + `/v1/chat/completions`).
Works with Ollama, OpenAI, LM Studio, Together AI, or any compatible provider.
Configure via environment variables — no code changes needed:

```bash
# Default (local Ollama)
export MEMORY_AI_BASE_URL=http://localhost:11434/v1
export MEMORY_EMBED_MODEL=nomic-embed-text   # 768-dim
export MEMORY_LLM_MODEL=llama3.2

# OpenAI
export MEMORY_AI_BASE_URL=https://api.openai.com/v1
export MEMORY_AI_API_KEY=sk-...
export MEMORY_EMBED_MODEL=text-embedding-3-small
export MEMORY_EMBED_DIM=1536
export MEMORY_LLM_MODEL=gpt-4o-mini
```

See `docs/ai-backend.md` for full configuration guide and provider examples.

## Testing

```bash
pip install -r requirements.txt
python -m pytest                     # full suite (308 tests, no Ollama needed)
python -m pytest tests/test_tools.py # just tool tests
```

See `docs/testing.md` for fixture design and conventions.

## OpenHome SDK config

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["/path/to/memory-mcp/server.py"]
    }
  }
}
```

## Schema

```
TIER 1
  entities          id, name*, type, meta(JSON), created, updated
  memories          id, entity_id, fact, category, confidence, source, created, updated,
                    last_accessed, access_count, superseded_by
  relations         id, entity_a, entity_b, rel_type, meta(JSON), created,
                    valid_from, valid_until
  memory_vectors    rowid=memories.id, embedding FLOAT[768]   ← sqlite-vec

TIER 1.5
  sessions          id, entity_id, started_at, ended_at, summary, meta
  session_turns     id, session_id, role, content, ts

TIER 2
  readings          id, entity_id, metric, unit, value_type, value_num,
                    value_cat, value_json, source, ts
                    (composite readings also decomposed into {metric}.{key} child rows)
  reading_rollups   id, entity_id, metric, bucket_type, bucket_ts,
                    count, avg_num, min_num, max_num, p10_num, p90_num, mode_cat
  rollup_watermarks entity_id, metric, last_ts   ← incremental build tracking
  schedule_events   id, entity_id, title, start_ts, end_ts, recurrence, meta, created

TIER 3
  promoted_patterns id, entity_id, metric, pattern_key, memory_id, detected
```

### Entity types (open — add any string)
`person` | `house` | `room` | `device`

### Memory categories
`preference` | `habit` | `routine` | `relationship` | `insight` | `general`

### Value types for readings
| value_type    | field         | example                                   |
|---------------|---------------|-------------------------------------------|
| `numeric`     | value_num     | temperature=71.4, heart_rate=62           |
| `categorical` | value_cat     | mood="calm", presence="home"              |
| `composite`   | value_json    | `{"mood":"calm","confidence":0.91}`       |

## MCP Tools

### Tier 1 — Semantic memory
| Tool           | Description                                           |
|----------------|-------------------------------------------------------|
| `remember`     | Store a fact about any entity (embeds + indexes it)   |
| `recall`       | Semantic search — multi-factor: cosine × recency × confidence |
| `get_context`  | Relevance-filtered context snapshot (preferred for ability use) |
| `get_profile`  | Full profile: memories + relationships + readings     |
| `relate`       | Create directed relationship between entities         |
| `unrelate`     | Soft-delete a relationship (sets valid_until, preserves history) |
| `forget`       | Delete a memory or entire entity                      |
| `extract_and_remember` | LLM-powered fact extraction from conversation text |

### Tier 2 — Time-series
| Tool           | Description                                           |
|----------------|-------------------------------------------------------|
| `record`       | Ingest a reading (numeric/categorical/composite)      |
| `query_stream` | Query readings: raw or hour/day/week rollups          |
| `get_trends`   | Natural-language trend summary for a metric           |
| `schedule`     | Add a schedule event (one-off or recurring)           |

### Episodic memory
| Tool             | Description                                         |
|------------------|-----------------------------------------------------|
| `open_session`   | Open a conversation session for an entity           |
| `log_turn`       | Append a turn (user/assistant/system) to a session  |
| `close_session`  | Close a session with optional summary               |
| `get_session`    | Retrieve full session transcript                    |

### Cross-tier
| Tool           | Description                                           |
|----------------|-------------------------------------------------------|
| `cross_query`  | Semantic search across memories AND live readings     |

### Maintenance
| Tool    | Description                                                    |
|---------|----------------------------------------------------------------|
| `prune` | Delete raw readings older than `RETENTION_DAYS` (default 30d) |

## HTTP API endpoints (api.py)

```
GET  /health                    liveness + row counts
GET  /entities                  list all entities
POST /remember                  store a memory
POST /recall                    semantic search (recency_weight + min_confidence)
POST /get_context               relevance-filtered context snapshot
GET  /profile/{entity_name}     full profile
POST /relate                    create relationship
POST /forget                    delete memory or entity
POST /record                    ingest a reading
POST /record/bulk               ingest multiple readings at once
POST /query_stream              query time-series
POST /get_trends                trend summary
POST /schedule                  add schedule event
POST /cross_query               unified search
POST /prune                     delete readings older than RETENTION_DAYS

GET  /voices/unknown            list unenrolled provisional speaker entities
POST /voices/enroll             rename provisional entity to real person
POST /voices/merge              merge provisional entity into enrolled entity
POST /voices/update_print       update voiceprint embedding (running average)

GET  /admin/                    dashboard
GET  /admin/entities            entity list
GET  /admin/entity/{name}       entity detail
GET  /admin/readings            readings stream
POST /admin/prune               prune (HTMX-friendly HTML response)
```

## Usage examples

### Ability: build context before responding to Brian

```python
# Pull full profile (memories + latest readings + schedule)
profile = await mem.tool_get_profile("Brian")
# → inject as <memory>...</memory> in system prompt

# Or cross-query to pull what's relevant to the current question
context = await mem.tool_cross_query("how is Brian feeling today?")
```

### Home Assistant → record sensor readings via HTTP

```yaml
# configuration.yaml — rest_command
rest_command:
  push_temperature:
    url: http://localhost:8900/record
    method: POST
    content_type: application/json
    payload: >
      {"entity_name":"{{ room }}","metric":"temperature",
       "value":{{ temp }},"unit":"F","source":"ha","entity_type":"room"}

  push_presence:
    url: http://localhost:8900/record
    method: POST
    content_type: application/json
    payload: >
      {"entity_name":"{{ person }}","metric":"presence",
       "value":"{{ state }}","source":"ha"}

  push_mood:
    url: http://localhost:8900/record
    method: POST
    content_type: application/json
    payload: >
      {"entity_name":"{{ person }}","metric":"mood",
       "value":{"mood":"{{ mood }}","confidence":{{ conf }}},"source":"avatar_ability"}
```

### Avatar ability: store inferred mood state

```python
# After detecting mood from conversation
await mem.tool_record(
    entity_name="Brian",
    metric="mood",
    value={"mood": "focused", "confidence": 0.87},
    source="avatar_ability",
)
# The pattern engine will promote this to a memory like:
# "Brian's mood is predominantly 'focused' (72% of days)"
```

### Query last week of temperature with daily rollup

```python
result = await mem.tool_query_stream(
    entity_name="living_room",
    metric="temperature",
    granularity="day",
    start_ts=time.time() - 7 * 86400,
)
```

### Cross-entity semantic query

```python
result = await mem.tool_cross_query("who in the house prefers a cooler environment?")
# Returns: matching memories (explicit preferences) + live temperature readings scored by relevance
```

## Swapping embedding models

```bash
# 1. Update EMBED_MODEL and EMBED_DIM in server.py
# 2. Pull the new model
ollama pull mxbai-embed-large   # 1024-dim — richer but slower

# 3. Re-embed all memories (non-destructive — only rebuilds memory_vectors)
python reembed.py --dry-run     # preview
python reembed.py               # run it
```

## Expanding the schema

- **New entity types**: pass any string — SQLite won't enforce the enum
- **New metric names**: pass any string to `record()` — fully dynamic
- **New memory categories**: same — add to enum in schema or free-text
- **New pattern detectors**: write `_detect_*(entity_name, metric, data) → list[tuple]`, call it via `_maybe_promote()` in `_promote_patterns()`. See `docs/pattern-engine.md`.
- **New rollup statistics**: add columns to `reading_rollups` and compute in `_build_rollups()`
- **Structured entity attributes**: use the `meta` JSON column on entities
  (e.g. `{"age": 35, "diet": "vegetarian", "wake_time": "06:30"}`)
- **Retention window**: change `RETENTION_DAYS` in `server.py`. See `docs/retention.md`.
