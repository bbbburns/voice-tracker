#!/usr/bin/env bash
# tail-voice-requests.sh
# Streams new entries from voice_requests.jsonl in real time,
# printing timestamp, request text, and handled_by (local/ai).
#
# Uses tail -F (uppercase) to survive log rotation/replacement,
# and jq --unbuffered to avoid output delay when piping.

tail -F data/voice_requests.jsonl | jq --unbuffered -r '[
  (.timestamp | split(".")[0] + "Z" | fromdateiso8601 | strflocaltime("%Y-%m-%d %H:%M:%S")),
  .handled_by,
  .intent_input
] | @tsv'
