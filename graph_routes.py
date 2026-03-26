"""
graph_routes.py — Entity relationship graph endpoint.

Routes:
    GET /graph      — Serves the vis.js graph SPA (auth-exempt, like /admin)
    GET /api/graph  — Returns { nodes, edges } JSON

The bearer token is injected into the rendered HTML so the JS fetch call
can include an Authorization header — no need to exempt /api/graph from auth.
"""

from pathlib import Path

import server as mem
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

router = APIRouter(tags=["graph"])
_templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates" / "graph")
)


@router.get("/graph", response_class=HTMLResponse, include_in_schema=False)
async def graph_page(request: Request):
    """Serve the entity relationship graph SPA."""
    return _templates.TemplateResponse(request, "graph.html", {
        "request":   request,
        "api_token": mem.get_api_token() or "",
    })


@router.get("/api/graph", summary="Entity relationship graph data")
async def graph_data():
    """
    Return entity graph as ``{ nodes, edges }``.

    **nodes** — id, name, type, memory_count (active only), memories list
    **edges** — from, to, label  (active relations only — ``valid_until IS NULL``)

    vis.js Network uses ``from``/``to`` for edge endpoints.
    Memories are included inline per node so the sidebar requires no second request.
    """
    db = mem.get_db()
    try:
        node_rows = db.execute(
            """
            SELECT  e.id,
                    e.name,
                    e.type,
                    COUNT(m.id) AS memory_count
            FROM    entities e
            LEFT JOIN memories m
                    ON  m.entity_id = e.id
                    AND m.superseded_by IS NULL
            GROUP   BY e.id
            ORDER   BY e.name
            """
        ).fetchall()

        edge_rows = db.execute(
            """
            SELECT  r.entity_a  AS "from",
                    r.entity_b  AS "to",
                    r.rel_type  AS label
            FROM    relations r
            WHERE   r.valid_until IS NULL
            """
        ).fetchall()

        mem_rows = db.execute(
            """
            SELECT  entity_id, category, fact, confidence
            FROM    memories
            WHERE   superseded_by IS NULL
            ORDER   BY entity_id, category
            """
        ).fetchall()
    finally:
        db.close()

    # Group memories by entity id for O(1) sidebar lookup
    by_entity: dict[int, list[dict]] = {}
    for m in mem_rows:
        eid = m["entity_id"]
        if eid not in by_entity:
            by_entity[eid] = []
        by_entity[eid].append({
            "fact":       m["fact"],
            "category":   m["category"],
            "confidence": m["confidence"],
        })

    nodes = [
        {
            "id":           r["id"],
            "name":         r["name"],
            "type":         r["type"],
            "memory_count": r["memory_count"],
            "memories":     by_entity.get(r["id"], []),
        }
        for r in node_rows
    ]

    return {
        "nodes": nodes,
        "edges": [dict(e) for e in edge_rows],
    }
