# Voice Tracker

Tracks how often your Home Assistant voice assistant handles requests locally versus sending them to Claude (AI). Runs as a Docker container alongside your Home Assistant instance.

## How it works

The tracker connects to Home Assistant via WebSocket and polls the Assist pipeline debug endpoint every 5 seconds for new voice pipeline runs. For each new run, it checks whether the request was handled by HA's built-in intent engine or passed off to Claude:

- **Local** → increments `counter.voice_requests_local`
- **AI** → increments `counter.voice_requests_ai`

Both are appended to `data/voice_requests.jsonl` with a `handled_by` field. The log lets you review everything being asked and identify requests worth building into local automations.

If the pipeline errors before intent processing (e.g. STT failure), the counter is not incremented and nothing is written to the log — a warning is logged instead.

## Requirements

- Docker + Docker Compose
- A running Home Assistant instance
- Two [counter helpers](https://www.home-assistant.io/integrations/counter/) created in HA:
  - `counter.voice_requests_local`
  - `counter.voice_requests_ai`
- A long-lived access token from your HA profile page
- The pipeline ID of the Assist pipeline to monitor (find it via Developer Tools → `assist_pipeline/pipeline/list`)

## Setup

1. Copy `.env.example` to `.env` and fill in your values (see below)
2. Start the container:

```sh
docker compose up -d --build
docker compose logs -f
```

## .env

| Variable      | Description                                                    |
|---------------|----------------------------------------------------------------|
| `HA_HOST`     | IP or hostname of your Home Assistant instance                 |
| `HA_PORT`     | HA port (usually `8123`)                                       |
| `HA_TOKEN`    | Long-lived access token from your HA profile page             |
| `PIPELINE_ID` | ID of the Assist pipeline to monitor                          |

Example:

```env
HA_HOST=your-ha-ip-or-hostname
HA_PORT=8123
HA_TOKEN=your-long-lived-access-token
PIPELINE_ID=your-pipeline-id
```

## Voice request log

All voice requests are written to `data/voice_requests.jsonl` on the host (bind-mounted into the container). Each line is a JSON object:

```json
{"timestamp": "2026-03-16T01:38:27.047039+00:00", "run_id": "your-run-id", "engine": "homeassistant", "intent_input": "turn off den light", "handled_by": "local"}
{"timestamp": "2026-03-16T01:45:04.611069+00:00", "run_id": "your-run-id", "engine": "conversation.claude_conversation", "intent_input": "What time will it rain tomorrow", "handled_by": "ai"}
```

Filter by type with `jq 'select(.handled_by == "ai")'` if needed. To find patterns and identify requests worth turning into local automations:

```sh
jq -r '.intent_input' data/voice_requests.jsonl | claude "Group these voice requests by common intent or theme. For each group, suggest a local automation or device rename that would let Home Assistant handle them without AI."
```

## Development

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install pytest websockets aiohttp
pytest tests/ -v
```
