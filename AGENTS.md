# Voice Tracker — Agent Notes

## Project summary

Single-file Python service (`tracker.py`) that watches a Home Assistant voice pipeline via WebSocket and increments two HA counter helper entities. No framework, no database — just asyncio, websockets, and aiohttp.

## Key design decisions

- **Polling, not subscriptions**: The script polls the debug list endpoint every 30 seconds rather than subscribing to pipeline events. No push API exists for pipeline events (open HA issue, unmerged as of early 2025). The interval is derived from the buffer math: HA's `STORED_PIPELINE_RUNS = 10`, so `max_safe_interval = 10 / (1/3s) = 30s` at the worst-case realistic sustained rate.
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

## Key functions

- `parse_intent_events(events)` — pure function; extracts `intent_input`, `engine`, `timestamp`, `processed_locally` from a run's event list. Any field may be `None` if the corresponding event was absent. Tested in `tests/test_tracker.py`.
- `log_request(run_id, timestamp, intent_input, engine, handled_by)` — appends a JSONL record to `VOICE_LOG_PATH`; catches `OSError` and logs rather than raising.

## Why `intent-end` or `intent-start` is sometimes missing

- **`intent-end` missing**: pipeline failed before reaching intent processing (STT failure, timeout, user cancel). WARNING is logged; no counter incremented, nothing written to log.
- **`intent-start` missing**: rare; if `processed_locally` is set but `intent_input` is `None`, WARNING is logged and `log_request` is skipped, but the counter is still incremented.
