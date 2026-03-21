# Admin UI

The admin UI is a lightweight web dashboard for browsing and managing the memory server.
Served by `api.py` at `/admin` — no separate process needed.

## Starting the server

```bash
python api.py
# Admin UI: http://localhost:8900/admin/
```

---

## Dashboard `/admin/`

The landing page shows the overall health of the server at a glance.

**Count cards** — six numbers across the top:
- **Entities** — total distinct entities in the database
- **Memories** — total semantic facts (including superseded ones)
- **Readings** — total raw time-series readings currently retained
- **Rollups** — total pre-aggregated rollup rows (hour/day/week buckets)
- **Patterns** — total promoted pattern dedup records
- **Schedule events** — total future and past schedule entries

**Reading window** — the oldest and newest timestamps in the readings table.
If readings span a wide window, the retention window is doing its job.

**Recent memories** — last 10 memories stored, with entity name, category, and fact text.

**Recent promoted patterns** — last 10 patterns promoted from the pattern engine,
with entity, metric, and pattern key.

**Prune button** — triggers `POST /admin/prune` via HTMX without a page reload.
The button is replaced inline with the result:
```
Pruned 142 readings older than 30 days. 28,350 readings remain.
```

---

## Entities `/admin/entities`

Table of all entities with:
- Entity name (clickable → entity detail page)
- Type (`person`, `room`, `device`, `house`, or custom)
- Memory count
- Reading count
- Meta attributes (JSON, collapsed)
- Last updated timestamp

Use this page to audit what entities exist and how much data each one holds.

---

## Entity detail `/admin/entity/{name}`

Full profile for a single entity. Sections from top to bottom:

### Meta attributes

Key/value pairs from the entity's `meta` JSON column. Examples:
`age`, `role`, `diet`, `wake_time`. Set via the `remember` tool with an entity
that has meta, or directly in the database.

### Memories

All memories grouped by category. Each row shows:
- **Fact** — the stored text
- **Confidence** — shown as a percentage badge when below 100% (yellow badge = inferred or approximate; no badge = full confidence)
- **Source** — where the fact came from (`manual`, `mqtt`, `ha`, `extract`, etc.)
- **Updated** — when the memory was last modified
- **Superseded** — if the memory was replaced by a newer contradicting one,
  it appears with a strikethrough and a grey "superseded" badge

**What superseded means:** When two memories are semantically similar (cosine
similarity ≥ 0.85), the older one is marked `superseded_by` the newer one.
Superseded memories don't appear in `recall` or `get_context` results but are
preserved here for audit purposes.

### Promoted patterns

Patterns detected by the pattern engine for this entity. Shows metric, pattern
description (which becomes a memory fact), and when it was detected.

### Latest readings

One row per metric showing the most recent value. Useful for a quick snapshot
of current sensor state.

### Recent readings

Last 50 raw readings for this entity, newest first. Shows metric, value, unit,
source, and timestamp.

### Relationships

Both **outgoing** (this entity → other) and **incoming** (other → this entity)
relationships. Shows relationship type, target entity, and active/inactive status.
Soft-deleted relationships (`valid_until` set) appear as inactive — the history
is preserved.

### Upcoming schedule events

Future schedule events for this entity. Shows title, start time, end time if set,
and recurrence pattern.

---

## Readings stream `/admin/readings`

Global readings log across all entities, newest first.

- **Limit selector** — 50 / 100 / 500 rows
- **Columns** — entity, metric, value (numeric or categorical), unit, source, timestamp
- **Prune button** — same as dashboard, available in the header

Use this page to verify that sensors are publishing correctly, diagnose missing
data, or check that the MQTT bridge is delivering readings.

---

## Settings `/admin/settings`

API token management page.

- **Current token** — shown masked. Use the "Show" toggle to reveal the full
  token once (useful when setting up a new caller).
- **Token source** — tells you whether the token came from the
  `MEMORY_API_TOKEN` environment variable or was auto-generated and stored in
  the database.
- **Regenerate** — generates a new random 64-character hex token and stores it
  in the database. The new token is shown in full once — copy it before
  navigating away. Only available when the token source is `db`; env-var tokens
  must be rotated by updating the environment variable.

---

## Prune action

Both Dashboard and Readings pages include a "Prune old readings" button.
Clicking it:
1. POSTs to `POST /admin/prune` via HTMX
2. The button is replaced with an inline result message (no page reload)
3. The readings count on the dashboard updates after the next page load

**What prune deletes:** raw readings older than `RETENTION_DAYS` (default 30 days).

**What prune never deletes:** rollups, memories, promoted patterns, entities,
relations, or schedule events. See `docs/retention.md`.

You can also trigger a prune from the command line:
```bash
curl -X POST http://localhost:8900/prune
```

---

## Reading the confidence badge

Memories stored at full confidence (`1.0`) show no badge — they were explicitly
stated. Memories below `1.0` show a percentage badge:

| Badge colour | Confidence range | Meaning |
|---|---|---|
| *(no badge)* | 1.0 | Explicitly stated — high trust |
| Yellow | < 1.0 | Inferred, approximate, or extracted by LLM — review if important |

The pattern engine always writes insight memories with `confidence < 1.0` to
distinguish them from manually stored facts.

---

## Dependencies

| Package | Purpose |
|---|---|
| `jinja2` | Template rendering |
| Bootstrap 5.3 | CSS — loaded from CDN, no local install |
| HTMX 1.9 | Partial-page updates — loaded from CDN, no local install |

The admin UI requires internet access to load Bootstrap and HTMX from CDN.
For air-gapped deployments, download the files and serve locally:

```bash
# Download to templates/admin/static/
curl -o templates/admin/static/bootstrap.min.css \
  https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css
curl -o templates/admin/static/htmx.min.js \
  https://unpkg.com/htmx.org@1.9.0/dist/htmx.min.js
```

Then update the `<link>` and `<script>` tags in `templates/admin/base.html`
to reference `/static/bootstrap.min.css` and `/static/htmx.min.js`.

---

## Security

The admin UI is **unauthenticated by default**. Anyone who can reach port 8900
can view all data and trigger a prune.

**Recommended:** restrict access at the network level — run the server on a
trusted LAN and don't expose port 8900 to the internet.

**To add IP allowlisting** via nginx:
```nginx
location /admin {
    allow 192.168.1.0/24;
    deny all;
    proxy_pass http://127.0.0.1:8900;
}
```

**To add HTTP basic auth** via nginx:
```nginx
location /admin {
    auth_basic "memory-mcp admin";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8900;
}
```

See `docs/deployment.md` for reverse proxy setup.
