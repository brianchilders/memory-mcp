# Deployment

Running memory-mcp reliably as a persistent service on a home server or Linux machine.

---

## What runs where

memory-mcp has two entry points. Most users only need one of them:

| Process | Command | When you need it |
|---|---|---|
| **HTTP API + admin UI** | `python api.py` | Always — this is the main server |
| **MCP stdio server** | `python server.py` | Only if an OpenHome ability or MCP client connects directly over stdio |

**`api.py` is the one to run.** It starts the HTTP API on port 8900, serves the admin UI at `/admin`, and runs the pattern engine as a built-in background task. The pattern engine is not a separate process — it starts automatically when `api.py` starts.

`server.py` is only needed when you want an MCP client (like an OpenHome ability) to connect via the stdio transport instead of HTTP. If you're wiring in Home Assistant, Node-RED, or any HTTP-based caller, you don't need it.

Optional sidecar processes — each runs independently alongside `api.py`:

| Process | What it does |
|---|---|
| `integrations/mqtt_bridge.py` | Bridges MQTT topics → memory-mcp HTTP API |
| `integrations/ha_state_poller.py` | Polls the HA REST API and pushes state changes |

---

## First startup

When `api.py` starts for the first time it:

1. Creates `memory.db` in the working directory (or at `MEMORY_DB_PATH` if set)
2. Runs all schema migrations — safe to run repeatedly, nothing is lost on restart
3. Generates a random bearer token and **prints it prominently to the console** — copy it now, or find it later at `http://localhost:8900/admin/settings`
4. Starts the pattern engine background task (first run is delayed 60 seconds to let the server settle)
5. Begins serving at `http://0.0.0.0:8900`

On subsequent starts it loads the existing database and token — no reinitialization.

---

## Environment configuration

All settings are controlled by environment variables. In production, create a
file at `/etc/memory-mcp/env` to keep them in one place:

```bash
# /etc/memory-mcp/env

# AI backend — Ollama running locally (default)
MEMORY_AI_BASE_URL=http://localhost:11434/v1
MEMORY_EMBED_MODEL=nomic-embed-text
MEMORY_EMBED_DIM=768
MEMORY_LLM_MODEL=llama3.2

# AI call timeout in seconds (embedding calls; LLM calls use max(timeout, 60))
# Increase if using a slow local model
# MEMORY_AI_TIMEOUT=30

# Database — use an absolute path so it doesn't depend on working directory
MEMORY_DB_PATH=/var/lib/memory-mcp/memory.db

# API token — leave unset to use the auto-generated token (recommended)
# Set this only if you want a fixed, known token (e.g. for scripted provisioning)
# MEMORY_API_TOKEN=your-token-here

# CORS — restrict to specific origins if the server is reachable beyond localhost
# Default is "*" (allow all), which is safe for a private LAN but should be
# locked down if the port is exposed to the internet or untrusted networks
# MEMORY_CORS_ORIGINS=http://homeassistant.local,http://localhost:3000
```

The server reads `.env` in the working directory automatically on startup
(via `python-dotenv`). For systemd services, use `EnvironmentFile=` instead —
see the service units below.

---

## Docker Compose (recommended for production)

The simplest production deployment. One command to start, automatic restarts,
database in a named volume that survives container rebuilds:

```bash
# Copy and configure the environment file
cp .env.example .env
# edit .env — set MEMORY_AI_BASE_URL and any other settings

# Start in the background
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

The `docker-compose.yml` sets `MEMORY_DB_PATH=/app/data/memory.db` inside a
named volume (`memory-data`), so your database is safe across `docker compose
pull && docker compose up -d` upgrades.

Check the container is healthy:
```bash
docker compose ps
# STATUS column should show "healthy" after ~30 seconds
```

---

## Running as a systemd service

### Python virtual environment and systemd

If you installed dependencies into a virtual environment (recommended — see
`docs/installation.md`), you **must** point `ExecStart` at the venv's Python
binary directly. systemd does not source `.bashrc`, `.profile`, or any shell
activation script, so `source venv/bin/activate` is never called and the
system Python would be used instead.

The fix is simple: replace `/usr/bin/python3` with the full path to the venv
Python. If your venv is at `/home/brian/memory-mcp/venv`, then:

```
ExecStart=/home/brian/memory-mcp/venv/bin/python api.py
```

The venv Python binary has the correct `sys.path` baked in at creation time —
no activation needed. To confirm the path on your machine:

```bash
# With the venv activated
which python
# → /home/brian/memory-mcp/venv/bin/python
```

Use that path in all `ExecStart` lines below, replacing `/path/to/memory-mcp/venv/bin/python`.

---

### HTTP API + admin UI

Create `/etc/systemd/system/memory-mcp.service`:

```ini
[Unit]
Description=memory-mcp HTTP API and admin UI
After=network.target
Wants=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/memory-mcp
EnvironmentFile=/etc/memory-mcp/env
ExecStart=/path/to/memory-mcp/venv/bin/python api.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=memory-mcp

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable memory-mcp
sudo systemctl start memory-mcp
sudo systemctl status memory-mcp
```

### MQTT bridge (optional, separate service)

Only needed if you're using Zigbee2MQTT, Tasmota, ESPHome, or any other
MQTT-publishing device. See `integrations/README.md` for setup.

Create `/etc/systemd/system/memory-mqtt-bridge.service`:

```ini
[Unit]
Description=memory-mcp MQTT bridge
After=network.target memory-mcp.service mosquitto.service
Wants=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/memory-mcp
EnvironmentFile=/etc/memory-mcp/env
Environment=MEMORY_MQTT_BROKER=your-broker-host
Environment=MEMORY_API_URL=http://localhost:8900
Environment=MEMORY_MQTT_MAPPINGS=/path/to/memory-mcp/integrations/mqtt_mappings.json
ExecStart=/path/to/memory-mcp/venv/bin/python integrations/mqtt_bridge.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=memory-mqtt-bridge

[Install]
WantedBy=multi-user.target
```

### HA state poller (optional, separate service)

Only needed if you want to pull sensor state from Home Assistant automatically,
without modifying `configuration.yaml`. See `integrations/README.md` for setup.

Create `/etc/systemd/system/memory-ha-poller.service`:

```ini
[Unit]
Description=memory-mcp Home Assistant state poller
After=network.target memory-mcp.service
Wants=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/memory-mcp
EnvironmentFile=/etc/memory-mcp/env
Environment=HA_URL=http://homeassistant.local:8123
Environment=HA_TOKEN=your-long-lived-access-token
Environment=MEMORY_API_URL=http://localhost:8900
Environment=MEMORY_API_TOKEN=your-memory-mcp-token
ExecStart=/path/to/memory-mcp/venv/bin/python integrations/ha_state_poller.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=memory-ha-poller

[Install]
WantedBy=multi-user.target
```

Enable and start any optional service the same way as the main service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable memory-ha-poller
sudo systemctl start memory-ha-poller
sudo systemctl status memory-ha-poller
```

---

## Viewing logs

```bash
# Live logs — main server
journalctl -u memory-mcp -f

# Last 100 lines
journalctl -u memory-mcp -n 100

# Since yesterday
journalctl -u memory-mcp --since yesterday

# Optional sidecar logs
journalctl -u memory-mqtt-bridge -f
journalctl -u memory-ha-poller -f
```

---

## Database location

By default, `memory.db` is created in the working directory (wherever you run
`python api.py` from). For production, set `MEMORY_DB_PATH` to an absolute path
so the location doesn't depend on which directory you start from:

1. Add to `/etc/memory-mcp/env` (or your `.env` file):
   ```bash
   MEMORY_DB_PATH=/var/lib/memory-mcp/memory.db
   ```

2. Create the directory and set ownership:
   ```bash
   sudo mkdir -p /var/lib/memory-mcp
   sudo chown your-username:your-username /var/lib/memory-mcp
   ```

The server creates the file on first startup — no need to create it manually.
If using Docker Compose, this is handled automatically via the named volume.

---

## Database backup

SQLite databases are single files — backup with a file copy. Use the SQLite
online backup API to ensure a consistent copy even while the server is running:

```bash
# Safe backup while server is running (uses SQLite's .backup command)
sqlite3 /var/lib/memory-mcp/memory.db ".backup '/var/backups/memory-mcp/memory-$(date +%Y%m%d).db'"
```

Automate with cron:
```bash
# /etc/cron.d/memory-mcp-backup
# Daily backup at 3am, keep 14 days
0 3 * * * your-username \
  sqlite3 /var/lib/memory-mcp/memory.db \
  ".backup '/var/backups/memory-mcp/memory-$(date +\%Y\%m\%d).db'" && \
  find /var/backups/memory-mcp -name "memory-*.db" -mtime +14 -delete
```

Create the backup directory:
```bash
sudo mkdir -p /var/backups/memory-mcp
sudo chown your-username:your-username /var/backups/memory-mcp
```

---

## Reverse proxy (nginx)

If you want to expose the API or admin UI on a standard port (80/443) or add
TLS, place nginx in front:

```nginx
# /etc/nginx/sites-available/memory-mcp
server {
    listen 80;
    server_name memory.yourdomain.local;

    location / {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

To restrict admin UI access by IP:
```nginx
location /admin {
    allow 192.168.1.0/24;   # local network only
    deny all;
    proxy_pass http://127.0.0.1:8900;
    proxy_set_header Host $host;
}
```

---

## Reverse proxy (Caddy)

```
# Caddyfile
memory.yourdomain.local {
    reverse_proxy localhost:8900
}
```

Caddy handles TLS automatically with Let's Encrypt for public domains.

---

## Upgrading

```bash
# Pull latest code
git pull

# Run any new tests to verify nothing is broken
python -m pytest --tb=short

# Restart the service
sudo systemctl restart memory-mcp
# or: docker compose pull && docker compose up -d
```

The database schema uses `_apply_migrations()` to add new columns idempotently
on startup — no manual migration steps needed between versions.

If you changed the embedding model or dimension, run `reembed.py` before
restarting. See `docs/maintenance.md` for the full procedure.

---

## Performance tuning

These two settings are compile-time constants in `server.py` rather than
environment variables — they affect core behaviour that should be set
deliberately and not vary between restarts. Edit the file directly, then
restart the service.

### Pattern engine interval

Default is 3600 seconds (one hour). For lower-frequency sensor data you can
increase it; for high-frequency data you could run it more often, though the
default is appropriate for most setups:

```python
# server.py
PATTERN_INTERVAL = 7200   # every 2 hours — good for daily-rhythm sensors
PATTERN_INTERVAL = 1800   # every 30 minutes — if you want faster insight promotion
```

### Retention window

Default is 30 days of raw readings. Rollups and promoted memories are kept
forever — only the raw sample rows are pruned:

```python
# server.py
RETENTION_DAYS = 7    # lean — raw readings gone after a week, rollups kept
RETENTION_DAYS = 90   # generous — better anomaly baselines, more storage
```

See `docs/retention.md` for storage estimates by sensor frequency.

### SQLite WAL checkpoint

The database runs in WAL (Write-Ahead Log) mode. WAL files can grow large if
not checkpointed. Add a weekly checkpoint cron:

```bash
# /etc/cron.d/memory-mcp-wal
0 4 * * 0 your-username sqlite3 /var/lib/memory-mcp/memory.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

---

## Health monitoring

The `/health` endpoint returns row counts and is suitable for basic uptime monitoring:

```bash
curl -sf http://localhost:8900/health | python -m json.tool
```

Use with a monitoring tool (UptimeRobot, Healthchecks.io, etc.) to alert if
the server goes down.

Minimal healthcheck script for cron:
```bash
#!/bin/bash
# /usr/local/bin/check-memory-mcp
curl -sf http://localhost:8900/health > /dev/null || \
  systemctl restart memory-mcp
```
