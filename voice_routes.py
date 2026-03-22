"""
voice_routes.py — Speaker identity management for memory-mcp.

Handles the full lifecycle of voiceprint-based speaker enrollment:
listing unknown provisional voices, enrolling them under a real name,
merging duplicates, and updating voiceprint embeddings.

Voiceprints are stored in the existing entity meta JSON column — no schema changes required.

Convention for provisional entities (created by the pipeline worker):
  entity.name   = "unknown_voice_{8-char-hash}"
  entity.type   = "person"
  entity.meta   = {
    "voiceprint": [...],          # 256-dim float list
    "voiceprint_samples": N,      # utterances averaged into this embedding
    "status": "unenrolled",
    "first_seen": "<ISO timestamp>",
    "first_seen_room": "<room name>",
    "detection_count": N
  }

Wire into api.py with:
    from voice_routes import router as voice_router
    app.include_router(voice_router)
"""

import json
import math
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import server as mem

router = APIRouter(prefix="/voices", tags=["voices"])


# ── Request models ─────────────────────────────────────────────────────────────

class EnrollRequest(BaseModel):
    entity_name: str
    new_name: str
    display_name: str | None = None


class MergeRequest(BaseModel):
    source_name: str
    target_name: str


class UpdatePrintRequest(BaseModel):
    entity_name: str
    embedding: list[float]
    weight: float = Field(default=0.1, ge=0.0, le=1.0)

    @field_validator("embedding")
    @classmethod
    def embedding_must_be_finite(cls, v: list[float]) -> list[float]:
        if any(not math.isfinite(x) for x in v):
            raise ValueError("Embedding values must be finite floats (no NaN or Infinity)")
        return v


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _embedding_norm(vec: list[float]) -> float:
    return round(sum(x * x for x in vec) ** 0.5, 4)


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/unknown")
async def list_unknown(limit: int = 20, min_detections: int = 1):
    """List all provisional (unenrolled) speaker entities with a sample transcript."""
    db = mem.get_db()
    try:
        rows = db.execute(
            """
            SELECT id, name,
                   json_extract(meta, '$.first_seen')      AS first_seen,
                   json_extract(meta, '$.first_seen_room') AS first_seen_room,
                   CAST(json_extract(meta, '$.detection_count') AS INTEGER) AS detection_count
            FROM entities
            WHERE type = 'person'
              AND json_extract(meta, '$.status') = 'unenrolled'
              AND CAST(json_extract(meta, '$.detection_count') AS INTEGER) >= ?
            ORDER BY detection_count DESC
            LIMIT ?
            """,
            (min_detections, limit),
        ).fetchall()

        results = []
        for row in rows:
            # Fetch last_seen and most recent transcript from voice_activity readings
            reading = db.execute(
                """
                SELECT MAX(ts) AS last_seen,
                       json_extract(value_json, '$.transcript') AS transcript
                FROM readings
                WHERE entity_id = ? AND metric = 'voice_activity'
                """,
                (row["id"],),
            ).fetchone()

            results.append({
                "entity_name": row["name"],
                "first_seen": row["first_seen"],
                "first_seen_room": row["first_seen_room"],
                "detection_count": row["detection_count"],
                "last_seen": reading["last_seen"] if reading else None,
                "sample_transcript": reading["transcript"] if reading else None,
            })

        return {"result": results, "ok": True}
    finally:
        db.close()


@router.post("/enroll")
async def enroll(req: EnrollRequest):
    """Rename a provisional entity to a real person's name and mark as enrolled."""
    db = mem.get_db()
    try:
        source = db.execute(
            "SELECT id FROM entities WHERE name = ?", (req.entity_name,)
        ).fetchone()
        if not source:
            raise HTTPException(status_code=404, detail=f"Entity '{req.entity_name}' not found")

        conflict = db.execute(
            "SELECT id FROM entities WHERE name = ?", (req.new_name,)
        ).fetchone()
        if conflict:
            raise HTTPException(
                status_code=409, detail=f"Entity '{req.new_name}' already exists"
            )

        # Count attached data — informational only, they stay in place via FK on entity_id
        n_memories = db.execute(
            "SELECT COUNT(*) FROM memories WHERE entity_id = ?", (source["id"],)
        ).fetchone()[0]
        n_readings = db.execute(
            "SELECT COUNT(*) FROM readings WHERE entity_id = ?", (source["id"],)
        ).fetchone()[0]

        now = time.time()
        if req.display_name is not None:
            db.execute(
                """UPDATE entities
                   SET name = ?, updated = ?,
                       meta = json_set(meta, '$.status', 'enrolled', '$.display_name', ?)
                   WHERE id = ?""",
                (req.new_name, now, req.display_name, source["id"]),
            )
        else:
            db.execute(
                """UPDATE entities
                   SET name = ?, updated = ?,
                       meta = json_set(meta, '$.status', 'enrolled')
                   WHERE id = ?""",
                (req.new_name, now, source["id"]),
            )
        db.commit()

        return {
            "result": {
                "entity_id": source["id"],
                "entity_name": req.new_name,
                "previous_name": req.entity_name,
                "memories_transferred": n_memories,
                "readings_transferred": n_readings,
            },
            "ok": True,
        }
    finally:
        db.close()


@router.post("/merge")
async def merge(req: MergeRequest):
    """
    Merge a provisional entity into an enrolled entity.

    Transfers all memories, readings, and active relations to the target,
    averages voiceprint embeddings weighted by sample count, then deletes
    the source entity. Runs in a single transaction.

    Relations that cannot be transferred due to a UNIQUE constraint conflict
    (target already has an identical relation) are skipped — they are deleted
    by CASCADE when the source entity is removed.
    """
    if req.source_name == req.target_name:
        raise HTTPException(status_code=400, detail="source_name and target_name must be different")

    db = mem.get_db()
    try:
        source = db.execute(
            "SELECT id, meta FROM entities WHERE name = ?", (req.source_name,)
        ).fetchone()
        if not source:
            raise HTTPException(status_code=404, detail=f"Entity '{req.source_name}' not found")

        target = db.execute(
            "SELECT id, meta FROM entities WHERE name = ?", (req.target_name,)
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail=f"Entity '{req.target_name}' not found")

        src_id = source["id"]
        tgt_id = target["id"]
        src_meta = json.loads(source["meta"])
        tgt_meta = json.loads(target["meta"])

        # Count what we're moving (for response — query before transfer)
        n_memories = db.execute(
            "SELECT COUNT(*) FROM memories WHERE entity_id = ?", (src_id,)
        ).fetchone()[0]
        n_readings = db.execute(
            "SELECT COUNT(*) FROM readings WHERE entity_id = ?", (src_id,)
        ).fetchone()[0]
        n_relations = db.execute(
            """SELECT COUNT(*) FROM relations
               WHERE (entity_a = ? OR entity_b = ?) AND valid_until IS NULL""",
            (src_id, src_id),
        ).fetchone()[0]

        # Transfer Tier 1 and Tier 2 data
        db.execute(
            "UPDATE memories SET entity_id = ? WHERE entity_id = ?", (tgt_id, src_id)
        )
        db.execute(
            "UPDATE readings SET entity_id = ? WHERE entity_id = ?", (tgt_id, src_id)
        )

        # Transfer active relations — UPDATE OR IGNORE silently skips UNIQUE conflicts
        db.execute(
            """UPDATE OR IGNORE relations
               SET entity_a = ? WHERE entity_a = ? AND valid_until IS NULL""",
            (tgt_id, src_id),
        )
        db.execute(
            """UPDATE OR IGNORE relations
               SET entity_b = ? WHERE entity_b = ? AND valid_until IS NULL""",
            (tgt_id, src_id),
        )

        # Merge voiceprints — weighted average by sample count
        src_vp = src_meta.get("voiceprint")
        tgt_vp = tgt_meta.get("voiceprint")

        if src_vp and tgt_vp:
            src_n = src_meta.get("voiceprint_samples", 1)
            tgt_n = tgt_meta.get("voiceprint_samples", 1)
            total = src_n + tgt_n
            merged_vp = _normalize(
                [(v * tgt_n + u * src_n) / total for v, u in zip(tgt_vp, src_vp)]
            )
            tgt_meta["voiceprint"] = merged_vp
            tgt_meta["voiceprint_samples"] = total
        elif src_vp and not tgt_vp:
            tgt_meta["voiceprint"] = src_vp
            tgt_meta["voiceprint_samples"] = src_meta.get("voiceprint_samples", 1)

        tgt_meta["detection_count"] = (
            tgt_meta.get("detection_count", 0) + src_meta.get("detection_count", 0)
        )

        now = time.time()
        db.execute(
            "UPDATE entities SET meta = ?, updated = ? WHERE id = ?",
            (json.dumps(tgt_meta), now, tgt_id),
        )

        # Delete source — ON DELETE CASCADE removes any relations that couldn't be transferred
        db.execute("DELETE FROM entities WHERE id = ?", (src_id,))
        db.commit()

        return {
            "result": {
                "target_name": req.target_name,
                "memories_merged": n_memories,
                "readings_merged": n_readings,
                "relations_merged": n_relations,
                "source_deleted": req.source_name,
            },
            "ok": True,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/update_print")
async def update_print(req: UpdatePrintRequest):
    """
    Update the voiceprint embedding for an entity using a running weighted average.

    Called by the pipeline worker after each confident speaker identification
    to refine the embedding over time. The new embedding is re-normalized after
    blending so it remains a unit vector.
    """
    if len(req.embedding) != 256:
        raise HTTPException(
            status_code=422,
            detail=f"Embedding must be 256-dimensional, got {len(req.embedding)}",
        )

    db = mem.get_db()
    try:
        entity = db.execute(
            "SELECT id, meta FROM entities WHERE name = ?", (req.entity_name,)
        ).fetchone()
        if not entity:
            raise HTTPException(
                status_code=404, detail=f"Entity '{req.entity_name}' not found"
            )

        meta = json.loads(entity["meta"])
        existing_vp = meta.get("voiceprint")
        existing_samples = meta.get("voiceprint_samples", 0)

        if not existing_vp:
            new_vp = list(req.embedding)
            new_samples = 1
        else:
            w = req.weight
            blended = [(1.0 - w) * v + w * u for v, u in zip(existing_vp, req.embedding)]
            new_vp = _normalize(blended)
            new_samples = existing_samples + 1

        meta["voiceprint"] = new_vp
        meta["voiceprint_samples"] = new_samples

        now = time.time()
        db.execute(
            "UPDATE entities SET meta = ?, updated = ? WHERE id = ?",
            (json.dumps(meta), now, entity["id"]),
        )
        db.commit()

        return {
            "result": {
                "entity_name": req.entity_name,
                "voiceprint_samples": new_samples,
                "embedding_norm": _embedding_norm(new_vp),
            },
            "ok": True,
        }
    finally:
        db.close()
