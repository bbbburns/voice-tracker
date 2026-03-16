import json
import os
import sys
import unittest.mock

import pytest

# tracker.py reads env vars at import time; provide stubs before importing.
os.environ.setdefault("HA_HOST", "localhost")
os.environ.setdefault("HA_PORT", "8123")
os.environ.setdefault("HA_TOKEN", "test-token")
os.environ.setdefault("PIPELINE_ID", "test-pipeline")

import tracker  # noqa: E402  (must come after env setup)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(type_, data, timestamp=None):
    e = {"type": type_, "data": data}
    if timestamp is not None:
        e["timestamp"] = timestamp
    return e


# ---------------------------------------------------------------------------
# parse_intent_events
# ---------------------------------------------------------------------------

def test_normal_local_intent():
    events = [
        make_event("intent-start", {"intent_input": "turn on the lights", "engine": "local"}, "2026-01-01T00:00:00Z"),
        make_event("intent-end", {"processed_locally": True}),
    ]
    result = tracker.parse_intent_events(events)
    assert result["intent_input"] == "turn on the lights"
    assert result["engine"] == "local"
    assert result["timestamp"] == "2026-01-01T00:00:00Z"
    assert result["processed_locally"] is True


def test_normal_ai_intent():
    events = [
        make_event("intent-start", {"intent_input": "what's the weather?", "engine": "claude"}, "2026-01-01T00:01:00Z"),
        make_event("intent-end", {"processed_locally": False}),
    ]
    result = tracker.parse_intent_events(events)
    assert result["intent_input"] == "what's the weather?"
    assert result["processed_locally"] is False


def test_missing_intent_end():
    events = [
        make_event("intent-start", {"intent_input": "hello", "engine": "local"}, "ts"),
    ]
    result = tracker.parse_intent_events(events)
    assert result["intent_input"] == "hello"
    assert result["processed_locally"] is None


def test_missing_intent_start():
    events = [
        make_event("intent-end", {"processed_locally": True}),
    ]
    result = tracker.parse_intent_events(events)
    assert result["intent_input"] is None
    assert result["engine"] is None
    assert result["timestamp"] is None
    assert result["processed_locally"] is True


def test_empty_event_list():
    result = tracker.parse_intent_events([])
    assert result == {"intent_input": None, "engine": None, "timestamp": None, "processed_locally": None}


def test_extra_irrelevant_events_are_ignored():
    events = [
        make_event("stt-end", {"text": "turn on the lights"}),
        make_event("intent-start", {"intent_input": "turn on the lights", "engine": "local"}, "ts"),
        make_event("intent-end", {"processed_locally": True}),
        make_event("tts-start", {"voice": "en"}),
    ]
    result = tracker.parse_intent_events(events)
    assert result["intent_input"] == "turn on the lights"
    assert result["processed_locally"] is True


def test_intent_end_before_intent_start():
    # intent-end appears first; break exits the loop before intent-start is seen.
    events = [
        make_event("intent-end", {"processed_locally": False}),
        make_event("intent-start", {"intent_input": "never reached", "engine": "local"}, "ts"),
    ]
    result = tracker.parse_intent_events(events)
    assert result["processed_locally"] is False
    assert result["intent_input"] is None  # loop broke before reaching intent-start


# ---------------------------------------------------------------------------
# log_request
# ---------------------------------------------------------------------------

def test_log_request_writes_valid_jsonl(tmp_path):
    log_path = tmp_path / "voice_requests.jsonl"
    with unittest.mock.patch.object(tracker, "VOICE_LOG_PATH", str(log_path)):
        tracker.log_request("run-1", "2026-01-01T00:00:00Z", "turn on lights", "local", "local")

    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_id"] == "run-1"
    assert record["intent_input"] == "turn on lights"
    assert record["handled_by"] == "local"
    assert record["engine"] == "local"
    assert record["timestamp"] == "2026-01-01T00:00:00Z"


def test_log_request_creates_directory(tmp_path):
    log_path = tmp_path / "subdir" / "voice_requests.jsonl"
    with unittest.mock.patch.object(tracker, "VOICE_LOG_PATH", str(log_path)):
        tracker.log_request("run-2", "ts", "hello", "claude", "ai")

    assert log_path.exists()


def test_log_request_oserror_logs_and_does_not_raise(tmp_path, caplog):
    import logging
    log_path = tmp_path / "voice_requests.jsonl"
    with unittest.mock.patch.object(tracker, "VOICE_LOG_PATH", str(log_path)):
        with unittest.mock.patch("builtins.open", side_effect=OSError("disk full")):
            with caplog.at_level(logging.ERROR, logger="tracker"):
                tracker.log_request("run-3", "ts", "hello", "local", "local")

    assert any("run-3" in r.message for r in caplog.records)
    assert any("disk full" in r.message for r in caplog.records)
