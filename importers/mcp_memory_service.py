"""
importers/mcp_memory_service.py — Import from doobidoo/mcp-memory-service SQLite DB.

mcp-memory-service (https://github.com/doobidoo/mcp-memory-service) stores
memories in a SQLite database.  This importer reads it directly with sqlite3,
uses PRAGMA table_info() to discover the schema rather than assuming column names,
and re-embeds content via the local embedding model.

Security:
  - Validates db_path is an existing regular file before opening.
  - Reads SQLite magic bytes to confirm it is a SQLite database.
  - Handles OperationalError from a locked database gracefully (prints a clear
    suggestion to stop mcp-memory-service first).
  - Never executes user-supplied SQL.

Usage:
    from importers.mcp_memory_service import import_mcp_memory_service
    result = await import_mcp_memory_service(
        db_path="/home/user/.config/mcp-memory/memories.db",
        entity_name="imported",
    )
"""

import sqlite3
import time
from pathlib import Path

import server as mem
from importers.base import ImportResult, sanitize_fact, sanitize_name

_SOURCE_TAG   = "import:mcp-memory-service"
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Allowlist regex for table and column names — prevents SQL injection when
# discovered names are interpolated into PRAGMA / SELECT statements.
import re as _re
_IDENT_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_db_path(db_path: str) -> Path:
    """
    Verify that db_path refers to an existing regular file that is a SQLite DB.
    Raises ValueError with a descriptive message on any validation failure.
    """
    path = Path(db_path)

    if not path.exists():
        raise ValueError(f"File not found: {db_path!r}")
    if not path.is_file():
        raise ValueError(f"Not a regular file: {db_path!r}")
    if path.stat().st_size < 100:
        raise ValueError(f"File too small to be a SQLite database: {db_path!r}")

    # Check SQLite magic header bytes (first 16 bytes)
    try:
        with path.open("rb") as fh:
            magic = fh.read(16)
    except OSError as exc:
        raise ValueError(f"Cannot read file {db_path!r}: {exc}") from exc

    if magic != _SQLITE_MAGIC:
        raise ValueError(
            f"File does not appear to be a SQLite database: {db_path!r}"
        )

    return path


def _discover_content_column(cursor: sqlite3.Cursor, table: str) -> str | None:
    """
    Use PRAGMA table_info() to find the most likely 'content' column in the
    given table.  Tries known column names in priority order.

    Both `table` and any column name used in the returned SQL must match the
    identifier allowlist (_IDENT_RE) before being interpolated.
    """
    if not _IDENT_RE.match(table):
        raise ValueError(f"Unsafe table name from source database: {table!r}")
    cursor.execute(f"PRAGMA table_info({table})")     # noqa: S608 (validated above)
    cols = {row[1].lower() for row in cursor.fetchall()}

    for candidate in ("content", "memory", "observation", "text", "fact", "value"):
        if candidate in cols:
            return candidate
    return None


async def import_mcp_memory_service(
    db_path: str,
    entity_name: str = "imported",
    entity_type: str = "person",
    source_trust: int | None = None,
) -> ImportResult:
    """
    Import memories from a mcp-memory-service SQLite database.

    Parameters
    ----------
    db_path     : str
        Absolute path to the mcp-memory-service SQLite file.
    entity_name : str
        Name of the entity in memory-mcp to receive all imported memories
        (default "imported").
    entity_type : str
        Entity type for the target entity (default "person").
    """
    path = _validate_db_path(db_path)

    name = sanitize_name(entity_name)
    if not name:
        raise ValueError(f"Invalid entity_name: {entity_name!r}")

    trust = (
        max(mem.TRUST_EXTERNAL, min(mem.TRUST_USER, int(source_trust)))
        if source_trust is not None
        else mem.TRUST_DEFAULT_IMPORT
    )

    result = ImportResult()

    # ── Open source database ───────────────────────────────────────────────────
    try:
        src_db = sqlite3.connect(
            str(path),
            timeout=3.0,
            check_same_thread=False,
        )
        src_db.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            raise RuntimeError(
                "The mcp-memory-service database is locked. "
                "Stop mcp-memory-service before importing, then retry."
            ) from exc
        raise

    try:
        # Discover available tables
        tables = {
            row[0].lower()
            for row in src_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        mem_table = next(
            (t for t in ("memories", "memory", "items", "data") if t in tables),
            None,
        )
        if not mem_table:
            raise RuntimeError(
                f"No recognised memory table in {db_path!r}. "
                f"Found tables: {sorted(tables)}"
            )

        content_col = _discover_content_column(src_db.cursor(), mem_table)
        if not content_col:
            raise RuntimeError(
                f"Cannot identify content column in table '{mem_table}'. "
                f"No known column name found."
            )
        # Validate the discovered column name before interpolation
        if not _IDENT_RE.match(content_col):
            raise RuntimeError(
                f"Unsafe column name from source database: {content_col!r}"
            )

        # Read all rows (embedding blob column excluded automatically)
        rows = src_db.execute(
            f"SELECT {content_col} FROM {mem_table}"   # noqa: S608 (both validated above)
        ).fetchall()

    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            raise RuntimeError(
                "The mcp-memory-service database is locked. "
                "Stop mcp-memory-service before importing, then retry."
            ) from exc
        raise
    finally:
        src_db.close()

    if not rows:
        return result

    # ── Import into memory-mcp ─────────────────────────────────────────────────
    dst_db = mem.get_db()
    now    = time.time()

    eid = mem.upsert_entity(
        dst_db, name, entity_type, meta={"import_source": "mcp-memory-service"}
    )
    dst_db.commit()

    existing: set[str] = {
        row["fact"]
        for row in dst_db.execute(
            "SELECT fact FROM memories WHERE entity_id=?", (eid,)
        ).fetchall()
    }

    for row in rows:
        raw = row[0]
        fact = sanitize_fact(raw)
        if not fact:
            result.skipped += 1
            continue
        if fact in existing:
            result.skipped += 1
            continue

        vec = await mem.embed(fact)
        cur = dst_db.execute(
            """INSERT INTO memories
                   (entity_id, fact, category, confidence, source, source_trust, created, updated)
               VALUES (?, ?, 'general', 1.0, ?, ?, ?, ?)""",
            (eid, fact, _SOURCE_TAG, trust, now, now),
        )
        mid = cur.lastrowid
        dst_db.execute(
            "INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
            (mid, mem.vec_blob(vec)),
        )
        existing.add(fact)
        result.added += 1

    dst_db.commit()
    dst_db.close()
    return result
