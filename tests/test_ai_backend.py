"""
Tests for AI backend configuration (Enhancement: OpenAI-compatible API).

These tests verify the HTTP wire format sent by embed() and _call_llm() —
specifically that they use the OpenAI-compatible endpoints and payload shapes
rather than Ollama-native format.

The autouse mock_embed fixture from conftest.py is overridden here so the
real embed() function is called and intercepted at the HTTP level by
pytest-httpx's httpx_mock fixture.
"""

import json
import pytest
import server as mem


# ── Override the global mock_embed autouse fixture ────────────────────────────
# conftest.py patches server.embed for every test. In this module we need the
# real embed() to run (its HTTP calls will be caught by httpx_mock instead).

@pytest.fixture(autouse=True)
def mock_embed():
    """Shadow the conftest autouse mock — let embed() make real httpx calls."""
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _embed_response(dim: int | None = None) -> dict:
    """Minimal valid OpenAI embeddings response."""
    d = dim or mem.EMBED_DIM
    return {"data": [{"embedding": [0.1] * d, "index": 0}], "model": mem.EMBED_MODEL}


def _llm_response(content: str = "test response") -> dict:
    """Minimal valid OpenAI chat completions response."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": mem.LLM_MODEL,
    }


# ── embed() wire format ───────────────────────────────────────────────────────

async def test_embed_calls_openai_embeddings_endpoint(httpx_mock):
    """`embed()` must POST to {AI_BASE_URL}/embeddings."""
    httpx_mock.add_response(json=_embed_response())
    await mem.embed("hello world")
    req = httpx_mock.get_request()
    assert req.url.path.endswith("/embeddings")


async def test_embed_uses_input_field_not_prompt(httpx_mock):
    """OpenAI format uses 'input', not Ollama-native 'prompt'."""
    httpx_mock.add_response(json=_embed_response())
    await mem.embed("test text")
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    assert body["input"] == "test text"
    assert "prompt" not in body


async def test_embed_sends_model_name(httpx_mock):
    httpx_mock.add_response(json=_embed_response())
    await mem.embed("test")
    body = json.loads(httpx_mock.get_request().content)
    assert body["model"] == mem.EMBED_MODEL


async def test_embed_sends_bearer_token_when_api_key_set(httpx_mock, monkeypatch):
    monkeypatch.setattr(mem, "AI_API_KEY", "sk-testkey123")
    httpx_mock.add_response(json=_embed_response())
    await mem.embed("test")
    req = httpx_mock.get_request()
    assert req.headers.get("authorization") == "Bearer sk-testkey123"


async def test_embed_omits_auth_header_when_no_api_key(httpx_mock, monkeypatch):
    monkeypatch.setattr(mem, "AI_API_KEY", "")
    httpx_mock.add_response(json=_embed_response())
    await mem.embed("test")
    req = httpx_mock.get_request()
    assert "authorization" not in req.headers


async def test_embed_uses_configured_base_url(httpx_mock, monkeypatch):
    monkeypatch.setattr(mem, "AI_BASE_URL", "http://custom-host:9999/v1")
    httpx_mock.add_response(json=_embed_response())
    await mem.embed("test")
    req = httpx_mock.get_request()
    assert "custom-host:9999" in str(req.url)


async def test_embed_returns_embedding_vector(httpx_mock):
    expected = [0.5] * mem.EMBED_DIM
    httpx_mock.add_response(
        json={"data": [{"embedding": expected, "index": 0}]}
    )
    result = await mem.embed("some text")
    assert result == expected


# ── _call_llm() wire format ───────────────────────────────────────────────────

async def test_call_llm_posts_to_chat_completions(httpx_mock):
    """`_call_llm()` must POST to {AI_BASE_URL}/chat/completions."""
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("hello", mem.LLM_MODEL)
    req = httpx_mock.get_request()
    assert req.url.path.endswith("/chat/completions")


async def test_call_llm_wraps_prompt_as_user_message(httpx_mock):
    """Prompt is sent as messages=[{role: user, content: prompt}]."""
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("What is 2+2?", mem.LLM_MODEL)
    body = json.loads(httpx_mock.get_request().content)
    assert "messages" in body
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "What is 2+2?"


async def test_call_llm_sends_model_name(httpx_mock):
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("test", "my-custom-model")
    body = json.loads(httpx_mock.get_request().content)
    assert body["model"] == "my-custom-model"


async def test_call_llm_not_streaming(httpx_mock):
    """stream must be False — we expect a single synchronous response."""
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("test", mem.LLM_MODEL)
    body = json.loads(httpx_mock.get_request().content)
    assert body.get("stream") is False


async def test_call_llm_returns_assistant_content(httpx_mock):
    httpx_mock.add_response(json=_llm_response("The answer is 42."))
    result = await mem._call_llm("test", mem.LLM_MODEL)
    assert result == "The answer is 42."


async def test_call_llm_sends_bearer_token_when_api_key_set(httpx_mock, monkeypatch):
    monkeypatch.setattr(mem, "AI_API_KEY", "sk-llmkey")
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("test", mem.LLM_MODEL)
    req = httpx_mock.get_request()
    assert req.headers.get("authorization") == "Bearer sk-llmkey"


async def test_call_llm_omits_auth_when_no_api_key(httpx_mock, monkeypatch):
    monkeypatch.setattr(mem, "AI_API_KEY", "")
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("test", mem.LLM_MODEL)
    req = httpx_mock.get_request()
    assert "authorization" not in req.headers


async def test_call_llm_uses_configured_base_url(httpx_mock, monkeypatch):
    monkeypatch.setattr(mem, "AI_BASE_URL", "https://api.openai.com/v1")
    httpx_mock.add_response(json=_llm_response())
    await mem._call_llm("test", mem.LLM_MODEL)
    req = httpx_mock.get_request()
    assert "api.openai.com" in str(req.url)


# ── Configuration via environment variables ───────────────────────────────────

def test_ai_base_url_defaults_to_ollama_v1_path(monkeypatch):
    """Default AI_BASE_URL must include /v1 for OpenAI-compat routing."""
    # Simulate a fresh import with no env var set
    monkeypatch.delenv("MEMORY_AI_BASE_URL", raising=False)
    assert "/v1" in mem.AI_BASE_URL


def test_embed_dim_is_integer():
    assert isinstance(mem.EMBED_DIM, int)
    assert mem.EMBED_DIM > 0


def test_embed_dim_default_is_768(monkeypatch):
    monkeypatch.delenv("MEMORY_EMBED_DIM", raising=False)
    # The module-level constant reflects the value at import time;
    # just verify it's a valid int for the default nomic-embed-text model.
    assert mem.EMBED_DIM == int(os.environ.get("MEMORY_EMBED_DIM", "768"))


def test_config_attributes_exist():
    """All required config attributes are present on the server module."""
    for attr in ("AI_BASE_URL", "AI_API_KEY", "EMBED_MODEL", "EMBED_DIM", "LLM_MODEL"):
        assert hasattr(mem, attr), f"server.{attr} missing"


import os  # used in test_embed_dim_default_is_768
