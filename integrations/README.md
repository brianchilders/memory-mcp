# integrations/

Standalone tools that connect memory-mcp to external systems.
These are **not part of the memory-mcp package** — they run as separate processes
and talk to memory-mcp over its existing HTTP API.

| File | What it does |
|---|---|
| `mqtt_bridge.py` | Subscribe to an MQTT broker and forward messages to memory-mcp |
| `background_example.py` | Background worker template: health sync, environment sensors, weather |
| `ha_state_poller.py` | Poll Home Assistant state via REST API and push to memory-mcp (no HA automations needed) |
| `openhome/` | OpenHome ability examples: background daemon (context injection + fact extraction) and recall skill |
| `cloudflare/` | Cloudflare Tunnel setup — expose memory-mcp safely to the internet (required for cloud-hosted callers) |
| `homeassistant/` | Home Assistant package: automations, rest_commands, scripts, sensors |

## ha_state_poller.py

Polls the [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/)
on a configurable interval and pushes entity states to memory-mcp. This is the
reverse direction from HA automations: rather than waiting for HA to push events,
the poller actively pulls current state and forwards it.

### When to use this instead of HA automations

| | HA automations (push) | ha_state_poller.py (pull) |
|---|---|---|
| **Requires HA config changes** | Yes | No — runs entirely outside HA |
| **Latency** | Immediate on state change | Up to poll interval (default 30–300s) |
| **High-frequency sensors** | Queued / throttled by HA | Sampled at poll interval |
| **Setup complexity** | Edit YAML, reload HA | Set env vars, run script |
| **Suitable for** | Presence, climate, door sensors | Quick setup, external machines, Docker |

### Dependencies

```bash
pip install httpx python-dotenv
```

### Quick start

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=your-long-lived-access-token   # HA Profile → Security → Long-Lived Access Tokens
export MEMORY_API_URL=http://localhost:8900
export MEMORY_API_TOKEN=your-memory-mcp-token  # memory-mcp admin at /admin/settings
python integrations/ha_state_poller.py
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `HA_URL` | `http://homeassistant.local:8123` | Home Assistant base URL |
| `HA_TOKEN` | *(required)* | HA Long-Lived Access Token |
| `MEMORY_API_URL` | `http://localhost:8900` | memory-mcp HTTP API base URL |
| `MEMORY_API_TOKEN` | *(required)* | memory-mcp bearer token |
| `MEMORY_PERSON_NAME` | `Brian` | Entity name used in memory-mcp for the primary person |
| `HA_POLL_INTERVAL` | `60` | Base poll interval in seconds |

### What it records

The poller ships with six job functions — edit entity IDs in the file to match
your home (search for `# ← CHANGE`):

| Job | Default interval | What it records |
|---|---|---|
| `job_person_presence` | 60s | `{person}/presence` — home/away/not_home |
| `job_room_temperatures` | 300s | `{room}/temperature` — numeric, unit F |
| `job_climate_state` | 300s | `{room}/thermostat_setpoint` + `hvac_mode` |
| `job_binary_sensors` | 30s | `{device}/state` — on/off, only on change |
| `job_energy_monitoring` | 300s | `home/energy_consumption` — numeric, unit W |
| `job_weather_station` | 300s | `weather/temperature`, `weather/humidity`, `weather/condition` |

Categorical and binary values are only pushed to memory-mcp when they change
(via `ChangeTracker`), preventing repeated identical records from flooding the
time-series store.

### Running as a service

```ini
# /etc/systemd/system/memory-ha-poller.service
[Unit]
Description=memory-mcp Home Assistant state poller
After=network.target

[Service]
User=your-user
WorkingDirectory=/path/to/memory-mcp
EnvironmentFile=/path/to/memory-mcp/.env
ExecStart=/usr/bin/python3 integrations/ha_state_poller.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now memory-ha-poller
```

---

## mqtt_bridge.py

Subscribes to an MQTT broker and forwards messages to the memory-mcp HTTP API.

### Why separate?

- memory-mcp stays focused on storing and retrieving memory
- The bridge can be restarted independently without dropping API service
- Any caller that doesn't speak MQTT keeps using HTTP as-is
- The bridge is easily swapped for a Node-RED flow or any other tool

### Dependencies

```bash
pip install "paho-mqtt>=2.0" httpx
```

### Quick start

```bash
export MEMORY_MQTT_BROKER=homeassistant.local
export MEMORY_API_URL=http://localhost:8900
python integrations/mqtt_bridge.py
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORY_MQTT_BROKER` | *(required)* | Broker hostname or IP |
| `MEMORY_MQTT_PORT` | `1883` | Broker port |
| `MEMORY_MQTT_USER` | *(empty)* | Username (if broker requires auth) |
| `MEMORY_MQTT_PASSWORD` | *(empty)* | Password |
| `MEMORY_MQTT_TOPICS` | `memory/#` | Comma-separated topic patterns to subscribe |
| `MEMORY_MQTT_MAPPINGS` | `mqtt_mappings.json` | Path to device mappings file |
| `MEMORY_API_URL` | `http://localhost:8900` | memory-mcp HTTP API base URL |
| `MEMORY_MQTT_CLIENT_ID` | `memory-mcp-bridge` | MQTT client ID |

### Topic schema

The bridge handles two categories of topics:

#### Built-in: `memory/*`

No configuration needed. Publish directly to these topics to ingest data.

**Record a sensor reading:**
```
Topic:   memory/record/{entity_name}/{metric}
Payload: {"value": 71.4, "unit": "F", "source": "ha"}
     or: 71.4                    (bare numeric)
     or: "home"                  (bare string)
```

**Store a memory fact:**
```
Topic:   memory/remember/{entity_name}
Payload: {"fact": "Prefers 68°F at night", "category": "preference", "confidence": 0.9}
     or: "Prefers 68°F at night" (bare string — uses default category/confidence)
```

The metric in `memory/record` can contain slashes:
```
memory/record/living_room/hvac/setpoint  → entity=living_room, metric=hvac/setpoint
```

#### Mapped: device topics (Zigbee2MQTT, HA state topics, etc.)

Define mappings in `mqtt_mappings.json` (see `mqtt_mappings.example.json`).
Exact topic string → configuration. Two modes:

**Multi-field JSON payload** (e.g. Zigbee2MQTT):
```json
"zigbee2mqtt/living_room_sensor": {
  "entity": "living_room",
  "entity_type": "room",
  "metrics": {
    "temperature": {"unit": "F"},
    "humidity":    {"unit": "%"}
  }
}
```
Publishes `{"temperature": 71.4, "humidity": 52, "linkquality": 89}` →
records `living_room/temperature = 71.4` and `living_room/humidity = 52`.
Unknown fields are ignored.

**Scalar payload** (e.g. Home Assistant state topics):
```json
"homeassistant/sensor/brian_presence/state": {
  "entity": "Brian",
  "entity_type": "person",
  "scalar_metric": "presence"
}
```
Publishes `home` → records `Brian/presence = "home"`.

### Home Assistant examples

Replace the `rest_command` approach with MQTT publishes. In `configuration.yaml`:

```yaml
automation:
  - alias: "Push presence to memory-mcp"
    trigger:
      - platform: state
        entity_id: person.brian
    action:
      - service: mqtt.publish
        data:
          topic: "memory/record/Brian/presence"
          payload: "{{ trigger.to_state.state }}"

  - alias: "Push thermostat setpoint to memory-mcp"
    trigger:
      - platform: state
        entity_id: climate.living_room
    action:
      - service: mqtt.publish
        data:
          topic: "memory/record/living_room/thermostat_setpoint"
          payload: >
            {"value": {{ state_attr('climate.living_room', 'temperature') }},
             "unit": "F", "source": "ha"}
```

Or, if Zigbee2MQTT is already publishing sensor data, just add the device to
`mqtt_mappings.json` — no HA automation needed at all.

### Running as a service

```ini
# /etc/systemd/system/memory-mqtt-bridge.service
[Unit]
Description=memory-mcp MQTT bridge
After=network.target mosquitto.service

[Service]
User=your-user
WorkingDirectory=/path/to/memory-mcp
Environment=MEMORY_MQTT_BROKER=homeassistant.local
Environment=MEMORY_API_URL=http://localhost:8900
ExecStart=/usr/bin/python3 integrations/mqtt_bridge.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now memory-mqtt-bridge
```

### Debugging

Use [MQTT Explorer](https://mqtt-explorer.com/) to inspect live topic traffic
on your broker. It shows all topics, payloads, and message history — the
easiest way to confirm what your devices are actually publishing.

To increase log verbosity:
```bash
PYTHONPATH=. python integrations/mqtt_bridge.py 2>&1 | grep -v DEBUG
```
