# HTTP API Reference

Base URL: `http://localhost:8900` (or your server's address)

All request and response bodies are JSON. All timestamps are Unix epoch seconds (float).

---

## Health

### `GET /health`

Liveness check. Returns row counts for all tables.

**Response:**
```json
{
  "ok": true,
  "entities": 3,
  "memories": 47,
  "readings": 12840,
  "rollups": 210,
  "patterns": 8,
  "schedule_events": 2
}
```

---

## Entities

### `GET /entities`

List all entities.

**Response:**
```json
{
  "entities": [
    {"name": "Brian",       "type": "person", "meta": {"age": 35}, "updated": 1700000000},
    {"name": "living_room", "type": "room",   "meta": {},          "updated": 1700000100}
  ]
}
```

---

## Semantic Memory

### `POST /remember`

Store a fact about an entity. Creates the entity if it doesn't exist.
Automatically detects and supersedes contradicting memories.

**Request:**
```json
{
  "entity_name":  "Brian",
  "fact":         "Prefers the bedroom temperature at 68°F when sleeping",
  "category":     "preference",
  "confidence":   0.95,
  "source":       "manual",
  "entity_type":  "person"
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity to attach the memory to |
| `fact` | string | yes | — | The fact to store |
| `category` | string | no | `"general"` | `preference`, `habit`, `routine`, `relationship`, `insight`, `general` |
| `confidence` | float | no | `1.0` | 0.0–1.0 |
| `source` | string | no | `"manual"` | Free-text source label |
| `entity_type` | string | no | `"person"` | Used only when creating a new entity |

**Response:**
```json
{"result": "Stored memory for Brian: Prefers the bedroom temperature at 68°F when sleeping", "ok": true}
```

---

### `POST /recall`

Semantic search for memories. Results ranked by cosine similarity × recency × confidence.
Superseded memories are always excluded.

**Request:**
```json
{
  "entity_name":     "Brian",
  "query":           "temperature preferences at night",
  "top_k":           5,
  "recency_weight":  0.0,
  "min_confidence":  0.5
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | yes | — | Natural-language search query |
| `entity_name` | string | no | `null` | Limit search to one entity; omit to search all |
| `category` | string | no | `null` | Limit to one category |
| `top_k` | int | no | `5` | Maximum results to return |
| `recency_weight` | float | no | `0.0` | 0.0 = pure semantic, 1.0 = strong recency bias |
| `min_confidence` | float | no | `0.0` | Filter out memories below this confidence |

**Response:**
```json
{
  "result": "Top 2 memories for Brian matching 'temperature preferences at night':\n1. [preference, conf=0.95] Prefers the bedroom temperature at 68°F when sleeping\n2. [habit, conf=0.8] Usually sets thermostat to 70°F during the day",
  "ok": true
}
```

---

### `POST /get_context`

Relevance-filtered context snapshot for an entity. Preferred over `/recall` for
AI ability use — returns a structured block combining memories, readings, and schedule.

**Request:**
```json
{
  "entity_name":   "Brian",
  "context_query": "how is Brian feeling today?",
  "max_facts":     10
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity to fetch context for |
| `context_query` | string | yes | — | Natural-language query used to rank which memories to surface |
| `max_facts` | int | no | `5` | Maximum number of memories to include |

**Response:**
```json
{
  "result": "Context for Brian:\n\nMemories:\n- [preference] Prefers 68°F at night (conf=0.95)\n\nLatest readings:\n- mood: calm (2024-01-15 14:30)\n- presence: home (2024-01-15 14:00)\n\nUpcoming schedule:\n- Morning workout at 2024-01-16 07:00",
  "ok": true
}
```

---

### `GET /profile/{entity_name}`

Full profile for one entity: all memories, relationships, latest readings, and
upcoming schedule. Returns a formatted text block suitable for injection into
an AI system prompt.

**Response:**
```json
{
  "result": "=== Profile: Brian (person) ===\nMeta: age=35\n\nPREFERENCE:\n  • Prefers 68°F at night  [3d ago]\n\nRELATIONSHIPS:\n  • lives_in → house\n\nLATEST READINGS:\n  • mood: calm  (just now)\n\nUPCOMING SCHEDULE:\n  • 2026-03-22 07:00 — Morning workout",
  "ok": true
}
```

---

### `POST /relate`

Create a directed relationship between two entities.
If the relationship already exists (and is active), it is updated in place.
If it was previously soft-deleted, it is reactivated.

**Request:**
```json
{
  "entity_a": "Brian",
  "entity_b": "house",
  "rel_type": "lives_in",
  "meta":     {}
}
```

**Response:**
```json
{"result": "Related Brian --[lives_in]--> house", "ok": true}
```

---

### `POST /unrelate`

Soft-delete a relationship. Sets `valid_until` to now. The relationship remains
in the database for historical queries but is excluded from active profiles.

**Request:**
```json
{
  "entity_a": "Brian",
  "entity_b": "old_house",
  "rel_type": "lives_in"
}
```

**Response:**
```json
{"result": "Unrelated Brian --[lives_in]--> old_house", "ok": true}
```

Returns an error if no active relationship is found.

---

### `POST /forget`

Delete a specific memory, or an entire entity and all its data.

**Delete one memory:**
```json
{"entity_name": "Brian", "memory_id": 42}
```

**Delete entire entity** (all memories, readings, relations, sessions):
```json
{"entity_name": "Brian"}
```

**Response:**
```json
{"result": "Deleted memory 42", "ok": true}
```
or
```json
{"result": "Deleted entity Brian and all associated data", "ok": true}
```

---

## Time-Series

### `POST /record`

Ingest a reading for an entity. Creates the entity if it doesn't exist.

**Request:**
```json
{
  "entity_name": "living_room",
  "entity_type": "room",
  "metric":      "temperature",
  "value":       71.4,
  "unit":        "F",
  "source":      "ha",
  "ts":          1700001000
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity name |
| `metric` | string | yes | — | Metric name (any string) |
| `value` | number/string/object | yes | — | See value types below |
| `unit` | string | no | `null` | Unit label (F, %, ppm, etc.) |
| `source` | string | no | `"api"` | Source label |
| `entity_type` | string | no | `"person"` | Used only when creating a new entity |
| `ts` | float | no | now | Unix timestamp — defaults to current time |

**Value types:**
| Value | Stored as | Example |
|---|---|---|
| Number | `numeric` | `71.4` |
| String | `categorical` | `"home"` |
| Object | `composite` + decomposed children | `{"mood":"calm","confidence":0.91}` |

Composite readings are automatically decomposed into child rows:
`mood.mood = "calm"` (categorical) and `mood.confidence = 0.91` (numeric).

**Response:**
```json
{"result": "Recorded living_room/temperature = 71.4", "ok": true}
```

---

### `POST /record/bulk`

Ingest multiple readings in one request.

**Request:**
```json
{
  "readings": [
    {"entity_name": "living_room", "entity_type": "room", "metric": "temperature", "value": 71.4, "unit": "F", "source": "ha"},
    {"entity_name": "bedroom",     "entity_type": "room", "metric": "temperature", "value": 68.1, "unit": "F", "source": "ha"}
  ]
}
```

**Response** — one result object per reading, in order:
```json
{
  "results": [
    {"ok": true,  "result": "Recorded living_room/temperature = 71.4"},
    {"ok": false, "error": "..."}
  ],
  "count": 2
}
```

Individual failures don't abort the batch — each reading is attempted
independently. Check the `ok` field per item if you need per-reading status.

---

### `POST /query_stream`

Query time-series readings with optional rollup aggregation.

**Request:**
```json
{
  "entity_name": "living_room",
  "metric":      "temperature",
  "granularity": "day",
  "start_ts":    1699200000,
  "end_ts":      1700000000
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity to query |
| `metric` | string | yes | — | Metric to query |
| `granularity` | string | no | `"raw"` | `raw`, `hour`, `day`, `week` |
| `start_ts` | float | no | 7 days ago | Start of window |
| `end_ts` | float | no | now | End of window |

**Response** — formatted text block:
```json
{
  "result": "temperature readings for living_room (raw, n=3):\n  2026-03-21 14:00:00  71.4 F\n  2026-03-21 13:00:00  71.1 F\n  2026-03-21 12:00:00  70.8 F",
  "ok": true
}
```

For rollup granularities (`hour`, `day`, `week`) the result shows aggregated
stats (avg, min, max, p10, p90) per bucket instead of raw rows.

---

### `POST /get_trends`

Natural-language trend summary for a metric.

**Request:**
```json
{
  "entity_name": "living_room",
  "metric":      "temperature"
}
```

**Response:**
```json
{
  "result": "living_room / temperature — 7-day trend:\n  avg=70.8°F  min=68.2°F  max=74.1°F  (147 readings)\n  Trend: stable",
  "ok": true
}
```

---

### `POST /schedule`

Add a schedule event for an entity.

**Request:**
```json
{
  "entity_name": "Brian",
  "title":       "Morning workout",
  "start_ts":    1700010000,
  "end_ts":      1700013600,
  "recurrence":  "daily",
  "meta":        {"location": "gym"}
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity this event belongs to |
| `title` | string | yes | — | Event title |
| `start_ts` | float | yes | — | Start time |
| `end_ts` | float | no | `null` | End time |
| `recurrence` | string | no | `"none"` | `"none"`, `"daily"`, `"weekly"` |
| `meta` | object | no | `{}` | Arbitrary metadata |

---

## Cross-Tier

### `POST /cross_query`

Semantic search across memories AND live readings from all entities simultaneously.

**Request:**
```json
{
  "query":  "who in the house prefers a cooler environment?",
  "top_k":  10
}
```

**Response:**
```json
{
  "result": "Unified search for 'who in the house prefers a cooler environment?':\n\nMemories:\n- Brian: Prefers 68°F at night [preference, 0.95]\n\nReadings:\n- bedroom/temperature: 68.1°F (latest)",
  "ok": true
}
```

---

## Maintenance

### `POST /prune`

Delete raw readings older than `RETENTION_DAYS` (default: 30 days).
Rollups, memories, and other data are never deleted.

**Request:** no body required

**Response:**
```json
{"result": "Pruned 142 readings older than 30 days. 28,350 readings remain.", "ok": true}
```

---

## Admin UI

The admin UI is served at `/admin/` and requires no separate authentication.
See `docs/admin-ui.md` for a full guide.

| URL | Description |
|---|---|
| `GET /admin/` | Dashboard with counts and recent activity |
| `GET /admin/entities` | All entities |
| `GET /admin/entity/{name}` | Full entity detail |
| `GET /admin/readings` | Live readings stream |
| `GET /admin/settings` | API token management |
| `POST /admin/token/regenerate` | Generate a new bearer token (HTMX) |
| `POST /admin/prune` | Prune old readings (HTMX — returns HTML fragment, not JSON) |

---

## Error responses

All endpoints return `{"ok": false, "error": "..."}` on failure with an appropriate
HTTP status code:

| Status | Meaning |
|---|---|
| `400` | Bad request — missing required field or invalid value |
| `404` | Entity or memory not found |
| `500` | Server error — check logs for details |

Example error:
```json
{"ok": false, "error": "Entity 'Brian' not found"}
```
