"""
reembed.py — Re-embed all memories when swapping embedding models.

Usage:
    # 1. Set MEMORY_EMBED_MODEL and MEMORY_EMBED_DIM (env vars or edit server.py defaults)
    # 2. Pull the new model if using Ollama:  ollama pull <new-model>
    # 3. Run this script:  python reembed.py

The script re-embeds every row in memories using the current EMBED_MODEL,
rebuilds the memory_vectors virtual table with the correct dimension, and
leaves all other data untouched.

Supports --dry-run to preview without writing.

Environment variables (same as server.py):
    MEMORY_AI_BASE_URL  — AI backend base URL (default: http://localhost:11434/v1)
    MEMORY_AI_API_KEY   — API key / Bearer token (default: empty)
    MEMORY_EMBED_MODEL  — embedding model name (default: nomic-embed-text)
    MEMORY_EMBED_DIM    — embedding dimension  (default: 768)
"""

import argparse
import asyncio
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))

# Import config and helpers from server — single source of truth
from server import (
    DB_PATH,
    AI_BASE_URL,
    AI_API_KEY,
    EMBED_MODEL,
    EMBED_DIM,
    embed,
    vec_blob,
    get_db,
)


# ── Validation ─────────────────────────────────────────────────────────────────

async def validate_model() -> int:
    """Check backend is reachable and the model returns the expected dimension."""
    print(f"  Backend : {AI_BASE_URL}")
    print(f"  Model   : {EMBED_MODEL}")
    test_vec = await embed("dimension check")
    actual = len(test_vec)
    if actual != EMBED_DIM:
        raise ValueError(
            f"Model {EMBED_MODEL!r} returned {actual}-dim vectors "
            f"but MEMORY_EMBED_DIM={EMBED_DIM}. "
            f"Set MEMORY_EMBED_DIM={actual} and re-run."
        )
    print(f"  Dim OK  : {actual}")
    return actual


# ── Core re-embed logic ────────────────────────────────────────────────────────

async def reembed(dry_run: bool = False, batch_size: int = 50):
    """
    Re-embed all memories in batches.
    Drops and recreates memory_vectors to ensure a clean slate for the new dim.
    """
    print(f"\nReembed utility")
    print(f"  DB       : {DB_PATH}")
    print(f"  Dry run  : {dry_run}\n")

    # Validate model first — fail fast before touching the DB
    print("Validating model…")
    await validate_model()

    db = get_db()

    # Count memories
    total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    if total == 0:
        print("No memories found — nothing to do.")
        db.close()
        return

    print(f"\nFound {total} memories to re-embed.")

    if dry_run:
        print("\n[DRY RUN] Would drop and recreate memory_vectors, then re-embed all memories.")
        db.close()
        return

    # Drop and recreate the vector table (cleanest — avoids rowid conflicts,
    # handles dimension changes correctly)
    print("\nDropping memory_vectors…")
    db.execute("DROP TABLE IF EXISTS memory_vectors")
    db.execute(f"""
        CREATE VIRTUAL TABLE memory_vectors USING vec0(
            embedding FLOAT[{EMBED_DIM}]
        )
    """)
    db.commit()

    # Re-embed in batches
    offset = 0
    done   = 0
    errors = 0
    start  = time.time()

    print("Re-embedding…")
    while True:
        rows = db.execute(
            "SELECT id, fact FROM memories ORDER BY id LIMIT ? OFFSET ?",
            (batch_size, offset),
        ).fetchall()
        if not rows:
            break

        for row in rows:
            mid, fact = row["id"], row["fact"]
            try:
                vec = await embed(fact)
                db.execute(
                    "INSERT INTO memory_vectors(rowid, embedding) VALUES(?,?)",
                    (mid, vec_blob(vec)),
                )
                done += 1
            except Exception as e:
                print(f"\n  ERROR memory #{mid}: {e}")
                errors += 1

            pct = (done + errors) / total * 100
            print(f"\r  {done+errors}/{total} ({pct:.1f}%)  errors={errors}", end="", flush=True)

        db.commit()
        offset += batch_size

    elapsed = time.time() - start
    print(f"\n\nDone in {elapsed:.1f}s")
    print(f"  Re-embedded : {done}")
    print(f"  Errors      : {errors}")
    if errors:
        print("  WARNING: Some memories were not re-embedded. "
              "Their rowids are missing from memory_vectors — recall() will skip them.")

    db.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-embed all memories with the current EMBED_MODEL."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would happen without modifying the database."
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of memories to process per batch (default: 50)."
    )
    args = parser.parse_args()
    asyncio.run(reembed(dry_run=args.dry_run, batch_size=args.batch_size))
