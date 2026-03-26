"""
Tests for Markdown import — parse_markdown() and POST /import/markdown.

  parse_markdown()        — pure parser, no DB, tests in isolation
  POST /import/markdown   — full round-trip via HTTP client

Follows the api_auth + client fixture pattern from test_api.py.
"""

import pytest
from fastapi.testclient import TestClient

from exporters.markdown import parse_markdown

_TEST_TOKEN = "test-import-token-abc77"


@pytest.fixture
def api_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_API_TOKEN", _TEST_TOKEN)
    return _TEST_TOKEN


@pytest.fixture
def client(api_auth):
    import api
    with TestClient(api.app, headers={"Authorization": f"Bearer {api_auth}"}) as c:
        yield c


# ── parse_markdown — unit tests (no DB, no HTTP) ──────────────────────────────

def test_parse_name_from_h1():
    md = "# Alice\n\n## Observations\n\n- A fact\n"
    assert parse_markdown(md)["name"] == "Alice"


def test_parse_name_missing_returns_none():
    md = "## Observations\n\n- A fact\n"
    assert parse_markdown(md)["name"] is None


def test_parse_type_from_frontmatter():
    md = "---\ntype: device\n---\n\n# Thermostat\n"
    assert parse_markdown(md)["type"] == "device"


def test_parse_type_defaults_to_person():
    md = "# Alice\n\n## Observations\n\n- A fact\n"
    assert parse_markdown(md)["type"] == "person"


def test_parse_facts_basic():
    md = "# Alice\n\n## Observations\n\n- Likes hiking\n- Drinks coffee\n"
    facts = parse_markdown(md)["facts"]
    assert len(facts) == 2
    assert facts[0]["fact"] == "Likes hiking"
    assert facts[1]["fact"] == "Drinks coffee"


def test_parse_facts_category_from_subheading():
    md = (
        "# Alice\n\n"
        "## Observations\n\n"
        "### Preference\n\n"
        "- Prefers tea\n\n"
        "### General\n\n"
        "- Works at a university\n"
    )
    facts = parse_markdown(md)["facts"]
    assert facts[0] == {"fact": "Prefers tea",           "category": "preference"}
    assert facts[1] == {"fact": "Works at a university", "category": "general"}


def test_parse_facts_default_category_is_general():
    md = "# Alice\n\n## Observations\n\n- A fact with no heading above\n"
    assert parse_markdown(md)["facts"][0]["category"] == "general"


def test_parse_facts_skips_empty_placeholder():
    md = "# Alice\n\n## Observations\n\n_No observations recorded yet._\n"
    assert parse_markdown(md)["facts"] == []


def test_parse_relations_em_dash():
    md = "# Alice\n\n## Relations\n\n- [[Bob]] — friend\n"
    rels = parse_markdown(md)["relations"]
    assert rels == [{"other_name": "Bob", "rel_type": "friend"}]


def test_parse_relations_en_dash():
    md = "# Alice\n\n## Relations\n\n- [[Bob]] – colleague\n"
    rels = parse_markdown(md)["relations"]
    assert rels == [{"other_name": "Bob", "rel_type": "colleague"}]


def test_parse_relations_hyphen():
    md = "# Alice\n\n## Relations\n\n- [[Bob]] - spouse\n"
    rels = parse_markdown(md)["relations"]
    assert rels == [{"other_name": "Bob", "rel_type": "spouse"}]


def test_parse_multiple_relations():
    md = (
        "# Alice\n\n"
        "## Relations\n\n"
        "- [[Bob]] — friend\n"
        "- [[homeassistant]] — controls\n"
    )
    rels = parse_markdown(md)["relations"]
    assert len(rels) == 2


def test_parse_no_frontmatter_ok():
    md = "# Alice\n\n## Observations\n\n- A fact\n"
    result = parse_markdown(md)
    assert result["name"] == "Alice"
    assert result["type"] == "person"
    assert len(result["facts"]) == 1


def test_parse_roundtrip_with_exported_format():
    """parse_markdown must handle exactly the format entity_to_markdown emits."""
    from exporters.markdown import entity_to_markdown
    # We can't call entity_to_markdown without a DB, so build the format manually
    md = (
        "---\n"
        "type: person\n"
        "created: 2026-01-01T00:00:00\n"
        "updated: 2026-03-25T12:00:00\n"
        "tags: [memory, auto]\n"
        "---\n\n"
        "# Brian\n\n"
        "## Observations\n\n"
        "### Preference\n\n"
        "- Prefers dark roast\n\n"
        "### General\n\n"
        "- Works on OpenHome\n\n"
        "## Relations\n\n"
        "- [[homeassistant]] — controls\n"
    )
    result = parse_markdown(md)
    assert result["name"] == "Brian"
    assert result["type"] == "person"
    assert {"fact": "Prefers dark roast", "category": "preference"} in result["facts"]
    assert {"fact": "Works on OpenHome",  "category": "general"}    in result["facts"]
    assert {"other_name": "homeassistant", "rel_type": "controls"}  in result["relations"]


# ── POST /import/markdown — HTTP integration tests ────────────────────────────

def _export_entity(client, name):
    return client.get(f"/export/markdown/{name}").text


def _remember(client, name, fact, entity_type="person", category="general"):
    r = client.post("/remember", json={
        "entity_name": name,
        "fact":        fact,
        "entity_type": entity_type,
        "category":    category,
    })
    assert r.status_code == 200, r.text


def _import(client, files: dict[str, str]):
    r = client.post("/import/markdown", json={"files": files})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_empty_files_ok(client):
    result = _import(client, {})
    assert result["ok"] is True
    assert result["imported"] == {}
    assert result["errors"] == []


def test_import_creates_entity(client):
    md = "# Alice\n\n## Observations\n\n- Likes hiking\n"
    _import(client, {"Alice.md": md})
    r = client.get("/entities")
    names = [e["name"] for e in r.json()["entities"]]
    assert "Alice" in names


def test_import_stores_memories(client):
    md = "# Alice\n\n## Observations\n\n- Likes hiking\n- Drinks coffee\n"
    _import(client, {"Alice.md": md})
    r = client.get("/profile/Alice")
    profile_text = r.json()["result"]
    assert "Likes hiking"  in profile_text
    assert "Drinks coffee" in profile_text


def test_import_result_status_created(client):
    md = "# Alice\n\n## Observations\n\n- A fact\n"
    result = _import(client, {"Alice.md": md})
    assert result["imported"]["Alice"]["status"]         == "created"
    assert result["imported"]["Alice"]["memories_added"] == 1


def test_import_result_status_existing(client):
    _remember(client, "Alice", "Pre-existing fact")
    md = "# Alice\n\n## Observations\n\n- New fact\n"
    result = _import(client, {"Alice.md": md})
    assert result["imported"]["Alice"]["status"]         == "existing"
    assert result["imported"]["Alice"]["memories_added"] == 1


def test_import_idempotent_memories(client):
    md = "# Alice\n\n## Observations\n\n- Likes hiking\n"
    _import(client, {"Alice.md": md})
    result = _import(client, {"Alice.md": md})
    assert result["imported"]["Alice"]["memories_added"]   == 0
    assert result["imported"]["Alice"]["memories_skipped"] == 1


def test_import_entity_type_from_frontmatter(client):
    md = "---\ntype: device\n---\n\n# Thermostat\n\n## Observations\n\n- Controls HVAC\n"
    _import(client, {"Thermostat.md": md})
    r = client.get("/entities")
    entity = next(e for e in r.json()["entities"] if e["name"] == "Thermostat")
    assert entity["type"] == "device"


def test_import_name_from_filename_when_no_h1(client):
    md = "## Observations\n\n- A fact with no heading\n"
    result = _import(client, {"FallbackName.md": md})
    assert "FallbackName" in result["imported"]
    r = client.get("/entities")
    names = [e["name"] for e in r.json()["entities"]]
    assert "FallbackName" in names


def test_import_creates_relations(client):
    alice_md = (
        "# Alice\n\n"
        "## Observations\n\n- A fact\n\n"
        "## Relations\n\n- [[Bob]] — friend\n"
    )
    bob_md = "# Bob\n\n## Observations\n\n- Another fact\n"
    result = _import(client, {"Alice.md": alice_md, "Bob.md": bob_md})
    assert result["imported"]["Alice"]["relations_added"] == 1


def test_import_relation_idempotent(client):
    md = (
        "# Alice\n\n"
        "## Observations\n\n- A fact\n\n"
        "## Relations\n\n- [[Bob]] — friend\n"
    )
    bob_md = "# Bob\n\n## Observations\n\n- Fact\n"
    _import(client, {"Alice.md": md, "Bob.md": bob_md})
    result = _import(client, {"Alice.md": md, "Bob.md": bob_md})
    # Re-import: relation already exists — no new one created, no crash
    assert result["imported"]["Alice"]["relations_added"] == 0


def test_import_multiple_files(client):
    alice_md = "# Alice\n\n## Observations\n\n- Likes hiking\n"
    bob_md   = "# Bob\n\n## Observations\n\n- Plays guitar\n"
    result   = _import(client, {"Alice.md": alice_md, "Bob.md": bob_md})
    assert "Alice" in result["imported"]
    assert "Bob"   in result["imported"]


def test_import_export_roundtrip(client):
    """Export an entity, then re-import it — memories should be skipped (not duplicated)."""
    _remember(client, "Alice", "Likes hiking",  category="preference")
    _remember(client, "Alice", "Works at CERN", category="general")
    exported = _export_entity(client, "Alice")
    result = _import(client, {"Alice.md": exported})
    # All facts already exist — none added, all skipped
    assert result["imported"]["Alice"]["memories_added"]   == 0
    assert result["imported"]["Alice"]["memories_skipped"] == 2


def test_import_requires_auth(api_auth):
    """POST /import/markdown is a write endpoint — must require auth."""
    import api
    with TestClient(api.app) as unauthenticated:
        r = unauthenticated.post("/import/markdown", json={"files": {}})
        assert r.status_code == 401
