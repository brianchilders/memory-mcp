"""
exporters/markdown.py — Two-way Obsidian-compatible Markdown sync for memory-mcp.

Export: each entity → one .md file with YAML frontmatter + observations + relations.
Import: parse .md files → create/update entities, memories, and relations.

── Export format ──────────────────────────────────────────────────────────────

    ---
    type: person
    created: 2026-01-15T08:30:00
    updated: 2026-03-25T14:22:00
    tags: [memory, auto]
    ---

    # Brian

    ## Observations

    ### Preference

    - Prefers dark roast coffee

    ## Relations

    - [[homeassistant]] — controls

── Import rules ───────────────────────────────────────────────────────────────

  • Entity name: from the first ``# H1`` heading; falls back to the filename
    (minus the ``.md`` suffix) if no heading is found.
  • Entity type: from frontmatter ``type:`` field; defaults to ``"person"``.
  • Observations: bullet items (``- `` or ``* ``) under ``## Observations``.
    ``### Category`` sub-headings set the category for following bullets;
    bullets before any sub-heading use category ``"general"``.
  • Relations: bullet items under ``## Relations`` matching
    ``- [[other_name]] — rel_type`` (em-dash, en-dash, or hyphen accepted).
  • Idempotency: an existing memory with the same ``fact`` text is skipped
    (reported in ``memories_skipped``), not duplicated.
  • Extra sections, prose paragraphs, and unknown frontmatter keys are ignored —
    safe to import hand-edited Obsidian files.

── Public API ─────────────────────────────────────────────────────────────────

    from exporters.markdown import entity_to_markdown, export_all
    from exporters.markdown import parse_markdown, import_files

    # Export
    md    = entity_to_markdown("Brian")          # str | None
    files = export_all()                         # {"Brian.md": "...", ...}

    # Import
    parsed = parse_markdown(content)             # dict (name, type, facts, relations)
    result = await import_files({"Brian.md": content, ...})
"""

import re
import time

import server as mem

# ── Regexes ────────────────────────────────────────────────────────────────────

# Matches: - [[other_name]] — rel_type  (em-dash, en-dash, or hyphen)
_RELATION_RE = re.compile(r'^\s*[-*]\s*\[\[([^\]]+)\]\]\s*[—–\-]+\s*(.+?)\s*$')

# Matches any bullet item: - text  or  * text
_BULLET_RE = re.compile(r'^\s*[-*]\s+(.+?)\s*$')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _iso(epoch: float) -> str:
    """Unix timestamp → ISO 8601 datetime string (local time)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


# ── Export ─────────────────────────────────────────────────────────────────────

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


# ── Import ─────────────────────────────────────────────────────────────────────

def parse_markdown(content: str) -> dict:
    """
    Parse an Obsidian-compatible Markdown string into structured data.

    Returns a dict::

        {
          "name":      str | None,   # from # H1 heading
          "type":      str,          # from frontmatter type:, default "person"
          "facts":     [{"fact": str, "category": str}, ...],
          "relations": [{"other_name": str, "rel_type": str}, ...],
        }

    The parser is lenient: missing frontmatter, missing sections, and unknown
    content are silently ignored.
    """
    name: str | None = None
    entity_type = "person"
    facts: list[dict] = []
    relations: list[dict] = []

    lines = content.splitlines()
    i = 0

    # ── Frontmatter (optional) ────────────────────────────────────────────────
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            if ":" in lines[i]:
                key, _, val = lines[i].partition(":")
                key = key.strip()
                val = val.strip()
                if key == "type" and val:
                    entity_type = val
            i += 1
        i += 1  # skip closing ---

    # ── Body ──────────────────────────────────────────────────────────────────
    current_section: str | None = None   # "observations" | "relations" | None
    current_category = "general"

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # H1 → entity name (first occurrence only)
        if stripped.startswith("# ") and name is None:
            name = stripped[2:].strip() or None

        # H2 section headers
        elif stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            if section == "observations":
                current_section = "observations"
                current_category = "general"
            elif section == "relations":
                current_section = "relations"
            else:
                current_section = None  # unknown section — ignore content below

        # H3 category sub-headings (observations only)
        elif stripped.startswith("### ") and current_section == "observations":
            current_category = stripped[4:].strip().lower() or "general"

        # Bullet items
        elif current_section == "observations":
            m = _BULLET_RE.match(line)
            if m:
                fact_text = m.group(1)
                # Skip the "no observations" placeholder emitted by export
                if fact_text and fact_text != "_No observations recorded yet._":
                    facts.append({"fact": fact_text, "category": current_category})

        elif current_section == "relations":
            m = _RELATION_RE.match(line)
            if m:
                relations.append({
                    "other_name": m.group(1).strip(),
                    "rel_type":   m.group(2).strip(),
                })

        i += 1

    return {
        "name":      name,
        "type":      entity_type,
        "facts":     facts,
        "relations": relations,
    }


async def import_files(files: dict[str, str]) -> dict:
    """
    Import entities from a ``{filename: markdown_content}`` mapping.

    Two-pass strategy:

    1. **Entities + memories** — each file is parsed, entity is created with
       the correct type from frontmatter, and memories are stored (deduped by
       exact fact text).
    2. **Relations** — all entities now exist with their correct types, so
       ``tool_relate`` can create directed edges without defaulting entity types.

    Returns::

        {
          "imported": {
            "Brian": {
              "status": "created" | "existing",
              "memories_added": int,
              "memories_skipped": int,
              "relations_added": int,
            },
            ...
          },
          "errors": [{"file": str, "error": str}, ...],
        }
    """
    errors: list[dict] = []
    parsed_by_name: dict[str, dict] = {}

    # ── Parse all files ───────────────────────────────────────────────────────
    for filename, content in files.items():
        parsed = parse_markdown(content)

        # Entity name: H1 heading → filename stem → skip
        name = parsed["name"]
        if not name:
            stem = filename[:-3] if filename.lower().endswith(".md") else filename
            name = stem.strip() or None
        if not name:
            errors.append({"file": filename, "error": "Could not determine entity name"})
            continue

        parsed["name"] = name
        parsed_by_name[name] = parsed

    results: dict[str, dict] = {}

    # ── Pass 1: entities + memories ───────────────────────────────────────────
    for name, parsed in parsed_by_name.items():
        try:
            result = await _import_entity_memories(name, parsed)
            results[name] = result
        except Exception as exc:
            errors.append({"file": f"{name}.md", "error": str(exc)})
            results[name] = {
                "status": "error",
                "memories_added": 0,
                "memories_skipped": 0,
                "relations_added": 0,
            }

    # ── Pass 2: relations ─────────────────────────────────────────────────────
    db = mem.get_db()
    for name, parsed in parsed_by_name.items():
        if results.get(name, {}).get("status") == "error":
            continue
        relations_added = 0
        for r in parsed["relations"]:
            try:
                # Skip if an active relation already exists (tool_relate always succeeds,
                # so we must guard here to report accurate relations_added counts).
                a = db.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
                b = db.execute("SELECT id FROM entities WHERE name=?", (r["other_name"],)).fetchone()
                if a and b:
                    already = db.execute(
                        """SELECT id FROM relations
                           WHERE entity_a=? AND entity_b=? AND rel_type=? AND valid_until IS NULL""",
                        (a["id"], b["id"], r["rel_type"]),
                    ).fetchone()
                    if already:
                        continue
                await mem.tool_relate(
                    entity_a=name,
                    entity_b=r["other_name"],
                    rel_type=r["rel_type"],
                )
                relations_added += 1
            except Exception:
                pass
        results[name]["relations_added"] = relations_added
    db.close()

    return {"imported": results, "errors": errors}


async def _import_entity_memories(name: str, parsed: dict) -> dict:
    """
    Ensure the entity exists (with the correct type) and store any new memories.
    Deduplicates by exact fact text — existing facts are counted in
    ``memories_skipped``, not re-inserted.
    """
    entity_type = parsed["type"]
    facts = parsed["facts"]

    # Fetch entity status and existing fact texts in one DB round-trip
    db = mem.get_db()
    try:
        e = db.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
        status = "existing" if e else "created"
        existing_facts: set[str] = set()
        if e:
            rows = db.execute(
                "SELECT fact FROM memories WHERE entity_id = ?", (e["id"],)
            ).fetchall()
            existing_facts = {r["fact"] for r in rows}
    finally:
        db.close()

    memories_added = 0
    memories_skipped = 0

    for f in facts:
        if f["fact"] in existing_facts:
            memories_skipped += 1
            continue
        # tool_remember creates the entity if it doesn't exist yet
        await mem.tool_remember(
            entity_name=name,
            fact=f["fact"],
            entity_type=entity_type,
            category=f["category"],
        )
        memories_added += 1
        existing_facts.add(f["fact"])  # guard against duplicates within this batch

    # If no facts were imported but entity is new, create it with tool_remember
    # using a minimal placeholder so the entity row exists for pass-2 relations.
    if status == "created" and memories_added == 0 and not facts:
        db = mem.get_db()
        try:
            e = db.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
            if not e:
                import time as _time
                now = _time.time()
                db.execute(
                    "INSERT INTO entities (name, type, meta, created, updated) VALUES (?,?,?,?,?)",
                    (name, entity_type, "{}", now, now),
                )
                db.commit()
        finally:
            db.close()

    return {
        "status":            status,
        "memories_added":    memories_added,
        "memories_skipped":  memories_skipped,
        "relations_added":   0,   # filled in by pass 2
    }
