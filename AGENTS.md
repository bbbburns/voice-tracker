# Voice Tracker — Agent Notes

## Project summary

Single-file Python service (`tracker.py`) that watches a Home Assistant voice pipeline via WebSocket and increments two HA counter helper entities. No framework, no database — just asyncio, websockets, and aiohttp.

## Key design decisions

- **Polling, not subscriptions**: The script polls the debug list endpoint every 5 seconds rather than subscribing to pipeline events. This keeps the implementation simple and avoids dealing with HA's event subscription lifecycle.
- **Seen-ID set**: A `seen_run_ids` set is seeded on startup (and on each reconnect) with all currently visible runs so pre-existing runs are never double-counted.
- **SSL verification disabled**: HA typically uses a self-signed cert on the local network; `ssl.CERT_NONE` is intentional.

## Important caveats when modifying

- `msg_id` must be incremented for every WebSocket request sent; HA rejects duplicate IDs within a session.
- `ws.recv()` is called immediately after each send. There are no concurrent sends, so this is currently safe — but any refactor that parallelizes WS calls must add proper response routing by `id`.
- Auth failure logs an error, waits 60 seconds, and continues the reconnect loop rather than killing the process.
- Runs that occur during a disconnection window are silently missed (they get seeded as seen on reconnect). This is consistent with startup behavior and is currently acceptable.

## HA API endpoints used

| Type      | Command                                      | Purpose                        |
|-----------|----------------------------------------------|--------------------------------|
| WebSocket | `assist_pipeline/pipeline_debug/list`        | List recent pipeline runs      |
| WebSocket | `assist_pipeline/pipeline_debug/get`         | Get events for a specific run  |
| REST POST | `/api/services/counter/increment`            | Increment a counter helper     |

## Request logging

Every completed run is written to `/data/voice_requests.jsonl` (bind-mounted to `./data/` on the host) with a `handled_by` field indicating `"local"` or `"ai"`. Example records:

```json
{"timestamp": "2026-03-16T01:38:27.047039+00:00", "run_id": "01KKT52V66VJTYQ4728RFJRVTK", "engine": "homeassistant", "intent_input": "turn off den light", "handled_by": "local"}
{"timestamp": "2026-03-16T01:45:04.611069+00:00", "run_id": "01KKT52V66VJTYQ4728RFJRVTK", "engine": "conversation.claude_conversation", "intent_input": "What time will it rain tomorrow", "handled_by": "ai"}
```

Fields come from two events in the same run's event list:
- `intent-start` → `intent_input`, `engine`, `timestamp`
- `intent-end` → `processed_locally`

## Why `intent-end` is sometimes missing

A WARNING is logged when a run's event list has no `intent-end` event. This happens when the pipeline fails before reaching intent processing — e.g. STT failed to transcribe, the pipeline timed out, or the user cancelled. No counter is incremented and nothing is written to the log file. This is expected and harmless.
