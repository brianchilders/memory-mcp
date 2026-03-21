"""
integrations/background_example.py — Background data pusher for memory-mcp

A standalone Python process that periodically collects data from external sources
and pushes it to a running memory-mcp HTTP API.  This is the recommended pattern
for integrating any Python-accessible data source — wearables, weather APIs,
calendar services, health apps, custom sensors, web scrapers, etc.

HOW TO USE THIS FILE
--------------------
1. Run memory-mcp first:  python api.py  (or docker compose up)
2. Copy this file and rename it for your use case
   (e.g. health_sync.py, weather_pusher.py)
3. Replace the TODO sections with your real data sources
4. Set env vars (MEMORY_API_URL, MEMORY_API_TOKEN) or edit the defaults below
5. Run:  python integrations/background_example.py

This file is intentionally self-contained — no memory-mcp imports.
It only uses httpx (already in requirements.txt) and the standard library.

DESIGN PRINCIPLES
-----------------
- One MemoryClient, many jobs.  Add a new job function and register it in main().
- Jobs are idempotent: each one checks whether today's data was already pushed
  and skips if so.  Safe to restart or run on a cron.
- All errors are logged and swallowed at the job level.  One failing job does
  not stop the others.
- Uses blocking httpx (not async) to keep the code simple and approachable.
  For high-frequency sensor ingestion, prefer the MQTT bridge or direct HTTP
  posts from the sensor process instead.
"""

import json
import logging
import os
import time
from datetime import datetime, date

import httpx

# ── Configuration ──────────────────────────────────────────────────────────────

API_URL   = os.environ.get("MEMORY_API_URL",   "http://localhost:8900")
API_TOKEN = os.environ.get("MEMORY_API_TOKEN", "")   # leave empty to disable auth

# How often (seconds) the main loop checks whether jobs are due to run.
# Individual jobs control their own schedule via RUN_EVERY_SECONDS.
TICK_INTERVAL = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("memory-bg")


# ── HTTP client ────────────────────────────────────────────────────────────────

class MemoryClient:
    """
    Thin wrapper around the memory-mcp HTTP API.

    All methods return the parsed JSON response on success and raise on HTTP
    errors so the caller can decide whether to retry or log-and-continue.
    """

    def __init__(self, base_url: str, token: str = ""):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=10.0,
        )

    def record(
        self,
        entity_name: str,
        metric: str,
        value,
        *,
        unit: str | None = None,
        source: str | None = None,
        entity_type: str = "person",
        ts: float | None = None,
    ) -> dict:
        """Push a single time-series reading (numeric, categorical, or composite)."""
        payload: dict = {
            "entity_name": entity_name,
            "metric":      metric,
            "value":       value,
            "entity_type": entity_type,
        }
        if unit:    payload["unit"]   = unit
        if source:  payload["source"] = source
        if ts:      payload["ts"]     = ts
        resp = self._client.post("/record", content=json.dumps(payload))
        resp.raise_for_status()
        return resp.json()

    def record_bulk(self, readings: list[dict]) -> dict:
        """Push multiple readings in one request — efficient for batch syncs."""
        resp = self._client.post("/record/bulk", content=json.dumps({"readings": readings}))
        resp.raise_for_status()
        return resp.json()

    def remember(
        self,
        entity_name: str,
        fact: str,
        *,
        category: str = "general",
        confidence: float = 1.0,
        source: str | None = None,
        entity_type: str = "person",
    ) -> dict:
        """Store a semantic fact about an entity."""
        payload = {
            "entity_name": entity_name,
            "fact":        fact,
            "category":    category,
            "confidence":  confidence,
            "entity_type": entity_type,
        }
        if source:
            payload["source"] = source
        resp = self._client.post("/remember", content=json.dumps(payload))
        resp.raise_for_status()
        return resp.json()

    def recall(self, query: str, entity_name: str | None = None, top_k: int = 5) -> str:
        """Semantic search — returns the formatted result string from the server."""
        payload: dict = {"query": query, "top_k": top_k}
        if entity_name:
            payload["entity_name"] = entity_name
        resp = self._client.post("/recall", content=json.dumps(payload))
        resp.raise_for_status()
        return resp.json().get("result", "")

    def health(self) -> dict:
        """Check server liveness and row counts."""
        resp = self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()


# ── Simple scheduler ───────────────────────────────────────────────────────────

class Job:
    """Runs a function at a fixed interval, tracking last-run time."""

    def __init__(self, name: str, fn, every_seconds: int):
        self.name    = name
        self.fn      = fn
        self._every  = every_seconds
        self._last   = 0.0   # run immediately on first tick

    def tick(self):
        if time.time() - self._last >= self._every:
            log.info("Running job: %s", self.name)
            try:
                self.fn()
                self._last = time.time()
            except Exception as exc:
                log.error("Job %s failed: %s", self.name, exc)
                self._last = time.time()   # back off — don't hammer a failing source


# ── Example job 1: Health metrics daily sync ───────────────────────────────────
#
# Pattern: once per day, pull yesterday's health summary from your data source
# and push it as time-series readings + a semantic fact if something notable happened.
#
# Plug in any source: Garmin Connect API, Oura Ring API, Withings API, Apple Health
# CSV export, a local Fitbit sync, etc.  The structure below stays the same.

def job_health_daily_sync(mem: MemoryClient, entity_name: str = "Brian"):
    """
    Push daily health metrics to memory-mcp.

    TODO: Replace _fetch_health_data() with a call to your actual health data source.
    The function should return a dict with the keys shown below — omit any keys your
    source does not provide.
    """

    today = date.today().isoformat()

    # Idempotency: check whether today's resting heart rate was already pushed.
    # If the recall returns a result mentioning today's date, skip.
    existing = mem.recall(f"resting heart rate {today}", entity_name=entity_name, top_k=1)
    if today in existing:
        log.info("health_daily_sync: %s data already pushed, skipping", today)
        return

    data = _fetch_health_data()   # TODO: replace with your data source
    if not data:
        log.warning("health_daily_sync: no data returned from source")
        return

    now_ts = time.time()
    readings = []

    if data.get("resting_hr") is not None:
        readings.append({
            "entity_name": entity_name,
            "metric":      "resting_heart_rate",
            "value":       float(data["resting_hr"]),
            "unit":        "bpm",
            "source":      "health_sync",
            "entity_type": "person",
            "ts":          now_ts,
        })

    if data.get("hrv") is not None:
        readings.append({
            "entity_name": entity_name,
            "metric":      "hrv",
            "value":       float(data["hrv"]),
            "unit":        "ms",
            "source":      "health_sync",
            "entity_type": "person",
            "ts":          now_ts,
        })

    if data.get("sleep_hours") is not None:
        readings.append({
            "entity_name": entity_name,
            "metric":      "sleep_duration",
            "value":       float(data["sleep_hours"]),
            "unit":        "hours",
            "source":      "health_sync",
            "entity_type": "person",
            "ts":          now_ts,
        })

    if data.get("steps") is not None:
        readings.append({
            "entity_name": entity_name,
            "metric":      "steps",
            "value":       int(data["steps"]),
            "unit":        "steps",
            "source":      "health_sync",
            "entity_type": "person",
            "ts":          now_ts,
        })

    if data.get("sleep_score") is not None:
        readings.append({
            "entity_name": entity_name,
            "metric":      "sleep_score",
            "value":       float(data["sleep_score"]),
            "unit":        "score",
            "source":      "health_sync",
            "entity_type": "person",
            "ts":          now_ts,
        })

    if readings:
        mem.record_bulk(readings)
        log.info("health_daily_sync: pushed %d readings", len(readings))

    # Store a notable fact if the data warrants it
    if data.get("sleep_hours") is not None and data["sleep_hours"] < 6:
        mem.remember(
            entity_name,
            f"Got only {data['sleep_hours']:.1f} hours of sleep on {today}",
            category="health",
            confidence=0.95,
            source="health_sync",
        )
        log.info("health_daily_sync: stored low-sleep fact for %s", today)


def _fetch_health_data() -> dict | None:
    """
    TODO: Replace this stub with your actual health data source.

    Examples:
      - Garmin Connect API via garminconnect library
      - Oura Ring API: GET https://api.ouraring.com/v2/usercollection/daily_sleep
      - Withings API via withings-api library
      - Apple Health: parse a recent export XML
      - A CSV file that your wearable syncs to a shared folder
      - A local SQLite DB written by a sync daemon

    Return a dict with any combination of:
      resting_hr   (int)    — bpm
      hrv          (float)  — RMSSD in ms
      sleep_hours  (float)  — total sleep in hours
      sleep_score  (int)    — device sleep score (0–100)
      steps        (int)    — daily step count

    Return None if data is unavailable (no sync today, API down, etc.).
    """
    # Stub: returns realistic-looking placeholder data so the example is runnable.
    # Delete this and return real data from your source.
    import random
    rng = random.Random(int(date.today().toordinal()))
    return {
        "resting_hr":  rng.randint(52, 68),
        "hrv":         round(rng.uniform(35.0, 75.0), 1),
        "sleep_hours": round(rng.uniform(5.5, 8.5), 2),
        "sleep_score": rng.randint(60, 95),
        "steps":       rng.randint(3000, 14000),
    }


# ── Example job 2: Environment sensor polling ──────────────────────────────────
#
# Pattern: every N minutes, read from a local sensor (GPIO, USB, serial, I²C,
# or a local API) and push a reading.  Classic Raspberry Pi use case.

def job_environment_sensors(mem: MemoryClient):
    """
    Push temperature, humidity, and CO₂ readings from local sensors.

    TODO: Replace _read_sensors() with your actual sensor reads.
    Works with any sensor reachable from Python:
      - DHT22 / AM2302 via Adafruit_DHT or CircuitPython
      - BME280 / BME680 via smbus2 (I²C)
      - SCD40 CO₂ sensor via adafruit-circuitpython-scd4x
      - A local REST API from a Shelly or Tasmota device
      - A /sys/bus temperature reading on Linux
    """
    sensors = _read_sensors()   # TODO: replace with your sensor reads

    for room, readings in sensors.items():
        for metric, (value, unit) in readings.items():
            if value is None:
                continue
            mem.record(
                entity_name=room,
                metric=metric,
                value=value,
                unit=unit,
                source="local_sensor",
                entity_type="room",
            )

    log.info("environment_sensors: pushed readings for %d rooms", len(sensors))


def _read_sensors() -> dict:
    """
    TODO: Replace with real sensor reads.

    Expected return shape:
      {
        "living_room": {
          "temperature": (71.4, "F"),
          "humidity":    (52.0, "%"),
          "co2":         (620,  "ppm"),
        },
        "bedroom": {
          "temperature": (68.1, "F"),
          "humidity":    (48.0, "%"),
        },
      }

    Omit a reading entirely or set value to None to skip it.
    """
    import random
    rng = random.Random(int(time.time() / 300))   # stable within 5-minute windows
    return {
        "living_room": {
            "temperature": (round(rng.uniform(68.0, 74.0), 1), "F"),
            "humidity":    (round(rng.uniform(40.0, 60.0), 1), "%"),
            "co2":         (rng.randint(400, 900), "ppm"),
        },
        "bedroom": {
            "temperature": (round(rng.uniform(65.0, 70.0), 1), "F"),
            "humidity":    (round(rng.uniform(38.0, 55.0), 1), "%"),
        },
    }


# ── Example job 3: Weather context ─────────────────────────────────────────────
#
# Pattern: once per hour, fetch outdoor weather and push it so the pattern engine
# can correlate indoor environment with outdoor conditions over time.

def job_weather(mem: MemoryClient, location: str = "home"):
    """
    Push outdoor weather as time-series readings.

    TODO: Replace _fetch_weather() with a real weather API call.
    Free options:
      - Open-Meteo API (no key needed): https://open-meteo.com/
      - OpenWeatherMap (free tier): https://openweathermap.org/api
      - National Weather Service (US, no key): https://api.weather.gov/
      - A local weather station via WeeWX or a Tempest API
    """
    weather = _fetch_weather()
    if not weather:
        return

    now_ts = time.time()
    readings = []

    for metric, (value, unit) in weather.items():
        if value is not None:
            readings.append({
                "entity_name": location,
                "metric":      f"outdoor_{metric}",
                "value":       value,
                "unit":        unit,
                "source":      "weather_api",
                "entity_type": "house",
                "ts":          now_ts,
            })

    if readings:
        mem.record_bulk(readings)
        log.info("weather: pushed %d outdoor readings for %s", len(readings), location)


def _fetch_weather() -> dict | None:
    """
    TODO: Replace with a real weather API call.

    Expected return shape:
      {
        "temperature": (55.2, "F"),
        "humidity":    (72.0, "%"),
        "wind_speed":  (8.0,  "mph"),
        "condition":   ("cloudy", None),   # categorical — unit is None
      }
    """
    import random
    rng = random.Random(int(time.time() / 3600))
    return {
        "temperature": (round(rng.uniform(45.0, 85.0), 1), "F"),
        "humidity":    (round(rng.uniform(30.0, 90.0), 1), "%"),
        "wind_speed":  (round(rng.uniform(0.0, 25.0), 1), "mph"),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    mem = MemoryClient(API_URL, API_TOKEN)

    # Verify connectivity before starting the loop
    try:
        info = mem.health()
        log.info(
            "Connected to memory-mcp — %d entities, %d memories, %d readings",
            info["entities"], info["memories"], info["readings"],
        )
    except Exception as exc:
        log.error("Cannot reach memory-mcp at %s: %s", API_URL, exc)
        log.error("Is memory-mcp running?  Try: python api.py")
        raise SystemExit(1)

    # ── Register jobs ──────────────────────────────────────────────────────────
    # Each job is a function that takes a MemoryClient and runs to completion.
    # Adjust RUN_EVERY_SECONDS to fit your use case:
    #   300    = 5 minutes   (sensor polling)
    #   3600   = 1 hour      (weather, light conditions)
    #   86400  = 24 hours    (daily health sync)

    PERSON = os.environ.get("MEMORY_PERSON_NAME", "Brian")   # who to store data for

    jobs = [
        Job(
            "health_daily_sync",
            lambda: job_health_daily_sync(mem, entity_name=PERSON),
            every_seconds=86400,
        ),
        Job(
            "environment_sensors",
            lambda: job_environment_sensors(mem),
            every_seconds=300,   # every 5 minutes
        ),
        Job(
            "weather",
            lambda: job_weather(mem),
            every_seconds=3600,  # every hour
        ),
        # ── Add your own jobs here ─────────────────────────────────────────────
        # Job("my_job", lambda: my_job_function(mem), every_seconds=600),
    ]

    log.info("Starting background worker — %d jobs registered", len(jobs))
    log.info("Tick interval: %ds.  Ctrl-C to stop.", TICK_INTERVAL)

    try:
        while True:
            for job in jobs:
                job.tick()
            time.sleep(TICK_INTERVAL)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        mem.close()


if __name__ == "__main__":
    main()
