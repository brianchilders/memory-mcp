"""
Tests for importers/mem0.py — import_mem0() and POST /import/mem0.

Uses httpx mock transport to avoid real network calls.
"""

import json

import pytest
from fastapi.testclient import TestClient

import server as mem
from importers.mem0 import import_mem0, _validate_base_url

_TEST_TOKEN = "test-mem0-token-abc88"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── URL validation tests (sync, no DB) ────────────────────────────────────────

def test_validate_base_url_https_ok():
    assert _validate_base_url("https://api.mem0.ai") == "https://api.mem0.ai"


def test_validate_base_url_http_ok():
    assert _validate_base_url("http://localhost:8000") == "http://localhost:8000"


def test_validate_base_url_trailing_slash_stripped():
    result = _validate_base_url("https://api.mem0.ai/")
    assert not result.endswith("/")


def test_validate_base_url_rejects_file_scheme():
    with pytest.raises(ValueError, match="http or https"):
        _validate_base_url("file:///etc/passwd")


def test_validate_base_url_rejects_ftp():
    with pytest.raises(ValueError, match="http or https"):
        _validate_base_url("ftp://example.com")


def test_validate_base_url_rejects_no_scheme():
    with pytest.raises(ValueError):
        _validate_base_url("api.mem0.ai")


def test_validate_base_url_rejects_empty():
    with pytest.raises(ValueError):
        _validate_base_url("")


# ── import_mem0() with mock HTTP transport ────────────────────────────────────

# ── import_mem0() with mock HTTP transport ────────────────────────────────────

@pytest.mark.asyncio
async def test_import_mem0_single_page(monkeypatch):
    """Mock a single page with two memories."""
    memories = [
        {"memory": "Prefers Python"},
        {"memory": "Works at Acme"},
    ]

    class FakeResponse:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self):
            return {"results": memories, "next": None}

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr("importers.mem0.httpx.AsyncClient", FakeClient)

    result = await import_mem0(user_id="alice_single", base_url="https://mock.mem0.ai")
    assert result.added   == 2
    assert result.skipped == 0
    assert result.errors  == []


@pytest.mark.asyncio
async def test_import_mem0_deduplication(monkeypatch):
    """Observations already in DB should be skipped."""
    # Pre-plant a memory for alice
    db  = mem.get_db()
    eid = mem.upsert_entity(db, "alice", "person")
    vec = await mem.embed("Prefers Python")
    cur = db.execute(
        "INSERT INTO memories(entity_id, fact, category, confidence, source, created, updated) "
        "VALUES (?, 'Prefers Python', 'general', 1.0, 'test', 0, 0)",
        (eid,),
    )
    mid = cur.lastrowid
    db.execute(
        "INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
        (mid, mem.vec_blob(vec)),
    )
    db.commit()
    db.close()

    memories = [{"memory": "Prefers Python"}, {"memory": "New fact"}]

    class FakeResponse:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self):
            return {"results": memories, "next": None}

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr("importers.mem0.httpx.AsyncClient", FakeClient)

    result = await import_mem0(user_id="alice", base_url="https://mock.mem0.ai")
    assert result.added   == 1
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_import_mem0_ssrf_next_url_rejected(monkeypatch):
    """'next' URL pointing to a different host must be rejected (SSRF prevention)."""
    call_count = 0

    class FakeResponse:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "results": [{"memory": "Page 1 fact"}],
                    "next": "https://evil.attacker.com/steal?data=1",
                }
            return {"results": [], "next": None}

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr("importers.mem0.httpx.AsyncClient", FakeClient)

    result = await import_mem0(user_id="ssrf_test_user", base_url="https://mock.mem0.ai")
    # Should have imported page 1's memory, then stopped at the bad next URL
    assert result.added == 1
    assert call_count == 1          # second request was never made
    assert any("different host" in e for e in result.errors)


@pytest.mark.asyncio
async def test_import_mem0_invalid_base_url():
    with pytest.raises(ValueError, match="http or https"):
        await import_mem0(user_id="alice", base_url="file:///etc/passwd")


@pytest.mark.asyncio
async def test_import_mem0_invalid_user_id():
    with pytest.raises(ValueError, match="user_id"):
        await import_mem0(user_id="", base_url="https://api.mem0.ai")


# ── POST /import/mem0 HTTP tests ──────────────────────────────────────────────

def test_http_import_mem0_rejects_file_scheme(client):
    r = client.post("/import/mem0", json={
        "user_id": "alice",
        "base_url": "file:///etc/passwd",
    })
    assert r.status_code == 400
    assert "http or https" in r.json()["detail"].lower()


def test_http_import_mem0_rejects_empty_user_id(client):
    r = client.post("/import/mem0", json={
        "user_id": "",
        "base_url": "https://api.mem0.ai",
    })
    assert r.status_code == 400


def test_http_import_mem0_requires_auth(api_auth):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.post("/import/mem0", json={
            "user_id": "alice",
            "base_url": "https://api.mem0.ai",
        })
        assert r.status_code == 401
