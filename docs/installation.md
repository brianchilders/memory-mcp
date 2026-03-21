# Installation

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| SQLite | 3.35+ | Bundled with Python — no separate install |
| An AI backend | — | Ollama (local) or any OpenAI-compatible provider |

## 1. Clone the repository

```bash
git clone <repo-url>
cd memory-mcp
```

## 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

Or individually:
```bash
pip install mcp sqlite-vec httpx fastapi uvicorn jinja2 python-dotenv
```

For the MQTT bridge (optional, separate process):
```bash
pip install "paho-mqtt>=2.0"
```

For running tests:
```bash
pip install pytest pytest-asyncio pytest-httpx
```

All dependencies in one command:
```bash
pip install -r requirements.txt
```

## 3. Set up an AI backend

memory-mcp needs two AI capabilities:

- **Embeddings** — to store and search memories semantically
- **LLM** (optional) — only used by the `extract_and_remember` tool

The default configuration points to a local Ollama instance.

### Option A: Ollama (recommended for local/home use)

```bash
# Install Ollama: https://ollama.com
ollama pull nomic-embed-text   # embedding model (required)
ollama pull llama3.2           # LLM for extract_and_remember (optional)
```

Ollama runs at `http://localhost:11434` by default. If it's on another machine
(e.g. a home server at `192.168.1.10`), set the base URL:

```bash
export MEMORY_AI_BASE_URL=http://192.168.1.10:11434/v1
```

### Option B: OpenAI

```bash
export MEMORY_AI_BASE_URL=https://api.openai.com/v1
export MEMORY_AI_API_KEY=sk-...
export MEMORY_EMBED_MODEL=text-embedding-3-small
export MEMORY_EMBED_DIM=1536
export MEMORY_LLM_MODEL=gpt-4o-mini
```

See `docs/ai-backend.md` for full provider examples including LM Studio and Together AI.

## 4. Verify sqlite-vec loads

sqlite-vec is a SQLite extension that powers semantic search. Verify it loads correctly:

```bash
python -c "import sqlite_vec; print('sqlite-vec OK:', sqlite_vec.__version__)"
```

If this fails, see [Troubleshooting → sqlite-vec won't load](troubleshooting.md#sqlite-vec-wont-load).

## 5. First run

### MCP server (for OpenHome / Claude integration)

```bash
python server.py
```

The server starts, creates the database at `memory.db`, and waits for MCP tool calls over stdio. No output is expected — it communicates via JSON-RPC on stdin/stdout.

### HTTP API + admin UI (for Home Assistant, scripts, Node-RED)

```bash
python api.py
# Listening on http://0.0.0.0:8900
# Admin UI: http://localhost:8900/admin/
```

## 6. Verify the server is running

```bash
curl http://localhost:8900/health
```

Expected response:
```json
{
  "ok": true,
  "entities": 0,
  "memories": 0,
  "readings": 0,
  "rollups": 0,
  "patterns": 0,
  "schedule_events": 0
}
```

## 7. Configure with a .env file

Copy the example and fill in your values:

```bash
cp .env.example .env
# edit .env
```

The server loads `.env` automatically on startup (`python-dotenv` is included in
`requirements.txt`). No shell sourcing needed.

Key settings in `.env`:

```bash
MEMORY_AI_BASE_URL=http://localhost:11434/v1
MEMORY_EMBED_MODEL=nomic-embed-text
MEMORY_EMBED_DIM=768
MEMORY_LLM_MODEL=llama3.2

# Optional: override the auto-generated API token
# MEMORY_API_TOKEN=your-token-here
```

For production deployment, see `docs/deployment.md`.

## Database location

The SQLite database is created at `memory.db` in the working directory by default.
To change the location, edit `DB_PATH` in `server.py`:

```python
DB_PATH = "/data/memory-mcp/memory.db"
```

## Directory structure after installation

```
memory-mcp/
  server.py           MCP server (stdio)
  api.py              HTTP API + admin UI
  admin.py            Admin UI router
  reembed.py          Re-embedding utility
  memory.db           Created on first run
  templates/admin/    Admin UI HTML templates
  docs/               Documentation
  integrations/       Standalone tools (MQTT bridge, etc.)
  tests/              Test suite
```

## Next steps

- **First usage:** `docs/quickstart.md`
- **Running as a service:** `docs/deployment.md`
- **Something went wrong:** `docs/troubleshooting.md`
