"""
Shared pytest fixtures for memory-mcp tests.

Autouse fixtures applied to every test:
  isolated_db  — patches server.DB_PATH to a per-test temp file and initialises schema
  mock_embed   — replaces async embed() with a deterministic, offline implementation

This means tests require no running Ollama instance and no network access.
Each test gets a fully isolated SQLite database via tmp_path.
"""

import hashlib
import random
import sys
from pathlib import Path

import pytest

# Ensure project root is importable from the tests/ subdirectory
sys.path.insert(0, str(Path(__file__).parent.parent))

import server as mem


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Give each test its own temp SQLite file, fully initialised with schema."""
    db_path = tmp_path / "test_memory.db"
    monkeypatch.setattr(mem, "DB_PATH", db_path)
    mem.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def mock_embed(monkeypatch):
    """
    Replace embed() with a deterministic, offline implementation.

    Different text → different (but stable) unit vectors, seeded by MD5 of the text.
    Tests verify structure and filtering, not semantic ranking — the mock vectors
    do not cluster by meaning.
    """
    async def fake_embed(text: str) -> list[float]:
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(mem.EMBED_DIM)]
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec]

    monkeypatch.setattr(mem, "embed", fake_embed)
