# What is memory-mcp?

Your smart home knows a lot about you. Your thermostat knows the temperature
history. Your presence sensor knows when you come and go. Your AI assistant
knows what you said five minutes ago.

But none of them *remember* you.

Ask your AI assistant what temperature you prefer and it draws a blank. Show up
at home after three days away and your smart home treats you like a stranger.
Every conversation starts from zero. Every session forgets the last.

**memory-mcp** is the missing layer that fixes this — a local server that gives
your AI abilities a persistent, searchable, self-improving memory. Run it once
and it quietly gets smarter every day.

---

## The idea

memory-mcp is a persistent, semantic memory and time-series intelligence server.
It sits between your AI abilities, your smart home sensors, and your devices,
and does one thing: **it remembers**.

Not just recent conversation turns. Not just raw sensor numbers.
It builds a living, queryable model of the people, places, and things in your
world — and it gets smarter the longer it runs.

- Your AI assistant learns that you prefer your bedroom at 68°F and stops
  suggesting 72° every morning.
- Your home notices that you are almost always home by 6pm on weekdays and
  starts preparing before you arrive.
- An anomaly detector flags when your heart rate is unusually elevated and your
  assistant can ask if everything is okay.
- Two weeks of mood data surfaces a pattern: you tend to be "focused" on Tuesday
  and Thursday mornings — your assistant learns to hold non-urgent interruptions.

This is what it looks like when an AI home actually *knows* you.

---

## Application design

### Architecture: five tiers

```
Tier 1   — Semantic memory      entities, facts, relations, vector embeddings
Tier 1.5 — Episodic memory      conversation sessions, turn-by-turn transcripts
Tier 1.75 — Working memory      task-scoped transient scratchpads with TTL
Tier 2   — Time-series store    readings (numeric/categorical/composite), rollups, schedule
Tier 3   — Pattern engine       background task: promotes stable trends → Tier 1 memories
```

The pattern engine closes the loop: raw sensor data (Tier 2) automatically
becomes searchable, natural-language memory ("Brian's temperature preference is
consistently 68°F") that any ability can recall semantically (Tier 1).

### Storage

Everything lives in a single SQLite file with two extensions:

- **sqlite-vec** — vector similarity search (cosine distance over FLOAT[768]
  embeddings). Every memory fact is embedded into a 768-dimensional vector for
  semantic recall.
- **FTS5** — full-text search with BM25 ranking. Three FTS5 virtual tables
  index memory facts, session turn content, and intention trigger text. Enables
  keyword recall with no embedding model required — ideal for Raspberry Pi or
  any environment without a GPU.

### AI backend

memory-mcp uses the OpenAI-compatible API (`/v1/embeddings` +
`/v1/chat/completions`). It works with Ollama, OpenAI, LM Studio, Together AI,
or any compatible provider. Configure via environment variables — no code
changes needed.

### Interfaces

Two interfaces are available simultaneously:

- **MCP stdio server** (`server.py`) — 35 tools exposed over the Model Context
  Protocol. Connect any MCP-compatible AI framework.
- **HTTP API + admin UI** (`api.py`) — FastAPI server on port 8900 with REST
  endpoints, auto-generated Swagger docs at `/docs`, and a web dashboard at
  `/admin/`.

---

## Memory types

memory-mcp covers six of the seven major memory types studied in cognitive
science and AI memory research. Each type maps directly to one or more storage
tiers and retrieval tools.

### 1. Semantic memory (long-term factual)

Timeless facts about entities: preferences, habits, relationships, insights.

- **Storage:** Tier 1 — `memories` table + `memory_vectors` (sqlite-vec)
- **Write:** `remember` / `extract_and_remember` / pattern engine promotions
- **Read:** `recall` (vector, keyword, or hybrid), `get_context`, `get_profile`,
  `cross_query`, `get_context_budget`
- **Notable:** Multi-factor ranking (cosine × recency × confidence). Automatic
  contradiction detection supersedes outdated facts. Trust-tiered writes
  prevent low-trust sources from overwriting high-trust memories.
  Confidence decays over time; stale memories surfaced via `get_fading_memories`.

### 2. Episodic memory (autobiographical)

What happened, when, and with whom — full conversation transcripts with
LLM-generated summaries.

- **Storage:** Tier 1.5 — `sessions` + `session_turns` tables + `session_turns_fts`
- **Write:** `open_session`, `log_turn`, `close_session`
- **Read:** `get_session`, `search_sessions` (FTS5/BM25 keyword search across
  all turn content)
- **Notable:** The pattern engine automatically **consolidates** closed sessions
  — it sends the transcript to the LLM, extracts key facts, and promotes them
  to Tier 1 semantic memory. Raw transcripts become queryable facts without any
  manual curation step.

### 3. Working memory (transient task state)

Short-lived scratchpads for multi-step agent tasks. Slots expire automatically
via TTL. Can be promoted to long-term semantic memory on close.

- **Storage:** Tier 1.75 — `working_memory_tasks` + `working_memory_slots`
- **Write:** `wm_open`, `wm_set`
- **Read:** `wm_get`, `wm_list`
- **Lifecycle:** `wm_close` (optionally promotes all slots to Tier 1)
- **Notable:** Bridges the gap between in-context state (lost when context
  window fills) and long-term memory (too slow to write for every step). An
  agent can maintain a slot like `{preferred_format: "bullet points"}` across
  multiple tool calls within a session, then promote it to a memory at the end.

### 4. Procedural memory (learned patterns)

Stable patterns extracted automatically from the sensor stream — the system
learns behaviours without being explicitly taught.

- **Storage:** Tier 1 (`memories`) via `promoted_patterns` deduplication table
- **Write:** Pattern engine (background, hourly) — `_promote_patterns()`
- **Read:** Same as semantic memory — facts are fully searchable
- **Detectors:** stable average, trend (rising/falling), dominant categorical,
  time-of-day, anomaly, cross-metric correlation
- **Example promotions:**
  - "Brian's resting heart rate is consistently 62 bpm"
  - "Brian's mood is predominantly 'focused' (78% of days)"
  - "Brian's presence is 'home' at 19:00 (91% of readings)"
  - "Anomaly: bedroom temperature was 88°F at 14:30 (4.1 std devs above normal)"
  - "Brian's heart_rate and stress_level are positively correlated (r=0.84)"

### 5. Prospective memory (intentions — novel approach)

Future-oriented: "when X happens, do Y." This is memory about *what to do*,
not what happened or what is known.

- **Storage:** Tier 4 — `intentions` table + `intentions_fts` (FTS5 index on
  `trigger_text`)
- **Write:** `intend` — stores a `(trigger_text, action_text)` pair with an
  optional `expires_ts`
- **Read:** `check_intentions` — FTS5/BM25 match against the current
  conversation text; increments `fired_count` on each match;
  `list_intentions` — enumerate active or all intentions for an entity
- **Dismiss:** `dismiss_intention` — soft-deactivate; row preserved for history
- **Why it's novel:** Most memory systems store what happened. Prospective
  memory stores what *should happen* — making it possible for an AI ability to
  act on user-expressed intentions ("remind me to buy milk when I mention
  shopping") without relying on the user to repeat themselves every session.
  The FTS5 trigger match is lightweight and requires no embedding model, so it
  fires reliably even on resource-constrained devices.

**Example workflow:**
```python
# User says: "next time I ask about recipes, mention I'm vegetarian"
await mem.tool_intend(
    entity_name="Brian",
    trigger_text="recipe cooking meal food ingredient",
    action_text="Brian is vegetarian — do not suggest meat-based recipes",
)

# Later, at the start of each user turn:
matches = await mem.tool_check_intentions("Brian", "what can I make for dinner?")
# → Returns the vegetarian intention, fired_count incremented
```

### 6. Sensory / short-term buffer

Raw time-series readings before pattern extraction — numeric, categorical, or
composite sensor data with full rollup history.

- **Storage:** Tier 2 — `readings`, `reading_rollups`, `schedule_events`
- **Write:** `record`, `record/bulk`
- **Read:** `query_stream` (raw or hourly/daily/weekly rollup), `get_trends`
- **Retention:** Raw readings pruned after `RETENTION_DAYS` (default: 30 days).
  Rollups are never pruned — aggregates survive indefinitely.

### 7. Spatial memory (object location tracking)

Where did you put something? Spatial memory answers the "where did I leave X?"
question by tracking the last-known location of objects with time-decaying
confidence.

- **Storage:** Tier 5 — `locations` table (one active row per object, historical rows archived)
- **Write:** `locate` — store or update where an object was last seen; creates object and container entities automatically
- **Confirm:** `seen_at` — confirm object is still at its current location; bumps confidence and refreshes timestamp
- **Read:** `find` — return last known location with confidence and time since last seen; includes previous location so the user knows where else to look
- **History:** `location_history` — full trail of past sightings in reverse-chronological order
- **Decay:** Location confidence decays with a 24-hour half-life by default (`MEMORY_LOCATION_DECAY_HALFLIFE_HOURS`). An unconfirmed location drops to 50% after 24 h, 25% after 48 h, and floors at 5% — so it stays visible but clearly uncertain.

**What this covers:** movable household objects (keys, remotes, books, passports, glasses). Objects that get moved are automatically archived as "previous", keeping the full trail intact.

**What this does not cover:** precise XY coordinates, floor-plan navigation, or robotics wayfinding. Locations are room-entity references, not coordinates.

**Example workflow:**
```python
# User leaves keys somewhere
await mem.tool_locate("keys", "entryway table")

# Next morning, user asks
result = await mem.tool_find("keys")
# → "'keys' was last seen at 'entryway table' — 9 hours ago (confidence: 75%)."

# User walks past the entryway and confirms they're still there
await mem.tool_seen_at("keys", "entryway table")
# → "Confirmed: 'keys' still at 'entryway table' (confidence now 85%)."

# Keys have moved — update the location
await mem.tool_locate("keys", "kitchen counter", note="next to the coffee maker")
# → "Located: 'keys' is at 'kitchen counter'."
# Old location archived automatically.
```

---

## Retrieval modes

memory-mcp offers three retrieval modes, selectable per query:

| Mode | How it works | Best for |
|---|---|---|
| `vector` | Cosine similarity via sqlite-vec; requires embedding model | Semantic / natural-language queries |
| `keyword` | BM25/FTS5 full-text search; no embedding model needed | Exact terms, Pi/low-resource, fast lookup |
| `hybrid` | Both paths run in parallel; scores normalised and merged by max | Best recall overall (default) |

**Why FTS5?** Large language models construct agent queries using the actual
vocabulary of the stored facts, not paraphrases. BM25 often outperforms pure
vector search in this regime. FTS5 also works offline with no Ollama instance —
important for Raspberry Pi deployments.

**Token-budget context assembly** (`get_context_budget`) is a greedy fill
algorithm that ranks available memories and readings by relevance, then adds
them to the response until a token budget is exhausted. The `truncated` flag
tells the caller whether any items were omitted. Use `recall_mode="keyword"` for
Pi environments to skip the embedding call entirely.

---

## Trust and contradiction handling

Every stored memory carries a `source_trust` tier:

| Tier | Label | Example sources |
|---|---|---|
| 5 | `user` | Direct user statements, manual entries |
| 4 | `hardware` | Verified sensors, signed device data |
| 3 | `system` | Pattern engine promotions, LLM extraction |
| 2 | `inferred` | Working memory promotions, low-confidence extractions |
| 1 | `external` | Third-party imports, unverified webhooks |

**Cross-check rule:** Before writing a new memory at trust < `user`, the system
embeds the new fact and searches for any existing memory with higher trust
within cosine distance 0.15. If found, the write is blocked and returns a
descriptive message. This prevents sensor noise or external data from
overwriting things the user explicitly told the system.

**Supersession:** When a higher-trust fact is written that contradicts an older
lower-trust fact, the older memory is soft-deleted (`superseded_by` set) rather
than hard-deleted. The old fact remains in the database for audit purposes but
is excluded from all recall and context queries.

---

## How it works

### Pattern engine (Tier 3)

Runs every hour in the background. For each entity/metric combination with new
data:

1. Builds or updates rollups (hourly/daily/weekly aggregates)
2. Runs all six detectors on the fresh aggregates
3. Promotes stable findings to Tier 1 semantic memory (deduplicated via
   `promoted_patterns`)
4. Decays confidence on older memories (exponential, per-category halflife)
5. Consolidates closed episodic sessions into Tier 1 facts
6. Expires working memory tasks past their TTL
7. Prunes raw readings older than `RETENTION_DAYS`

### Episodic consolidation

When a session is closed, it is queued for consolidation. The pattern engine
picks up unconsolidated sessions (up to 20 per cycle), sends a compact
transcript to the configured LLM, and stores the extracted facts as `inferred`-
trust semantic memories. Sessions are marked `consolidated=1` after processing
(even on LLM error, to prevent retry loops).

---

## Integrations

### OpenHome abilities

memory-mcp speaks the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) over stdio. Any OpenHome ability can connect to it as an MCP client and
get access to all tools with zero additional infrastructure:

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

A well-integrated ability uses memory-mcp like this:

```python
# At the start of every session — pull what's relevant right now
context = await mem.get_context_budget("Brian", context_query="current session topic",
                                       token_budget=1200, recall_mode="hybrid")
# → inject into system prompt

# Check for pending intentions
matches = await mem.check_intentions("Brian", user_message)

# During conversation — store anything learned
await mem.remember("Brian", "Prefers responses under 3 sentences when busy",
                   category="preference")

# When the conversation ends — log the full session for future reference
await mem.close_session(session_id, summary="Discussed travel plans for April")
```

Ready-to-use ability files are included in `integrations/openhome/` — a
background daemon that injects context at session start and extracts new facts
as you talk, plus an interactive recall skill triggered by voice. See
`integrations/openhome/README.md` to get wired up.

> **Network note:** OpenHome abilities run in a cloud sandbox and need your
> memory-mcp server reachable from the internet. The included Cloudflare Tunnel
> guide (`integrations/cloudflare/README.md`) covers the full setup — stable
> HTTPS subdomain, no port-forwarding, no dynamic DNS, free tier.

### Home Assistant

Home Assistant can push sensor readings to memory-mcp over HTTP using
`rest_command`. Once wired up, every sensor state change builds your home's
long-term memory:

```yaml
# configuration.yaml
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
```

Wire these to HA automations that fire on sensor state changes and your home
starts building a rich, queryable history automatically. After a few weeks the
pattern engine starts surfacing insights you didn't ask for: preferred
temperatures per room, typical arrival times, energy usage patterns, air quality
trends.

If you'd rather not modify `configuration.yaml` at all, `integrations/ha_state_poller.py`
is a standalone script that polls the HA REST API on a schedule and pushes state
changes automatically — same result, zero HA config changes required.

### MQTT / IoT devices

The included `integrations/mqtt_bridge.py` is a standalone process that bridges
any MQTT-speaking device to memory-mcp. This covers Zigbee2MQTT, Tasmota,
ESPHome, and any other MQTT publisher. Configure topic mappings in
`mqtt_mappings.json`; the bridge handles type detection, entity creation,
batching, and reconnection.

### Other devices and services

Anything that can make an HTTP POST can feed memory-mcp. Some ideas:

- **Node-RED** flows pushing sensor aggregates on a schedule
- **Python scripts** scraping wearable health data (sleep scores, HRV, steps)
- **Calendar integrations** calling `/schedule` to surface upcoming events
- **Wearables via webhook** — Garmin, Oura, Withings all support webhook exports
- **Voice assistant post-processing** — push a mood composite to `/record` after
  each conversation; the pattern engine builds a long-term emotional landscape

---

## What you get out of the box

When you first clone and run memory-mcp, you get:

- A **persistent SQLite database** with full-text and vector search — no
  separate database server to run
- A **FastAPI HTTP server** on port 8900 with a clean REST API and
  auto-generated Swagger docs at `/docs`
- An **admin web dashboard** at `/admin` where you can browse entities, inspect
  memories, watch the readings stream, manage the API token, and prune old data
- An **MCP stdio server** ready to connect to any MCP-compatible AI framework
- A **background pattern engine** that starts learning from your data immediately
- **Bearer token auth** auto-configured on first startup — token visible in the
  admin settings page
- **Docker Compose** for single-command production deployment:
  `docker compose up -d`
- Full test suite (722 tests, no Ollama or internet connection required to run)
- Comprehensive documentation covering every aspect of installation, operation,
  and extension

---

## Why run it locally?

Your personal data — health metrics, presence patterns, mood states, preferences,
conversation history — should not live on someone else's server. memory-mcp runs
on the same machine as your AI stack: no cloud dependency, no subscription, no
data leaving your home.

It works with a local [Ollama](https://ollama.ai) instance for embeddings and
LLM inference, and it is fully compatible with any OpenAI-compatible provider if
you prefer cloud models. Switching providers is one environment variable.

It also runs on a **Raspberry Pi**. The keyword retrieval mode (`recall_mode=
"keyword"`) bypasses the embedding model entirely — FTS5/BM25 search works with
no GPU, no Ollama, and no network access. The token-budget context tool
(`get_context_budget`) keeps responses within the limits of smaller models by
greedily filling a configurable token budget and signalling when items were
omitted.

---

## Getting started

```bash
git clone <repo-url>
cd memory-mcp

pip install -r requirements.txt

# Pull the embedding and LLM models (if using local Ollama)
ollama pull nomic-embed-text
ollama pull llama3.2

# Start the HTTP API + admin UI
python api.py
# → Admin UI at http://localhost:8900/admin/
# → API docs at http://localhost:8900/docs
# → Your bearer token is printed to the console on first startup
```

That is the entire setup. The pattern engine starts running immediately, and
after a few days it will surface patterns you didn't ask it to find. That is
when it gets interesting.

From there, see [Installation](installation.md) for environment configuration,
[Quickstart](quickstart.md) for your first entity and memory, and
[API Reference](api-reference.md) for the full endpoint list.

To connect Home Assistant or MQTT devices, see the sections above and
[integrations/README.md](../integrations/README.md).

To connect an OpenHome ability or any MCP client, the server is
`python server.py` and the config block is two lines.

---

## The bigger picture

A home that remembers you is just the beginning.

The same memory layer that tracks room temperatures and presence patterns can
track anything with a number or a category: sleep quality, exercise consistency,
mood trends, energy usage, air quality, medication timing, plant watering
schedules, anything you care about measuring.

And because every stable pattern becomes a semantic memory, your AI abilities do
not need to know the schema in advance. They ask in plain English. The system
finds the answer.

> "What does Brian prefer for his bedroom at night?"
> → "Brian's bedroom temperature is consistently around 66°F at 22:00 (stable over 14 days)"

> "Has anything unusual happened with the air quality lately?"
> → "Anomaly: living room CO₂ was 1840 ppm at 15:30 yesterday (3.2 std devs above normal 620 ppm)"

> "When is Brian usually home?"
> → "Brian's presence is 'home' at 18:00 (88% of 21 readings), 19:00 (94% of 21 readings)"

> "next time I mention shopping, remind me about milk"
> → Stored as a prospective intention — fires automatically the next time "shopping" or related words appear in conversation

That is what a home that knows you feels like. And it runs on a Raspberry Pi.
