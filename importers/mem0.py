"""
importers/mem0.py — Import memories from mem0 (cloud or self-hosted).

mem0 uses a flat memory model — all memories belong to a user_id with no entity
graph.  Each memory string is imported as a general observation on an entity
named after the user_id.

API reference:
    GET /v1/memories/?user_id={id}&page_size=50
    Auth: Authorization: Token <api_key>

Supports both cloud (https://api.mem0.ai) and self-hosted via base_url override.
Paginates until all memories are fetched or max_pages is reached.
Applies exponential backoff on HTTP 429 rate-limit responses.

Usage:
    from importers.mem0 import import_mem0
    result = await import_mem0(
        user_id="alice",
        api_key="m0-...",
        base_url="https://api.mem0.ai",  # or "http://localhost:8000"
    )
"""

import asyncio
import time
from urllib.parse import urlparse

import httpx

import server as mem
from importers.base import ImportResult, sanitize_fact, sanitize_name

_PAGE_SIZE  = 50
_MAX_PAGES  = 200          # upper bound: 200 × 50 = 10 000 memories
_SOURCE_TAG = "import:mem0"
_TIMEOUT    = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# Backoff config for 429 rate-limit responses
_BACKOFF_BASE    = 2.0     # seconds
_BACKOFF_FACTOR  = 2.0
_BACKOFF_MAX     = 60.0
_BACKOFF_RETRIES = 5


def _validate_base_url(url: str) -> str:
    """
    Validate base_url is a legitimate HTTP/HTTPS URL with a hostname.
    Raises ValueError for unsafe schemes (file://, ftp://, javascript:, etc.)
    or missing hostnames.
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        raise ValueError(f"Invalid base_url: {url!r}")

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"base_url scheme must be http or https, got {parsed.scheme!r}"
        )
    if not parsed.netloc:
        raise ValueError(f"base_url has no hostname: {url!r}")
    return url.rstrip("/")


async def import_mem0(
    user_id: str,
    api_key: str | None = None,
    base_url: str = "https://api.mem0.ai",
    agent_id: str | None = None,
    app_id: str | None = None,
    entity_type: str = "person",
    source_trust: int | None = None,
) -> ImportResult:
    """
    Paginate through all memories for user_id and import them into memory-mcp.

    Parameters
    ----------
    user_id     : str
        mem0 user identifier.  Used as the entity name in memory-mcp.
    api_key     : str, optional
        mem0 API key (required for cloud; omit or leave empty for unauthenticated
        self-hosted instances).
    base_url    : str
        API base URL.  Must be http:// or https://.  Defaults to mem0 cloud.
    agent_id    : str, optional
        Filter by agent_id (mem0 multi-agent setups).
    app_id      : str, optional
        Filter by app_id.
    entity_type : str
        memory-mcp entity type to assign to the user entity (default "person").
    """
    base_url = _validate_base_url(base_url)

    entity_name = sanitize_name(user_id)
    if not entity_name:
        raise ValueError(f"Invalid user_id: {user_id!r}")

    trust = (
        max(mem.TRUST_EXTERNAL, min(mem.TRUST_USER, int(source_trust)))
        if source_trust is not None
        else mem.TRUST_DEFAULT_IMPORT
    )

    result  = ImportResult()
    headers = {}
    if api_key:
        headers["Authorization"] = f"Token {api_key}"

    # Build initial URL
    params: dict = {"user_id": entity_name, "page_size": _PAGE_SIZE}
    if agent_id:
        params["agent_id"] = agent_id
    if app_id:
        params["app_id"] = app_id

    # Ensure entity exists in memory-mcp
    db  = mem.get_db()
    now = time.time()
    mem.upsert_entity(db, entity_name, entity_type, meta={"import_source": "mem0"})
    db.commit()

    # Pre-fetch existing facts for dedup
    eid = db.execute(
        "SELECT id FROM entities WHERE name=?", (entity_name,)
    ).fetchone()["id"]
    existing: set[str] = {
        row["fact"]
        for row in db.execute(
            "SELECT fact FROM memories WHERE entity_id=?", (eid,)
        ).fetchall()
    }

    async with httpx.AsyncClient(headers=headers, timeout=_TIMEOUT) as client:
        url: str | None = f"{base_url}/v1/memories/"
        page = 0

        while url and page < _MAX_PAGES:
            # Fetch one page (with exponential backoff on 429)
            response = await _fetch_with_backoff(
                client, url, params if page == 0 else None
            )

            try:
                data = response.json()
            except Exception:
                result.errors.append(
                    f"Page {page + 1}: response is not valid JSON"
                )
                break

            memories = data.get("results") or data.get("memories") or []
            if not isinstance(memories, list):
                result.errors.append(
                    f"Page {page + 1}: unexpected response shape — "
                    f"'results' key missing or not a list"
                )
                break

            for item in memories:
                if not isinstance(item, dict):
                    continue

                raw_text = item.get("memory") or item.get("content") or ""
                fact = sanitize_fact(raw_text)
                if not fact:
                    result.skipped += 1
                    continue
                if fact in existing:
                    result.skipped += 1
                    continue

                vec = await mem.embed(fact)
                cur = db.execute(
                    """INSERT INTO memories
                           (entity_id, fact, category, confidence,
                            source, source_trust, created, updated)
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

            # Follow pagination — validate next_url stays on the same host/scheme
            # to prevent server-side request forgery via a malicious API response.
            next_url = data.get("next")
            if next_url and isinstance(next_url, str):
                try:
                    parsed_next = urlparse(next_url)
                    parsed_base = urlparse(base_url)
                    if (
                        parsed_next.scheme in ("http", "https")
                        and parsed_next.netloc == parsed_base.netloc
                        and parsed_next.scheme == parsed_base.scheme
                    ):
                        url = next_url
                        params = {}   # params are baked into next_url
                    else:
                        truncated = str(next_url)[:80]
                        result.errors.append(
                            f"Page {page + 1}: 'next' URL redirects to a different "
                            f"host/scheme ({truncated!r}); stopping pagination."
                        )
                        url = None
                except Exception:
                    url = None
            else:
                url = None
            page += 1

    db.close()
    return result


async def _fetch_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None,
) -> httpx.Response:
    """GET url with exponential backoff on HTTP 429."""
    delay = _BACKOFF_BASE
    for attempt in range(_BACKOFF_RETRIES):
        try:
            response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise RuntimeError(f"HTTP request failed: {exc}") from exc

        if response.status_code == 429:
            # Respect Retry-After header if present
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            wait = min(wait, _BACKOFF_MAX)
            await asyncio.sleep(wait)
            delay = min(delay * _BACKOFF_FACTOR, _BACKOFF_MAX)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError(f"Exceeded {_BACKOFF_RETRIES} retries on rate-limited URL: {url}")
