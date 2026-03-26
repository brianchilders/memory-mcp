# HTTP API Reference

Base URL: `http://localhost:8900` (or your server's address)

All request and response bodies are JSON. All timestamps are Unix epoch seconds (float).

---

## Health

### `GET /health`

Liveness check. Returns row counts for all tables and the MCP protocol version.

**Response:**
```json
{
  "status": "ok",
  "entities": 3,
  "memories": 47,
  "readings": 12840,
  "ts": 1711123456.78,
  "mcp_protocol_version": "2025-11-25"
}
```

---

### `GET /mcp-info`

MCP spec compliance information. Returns the protocol version the server implements,
the SDK version in use, and the full list of registered MCP tools.

Useful for MCP clients that need to verify compatibility before connecting,
and for monitoring when the SDK is updated to a new protocol version.

**Response:**
```json
{
  "mcp_sdk_version": "1.26.0",
  "mcp_protocol_version": "2025-11-25",
  "mcp_default_negotiated_version": "2025-03-26",
  "tool_count": 20,
  "tools": [
    {"name": "remember",    "description": "Store a semantic fact/memory about any entity."},
    {"name": "recall",      "description": "Semantic search across all stored memories."},
    {"name": "get_context", "description": "..."},
    "..."
  ]
}
```

| Field | Description |
|---|---|
| `mcp_sdk_version` | Installed `mcp` Python package version |
| `mcp_protocol_version` | `LATEST_PROTOCOL_VERSION` from `mcp.types` — the spec version the SDK targets |
| `mcp_default_negotiated_version` | `DEFAULT_NEGOTIATED_VERSION` — what is negotiated by default during handshake |
| `tool_count` | Number of registered MCP tools |
| `tools` | Array of `{name, description}` for every registered tool |

**Protocol version format:** `YYYY-MM-DD` (MCP uses date-based versioning).

**SDK upgrade notice:** `test_mcp_info_protocol_version_matches_sdk` in
`tests/test_mcp_compliance.py` will fail immediately if the SDK is upgraded to
a new spec version, prompting you to validate the server against the new spec
before the upgrade lands in production.

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

## Episodic Memory

### `POST /open_session`

Open a new conversation session for an entity. Creates the entity if it does not exist.

**Request:**
```json
{"entity_name": "Brian", "entity_type": "person"}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity to attach the session to |
| `entity_type` | string | no | `"person"` | Used only when creating a new entity |

**Response:** `{"result": 42, "ok": true}` — `result` is the integer `session_id`. Pass it to `/log_turn`, `/close_session`, and `/get_session`.

---

### `POST /log_turn`

Append one turn to an open session.

**Request:**
```json
{"session_id": 42, "role": "user", "content": "I need to pick up groceries tomorrow"}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | int | yes | Session to append to |
| `role` | string | yes | Must be `"user"`, `"assistant"`, or `"system"` |
| `content` | string | yes | Turn content |

**Response:** `{"result": "Logged [user] turn to session 42.", "ok": true}`

**Note:** If `session_id` does not exist the tool returns a descriptive error string in `result` with HTTP 200 — consistent with all other tool-wrapping routes.

**Error:** `422` if `role` is not one of the three allowed values.

---

### `POST /close_session`

Close a session and optionally store a summary. Sets `ended_at` to now; session becomes read-only.

**Request:**
```json
{"session_id": 42, "summary": "Brian discussed grocery shopping and meal planning."}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | int | yes | Session to close |
| `summary` | string | no | Optional summary; omit or `null` to close without one |

**Response:** `{"result": "Closed session 42. Summary: '...'", "ok": true}`

---

### `GET /get_session/{session_id}`

Retrieve a full session transcript: all turns in order, entity name, open/close times, and summary.

**Response:**
```json
{
  "result": "Session 42 — Brian | 2026-03-22 10:30 → 2026-03-22 10:45\nSummary: Brian discussed grocery shopping.\n\n  [10:30:01] user: I need to pick up groceries\n  [10:30:05] assistant: I'll remind you tomorrow.",
  "ok": true
}
```

If `session_id` does not exist, returns a descriptive string in `result` with HTTP 200.

---

## LLM Extraction

### `POST /extract_and_remember`

Extract structured facts from free text using the configured LLM and store them as Tier 1 memories for the entity.

**Request:**
```json
{
  "entity_name": "Brian",
  "text":        "I prefer dark roast coffee and usually wake up at 6am",
  "entity_type": "person",
  "model":       null
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entity_name` | string | yes | — | Entity to store facts for |
| `text` | string | yes | — | Free text to extract facts from |
| `entity_type` | string | no | `"person"` | Used only when creating a new entity |
| `model` | string | no | `null` | LLM model override; defaults to `MEMORY_LLM_MODEL` env var |

**Response:** `{"result": "Extracted and stored 2 fact(s) for 'Brian'.", "ok": true}`

**Note:** Requires a running Ollama instance (or configured LLM backend). If the LLM is unavailable, the tool catches the exception and returns `"result": "Extraction failed: ..."` with HTTP 200 — it does not raise a 500.

---

## Voice — Speaker Identity

Routes for managing voiceprint-based speaker enrollment. Voiceprints are stored
in the entity `meta` JSON column — no schema changes required. Provisional
entities are created by the pipeline worker with name `unknown_voice_{hash}` and
`meta.status = "unenrolled"`.

All voice routes require the standard `Authorization: Bearer <token>` header.

### `GET /voices/unknown`

List all provisional (unenrolled) speaker entities, ordered by detection count descending.

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Maximum results |
| `min_detections` | int | `1` | Exclude entities seen fewer than this many times |

**Response:**
```json
{
  "result": [
    {
      "entity_name": "unknown_voice_a3f2c8d1",
      "first_seen": "2026-03-22T10:30:00Z",
      "first_seen_room": "kitchen",
      "detection_count": 7,
      "last_seen": 1742644500.0,
      "sample_transcript": "I need to pick up groceries tomorrow"
    }
  ],
  "ok": true
}
```

`last_seen` and `sample_transcript` come from the entity's most recent
`voice_activity` reading. Both are `null` if no voice readings have been recorded.

---

### `POST /voices/enroll`

Rename a provisional entity to a real person's name and mark it enrolled.
All existing memories, readings, and relations remain attached — only the
entity name and `meta.status` change.

**Request:**
```json
{
  "entity_name":  "unknown_voice_a3f2c8d1",
  "new_name":     "Brian",
  "display_name": "Brian Childers"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `entity_name` | string | yes | Current provisional name |
| `new_name` | string | yes | Real name to assign |
| `display_name` | string | no | Human-readable name stored in meta |

**Response:**
```json
{
  "result": {
    "entity_id": 42,
    "entity_name": "Brian",
    "previous_name": "unknown_voice_a3f2c8d1",
    "memories_transferred": 14,
    "readings_transferred": 7
  },
  "ok": true
}
```

**Errors:** `404` if `entity_name` not found; `409` if `new_name` already exists.

---

### `POST /voices/merge`

Merge a provisional entity into an existing enrolled entity. Transfers all
memories, readings, and active relations to the target; averages voiceprint
embeddings weighted by sample count; then deletes the source entity.

Runs as a single atomic transaction. Relations that conflict with a UNIQUE
constraint on the target are skipped — they are removed by CASCADE when the
source entity is deleted.

**Request:**
```json
{
  "source_name": "unknown_voice_a3f2c8d1",
  "target_name": "Brian"
}
```

**Response:**
```json
{
  "result": {
    "target_name": "Brian",
    "memories_merged": 6,
    "readings_merged": 12,
    "relations_merged": 2,
    "source_deleted": "unknown_voice_a3f2c8d1"
  },
  "ok": true
}
```

**Errors:** `400` if source and target are the same entity; `404` if either entity not found.

---

### `POST /voices/update_print`

Update the voiceprint embedding for an entity using a running weighted average.
Called by the pipeline worker after each confident speaker identification to
refine the embedding over time. Result is re-normalized to a unit vector.

**Request:**
```json
{
  "entity_name": "Brian",
  "embedding":   [0.012, -0.034, "... 256 values total ..."],
  "weight":      0.1
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `entity_name` | string | yes | Enrolled entity name |
| `embedding` | float[256] | yes | New resemblyzer embedding (must be finite — no NaN/Infinity) |
| `weight` | float | no (default `0.1`) | Contribution of new sample; range 0.0–1.0 |

**Response:**
```json
{
  "result": {
    "entity_name": "Brian",
    "voiceprint_samples": 13,
    "embedding_norm": 1.0
  },
  "ok": true
}
```

`embedding_norm` should be ~1.0 after normalization — a sanity check for the caller.

**Errors:** `404` if entity not found; `422` if embedding is not 256-dimensional or contains non-finite values.

---

## Entity Graph

### `GET /graph`

Serves the vis.js entity relationship graph as a standalone web page.
Auth-exempt — protected at the network layer like `/admin`.

Open in a browser: `http://localhost:8900/graph`

- Nodes represent entities, sized by memory count, coloured by type
- Edges represent active directed relations with their `rel_type` as labels
- Click any node to see its memories in a sidebar panel
- "Export .md" button in the sidebar downloads the entity as Markdown

---

### `GET /api/graph`

Return the entity relationship graph as JSON for the vis.js frontend.
Requires `Authorization: Bearer <token>` (token injected into the SPA automatically).

**Response:**
```json
{
  "nodes": [
    {
      "id":           1,
      "name":         "Brian",
      "type":         "person",
      "memory_count": 12,
      "memories": [
        {"fact": "Prefers dark roast coffee", "category": "preference", "confidence": 0.9}
      ]
    }
  ],
  "edges": [
    {"from": 1, "to": 2, "label": "spouse"}
  ]
}
```

- `nodes.memories` — active memories only (`superseded_by IS NULL`)
- `edges` — active directed relations only (`valid_until IS NULL`)
- vis.js uses `from`/`to` for edge endpoints

---

## Markdown Export

Exports entity memories in Obsidian-compatible Markdown format:
YAML frontmatter + `## Observations` (active memories, grouped by category) +
`## Relations` (`[[wikilinks]]` for active directed relations).

Both endpoints are auth-exempt so browsers can download files directly
via `<a href>` without needing JS fetch with an auth header.

### `GET /export/markdown/{entity_name}`

Export a single entity as a `.md` file download.

**Response:** `text/plain` with `Content-Disposition: attachment; filename="{entity_name}.md"`

**Example output:**
```markdown
---
type: person
created: 2026-01-15T08:30:00
updated: 2026-03-25T14:22:00
tags: [memory, auto]
---

# Brian

## Observations

### General

- Works on OpenHome AI speaker platform

### Preference

- Prefers dark roast coffee

## Relations

- [[homeassistant]] — controls
```

**Errors:** `404` if entity not found.

---

### `GET /export/markdown`

Export all entities. Returns a JSON object mapping `{entity_name}.md` → markdown content.

**Response:**
```json
{
  "files": {
    "Brian.md":         "---\ntype: person\n...",
    "homeassistant.md": "---\ntype: device\n..."
  }
}
```

To write all files to an Obsidian vault directory:
```python
import pathlib, requests

vault = pathlib.Path("/path/to/obsidian/vault")
files = requests.get("http://localhost:8900/export/markdown").json()["files"]
for filename, content in files.items():
    (vault / filename).write_text(content)
```

---

### `POST /import/markdown`

Import entities from Obsidian-compatible Markdown files. Accepts the same
`{ files: {...} }` shape that `GET /export/markdown` returns — making
export → edit → import a clean round-trip.

Requires `Authorization: Bearer <token>` (write operation).

**Request:**
```json
{
  "files": {
    "Brian.md":         "---\ntype: person\n...\n# Brian\n\n## Observations\n\n- Prefers dark roast\n",
    "homeassistant.md": "---\ntype: device\n...\n# homeassistant\n\n## Observations\n\n- Smart home hub\n"
  }
}
```

**Parse rules:**

| Element | Source |
|---|---|
| Entity name | First `# H1` heading; falls back to filename stem (minus `.md`) |
| Entity type | Frontmatter `type:` field; defaults to `"person"` |
| Observations | Bullet items under `## Observations`; `### Category` sub-headings set the category |
| Relations | Bullet items under `## Relations` matching `- [[other_name]] — rel_type` (em-dash, en-dash, or hyphen) |

**Idempotency:**
- Memories: a fact with the same text as an existing memory is skipped, not duplicated (`memories_skipped`)
- Relations: re-importing an active relation is a no-op (no error, no duplicate)

**Response:**
```json
{
  "imported": {
    "Brian": {
      "status":            "created",
      "memories_added":    2,
      "memories_skipped":  0,
      "relations_added":   1
    },
    "homeassistant": {
      "status":            "created",
      "memories_added":    1,
      "memories_skipped":  0,
      "relations_added":   0
    }
  },
  "errors": [],
  "ok": true
}
```

`status` is `"created"` if the entity was new, `"existing"` if it already existed.
`errors` contains entries for files that failed to parse or import entirely.

**To import from an Obsidian vault directory:**
```python
import pathlib, requests

vault = pathlib.Path("/path/to/obsidian/vault")
files = {p.name: p.read_text() for p in vault.glob("*.md")}
result = requests.post(
    "http://localhost:8900/import/markdown",
    json={"files": files},
    headers={"Authorization": "Bearer <token>"},
).json()
```

---

## Graph Traversal

### `GET /related/{entity_name}`

Find all entities reachable from `entity_name` within `depth` hops via active relations.
Traversal is **bidirectional** — both outgoing and incoming edges are followed.
Only relations with `valid_until IS NULL` are included.

| Query param | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `depth` | int | `2` | 1–5 | Maximum hops (clamped server-side) |
| `max_results` | int | `50` | 1–500 | Maximum entities to return |

**Response:**
```json
{"result": "Entities related to 'Alice' (depth=2):\n[1 hop] Bob (person)\n[2 hops] Acme Corp (company)", "ok": true}
```

Returns `"No entity named '...'"` (HTTP 200) if the entity doesn't exist.
Returns `"Alice has no related entities"` (HTTP 200) if no active relations exist.

---

## Importers

### `POST /import/jsonl`

Import entities, observations, and relations from Anthropic's official
[`@modelcontextprotocol/server-memory`](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)
JSONL format — one JSON object per line.

**Request:**
```json
{
  "content": "<newline-delimited JSON>"
}
```

**Line types:**

```jsonl
{"type": "entity", "name": "Alice", "entityType": "person", "observations": ["Likes coffee", "Works at CERN"]}
{"type": "relation", "from": "Alice", "to": "Bob", "relationType": "friend"}
```

- `content` is a raw string (not a file path). Maximum 5 MB, 10,000 lines.
- Two-pass: entities are created first, then relations.
- Relations referencing unknown entities create stub entities automatically.
- Observations are deduplicated — already-stored facts are skipped.
- Relations are idempotent — reimporting an active relation is a no-op.
- All stored memories are tagged `source = "import:jsonl"`.

**Response:**
```json
{"ok": true, "added": 3, "skipped": 1, "errors": []}
```

| Field | Description |
|---|---|
| `added` | Number of observations written to the DB |
| `skipped` | Observations that already existed (deduplicated) |
| `errors` | Array of per-line error messages (malformed JSON, bad field types, etc.) |

---

### `POST /import/mem0`

Import memories from a [mem0](https://mem0.ai) instance (cloud or self-hosted).
Paginates through all pages with exponential backoff on HTTP 429.

**Request:**
```json
{
  "user_id":     "alice",
  "api_key":     "m0-...",
  "base_url":    "https://api.mem0.ai",
  "entity_type": "person"
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `user_id` | string | yes | — | mem0 user identifier; used as entity name |
| `api_key` | string | no | `null` | API key (required for mem0 cloud; omit for unauthenticated self-hosted) |
| `base_url` | string | no | `"https://api.mem0.ai"` | Must be `http://` or `https://` |
| `agent_id` | string | no | `null` | Filter by agent_id |
| `app_id` | string | no | `null` | Filter by app_id |
| `entity_type` | string | no | `"person"` | Entity type for the imported user entity |

**Response:**
```json
{"ok": true, "added": 42, "skipped": 3, "errors": []}
```

Errors are returned with HTTP 400 for bad parameters (invalid URL scheme, empty `user_id`)
or HTTP 500 for unexpected server errors.  HTTP 409 is never returned — mem0 errors appear
in the `errors` array.

---

### `POST /import/mcp-memory-service`

Import from a [doobidoo/mcp-memory-service](https://github.com/doobidoo/mcp-memory-service)
SQLite database by reading it directly (no running service required).

**Stop `mcp-memory-service` before importing** to avoid a locked-database error.

**Request:**
```json
{
  "db_path":     "/home/user/.config/mcp-memory/memories.db",
  "entity_name": "imported",
  "entity_type": "person"
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `db_path` | string | yes | — | Absolute path to the SQLite file on the server |
| `entity_name` | string | no | `"imported"` | Entity in memory-mcp to receive all imported memories |
| `entity_type` | string | no | `"person"` | Entity type |

**Response:**
```json
{"ok": true, "added": 156, "skipped": 0, "errors": []}
```

| Status | Meaning |
|---|---|
| `400` | Bad request — file not found, not a SQLite database, invalid entity name |
| `409` | Conflict — SQLite database is locked (stop mcp-memory-service first) |

The importer auto-discovers the table name (`memories`, `memory`, `items`, or `data`) and
content column (`content`, `memory`, `observation`, `text`, `fact`, or `value`) via
`PRAGMA table_info()`.

---

## Admin UI Curation

These endpoints are called by the Admin UI (HTMX). They return HTML fragments, not JSON.

### `POST /admin/memory/{memory_id}/delete`

Delete a single memory and its vector embedding.

- Returns `""` (empty body) with HTTP 200 on success — HTMX swaps the `<li>` row out of the DOM.
- Returns HTTP 404 if `memory_id` does not exist.

### `POST /admin/entity/{name}/remember`

Add an observation to an existing entity from the Admin UI.

**Form fields** (`Content-Type: application/x-www-form-urlencoded`):

| Field | Required | Default | Description |
|---|---|---|---|
| `fact` | yes | — | The observation text (stripped, max 10,000 chars) |
| `category` | no | `"general"` | Memory category; invalid values silently default to `"general"` |

- Returns HTTP 400 if `fact` is blank after stripping.
- Returns HTTP 404 if the entity `name` does not exist.
- Returns an HTML `<li>` fragment on success — HTMX appends it to the memory list.
- All stored memories are tagged `source = "admin_ui"`.

---

## Error responses

All endpoints return a JSON error body with an appropriate HTTP status code.

**Format:**
```json
{"detail": "Entity 'Brian' not found"}
```

| Status | Meaning |
|---|---|
| `400` | Bad request — invalid combination of parameters |
| `404` | Entity or memory not found |
| `409` | Conflict — target name already exists |
| `422` | Validation error — missing required field, wrong type, or invalid value |
| `500` | Server error — check logs for details |
