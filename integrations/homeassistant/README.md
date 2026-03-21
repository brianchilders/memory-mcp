# Home Assistant Integration

This folder contains everything you need to connect memory-mcp to
[Home Assistant](https://www.home-assistant.io/) (HA).

> **Official docs are always the ground truth.**
> Home Assistant evolves quickly. When in doubt, consult the links throughout
> this guide rather than relying solely on the examples here.

---

## Integration modes

There are three ways memory-mcp and Home Assistant can work together.
They are not mutually exclusive — many setups use all three.

| Mode | Direction | Best for |
|---|---|---|
| [REST commands](#mode-1-rest-commands) | HA → memory-mcp | Sensor data, presence, climate, semantic facts |
| [MQTT bridge](#mode-2-mqtt-bridge) | HA → MQTT → memory-mcp | High-frequency sensors, Zigbee2MQTT devices |
| [MCP integration](#mode-3-mcp-integration) | HA AI ↔ memory-mcp | AI assistant access to persistent memory |

---

## Prerequisites

- **Home Assistant** 2023.6 or newer (for `rest_command` template improvements)
- **memory-mcp** running and reachable from HA's network (default port 8900)
- **Bearer token** from the memory-mcp admin UI at `http://<your-host>:8900/admin/settings`

Confirm reachability from the HA machine before configuring:

```bash
curl http://<memory-mcp-host>:8900/health
# Expected: {"status":"ok","entities":...}
```

---

## Authentication

### memory-mcp bearer token (HA → memory-mcp)

Memory-mcp uses bearer token authentication on all API endpoints except `/health`.
The token is auto-generated on first startup and shown in the admin UI at
`/admin/settings`. You can regenerate it there at any time.

**Never put the token directly in a YAML file.** Store it in HA's
[`secrets.yaml`](https://www.home-assistant.io/docs/configuration/secrets/)
instead:

```yaml
# /config/secrets.yaml
memory_mcp_auth_header: "Bearer paste-your-token-here"
```

The package file references this secret as `!secret memory_mcp_auth_header`
on every `Authorization` header — no further edits needed once the secret is set.

### HA Long-Lived Access Token (external scripts → HA)

If you run external scripts (such as `background_example.py`) that need to
**read state from Home Assistant** — for example, pulling sensor values to
enrich memory-mcp records — those scripts authenticate *with HA* using a
Long-Lived Access Token (LLAT).

To create one:
1. Open Home Assistant at `http://homeassistant.local:8123`
2. Go to your **Profile** (bottom-left avatar) → **Security** tab
3. Scroll to **Long-Lived Access Tokens** → **Create Token**
4. Copy the token — it is shown only once

Store it in the external script's `.env` or environment:

```bash
export HA_TOKEN="your-long-lived-access-token"
export HA_URL="http://homeassistant.local:8123"
```

> **Official reference:** [Home Assistant Authentication](https://www.home-assistant.io/docs/authentication/)
> and [Long-Lived Access Tokens](https://www.home-assistant.io/docs/authentication/#long-lived-access-tokens)

### Application Credentials (OAuth integrations)

If you build a custom HA integration that uses OAuth to authenticate users
against memory-mcp or another external service, HA's Application Credentials
framework provides a standardised flow.

> **Official reference:** [Application Credentials integration](https://www.home-assistant.io/integrations/application_credentials/)

For the use cases in this folder (rest_commands and MQTT), bearer tokens and
LLATs are sufficient. Application Credentials are relevant only if you build
a custom HA integration component.

---

## Mode 1: REST commands

The `memory_mcp_package.yaml` in this folder is a HA
[package](https://www.home-assistant.io/docs/configuration/packages/)
that exposes every memory-mcp endpoint as a
[`rest_command`](https://www.home-assistant.io/integrations/rest_command/)
and provides ready-to-use automations for presence, temperature, climate,
energy monitoring, and daily summaries.

### Step 1 — Enable packages in configuration.yaml

Create a `packages/` folder in your HA config directory if it doesn't exist,
then add to `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

> **Official reference:** [Packages](https://www.home-assistant.io/docs/configuration/packages/)

### Step 2 — Add the token to secrets.yaml

```yaml
# /config/secrets.yaml
memory_mcp_auth_header: "Bearer paste-your-token-here"
```

Retrieve the token from `http://<memory-mcp-host>:8900/admin/settings`.
The `Bearer ` prefix must be included — the package file uses this secret
directly as the full `Authorization` header value.

### Step 3 — Copy and configure the package file

```bash
cp memory_mcp_package.yaml /config/packages/memory_mcp_package.yaml
```

Open the file and make two kinds of changes:

**a) Replace the host URL throughout the file.**
Every `rest_command` URL is set to `http://memory-mcp.local:8900` as a
placeholder. Do a find-and-replace of `memory-mcp.local:8900` with your
actual host (e.g. `192.168.1.42:8900`).

**b) Replace the token placeholder.**
Every `Authorization` header reads `"Bearer !secret memory_mcp_token"`.
This already references `secrets.yaml` — no further change needed if you
completed Step 2.

**c) Update entity IDs.**
Search for `# ← CHANGE` comments throughout the automations and update
each `entity_id` to match your actual HA entities.
Find your entity IDs at **Developer Tools → States**.

### Step 4 — Reload

**Developer Tools → YAML → Reload All YAML**, or restart HA.

Verify the commands are registered at **Developer Tools → Services** —
search for `rest_command.memory_record`.

### What the package includes

**REST commands** — callable from any automation, script, or the Services panel:

| Service | Endpoint | Purpose |
|---|---|---|
| `rest_command.memory_record` | `POST /record` | Push a time-series reading |
| `rest_command.memory_remember` | `POST /remember` | Store a semantic fact |
| `rest_command.memory_recall` | `POST /recall` | Semantic search |
| `rest_command.memory_get_context` | `POST /get_context` | Context snapshot |
| `rest_command.memory_record_bulk` | `POST /record/bulk` | Batch readings |
| `rest_command.memory_prune` | `POST /prune` | Delete old raw readings |

**Automations** (edit entity IDs to match your home):

| Automation | Trigger | What it records |
|---|---|---|
| Presence | `person.*` state change | `{person}/presence` categorical |
| Living room temperature | Sensor state | `living_room/temperature` numeric |
| Bedroom temperature | Sensor state | `bedroom/temperature` numeric |
| Living room humidity | Sensor state | `living_room/humidity` numeric |
| Thermostat setpoint | `climate.*` attribute | `living_room/thermostat_setpoint` |
| HVAC mode | `climate.*` state | `living_room/hvac_mode` categorical |
| Front door | `binary_sensor.*` state | `front_door/state` categorical |
| Energy (hourly) | Time pattern | `home/energy_consumption` numeric |
| Daily summary | 06:00 daily | Semantic memory snapshot |
| HA startup | `homeassistant` start | Semantic memory: restart log |

**Scripts** — callable from voice assistants, Lovelace buttons, or automations:

| Script | Purpose |
|---|---|
| `script.memory_store_preference` | Store a preference fact manually |
| `script.memory_store_observation` | Store any free-text observation |
| `script.memory_prune_old_readings` | Run a prune pass |

### Adding sensors

For each additional numeric sensor:

```yaml
- id: memory_mcp_my_sensor
  alias: "memory-mcp: push my sensor"
  mode: queued
  trigger:
    - platform: state
      entity_id: sensor.my_entity_id        # ← your entity
  condition:
    - condition: template
      value_template: "{{ trigger.to_state.state | float(-999) > -999 }}"
  action:
    - service: rest_command.memory_record
      data:
        entity_name: "room_name"
        metric:      "metric_name"
        value:       "{{ trigger.to_state.state | float }}"
        unit:        "F"
        source:      "ha"
        entity_type: "room"
```

For categorical sensors (binary sensors, string states), wrap the value in
extra quotes to send a JSON string:

```yaml
value: '"{{ trigger.to_state.state }}"'
```

> **Official reference:** [rest_command integration](https://www.home-assistant.io/integrations/rest_command/)

---

## Mode 2: MQTT bridge

If your home runs an MQTT broker (Mosquitto is the most common, built into the
HA Mosquitto add-on), use the MQTT bridge instead of or alongside REST commands.
Advantages: handles high-frequency sensors cleanly, and Zigbee2MQTT devices need
zero HA automation — just a mapping entry in `mqtt_mappings.json`.

**High-level steps:**

1. Ensure the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/)
   is configured in HA.
   > **Official reference:** [MQTT integration](https://www.home-assistant.io/integrations/mqtt/)

2. Start the memory-mcp MQTT bridge as a separate process on your server:
   ```bash
   export MEMORY_MQTT_BROKER=homeassistant.local
   export MEMORY_API_URL=http://localhost:8900
   python integrations/mqtt_bridge.py
   ```
   See [`integrations/README.md`](../README.md) for full bridge configuration
   and `mqtt_mappings.example.json` for device mapping syntax.

3. In HA, publish sensor data to the `memory/record/{entity}/{metric}` topic
   from any automation:
   ```yaml
   action:
     - service: mqtt.publish
       data:
         topic: "memory/record/{{ room }}/temperature"
         payload: "{{ states(sensor_entity) }}"
   ```

The MQTT bridge and REST commands are complementary. Use MQTT for high-frequency
sensor data and REST commands for semantic facts (which need richer payloads).

---

## Mode 3: MCP integration

Memory-mcp is a fully compliant [Model Context Protocol](https://modelcontextprotocol.io/)
server implementing the
[MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
This means any MCP-capable AI client — including HA's AI assistant — can
connect to it and call its tools in natural language.

### What this enables

When HA's AI assistant is connected to memory-mcp as an MCP tool server, it can:

- Ask "What does Brian prefer for the bedroom temperature?" and get a
  semantically-searched answer from stored memories
- Call `remember` to store facts learned during a conversation
- Call `get_context` at the start of each session to pull relevant personal
  context before responding
- Call `record` to log sensor readings or mood states directly

### Transport and connectivity

Memory-mcp's `server.py` runs over **stdio transport** — the standard transport
for local process-to-process MCP communication (used by OpenHome abilities,
Claude Code, Cursor, and similar clients).

Home Assistant's MCP integration can connect to external MCP servers. Consult
the current HA MCP documentation for supported transports and configuration
syntax, as this evolves with HA releases.

> **Official reference:** [Home Assistant MCP integration](https://www.home-assistant.io/integrations/mcp)

### Connecting HA to memory-mcp

The high-level steps (verify exact UI paths against current HA docs):

1. In HA, go to **Settings → Devices & Services → Add Integration**
2. Search for **"MCP"** or **"Model Context Protocol"**
3. When prompted for the server command or URL, provide the path to
   `server.py` (for stdio) or the memory-mcp HTTP endpoint (if HTTP transport
   is supported by the integration version you are running)
4. When prompted for authentication, provide the **memory-mcp bearer token**
   from `/admin/settings` — this is distinct from a HA Long-Lived Access Token;
   it authenticates the HA client *with memory-mcp*, not with HA itself

### MCP tools exposed by memory-mcp

Once connected, the following tools become available to the HA AI assistant:

| Tool | What the AI can do |
|---|---|
| `remember` | Store a fact about a person, room, or device |
| `recall` | Semantic search across all stored memories |
| `get_context` | Pull relevant context before responding |
| `get_profile` | Full profile: memories, readings, schedule |
| `record` | Log a sensor reading or mood state |
| `get_trends` | Summarise trends for a metric over time |
| `cross_query` | Search across memories and live readings |
| `relate` | Record a relationship between entities |
| `schedule` | Add a calendar event |

> **MCP tools specification:** [modelcontextprotocol.io/specification/2025-06-18/server/tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

---

## Troubleshooting

**Automation fires but nothing appears in memory-mcp**

Check **Settings → System → Logs** for `rest_command` errors. Common causes:
- Wrong host URL — confirm with `curl http://<host>:8900/health` from the HA host
- Token missing or incorrect — verify at `/admin/settings`
- Sensor returns `unavailable` or `unknown` — the `float(-999) > -999` guard
  filters these, but check the automation trace at
  **Settings → Automations → [your automation] → Traces**

**`rest_command` returns 401 Unauthorized**

The bearer token is wrong or missing. Copy the current token from
`http://<host>:8900/admin/settings` and update `secrets.yaml`.
After updating `secrets.yaml`, reload YAML (Developer Tools → YAML →
Reload Secrets).

**`rest_command` returns 422 Unprocessable Entity**

The JSON payload is malformed. Numeric values must not be quoted; string
values require quotes inside the outer template quotes. Test the exact payload
via **Developer Tools → Services → rest_command.memory_record**.

**Memory-mcp not reachable from HA**

- If running on the same machine: use `localhost` or `127.0.0.1`
- If on a different machine: ensure TCP 8900 is open in the firewall from HA's IP
- If using Docker: ensure the container exposes port 8900 and is on the correct network

**Token in secrets.yaml not updating**

After editing `secrets.yaml`, choose **Developer Tools → YAML → Reload Secrets**
(not just Reload Automations). The secret is cached until explicitly reloaded.

> **Official HA troubleshooting:** [Debugging automations](https://www.home-assistant.io/docs/automation/troubleshooting/)
> and [rest_command](https://www.home-assistant.io/integrations/rest_command/)
