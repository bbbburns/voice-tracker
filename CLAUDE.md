# Voice Tracker

A Docker containerized Python service that monitors a local Home Assistant instance for voice assistant pipeline runs and increments counter helpers based on whether each request was handled locally or by an AI (Claude).

## What it does

- Connects to HA via WebSocket (`wss://`) and authenticates with a long-lived access token
- On startup, loads previously-logged `run_id` values from `voice_requests.jsonl` to seed `seen_run_ids`, then polls `assist_pipeline/pipeline_debug/list` every 30 seconds for new runs (HA's debug buffer holds 10 runs; 30s is safe up to a sustained rate of 1 query/3s)
- For each new run, fetches full event detail and checks the `processed_locally` field in the `intent-end` event; all runs are appended to `data/voice_requests.jsonl` with a `handled_by` field
  - `true` → increments `counter.voice_requests_local`, logs `"handled_by": "local"`
  - `false` → increments `counter.voice_requests_ai`, logs `"handled_by": "ai"`
- Reconnects automatically on connection loss

## Project structure

```
tracker.py              Main script (asyncio + websockets + aiohttp)
tests/
  test_tracker.py       Pure-function unit tests (pytest)
Dockerfile              python:3.12-slim image
compose.yml             Single-service Docker Compose
requirements.txt        websockets==12.0, aiohttp==3.9.5
tail-voice-requests.sh  Stream voice_requests.jsonl in real time (human-readable)
.env                    Runtime secrets (never commit this)
.gitignore              Excludes .env and data/
data/                   Bind-mounted into container at /data (gitignored)
  voice_requests.jsonl  JSONL log of all voice requests with handled_by field
```

## Configuration (.env)

| Variable      | Description                                      |
|---------------|--------------------------------------------------|
| `HA_HOST`     | IP or hostname of Home Assistant                 |
| `HA_PORT`     | HA port (usually 8123)                           |
| `HA_TOKEN`    | Long-lived access token from HA profile page     |
| `PIPELINE_ID` | Pipeline ID from `assist_pipeline/pipeline/list` |

## Known issues / future work

- Auth failure waits 60 seconds and retries rather than killing the process — useful if the token is rotated without restarting the container
- WebSocket responses are read with `ws.recv()` without validating that the response `id` matches the request `id`
- SSL certificate verification is disabled (`CERT_NONE`) to support HA's self-signed local cert
- `intent-end` is occasionally missing from a run's event list (e.g. pipeline errored before reaching intent processing, or STT failed). These runs log a WARNING and are silently skipped — no counter is incremented and nothing is written to the log file.
- `intent-start` is occasionally missing (e.g. STT failure). If `processed_locally` is set but `intent_input` is absent, the counter is still incremented but the run is not written to the log file (WARNING is logged).
- `log_request` I/O errors (e.g. disk full) log an error and continue — they do not trigger a reconnect.
- `load_seen_run_ids` silently returns an empty set if the log file is missing or unreadable — on a fresh install this is correct, but if the file becomes unreadable mid-deployment, a restart could double-count runs still in HA's 10-run buffer.

## Running

```sh
docker compose up -d --build
docker compose logs -f
```

## Tests

Tests cover `parse_intent_events` and `log_request` (the pure/side-effecting functions that hold the tricky logic). The async WebSocket loop is not tested.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install pytest websockets aiohttp
pytest tests/ -v
```
