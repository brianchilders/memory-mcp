"""
memory-mcp-server — Unified semantic memory + time-series intelligence layer.

Architecture:
  Tier 1 — Semantic memory   (entities, memories, relations, vectors)
  Tier 2 — Time-series store (readings: numeric, categorical, composite JSON)
  Tier 3 — Pattern engine    (background task: promotes stable trends → Tier 1)

Storage:  SQLite + sqlite-vec (cosine similarity)
Embeddings: Ollama (nomic-embed-text, 768-dim, swappable via config)
Transport: stdio MCP  (wire into OpenHome SDK)

Install:
    pip install mcp sqlite-vec httpx

Pull embedding model (Ollama):
    ollama pull nomic-embed-text
"""

import asyncio
import json
import logging
import math
import os
import secrets
import sqlite3
import struct
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import sqlite_vec
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Load .env file if present — must run before os.environ.get() calls below.
# python-dotenv is optional: install with `pip install python-dotenv`
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────
# MEMORY_DB_PATH overrides the default location (memory.db next to server.py)
DB_PATH = Path(os.environ.get("MEMORY_DB_PATH", str(Path(__file__).parent / "memory.db")))

# ── AI backend — OpenAI-compatible (works with Ollama, OpenAI, LM Studio, etc.) ──
#
# Ollama (default — local, no key needed):
#   MEMORY_AI_BASE_URL = http://localhost:11434/v1
#   MEMORY_AI_API_KEY  = (empty)
#   MEMORY_EMBED_MODEL = nomic-embed-text
#   MEMORY_EMBED_DIM   = 768
#   MEMORY_LLM_MODEL   = llama3.2
#
# OpenAI:
#   MEMORY_AI_BASE_URL = https://api.openai.com/v1
#   MEMORY_AI_API_KEY  = sk-...
#   MEMORY_EMBED_MODEL = text-embedding-3-small
#   MEMORY_EMBED_DIM   = 1536
#   MEMORY_LLM_MODEL   = gpt-4o-mini
#
# LM Studio (local):
#   MEMORY_AI_BASE_URL = http://localhost:1234/v1
#   MEMORY_AI_API_KEY  = lm-studio
#   MEMORY_EMBED_MODEL = <loaded-embed-model>
#   MEMORY_EMBED_DIM   = <model-dim>
#   MEMORY_LLM_MODEL   = <loaded-chat-model>
#
AI_BASE_URL = os.environ.get("MEMORY_AI_BASE_URL", "http://localhost:11434/v1")
AI_API_KEY  = os.environ.get("MEMORY_AI_API_KEY",  "")
EMBED_MODEL = os.environ.get("MEMORY_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM   = int(os.environ.get("MEMORY_EMBED_DIM",   "768"))
LLM_MODEL   = os.environ.get("MEMORY_LLM_MODEL",   "llama3.2")

TOP_K_DEFAULT           = 5
PATTERN_INTERVAL        = 3600   # seconds between pattern engine runs
RETENTION_DAYS          = 30     # raw readings older than this are deleted by _prune_readings()
CONTRADICTION_THRESHOLD = 0.85   # cosine similarity above which an older memory is superseded

# ── Logging ────────────────────────────────────────────────────────────────────
#
# MEMORY_LOG_LEVEL  = DEBUG | INFO | WARNING | ERROR  (default: INFO)
# MEMORY_LOG_FORMAT = pretty | json                   (default: pretty)
#
# pretty — human-readable timestamped lines
# json   — one JSON object per line, suitable for log aggregators (Loki, etc.)

class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""
    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)


def setup_logging() -> None:
    """Configure root logger based on MEMORY_LOG_LEVEL and MEMORY_LOG_FORMAT."""
    level = getattr(
        logging,
        os.environ.get("MEMORY_LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    )
    fmt = os.environ.get("MEMORY_LOG_FORMAT", "pretty").lower()
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    logging.basicConfig(level=level, handlers=[handler], force=True)


log = logging.getLogger("memory-mcp")

# ── API token (in-memory cache populated by _init_server_config) ───────────────
_api_token: str | None = None


# ── Database init ──────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Open DB, load sqlite-vec extension, enable WAL for concurrent access."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    sqlite_vec.load(db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    """Create all tables. Safe to call repeatedly (IF NOT EXISTS)."""
    db = get_db()
    db.executescript("""

    -- ══════════════════════════════════════════════════════════════════════════
    -- TIER 1 — Semantic memory
    -- ══════════════════════════════════════════════════════════════════════════

    -- Central identity node.
    -- type: 'person' | 'house' | 'room' | 'device' | <any string — open schema>
    -- meta: free-form JSON for structured attributes (age, role, diet, etc.)
    CREATE TABLE IF NOT EXISTS entities (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL UNIQUE,
        type      TEXT NOT NULL DEFAULT 'person',
        meta      TEXT NOT NULL DEFAULT '{}',
        created   REAL NOT NULL,
        updated   REAL NOT NULL
    );

    -- Individual fact associated with an entity.
    -- category: 'preference' | 'habit' | 'routine' | 'relationship' | 'insight' | 'general'
    -- source:   'user' | <ability name> | 'pattern_engine'
    -- confidence: 0.0-1.0  (1.0 = explicit statement, <1.0 = inferred)
    CREATE TABLE IF NOT EXISTS memories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        fact        TEXT NOT NULL,
        category    TEXT NOT NULL DEFAULT 'general',
        confidence  REAL NOT NULL DEFAULT 1.0,
        source      TEXT,
        created     REAL NOT NULL,
        updated     REAL NOT NULL
    );

    -- Directed relationship graph between entities.
    -- e.g.  Brian --[spouse]--> Sarah
    --       Emma  --[lives_in]--> house
    CREATE TABLE IF NOT EXISTS relations (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_a  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        entity_b  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        rel_type  TEXT NOT NULL,
        meta      TEXT NOT NULL DEFAULT '{}',
        created   REAL NOT NULL,
        UNIQUE(entity_a, entity_b, rel_type)
    );

    -- memory_vectors is created separately below (dynamic EMBED_DIM).

    -- ══════════════════════════════════════════════════════════════════════════
    -- TIER 2 — Time-series store
    -- ══════════════════════════════════════════════════════════════════════════

    -- A single time-stamped reading attached to an entity.
    -- metric:     'temperature' | 'mood' | 'presence' | 'heart_rate' | <any>
    -- unit:       'F' | 'C' | 'label' | 'boolean' | 'lux' | <any>
    -- value_type: 'numeric'     -> value_num populated
    --             'categorical' -> value_cat populated (e.g. "happy", "present")
    --             'composite'   -> value_json populated (e.g. {"mood":"calm","conf":0.9})
    CREATE TABLE IF NOT EXISTS readings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        metric      TEXT NOT NULL,
        unit        TEXT,
        value_type  TEXT NOT NULL DEFAULT 'numeric',
        value_num   REAL,
        value_cat   TEXT,
        value_json  TEXT,
        source      TEXT,
        ts          REAL NOT NULL   -- unix epoch (float = sub-second precision)
    );

    -- Pre-aggregated rollup stats per entity/metric/bucket.
    -- Populated by the pattern engine to avoid full-scan aggregations.
    -- bucket_type: 'hour' | 'day' | 'week'
    CREATE TABLE IF NOT EXISTS reading_rollups (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        metric       TEXT NOT NULL,
        bucket_type  TEXT NOT NULL,
        bucket_ts    REAL NOT NULL,     -- start of bucket (unix epoch)
        count        INTEGER NOT NULL,
        avg_num      REAL,
        min_num      REAL,
        max_num      REAL,
        p10_num      REAL,
        p90_num      REAL,
        mode_cat     TEXT,              -- most common categorical value in bucket
        UNIQUE(entity_id, metric, bucket_type, bucket_ts)
    );

    -- Calendar-style schedule events for an entity.
    -- recurrence: 'none' | 'daily' | 'weekly' | 'weekdays' | 'weekends'
    CREATE TABLE IF NOT EXISTS schedule_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        title       TEXT NOT NULL,
        start_ts    REAL NOT NULL,
        end_ts      REAL,
        recurrence  TEXT NOT NULL DEFAULT 'none',
        meta        TEXT NOT NULL DEFAULT '{}',
        created     REAL NOT NULL
    );

    -- ══════════════════════════════════════════════════════════════════════════
    -- TIER 1.5 — Episodic / session memory
    -- ══════════════════════════════════════════════════════════════════════════

    -- A conversation session for an entity.
    -- open: ended_at IS NULL  |  closed: ended_at IS NOT NULL
    CREATE TABLE IF NOT EXISTS sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        started_at  REAL NOT NULL,
        ended_at    REAL,
        summary     TEXT,
        meta        TEXT NOT NULL DEFAULT '{}'
    );

    -- Individual turns within a session (interleaved user / assistant / system).
    CREATE TABLE IF NOT EXISTS session_turns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role        TEXT NOT NULL,   -- 'user' | 'assistant' | 'system'
        content     TEXT NOT NULL,
        ts          REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_entity     ON sessions(entity_id, started_at);
    CREATE INDEX IF NOT EXISTS idx_turns_session       ON session_turns(session_id, ts);

    -- ══════════════════════════════════════════════════════════════════════════
    -- TIER 3 — Pattern tracking
    -- ══════════════════════════════════════════════════════════════════════════

    -- Prevents duplicate promotions when the same pattern is detected again.
    CREATE TABLE IF NOT EXISTS promoted_patterns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        metric      TEXT NOT NULL,
        pattern_key TEXT NOT NULL,     -- deterministic string key for this pattern
        memory_id   INTEGER REFERENCES memories(id) ON DELETE SET NULL,
        detected    REAL NOT NULL,
        UNIQUE(entity_id, metric, pattern_key)
    );

    -- Tracks the highest reading.ts processed per entity/metric,
    -- enabling _build_rollups() to skip pairs with no new data.
    CREATE TABLE IF NOT EXISTS rollup_watermarks (
        entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        metric      TEXT NOT NULL,
        last_ts     REAL NOT NULL,
        PRIMARY KEY (entity_id, metric)
    );

    -- ══════════════════════════════════════════════════════════════════════════
    -- Server configuration (key/value pairs — API token, etc.)
    -- ══════════════════════════════════════════════════════════════════════════

    CREATE TABLE IF NOT EXISTS config (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_readings_entity_metric ON readings(entity_id, metric);
    CREATE INDEX IF NOT EXISTS idx_readings_ts            ON readings(ts);
    CREATE INDEX IF NOT EXISTS idx_rollups_bucket         ON reading_rollups(entity_id, metric, bucket_type, bucket_ts);
    CREATE INDEX IF NOT EXISTS idx_schedule_entity        ON schedule_events(entity_id, start_ts);
    CREATE INDEX IF NOT EXISTS idx_memories_entity        ON memories(entity_id);
    CREATE INDEX IF NOT EXISTS idx_memories_category      ON memories(category);
    CREATE INDEX IF NOT EXISTS idx_relations_a            ON relations(entity_a);
    CREATE INDEX IF NOT EXISTS idx_relations_b            ON relations(entity_b);
    """)
    # Vector store: dimension is set by EMBED_DIM (MEMORY_EMBED_DIM env var, default 768).
    # Created separately so the f-string doesn't collide with SQL literal braces.
    # Changing embedding models with different dims requires running reembed.py.
    db.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0("
        f"embedding FLOAT[{EMBED_DIM}])"
    )
    db.commit()
    # Checkpoint WAL on startup so the WAL file doesn't grow unbounded
    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    db.close()
    _apply_migrations()
    _init_server_config()


def _apply_migrations():
    """Add columns introduced after initial schema (idempotent — safe to call repeatedly)."""
    db = get_db()
    for sql in [
        "ALTER TABLE memories ADD COLUMN last_accessed REAL",
        "ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN superseded_by INTEGER REFERENCES memories(id)",
        "ALTER TABLE relations ADD COLUMN valid_from REAL",
        "ALTER TABLE relations ADD COLUMN valid_until REAL",
    ]:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass   # column already exists
    db.commit()
    db.close()


# ── API token management ───────────────────────────────────────────────────────

def _init_server_config() -> None:
    """
    Initialise server configuration at startup.

    Token priority:
      1. MEMORY_API_TOKEN env var — static token, never written to DB
      2. Existing token in DB config table
      3. Auto-generated on first startup — stored in DB and logged prominently

    If MEMORY_API_TOKEN is not set and no DB token exists, a random 32-byte hex
    token is generated, persisted, and printed so the operator can copy it.
    """
    global _api_token
    env_token = os.environ.get("MEMORY_API_TOKEN", "").strip()
    if env_token:
        _api_token = env_token
        log.info("API auth: using token from MEMORY_API_TOKEN env var")
        return

    db = get_db()
    row = db.execute("SELECT value FROM config WHERE key='api_token'").fetchone()
    if row:
        _api_token = row[0]
        log.info("API auth: token loaded from database")
    else:
        token = secrets.token_hex(32)
        db.execute(
            "INSERT INTO config(key, value) VALUES ('api_token', ?)", (token,)
        )
        db.commit()
        _api_token = token
        log.warning("=" * 60)
        log.warning("API TOKEN (first startup — copy this now):")
        log.warning("  %s", token)
        log.warning("Manage it at: http://<host>:8900/admin/settings")
        log.warning("=" * 60)
    db.close()


def get_api_token() -> str | None:
    """Return the currently active API bearer token, or None if auth is disabled."""
    return _api_token


def get_token_source() -> str:
    """Return 'env', 'db', or 'none' — where the current token came from."""
    if not _api_token:
        return "none"
    if os.environ.get("MEMORY_API_TOKEN", "").strip():
        return "env"
    return "db"


def set_api_token(token: str) -> None:
    """
    Replace the API token in both the DB and the in-memory cache.
    Only valid when the token source is 'db' — env-var tokens cannot be overridden
    from the admin UI (they are controlled by the deployment environment).
    """
    global _api_token
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO config(key, value) VALUES ('api_token', ?)", (token,)
    )
    db.commit()
    db.close()
    _api_token = token
    log.info("API token regenerated")


# ── Embedding helpers ──────────────────────────────────────────────────────────

def _ai_headers() -> dict:
    """Build Authorization header when an API key is configured."""
    return {"Authorization": f"Bearer {AI_API_KEY}"} if AI_API_KEY else {}


async def embed(text: str) -> list[float]:
    """
    Embed text using the configured AI backend (OpenAI-compatible /v1/embeddings).

    Works with Ollama, OpenAI, LM Studio, Together AI, and any provider that
    implements the OpenAI embeddings spec.  Configure via MEMORY_AI_BASE_URL,
    MEMORY_AI_API_KEY, MEMORY_EMBED_MODEL, and MEMORY_EMBED_DIM env vars.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{AI_BASE_URL}/embeddings",
            headers=_ai_headers(),
            json={"model": EMBED_MODEL, "input": text},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


_EXTRACT_PROMPT = """\
Extract factual statements about the person named "{entity}" from the conversation text below.
Return ONLY a JSON array of objects, each with:
  "fact"       : string  — a concise, standalone factual statement in third person
  "category"   : string  — one of: preference, habit, routine, relationship, insight, general
  "confidence" : number  — 0.0–1.0 (how certain you are based on the text)

Rules:
- Only extract facts explicitly stated or strongly implied in the text.
- Do NOT infer facts not supported by the text.
- If there are no extractable facts, return an empty array [].
- Return only the JSON array, no other text.

Text:
{text}
"""


async def _call_llm(prompt: str, model: str) -> str:
    """
    Call the configured AI backend for text generation (OpenAI-compatible
    /v1/chat/completions).

    Works with Ollama, OpenAI, LM Studio, Together AI, and any provider that
    implements the OpenAI chat completions spec.  Mockable in tests via
    monkeypatch.setattr(server, '_call_llm', mock_fn).
    """
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{AI_BASE_URL}/chat/completions",
            headers=_ai_headers(),
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def vec_blob(v: list[float]) -> bytes:
    """Pack float list into binary blob for sqlite-vec."""
    return struct.pack(f"{len(v)}f", *v)


def cosine_dist(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine distance (used for small in-memory scoring)."""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return 1.0 - (dot / (na * nb + 1e-9))


def _recency_factor(updated_ts: float, weight: float = 1.0) -> float:
    """
    Exponential decay based on memory age.

    Returns 1.0 for brand-new memories, decays toward 0 for older ones.
    The half-life is ~255 days at weight=1.0 (exp(-1) ≈ 0.37 at 365 days).
    weight=0.0 always returns 1.0 (disables recency bias).
    """
    if weight == 0.0:
        return 1.0
    age_days = (time.time() - updated_ts) / 86400
    return math.exp(-weight * age_days / 365)


# ── Entity helpers ─────────────────────────────────────────────────────────────

def upsert_entity(
    db: sqlite3.Connection,
    name: str,
    entity_type: str = "person",
    meta: dict | None = None,
) -> int:
    """
    Create entity if missing, merge meta dict if present. Returns entity id.

    Entity type is set once at creation and never overwritten on update — callers
    that omit entity_type (defaulting to 'person') will not corrupt existing
    entities whose type was set to 'room', 'device', etc.  If you need to correct
    an entity's type, update it directly via SQL or the admin UI.
    """
    now = time.time()
    row = db.execute("SELECT id, meta FROM entities WHERE name=?", (name,)).fetchone()
    if row:
        merged = {**json.loads(row["meta"]), **(meta or {})}
        # Type is intentionally NOT updated — preserve the type set at creation.
        db.execute(
            "UPDATE entities SET meta=?, updated=? WHERE id=?",
            (json.dumps(merged), now, row["id"]),
        )
        return row["id"]
    cur = db.execute(
        "INSERT INTO entities(name,type,meta,created,updated) VALUES(?,?,?,?,?)",
        (name, entity_type, json.dumps(meta or {}), now, now),
    )
    return cur.lastrowid


def _fmt(row) -> str:
    """Format a readings row value as a readable string."""
    if row["value_type"] == "numeric" and row["value_num"] is not None:
        return f"{row['value_num']} {row['unit'] or ''}".strip()
    if row["value_type"] == "categorical":
        return row["value_cat"] or "?"
    if row["value_type"] == "composite":
        return row["value_json"] or "?"
    return "?"


def _age_label(ts: float) -> str:
    """
    Return a compact human-readable age for use in tool outputs.

    Gives AI abilities temporal grounding so they can reason about whether
    a memory or reading is current, recent, or stale.

    Examples: 'just now', '5m ago', '3h ago', '2d ago', '2024-03-15'
    """
    age = time.time() - ts
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        return f"{int(age / 3600)}h ago"
    if age < 7 * 86400:
        return f"{int(age / 86400)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ── Tier 1 — Semantic memory tools ────────────────────────────────────────────

async def tool_remember(
    entity_name: str,
    fact: str,
    entity_type: str = "person",
    category: str = "general",
    confidence: float = 1.0,
    source: str | None = None,
    meta: dict | None = None,
) -> str:
    db = get_db()
    now = time.time()
    eid = upsert_entity(db, entity_name, entity_type, meta)
    cur = db.execute(
        "INSERT INTO memories(entity_id,fact,category,confidence,source,created,updated) VALUES(?,?,?,?,?,?,?)",
        (eid, fact, category, confidence, source, now, now),
    )
    mid = cur.lastrowid
    vec = await embed(fact)
    db.execute("INSERT INTO memory_vectors(rowid,embedding) VALUES(?,?)", (mid, vec_blob(vec)))

    # Contradiction detection: supersede existing memories that are semantically
    # similar (cosine distance < 1 - CONTRADICTION_THRESHOLD) for the same entity.
    # The new memory wins; older ones are marked superseded_by = new mid.
    dist_threshold = 1.0 - CONTRADICTION_THRESHOLD
    similar = db.execute(
        """SELECT m.id FROM memory_vectors v
           JOIN memories m ON m.id=v.rowid
           WHERE m.entity_id=? AND m.id != ? AND m.superseded_by IS NULL
             AND vec_distance_cosine(v.embedding, ?) < ?""",
        (eid, mid, vec_blob(vec), dist_threshold),
    ).fetchall()
    for s in similar:
        db.execute("UPDATE memories SET superseded_by=? WHERE id=?", (mid, s["id"]))

    db.commit(); db.close()
    if similar:
        return (f"Remembered [{category}] for {entity_name!r}: {fact!r} "
                f"(superseded {len(similar)} similar memory/memories)")
    return f"Remembered [{category}] for {entity_name!r}: {fact!r}"


async def tool_recall(
    query: str,
    entity_name: str | None = None,
    category: str | None = None,
    top_k: int = TOP_K_DEFAULT,
    recency_weight: float = 0.0,
    min_confidence: float = 0.0,
) -> str:
    """
    Semantic search with multi-factor scoring.

    Final score = cosine_similarity × recency_factor × confidence
      recency_weight: 0.0 = pure cosine (default); 1.0 = strong recency bias
      min_confidence: exclude memories below this threshold (default 0 = show all)
    """
    db = get_db()
    # superseded_by IS NULL always applied — superseded memories are hidden by default
    wheres, params = ["m.superseded_by IS NULL"], []
    if entity_name:
        wheres.append("e.name=?");        params.append(entity_name)
    if category:
        wheres.append("m.category=?");    params.append(category)
    if min_confidence > 0.0:
        wheres.append("m.confidence>=?"); params.append(min_confidence)
    where_sql = "WHERE " + " AND ".join(wheres)
    q_vec = await embed(query)

    # Fetch a wider candidate set so re-ranking has material to work with
    rows = db.execute(
        f"""SELECT e.name, m.id, m.fact, m.category, m.confidence, m.updated,
                   vec_distance_cosine(v.embedding,?) AS dist
            FROM memory_vectors v
            JOIN memories m ON m.id=v.rowid
            JOIN entities e ON e.id=m.entity_id
            {where_sql}
            ORDER BY dist ASC LIMIT ?""",
        [vec_blob(q_vec)] + params + [top_k * 3],
    ).fetchall()

    if not rows:
        db.close()
        return "No relevant memories found."

    # Multi-factor re-ranking: sim × recency × confidence
    scored = []
    for r in rows:
        sim   = 1.0 - r["dist"]
        rec   = _recency_factor(r["updated"], recency_weight)
        score = sim * rec * r["confidence"]
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    scored = scored[:top_k]

    # Update access tracking for returned memories only
    now = time.time()
    for _, r in scored:
        db.execute(
            "UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE id=?",
            (now, r["id"]),
        )
    db.commit()
    db.close()

    lines = [f"Top {len(scored)} memories for: {query!r}\n"]
    for score, r in scored:
        age = _age_label(r["updated"])
        lines.append(f"  [{r['name']} / {r['category']}] sim={round(score,3)}  {r['fact']}  ({age})")
    return "\n".join(lines)


async def tool_get_profile(entity_name: str) -> str:
    db = get_db()
    e = db.execute("SELECT * FROM entities WHERE name=?", (entity_name,)).fetchone()
    if not e:
        db.close(); return f"No entity named {entity_name!r}."
    eid = e["id"]

    mems = db.execute(
        """SELECT category, fact, confidence, created
           FROM memories WHERE entity_id=? AND superseded_by IS NULL
           ORDER BY category, updated DESC""",
        (eid,)
    ).fetchall()

    rels = db.execute(
        """SELECT e2.name AS other, r.rel_type FROM relations r
           JOIN entities e2 ON e2.id=r.entity_b WHERE r.entity_a=? AND r.valid_until IS NULL
           UNION
           SELECT e1.name AS other, r.rel_type||'_of' FROM relations r
           JOIN entities e1 ON e1.id=r.entity_a WHERE r.entity_b=? AND r.valid_until IS NULL""",
        (eid, eid)
    ).fetchall()

    # Latest reading per metric
    latest = db.execute(
        """SELECT metric, unit, value_type, value_num, value_cat, value_json, MAX(ts) AS ts
           FROM readings WHERE entity_id=? GROUP BY metric ORDER BY metric""",
        (eid,)
    ).fetchall()

    # Upcoming schedule events
    events = db.execute(
        """SELECT title, start_ts, recurrence FROM schedule_events
           WHERE entity_id=? AND start_ts >= ? ORDER BY start_ts LIMIT 5""",
        (eid, time.time())
    ).fetchall()

    db.close()

    out = [f"=== Profile: {entity_name} ({e['type']}) ==="]
    meta = json.loads(e["meta"])
    if meta:
        out.append("Meta: " + ", ".join(f"{k}={v}" for k, v in meta.items()))

    by_cat: dict[str, list] = defaultdict(list)
    for m in mems:
        conf = f" (conf={m['confidence']})" if m["confidence"] < 1.0 else ""
        age = _age_label(m["created"])
        by_cat[m["category"]].append(f"  • {m['fact']}{conf}  [{age}]")
    for cat, facts in by_cat.items():
        out.append(f"\n{cat.upper()}:"); out.extend(facts)

    if rels:
        out.append("\nRELATIONSHIPS:")
        for r in rels: out.append(f"  • {r['rel_type']} → {r['other']}")

    if latest:
        out.append("\nLATEST READINGS:")
        for r in latest:
            out.append(f"  • {r['metric']}: {_fmt(r)}  ({_age_label(r['ts'])})")

    if events:
        out.append("\nUPCOMING SCHEDULE:")
        for ev in events:
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["start_ts"]))
            rec = f" [{ev['recurrence']}]" if ev["recurrence"] != "none" else ""
            out.append(f"  • {t}{rec} — {ev['title']}")

    return "\n".join(out)


async def tool_relate(
    entity_a: str, entity_b: str, rel_type: str, meta: dict | None = None
) -> str:
    db = get_db()
    a = upsert_entity(db, entity_a)
    b = upsert_entity(db, entity_b)
    now = time.time()
    # INSERT OR REPLACE recreates the row — instead do an upsert that preserves
    # created and resets valid_until (reactivation after tool_unrelate).
    existing = db.execute(
        "SELECT id FROM relations WHERE entity_a=? AND entity_b=? AND rel_type=?",
        (a, b, rel_type),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE relations SET meta=?, valid_from=?, valid_until=NULL WHERE id=?",
            (json.dumps(meta or {}), now, existing["id"]),
        )
    else:
        db.execute(
            """INSERT INTO relations(entity_a,entity_b,rel_type,meta,created,valid_from,valid_until)
               VALUES(?,?,?,?,?,?,NULL)""",
            (a, b, rel_type, json.dumps(meta or {}), now, now),
        )
    db.commit(); db.close()
    return f"Related: {entity_a} --[{rel_type}]--> {entity_b}"


async def tool_unrelate(entity_a: str, entity_b: str, rel_type: str) -> str:
    """
    Soft-delete a relationship by setting valid_until to now.

    The row is preserved for historical audit; active-only queries filter
    on valid_until IS NULL.
    """
    db = get_db()
    ea = db.execute("SELECT id FROM entities WHERE name=?", (entity_a,)).fetchone()
    eb = db.execute("SELECT id FROM entities WHERE name=?", (entity_b,)).fetchone()
    if not ea or not eb:
        db.close()
        return f"No entity named {entity_a!r} or {entity_b!r}."
    rel = db.execute(
        """SELECT id FROM relations
           WHERE entity_a=? AND entity_b=? AND rel_type=? AND valid_until IS NULL""",
        (ea["id"], eb["id"], rel_type),
    ).fetchone()
    if not rel:
        db.close()
        return f"No active {rel_type!r} relation from {entity_a!r} to {entity_b!r}."
    db.execute(
        "UPDATE relations SET valid_until=? WHERE id=?",
        (time.time(), rel["id"]),
    )
    db.commit(); db.close()
    return f"Ended: {entity_a} --[{rel_type}]--> {entity_b}"


async def tool_forget(entity_name: str, memory_id: int | None = None) -> str:
    db = get_db()
    if memory_id is not None:
        db.execute("DELETE FROM memory_vectors WHERE rowid=?", (memory_id,))
        db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        db.commit(); db.close()
        return f"Deleted memory #{memory_id}."
    e = db.execute("SELECT id FROM entities WHERE name=?", (entity_name,)).fetchone()
    if not e:
        db.close(); return f"No entity named {entity_name!r}."
    for m in db.execute("SELECT id FROM memories WHERE entity_id=?", (e["id"],)).fetchall():
        db.execute("DELETE FROM memory_vectors WHERE rowid=?", (m["id"],))
    db.execute("DELETE FROM entities WHERE id=?", (e["id"],))
    db.commit(); db.close()
    return f"Deleted all data for {entity_name!r}."


# ── Tier 2 — Time-series tools ─────────────────────────────────────────────────

async def tool_record(
    entity_name: str,
    metric: str,
    value: Any,
    unit: str | None = None,
    source: str | None = None,
    entity_type: str = "person",
    ts: float | None = None,
) -> str:
    """
    Ingest a single time-series reading.
    value: float/int → numeric | str → categorical | dict → composite
    """
    db = get_db()
    eid = upsert_entity(db, entity_name, entity_type)
    now = ts or time.time()

    if isinstance(value, dict):
        vtype, vnum, vcat, vjson = "composite", None, None, json.dumps(value)
    elif isinstance(value, (int, float)):
        vtype, vnum, vcat, vjson = "numeric", float(value), None, None
    else:
        vtype, vnum, vcat, vjson = "categorical", None, str(value), None

    db.execute(
        """INSERT INTO readings(entity_id,metric,unit,value_type,
                                value_num,value_cat,value_json,source,ts)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (eid, metric, unit, vtype, vnum, vcat, vjson, source, now),
    )

    # Decompose composite readings into individually queryable child rows.
    # Each scalar key becomes a separate reading: metric "{parent}.{key}".
    # Nested dicts are skipped (only one level of decomposition).
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, dict):
                continue  # no recursive decomposition
            if isinstance(val, bool):
                # bool is a subclass of int — must check before (int, float)
                cvtype, cvnum, cvcat = "categorical", None, str(val)
            elif isinstance(val, (int, float)):
                cvtype, cvnum, cvcat = "numeric", float(val), None
            else:
                cvtype, cvnum, cvcat = "categorical", None, str(val)
            db.execute(
                """INSERT INTO readings(entity_id,metric,unit,value_type,
                                        value_num,value_cat,value_json,source,ts)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (eid, f"{metric}.{key}", None, cvtype, cvnum, cvcat, None, source, now),
            )

    db.commit(); db.close()
    return f"Recorded {entity_name}/{metric}={value} @ {time.strftime('%H:%M:%S', time.localtime(now))}"


async def tool_query_stream(
    entity_name: str,
    metric: str,
    start_ts: float | None = None,
    end_ts: float | None = None,
    granularity: str = "raw",    # 'raw' | 'hour' | 'day' | 'week'
    limit: int = 100,
) -> str:
    db = get_db()
    e = db.execute("SELECT id FROM entities WHERE name=?", (entity_name,)).fetchone()
    if not e:
        db.close(); return f"No entity named {entity_name!r}."
    now = time.time()
    start = start_ts or (now - 86400)
    end   = end_ts   or now

    if granularity == "raw":
        rows = db.execute(
            """SELECT ts, value_type, value_num, value_cat, value_json, unit
               FROM readings WHERE entity_id=? AND metric=? AND ts BETWEEN ? AND ?
               ORDER BY ts DESC LIMIT ?""",
            (e["id"], metric, start, end, limit),
        ).fetchall()
        db.close()
        if not rows: return f"No {metric} readings for {entity_name} in that window."
        lines = [f"{metric} readings for {entity_name} (raw, n={len(rows)}):"]
        for r in rows:
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            lines.append(f"  {t}  {_fmt(r)}")
        return "\n".join(lines)

    # Rollup path
    rows = db.execute(
        """SELECT bucket_ts, count, avg_num, min_num, max_num, mode_cat, p10_num, p90_num
           FROM reading_rollups
           WHERE entity_id=? AND metric=? AND bucket_type=? AND bucket_ts BETWEEN ? AND ?
           ORDER BY bucket_ts DESC LIMIT ?""",
        (e["id"], metric, granularity, start, end, limit),
    ).fetchall()
    db.close()
    if not rows:
        return (f"No {granularity} rollups yet for {entity_name}/{metric}. "
                "Pattern engine runs hourly — try granularity='raw' in the meantime.")
    lines = [f"{metric} [{granularity} rollup] for {entity_name} (n={len(rows)}):"]
    for r in rows:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["bucket_ts"]))
        if r["avg_num"] is not None:
            lines.append(
                f"  {t}  avg={r['avg_num']:.2f} "
                f"[p10={r['p10_num']:.2f} p90={r['p90_num']:.2f}] "
                f"min={r['min_num']:.2f} max={r['max_num']:.2f} n={r['count']}"
            )
        else:
            lines.append(f"  {t}  mode={r['mode_cat']}  n={r['count']}")
    return "\n".join(lines)


async def tool_get_trends(
    entity_name: str,
    metric: str,
    window: str = "week",    # 'day' | 'week' | 'month'
) -> str:
    db = get_db()
    e = db.execute("SELECT id FROM entities WHERE name=?", (entity_name,)).fetchone()
    if not e:
        db.close(); return f"No entity named {entity_name!r}."

    window_secs = {"day": 86400, "week": 604800, "month": 2592000}.get(window, 604800)
    start = time.time() - window_secs

    stats = db.execute(
        """SELECT COUNT(*) AS n, AVG(value_num) AS avg,
                  MIN(value_num) AS mn, MAX(value_num) AS mx
           FROM readings WHERE entity_id=? AND metric=? AND ts>=?""",
        (e["id"], metric, start),
    ).fetchone()

    mode_row = db.execute(
        """SELECT value_cat, COUNT(*) AS c FROM readings
           WHERE entity_id=? AND metric=? AND ts>=? AND value_cat IS NOT NULL
           GROUP BY value_cat ORDER BY c DESC LIMIT 1""",
        (e["id"], metric, start),
    ).fetchone()

    # Already-promoted insights mentioning this metric
    insights = db.execute(
        """SELECT fact FROM memories
           WHERE entity_id=? AND category='insight' AND source='pattern_engine'
             AND fact LIKE ? ORDER BY updated DESC LIMIT 5""",
        (e["id"], f"%{metric}%"),
    ).fetchall()

    db.close()
    if not stats or stats["n"] == 0:
        return f"No {metric} data for {entity_name} in the last {window}."

    start_str = time.strftime("%Y-%m-%d", time.localtime(start))
    end_str   = time.strftime("%Y-%m-%d", time.localtime(time.time()))
    lines = [f"Trend: {entity_name} / {metric} / {start_str} → {end_str} ({window})"]
    lines.append(f"  Samples : {stats['n']}")
    if stats["avg"] is not None:
        lines.append(f"  Avg={stats['avg']:.2f}  Min={stats['mn']:.2f}  Max={stats['mx']:.2f}")
    if mode_row:
        lines.append(f"  Most common state : {mode_row['value_cat']} ({mode_row['c']} times)")
    if insights:
        lines.append("\nLearned patterns:")
        for i in insights: lines.append(f"  • {i['fact']}")
    return "\n".join(lines)


async def tool_schedule(
    entity_name: str,
    title: str,
    start_ts: float,
    end_ts: float | None = None,
    recurrence: str = "none",
    meta: dict | None = None,
    entity_type: str = "person",
) -> str:
    db = get_db()
    eid = upsert_entity(db, entity_name, entity_type)
    db.execute(
        """INSERT INTO schedule_events(entity_id,title,start_ts,end_ts,recurrence,meta,created)
           VALUES(?,?,?,?,?,?,?)""",
        (eid, title, start_ts, end_ts, recurrence, json.dumps(meta or {}), time.time()),
    )
    db.commit(); db.close()
    t = time.strftime("%Y-%m-%d %H:%M", time.localtime(start_ts))
    return f"Scheduled for {entity_name!r}: {title!r} @ {t} [{recurrence}]"


async def tool_cross_query(query: str, top_k: int = TOP_K_DEFAULT) -> str:
    """
    Unified semantic search across BOTH Tier 1 memories AND
    recent Tier 2 readings (converted to text on the fly).
    """
    db = get_db()
    q_vec = await embed(query)

    # Tier 1 hits via sqlite-vec
    sem_rows = db.execute(
        """SELECT e.name, m.id, m.fact, m.category, m.updated,
                  vec_distance_cosine(v.embedding,?) AS dist
           FROM memory_vectors v
           JOIN memories m ON m.id=v.rowid
           JOIN entities e ON e.id=m.entity_id
           WHERE m.superseded_by IS NULL
           ORDER BY dist ASC LIMIT ?""",
        [vec_blob(q_vec), top_k],
    ).fetchall()

    # Update access tracking for returned memories
    if sem_rows:
        now = time.time()
        for r in sem_rows:
            db.execute(
                "UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE id=?",
                (now, r["id"]),
            )
        db.commit()

    # Latest Tier 2 readings in the last hour
    recents = db.execute(
        """SELECT e.name, r.metric, r.value_type, r.value_num,
                  r.value_cat, r.value_json, r.unit, MAX(r.ts) AS ts
           FROM readings r JOIN entities e ON e.id=r.entity_id
           WHERE r.ts >= ? GROUP BY r.entity_id, r.metric""",
        (time.time() - 3600,),
    ).fetchall()

    db.close()

    lines = [f"Cross-query: {query!r}\n"]
    if sem_rows:
        lines.append("── Semantic memories ──")
        for r in sem_rows:
            age = _age_label(r["updated"])
            lines.append(f"  [{r['name']} / {r['category']}] sim={round(1-r['dist'],3)}  {r['fact']}  ({age})")

    if recents:
        # Score each recent reading against the query
        scored = []
        for r in recents:
            text = f"{r['name']} {r['metric']} is {_fmt(r)}"
            rv = await embed(text)
            scored.append((cosine_dist(q_vec, rv), r["name"], r["metric"], _fmt(r)))
        scored.sort()
        lines.append("\n── Live readings (last 1h) ──")
        for dist, name, metric, val in scored[:top_k]:
            lines.append(f"  [{name} / {metric}] sim={round(1-dist,3)}  current: {val}")

    return "\n".join(lines)


async def tool_get_context(
    entity_name: str,
    context_query: str,
    max_facts: int = 5,
) -> str:
    """
    Relevance-filtered context snapshot for an entity — preferred over get_profile
    for ability use because it stays within a predictable token budget.

    Returns the max_facts most query-relevant memories, plus relationships,
    latest readings, and upcoming schedule.  Access tracking is updated for
    all returned memories so popular facts rise in future rankings.
    """
    db = get_db()
    e = db.execute("SELECT * FROM entities WHERE name=?", (entity_name,)).fetchone()
    if not e:
        db.close()
        return f"No entity named {entity_name!r}."
    eid = e["id"]

    # Semantically relevant memories
    q_vec = await embed(context_query)
    mem_rows = db.execute(
        """SELECT m.id, m.fact, m.category, m.confidence, m.updated,
                  vec_distance_cosine(v.embedding,?) AS dist
           FROM memory_vectors v
           JOIN memories m ON m.id=v.rowid
           WHERE m.entity_id=? AND m.superseded_by IS NULL
           ORDER BY dist ASC LIMIT ?""",
        [vec_blob(q_vec), eid, max_facts],
    ).fetchall()

    # Update access tracking
    if mem_rows:
        now = time.time()
        for r in mem_rows:
            db.execute(
                "UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE id=?",
                (now, r["id"]),
            )
        db.commit()

    # Relationships
    rels = db.execute(
        """SELECT e2.name AS other, r.rel_type FROM relations r
           JOIN entities e2 ON e2.id=r.entity_b WHERE r.entity_a=? AND r.valid_until IS NULL
           UNION
           SELECT e1.name AS other, r.rel_type||'_of' FROM relations r
           JOIN entities e1 ON e1.id=r.entity_a WHERE r.entity_b=? AND r.valid_until IS NULL""",
        (eid, eid),
    ).fetchall()

    # Latest reading per metric
    latest = db.execute(
        """SELECT metric, unit, value_type, value_num, value_cat, value_json, MAX(ts) AS ts
           FROM readings WHERE entity_id=? GROUP BY metric ORDER BY metric""",
        (eid,),
    ).fetchall()

    # Upcoming schedule
    events = db.execute(
        """SELECT title, start_ts, recurrence FROM schedule_events
           WHERE entity_id=? AND start_ts >= ? ORDER BY start_ts LIMIT 5""",
        (eid, time.time()),
    ).fetchall()

    db.close()

    out = [f"Context: {entity_name} ({e['type']})"]

    if mem_rows:
        out.append(f"\nMemories (top {len(mem_rows)} for {context_query!r}):")
        for r in mem_rows:
            conf = f" (conf={r['confidence']:.2f})" if r["confidence"] < 1.0 else ""
            age = _age_label(r["updated"])
            out.append(f"  • [{r['category']}] {r['fact']}{conf}  ({age})")

    if rels:
        out.append("\nRelationships:")
        for r in rels:
            out.append(f"  • {r['rel_type']} → {r['other']}")

    if latest:
        out.append("\nLatest readings:")
        for r in latest:
            out.append(f"  • {r['metric']}: {_fmt(r)}  ({_age_label(r['ts'])})")

    if events:
        out.append("\nUpcoming schedule:")
        for ev in events:
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["start_ts"]))
            rec = f" [{ev['recurrence']}]" if ev["recurrence"] != "none" else ""
            out.append(f"  • {t}{rec} — {ev['title']}")

    return "\n".join(out)


# ── Auto-extraction ───────────────────────────────────────────────────────────

async def tool_extract_and_remember(
    entity_name: str,
    text: str,
    entity_type: str = "person",
    model: str | None = None,
) -> str:
    """
    Extract facts from conversation text using an Ollama LLM and store them
    as memories for entity_name.

    Uses LLM_MODEL (default llama3.2) — override with the model parameter.
    Returns a summary of how many facts were stored.
    """
    llm_model = model or LLM_MODEL
    prompt = _EXTRACT_PROMPT.format(entity=entity_name, text=text)

    try:
        raw = await _call_llm(prompt, llm_model)
        # Strip markdown code fences if the model wrapped the JSON
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        facts = json.loads(raw)
    except Exception as e:
        return f"Extraction failed: {e}"

    if not isinstance(facts, list):
        return "Extraction returned unexpected format (expected JSON array)."
    if not facts:
        return f"No extractable facts found in text for {entity_name!r}."

    stored = 0
    for item in facts:
        if not isinstance(item, dict) or "fact" not in item:
            continue
        fact       = str(item["fact"])
        category   = str(item.get("category", "general"))
        confidence = float(item.get("confidence", 0.75))
        await tool_remember(
            entity_name=entity_name,
            fact=fact,
            entity_type=entity_type,
            category=category,
            confidence=confidence,
            source="extract",
        )
        stored += 1

    return f"Extracted and stored {stored} fact(s) for {entity_name!r}."


# ── Episodic / session memory tools ───────────────────────────────────────────

async def tool_open_session(entity_name: str, entity_type: str = "person") -> int:
    """
    Open a new conversation session for an entity.
    Returns the session_id (integer) — pass it to tool_log_turn and tool_close_session.
    """
    db = get_db()
    eid = upsert_entity(db, entity_name, entity_type)
    cur = db.execute(
        "INSERT INTO sessions(entity_id,started_at,ended_at,summary,meta) VALUES(?,?,NULL,NULL,'{}')",
        (eid, time.time()),
    )
    sid = cur.lastrowid
    db.commit(); db.close()
    return sid


async def tool_log_turn(session_id: int, role: str, content: str) -> str:
    """
    Append a turn to an open session.
    role: 'user' | 'assistant' | 'system'
    """
    db = get_db()
    s = db.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not s:
        db.close()
        return f"No session with id={session_id}."
    db.execute(
        "INSERT INTO session_turns(session_id,role,content,ts) VALUES(?,?,?,?)",
        (session_id, role, content, time.time()),
    )
    db.commit(); db.close()
    return f"Logged [{role}] turn to session {session_id}."


async def tool_close_session(session_id: int, summary: str | None = None) -> str:
    """
    Close a session, optionally storing a summary.
    Sets ended_at to now; session is then read-only.
    """
    db = get_db()
    s = db.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not s:
        db.close()
        return f"No session with id={session_id}."
    db.execute(
        "UPDATE sessions SET ended_at=?, summary=? WHERE id=?",
        (time.time(), summary, session_id),
    )
    db.commit(); db.close()
    return f"Closed session {session_id}." + (f" Summary: {summary!r}" if summary else "")


async def tool_get_session(session_id: int) -> str:
    """
    Retrieve a session transcript with all turns, entity name, and summary.
    """
    db = get_db()
    row = db.execute(
        """SELECT s.*, e.name AS entity_name FROM sessions s
           JOIN entities e ON e.id=s.entity_id
           WHERE s.id=?""",
        (session_id,),
    ).fetchone()
    if not row:
        db.close()
        return f"No session with id={session_id}."
    turns = db.execute(
        "SELECT role, content, ts FROM session_turns WHERE session_id=? ORDER BY ts",
        (session_id,),
    ).fetchall()
    db.close()

    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
    ended   = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ended_at"])) if row["ended_at"] else "open"
    out = [f"Session {session_id} — {row['entity_name']} | {started} → {ended}"]
    if row["summary"]:
        out.append(f"Summary: {row['summary']}")
    if turns:
        out.append("")
        for t in turns:
            ts = time.strftime("%H:%M:%S", time.localtime(t["ts"]))
            out.append(f"  [{ts}] {t['role']}: {t['content']}")
    else:
        out.append("  (no turns recorded)")
    return "\n".join(out)


# ── Tier 3 — Pattern engine ────────────────────────────────────────────────────

def _percentile(data: list[float], p: int) -> float:
    """Percentile without numpy."""
    if not data: return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


async def _build_rollups():
    """
    Aggregate raw readings into hourly/daily/weekly rollup buckets.

    Incremental: uses rollup_watermarks to skip entity/metric pairs with no
    new data since the last run.  Only buckets that overlap with new readings
    are recomputed (INSERT OR REPLACE is idempotent for buckets that didn't
    change, but we avoid the full scan cost for cold pairs).
    """
    db = get_db()
    pairs = db.execute("SELECT DISTINCT entity_id, metric FROM readings").fetchall()

    for p in pairs:
        eid, metric = p["entity_id"], p["metric"]

        # Check watermark: skip if no new readings since last build
        wm = db.execute(
            "SELECT last_ts FROM rollup_watermarks WHERE entity_id=? AND metric=?",
            (eid, metric),
        ).fetchone()
        watermark_ts = wm["last_ts"] if wm else 0.0

        latest_ts = db.execute(
            "SELECT MAX(ts) FROM readings WHERE entity_id=? AND metric=?",
            (eid, metric),
        ).fetchone()[0]
        if latest_ts is None or latest_ts <= watermark_ts:
            continue   # nothing new — skip

        # Find which bucket boundaries contain new data
        new_rows = db.execute(
            "SELECT ts FROM readings WHERE entity_id=? AND metric=? AND ts > ?",
            (eid, metric, watermark_ts),
        ).fetchall()
        new_tss = {r["ts"] for r in new_rows}

        # Recompute only the affected buckets (those containing at least one new reading)
        all_rows = db.execute(
            "SELECT ts, value_num, value_cat FROM readings WHERE entity_id=? AND metric=?",
            (eid, metric),
        ).fetchall()

        for bucket_type, bucket_secs in [("hour", 3600), ("day", 86400), ("week", 604800)]:
            # Identify dirty bucket timestamps
            dirty_buckets = {(t // bucket_secs) * bucket_secs for t in new_tss}

            # Group all rows by bucket
            buckets: dict[float, list] = defaultdict(list)
            for r in all_rows:
                bts = (r["ts"] // bucket_secs) * bucket_secs
                if bts in dirty_buckets:
                    buckets[bts].append(r)

            for bts, brows in buckets.items():
                nums = [r["value_num"] for r in brows if r["value_num"] is not None]
                cats = [r["value_cat"] for r in brows if r["value_cat"] is not None]
                db.execute(
                    """INSERT OR REPLACE INTO reading_rollups
                       (entity_id,metric,bucket_type,bucket_ts,count,
                        avg_num,min_num,max_num,p10_num,p90_num,mode_cat)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, metric, bucket_type, bts, len(brows),
                     sum(nums)/len(nums) if nums else None,
                     min(nums) if nums else None,
                     max(nums) if nums else None,
                     _percentile(nums, 10) if nums else None,
                     _percentile(nums, 90) if nums else None,
                     max(set(cats), key=cats.count) if cats else None),
                )

        # Advance watermark to the latest reading processed
        db.execute(
            """INSERT INTO rollup_watermarks(entity_id, metric, last_ts)
               VALUES(?,?,?)
               ON CONFLICT(entity_id, metric) DO UPDATE SET last_ts=excluded.last_ts""",
            (eid, metric, latest_ts),
        )

    db.commit(); db.close()


def _detect_patterns(entity_name: str, metric: str, rollups: list) -> list[tuple]:
    """
    Heuristics over daily rollups → list of (fact_text, pattern_key, confidence).
    Add new detectors here freely — they auto-integrate with promotion logic.
    """
    results = []
    nums  = [r["avg_num"]  for r in rollups if r["avg_num"]  is not None]
    modes = [r["mode_cat"] for r in rollups if r["mode_cat"] is not None]

    # Stable numeric average (CV < 10%)
    if len(nums) >= 3:
        avg = sum(nums) / len(nums)
        std = (sum((x - avg)**2 for x in nums) / len(nums)) ** 0.5
        cv  = std / (abs(avg) + 1e-9)
        if cv < 0.10:
            fact = (f"{entity_name}'s {metric} is consistently around {avg:.1f} "
                    f"(std={std:.1f}, stable over {len(nums)} days)")
            results.append((fact, f"stable_avg_{avg:.0f}", 0.85))

    # Rising / falling trend
    if len(nums) >= 5:
        n2 = len(nums) // 2
        first  = sum(nums[:n2]) / n2
        second = sum(nums[n2:]) / (len(nums) - n2)
        delta_pct = (second - first) / (abs(first) + 1e-9) * 100
        if abs(delta_pct) > 15:
            direction = "rising" if delta_pct > 0 else "falling"
            fact = (f"{entity_name}'s {metric} has been {direction} "
                    f"({first:.1f} → {second:.1f}, {delta_pct:+.0f}% over {len(nums)} days)")
            results.append((fact, f"{direction}_{abs(delta_pct):.0f}", 0.80))

    # Dominant categorical state (>=70% of days)
    if len(modes) >= 3:
        dominant = max(set(modes), key=modes.count)
        pct = modes.count(dominant) / len(modes) * 100
        if pct >= 70:
            fact = (f"{entity_name}'s {metric} is predominantly '{dominant}' "
                    f"({pct:.0f}% of {len(modes)} days)")
            results.append((fact, f"dominant_{dominant}", min(0.95, 0.75 + pct / 400)))

    return results


# ── New pattern detectors ──────────────────────────────────────────────────────

def _detect_tod_patterns(entity_name: str, metric: str, readings: list) -> list[tuple]:
    """
    Detect time-of-day patterns in categorical readings.

    Groups readings by hour-of-day (0–23).  For each hour that has at least 5
    readings, if one category accounts for ≥75% of them, emit a pattern like:
      "Brian's presence is 'home' at 19:00 (87% of 8 readings)"

    Only categorical readings (value_cat is not None) are considered.
    """
    from collections import Counter

    hour_cats: dict[int, list[str]] = defaultdict(list)
    for r in readings:
        if r["value_cat"] is None:
            continue
        hour = int(r["ts"] % 86400 // 3600)
        hour_cats[hour].append(r["value_cat"])

    results = []
    for hour, cats in hour_cats.items():
        if len(cats) < 5:
            continue
        dominant = max(set(cats), key=cats.count)
        pct = cats.count(dominant) / len(cats) * 100
        if pct >= 75.0:
            fact = (
                f"{entity_name}'s {metric} is '{dominant}' at "
                f"{hour:02d}:00 ({pct:.0f}% of {len(cats)} readings)"
            )
            results.append((fact, f"tod_{hour:02d}_{dominant}", min(0.90, 0.70 + pct / 500)))
    return results


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient for two equal-length lists. Returns 0.0 for n<2."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy  = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / (dx * dy + 1e-9)


def _detect_correlations(entity_name: str, metrics_rollups: dict) -> list[tuple]:
    """
    Detect pairwise correlations between numeric metrics for the same entity.

    metrics_rollups: {metric_name: [rollup_row_dicts]}

    For each pair of metrics that share ≥5 day-buckets with numeric averages,
    compute Pearson r.  |r| ≥ 0.7 is reported as an insight.
    """
    results = []
    names = list(metrics_rollups.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ma, mb = names[i], names[j]

            # Build {bucket_ts: avg_num} for each metric, skip None values
            ts_a = {
                r["bucket_ts"]: r["avg_num"]
                for r in metrics_rollups[ma]
                if r["avg_num"] is not None
            }
            ts_b = {
                r["bucket_ts"]: r["avg_num"]
                for r in metrics_rollups[mb]
                if r["avg_num"] is not None
            }

            common = sorted(set(ts_a) & set(ts_b))
            if len(common) < 5:
                continue

            xs = [ts_a[t] for t in common]
            ys = [ts_b[t] for t in common]
            r_val = _pearson(xs, ys)

            if abs(r_val) >= 0.7:
                direction = "positively" if r_val > 0 else "negatively"
                sign      = "+" if r_val > 0 else "-"
                fact = (
                    f"{entity_name}'s {ma} and {mb} are {direction} correlated "
                    f"(r={r_val:.2f}, n={len(common)} days)"
                )
                results.append((fact, f"corr_{ma}_{mb}_{sign}", 0.75))
    return results


def _detect_anomalies(
    entity_name: str,
    metric: str,
    recent_readings: list,
    baseline_rollups: list,
) -> list[tuple]:
    """
    Flag numeric readings that are ≥3 standard deviations from the rolling baseline.

    baseline_rollups: daily rollup dicts used to compute mean/std.
    recent_readings:  raw reading dicts with keys 'id', 'ts', 'value_num'.

    Returns nothing if baseline has <5 points or zero variance.
    Each anomaly gets a dedup key of "anomaly_{reading_id}" so the same reading
    is never promoted twice even across multiple engine runs.
    """
    nums = [r["avg_num"] for r in baseline_rollups if r["avg_num"] is not None]
    if len(nums) < 5:
        return []

    mean = sum(nums) / len(nums)
    std  = (sum((x - mean) ** 2 for x in nums) / len(nums)) ** 0.5
    if std < 0.01:
        return []   # zero variance — z-score undefined

    results = []
    for r in recent_readings:
        if r["value_num"] is None:
            continue
        z = abs(r["value_num"] - mean) / std
        if z >= 3.0:
            ts_str    = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            direction = "above" if r["value_num"] > mean else "below"
            fact = (
                f"Anomaly: {entity_name}'s {metric} was {r['value_num']:.1f} "
                f"at {ts_str} ({z:.1f} std devs {direction} normal {mean:.1f})"
            )
            confidence = min(0.95, 0.70 + (z - 3.0) * 0.05)
            results.append((fact, f"anomaly_{r['id']}", confidence))
    return results


# ── Promotion helper ───────────────────────────────────────────────────────────

async def _maybe_promote(
    db: sqlite3.Connection,
    eid: int,
    metric: str,
    fact: str,
    pkey: str,
    confidence: float,
) -> None:
    """
    Promote a detected pattern to a Tier 1 insight memory — unless it has been
    promoted before (dedup via promoted_patterns table).

    Does NOT commit; the caller is responsible for db.commit().
    """
    exists = db.execute(
        "SELECT id FROM promoted_patterns WHERE entity_id=? AND metric=? AND pattern_key=?",
        (eid, metric, pkey),
    ).fetchone()
    if exists:
        return

    now = time.time()
    vec = await embed(fact)
    cur = db.execute(
        """INSERT INTO memories(entity_id,fact,category,confidence,source,created,updated)
           VALUES(?,?,'insight',?,'pattern_engine',?,?)""",
        (eid, fact, confidence, now, now),
    )
    mid = cur.lastrowid
    db.execute(
        "INSERT INTO memory_vectors(rowid,embedding) VALUES(?,?)",
        (mid, vec_blob(vec)),
    )
    db.execute(
        """INSERT INTO promoted_patterns
           (entity_id,metric,pattern_key,memory_id,detected) VALUES(?,?,?,?,?)""",
        (eid, metric, pkey, mid, now),
    )
    log.info(f"Pattern promoted: {eid}/{metric}/{pkey}")


async def _promote_patterns():
    """
    Run all pattern detectors and promote new findings as Tier 1 insight memories.

    Per-metric detectors (called for every entity/metric with ≥3 day-rollups):
      _detect_patterns()     — stable avg, rising/falling trend, dominant categorical
      _detect_tod_patterns() — time-of-day categorical patterns
      _detect_anomalies()    — z-score anomalies vs rolling baseline

    Cross-metric detectors (called once per entity with ≥2 numeric metrics):
      _detect_correlations() — Pearson r between pairs of numeric metrics
    """
    db = get_db()
    entities = db.execute("SELECT id, name FROM entities").fetchall()
    cutoff = time.time() - 14 * 86400

    for entity in entities:
        eid, ename = entity["id"], entity["name"]
        metrics = db.execute(
            "SELECT DISTINCT metric FROM readings WHERE entity_id=?", (eid,)
        ).fetchall()

        # Collect rollups per metric for the cross-metric correlation detector
        numeric_rollups: dict[str, list] = {}

        for m in metrics:
            metric = m["metric"]

            # ── Daily rollups for this entity/metric ──────────────────────────
            rollups = db.execute(
                """SELECT avg_num, min_num, max_num, mode_cat, p10_num, p90_num, count,
                          bucket_ts
                   FROM reading_rollups
                   WHERE entity_id=? AND metric=? AND bucket_type='day' AND bucket_ts>=?
                   ORDER BY bucket_ts""",
                (eid, metric, cutoff),
            ).fetchall()

            if len(rollups) >= 3:
                # Existing detectors: stable avg, rising/falling, dominant categorical
                for fact, pkey, conf in _detect_patterns(ename, metric, rollups):
                    await _maybe_promote(db, eid, metric, fact, pkey, conf)

                # Anomaly detector: readings since the last engine run vs baseline
                anomaly_since = time.time() - PATTERN_INTERVAL
                recent = db.execute(
                    """SELECT id, ts, value_num FROM readings
                       WHERE entity_id=? AND metric=? AND ts>=? AND value_num IS NOT NULL""",
                    (eid, metric, anomaly_since),
                ).fetchall()
                for fact, pkey, conf in _detect_anomalies(ename, metric, recent, rollups):
                    await _maybe_promote(db, eid, metric, fact, pkey, conf)

                # Accumulate numeric metrics for correlation pass
                if any(r["avg_num"] is not None for r in rollups):
                    numeric_rollups[metric] = rollups

            # ── TOD detector needs raw readings (not rollups) ─────────────────
            tod_readings = db.execute(
                """SELECT ts, value_cat FROM readings
                   WHERE entity_id=? AND metric=? AND ts>=? AND value_cat IS NOT NULL""",
                (eid, metric, cutoff),
            ).fetchall()
            if len(tod_readings) >= 5:
                for fact, pkey, conf in _detect_tod_patterns(ename, metric, tod_readings):
                    await _maybe_promote(db, eid, metric, fact, pkey, conf)

        # ── Cross-metric correlation ──────────────────────────────────────────
        if len(numeric_rollups) >= 2:
            for fact, pkey, conf in _detect_correlations(ename, numeric_rollups):
                await _maybe_promote(db, eid, "correlation", fact, pkey, conf)

        db.commit()

    db.close()


async def _prune_readings() -> int:
    """
    Delete raw readings older than RETENTION_DAYS.

    Rollups and memories are never touched — only the readings table is pruned.
    Returns the number of rows deleted.
    """
    db = get_db()
    cutoff = time.time() - RETENTION_DAYS * 86400
    cur = db.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
    count = cur.rowcount
    db.commit()
    db.close()
    log.info(f"Retention: pruned {count} readings older than {RETENTION_DAYS} days")
    return count


async def tool_prune() -> str:
    """MCP-callable wrapper around _prune_readings."""
    count = await _prune_readings()
    return f"Pruned {count} readings older than {RETENTION_DAYS} days."


# ── Memory consolidation ───────────────────────────────────────────────────────

CONSOLIDATION_THRESHOLD = 0.92  # cosine sim above which two memories are "near-duplicate"


async def _consolidate_memories() -> int:
    """
    Pattern-engine pass: cluster memories per entity by vector similarity and
    supersede near-duplicates (cosine sim >= CONSOLIDATION_THRESHOLD).

    Within each cluster the highest-confidence memory wins.  Tie-break: newest
    memory (largest id) wins.  Only non-superseded memories are considered.

    Returns the number of memories newly superseded.
    """
    db = get_db()
    entities = db.execute("SELECT id FROM entities").fetchall()
    total_superseded = 0
    dist_threshold = 1.0 - CONSOLIDATION_THRESHOLD

    for entity in entities:
        eid = entity["id"]
        # Load all active memories + their vectors for this entity
        mems = db.execute(
            """SELECT m.id, m.confidence FROM memories m
               WHERE m.entity_id=? AND m.superseded_by IS NULL""",
            (eid,),
        ).fetchall()
        if len(mems) < 2:
            continue

        # Build list of (id, confidence, vector_blob) tuples
        candidates = []
        for m in mems:
            vec_row = db.execute(
                "SELECT embedding FROM memory_vectors WHERE rowid=?", (m["id"],)
            ).fetchone()
            if vec_row:
                candidates.append((m["id"], m["confidence"], vec_row["embedding"]))

        # Find clusters of similar memories using a simple greedy approach:
        # for each pair, if similar, mark the weaker as superseded by the stronger.
        superseded_ids: set[int] = set()
        for i in range(len(candidates)):
            if candidates[i][0] in superseded_ids:
                continue
            for j in range(i + 1, len(candidates)):
                if candidates[j][0] in superseded_ids:
                    continue
                id_a, conf_a, vec_a = candidates[i]
                id_b, conf_b, vec_b = candidates[j]
                # Compute cosine distance using sqlite-vec
                dist = db.execute(
                    "SELECT vec_distance_cosine(?, ?)", (vec_a, vec_b)
                ).fetchone()[0]
                if dist < dist_threshold:
                    # Merge: loser is the lower-confidence one; tie → lower id loses
                    if conf_a > conf_b or (conf_a == conf_b and id_a > id_b):
                        loser, winner = id_b, id_a
                    else:
                        loser, winner = id_a, id_b
                    db.execute(
                        "UPDATE memories SET superseded_by=? WHERE id=?",
                        (winner, loser),
                    )
                    superseded_ids.add(loser)
                    total_superseded += 1

    db.commit()
    db.close()
    return total_superseded


async def pattern_engine_loop():
    """Background task: build rollups, detect patterns, and prune old readings."""
    await asyncio.sleep(60)   # let server settle before first run
    while True:
        try:
            log.info("Pattern engine: building rollups…")
            await _build_rollups()
            log.info("Pattern engine: detecting patterns…")
            await _promote_patterns()
            log.info("Pattern engine: consolidating near-duplicate memories…")
            n = await _consolidate_memories()
            if n:
                log.info(f"Pattern engine: consolidated {n} near-duplicate memories.")
            log.info("Pattern engine: pruning old readings…")
            await _prune_readings()
            db = get_db()
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            db.close()
            log.info("Pattern engine: done.")
        except Exception as e:
            log.error(f"Pattern engine error: {e}")
        await asyncio.sleep(PATTERN_INTERVAL)


# ── MCP Server wiring ──────────────────────────────────────────────────────────

server = Server("memory-mcp")

TOOLS = [
    # Tier 1
    Tool(name="remember",
         description="Store a semantic fact/memory about any entity.",
         inputSchema={"type":"object","required":["entity_name","fact"],"properties":{
             "entity_name":{"type":"string"},
             "fact":{"type":"string"},
             "entity_type":{"type":"string","default":"person"},
             "category":{"type":"string","enum":["preference","habit","routine","relationship","insight","general"],"default":"general"},
             "confidence":{"type":"number","default":1.0},
             "source":{"type":"string"},
             "meta":{"type":"object"}}}),
    Tool(name="recall",
         description="Semantic search across all stored memories.",
         inputSchema={"type":"object","required":["query"],"properties":{
             "query":{"type":"string"},
             "entity_name":{"type":"string"},
             "category":{"type":"string"},
             "top_k":{"type":"integer","default":5},
             "recency_weight":{"type":"number","default":0.0,
                 "description":"0=pure cosine, 1=strong recency bias"},
             "min_confidence":{"type":"number","default":0.0,
                 "description":"Exclude memories below this confidence threshold"}}}),
    Tool(name="get_context",
         description=(
             "Relevance-filtered context snapshot for an entity — preferred over "
             "get_profile for ability use (stays within a predictable token budget)."
         ),
         inputSchema={"type":"object","required":["entity_name","context_query"],"properties":{
             "entity_name":{"type":"string"},
             "context_query":{"type":"string",
                 "description":"Current topic — used to select the most relevant memories"},
             "max_facts":{"type":"integer","default":5}}}),
    Tool(name="get_profile",
         description="Full profile: memories + relationships + latest readings + upcoming schedule.",
         inputSchema={"type":"object","required":["entity_name"],"properties":{"entity_name":{"type":"string"}}}),
    Tool(name="relate",
         description="Create a directional relationship between two entities.",
         inputSchema={"type":"object","required":["entity_a","entity_b","rel_type"],"properties":{
             "entity_a":{"type":"string"},"entity_b":{"type":"string"},
             "rel_type":{"type":"string"},"meta":{"type":"object"}}}),
    Tool(name="unrelate",
         description="Soft-delete a relationship (sets valid_until). Row preserved for history.",
         inputSchema={"type":"object","required":["entity_a","entity_b","rel_type"],"properties":{
             "entity_a":{"type":"string"},"entity_b":{"type":"string"},
             "rel_type":{"type":"string"}}}),
    Tool(name="forget",
         description="Delete a specific memory or entire entity profile.",
         inputSchema={"type":"object","required":["entity_name"],"properties":{
             "entity_name":{"type":"string"},"memory_id":{"type":"integer"}}}),
    # Tier 2
    Tool(name="record",
         description="Ingest a time-series reading (temperature, mood, presence, etc.).",
         inputSchema={"type":"object","required":["entity_name","metric","value"],"properties":{
             "entity_name":{"type":"string"},
             "metric":{"type":"string","description":"e.g. 'temperature','mood','presence'"},
             "value":{"description":"float (numeric) | str (categorical) | dict (composite)"},
             "unit":{"type":"string"},
             "source":{"type":"string"},
             "entity_type":{"type":"string","default":"person"},
             "ts":{"type":"number","description":"Unix epoch override; defaults to now"}}}),
    Tool(name="query_stream",
         description="Query time-series data with flexible granularity (raw/hour/day/week).",
         inputSchema={"type":"object","required":["entity_name","metric"],"properties":{
             "entity_name":{"type":"string"},
             "metric":{"type":"string"},
             "start_ts":{"type":"number"},
             "end_ts":{"type":"number"},
             "granularity":{"type":"string","enum":["raw","hour","day","week"],"default":"raw"},
             "limit":{"type":"integer","default":100}}}),
    Tool(name="get_trends",
         description="Natural-language trend summary for an entity/metric.",
         inputSchema={"type":"object","required":["entity_name","metric"],"properties":{
             "entity_name":{"type":"string"},
             "metric":{"type":"string"},
             "window":{"type":"string","enum":["day","week","month"],"default":"week"}}}),
    Tool(name="schedule",
         description="Add a recurring or one-off schedule event for an entity.",
         inputSchema={"type":"object","required":["entity_name","title","start_ts"],"properties":{
             "entity_name":{"type":"string"},
             "title":{"type":"string"},
             "start_ts":{"type":"number","description":"Unix epoch"},
             "end_ts":{"type":"number"},
             "recurrence":{"type":"string","enum":["none","daily","weekly","weekdays","weekends"],"default":"none"},
             "meta":{"type":"object"},
             "entity_type":{"type":"string","default":"person"}}}),
    # Cross-tier
    Tool(name="cross_query",
         description="Unified semantic search across memories AND live sensor readings.",
         inputSchema={"type":"object","required":["query"],"properties":{
             "query":{"type":"string"},"top_k":{"type":"integer","default":5}}}),
    # Auto-extraction
    Tool(name="extract_and_remember",
         description=(
             "Extract facts from conversation text using an LLM and store them "
             f"as memories. Uses {LLM_MODEL} by default (override with model param)."
         ),
         inputSchema={"type":"object","required":["entity_name","text"],"properties":{
             "entity_name":{"type":"string"},
             "text":{"type":"string","description":"Raw conversation text to mine for facts"},
             "entity_type":{"type":"string","default":"person"},
             "model":{"type":"string","description":"Ollama model override"}}}),
    # Episodic memory
    Tool(name="open_session",
         description="Open a new conversation session for an entity. Returns integer session_id.",
         inputSchema={"type":"object","required":["entity_name"],"properties":{
             "entity_name":{"type":"string"},
             "entity_type":{"type":"string","default":"person"}}}),
    Tool(name="log_turn",
         description="Append a conversation turn to an open session.",
         inputSchema={"type":"object","required":["session_id","role","content"],"properties":{
             "session_id":{"type":"integer"},
             "role":{"type":"string","enum":["user","assistant","system"]},
             "content":{"type":"string"}}}),
    Tool(name="close_session",
         description="Close a session, optionally with a summary. Sets ended_at.",
         inputSchema={"type":"object","required":["session_id"],"properties":{
             "session_id":{"type":"integer"},
             "summary":{"type":"string"}}}),
    Tool(name="get_session",
         description="Retrieve a full session transcript with all turns and summary.",
         inputSchema={"type":"object","required":["session_id"],"properties":{
             "session_id":{"type":"integer"}}}),
    # Maintenance
    Tool(name="prune",
         description=(
             f"Delete raw readings older than {RETENTION_DAYS} days. "
             "Rollups and memories are preserved. Returns count of deleted rows."
         ),
         inputSchema={"type":"object","properties":{}}),
]


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    dispatch = {
        "remember":     tool_remember,
        "recall":       tool_recall,
        "get_context":  tool_get_context,
        "get_profile":  tool_get_profile,
        "relate":       tool_relate,
        "unrelate":     tool_unrelate,
        "forget":       tool_forget,
        "record":       tool_record,
        "query_stream": tool_query_stream,
        "get_trends":   tool_get_trends,
        "schedule":     tool_schedule,
        "cross_query":  tool_cross_query,
        "extract_and_remember": tool_extract_and_remember,
        "open_session":  tool_open_session,
        "log_turn":      tool_log_turn,
        "close_session": tool_close_session,
        "get_session":   tool_get_session,
        "prune":         tool_prune,
    }
    fn = dispatch.get(name)
    if not fn:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        result = await fn(**arguments)
    except Exception as ex:
        result = f"Error in {name}: {ex}"
    return [TextContent(type="text", text=result)]


async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    asyncio.create_task(pattern_engine_loop())
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
