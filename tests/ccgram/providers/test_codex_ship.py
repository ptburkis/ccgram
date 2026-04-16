"""Tests for Codex outbound ship path — dedup, window_id propagation, fallback routing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ccgram.providers.codex import CodexProvider
from ccgram.session_monitor import NewMessage, SessionMonitor


# ── Test 1: parse_transcript_entries dedup ────────────────────────────────────


def test_codex_parse_dedup_event_and_response_item() -> None:
    """Both event_msg and response_item carry the same text — only one AgentMessage."""
    entries = [
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "Fine. What do you need?",
                "phase": "final_answer",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fine. What do you need?"}],
                "phase": "final_answer",
            },
        },
    ]
    provider = CodexProvider()
    messages, _ = provider.parse_transcript_entries(entries, {})

    assert len(messages) == 1
    assert messages[0].text == "Fine. What do you need?"
    assert messages[0].role == "assistant"


# ── Test 2: _process_session_file sets window_id on emitted NewMessage ────────


def test_process_session_file_sets_window_id(tmp_path: Path) -> None:
    """NewMessage emitted from _process_session_file carries the window_id."""
    # Write a minimal Codex rollout JSONL
    jsonl = tmp_path / "rollout.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "019d9311-fake", "cwd": str(tmp_path)}}),
        json.dumps({
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Fine. What do you need?"},
        }),
    ]
    jsonl.write_text("\n".join(lines) + "\n")

    monitor = SessionMonitor.__new__(SessionMonitor)
    monitor.state = MagicMock()
    monitor.state.get_session.return_value = None
    monitor.state.update_session = MagicMock()
    monitor._file_mtimes = {}
    monitor._pending_tools = {}
    monitor._last_session_map = {}

    new_messages: list[NewMessage] = []

    with (
        patch("ccgram.session_monitor.get_provider_for_window") as mock_prov,
        patch("ccgram.session_monitor.detect_provider_from_transcript_path"),
        patch("ccgram.session_monitor.claude_task_state"),
    ):
        from ccgram.providers.codex import CodexProvider
        mock_prov.return_value = CodexProvider()

        asyncio.get_event_loop().run_until_complete(
            monitor._process_session_file(
                "019d9311-fake",
                jsonl,
                new_messages,
                window_id="@412",
            )
        )

    # The session was new so only the catchup notice should be emitted,
    # OR the message itself — either way window_id must be set.
    assert all(m.window_id == "@412" for m in new_messages), (
        f"Expected all messages to have window_id='@412', got: {new_messages}"
    )


# ── Test 3: router routes correctly on ccgram session_id (no hint needed) ─────


def test_find_users_fallback_routing() -> None:
    """After the split-ids refactor, routing works on ccgram session_id directly.

    Previously this test verified the window_id_hint fallback. Now that
    register_hookless_session stores the provider UUID in provider_session_id
    and preserves the ccgram DB session_id in session_id, the router sees the
    correct ccgram session_id in window_states and no hint is needed.
    """
    from ccgram.session_resolver import SessionResolver

    resolver = SessionResolver.__new__(SessionResolver)

    fake_state = MagicMock()
    # Now session_id holds the ccgram routing id (set by session_lifecycle)
    fake_state.session_id = "ccgram-uuid"

    fake_window_store = MagicMock()
    fake_window_store.window_states = {"@412": fake_state}

    fake_thread_router = MagicMock()
    # user_id=42, thread_id=100, window_id="@412"
    fake_thread_router.iter_thread_bindings.return_value = [(42, 100, "@412")]

    with (
        patch("ccgram.session_resolver.window_store", fake_window_store),
        patch("ccgram.session_resolver.thread_router", fake_thread_router),
    ):
        # No window_id_hint needed: session_id is now the ccgram routing id
        result = resolver.find_users_for_session("ccgram-uuid")

    assert result == [(42, "@412", 100)]
