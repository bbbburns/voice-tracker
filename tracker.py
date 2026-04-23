import asyncio
import json
import logging
import os
import ssl
import aiohttp
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration - all values loaded from environment variables in .env
# ---------------------------------------------------------------------------
HA_HOST     = os.environ["HA_HOST"]       # IP or hostname of Home Assistant
HA_PORT     = os.environ["HA_PORT"]       # Usually 8123
HA_TOKEN    = os.environ["HA_TOKEN"]      # Long-lived access token from HA profile
PIPELINE_ID = os.environ["PIPELINE_ID"]  # ID of the pipeline to monitor (from assist_pipeline/pipeline/list)

WS_URL    = f"wss://{HA_HOST}:{HA_PORT}/api/websocket"  # WebSocket endpoint
REST_URL  = f"https://{HA_HOST}:{HA_PORT}/api"          # REST API endpoint

# HA's pipeline debug buffer holds exactly 10 runs (STORED_PIPELINE_RUNS in HA core).
# Safe interval = buffer_size / max_sustainable_rate.
# At 1 query/3s sustained (far above realistic home use), max safe = 30s.
POLL_INTERVAL = 30  # How often (seconds) to check for new pipeline runs

# ---------------------------------------------------------------------------
# Home Assistant auth header for REST API calls
# ---------------------------------------------------------------------------
HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Counter helper entity IDs in Home Assistant.
# These must match the helpers created in Settings -> Devices & Services -> Helpers
# ---------------------------------------------------------------------------
LOCAL_ENTITY = "counter.voice_requests_local"  # Incremented when HA handled intent locally
AI_ENTITY    = "counter.voice_requests_ai"     # Incremented when Claude handled the intent

VOICE_LOG_PATH = "/data/voice_requests.jsonl"

# ---------------------------------------------------------------------------
# SSL context - disables certificate verification since HA typically uses
# a self-signed certificate on the local network
# ---------------------------------------------------------------------------
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


def log_request(run_id, timestamp, intent_input, engine, handled_by):
    os.makedirs(os.path.dirname(VOICE_LOG_PATH), exist_ok=True)
    record = {
        "timestamp": timestamp,
        "run_id": run_id,
        "engine": engine,
        "intent_input": intent_input,
        "handled_by": handled_by,  # "local" or "ai"
    }
    try:
        with open(VOICE_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log.error(f"Failed to write log entry for run {run_id}: {e}")


def load_seen_run_ids():
    seen = set()
    try:
        with open(VOICE_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["run_id"])
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        pass
    return seen


def parse_intent_events(events):
    """
    Extract intent fields from a pipeline run's event list.
    Returns a dict with intent_input, engine, timestamp, processed_locally.
    Any field may be None if the corresponding event was absent.
    """
    intent_input = None
    engine = None
    timestamp = None
    processed_locally = None
    for event in events:
        if event["type"] == "intent-start":
            intent_input = event["data"].get("intent_input")
            engine = event["data"].get("engine")
            timestamp = event.get("timestamp")
        elif event["type"] == "intent-end":
            processed_locally = event["data"].get("processed_locally")
            break
    return {
        "intent_input": intent_input,
        "engine": engine,
        "timestamp": timestamp,
        "processed_locally": processed_locally,
    }


async def increment(session, entity_id):
    """Increment a counter helper in Home Assistant via the REST API."""
    url = f"{REST_URL}/services/counter/increment"
    async with session.post(
        url,
        headers=HEADERS,
        json={"entity_id": entity_id},
        ssl=ssl_context
    ) as resp:
        if resp.status == 200:
            log.info(f"Incremented {entity_id}")
        else:
            log.error(f"Failed to increment {entity_id}: {resp.status}")


async def get_pipeline_runs(ws, msg_id):
    """
    Fetch the list of recent pipeline runs from HA via WebSocket.
    Returns a list of dicts with pipeline_run_id and timestamp.
    HA only keeps a limited buffer of recent runs in memory.
    """
    await ws.send(json.dumps({
        "id": msg_id,
        "type": "assist_pipeline/pipeline_debug/list",
        "pipeline_id": PIPELINE_ID,
    }))
    resp = json.loads(await ws.recv())
    return resp.get("result", {}).get("pipeline_runs", [])


async def get_run_detail(ws, msg_id, run_id):
    """
    Fetch the full event log for a specific pipeline run.
    The events include intent-start, intent-end (with processed_locally),
    stt-end, tts-start, etc.
    """
    await ws.send(json.dumps({
        "id": msg_id,
        "type": "assist_pipeline/pipeline_debug/get",
        "pipeline_id": PIPELINE_ID,
        "pipeline_run_id": run_id,
    }))
    resp = json.loads(await ws.recv())
    return resp.get("result", {}).get("events", [])


async def main():
    """
    Main loop. Maintains a WebSocket connection to HA and polls for new
    pipeline runs every POLL_INTERVAL seconds. For each new run, fetches
    the full event detail and checks the processed_locally field in the
    intent-end event to determine whether the request was handled by
    HA's local intent engine or passed to Claude.

    Reconnects automatically if the connection is lost.
    """
    async with aiohttp.ClientSession() as session:
        seen_run_ids = load_seen_run_ids()
        log.info(f"Loaded {len(seen_run_ids)} previously-logged run(s)")
        while True:
            try:
                log.info(f"Connecting to {WS_URL}")
                async with websockets.connect(WS_URL, ssl=ssl_context) as ws:

                    # --- Authentication handshake ---
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "auth_required"
                    await ws.send(json.dumps({
                        "type": "auth",
                        "access_token": HA_TOKEN
                    }))
                    msg = json.loads(await ws.recv())
                    if msg["type"] != "auth_ok":
                        log.error("Authentication failed — check HA_TOKEN. Retrying in 60s")
                        await asyncio.sleep(60)
                        continue
                    log.info("Authenticated successfully")

                    msg_id = 1

                    # --- Polling loop ---
                    while True:
                        await asyncio.sleep(POLL_INTERVAL)
                        runs = await get_pipeline_runs(ws, msg_id)
                        msg_id += 1

                        for run in runs:
                            run_id = run["pipeline_run_id"]

                            # Skip runs we've already processed
                            if run_id in seen_run_ids:
                                continue

                            seen_run_ids.add(run_id)

                            # Fetch full event detail for this run
                            events = await get_run_detail(ws, msg_id, run_id)
                            msg_id += 1

                            # Collect intent-start and intent-end data from the event list.
                            # True  = HA matched the intent locally, no LLM involved.
                            # False = Claude processed the request.
                            parsed = parse_intent_events(events)
                            intent_input = parsed["intent_input"]
                            engine = parsed["engine"]
                            timestamp = parsed["timestamp"]
                            processed_locally = parsed["processed_locally"]

                            if processed_locally is True:
                                handled_by, entity = "local", LOCAL_ENTITY
                                log.info(f"Run {run_id}: local intent")
                            elif processed_locally is False:
                                handled_by, entity = "ai", AI_ENTITY
                                log.info(f"Run {run_id}: AI intent — {intent_input!r}")
                            else:
                                log.warning(f"Run {run_id}: processed_locally not found in intent-end event")
                                continue

                            if intent_input is None:
                                log.warning(f"Run {run_id}: intent-start missing, skipping log entry")
                            else:
                                log_request(run_id, timestamp, intent_input, engine, handled_by)
                            await increment(session, entity)

            except Exception as e:
                log.error(f"Connection error: {e} — reconnecting in 10s")
                await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
