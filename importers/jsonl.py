"""
importers/jsonl.py — Import from the Anthropic official MCP Memory JSONL format.

Every Claude Desktop / Cursor / VS Code user with the official
@modelcontextprotocol/server-memory server has a file in this format.

Format (one JSON object per line — it is JSONL despite sometimes being named .json):
    {"type": "entity",   "name": "John_Smith", "entityType": "person",
     "observations": ["Speaks fluent Spanish", "Graduated in 2019"]}
    {"type": "relation", "from": "John_Smith",  "to": "Acme_Corp",
     "relationType": "works_at"}

Deduplication: observations that already exist (exact text match) are skipped.
All imported records are tagged source='import:jsonl' for traceability.

Usage:
    from importers.jsonl import import_jsonl
    result = await import_jsonl(content)   # content = raw JSONL string
"""

import json
import time

import server as mem
from importers.base import (
    ImportResult,
    sanitize_fact,
    sanitize_name,
    sanitize_rel_type,
)

_MAX_CONTENT_BYTES = 5 * 1024 * 1024   # 5 MB hard cap
_MAX_LINES         = 10_000             # truncate after this many lines
_SOURCE_TAG        = "import:jsonl"


async def import_jsonl(
    content: str,
    source_trust: int | None = None,
) -> ImportResult:
    """
    Parse and import JSONL content into memory-mcp.

    Two-pass strategy:
        Pass 1 — entities + observations (ensures all entities exist for pass 2)
        Pass 2 — relations

    Security:
        - Content size is capped at 5 MB before any parsing.
        - Line count is capped at 10 000 to prevent memory exhaustion.
        - All entity names and fact strings are sanitized (length + non-empty).
        - No file-system access — content is passed as a string, never as a path.
    """
    if len(content.encode()) > _MAX_CONTENT_BYTES:
        raise ValueError(
            f"JSONL content exceeds {_MAX_CONTENT_BYTES // 1024 // 1024} MB limit"
        )

    trust = (
        max(mem.TRUST_EXTERNAL, min(mem.TRUST_USER, int(source_trust)))
        if source_trust is not None
        else mem.TRUST_DEFAULT_IMPORT
    )

    result   = ImportResult()
    entities: list[tuple[int, dict]] = []
    relations: list[tuple[int, dict]] = []

    # ── Parse all lines ────────────────────────────────────────────────────────
    for lineno, line in enumerate(content.splitlines(), start=1):
        if lineno > _MAX_LINES:
            result.errors.append(
                f"Truncated: only the first {_MAX_LINES} lines were processed"
            )
            break

        line = line.strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            result.errors.append(f"Line {lineno}: invalid JSON — {exc}")
            continue

        if not isinstance(obj, dict):
            result.errors.append(
                f"Line {lineno}: expected a JSON object, got {type(obj).__name__}"
            )
            continue

        record_type = obj.get("type", "")
        if record_type == "entity":
            entities.append((lineno, obj))
        elif record_type == "relation":
            relations.append((lineno, obj))
        # Unknown types are silently skipped for forward-compatibility

    # ── Pass 1: entities + observations ───────────────────────────────────────
    db  = mem.get_db()
    now = time.time()

    for lineno, obj in entities:
        name = sanitize_name(obj.get("name"))
        if not name:
            result.errors.append(f"Line {lineno}: empty or invalid entity name")
            continue

        entity_type = (
            str(obj.get("entityType") or "person").strip().lower() or "person"
        )
        eid = mem.upsert_entity(db, name, entity_type, meta={"import_source": "jsonl"})

        observations = obj.get("observations") or []
        if not isinstance(observations, list):
            result.errors.append(
                f"Line {lineno}: 'observations' must be a JSON array"
            )
            continue

        # Pre-fetch existing fact strings for this entity to avoid duplicates
        existing: set[str] = {
            row["fact"]
            for row in db.execute(
                "SELECT fact FROM memories WHERE entity_id=?", (eid,)
            ).fetchall()
        }

        for obs in observations:
            if not isinstance(obs, str):
                continue
            fact = sanitize_fact(obs)
            if not fact:
                continue
            if fact in existing:
                result.skipped += 1
                continue

            vec = await mem.embed(fact)
            cur = db.execute(
                """INSERT INTO memories
                       (entity_id, fact, category, confidence, source, source_trust, created, updated)
                   VALUES (?, ?, 'general', 1.0, ?, ?, ?, ?)""",
                (eid, fact, _SOURCE_TAG, trust, now, now),
            )
            mid = cur.lastrowid
            db.execute(
                "INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
                (mid, mem.vec_blob(vec)),
            )
            existing.add(fact)
            result.added += 1

    db.commit()

    # ── Pass 2: relations ──────────────────────────────────────────────────────
    for lineno, obj in relations:
        from_name = sanitize_name(obj.get("from"))
        to_name   = sanitize_name(obj.get("to"))
        rel_type  = sanitize_rel_type(obj.get("relationType"))

        if not from_name or not to_name or not rel_type:
            result.errors.append(
                f"Line {lineno}: relation missing or invalid from/to/relationType"
            )
            continue

        try:
            a_row = db.execute(
                "SELECT id FROM entities WHERE name=?", (from_name,)
            ).fetchone()
            b_row = db.execute(
                "SELECT id FROM entities WHERE name=?", (to_name,)
            ).fetchone()

            # Create stub entities for names referenced in relations but not declared
            a_id = a_row["id"] if a_row else mem.upsert_entity(db, from_name, "person")
            b_id = b_row["id"] if b_row else mem.upsert_entity(db, to_name,   "person")

            # Skip if an active relation already exists
            already = db.execute(
                """SELECT id FROM relations
                   WHERE entity_a=? AND entity_b=? AND rel_type=? AND valid_until IS NULL""",
                (a_id, b_id, rel_type),
            ).fetchone()
            if not already:
                db.execute(
                    """INSERT INTO relations
                           (entity_a, entity_b, rel_type, meta, created, valid_from, valid_until)
                       VALUES (?, ?, ?, '{}', ?, ?, NULL)""",
                    (a_id, b_id, rel_type, now, now),
                )
        except Exception as exc:
            result.errors.append(f"Line {lineno}: relation error — {exc}")

    db.commit()
    db.close()
    return result
