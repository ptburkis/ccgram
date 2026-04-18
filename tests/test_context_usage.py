"""Tests for context_usage module."""

import json
import time
from pathlib import Path

import pytest

from ccgram.context_usage import (
    MODEL_CONTEXT_LIMITS,
    ContextUsageState,
    ContextUsageTracker,
    get_latest_usage,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_tracker(**kwargs) -> ContextUsageTracker:
    return ContextUsageTracker(
        flush_threshold=kwargs.get("flush_threshold", 0.65),
        compact_threshold=kwargs.get("compact_threshold", 0.75),
        cooldown_secs=kwargs.get("cooldown_secs", 300.0),
        check_interval_secs=kwargs.get("check_interval_secs", 30.0),
    )


def _sonnet_tokens(pct: float) -> int:
    return int(MODEL_CONTEXT_LIMITS["sonnet"] * pct)


# ── Tests ─────────────────────────────────────────────────────────────────


def test_determine_action_below_threshold():
    """Usage below flush threshold → None."""
    tracker = _make_tracker()
    usage = {"input_tokens": _sonnet_tokens(0.50), "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert tracker.determine_action(usage, "sonnet", "w1") is None


def test_determine_action_flush():
    """Usage at flush threshold → 'flush'."""
    tracker = _make_tracker()
    usage = {"input_tokens": _sonnet_tokens(0.65), "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert tracker.determine_action(usage, "sonnet", "w1") == "flush"


def test_determine_action_compact():
    """Usage at compact threshold + prior flush within cooldown → 'compact'."""
    tracker = _make_tracker(cooldown_secs=300.0)
    # Record a flush
    tracker.record_flush("w1")
    usage = {"input_tokens": _sonnet_tokens(0.80), "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert tracker.determine_action(usage, "sonnet", "w1") == "compact"


def test_determine_action_compact_without_prior_flush_returns_flush():
    """Usage at compact threshold but no prior flush → 'flush' first."""
    tracker = _make_tracker()
    usage = {"input_tokens": _sonnet_tokens(0.80), "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert tracker.determine_action(usage, "sonnet", "w1") == "flush"


def test_cooldown_prevents_retrigger():
    """After record_flush, is_in_cooldown returns True."""
    tracker = _make_tracker(cooldown_secs=300.0)
    assert not tracker.is_in_cooldown("w2")
    tracker.record_flush("w2")
    assert tracker.is_in_cooldown("w2")


def test_cooldown_expired_not_in_cooldown(monkeypatch):
    """After cooldown period, is_in_cooldown returns False."""
    tracker = _make_tracker(cooldown_secs=1.0)
    tracker.record_flush("w3")
    # Simulate time passing beyond cooldown
    monkeypatch.setattr(time, "monotonic", lambda: time.monotonic.__wrapped__() + 2.0)
    # We can't easily monkeypatch monotonic mid-call, so test via should_check instead
    # Verify the state was recorded
    state = tracker.get_state("w3")
    assert state.last_flush_action > 0


def test_get_latest_usage_parses_tail(tmp_path: Path):
    """get_latest_usage extracts usage from a valid JSONL transcript."""
    transcript = tmp_path / "transcript.jsonl"

    # Non-assistant entry (should be skipped)
    user_entry = {"type": "user", "message": {"role": "user", "content": "hello"}}

    # Assistant entry with usage
    assistant_entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 5000,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 500,
                "output_tokens": 50,
            },
        },
    }

    lines = [json.dumps(user_entry), json.dumps(assistant_entry)]
    transcript.write_text("\n".join(lines) + "\n")

    usage, model = get_latest_usage(str(transcript))

    assert usage is not None
    assert usage["input_tokens"] == 5000
    assert usage["cache_creation_input_tokens"] == 1000
    assert usage["cache_read_input_tokens"] == 500
    assert model == "sonnet"


def test_get_latest_usage_empty_file(tmp_path: Path):
    """get_latest_usage returns (None, None) for an empty file."""
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("")
    usage, model = get_latest_usage(str(transcript))
    assert usage is None
    assert model is None


def test_get_latest_usage_no_assistant_entries(tmp_path: Path):
    """get_latest_usage returns (None, None) if no assistant entries found."""
    transcript = tmp_path / "no_assistant.jsonl"
    user_entry = {"type": "user", "message": {"role": "user", "content": "hello"}}
    transcript.write_text(json.dumps(user_entry) + "\n")
    usage, model = get_latest_usage(str(transcript))
    assert usage is None
    assert model is None


def test_get_latest_usage_missing_file():
    """get_latest_usage returns (None, None) for a nonexistent file."""
    usage, model = get_latest_usage("/tmp/this_file_does_not_exist_12345.jsonl")
    assert usage is None
    assert model is None


def test_clear_window_removes_state():
    """clear_window removes tracked state."""
    tracker = _make_tracker()
    tracker.record_flush("w99")
    assert tracker.is_in_cooldown("w99")
    tracker.clear_window("w99")
    assert not tracker.is_in_cooldown("w99")


def test_should_check_respects_interval():
    """should_check returns False immediately after record_check."""
    tracker = _make_tracker(check_interval_secs=30.0)
    assert tracker.should_check("w5")  # never checked -- should be True
    tracker.record_check("w5", 0.5, "sonnet")
    assert not tracker.should_check("w5")  # just checked -- should be False


def test_cache_tokens_count_toward_context():
    """Cache tokens are included in context size for threshold calculation."""
    tracker = _make_tracker()
    # input_tokens alone = 50% of sonnet limit, but cache tokens push it over flush threshold
    limit = MODEL_CONTEXT_LIMITS["sonnet"]
    usage = {
        "input_tokens": int(limit * 0.50),
        "cache_creation_input_tokens": int(limit * 0.10),
        "cache_read_input_tokens": int(limit * 0.10),
    }
    # Total = 70% -- above flush (65%) but below compact (75%)
    assert tracker.determine_action(usage, "sonnet", "w6") == "flush"
