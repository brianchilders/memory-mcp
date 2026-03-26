# Testing

## Running tests

```bash
# Install test dependencies (first time only)
pip install pytest pytest-asyncio httpx

# Run full suite
python -m pytest

# Run a specific module
python -m pytest tests/test_tools.py -v
python -m pytest tests/test_patterns.py -v
python -m pytest tests/test_retention.py -v
python -m pytest tests/test_api.py -v
python -m pytest tests/test_admin.py -v
python -m pytest tests/test_fts_recall.py -v
python -m pytest tests/test_context_budget.py -v
python -m pytest tests/test_spatial.py -v

# Run with short failure output
python -m pytest --tb=short
```

## Test modules

| Module | What it covers |
|---|---|
| `tests/test_ai_backend.py` | HTTP wire format for embed() and _call_llm() — OpenAI endpoint paths, payload shape, auth headers, configurable base URL |
| `tests/test_tools.py` | Core MCP tool functions (remember, recall, get_profile, relate, forget, record, query_stream, get_trends, schedule, cross_query, prune) |
| `tests/test_retrieval.py` | Access logging (H), multi-factor scoring (A), get_context tool (D) |
| `tests/test_composite.py` | Composite reading decomposition (C) — dotted child metrics, type inference |
| `tests/test_temporal_relations.py` | Temporal graph edges (G) — valid_from, valid_until, tool_unrelate, active filtering |
| `tests/test_contradiction.py` | Contradiction detection (B) — superseded_by, recall/context filtering |
| `tests/test_sessions.py` | Session/episodic memory (E) — open/log/close/get_session |
| `tests/test_incremental_rollups.py` | Incremental rollup processing (F) — watermarks, dirty-bucket recompute |
| `tests/test_consolidation.py` | Memory consolidation (I) — _consolidate_memories, cluster dedup |
| `tests/test_extraction.py` | Auto-extraction (J) — tool_extract_and_remember, LLM mock, error handling |
| `tests/test_patterns.py` | All pattern detectors (_detect_patterns, _detect_tod_patterns, _pearson, _detect_correlations, _detect_anomalies), _build_rollups, _promote_patterns |
| `tests/test_retention.py` | _prune_readings and tool_prune — boundary conditions, cascade safety |
| `tests/test_api.py` | All FastAPI HTTP endpoints including /prune |
| `tests/test_admin.py` | Admin UI smoke tests — route 200s, key HTML fragments present |
| `tests/test_api_sessions.py` | Episodic memory + extraction HTTP endpoints — open/log/close/get_session, extract_and_remember, role validation, offline mock_llm fixture |
| `tests/test_voice_routes.py` | Voice identity routes — GET /voices/unknown, enroll, merge (including voiceprint math), update_print, self-merge guard, NaN/Infinity rejection |
| `tests/test_graph.py` | Graph endpoints — GET /graph HTML page, GET /api/graph nodes/edges structure, memory_count, inline memories, active-only edge filter (valid_until), auth exemption |
| `tests/test_markdown_export.py` | Markdown export — single entity (frontmatter, heading, observations, wikilinks, 404), bulk export (JSON files dict), inactive relation exclusion, auth exemption |
| `tests/test_markdown_import.py` | Markdown import — parse_markdown() unit tests (name, type, category, wikilinks, em/en-dash, placeholder skip), POST /import/markdown (create, existing, idempotency, roundtrip, requires auth) |
| `tests/test_decay.py` | Confidence decay — _decay_memories() (exponential formula, floor, per-category halflife, disabled at 0, superseded rows skipped), recall confidence boost, tool_get_fading_memories() (threshold, scope, ordering, limit, superseded exclusion), GET /fading HTTP endpoint |
| `tests/test_get_related.py` | Graph traversal — tool_get_related() (direct neighbor, bidirectional, multi-hop, isolated entity, depth clamping 1–5, max_results clamping 1–500, starting entity excluded, inactive relation excluded, hop count label), GET /related/{name} (HTTP round-trip, 422 on out-of-range max_results, auth) |
| `tests/test_import_jsonl.py` | JSONL importer — import_jsonl() (entity creation, type preservation, observation dedup, partial dedup, empty observations, two-pass relations, relation idempotency, stub entities, malformed JSON, non-object JSON, empty name, blank lines, unknown type, multiple entities, non-list observations, missing relation fields, 5 MB size limit, source tag), POST /import/jsonl (HTTP 200, empty content, auth, response shape) |
| `tests/test_admin_curation.py` | Admin UI curation — POST /admin/memory/{id}/delete (200/empty body/removes from DB/removes vector/leaves others, 404), POST /admin/entity/{name}/remember (200/stored/response contains fact+category, invalid category defaults, empty fact 400, nonexistent entity 404, source tag admin_ui, increments count) |
| `tests/test_import_mem0.py` | mem0 importer — _validate_base_url() (https/http ok, trailing slash stripped, file:// rejected, ftp rejected, no scheme rejected, empty rejected), import_mem0() with mock HTTP (single page, deduplication, SSRF next-URL rejection), invalid base_url/user_id raises ValueError, POST /import/mem0 (rejects file:// scheme 400, rejects empty user_id 400, auth 401) |
| `tests/test_import_mcp_memory_service.py` | mcp-memory-service importer — _validate_db_path() (nonexistent, directory, non-SQLite >100 bytes, valid SQLite, tiny file), _discover_content_column() (finds content/memory columns, returns None for unknown schema), import_mcp_memory_service() (basic, stored in DB, entity type, dedup on reimport, empty DB, nonexistent path, invalid entity name, source tag, no recognised table), POST /import/mcp-memory-service (nonexistent path 400, auth 401, valid DB 200) |
| `tests/test_mcp_compliance.py` | MCP protocol spec compliance — GET /mcp-info (200, required keys, protocol version is YYYY-MM-DD date, version matches SDK LATEST_PROTOCOL_VERSION, SDK version matches installed package, latest >= negotiated, tool list non-empty, tool count matches, all tools have names and descriptions, names unique, core tools registered, auth required), GET /health (includes mcp_protocol_version, date format, matches SDK) |
| `tests/test_working_memory.py` | Working memory (Tier 1.75) — wm_open (creates task, entity link, TTL, status=open), wm_set (stores scalar/dict, overwrites, rejects closed/unknown tasks), wm_get (single slot, all slots with metadata, missing slot, unknown task, entity name in output), wm_list (filter by status, filter by entity, slot count, invalid status/entity), wm_close (marks closed, closed_at set, blocks writes, double-close), promote on close (creates LTM memory at TRUST_INFERRED, no entity skips, no slots message), TTL expiry (_expire_working_memory: past TTL, future TTL untouched, no TTL untouched, already-closed skipped), HTTP (POST /wm/open /wm/set /wm/get /wm/close, GET /wm/list /wm/{id}, negative TTL 422, auth 401), MCP tool dispatch (all 5 tool names in TOOLS) |
| `tests/test_fts_recall.py` | FTS5/keyword retrieval and hybrid recall — `_fts_query()` sanitisation (strips special chars, OR-joins tokens, empty fallback), FTS5 triggers (INSERT/UPDATE/DELETE auto-sync to `memories_fts`), FTS5 backfill (config key idempotency), keyword recall mode (returns BM25-ranked results, no embed call), hybrid recall (merges vector + keyword scores by max, deduplicates), `tool_search_sessions` (keyword search across session turns, groups by session, entity/summary output, scoped by entity_name), pre-write cross-check (blocks lower-trust write that contradicts higher-trust memory within cosine 0.15, passes for new entities, passes when no contradiction, passes for user-trust writes), HTTP endpoints (/recall with mode param, /search_sessions) |
| `tests/test_spatial.py` | Spatial / location memory — `_format_age` helper (seconds/minutes/hours/days), `tool_locate` (new object, entity creation, active row, same-container refresh, different-container archives old, note stored, confidence capped at 1.0), `tool_find` (current location, unknown object, shows previous after move, historical-only fallback, note in output), `tool_seen_at` (confidence boost, cap at 1.0, different location redirects to locate, case-insensitive container match), `tool_location_history` (no records, single entry, multiple entries with current/previous labels, limit param, reverse-chronological order), `_decay_locations` (reduces confidence after time backdate, respects floor, skipped when halflife=0, skips inactive rows), HTTP endpoints (POST /locate /find /seen_at, GET /location_history/{name}, confidence out-of-range 422, limit out-of-range 422, 401 without token) |
| `tests/test_context_budget.py` | Token-budget context and prospective memory — `_est_tokens()` (len//4 heuristic), `tool_get_context_budget` (header always included, memories added until budget exhausted, truncated flag set, keyword and hybrid modes, include_readings=False omits readings, relations included in budget), `_consolidate_episodes()` (extracts facts from closed sessions, stores at TRUST_INFERRED, marks consolidated=1, LLM error still marks consolidated, skips already-consolidated sessions), `tool_intend` (stores intention, entity creation, expires_ts), `tool_check_intentions` (FTS5 match fires, increments fired_count, expired intentions skipped, dismissed intentions skipped, no match returns empty), `tool_dismiss_intention` (sets active=0, no longer matched), `tool_list_intentions` (active_only filter, all filter, entity scoping), HTTP endpoints (/intend, /check_intentions, /dismiss_intention, GET /intentions) |

## Fixture design

All tests use two autouse fixtures defined in `tests/conftest.py`:

### `isolated_db`

Patches `server.DB_PATH` to a per-test temp file created by pytest's `tmp_path` fixture. Each test starts with a fresh, fully-initialised schema. No cleanup needed — pytest discards `tmp_path` automatically after each test.

### `mock_embed`

Replaces `server.embed()` with a deterministic, offline implementation:

```python
async def fake_embed(text: str) -> list[float]:
    seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(mem.EMBED_DIM)]
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]
```

Different text → different unit vector, seeded by MD5. **No Ollama or network access required.** The vectors do not cluster by semantic meaning, so tests verify structure, filtering, and counts — not semantic ranking.

## What we do NOT test

- **Semantic ranking quality** — mock embeddings are hash-based, not meaning-based. Ranking tests would be brittle and require a live Ollama instance.
- **Pattern engine timing** — `pattern_engine_loop()` is an infinite asyncio loop. We test its constituent functions (`_build_rollups`, `_promote_patterns`, `_prune_readings`) directly instead.
- **Concurrent writes** — WAL mode is tested by SQLite's own test suite.

## Adding new tests

Follow the existing convention:

1. Async functions (tool tests, pattern tests) are detected and run automatically by `pytest-asyncio` in `asyncio_mode=auto`.
2. Sync functions (API/admin tests) use `TestClient` from Starlette — import `api` inside the fixture so the autouse patches are active before the app starts.
3. Keep each test focused on one behaviour. Use descriptive names (`test_prune_boundary_old_side_deleted` not `test_prune_3`).
4. If a test needs pre-existing data, set it up explicitly in the test body — do not rely on ordering.
