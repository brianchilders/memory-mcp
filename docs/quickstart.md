# Quickstart

This guide walks through the most common operations using the HTTP API.
If you're using the MCP server via Claude, the tool names are the same but
called as MCP tools rather than HTTP endpoints.

Prerequisites: server is installed and running. See `docs/installation.md`.

## Start the server

```bash
python api.py
# http://localhost:8900
```

Open the admin UI in a browser: `http://localhost:8900/admin/`

---

## Concepts

**Entity** — anything you want to track: a person, room, device, or house.
Entities are created automatically the first time you reference them.

**Memory** — a semantic fact about an entity ("Brian prefers 68°F at night").
Stored as text + a vector embedding. Searchable by meaning.

**Reading** — a time-stamped numeric or categorical measurement ("temperature=71.4F").
Builds the time-series store. The pattern engine promotes stable readings into memories.

**Relationship** — a directed link between two entities ("Brian lives_in house").

---

## Storing your first memory

```bash
curl -s -X POST http://localhost:8900/remember \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "Brian",
    "fact": "Prefers the bedroom temperature at 68°F when sleeping",
    "category": "preference",
    "confidence": 0.95
  }'
```

Response:
```json
{"result": "Stored memory for Brian: Prefers the bedroom temperature at 68°F when sleeping", "ok": true}
```

`entity_name` is created automatically if it doesn't exist. `entity_type` defaults
to `"person"` — pass `"entity_type": "room"` or `"device"` if needed.

## Searching memories (semantic recall)

```bash
curl -s -X POST http://localhost:8900/recall \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "Brian",
    "query": "temperature preferences at night"
  }'
```

The search uses vector similarity — you don't need to match exact words.
"sleeping temperature" and "bedroom at night" would both surface the same memory.

## Getting a full profile

```bash
curl -s http://localhost:8900/profile/Brian | python -m json.tool
```

Returns everything known about Brian: all memories, relationships, and latest readings.

## Recording a sensor reading

```bash
curl -s -X POST http://localhost:8900/record \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "living_room",
    "entity_type": "room",
    "metric": "temperature",
    "value": 71.4,
    "unit": "F",
    "source": "ha"
  }'
```

Value types:
- **Numeric:** pass a number — `"value": 71.4`
- **Categorical:** pass a string — `"value": "home"`
- **Composite:** pass a dict — `"value": {"mood": "calm", "confidence": 0.91}`
  Composite readings are automatically decomposed into child metrics
  (`mood` and `mood.confidence`).

## Querying a time series

```bash
# Raw readings — last 24 hours
curl -s -X POST http://localhost:8900/query_stream \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "living_room",
    "metric": "temperature",
    "granularity": "raw",
    "start_ts": '"$(python -c 'import time; print(int(time.time()-86400))')"'
  }'

# Daily rollups — last 7 days
curl -s -X POST http://localhost:8900/query_stream \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "living_room",
    "metric": "temperature",
    "granularity": "day",
    "start_ts": '"$(python -c 'import time; print(int(time.time()-604800))')"'
  }'
```

`granularity` options: `raw`, `hour`, `day`, `week`

## Getting a trend summary

```bash
curl -s -X POST http://localhost:8900/get_trends \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "living_room",
    "metric": "temperature"
  }'
```

Returns a natural-language summary like:
```
living_room / temperature — 7-day trend:
  avg=70.8°F  min=68.2°F  max=74.1°F  (147 readings)
  Trend: stable
```

## Creating a relationship

```bash
curl -s -X POST http://localhost:8900/relate \
  -H "Content-Type: application/json" \
  -d '{
    "entity_a": "Brian",
    "entity_b": "house",
    "rel_type": "lives_in"
  }'
```

Relationships appear in the entity profile and are soft-deletable (history preserved):

```bash
curl -s -X POST http://localhost:8900/unrelate \
  -H "Content-Type: application/json" \
  -d '{
    "entity_a": "Brian",
    "entity_b": "house",
    "rel_type": "lives_in"
  }'
```

## Cross-entity semantic search

Search across all entities at once:

```bash
curl -s -X POST http://localhost:8900/cross_query \
  -H "Content-Type: application/json" \
  -d '{"query": "who in the house prefers a cooler environment?"}'
```

Returns matching memories from any entity, ranked by relevance.

## Scheduling an event

```bash
curl -s -X POST http://localhost:8900/schedule \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "Brian",
    "title": "Morning workout",
    "start_ts": '"$(python -c 'import time; print(int(time.time()+3600))')"',
    "recurrence": "daily"
  }'
```

`recurrence` options: `null` (one-off), `"daily"`, `"weekly"`

## Getting relevant context (for AI abilities)

```bash
curl -s -X POST http://localhost:8900/get_context \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "Brian",
    "query": "how is Brian feeling today?"
  }'
```

This is the preferred endpoint for AI ability integration — it returns a
relevance-filtered snapshot combining memories, live readings, and schedule,
formatted for injection into a system prompt.

## Deleting data

Delete a specific memory by ID:
```bash
curl -s -X POST http://localhost:8900/forget \
  -H "Content-Type: application/json" \
  -d '{"entity_name": "Brian", "memory_id": 42}'
```

Delete an entire entity and all its data:
```bash
curl -s -X POST http://localhost:8900/forget \
  -H "Content-Type: application/json" \
  -d '{"entity_name": "Brian"}'
```

## Pruning old readings

Delete raw readings older than `RETENTION_DAYS` (default: 30 days):
```bash
curl -s -X POST http://localhost:8900/prune
```

Rollups and memories are never deleted by prune. See `docs/retention.md`.

## Bulk ingestion

Record multiple readings in one request:
```bash
curl -s -X POST http://localhost:8900/record/bulk \
  -H "Content-Type: application/json" \
  -d '{
    "readings": [
      {"entity_name": "living_room", "entity_type": "room", "metric": "temperature", "value": 71.4, "unit": "F", "source": "ha"},
      {"entity_name": "bedroom",     "entity_type": "room", "metric": "temperature", "value": 68.1, "unit": "F", "source": "ha"},
      {"entity_name": "office",      "entity_type": "room", "metric": "co2",         "value": 820,  "unit": "ppm", "source": "ha"}
    ]
  }'
```

---

## Using the admin UI

Navigate to `http://localhost:8900/admin/` to:
- See counts of all data at a glance (Dashboard)
- Browse entities and click into any one for its full profile
- Watch the live readings stream
- Trigger a prune without using curl

See `docs/admin-ui.md` for a full page-by-page guide.

---

## Using via MCP (Claude / OpenHome)

If you're using `server.py` via MCP, the same operations are available as tools:

```python
# Store a memory
await tool_remember(entity_name="Brian", fact="Prefers 68°F", category="preference")

# Recall by semantic query
await tool_recall(entity_name="Brian", query="temperature preferences")

# Record a reading
await tool_record(entity_name="living_room", metric="temperature", value=71.4, unit="F")

# Get context for a prompt
await tool_get_context(entity_name="Brian", query="how is Brian feeling?")
```

See `docs/mcp-tools.md` for the full tool reference with all parameters.
