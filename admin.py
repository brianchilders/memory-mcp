"""
admin.py — Web dashboard for the memory-mcp-server.

Mounted at /admin in api.py.  Server-rendered HTML with Bootstrap 5 (CDN)
and HTMX for partial-page updates (entity detail, live refresh, prune action).

Routes:
  GET  /admin/                         Dashboard — counts + recent activity
  GET  /admin/entities                 Entity list
  GET  /admin/entity/{name}            Entity detail (profile + readings)
  GET  /admin/readings                 Recent readings stream
  POST /admin/prune                    Prune old readings (HTMX fragment)
  POST /admin/memory/{id}/delete       Delete a single memory (HTMX fragment)
  POST /admin/entity/{name}/remember   Add an observation (HTMX fragment)
  GET  /admin/settings                 Token management
  POST /admin/token/regenerate         Generate new token (HTMX fragment)
"""

import html as html_mod
import secrets
import time
from pathlib import Path

import server as mem
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "admin"))


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = mem.get_db()
    stats = {
        "entities": db.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "memories": db.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
        "readings": db.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
        "rollups":  db.execute("SELECT COUNT(*) FROM reading_rollups").fetchone()[0],
        "patterns": db.execute("SELECT COUNT(*) FROM promoted_patterns").fetchone()[0],
        "schedules":db.execute("SELECT COUNT(*) FROM schedule_events").fetchone()[0],
    }

    oldest = db.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
    newest = db.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    stats["oldest_reading"] = _ts(oldest) if oldest else "—"
    stats["newest_reading"] = _ts(newest) if newest else "—"

    recent_memories = db.execute(
        """SELECT e.name AS entity, m.category, m.fact, m.updated
           FROM memories m JOIN entities e ON e.id=m.entity_id
           ORDER BY m.updated DESC LIMIT 10"""
    ).fetchall()
    recent_memories = [
        {**dict(r), "updated": _ts(r["updated"])} for r in recent_memories
    ]

    recent_patterns = db.execute(
        """SELECT e.name AS entity, pp.metric, pp.pattern_key, pp.detected
           FROM promoted_patterns pp JOIN entities e ON e.id=pp.entity_id
           ORDER BY pp.detected DESC LIMIT 10"""
    ).fetchall()
    recent_patterns = [
        {**dict(r), "detected": _ts(r["detected"])} for r in recent_patterns
    ]

    db.close()

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "stats": stats,
        "recent_memories": recent_memories,
        "recent_patterns": recent_patterns,
        "retention_days": mem.RETENTION_DAYS,
        "now": _ts(time.time()),
    })


# ── Entity list ────────────────────────────────────────────────────────────────

@router.get("/entities", response_class=HTMLResponse)
async def entity_list(request: Request):
    import json
    db = mem.get_db()
    rows = db.execute(
        """SELECT e.name, e.type, e.meta, e.updated,
                  COUNT(DISTINCT m.id) AS mem_count,
                  COUNT(DISTINCT r.id) AS reading_count
           FROM entities e
           LEFT JOIN memories m ON m.entity_id=e.id
           LEFT JOIN readings r ON r.entity_id=e.id
           GROUP BY e.id ORDER BY e.name"""
    ).fetchall()
    db.close()

    entities = []
    for r in rows:
        meta = json.loads(r["meta"]) if r["meta"] else {}
        entities.append({
            "name":          r["name"],
            "type":          r["type"],
            "meta":          meta,
            "updated":       _ts(r["updated"]),
            "mem_count":     r["mem_count"],
            "reading_count": r["reading_count"],
        })

    return templates.TemplateResponse(request, "entities.html", {
        "request":  request,
        "entities": entities,
    })


# ── Entity detail ──────────────────────────────────────────────────────────────

@router.get("/entity/{name}", response_class=HTMLResponse)
async def entity_detail(request: Request, name: str):
    import json
    db = mem.get_db()
    e = db.execute("SELECT * FROM entities WHERE name=?", (name,)).fetchone()
    if not e:
        db.close()
        return HTMLResponse(
            f"<p>No entity named <strong>{html_mod.escape(name)}</strong>.</p>", status_code=404
        )

    eid = e["id"]

    memories = db.execute(
        """SELECT id, category, fact, confidence, source, updated, superseded_by
           FROM memories WHERE entity_id=? ORDER BY category, updated DESC""",
        (eid,)
    ).fetchall()

    relations = db.execute(
        """SELECT e2.name AS other, r.rel_type FROM relations r
           JOIN entities e2 ON e2.id=r.entity_b WHERE r.entity_a=?
           UNION
           SELECT e1.name AS other, r.rel_type||'_of' FROM relations r
           JOIN entities e1 ON e1.id=r.entity_a WHERE r.entity_b=?""",
        (eid, eid)
    ).fetchall()

    latest_readings = db.execute(
        """SELECT metric, unit, value_type, value_num, value_cat, value_json, MAX(ts) AS ts
           FROM readings WHERE entity_id=? GROUP BY metric ORDER BY metric""",
        (eid,)
    ).fetchall()

    recent_readings = db.execute(
        """SELECT metric, value_type, value_num, value_cat, value_json, unit, ts
           FROM readings WHERE entity_id=? ORDER BY ts DESC LIMIT 50""",
        (eid,)
    ).fetchall()

    patterns = db.execute(
        """SELECT pp.metric, pp.pattern_key, m.fact, pp.detected
           FROM promoted_patterns pp
           JOIN memories m ON m.id=pp.memory_id
           WHERE pp.entity_id=?
           ORDER BY pp.detected DESC""",
        (eid,)
    ).fetchall()

    upcoming = db.execute(
        """SELECT title, start_ts, end_ts, recurrence FROM schedule_events
           WHERE entity_id=? AND start_ts>=? ORDER BY start_ts LIMIT 10""",
        (eid, time.time())
    ).fetchall()

    db.close()

    # Group memories by category
    by_cat: dict[str, list] = {}
    for m in memories:
        cat = m["category"]
        if cat not in by_cat:
            by_cat[cat] = []
        by_cat[cat].append({
            "id":          m["id"],
            "fact":        m["fact"],
            "confidence":  m["confidence"],
            "source":      m["source"],
            "updated":     _ts(m["updated"]),
            "superseded":  m["superseded_by"] is not None,
        })

    return templates.TemplateResponse(request, "entity.html", {
        "request":        request,
        "name":           name,
        "entity_type":    e["type"],
        "meta":           json.loads(e["meta"]) if e["meta"] else {},
        "memories_by_cat": by_cat,
        "relations":      [dict(r) for r in relations],
        "latest_readings": [
            {"metric": r["metric"], "value": mem._fmt(r), "ts": _ts(r["ts"])}
            for r in latest_readings
        ],
        "recent_readings": [
            {"metric": r["metric"], "value": mem._fmt(r), "ts": _ts(r["ts"])}
            for r in recent_readings
        ],
        "patterns":       [dict(p) for p in patterns],
        "upcoming":       [
            {
                "title":       ev["title"],
                "start":       _ts(ev["start_ts"]),
                "end":         _ts(ev["end_ts"]) if ev["end_ts"] else "—",
                "recurrence":  ev["recurrence"],
            }
            for ev in upcoming
        ],
    })


# ── Readings stream ────────────────────────────────────────────────────────────

@router.get("/readings", response_class=HTMLResponse)
async def readings_stream(request: Request, limit: int = 100):
    db = mem.get_db()
    rows = db.execute(
        """SELECT e.name AS entity, r.metric, r.value_type,
                  r.value_num, r.value_cat, r.value_json, r.unit, r.source, r.ts
           FROM readings r JOIN entities e ON e.id=r.entity_id
           ORDER BY r.ts DESC LIMIT ?""",
        (limit,)
    ).fetchall()

    oldest = db.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
    total  = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()

    readings = [
        {
            "entity": r["entity"],
            "metric": r["metric"],
            "value":  mem._fmt(r),
            "source": r["source"] or "—",
            "ts":     _ts(r["ts"]),
        }
        for r in rows
    ]

    return templates.TemplateResponse(request, "readings.html", {
        "request":       request,
        "readings":      readings,
        "total":         total,
        "oldest":        _ts(oldest) if oldest else "—",
        "limit":         limit,
        "retention_days": mem.RETENTION_DAYS,
    })


# ── Prune action (HTMX target) ─────────────────────────────────────────────────

@router.post("/prune", response_class=HTMLResponse)
async def prune_action(request: Request):
    count = await mem._prune_readings()
    db = mem.get_db()
    remaining = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    db.close()
    return HTMLResponse(
        f'<div class="alert alert-success mt-2">'
        f'Pruned <strong>{count}</strong> readings older than '
        f'{mem.RETENTION_DAYS} days. '
        f'<strong>{remaining:,}</strong> readings remain.'
        f'</div>'
    )


# ── Memory curation ────────────────────────────────────────────────────────────

@router.post("/memory/{memory_id}/delete", response_class=HTMLResponse)
async def memory_delete(request: Request, memory_id: int):
    """
    Delete a single memory by integer id.  Returns an HTMX-swappable HTML
    fragment — an empty string on success (the list item removes itself) or a
    small error badge on failure.

    Uses POST (not DELETE) so plain HTML forms work without JavaScript.
    """
    db = mem.get_db()
    row = db.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row:
        db.close()
        return HTMLResponse(
            f'<span class="badge bg-danger">Memory {memory_id} not found</span>',
            status_code=404,
        )
    db.execute("DELETE FROM memory_vectors WHERE rowid=?", (memory_id,))
    db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    db.commit()
    db.close()
    # Return empty — HTMX hx-swap="outerHTML" on the list item removes the row
    return HTMLResponse("", status_code=200)


@router.post("/entity/{name}/remember", response_class=HTMLResponse)
async def entity_remember(
    request: Request,
    name: str,
    fact: str = Form(...),
    category: str = Form("general"),
):
    """
    Add an observation (memory) to an existing entity from the admin UI.
    Returns an HTMX-swappable HTML fragment — a new <li> row on success or
    an error alert on failure.

    The entity must already exist.  Fact is limited to 10 000 characters.
    Category is validated against the allowed enum.
    """
    _VALID_CATEGORIES = {
        "preference", "habit", "routine", "relationship", "insight", "general"
    }
    fact = fact.strip()[:10_000]
    category = category.strip().lower()
    if not fact:
        return HTMLResponse(
            '<div class="alert alert-warning py-1 mb-0">Fact cannot be empty.</div>',
            status_code=400,
        )
    if category not in _VALID_CATEGORIES:
        category = "general"

    # Validate entity exists
    db = mem.get_db()
    e = db.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
    if not e:
        db.close()
        return HTMLResponse(
            f'<div class="alert alert-danger py-1 mb-0">'
            f'Entity {html_mod.escape(name)!r} not found.</div>',
            status_code=404,
        )
    db.close()

    # Delegate to tool_remember (handles embedding + contradiction detection)
    try:
        await mem.tool_remember(
            entity_name=name,
            fact=fact,
            category=category,
            source="admin_ui",
        )
    except Exception as exc:
        return HTMLResponse(
            f'<div class="alert alert-danger py-1 mb-0">'
            f'Error: {html_mod.escape(str(exc))}</div>',
            status_code=500,
        )

    escaped_fact     = html_mod.escape(fact)
    escaped_category = html_mod.escape(category)
    return HTMLResponse(
        f'<li class="list-group-item py-2" id="new-memory-row">'
        f'  <div class="d-flex justify-content-between align-items-start">'
        f'    <pre class="fact flex-grow-1 me-2">{escaped_fact}</pre>'
        f'    <div class="text-end text-muted small" style="min-width:110px">'
        f'      <span class="badge bg-success mb-1">just added</span>'
        f'    </div>'
        f'  </div>'
        f'  <small class="text-muted">category: {escaped_category} · source: admin_ui</small>'
        f'</li>',
        status_code=200,
    )


# ── Settings / token management ────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    token        = mem.get_api_token()
    token_source = mem.get_token_source()
    return templates.TemplateResponse(request, "settings.html", {
        "request":      request,
        "token":        token,
        "token_source": token_source,   # "env" | "db" | "none"
        "auth_enabled": token is not None,
    })


@router.post("/token/regenerate", response_class=HTMLResponse)
async def token_regenerate(request: Request):
    """Generate a new random token and store it. HTMX target — returns HTML fragment."""
    if mem.get_token_source() == "env":
        return HTMLResponse(
            '<div class="alert alert-warning mt-2">'
            'Token is set via <code>MEMORY_API_TOKEN</code> environment variable '
            'and cannot be regenerated from the UI. Update the env var instead.'
            '</div>'
        )
    new_token = secrets.token_hex(32)
    mem.set_api_token(new_token)
    return HTMLResponse(
        f'<div class="alert alert-success mt-3">'
        f'<strong>New token generated.</strong> Copy it now — it will not be shown again in full.<br>'
        f'<code id="new-token" class="d-block mt-2 p-2 bg-light rounded" '
        f'style="word-break:break-all;font-size:0.85rem">{new_token}</code>'
        f'<button class="btn btn-sm btn-outline-secondary mt-2" '
        f'onclick="navigator.clipboard.writeText(document.getElementById(\'new-token\').textContent)'
        f'.then(()=>this.textContent=\'Copied!\')">Copy to clipboard</button>'
        f'<div class="text-muted small mt-2">Reload the page to see the masked token.</div>'
        f'</div>'
    )
