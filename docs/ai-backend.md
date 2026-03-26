# AI Backend Configuration

memory-mcp uses the **OpenAI-compatible REST API** for both embeddings and LLM
calls.  The default and recommended setup is a local [Ollama](https://ollama.ai)
instance — no cloud account, no API key, no data leaving your machine.  It also
works with OpenAI, LM Studio, Together AI, and any other provider that
implements `/v1/embeddings` and `/v1/chat/completions`.

## Configuration

All settings are controlled by environment variables.  The server reads them at
startup; no code changes are needed to switch providers.

| Variable | Default | Description |
|---|---|---|
| `MEMORY_AI_BASE_URL` | `http://localhost:11434/v1` | Base URL for **both** embed and LLM (shared fallback) |
| `MEMORY_AI_API_KEY` | *(empty)* | API key for the shared backend — leave empty for local providers |
| `MEMORY_EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `MEMORY_EMBED_DIM` | `768` | Embedding vector dimension — **must match the model** |
| `MEMORY_LLM_MODEL` | `llama3.2` | Chat/generation model for `extract_and_remember` |
| `MEMORY_LLM_BASE_URL` | *(same as `MEMORY_AI_BASE_URL`)* | Override base URL for LLM calls only — set to route LLM to a different host |
| `MEMORY_LLM_API_KEY` | *(same as `MEMORY_AI_API_KEY`)* | Override API key for LLM calls only |
| `MEMORY_AI_TIMEOUT` | `30` | HTTP timeout in seconds for embedding calls; LLM calls use `max(timeout, 60)` |

## Provider Examples

### Ollama (default — local, no key required)

```bash
# Ollama exposes OpenAI-compatible endpoints at /v1
export MEMORY_AI_BASE_URL=http://localhost:11434/v1
export MEMORY_AI_API_KEY=         # leave empty
export MEMORY_EMBED_MODEL=nomic-embed-text
export MEMORY_EMBED_DIM=768
export MEMORY_LLM_MODEL=llama3.2
```

Ollama must have the models pulled:
```bash
ollama pull nomic-embed-text
ollama pull llama3.2
```

### OpenAI

```bash
export MEMORY_AI_BASE_URL=https://api.openai.com/v1
export MEMORY_AI_API_KEY=sk-...
export MEMORY_EMBED_MODEL=text-embedding-3-small
export MEMORY_EMBED_DIM=1536
export MEMORY_LLM_MODEL=gpt-4o-mini
```

### LM Studio (local)

```bash
export MEMORY_AI_BASE_URL=http://localhost:1234/v1
export MEMORY_AI_API_KEY=lm-studio    # LM Studio requires any non-empty key
export MEMORY_EMBED_MODEL=<your-embed-model-name>
export MEMORY_EMBED_DIM=<model-dimension>
export MEMORY_LLM_MODEL=<your-chat-model-name>
```

### Together AI

```bash
export MEMORY_AI_BASE_URL=https://api.together.xyz/v1
export MEMORY_AI_API_KEY=<together-api-key>
export MEMORY_EMBED_MODEL=togethercomputer/m2-bert-80M-8k-retrieval
export MEMORY_EMBED_DIM=768
export MEMORY_LLM_MODEL=meta-llama/Llama-3-8b-chat-hf
```

## Changing Embedding Models

> **Important:** The embedding dimension is baked into the `memory_vectors`
> SQLite virtual table at DB creation time.  Changing `MEMORY_EMBED_MODEL` to a
> model with a different dimension requires re-embedding all memories.

Steps to swap embedding models:

```bash
# 1. Set the new model and dimension
export MEMORY_EMBED_MODEL=mxbai-embed-large
export MEMORY_EMBED_DIM=1024

# 2. Pull the model (Ollama) or ensure it's available via your provider
ollama pull mxbai-embed-large

# 3. Dry run — preview what will happen
python reembed.py --dry-run

# 4. Re-embed all memories (non-destructive — only rebuilds memory_vectors)
python reembed.py
```

`reembed.py` drops and recreates the `memory_vectors` table with the new
dimension, then re-embeds every memory.  All other data (readings, sessions,
relations, etc.) is untouched.

For a full walkthrough — including when to do this, how long it takes, and what
to do if it's interrupted — see `docs/maintenance.md`.

**Common model dimensions:**

| Model | Provider | Dim |
|---|---|---|
| `nomic-embed-text` | Ollama | 768 |
| `mxbai-embed-large` | Ollama | 1024 |
| `text-embedding-3-small` | OpenAI | 1536 |
| `text-embedding-3-large` | OpenAI | 3072 |
| `text-embedding-ada-002` | OpenAI | 1536 |

## API Wire Format

### Embeddings — `POST {AI_BASE_URL}/embeddings`

Request:
```json
{
  "model": "nomic-embed-text",
  "input": "Brian prefers 68°F at night"
}
```

Response (used field):
```json
{
  "data": [{ "embedding": [0.12, -0.34, ...] }]
}
```

### LLM — `POST {AI_BASE_URL}/chat/completions`

Request:
```json
{
  "model": "llama3.2",
  "messages": [{ "role": "user", "content": "..." }],
  "stream": false
}
```

Response (used field):
```json
{
  "choices": [{ "message": { "content": "..." } }]
}
```

Authorization header (when `MEMORY_AI_API_KEY` is set):
```
Authorization: Bearer sk-...
```

## Split Backends (embed on one host, LLM on another)

Embeddings are called on every `remember`, `recall`, `get_context`, and
`locate` — constantly, fast, and lightweight.  LLM is only called by
`extract_and_remember` and the pattern engine's episodic consolidation — rare
and heavy.  You can route them to different machines.

Set `MEMORY_LLM_BASE_URL` (and optionally `MEMORY_LLM_API_KEY`) to override
the LLM destination.  The embed backend continues to use `MEMORY_AI_BASE_URL`.

### Example: local embed, GPU machine for LLM

```bash
# Embed — local Ollama on this machine (CPU, always running)
export MEMORY_AI_BASE_URL=http://localhost:11434/v1
export MEMORY_EMBED_MODEL=nomic-embed-text
export MEMORY_EMBED_DIM=768

# LLM — separate machine with a GPU
export MEMORY_LLM_BASE_URL=http://192.168.1.50:11434/v1
export MEMORY_LLM_MODEL=llama3.2
```

### Example: two Ollama instances on the same machine (different ports)

```bash
export MEMORY_AI_BASE_URL=http://localhost:11434/v1   # instance 1 — CPU, embed
export MEMORY_LLM_BASE_URL=http://localhost:11435/v1  # instance 2 — GPU, LLM
export MEMORY_EMBED_MODEL=nomic-embed-text
export MEMORY_LLM_MODEL=llama3.2
```

### Example: local embed, OpenAI for LLM

```bash
export MEMORY_AI_BASE_URL=http://localhost:11434/v1
export MEMORY_EMBED_MODEL=nomic-embed-text
export MEMORY_EMBED_DIM=768

export MEMORY_LLM_BASE_URL=https://api.openai.com/v1
export MEMORY_LLM_API_KEY=sk-...
export MEMORY_LLM_MODEL=gpt-4o-mini
```

At startup, `api.py` probes each backend separately and logs the result — you'll
see distinct log lines for the embed and LLM backends when they differ.

---

## Architecture Note

The AI backend is used in two places within `server.py`:

| Function | Purpose | Endpoint |
|---|---|---|
| `embed(text)` | Vector index for semantic search | `/v1/embeddings` |
| `_call_llm(prompt, model)` | Fact extraction from conversation text | `/v1/chat/completions` |

Both functions are independently mockable in tests, making the test suite
completely offline — no AI backend is needed to run `python -m pytest`.
