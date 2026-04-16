"""Tests for hook_events — SessionStart primary/subagent filtering and
StopFailure intentional-stop suppression."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ccgram.providers.base import HookEvent


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_event(
    event_type: str = "SessionStart",
    window_key: str = "ccgram:@5",
    session_id: str = "sid-0001",
    data: dict | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        window_key=window_key,
        session_id=session_id,
        data=data or {},
        timestamp=time.time(),
    )


# ── SessionStart: primary vs subagent detection ────────────────────────────────


class TestIsPrimarySessionStart:
    def test_primary_no_parent_no_agents_dir(self):
        from ccgram.handlers.hook_events import _is_primary_session_start

        event = _make_event(
            data={
                "cwd": "/home/peter/projects/james",
                "transcript_path": "/home/peter/.claude/projects/-home-peter-projects-james/sid-0001.jsonl",
            }
        )
        assert _is_primary_session_start(event) is True

    def test_subagent_has_parent_session_id(self):
        from ccgram.handlers.hook_events import _is_primary_session_start

        event = _make_event(
            data={
                "parent_session_id": "parent-sid-xxxx",
                "transcript_path": "/home/peter/.claude/projects/-home-peter-projects-james/sid-0002.jsonl",
            }
        )
        assert _is_primary_session_start(event) is False

    def test_subagent_agents_dir_in_path(self):
        from ccgram.handlers.hook_events import _is_primary_session_start

        event = _make_event(
            data={
                "transcript_path": "/home/peter/.claude/projects/-home-peter-projects-james/agents/parent-sid/child-sid.jsonl",
            }
        )
        assert _is_primary_session_start(event) is False

    def test_primary_empty_transcript(self):
        from ccgram.handlers.hook_events import _is_primary_session_start

        event = _make_event(data={"cwd": "/home/peter/projects/james"})
        assert _is_primary_session_start(event) is True


# ── SessionStart: silent state update (no Telegram post) ──────────────────────


class TestSessionStartSilent:
    OLD_SID = "old-sid-0000-0000-0000-000000000000"
    NEW_SID = "new-sid-1111-1111-1111-111111111111"
    WINDOW_ID = "@5"
    CWD = "/home/peter/projects/james"

    def _seed_window_store(self) -> None:
        from ccgram.window_state_store import window_store

        state = window_store.get_window_state(self.WINDOW_ID)
        state.session_id = self.OLD_SID
        state.cwd = self.CWD

    def _seed_session_map(self, ccgram_dir: Path) -> None:
        from ccgram import config as cfg_module

        sm = cfg_module.config.session_map_file
        sm.parent.mkdir(parents=True, exist_ok=True)
        sm.write_text(
            json.dumps(
                {
                    f"ccgram:{self.WINDOW_ID}": {
                        "session_id": self.OLD_SID,
                        "cwd": self.CWD,
                        "window_name": "james",
                        "transcript_path": f".../{self.OLD_SID}.jsonl",
                        "provider_name": "claude",
                    }
                }
            )
        )
        ms = cfg_module.config.monitor_state_file
        ms.parent.mkdir(parents=True, exist_ok=True)
        ms.write_text(json.dumps({"tracked_sessions": {}}))

    @pytest.fixture()
    def ccgram_dir(self, tmp_path, monkeypatch) -> Path:
        from ccgram import config as cfg_module

        d = tmp_path / ".ccgram"
        d.mkdir()
        monkeypatch.setattr(cfg_module.config, "config_dir", d)
        monkeypatch.setattr(cfg_module.config, "session_map_file", d / "session_map.json")
        monkeypatch.setattr(cfg_module.config, "monitor_state_file", d / "monitor_state.json")
        return d

    @pytest.fixture()
    def db_path(self, ccgram_dir) -> Path:
        from ccgram.store import init_db

        p = ccgram_dir / "state.db"
        init_db(p)
        return p

    @pytest.mark.asyncio()
    async def test_state_updated_no_telegram_post(self, ccgram_dir, db_path, monkeypatch):
        """Primary rotation: state is updated, no Telegram message sent."""
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()

        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([(12345, 99, self.WINDOW_ID)]),
        )
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.resolve_chat_id",
            lambda uid, tid: -100111,
        )

        bot = AsyncMock()
        send_mock = AsyncMock()

        from ccgram.handlers.hook_events import _handle_session_start, _rotation_notice_last

        _rotation_notice_last.clear()

        event = _make_event(
            session_id=self.NEW_SID,
            data={
                "cwd": self.CWD,
                "transcript_path": f"/home/peter/.claude/projects/-home-peter-projects-james/{self.NEW_SID}.jsonl",
            },
        )

        with (
            patch("ccgram.handlers.hook_events._ROTATION_NOTICES_ENABLED", False),
            patch("ccgram.session_autoheal.apply_session_update", new_callable=AsyncMock),
            patch("ccgram.session.session_manager.load_session_map", new_callable=AsyncMock),
            patch("ccgram.handlers.message_sender.rate_limit_send_message", send_mock),
        ):
            await _handle_session_start(event, bot)

        send_mock.assert_not_called()

    @pytest.mark.asyncio()
    async def test_subagent_start_skipped_entirely(self, ccgram_dir, db_path, monkeypatch):
        """Subagent SessionStart is dropped before any state update."""
        self._seed_session_map(ccgram_dir)
        self._seed_window_store()

        bot = AsyncMock()
        apply_mock = AsyncMock()

        from ccgram.handlers.hook_events import _handle_session_start

        event = _make_event(
            session_id="subagent-sid-xxxx",
            data={
                "parent_session_id": "parent-sid-yyyy",
                "transcript_path": "/home/peter/.claude/projects/-home-peter-projects-james/agents/parent-sid-yyyy/subagent-sid-xxxx.jsonl",
            },
        )

        with patch("ccgram.session_autoheal.apply_session_update", apply_mock):
            await _handle_session_start(event, bot)

        apply_mock.assert_not_called()


# ── StopFailure: intentional-stop suppression ─────────────────────────────────


class TestStopFailureIntentionalStop:
    WINDOW_ID = "@7"

    @pytest.fixture()
    def stop_file(self, tmp_path, monkeypatch) -> Path:
        f = tmp_path / "intentional-stops.json"
        monkeypatch.setattr(
            "ccgram.handlers.hook_events._INTENTIONAL_STOP_FILE", f
        )
        return f

    @pytest.mark.asyncio()
    async def test_suppressed_when_marker_present(self, stop_file, monkeypatch):
        """StopFailure for a window with a fresh intentional-stop marker fires no alert."""
        stop_file.write_text(
            json.dumps({self.WINDOW_ID: time.time() + 10.0})
        )

        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([(12345, 99, self.WINDOW_ID)]),
        )

        bot = AsyncMock()
        send_mock = AsyncMock()
        event = _make_event(
            event_type="StopFailure",
            window_key=f"ccgram:{self.WINDOW_ID}",
            data={},
        )

        from ccgram.handlers.hook_events import _handle_stop_failure

        with patch("ccgram.handlers.message_sender.rate_limit_send_message", send_mock):
            await _handle_stop_failure(event, bot)

        send_mock.assert_not_called()

    @pytest.mark.asyncio()
    async def test_empty_payload_suppressed_entirely(self, stop_file, monkeypatch):
        """StopFailure with empty payload and no marker → suppressed (no send)."""
        # Empty error AND empty error_details → nothing actionable, log only.
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([(12345, 99, self.WINDOW_ID)]),
        )
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.resolve_chat_id",
            lambda uid, tid: -100111,
        )

        bot = AsyncMock()
        send_mock = AsyncMock()
        event = _make_event(
            event_type="StopFailure",
            window_key=f"ccgram:{self.WINDOW_ID}",
            data={},
        )

        from ccgram.handlers.hook_events import _handle_stop_failure

        with patch("ccgram.handlers.message_sender.rate_limit_send_message", send_mock):
            await _handle_stop_failure(event, bot)

        send_mock.assert_not_called()

    @pytest.mark.asyncio()
    async def test_fires_api_error_text_when_error_field_present(self, stop_file, monkeypatch):
        """When error field is non-empty, the alert uses the 'API error' format."""
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.iter_thread_bindings",
            lambda: iter([(12345, 99, self.WINDOW_ID)]),
        )
        monkeypatch.setattr(
            "ccgram.handlers.hook_events.thread_router.resolve_chat_id",
            lambda uid, tid: -100111,
        )

        bot = AsyncMock()
        send_mock = AsyncMock()
        event = _make_event(
            event_type="StopFailure",
            window_key=f"ccgram:{self.WINDOW_ID}",
            data={"error": "rate_limit_error", "error_details": "Too many requests"},
        )

        from ccgram.handlers.hook_events import _handle_stop_failure

        with patch("ccgram.handlers.message_sender.rate_limit_send_message", send_mock):
            await _handle_stop_failure(event, bot)

        send_mock.assert_called_once()
        args = send_mock.call_args[0]
        assert "rate_limit_error" in args[2]
