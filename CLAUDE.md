# Voice Tracker

A Docker containerized Python service that monitors a local Home Assistant instance for voice assistant pipeline runs and increments counter helpers based on whether each request was handled locally or by an AI (Claude).

## What it does

- Connects to HA via WebSocket (`wss://`) and authenticates with a long-lived access token
- Polls `assist_pipeline/pipeline_debug/list` every 5 seconds for new pipeline runs
- For each new run, fetches full event detail and checks the `processed_locally` field in the `intent-end` event
  - `true` → increments `counter.voice_requests_local`
  - `false` → increments `counter.voice_requests_ai` and appends a JSONL record to `data/ai_requests.jsonl`
- Reconnects automatically on connection loss

## Project structure

```
tracker.py        Main script (asyncio + websockets + aiohttp)
Dockerfile        python:3.12-slim image
compose.yml       Single-service Docker Compose
requirements.txt  websockets==12.0, aiohttp==3.9.5
.env              Runtime secrets (never commit this)
.gitignore        Excludes .env and data/
data/             Bind-mounted into container at /data (gitignored)
  ai_requests.jsonl   JSONL log of AI-handled requests (created on first AI request)
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

## Running

```sh
docker compose up -d --build
docker compose logs -f
```
