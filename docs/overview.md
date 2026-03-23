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

## Motivation

Modern AI assistants are stateless by design. Every MCP tool call, every Home
Assistant automation, every voice interaction starts with a blank slate. The
large language models powering these abilities are brilliant at reasoning — but
they cannot remember yesterday, let alone last month.

The standard workaround is to dump everything into a system prompt and hope it
fits in the context window. That approach breaks under the weight of a real home
with real history.

memory-mcp was built to solve this properly:

- **Semantic search, not keyword grep.** Facts are embedded as vectors. Ask
  "does Brian have any dietary restrictions?" and the system finds "is
  vegetarian", even if that exact phrase was never used.
- **Structured sensor history, not a log file.** Time-series readings are stored
  in a tiered system — raw readings roll up into hourly, daily, and weekly
  aggregates. You can ask for the last hour or the last month with the same call.
- **Automatic pattern promotion.** A background engine watches the sensor stream
  and promotes stable patterns — consistent temperatures, time-of-day habits,
  anomalies, correlations — directly into the semantic memory layer. The system
  learns without you teaching it.
- **Works with what you already have.** HTTP API, MCP stdio transport, MQTT
  bridge. If it can send a webhook, it can feed memory-mcp.

---

## How it works

memory-mcp organises everything into four tiers:

```
Tier 1 — Semantic memory
  People, places, things → facts → embedded vectors → instant semantic search

Tier 1.5 — Episodic memory
  Conversation sessions with full transcripts and LLM-generated summaries

Tier 2 — Time-series store
  Raw sensor readings → hourly/daily/weekly rollups → trend queries

Tier 3 — Pattern engine (background, runs every hour)
  Watches Tier 2 → detects stable patterns → writes insight memories to Tier 1
```

The pattern engine is where memory-mcp earns its keep. It runs four detectors:

| Detector | What it finds | Example |
|---|---|---|
| Stable average | A numeric metric that barely changes | "Brian's resting heart rate is consistently 62 bpm" |
| Trend | A metric that is rising or falling | "Living room CO₂ has been rising (+22% over 6 days)" |
| Dominant categorical | A state that dominates a metric | "Brian's mood is predominantly 'focused' (78% of days)" |
| Time-of-day | A state that is consistent at a specific hour | "Brian's presence is 'home' at 19:00 (91% of readings)" |
| Anomaly | A reading that is 3+ std devs from normal | "Anomaly: bedroom temperature was 88°F at 14:30 (4.1 std devs above normal 69°F)" |
| Correlation | Two metrics that move together | "Brian's heart_rate and stress_level are positively correlated (r=0.84)" |

Every pattern that gets promoted becomes a semantic memory — searchable by any
AI ability using natural language, with no SQL required.

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
context = await mem.get_context("Brian", context_query="current session topic")
# → inject into system prompt: Brian's relevant memories, latest readings, schedule

# During conversation — store anything learned
await mem.remember("Brian", "Prefers responses under 3 sentences when busy",
                   category="preference")

# When the conversation ends — log the full session for future reference
await mem.close_session(session_id, summary="Discussed travel plans for April")
```

The ability gets instant access to relevant personal context without you having
to manually curate it. It grows richer every session.

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

You can also push semantic facts directly — for example, an HA automation that
fires when a grocery delivery arrives could call `/remember` to store
"Brian received a grocery delivery on 2026-03-21".

If you'd rather not modify `configuration.yaml` at all, `integrations/ha_state_poller.py`
is a standalone script that polls the HA REST API on a schedule and pushes state
changes automatically — same result, zero HA config changes required.

### MQTT / IoT devices

The included `integrations/mqtt_bridge.py` is a standalone process that bridges
any MQTT-speaking device to memory-mcp. This covers:

- **Zigbee2MQTT** — air quality sensors, door sensors, motion sensors, power plugs
- **Tasmota** devices — temperature, humidity, energy monitoring
- **ESPHome** nodes — custom sensors, buttons, LED state
- **Any other MQTT publisher** — if it publishes to a topic, it can feed memory

Configure topic mappings in `mqtt_mappings.json`:

```json
{
  "zigbee2mqtt/bedroom_sensor": {
    "entity": "bedroom",
    "entity_type": "room",
    "metrics": {
      "temperature": {"unit": "F"},
      "humidity":    {"unit": "%"},
      "co2":         {"unit": "ppm"}
    }
  }
}
```

The bridge handles the rest — type detection, entity creation, batching, and
reconnection. Three readings from a single JSON payload become three independent
time-series in memory-mcp, each with full rollup and pattern detection.

### Other devices and services

Anything that can make an HTTP POST can feed memory-mcp. The API is intentionally
simple — no authentication complexity for local network use, straightforward JSON
shapes, and a `/record/bulk` endpoint for batch ingestion.

Some ideas people have wired up or could wire up:

- **Node-RED** flows pushing sensor aggregates on a schedule
- **Python scripts** that scrape wearable health data (sleep scores, HRV, steps)
  and push it as readings for `Brian` — the pattern engine will find the trends
- **Calendar integrations** that call `/schedule` to keep upcoming events in the
  memory layer so AI abilities can reference them
- **Shell scripts** running on a cron that push daily summaries as facts via
  `/remember`
- **Wearables via webhook** — Garmin, Oura, Withings all support webhook exports;
  point them at `/record` and your health data starts building a personal baseline
- **Voice assistant post-processing** — after each conversation, push a mood
  composite to `/record` and let the pattern engine build a long-term emotional
  landscape

---

## What you get out of the box

When you first clone and run memory-mcp, you get:

- A **persistent SQLite database** with full-text and vector search — no separate
  database server to run
- A **FastAPI HTTP server** on port 8900 with a clean REST API and auto-generated
  Swagger docs at `/docs`
- An **admin web dashboard** at `/admin` where you can browse entities, inspect
  memories, watch the readings stream, manage the API token, and prune old data
- An **MCP stdio server** ready to connect to any MCP-compatible AI framework
- A **background pattern engine** that starts learning from your data immediately
- **Bearer token auth** auto-configured on first startup — token visible in the
  admin settings page
- **Docker Compose** for single-command production deployment:
  `docker compose up -d`
- Full test suite (336 tests, no Ollama or internet connection required to run)
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

That is what a home that knows you feels like. And it runs on a Raspberry Pi.
