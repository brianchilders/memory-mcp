"""
integrations/ha_state_poller.py — Pull Home Assistant entity states into memory-mcp

The complement to the HA automations approach (memory_mcp_package.yaml).
Instead of waiting for HA to fire automations and push data, this script
polls the Home Assistant REST API on a schedule and pushes state changes
and readings to memory-mcp itself.

WHEN TO USE THIS INSTEAD OF HA AUTOMATIONS
───────────────────────────────────────────
  - You want to get started without configuring HA automations and packages
  - You want finer control over polling frequency and batching
  - Your HA instance is read-only or managed by someone else
  - You want to pull historical snapshots rather than react to live changes
  - You want to combine HA state with data from other sources in one place

BOTH APPROACHES CAN RUN TOGETHER
─────────────────────────────────
  Automations push on state change (immediate, event-driven).
  This poller pushes on a schedule (periodic, always consistent).
  Memory-mcp handles duplicate readings gracefully — time-series just
  accumulates more data points, which the pattern engine uses.
  For categorical readings (presence, door state), this script uses
  change detection so it does not flood memory-mcp with repeated identical values.

PREREQUISITES
─────────────
  1. A Home Assistant Long-Lived Access Token (LLAT):
     - Open HA at http://homeassistant.local:8123
     - Profile (bottom-left) → Security tab → Long-Lived Access Tokens → Create Token
     - Copy the token — it is shown only once
     Official docs: https://www.home-assistant.io/docs/authentication/#long-lived-access-tokens

  2. memory-mcp running:  python api.py  (or docker compose up)

  3. HA reachable from this machine:  curl http://homeassistant.local:8123/api/

CONFIGURATION
─────────────
  Set environment variables (or create a .env file):

    HA_URL=http://homeassistant.local:8123      # your HA URL
    HA_TOKEN=your-long-lived-access-token       # LLAT from HA Profile
    MEMORY_API_URL=http://localhost:8900        # memory-mcp HTTP API
    MEMORY_API_TOKEN=your-memory-mcp-token      # from /admin/settings (leave empty if auth disabled)
    MEMORY_PERSON_NAME=Brian                    # entity name to use for person data in memory-mcp

DEPENDENCIES
────────────
  pip install httpx python-dotenv
  (httpx is already in requirements.txt; python-dotenv is optional but recommended)
"""

import json
import logging
import os
import time
from pathlib import Path

# Load .env if present — allows storing secrets outside the script
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass   # python-dotenv not installed — use real environment variables

import httpx

# ── Configuration ──────────────────────────────────────────────────────────────

HA_URL       = os.environ.get("HA_URL",          "http://homeassistant.local:8123")
HA_TOKEN     = os.environ.get("HA_TOKEN",        "")          # HA Long-Lived Access Token
MEMORY_URL   = os.environ.get("MEMORY_API_URL",  "http://localhost:8900")
MEMORY_TOKEN = os.environ.get("MEMORY_API_TOKEN","")

POLL_INTERVAL_SECONDS = int(os.environ.get("HA_POLL_INTERVAL", "300"))   # default 5 minutes
PERSON_NAME           = os.environ.get("MEMORY_PERSON_NAME",  "Brian")   # who to store data for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ha-poller")


# ── Home Assistant REST API client ────────────────────────────────────────────

class HomeAssistantClient:
    """
    Minimal wrapper around the Home Assistant REST API.

    Full API reference: https://developers.home-assistant.io/docs/api/rest/

    Authentication uses a Long-Lived Access Token passed as a Bearer header.
    Create one at: http://<your-ha>/profile → Security → Long-Lived Access Tokens
    """

    def __init__(self, base_url: str, token: str):
        if not token:
            raise ValueError(
                "HA_TOKEN is required.\n"
                "Create a Long-Lived Access Token at:\n"
                f"  {base_url}/profile\n"
                "Profile → Security tab → Long-Lived Access Tokens → Create Token\n"
                "Then set:  export HA_TOKEN=your-token-here"
            )
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=10.0,
        )

    def get_state(self, entity_id: str) -> dict | None:
        """
        Fetch the current state of a single HA entity.

        Returns a dict with keys: entity_id, state, attributes, last_changed,
        last_updated — or None if the entity does not exist.

        Example response for a temperature sensor:
          {
            "entity_id": "sensor.living_room_temperature",
            "state": "71.4",
            "attributes": {"unit_of_measurement": "°F", "friendly_name": "Living Room Temperature"},
            "last_changed": "2026-03-21T14:30:00+00:00",
            "last_updated": "2026-03-21T14:30:00+00:00"
          }
        """
        try:
            resp = self._client.get(f"/api/states/{entity_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            log.warning("HA API error for %s: %s", entity_id, e)
            return None

    def get_states(self, entity_ids: list[str] | None = None) -> list[dict]:
        """
        Fetch multiple entity states in one request.

        If entity_ids is None, returns ALL states (can be large).
        If entity_ids is a list, fetches each individually and combines.
        For large lists, fetching all and filtering client-side is more efficient.
        """
        if entity_ids is None:
            resp = self._client.get("/api/states")
            resp.raise_for_status()
            return resp.json()

        results = []
        for eid in entity_ids:
            state = self.get_state(eid)
            if state:
                results.append(state)
        return results

    def get_numeric_state(self, entity_id: str) -> tuple[float | None, str | None]:
        """
        Convenience: return (value, unit) for a numeric sensor, or (None, None).
        """
        state = self.get_state(entity_id)
        if not state:
            return None, None
        try:
            value = float(state["state"])
            unit  = state["attributes"].get("unit_of_measurement")
            return value, unit
        except (ValueError, TypeError):
            return None, None

    def is_available(self) -> bool:
        """Check that HA is reachable and the token is valid."""
        try:
            resp = self._client.get("/api/")
            return resp.status_code == 200
        except Exception:
            return False

    def close(self):
        self._client.close()


# ── Memory-mcp client ─────────────────────────────────────────────────────────

class MemoryClient:
    """Thin wrapper around the memory-mcp HTTP API."""

    def __init__(self, base_url: str, token: str = ""):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=10.0)

    def record(self, entity_name: str, metric: str, value,
               unit: str | None = None, source: str = "ha_poller",
               entity_type: str = "person") -> dict:
        payload: dict = {
            "entity_name": entity_name, "metric": metric,
            "value": value, "entity_type": entity_type, "source": source,
        }
        if unit:
            payload["unit"] = unit
        resp = self._client.post("/record", content=json.dumps(payload))
        resp.raise_for_status()
        return resp.json()

    def record_bulk(self, readings: list[dict]) -> dict:
        resp = self._client.post("/record/bulk", content=json.dumps({"readings": readings}))
        resp.raise_for_status()
        return resp.json()

    def remember(self, entity_name: str, fact: str, category: str = "general",
                 confidence: float = 1.0, source: str = "ha_poller",
                 entity_type: str = "person") -> dict:
        payload = {
            "entity_name": entity_name, "fact": fact, "category": category,
            "confidence": confidence, "source": source, "entity_type": entity_type,
        }
        resp = self._client.post("/remember", content=json.dumps(payload))
        resp.raise_for_status()
        return resp.json()

    def health(self) -> dict:
        resp = self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()


# ── Change tracker ─────────────────────────────────────────────────────────────

class ChangeTracker:
    """
    Tracks previous values so categorical/binary readings are only pushed
    to memory-mcp when the state actually changes.

    Numeric sensors (temperature, humidity) bypass this — every poll produces
    a fresh reading that adds a data point for the pattern engine.
    """

    def __init__(self):
        self._last: dict[str, str] = {}

    def changed(self, key: str, new_value: str) -> bool:
        """Return True if the value is different from the last seen value."""
        if self._last.get(key) != new_value:
            self._last[key] = new_value
            return True
        return False

    def update(self, key: str, value: str):
        """Record a value without checking — use for initial load."""
        self._last[key] = value


# ── Job functions ─────────────────────────────────────────────────────────────

def job_person_presence(ha: HomeAssistantClient, mem: MemoryClient,
                        tracker: ChangeTracker, person_name: str):
    """
    Poll person entity state (home / away / not_home) and push to memory-mcp.
    Only pushes when state changes — presence doesn't need a reading every 5 minutes.

    TODO: replace person.brian with your HA person entity ID.
          Add more entries to the mapping for additional people.
    """
    entity_map = {
        "person.brian": person_name,   # ← CHANGE: HA entity ID → memory-mcp entity name
        # "person.sarah": "Sarah",
    }

    for ha_entity, mem_entity in entity_map.items():
        state = ha.get_state(ha_entity)
        if not state:
            log.warning("Entity %s not found in HA", ha_entity)
            continue

        presence = state["state"]   # "home", "away", "not_home"

        if tracker.changed(ha_entity, presence):
            mem.record(mem_entity, "presence", presence,
                       source="ha_poller", entity_type="person")
            log.info("presence: %s → %s = %s", ha_entity, mem_entity, presence)


def job_room_temperatures(ha: HomeAssistantClient, mem: MemoryClient):
    """
    Poll temperature (and optionally humidity) sensors for each room and push
    to memory-mcp as numeric time-series readings.

    Every poll pushes a reading — the pattern engine needs regular data points
    to detect stable averages, rising/falling trends, and anomalies.

    TODO: replace the entity IDs with your actual HA sensor entity IDs.
          Add or remove rooms as needed.
    """
    # Map: (HA entity ID, memory-mcp entity name, entity_type, unit override or None)
    # Unit override: if None, uses the unit_of_measurement attribute from HA.
    temp_sensors = [
        ("sensor.living_room_temperature", "living_room", "room",  None),   # ← CHANGE
        ("sensor.bedroom_temperature",     "bedroom",     "room",  None),   # ← CHANGE
        ("sensor.office_temperature",      "office",      "room",  None),   # ← CHANGE
        # ("sensor.garage_temperature",    "garage",      "room",  "F"),
    ]
    humidity_sensors = [
        ("sensor.living_room_humidity",    "living_room", "room",  None),   # ← CHANGE
        ("sensor.bedroom_humidity",        "bedroom",     "room",  None),   # ← CHANGE
    ]

    readings = []
    now_ts   = time.time()

    for ha_entity, mem_entity, entity_type, unit_override in temp_sensors + humidity_sensors:
        value, unit = ha.get_numeric_state(ha_entity)
        if value is None:
            continue
        metric = "temperature" if "temperature" in ha_entity else "humidity"
        readings.append({
            "entity_name": mem_entity,
            "metric":      metric,
            "value":       value,
            "unit":        unit_override or unit or "",
            "source":      "ha_poller",
            "entity_type": entity_type,
            "ts":          now_ts,
        })

    if readings:
        mem.record_bulk(readings)
        log.info("temperatures: pushed %d readings", len(readings))


def job_climate_state(ha: HomeAssistantClient, mem: MemoryClient,
                      tracker: ChangeTracker):
    """
    Poll climate (thermostat) entities and push setpoint and HVAC mode.

    Setpoint is pushed on every cycle (numeric time-series).
    HVAC mode (heat/cool/off/auto) is pushed only on change.

    TODO: replace entity IDs and room names with your actual HA climate entities.
    """
    climate_map = {
        "climate.living_room": "living_room",   # ← CHANGE: HA entity → room name
        # "climate.bedroom":   "bedroom",
    }

    for ha_entity, mem_entity in climate_map.items():
        state = ha.get_state(ha_entity)
        if not state:
            continue

        attrs    = state.get("attributes", {})
        setpoint = attrs.get("temperature")
        current  = attrs.get("current_temperature")
        mode     = state["state"]   # "heat", "cool", "off", "heat_cool", "auto"

        now_ts = time.time()
        readings = []

        if setpoint is not None:
            try:
                readings.append({
                    "entity_name": mem_entity,
                    "metric":      "thermostat_setpoint",
                    "value":       float(setpoint),
                    "unit":        "F",
                    "source":      "ha_poller",
                    "entity_type": "room",
                    "ts":          now_ts,
                })
            except (TypeError, ValueError):
                pass

        if current is not None:
            try:
                readings.append({
                    "entity_name": mem_entity,
                    "metric":      "current_temperature",
                    "value":       float(current),
                    "unit":        "F",
                    "source":      "ha_poller",
                    "entity_type": "room",
                    "ts":          now_ts,
                })
            except (TypeError, ValueError):
                pass

        if readings:
            mem.record_bulk(readings)

        # HVAC mode: push only on change
        mode_key = f"hvac_mode_{ha_entity}"
        if tracker.changed(mode_key, mode):
            mem.record(mem_entity, "hvac_mode", mode,
                       source="ha_poller", entity_type="room")
            log.info("climate: %s mode changed → %s", ha_entity, mode)


def job_binary_sensors(ha: HomeAssistantClient, mem: MemoryClient,
                       tracker: ChangeTracker):
    """
    Poll binary sensors (doors, windows, motion, presence) and push to memory-mcp.
    Only pushes on state change — open/closed doesn't need a reading every 5 minutes.

    HA binary sensor states: "on" = active/open/detected, "off" = inactive/closed/clear.
    The metric name in memory-mcp uses a human-readable value ("open"/"closed", etc.)
    so it reads naturally in semantic search results.

    TODO: replace entity IDs and descriptors with your actual sensors.
    """
    # (HA entity ID, memory-mcp entity name, metric, on_value, off_value, entity_type)
    sensors = [
        ("binary_sensor.front_door",    "front_door",    "state", "open",     "closed",  "device"),  # ← CHANGE
        ("binary_sensor.back_door",     "back_door",     "state", "open",     "closed",  "device"),  # ← CHANGE
        ("binary_sensor.garage_door",   "garage_door",   "state", "open",     "closed",  "device"),  # ← CHANGE
        ("binary_sensor.motion_living", "living_room",   "motion","detected", "clear",   "room"),    # ← CHANGE
        # ("binary_sensor.smoke_alarm", "home",          "smoke", "detected", "clear",   "house"),
    ]

    for ha_entity, mem_entity, metric, on_val, off_val, entity_type in sensors:
        state = ha.get_state(ha_entity)
        if not state or state["state"] == "unavailable":
            continue

        value = on_val if state["state"] == "on" else off_val

        if tracker.changed(ha_entity, value):
            mem.record(mem_entity, metric, value,
                       source="ha_poller", entity_type=entity_type)
            log.info("binary_sensor: %s → %s = %s", ha_entity, mem_entity, value)


def job_energy_monitoring(ha: HomeAssistantClient, mem: MemoryClient):
    """
    Poll whole-home energy consumption from HA energy sensors.
    Pushes on every cycle to build a complete power usage time-series.

    TODO: replace sensor IDs with your actual energy monitoring entities.
    See HA Energy dashboard (Settings → Energy) for entity IDs.
    """
    energy_sensors = [
        # (HA entity ID,                     memory entity,  metric,            unit)
        ("sensor.whole_home_power",           "home",         "power",           "W"),   # ← CHANGE
        ("sensor.whole_home_energy_today",    "home",         "energy_today",    "kWh"), # ← CHANGE
        # ("sensor.solar_production_power",   "home",         "solar_power",     "W"),
        # ("sensor.grid_power",               "home",         "grid_power",      "W"),
    ]

    readings = []
    now_ts   = time.time()

    for ha_entity, mem_entity, metric, unit in energy_sensors:
        value, _ = ha.get_numeric_state(ha_entity)
        if value is None:
            continue
        readings.append({
            "entity_name": mem_entity,
            "metric":      metric,
            "value":       value,
            "unit":        unit,
            "source":      "ha_poller",
            "entity_type": "house",
            "ts":          now_ts,
        })

    if readings:
        mem.record_bulk(readings)
        log.info("energy: pushed %d readings", len(readings))


def job_weather_station(ha: HomeAssistantClient, mem: MemoryClient):
    """
    If you have a local weather station integrated into HA (Tempest, Davis,
    Ambient Weather, or the built-in weather integrations), pull outdoor
    conditions from HA rather than a separate weather API.

    TODO: replace entity IDs with your actual outdoor sensor entities.
          These may live under weather.*, sensor.outdoor_*, or a named integration.
    """
    outdoor_sensors = [
        # (HA entity ID,                         memory entity, metric,               unit)
        ("sensor.outdoor_temperature",            "home",        "outdoor_temperature", "F"),   # ← CHANGE
        ("sensor.outdoor_humidity",               "home",        "outdoor_humidity",    "%"),   # ← CHANGE
        ("sensor.outdoor_wind_speed",             "home",        "wind_speed",          "mph"), # ← CHANGE
        # ("sensor.outdoor_uv_index",             "home",        "uv_index",            ""),
        # ("sensor.outdoor_rain_rate",            "home",        "rain_rate",           "in/h"),
    ]

    readings = []
    now_ts   = time.time()

    for ha_entity, mem_entity, metric, unit in outdoor_sensors:
        value, _ = ha.get_numeric_state(ha_entity)
        if value is None:
            continue
        readings.append({
            "entity_name": mem_entity,
            "metric":      metric,
            "value":       value,
            "unit":        unit,
            "source":      "ha_poller",
            "entity_type": "house",
            "ts":          now_ts,
        })

    if readings:
        mem.record_bulk(readings)
        log.info("weather_station: pushed %d outdoor readings", len(readings))


# ── Simple scheduler ───────────────────────────────────────────────────────────

class Job:
    def __init__(self, name: str, fn, every_seconds: int):
        self.name   = name
        self.fn     = fn
        self._every = every_seconds
        self._last  = 0.0

    def tick(self):
        if time.time() - self._last >= self._every:
            log.info("Running job: %s", self.name)
            try:
                self.fn()
                self._last = time.time()
            except Exception as exc:
                log.error("Job %s failed: %s", self.name, exc)
                self._last = time.time()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not HA_TOKEN:
        log.error(
            "HA_TOKEN is not set.\n"
            "Create a Long-Lived Access Token at:\n"
            "  %s/profile\n"
            "Profile → Security tab → Long-Lived Access Tokens → Create Token\n"
            "Then:  export HA_TOKEN=your-token-here",
            HA_URL,
        )
        raise SystemExit(1)

    ha  = HomeAssistantClient(HA_URL, HA_TOKEN)
    mem = MemoryClient(MEMORY_URL, MEMORY_TOKEN)

    # Verify both services before starting the loop
    if not ha.is_available():
        log.error("Cannot reach Home Assistant at %s — check HA_URL and HA_TOKEN", HA_URL)
        raise SystemExit(1)
    log.info("Connected to Home Assistant at %s", HA_URL)

    try:
        info = mem.health()
        log.info(
            "Connected to memory-mcp — %d entities, %d memories, %d readings",
            info["entities"], info["memories"], info["readings"],
        )
    except Exception as exc:
        log.error("Cannot reach memory-mcp at %s: %s", MEMORY_URL, exc)
        raise SystemExit(1)

    tracker = ChangeTracker()

    # ── Register jobs ──────────────────────────────────────────────────────────
    # Adjust every_seconds to suit your polling needs.
    # Faster polling = more data points for the pattern engine, more HA API calls.
    # A 5-minute interval (300s) is a reasonable default for most sensors.

    jobs = [
        Job(
            "person_presence",
            lambda: job_person_presence(ha, mem, tracker, PERSON_NAME),
            every_seconds=60,    # presence changes matter quickly — poll every minute
        ),
        Job(
            "room_temperatures",
            lambda: job_room_temperatures(ha, mem),
            every_seconds=POLL_INTERVAL_SECONDS,   # default 5 minutes
        ),
        Job(
            "climate_state",
            lambda: job_climate_state(ha, mem, tracker),
            every_seconds=POLL_INTERVAL_SECONDS,
        ),
        Job(
            "binary_sensors",
            lambda: job_binary_sensors(ha, mem, tracker),
            every_seconds=30,    # doors/motion — fast polling for timely change detection
        ),
        Job(
            "energy_monitoring",
            lambda: job_energy_monitoring(ha, mem),
            every_seconds=POLL_INTERVAL_SECONDS,
        ),
        Job(
            "weather_station",
            lambda: job_weather_station(ha, mem),
            every_seconds=POLL_INTERVAL_SECONDS,
        ),
        # ── Add your own jobs here ─────────────────────────────────────────────
        # Job("my_job", lambda: my_job_fn(ha, mem, tracker), every_seconds=300),
    ]

    log.info(
        "Starting HA state poller — %d jobs, polling HA at %s",
        len(jobs), HA_URL,
    )
    log.info("Ctrl-C to stop.")

    TICK = 10   # check every 10 seconds whether any job is due

    try:
        while True:
            for job in jobs:
                job.tick()
            time.sleep(TICK)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        ha.close()
        mem.close()


if __name__ == "__main__":
    main()
