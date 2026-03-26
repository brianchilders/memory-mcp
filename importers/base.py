"""
importers/base.py — Shared result type and validation helpers for all importers.

Every importer returns an ImportResult.  Validation helpers enforce uniform
constraints on entity names and fact strings across all import sources.
"""

from dataclasses import dataclass, field

# Hard limits applied uniformly across all importers
MAX_ENTITY_NAME = 500
MAX_FACT_LEN    = 10_000
MAX_REL_TYPE    = 200


@dataclass
class ImportResult:
    """Unified outcome type returned by every importer."""
    added:   int = 0
    skipped: int = 0
    errors:  list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"added": self.added, "skipped": self.skipped, "errors": self.errors}


def sanitize_name(raw: object) -> str | None:
    """
    Coerce to str, strip whitespace, validate non-empty and within MAX_ENTITY_NAME.
    Returns None when the name is not usable (caller should skip/log the record).
    """
    if raw is None:
        return None
    name = str(raw).strip()
    if not name or len(name) > MAX_ENTITY_NAME:
        return None
    return name


def sanitize_fact(raw: object) -> str | None:
    """
    Coerce to str, strip whitespace, validate non-empty and within MAX_FACT_LEN.
    Returns None when unusable.
    """
    if raw is None:
        return None
    fact = str(raw).strip()
    if not fact or len(fact) > MAX_FACT_LEN:
        return None
    return fact


def sanitize_rel_type(raw: object) -> str | None:
    """Coerce, strip, validate a relation type string."""
    if raw is None:
        return None
    rel = str(raw).strip()[:MAX_REL_TYPE]
    return rel if rel else None
