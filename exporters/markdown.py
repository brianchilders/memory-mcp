"""
exporters/markdown.py — Export entity memories as Obsidian-compatible Markdown.

Each entity produces a single .md file:
  - YAML frontmatter  — type, created, updated, tags
  - H1 heading        — entity name
  - ## Observations   — active memories grouped by category (superseded excluded)
  - ## Relations      — active directed relations as [[wikilinks]] (valid_until IS NULL)

Usage:
    from exporters.markdown import entity_to_markdown, export_all

    # Single entity → str (or None if not found)
    md = entity_to_markdown("Brian")

    # All entities → { "Brian.md": "...", "homeassistant.md": "...", ... }
    files = export_all()
"""

import time

import server as mem


def _iso(epoch: float) -> str:
    """Unix timestamp → ISO 8601 datetime string (local time)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def entity_to_markdown(entity_name: str) -> str | None:
    """
    Render a single entity as an Obsidian-compatible Markdown string.

    Returns ``None`` if the entity does not exist.
    Only active memories (``superseded_by IS NULL``) and active relations
    (``valid_until IS NULL``) are included.
    """
    db = mem.get_db()
    try:
        e = db.execute(
            "SELECT id, name, type, created, updated FROM entities WHERE name = ?",
            (entity_name,),
        ).fetchone()
        if not e:
            return None

        eid = e["id"]

        memories = db.execute(
            """
            SELECT  category, fact, confidence
            FROM    memories
            WHERE   entity_id = ? AND superseded_by IS NULL
            ORDER   BY category, confidence DESC
            """,
            (eid,),
        ).fetchall()

        relations = db.execute(
            """
            SELECT  ea.name AS other_name, r.rel_type
            FROM    relations r
            JOIN    entities  ea ON ea.id = r.entity_b
            WHERE   r.entity_a = ? AND r.valid_until IS NULL
            ORDER   BY r.rel_type, ea.name
            """,
            (eid,),
        ).fetchall()
    finally:
        db.close()

    # ── Frontmatter ──────────────────────────────────────────────────────────
    lines = [
        "---",
        f"type: {e['type']}",
        f"created: {_iso(e['created'])}",
        f"updated: {_iso(e['updated'])}",
        "tags: [memory, auto]",
        "---",
        "",
        f"# {e['name']}",
        "",
    ]

    # ── Observations ─────────────────────────────────────────────────────────
    lines.append("## Observations")
    lines.append("")

    if memories:
        current_cat = None
        for m in memories:
            if m["category"] != current_cat:
                current_cat = m["category"]
                lines.append(f"### {current_cat.capitalize()}")
                lines.append("")
            lines.append(f"- {m['fact']}")
        lines.append("")
    else:
        lines.append("_No observations recorded yet._")
        lines.append("")

    # ── Relations ────────────────────────────────────────────────────────────
    if relations:
        lines.append("## Relations")
        lines.append("")
        for r in relations:
            lines.append(f"- [[{r['other_name']}]] — {r['rel_type']}")
        lines.append("")

    return "\n".join(lines)


def export_all() -> dict[str, str]:
    """
    Return a mapping of ``{entity_name}.md`` → markdown content for every entity.

    Entities that no longer exist between the name query and the detail query
    are silently skipped (safe under concurrent writes).
    """
    db = mem.get_db()
    try:
        names = [
            r[0]
            for r in db.execute(
                "SELECT name FROM entities ORDER BY name"
            ).fetchall()
        ]
    finally:
        db.close()

    result: dict[str, str] = {}
    for name in names:
        md = entity_to_markdown(name)
        if md is not None:
            result[f"{name}.md"] = md
    return result
