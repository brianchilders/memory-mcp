#!/usr/bin/env python3
"""
mqtt_bridge.py — MQTT → memory-mcp bridge

Subscribes to MQTT topics and forwards messages to the memory-mcp HTTP API.
Runs as a standalone process, completely independent of memory-mcp.

--------------------------------------------------------------------------------
Topic schema (built-in, no config needed)
--------------------------------------------------------------------------------

  memory/record/{entity_name}/{metric}
    Payload options:
      JSON object:  {"value": 71.4, "unit": "F", "source": "ha"}
      Bare scalar:  71.4   or   "home"

  memory/remember/{entity_name}
    Payload (JSON): {"fact": "Prefers 68°F at night", "category": "preference",
                     "confidence": 0.9, "source": "mqtt"}
    Payload (plain string): treated as the fact with defaults applied

--------------------------------------------------------------------------------
Mapped topics (mqtt_mappings.json)
--------------------------------------------------------------------------------

For devices that publish to their own topic format (Zigbee2MQTT, Z-Wave JS, etc.),
define static topic → entity/metric mappings in mqtt_mappings.json.

Two payload modes:

  JSON object payload (multi-field, e.g. Zigbee2MQTT):
    {
      "zigbee2mqtt/living_room_sensor": {
        "entity": "living_room",
        "entity_type": "room",
        "metrics": {
          "temperature": {"unit": "F"},
          "humidity":    {"unit": "%"},
          "occupancy":   {}
        }
      }
    }

  Scalar payload (single value, e.g. HA state topics):
    {
      "homeassistant/sensor/brian_presence/state": {
        "entity": "Brian",
        "entity_type": "person",
        "scalar_metric": "presence"
      }
    }

See mqtt_mappings.example.json for a complete reference.

--------------------------------------------------------------------------------
Environment variables
--------------------------------------------------------------------------------

  MEMORY_MQTT_BROKER      Broker hostname or IP  (required)
  MEMORY_MQTT_PORT        Broker port            (default: 1883)
  MEMORY_MQTT_USER        Username               (optional)
  MEMORY_MQTT_PASSWORD    Password               (optional)
  MEMORY_MQTT_TOPICS      Comma-separated topic patterns to subscribe
                          (default: memory/#)
  MEMORY_MQTT_MAPPINGS    Path to mappings JSON  (default: mqtt_mappings.json
                          next to this script)
  MEMORY_API_URL          memory-mcp HTTP API    (default: http://localhost:8900)
  MEMORY_MQTT_CLIENT_ID   MQTT client ID         (default: memory-mcp-bridge)

--------------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------------

  pip install "paho-mqtt>=2.0" httpx

  export MEMORY_MQTT_BROKER=homeassistant.local
  export MEMORY_API_URL=http://localhost:8900
  python integrations/mqtt_bridge.py

  # Or with a mappings file:
  cp integrations/mqtt_mappings.example.json integrations/mqtt_mappings.json
  # edit mqtt_mappings.json to match your devices
  python integrations/mqtt_bridge.py
"""

import json
import logging
import os
import sys
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BROKER        = os.environ.get("MEMORY_MQTT_BROKER", "")
PORT          = int(os.environ.get("MEMORY_MQTT_PORT", "1883"))
USER          = os.environ.get("MEMORY_MQTT_USER", "")
PASSWORD      = os.environ.get("MEMORY_MQTT_PASSWORD", "")
TOPICS_RAW    = os.environ.get("MEMORY_MQTT_TOPICS", "memory/#")
MAPPINGS_FILE = os.environ.get(
    "MEMORY_MQTT_MAPPINGS",
    str(Path(__file__).parent / "mqtt_mappings.json"),
)
API_URL    = os.environ.get("MEMORY_API_URL", "http://localhost:8900")
CLIENT_ID  = os.environ.get("MEMORY_MQTT_CLIENT_ID", "memory-mcp-bridge")

TOPICS = [t.strip() for t in TOPICS_RAW.split(",") if t.strip()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mqtt_bridge")


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

def load_mappings(path: str) -> dict:
    """Load optional device topic mappings from JSON. Returns {} if not found."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        data = json.load(f)
    log.info("Loaded %d topic mapping(s) from %s", len(data), p)
    return data


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def api_post(endpoint: str, payload: dict) -> bool:
    """POST to memory-mcp API. Returns True on success, False on error."""
    url = f"{API_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        r = httpx.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        log.error("API %s → HTTP %s: %s", url, e.response.status_code, e.response.text[:200])
    except httpx.RequestError as e:
        log.error("API unreachable at %s: %s", url, e)
    return False


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _parse_payload(raw: str) -> int | float | str | dict | list:
    """Parse payload as JSON if possible, otherwise as a bare scalar."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            return float(raw)
        except ValueError:
            return raw.strip()


# ---------------------------------------------------------------------------
# Topic handlers
# ---------------------------------------------------------------------------

def handle_record(parts: list[str], payload_raw: str) -> None:
    """
    Handle  memory/record/{entity_name}/{metric}

    The metric segment may contain slashes, so everything after index 2 is
    joined back:  memory/record/living_room/hvac/setpoint
    → entity=living_room, metric=hvac/setpoint
    """
    if len(parts) < 4:
        log.warning("Malformed record topic (need >=4 parts): %s", "/".join(parts))
        return

    entity_name = parts[2]
    metric = "/".join(parts[3:])
    data = _parse_payload(payload_raw)

    if isinstance(data, dict):
        if "value" not in data:
            log.warning("Payload missing 'value' on memory/record/%s/%s", entity_name, metric)
            return
    else:
        data = {"value": data}

    body = {
        "entity_name": entity_name,
        "metric":      metric,
        "value":       data["value"],
        "source":      data.get("source", "mqtt"),
    }
    if "unit"        in data: body["unit"]        = data["unit"]
    if "entity_type" in data: body["entity_type"] = data["entity_type"]

    if api_post("/record", body):
        log.info("recorded   %s / %s = %r", entity_name, metric, data["value"])


def handle_remember(parts: list[str], payload_raw: str) -> None:
    """Handle  memory/remember/{entity_name}"""
    if len(parts) < 3:
        log.warning("Malformed remember topic (need >=3 parts): %s", "/".join(parts))
        return

    entity_name = parts[2]
    data = _parse_payload(payload_raw)

    if isinstance(data, str):
        data = {"fact": data}
    elif not isinstance(data, dict):
        log.warning("Unexpected payload type on memory/remember/%s", entity_name)
        return

    if "fact" not in data:
        log.warning("Payload missing 'fact' on memory/remember/%s", entity_name)
        return

    body = {
        "entity_name": entity_name,
        "fact":        data["fact"],
        "category":    data.get("category", "general"),
        "confidence":  data.get("confidence", 0.9),
        "source":      data.get("source", "mqtt"),
    }
    if "entity_type" in data: body["entity_type"] = data["entity_type"]

    if api_post("/remember", body):
        log.info("remembered  %s: %s", entity_name, data["fact"][:80])


def handle_mapped(topic: str, mapping: dict, payload_raw: str) -> None:
    """
    Handle a device topic via a static mapping entry.

    Two modes:
      "metrics" dict   — expects JSON object payload, maps each named field
      "scalar_metric"  — expects a bare value (or single-value JSON), maps it
                         to one metric
    """
    entity_name = mapping.get("entity")
    entity_type = mapping.get("entity_type")

    if not entity_name:
        log.warning("Mapping for %s missing required 'entity' key", topic)
        return

    data = _parse_payload(payload_raw)

    # --- Scalar mode ---
    if "scalar_metric" in mapping:
        metric = mapping["scalar_metric"]
        if isinstance(data, dict):
            value = data.get("state") or data.get("value") or next(iter(data.values()), None)
        else:
            value = data

        if value is None:
            log.warning("Could not extract value for scalar_metric on %s", topic)
            return

        body = {"entity_name": entity_name, "metric": metric, "value": value, "source": "mqtt"}
        if entity_type:        body["entity_type"] = entity_type
        if mapping.get("unit"): body["unit"]        = mapping["unit"]

        if api_post("/record", body):
            log.info("mapped     %s / %s = %r  (from %s)", entity_name, metric, value, topic)
        return

    # --- Multi-field mode ---
    metrics_map = mapping.get("metrics", {})
    if not metrics_map:
        log.warning("Mapping for %s has neither 'metrics' nor 'scalar_metric'", topic)
        return

    if not isinstance(data, dict):
        log.warning("Expected JSON object on %s, got %s", topic, type(data).__name__)
        return

    for field, metric_cfg in metrics_map.items():
        if field not in data:
            continue
        metric = metric_cfg.get("metric", field)
        body = {
            "entity_name": entity_name,
            "metric":      metric,
            "value":       data[field],
            "source":      "mqtt",
        }
        if entity_type:            body["entity_type"] = entity_type
        if metric_cfg.get("unit"): body["unit"]        = metric_cfg["unit"]

        if api_post("/record", body):
            log.info("mapped     %s / %s = %r  (from %s)", entity_name, field, data[field], topic)


# ---------------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------------

def on_connect(client, userdata, connect_flags, reason_code, properties=None) -> None:
    if reason_code == 0:
        log.info("Connected to %s:%s", BROKER, PORT)
        for topic in TOPICS:
            client.subscribe(topic, qos=1)
            log.info("Subscribed  %s  (QoS 1)", topic)
    else:
        log.error("Connection refused — reason code %s", reason_code)


def on_disconnect(client, userdata, disconnect_flags=None, reason_code=None, properties=None) -> None:
    if reason_code != 0:
        log.warning("Unexpected disconnect (rc=%s) — reconnect in progress", reason_code)


def on_message(client, userdata, msg: mqtt.MQTTMessage) -> None:
    topic = msg.topic
    payload_raw = msg.payload.decode("utf-8", errors="replace").strip()

    if not payload_raw:
        log.debug("Empty payload on %s — skipping", topic)
        return

    mappings: dict = userdata.get("mappings", {})

    # 1. Static mappings take priority (exact topic match)
    if topic in mappings:
        handle_mapped(topic, mappings[topic], payload_raw)
        return

    # 2. Route memory/* topics
    parts = topic.split("/")
    if parts[0] != "memory" or len(parts) < 2:
        log.debug("No handler for topic %s", topic)
        return

    action = parts[1]
    if action == "record":
        handle_record(parts, payload_raw)
    elif action == "remember":
        handle_remember(parts, payload_raw)
    else:
        log.debug("Unknown memory action '%s' on %s", action, topic)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BROKER:
        log.error("MEMORY_MQTT_BROKER is not set.")
        log.error("Example: export MEMORY_MQTT_BROKER=your-broker-host")
        sys.exit(1)

    mappings = load_mappings(MAPPINGS_FILE)

    client = mqtt.Client(
        client_id=CLIENT_ID,
        userdata={"mappings": mappings},
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    if USER:
        client.username_pw_set(USER, PASSWORD or None)

    # paho retries automatically with backoff when loop_forever() is active
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    log.info("Connecting to %s:%s (client_id=%s) ...", BROKER, PORT, CLIENT_ID)
    try:
        client.connect(BROKER, PORT, keepalive=60)
    except OSError as e:
        log.error("Could not connect to broker: %s", e)
        sys.exit(1)

    client.loop_forever()


if __name__ == "__main__":
    main()
