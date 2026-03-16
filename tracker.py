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

POLL_INTERVAL = 5  # How often (seconds) to check for new pipeline runs

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

                    seen_run_ids = set()
                    msg_id = 1

                    # --- Seed existing runs on startup ---
                    # We fetch the current run list immediately and mark all
                    # existing runs as already seen, so we don't double-count
                    # runs that occurred before this container started.
                    runs = await get_pipeline_runs(ws, msg_id)
                    msg_id += 1
                    for run in runs:
                        seen_run_ids.add(run["pipeline_run_id"])
                    log.info(f"Seeded {len(seen_run_ids)} existing run(s), watching for new ones")

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
                                log.info(f"Run {run_id}: local intent")
                                if intent_input is None:
                                    log.warning(f"Run {run_id}: intent-start missing, skipping log entry")
                                else:
                                    log_request(run_id, timestamp, intent_input, engine, "local")
                                await increment(session, LOCAL_ENTITY)
                            elif processed_locally is False:
                                log.info(f"Run {run_id}: AI intent — {intent_input!r}")
                                if intent_input is None:
                                    log.warning(f"Run {run_id}: intent-start missing, skipping log entry")
                                else:
                                    log_request(run_id, timestamp, intent_input, engine, "ai")
                                await increment(session, AI_ENTITY)
                            else:
                                log.warning(f"Run {run_id}: processed_locally not found in intent-end event")

            except Exception as e:
                log.error(f"Connection error: {e} — reconnecting in 10s")
                await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
