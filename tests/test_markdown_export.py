"""
Tests for the Markdown export endpoints.

  GET /export/markdown/{entity_name}  — single entity as text/plain
  GET /export/markdown                — all entities as JSON { files: {...} }

Follows the api_auth + client fixture pattern from test_api.py.
"""

import pytest
from fastapi.testclient import TestClient

_TEST_TOKEN = "test-export-token-abc99"


@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── Helpers ───────────────────────────────────────────────────────────────────

def _remember(client, name, fact, entity_type="person", category="general"):
    r = client.post("/remember", json={
        "entity_name": name,
        "fact":        fact,
        "entity_type": entity_type,
        "category":    category,
    })
    assert r.status_code == 200, r.text


def _relate(client, a, b, rel_type):
    r = client.post("/relate", json={"entity_a": a, "entity_b": b, "rel_type": rel_type})
    assert r.status_code == 200, r.text


def _unrelate(client, a, b, rel_type):
    r = client.post("/unrelate", json={"entity_a": a, "entity_b": b, "rel_type": rel_type})
    assert r.status_code == 200, r.text


# ── GET /export/markdown/{entity_name} ────────────────────────────────────────

def test_export_single_returns_200(client):
    _remember(client, "Brian", "Works on OpenHome")
    r = client.get("/export/markdown/Brian")
    assert r.status_code == 200


def test_export_single_content_type(client):
    _remember(client, "Brian", "Works on OpenHome")
    r = client.get("/export/markdown/Brian")
    assert "text/plain" in r.headers["content-type"]


def test_export_single_content_disposition(client):
    _remember(client, "Brian", "Works on OpenHome")
    r = client.get("/export/markdown/Brian")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "Brian.md"   in cd


def test_export_single_has_frontmatter(client):
    _remember(client, "Brian", "Works on OpenHome")
    text = client.get("/export/markdown/Brian").text
    assert text.startswith("---\n")
    assert "type: person"        in text
    assert "created:"            in text
    assert "updated:"            in text
    assert "tags: [memory, auto]" in text
    assert text.count("---")    >= 2   # opening and closing ---


def test_export_single_has_h1_heading(client):
    _remember(client, "Brian", "Works on OpenHome")
    assert "\n# Brian\n" in client.get("/export/markdown/Brian").text


def test_export_single_has_observations_section(client):
    _remember(client, "Brian", "A fact")
    assert "## Observations" in client.get("/export/markdown/Brian").text


def test_export_single_includes_all_facts(client):
    _remember(client, "Brian", "Works on OpenHome",  category="general")
    _remember(client, "Brian", "Prefers dark roast", category="preference")
    text = client.get("/export/markdown/Brian").text
    assert "Works on OpenHome"  in text
    assert "Prefers dark roast" in text


def test_export_single_groups_by_category(client):
    _remember(client, "Brian", "Prefers coffee", category="preference")
    text = client.get("/export/markdown/Brian").text
    assert "### Preference" in text or "### preference" in text


def test_export_single_not_found_returns_404(client):
    r = client.get("/export/markdown/Nobody")
    assert r.status_code == 404


def test_export_single_includes_wikilinks(client):
    _remember(client, "Brian",        "Some fact")
    _remember(client, "homeassistant", "Smart home hub", entity_type="device")
    _relate(client, "Brian", "homeassistant", "controls")
    text = client.get("/export/markdown/Brian").text
    assert "## Relations"       in text
    assert "[[homeassistant]]"  in text
    assert "controls"           in text


def test_export_single_excludes_inactive_relations(client):
    _remember(client, "Brian", "Fact")
    _remember(client, "Alice", "Fact")
    _relate(client,   "Brian", "Alice", "friend")
    _unrelate(client, "Brian", "Alice", "friend")
    text = client.get("/export/markdown/Brian").text
    assert "## Relations" not in text
    assert "[[Alice]]"    not in text


def test_export_single_no_relations_section_when_empty(client):
    _remember(client, "Brian", "Fact")
    text = client.get("/export/markdown/Brian").text
    assert "## Relations" not in text


# ── GET /export/markdown ──────────────────────────────────────────────────────

def test_export_all_returns_200(client):
    assert client.get("/export/markdown").status_code == 200


def test_export_all_has_files_key(client):
    body = client.get("/export/markdown").json()
    assert "files" in body
    assert isinstance(body["files"], dict)


def test_export_all_empty_db(client):
    assert client.get("/export/markdown").json()["files"] == {}


def test_export_all_includes_all_entities(client):
    _remember(client, "Alice", "Fact A")
    _remember(client, "Bob",   "Fact B")
    files = client.get("/export/markdown").json()["files"]
    assert "Alice.md" in files
    assert "Bob.md"   in files


def test_export_all_filenames_end_with_md(client):
    _remember(client, "TestEntity", "Fact")
    files = client.get("/export/markdown").json()["files"]
    assert all(k.endswith(".md") for k in files)


def test_export_all_content_is_valid_markdown(client):
    _remember(client, "Alice", "Likes hiking")
    content = client.get("/export/markdown").json()["files"]["Alice.md"]
    assert content.startswith("---\n")
    assert "# Alice" in content
    assert "Likes hiking" in content


# ── Auth exemption ────────────────────────────────────────────────────────────

def test_export_single_accessible_without_auth(api_auth):
    """Export is auth-exempt so <a href> downloads work without JS fetch trickery."""
    import api
    with TestClient(api.app) as unauthenticated:
        # Need an entity — create one via the authenticated path first
        with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
            _remember(c, "Alice", "Fact")
        r = unauthenticated.get("/export/markdown/Alice")
        assert r.status_code == 200


def test_export_all_accessible_without_auth(client):
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.get("/export/markdown")
        assert r.status_code == 200
