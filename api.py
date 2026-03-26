"""
api.py — FastAPI HTTP wrapper for the memory-mcp-server.

Exposes every MCP tool as a REST endpoint so non-MCP callers
(Home Assistant webhooks, Node-RED, shell scripts, etc.) can
push readings and query memories over plain HTTP.

Install extra dep:
    pip install fastapi uvicorn

Run:
    python api.py
    # or with auto-reload during dev:
    uvicorn api:app --host 0.0.0.0 --port 8900 --reload

Endpoints:
    GET  /health                GET  /entities
    POST /remember              POST /recall
    POST /get_context           GET  /profile/{name}
    POST /relate                POST /unrelate
    POST /forget

    POST /open_session          POST /log_turn
    POST /close_session         GET  /get_session/{id}
    POST /extract_and_remember

    POST /record                POST /record/bulk
    POST /query_stream          POST /get_trends
    POST /schedule              POST /cross_query
    POST /prune                 GET  /fading
    GET  /related/{name}

    POST /import/jsonl          POST /import/mem0
    POST /import/mcp-memory-service

    GET  /graph                 GET  /api/graph
    GET  /export/markdown       GET  /export/markdown/{name}
    POST /import/markdown

    GET  /voices/unknown        POST /voices/enroll
    POST /voices/merge          POST /voices/update_print

    GET  /admin/                GET  /admin/entities
    GET  /admin/entity/{name}   GET  /admin/readings
    GET  /admin/settings        POST /admin/token/regenerate
    POST /admin/prune
"""

import asyncio
import json
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response

# All business logic lives in server.py — no duplication
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import server as mem
from admin import router as admin_router
from voice_routes import router as voice_router
from graph_routes import router as graph_router
from exporters.markdown import (
    entity_to_markdown,
    export_all as export_all_markdown,
    import_files as import_markdown_files,
)
from importers.jsonl import import_jsonl
from importers.mem0 import import_mem0
from importers.mcp_memory_service import import_mcp_memory_service

# ── Lifespan ───────────────────────────────────────────────────────────────────

async def _probe_ollama() -> None:
    """
    Probe the configured AI base URL at startup and log the result.

    Uses the OpenAI-compatible /models endpoint — works with Ollama, LM Studio,
    and vLLM. Runs in a thread so it doesn't block the async event loop.
    Failure is a warning, not an error — the server starts regardless.
    """
    base = mem.os.environ.get("MEMORY_AI_BASE_URL", "http://localhost:11434/v1")
    probe_url = base.rstrip("/") + "/models"

    def _check() -> tuple[bool, str]:
        try:
            urllib.request.urlopen(probe_url, timeout=3)
            return True, ""
        except urllib.error.URLError as exc:
            return False, str(exc.reason)
        except Exception as exc:
            return False, str(exc)

    ok, reason = await asyncio.to_thread(_check)
    if ok:
        mem.log.info("AI backend reachable at %s", base)
    else:
        mem.log.warning(
            "AI backend NOT reachable at %s — embedding and LLM calls will fail until it is (%s)",
            base, reason,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    mem.setup_logging()
    mem.init_db()
    await _probe_ollama()
    asyncio.create_task(mem.pattern_engine_loop())
    yield


app = FastAPI(
    title="Memory MCP — HTTP API",
    description="Persistent semantic memory + time-series for OpenHome abilities.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# MEMORY_CORS_ORIGINS — comma-separated list of allowed origins, or "*" (default)
# Examples:
#   MEMORY_CORS_ORIGINS=*
#   MEMORY_CORS_ORIGINS=http://homeassistant.local,http://localhost:3000

_cors_raw = mem.os.environ.get("MEMORY_CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Bearer token authentication ────────────────────────────────────────────────
# Paths that bypass auth (monitoring + admin UI + API docs)
_AUTH_EXEMPT = (
    "/health",
    "/admin",
    "/docs",
    "/openapi",
    "/redoc",
    "/favicon.ico",
    "/graph",            # vis.js SPA — protect at network layer like /admin
    "/export/markdown",  # browser download; auth-exempt so <a href> works directly
)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Require 'Authorization: Bearer <token>' on all API endpoints.

    Exemptions (no token needed):
      /health        — uptime monitoring
      /admin/*       — admin UI (protect at network layer instead)
      /docs, /redoc  — Swagger / ReDoc UI
      /openapi.json  — OpenAPI schema

    Auth is disabled entirely when no token is configured (MEMORY_API_TOKEN not set
    and no token in the database). In that case all requests pass through.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        for prefix in _AUTH_EXEMPT:
            if request.url.path.startswith(prefix):
                return await call_next(request)

        expected = mem.get_api_token()
        if not expected:
            return await call_next(request)  # auth disabled

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"ok": False, "error": "Missing Authorization: Bearer <token> header"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if auth_header[7:] != expected:
            mem.log.warning("Auth failure from %s", request.client.host if request.client else "unknown")
            return JSONResponse(
                {"ok": False, "error": "Invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


app.add_middleware(AuthMiddleware)

app.include_router(admin_router)
app.include_router(voice_router)
app.include_router(graph_router)

# Serve vendored frontend assets (Bootstrap, HTMX, vis-network) from local disk.
# No CDN dependencies — eliminates supply-chain risk from third-party script injection.
# To update a library: edit tools/download_vendor.py and re-run it.
_VENDOR_DIR = Path(__file__).parent / "static" / "vendor"
app.mount("/static/vendor", StaticFiles(directory=str(_VENDOR_DIR)), name="vendor")


# ── Request / response models ──────────────────────────────────────────────────

class RememberRequest(BaseModel):
    entity_name: str
    fact: str
    entity_type: str = "person"
    category: str = "general"
    confidence: float = 1.0
    source: str | None = None
    source_trust: int | None = Field(
        default=None,
        ge=1, le=5,
        description="Source trust tier: 5=user, 4=hardware, 3=system, 2=inferred, 1=external",
    )
    meta: dict | None = None

class RecallRequest(BaseModel):
    query: str
    entity_name: str | None = None
    category: str | None = None
    top_k: int = 5
    recency_weight: float = 0.0
    min_confidence: float = 0.0
    min_trust: int = Field(
        default=0,
        ge=0, le=5,
        description="Exclude memories below this trust tier (0=all, 1=external+, 3=system+, 5=user only)",
    )
    mode: str = Field(
        default="vector",
        pattern="^(vector|keyword|hybrid)$",
        description="Retrieval mode: vector (cosine), keyword (FTS5/BM25), or hybrid",
    )

class GetContextRequest(BaseModel):
    entity_name: str
    context_query: str
    max_facts: int = 5
    min_trust: int = Field(
        default=0,
        ge=0, le=5,
        description="Only include memories at or above this trust tier (0=all)",
    )

class RelateRequest(BaseModel):
    entity_a: str
    entity_b: str
    rel_type: str
    meta: dict | None = None

class UnrelateRequest(BaseModel):
    entity_a: str
    entity_b: str
    rel_type: str

class ForgetRequest(BaseModel):
    entity_name: str
    memory_id: int | None = None

class RecordRequest(BaseModel):
    entity_name: str
    metric: str
    value: Any = Field(description="float (numeric), str (categorical), or dict (composite)")
    unit: str | None = None
    source: str | None = None
    entity_type: str = "person"
    ts: float | None = None

class QueryStreamRequest(BaseModel):
    entity_name: str
    metric: str
    start_ts: float | None = None
    end_ts: float | None = None
    granularity: str = "raw"    # 'raw' | 'hour' | 'day' | 'week'
    limit: int = 100

class TrendsRequest(BaseModel):
    entity_name: str
    metric: str
    window: str = "week"        # 'day' | 'week' | 'month'

class ScheduleRequest(BaseModel):
    entity_name: str
    title: str
    start_ts: float
    end_ts: float | None = None
    recurrence: str = "none"
    meta: dict | None = None
    entity_type: str = "person"

class CrossQueryRequest(BaseModel):
    query: str
    top_k: int = 5

class FadingRequest(BaseModel):
    entity_name: str | None = None
    threshold: float = 0.5
    limit: int = 20

class OpenSessionRequest(BaseModel):
    entity_name: str
    entity_type: str = "person"

class LogTurnRequest(BaseModel):
    session_id: int
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str

class CloseSessionRequest(BaseModel):
    session_id: int
    summary: str | None = None

class ExtractAndRememberRequest(BaseModel):
    entity_name: str
    text: str
    entity_type: str = "person"
    model: str | None = None


class WmOpenRequest(BaseModel):
    task_name: str
    entity_name: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)

class WmSetRequest(BaseModel):
    task_id: int
    key: str
    value: object  # any JSON-serialisable value

class WmGetRequest(BaseModel):
    task_id: int
    key: str | None = None

class WmListRequest(BaseModel):
    entity_name: str | None = None
    status: str = Field(default="open", pattern="^(open|closed|expired|all)$")

class WmCloseRequest(BaseModel):
    task_id: int
    promote: bool = False


class SearchSessionsRequest(BaseModel):
    query: str
    entity_name: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class GetContextBudgetRequest(BaseModel):
    entity_name: str
    context_query: str
    token_budget: int = Field(default=1500, ge=50, le=32000)
    recall_mode: str = Field(default="hybrid", pattern="^(vector|keyword|hybrid)$")
    include_readings: bool = True


class IntendRequest(BaseModel):
    entity_name: str
    trigger_text: str
    action_text: str
    entity_type: str = "person"
    expires_ts: float | None = None


class CheckIntentionsRequest(BaseModel):
    entity_name: str
    text: str


class DismissIntentionRequest(BaseModel):
    intention_id: int


class LocateRequest(BaseModel):
    entity_name: str
    container_name: str
    entity_type: str = "object"
    container_type: str = "room"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "manual"
    note: str | None = None


class FindRequest(BaseModel):
    entity_name: str


class SeenAtRequest(BaseModel):
    entity_name: str
    container_name: str


# ── Helper: wrap any coroutine and surface errors as HTTP 500 ──────────────────

async def run(coro):
    try:
        result = await coro
        return {"result": result, "ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Favicon ────────────────────────────────────────────────────────────────────

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#4f46e5"/>'
    '<text x="16" y="23" font-size="18" font-family="sans-serif" '
    'font-weight="bold" text-anchor="middle" fill="white">M</text>'
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ── MCP protocol version (resolved once at import time) ────────────────────────

from importlib.metadata import version as _pkg_version
import mcp.types as _mcp_types

_MCP_SDK_VERSION        = _pkg_version("mcp")
_MCP_PROTOCOL_VERSION   = _mcp_types.LATEST_PROTOCOL_VERSION
_MCP_DEFAULT_NEGOTIATED = _mcp_types.DEFAULT_NEGOTIATED_VERSION


# ── Health + introspection ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Quick liveness check — returns row counts and MCP protocol version."""
    db = mem.get_db()
    n_entities = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    n_memories = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    n_readings = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    return {
        "status": "ok",
        "entities": n_entities,
        "memories": n_memories,
        "readings": n_readings,
        "ts": time.time(),
        "mcp_protocol_version": _MCP_PROTOCOL_VERSION,
    }


@app.get("/mcp-info")
async def mcp_info():
    """
    MCP spec compliance information.

    Returns the MCP protocol version this server implements, the SDK version
    in use, and the registered tool list.  Useful for clients that need to
    verify compatibility before connecting.
    """
    tools = [
        {"name": t.name, "description": t.description or ""}
        for t in mem.TOOLS
    ]
    return {
        "mcp_sdk_version":               _MCP_SDK_VERSION,
        "mcp_protocol_version":          _MCP_PROTOCOL_VERSION,
        "mcp_default_negotiated_version": _MCP_DEFAULT_NEGOTIATED,
        "tool_count":                    len(tools),
        "tools":                         tools,
    }


@app.get("/entities")
async def list_entities():
    """List all known entities with type and meta."""
    db = mem.get_db()
    rows = db.execute("SELECT name, type, meta, updated FROM entities ORDER BY name").fetchall()
    db.close()
    return {
        "entities": [
            {"name": r["name"], "type": r["type"],
             "meta": json.loads(r["meta"]),
             "updated": r["updated"]}
            for r in rows
        ]
    }


# ── Tier 1 — Semantic memory endpoints ────────────────────────────────────────

@app.post("/remember")
async def remember(req: RememberRequest):
    return await run(mem.tool_remember(**req.model_dump()))


@app.post("/recall")
async def recall(req: RecallRequest):
    return await run(mem.tool_recall(**req.model_dump()))


@app.post("/get_context")
async def get_context(req: GetContextRequest):
    return await run(mem.tool_get_context(**req.model_dump()))


@app.get("/profile/{entity_name}")
async def get_profile(entity_name: str):
    return await run(mem.tool_get_profile(entity_name))


@app.post("/relate")
async def relate(req: RelateRequest):
    return await run(mem.tool_relate(**req.model_dump()))


@app.post("/unrelate")
async def unrelate(req: UnrelateRequest):
    return await run(mem.tool_unrelate(**req.model_dump()))


@app.post("/forget")
async def forget(req: ForgetRequest):
    return await run(mem.tool_forget(**req.model_dump()))


# ── Tier 1.5 — Episodic memory endpoints ──────────────────────────────────────

@app.post("/open_session")
async def open_session(req: OpenSessionRequest):
    """Open a new conversation session for an entity. Returns session_id (int)."""
    return await run(mem.tool_open_session(**req.model_dump()))


@app.post("/log_turn")
async def log_turn(req: LogTurnRequest):
    """Append a turn to an open session. role: 'user' | 'assistant' | 'system'."""
    return await run(mem.tool_log_turn(**req.model_dump()))


@app.post("/close_session")
async def close_session(req: CloseSessionRequest):
    """Close a session and optionally store a summary."""
    return await run(mem.tool_close_session(**req.model_dump()))


@app.get("/get_session/{session_id}")
async def get_session(session_id: int):
    """Retrieve a session transcript with all turns, entity name, and summary."""
    return await run(mem.tool_get_session(session_id))


@app.post("/extract_and_remember")
async def extract_and_remember(req: ExtractAndRememberRequest):
    """Extract facts from text via LLM and store them as memories for the entity."""
    return await run(mem.tool_extract_and_remember(**req.model_dump()))


# ── Working memory (Tier 1.75) ─────────────────────────────────────────────────

@app.post("/wm/open")
async def wm_open(req: WmOpenRequest):
    """Open a new working-memory task scope. Returns task_id."""
    return await run(mem.tool_wm_open(**req.model_dump()))


@app.post("/wm/set")
async def wm_set(req: WmSetRequest):
    """Set or overwrite a key/value slot in a working-memory task."""
    return await run(mem.tool_wm_set(**req.model_dump()))


@app.post("/wm/get")
async def wm_get(req: WmGetRequest):
    """Get one slot (by key) or all slots from a working-memory task."""
    return await run(mem.tool_wm_get(**req.model_dump()))


@app.get("/wm/list")
async def wm_list(entity_name: str | None = None, status: str = "open"):
    """List working-memory tasks, optionally filtered by entity or status."""
    return await run(mem.tool_wm_list(entity_name=entity_name, status=status))


@app.get("/wm/{task_id}")
async def wm_get_task(task_id: int):
    """Get all slots and metadata for a working-memory task by id."""
    return await run(mem.tool_wm_get(task_id=task_id))


@app.post("/wm/close")
async def wm_close(req: WmCloseRequest):
    """Close a working-memory task, optionally promoting slots to long-term memory."""
    return await run(mem.tool_wm_close(**req.model_dump()))


# ── Session search + token-budget context ─────────────────────────────────────

@app.post("/search_sessions")
async def search_sessions(req: SearchSessionsRequest):
    """
    Full-text keyword search across session turn content (FTS5/BM25).
    No embedding model required.
    """
    return await run(mem.tool_search_sessions(**req.model_dump()))


@app.post("/get_context_budget")
async def get_context_budget(req: GetContextBudgetRequest):
    """Token-budget-aware context snapshot. Use recall_mode='keyword' for Pi environments."""
    return await run(mem.tool_get_context_budget(**req.model_dump()))


# ── Prospective / intention memory ─────────────────────────────────────────────

@app.post("/intend")
async def intend(req: IntendRequest):
    """Set a prospective intention: trigger_text → action_text."""
    return await run(mem.tool_intend(**req.model_dump()))


@app.post("/check_intentions")
async def check_intentions(req: CheckIntentionsRequest):
    """Check whether text triggers any active intentions for an entity."""
    return await run(mem.tool_check_intentions(**req.model_dump()))


@app.post("/dismiss_intention")
async def dismiss_intention(req: DismissIntentionRequest):
    """Deactivate an intention by id."""
    return await run(mem.tool_dismiss_intention(**req.model_dump()))


@app.get("/intentions")
async def list_intentions(entity_name: str | None = None, active_only: bool = True):
    """List intentions, optionally filtered by entity."""
    return await run(mem.tool_list_intentions(entity_name=entity_name, active_only=active_only))


# ── Tier 5 — Spatial / location memory endpoints ───────────────────────────────

@app.post("/locate")
async def locate(req: LocateRequest):
    """Store or update the last-known location of an object."""
    return await run(mem.tool_locate(**req.model_dump()))


@app.post("/find")
async def find(req: FindRequest):
    """Return the last known location of an object with confidence and age."""
    return await run(mem.tool_find(**req.model_dump()))


@app.post("/seen_at")
async def seen_at(req: SeenAtRequest):
    """Confirm an object is still at a location; bumps confidence."""
    return await run(mem.tool_seen_at(**req.model_dump()))


@app.get("/location_history/{entity_name}")
async def location_history(entity_name: str, limit: int = Query(default=10, ge=1, le=100)):
    """Return the full location history of an object."""
    return await run(mem.tool_location_history(entity_name=entity_name, limit=limit))


# ── Tier 2 — Time-series endpoints ────────────────────────────────────────────

@app.post("/record")
async def record(req: RecordRequest):
    """
    Ingest a single time-series reading.

    Designed for high-frequency callers — Home Assistant automations,
    Node-RED flows, cron jobs, etc.

    Example (curl):
        curl -X POST http://localhost:8900/record \\
             -H 'Content-Type: application/json' \\
             -d '{"entity_name":"living_room","metric":"temperature","value":71.4,"unit":"F","source":"ha","entity_type":"room"}'
    """
    return await run(mem.tool_record(**req.model_dump()))


@app.post("/query_stream")
async def query_stream(req: QueryStreamRequest):
    return await run(mem.tool_query_stream(**req.model_dump()))


@app.post("/get_trends")
async def get_trends(req: TrendsRequest):
    return await run(mem.tool_get_trends(**req.model_dump()))


@app.post("/schedule")
async def schedule(req: ScheduleRequest):
    return await run(mem.tool_schedule(**req.model_dump()))


# ── Cross-tier ─────────────────────────────────────────────────────────────────

@app.post("/cross_query")
async def cross_query(req: CrossQueryRequest):
    return await run(mem.tool_cross_query(**req.model_dump()))


# ── Maintenance ─────────────────────────────────────────────────────────────────

@app.post("/prune")
async def prune():
    """
    Delete raw readings older than RETENTION_DAYS (default 30 days).
    Rollups and memories are not affected.
    """
    return await run(mem.tool_prune())


@app.get("/fading")
async def fading_memories(
    entity_name: str | None = None,
    threshold: float = 0.5,
    limit: int = 20,
):
    """
    Return memories whose confidence has fallen below `threshold`, most faded first.

    Query params:
      entity_name — scope to one entity (optional)
      threshold   — confidence ceiling (default 0.5)
      limit       — max rows to return (default 20)
    """
    return await run(mem.tool_get_fading_memories(
        entity_name=entity_name,
        threshold=threshold,
        limit=limit,
    ))


# ── Convenience: bulk record (for batch sensor pushes) ────────────────────────

class BulkRecordRequest(BaseModel):
    readings: list[RecordRequest]

_TRUST_FIELD = Field(
    default=None, ge=1, le=5,
    description=(
        "Override source trust tier for imported memories. "
        "5=user, 4=hardware, 3=system, 2=inferred, 1=external. "
        "Defaults to MEMORY_TRUST_DEFAULT_IMPORT (env var, default 1)."
    ),
)

class ImportJSONLRequest(BaseModel):
    content: str = Field(
        description="Raw JSONL text (one JSON object per line). Max 5 MB.",
        max_length=5 * 1024 * 1024,
    )
    source_trust: int | None = _TRUST_FIELD

class ImportMem0Request(BaseModel):
    user_id: str = Field(description="mem0 user identifier; becomes the entity name")
    api_key: str | None = Field(default=None, description="mem0 API key")
    base_url: str = Field(
        default="https://api.mem0.ai",
        description="mem0 API base URL (http/https only)",
    )
    agent_id: str | None = None
    app_id: str | None = None
    entity_type: str = "person"
    source_trust: int | None = _TRUST_FIELD

class ImportMCPMemoryServiceRequest(BaseModel):
    db_path: str = Field(
        description="Absolute path to the mcp-memory-service SQLite file"
    )
    entity_name: str = Field(
        default="imported",
        description="Entity name in memory-mcp to receive all imported memories",
    )
    entity_type: str = "person"
    source_trust: int | None = _TRUST_FIELD

class ImportMarkdownRequest(BaseModel):
    files: dict[str, str] = Field(
        description="Mapping of filename → markdown content, e.g. {'Brian.md': '---\\ntype: person\\n...'}"
    )

@app.post("/record/bulk")
async def record_bulk(req: BulkRecordRequest):
    """
    Ingest multiple readings in one request.
    Useful for Home Assistant batch webhooks or IoT gateways.
    """
    results = []
    for r in req.readings:
        try:
            result = await mem.tool_record(**r.model_dump())
            results.append({"ok": True, "result": result})
        except Exception as e:
            results.append({"ok": False, "error": str(e)})
    return {"results": results, "count": len(results)}


# ── Graph traversal ────────────────────────────────────────────────────────────

@app.get("/related/{entity_name}")
async def related(entity_name: str, depth: int = 2, max_results: int = Query(50, ge=1, le=500)):
    """
    Find all entities reachable from entity_name within `depth` hops via active
    relations.  Traversal is bidirectional.  Depth is clamped to 1–5.

    Query params:
      depth       — max hops from the starting entity (default 2, max 5)
      max_results — max entities to return (default 50)
    """
    return await run(mem.tool_get_related(
        entity_name=entity_name,
        depth=depth,
        max_results=max_results,
    ))


# ── JSONL import (Anthropic official MCP Memory format) ────────────────────────

@app.post("/import/jsonl")
async def import_jsonl_endpoint(req: ImportJSONLRequest):
    """
    Import from the Anthropic official @modelcontextprotocol/server-memory JSONL format.

    Accepts the raw JSONL text (content string — never a file path).  Content is
    capped at 5 MB; up to 10 000 lines are processed.

    Format: one JSON object per line::

        {"type": "entity",   "name": "John_Smith", "entityType": "person",
         "observations": ["Speaks fluent Spanish"]}
        {"type": "relation", "from": "John_Smith",  "to": "Acme_Corp",
         "relationType": "works_at"}

    Returns:
        {"added": int, "skipped": int, "errors": [...], "ok": true}
    """
    try:
        result = await import_jsonl(req.content, source_trust=req.source_trust)
        return {**result.to_dict(), "ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── mem0 import ────────────────────────────────────────────────────────────────

@app.post("/import/mem0")
async def import_mem0_endpoint(req: ImportMem0Request):
    """
    Import memories from mem0 (cloud or self-hosted).

    All memories for the given user_id are imported as general observations on
    a single entity named after the user_id.  Pagination is handled automatically;
    exponential backoff is applied on HTTP 429 responses.

    base_url must be http:// or https:// — other schemes are rejected.
    """
    try:
        result = await import_mem0(**req.model_dump())
        return {**result.to_dict(), "ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── mcp-memory-service import ──────────────────────────────────────────────────

@app.post("/import/mcp-memory-service")
async def import_mcp_memory_service_endpoint(req: ImportMCPMemoryServiceRequest):
    """
    Import from a doobidoo/mcp-memory-service SQLite database.

    Reads the database at db_path directly (no network call).  The file must
    exist, be a regular file, and pass the SQLite magic-byte check.  Stop
    mcp-memory-service before importing to avoid a locked-database error.
    """
    try:
        result = await import_mcp_memory_service(**req.model_dump())
        return {**result.to_dict(), "ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Markdown export ────────────────────────────────────────────────────────────

@app.post("/import/markdown")
async def import_markdown(req: ImportMarkdownRequest):
    """
    Import entities from Obsidian-compatible Markdown files.

    Accepts the same ``{ files: { "Brian.md": "...", ... } }`` shape that
    ``GET /export/markdown`` returns — making export → edit → import a clean
    round-trip.

    Each file is parsed for:

    - Entity name (``# H1`` heading, or filename stem as fallback)
    - Entity type (frontmatter ``type:`` field, default ``"person"``)
    - Memories (``## Observations`` bullets, grouped by ``### Category``)
    - Relations (``## Relations`` bullets: ``- [[other_name]] — rel_type``)

    Memories are **deduplicated** — an existing fact with the same text is
    counted in ``memories_skipped`` rather than re-inserted.
    Relations are **idempotent** — re-importing an active relation is a no-op.
    """
    try:
        result = await import_markdown_files(req.files)
        return {**result, "ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/export/markdown/{entity_name}")
async def export_markdown_entity(entity_name: str):
    """
    Export a single entity's memories as Obsidian-compatible Markdown.

    Returns ``text/plain`` with a ``Content-Disposition: attachment`` header
    so browsers trigger a file download.  Active memories and active relations
    only (superseded and soft-deleted excluded).
    """
    content = entity_to_markdown(entity_name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_name}' not found")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{entity_name}.md"'},
    )


@app.get("/export/markdown")
async def export_markdown_all():
    """
    Export all entities as Obsidian-compatible Markdown.

    Returns a JSON object mapping ``{entity_name}.md`` → markdown string.
    Pipe through a script to write individual files to your vault directory::

        import json, pathlib, requests
        vault = pathlib.Path("/path/to/obsidian/vault")
        files = requests.get("http://localhost:8900/export/markdown").json()["files"]
        for filename, content in files.items():
            (vault / filename).write_text(content)
    """
    return {"files": export_all_markdown()}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8900, reload=False)
