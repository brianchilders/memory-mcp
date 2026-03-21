# OpenHome Integration

This folder contains two OpenHome Ability examples that connect your OpenHome
agent to memory-mcp, giving it persistent memory across sessions.

> **Official OpenHome docs are always the ground truth.**
> The SDK and dashboard evolve. When in doubt, consult
> [docs.openhome.com](https://docs.openhome.com) rather than relying solely
> on the examples here.

---

## What this integration does

| Ability | Type | Pattern | What it does |
|---|---|---|---|
| `background.py` | Background daemon | Observer | On session start: injects stored memories into the agent prompt. During the session: extracts new facts from conversation and stores them. |
| `main.py` | Interactive skill | Responder | When triggered by voice: asks what to recall, searches memory-mcp, and speaks the result. |

Together they create a voice agent that remembers who you are, what you prefer,
and what you've talked about — and gets smarter every session.

---

## How it works

### Session start (background.py)

When your session begins, the background daemon calls `/get_context` on
memory-mcp for the configured person. The returned context — preferences,
habits, recent observations — is injected directly into the agent's live
personality prompt via `update_personality_agent_prompt()`. Your agent
immediately knows your stored facts before you say a word.

### Observation loop (background.py)

Every `OBSERVATION_INTERVAL_SEC` seconds (default: 60), the daemon checks for
new conversation turns. Once `MIN_NEW_TURNS_TO_EXTRACT` new turns have
accumulated (default: 4), it feeds the excerpt to the agent's LLM with a
fact-extraction prompt, then pushes any extracted facts to `/remember`. Over
time, memory-mcp builds a self-updating profile — entirely hands-free.

### Voice recall (main.py)

Triggered by a hotword phrase (e.g. "what do you remember"), the skill asks
what you'd like to recall, passes your voice query to `/recall` for semantic
search, formats the result as natural speech via the LLM, and speaks it.

---

## Prerequisites

- **memory-mcp** running and accessible over HTTPS from the internet
  (see [Network setup](#network-setup) below)
- **Bearer token** from the memory-mcp admin UI at `/admin/settings`
- **OpenHome account** with an agent created and the DevKit running
- **httpx** added to your ability's requirements

---

## Network setup

OpenHome abilities run in a cloud sandbox. They cannot reach `localhost` or
LAN addresses like `192.168.x.x`. Your memory-mcp server must be reachable
from the internet.

**Option A — Cloudflare Tunnel (recommended, free)**

Cloudflare Tunnel creates a stable HTTPS subdomain that proxies to your local
server. No port-forwarding, no firewall changes, no dynamic DNS.

```bash
# Install cloudflared
brew install cloudflared          # macOS
# or: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

# Log in (one-time)
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create memory-mcp

# Start the tunnel (replace <tunnel-id> with the ID printed above)
cloudflared tunnel route dns <tunnel-id> memory.yourdomain.com
cloudflared tunnel run --url http://localhost:8900 <tunnel-id>
```

Your memory-mcp API is now available at `https://memory.yourdomain.com`.
Use this URL as `MEMORY_API_URL` in the ability configuration below.

> **Official reference:** [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)

**Option B — ngrok (quick for testing)**

```bash
ngrok http 8900
# Prints a URL like https://abc123.ngrok-free.app
```

Use the ngrok URL as `MEMORY_API_URL`. Note: free ngrok URLs change on restart.

**Option C — Self-hosted OpenHome**

If you run the OpenHome server on the same LAN as memory-mcp (e.g. both on
the same machine or a Raspberry Pi), you can use a local URL directly:
`http://localhost:8900` or `http://192.168.x.x:8900`.

---

## Configuration

Both abilities share the same three config values. Edit the `CONFIGURATION`
block near the top of each file (search for `# ← CHANGE`):

| Variable | Where to find it |
|---|---|
| `MEMORY_API_URL` | Your public memory-mcp URL (see Network setup above) |
| `MEMORY_API_TOKEN` | `http://<your-host>:8900/admin/settings` → copy the token |
| `PERSON_NAME` | The entity name used in memory-mcp (usually your first name) |

**Never commit a file with real tokens to a public repository.** Deploy your
configured ability zips through the OpenHome dashboard only.

---

## Installation

Both abilities are deployed as separate zip files through the OpenHome dashboard.

> **Official reference:** [Building an Ability](https://docs.openhome.com)

### background.py (memory context daemon)

1. Create a new folder: `memory_context_daemon/`
2. Copy `background.py` into it and edit the configuration
3. Create `requirements.txt` containing:
   ```
   httpx>=0.27
   ```
4. Zip the folder: `zip -r memory_context_daemon.zip memory_context_daemon/`
5. Upload via **OpenHome Dashboard → Abilities → Upload Ability**
6. Install the ability on your agent
7. No trigger words needed — background daemons start automatically with the session

### main.py (memory recall skill)

1. Create a new folder: `memory_recall/`
2. Copy `main.py` into it and edit the configuration
3. Create `requirements.txt` containing:
   ```
   httpx>=0.27
   ```
4. Zip the folder: `zip -r memory_recall.zip memory_recall/`
5. Upload via **OpenHome Dashboard → Abilities → Upload Ability**
6. Install the ability on your agent
7. In the ability settings, add trigger phrases:
   - `what do you remember`
   - `recall memory`
   - `what do you know about`
   - `what have you learned about me`

---

## Customising the background daemon

### Adjust extraction frequency

In `background.py`:

```python
OBSERVATION_INTERVAL_SEC = 60   # how often to check for new turns (seconds)
MIN_NEW_TURNS_TO_EXTRACT = 4    # wait for at least this many new turns
```

Raising `MIN_NEW_TURNS_TO_EXTRACT` reduces LLM calls and avoids extracting
facts from short exchanges. Lower it to capture more.

### Change the context query

```python
CONTEXT_QUERY = "preferences, habits, routines, and recent observations"
```

This string is sent as the semantic search query to `/get_context`. Tune it
to what your agent should know at session start. Examples:
- `"health, sleep, and fitness preferences"`
- `"home automation preferences and device settings"`
- `"work schedule, meetings, and priorities"`

### Store mood states

The background daemon can also push composite mood readings by adding a
`/record` call after conversation observation. Edit `background.py` to add:

```python
def _record_mood(self, worker, turns: list, log) -> None:
    """Infer and record a mood state from recent conversation turns."""
    # TODO: use text_to_text_response() to infer mood from turns
    # Then call POST /record with:
    #   entity_name: PERSON_NAME
    #   metric: "mood"
    #   value: {"mood": "calm", "confidence": 0.9}   # composite reading
    pass
```

The pattern engine will promote stable mood states to searchable memories
automatically after enough data accumulates.

---

## What the agent says vs. what is stored

The OpenHome SDK injects the memory-mcp context via
`update_personality_agent_prompt()`, which appends it to the live agent
personality. The agent will speak from this context naturally — it does not
announce "I found a memory about you."

When the recall skill (`main.py`) is triggered, the agent explicitly speaks
the retrieved memories formatted as natural prose.

Both paths respect the OpenHome voice UX standard: no markdown, no lists,
no bullet points — just natural speech.

---

## Troubleshooting

**Background daemon starts but nothing appears in memory-mcp**

- Check the Live Editor logs in the dashboard for `memory-mcp:` log lines
- Verify `MEMORY_API_URL` is reachable: `curl https://memory.yourdomain.com/health`
- Verify the token is correct: the `/health` endpoint is unauthenticated, but
  `/get_context` and `/remember` require the bearer token
- Check the memory-mcp admin UI at `/admin/` for recent activity

**Recall skill returns "I don't have anything stored about that yet"**

- The skill returned results but the LLM produced empty output — check the
  Live Editor logs for `memory-mcp recall failed` messages
- The `/recall` endpoint returned an empty result — no memories match the query
  yet; run a few sessions with the background daemon first to build up facts

**`update_personality_agent_prompt()` does not seem to take effect**

Context injection happens at session start. If you restart the ability
mid-session, the prompt update runs again — check the Live Editor logs for
`context injected` confirmation.

**Token errors (401 Unauthorized)**

The bearer token is wrong or missing. Retrieve the current token from
`http://<your-host>:8900/admin/settings` and update `MEMORY_API_TOKEN` in
both ability files, then re-zip and re-upload.

> **Official OpenHome troubleshooting:** [docs.openhome.com](https://docs.openhome.com)
