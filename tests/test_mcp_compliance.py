"""
tests/test_mcp_compliance.py — MCP protocol specification compliance tests.

Verifies that:
  - The server reports a valid MCP protocol version string (YYYY-MM-DD format).
  - The reported version matches what the installed mcp SDK advertises as
    LATEST_PROTOCOL_VERSION, so a version drift is immediately visible.
  - GET /mcp-info returns the expected shape and all required fields.
  - GET /health includes mcp_protocol_version.
  - The registered tool list is non-empty, all tools have names and descriptions,
    and names are unique — guarding against registration bugs.
  - The SDK and negotiated version strings are well-formed dates.
"""

import re

import pytest
from fastapi.testclient import TestClient

import mcp.types as mcp_types
from importlib.metadata import version as pkg_version

_TEST_TOKEN = "test-mcp-compliance-token"
_DATE_RE    = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── /mcp-info shape and content ───────────────────────────────────────────────

def test_mcp_info_returns_200(client):
    r = client.get("/mcp-info")
    assert r.status_code == 200


def test_mcp_info_required_keys(client):
    body = client.get("/mcp-info").json()
    required = {
        "mcp_sdk_version",
        "mcp_protocol_version",
        "mcp_default_negotiated_version",
        "tool_count",
        "tools",
    }
    assert required.issubset(body.keys()), f"Missing keys: {required - body.keys()}"


def test_mcp_info_protocol_version_is_date(client):
    body = client.get("/mcp-info").json()
    pv = body["mcp_protocol_version"]
    assert _DATE_RE.match(pv), f"protocol_version {pv!r} is not YYYY-MM-DD"


def test_mcp_info_default_negotiated_version_is_date(client):
    body = client.get("/mcp-info").json()
    dv = body["mcp_default_negotiated_version"]
    assert _DATE_RE.match(dv), f"default_negotiated_version {dv!r} is not YYYY-MM-DD"


def test_mcp_info_sdk_version_non_empty(client):
    body = client.get("/mcp-info").json()
    assert body["mcp_sdk_version"], "mcp_sdk_version must not be empty"


def test_mcp_info_protocol_version_matches_sdk(client):
    """Reported protocol version must match what the installed mcp SDK declares.

    This test fails immediately if the SDK is upgraded to a new spec version
    without the server being validated against it — acting as a drift detector.
    """
    body     = client.get("/mcp-info").json()
    expected = mcp_types.LATEST_PROTOCOL_VERSION
    assert body["mcp_protocol_version"] == expected, (
        f"Server reports {body['mcp_protocol_version']!r} "
        f"but SDK LATEST_PROTOCOL_VERSION is {expected!r}"
    )


def test_mcp_info_sdk_version_matches_installed_package(client):
    """Reported SDK version must match the installed mcp package."""
    body     = client.get("/mcp-info").json()
    expected = pkg_version("mcp")
    assert body["mcp_sdk_version"] == expected, (
        f"Server reports SDK {body['mcp_sdk_version']!r} "
        f"but installed package is {expected!r}"
    )


def test_mcp_info_protocol_version_gte_negotiated(client):
    """LATEST_PROTOCOL_VERSION must be >= DEFAULT_NEGOTIATED_VERSION (both YYYY-MM-DD)."""
    body = client.get("/mcp-info").json()
    assert body["mcp_protocol_version"] >= body["mcp_default_negotiated_version"], (
        "LATEST_PROTOCOL_VERSION must not be older than DEFAULT_NEGOTIATED_VERSION"
    )


# ── Tool registry ─────────────────────────────────────────────────────────────

def test_mcp_info_tool_list_non_empty(client):
    body = client.get("/mcp-info").json()
    assert body["tool_count"] > 0
    assert len(body["tools"]) > 0


def test_mcp_info_tool_count_matches_list(client):
    body = client.get("/mcp-info").json()
    assert body["tool_count"] == len(body["tools"])


def test_mcp_info_all_tools_have_names(client):
    body  = client.get("/mcp-info").json()
    names = [t["name"] for t in body["tools"]]
    assert all(names), "Every tool must have a non-empty name"


def test_mcp_info_all_tools_have_descriptions(client):
    body  = client.get("/mcp-info").json()
    empty = [t["name"] for t in body["tools"] if not t.get("description")]
    assert not empty, f"Tools without descriptions: {empty}"


def test_mcp_info_tool_names_are_unique(client):
    body  = client.get("/mcp-info").json()
    names = [t["name"] for t in body["tools"]]
    assert len(names) == len(set(names)), f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"


def test_mcp_info_core_tools_registered(client):
    """Core tools that every memory-mcp deployment must expose."""
    body  = client.get("/mcp-info").json()
    names = {t["name"] for t in body["tools"]}
    for required in ("remember", "recall", "get_context", "forget", "relate"):
        assert required in names, f"Core tool {required!r} is not registered"


def test_mcp_info_requires_auth(api_auth):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.get("/mcp-info")
        assert r.status_code == 401


# ── /health includes mcp_protocol_version ─────────────────────────────────────

def test_health_includes_mcp_protocol_version(client):
    body = client.get("/health").json()
    assert "mcp_protocol_version" in body


def test_health_mcp_protocol_version_is_date(client):
    body = client.get("/health").json()
    pv   = body["mcp_protocol_version"]
    assert _DATE_RE.match(pv), f"health mcp_protocol_version {pv!r} is not YYYY-MM-DD"


def test_health_mcp_protocol_version_matches_sdk(client):
    body     = client.get("/health").json()
    expected = mcp_types.LATEST_PROTOCOL_VERSION
    assert body["mcp_protocol_version"] == expected
