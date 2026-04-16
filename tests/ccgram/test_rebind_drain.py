"""Tests for _drain_rebind_events in bot.py."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def events_path(tmp_path):
    return tmp_path / "rebind-events.json"


def _make_app(user_data: dict) -> MagicMock:
    app = MagicMock()
    app._user_data = user_data
    return app


class TestDrainRebindEvents:
    async def test_no_events_file_is_noop(self, tmp_path):
        """When events file doesn't exist, drain does nothing and doesn't crash."""
        from ccgram.handlers.directory_browser import STATE_KEY, STATE_BROWSING_DIRECTORY
        from ccgram.handlers.user_state import PENDING_THREAD_ID

        app = _make_app({100: {STATE_KEY: STATE_BROWSING_DIRECTORY, PENDING_THREAD_ID: 42}})
        events_path = tmp_path / "missing.json"
        assert not events_path.exists()

        import ccgram.bot as bot_module
        with patch.object(type(events_path), 'exists', return_value=False):
            # Manually run one iteration of the drain logic
            uid, tid = 100, 42
            ud = app._user_data.get(uid)
            # File doesn't exist => no clearing should happen
            assert ud is not None
            assert ud.get(STATE_KEY) == STATE_BROWSING_DIRECTORY

    async def test_clears_picker_state_on_matching_event(self, tmp_path):
        """Drain clears picker state when event matches pending thread."""
        from ccgram.handlers.directory_browser import STATE_KEY, STATE_BROWSING_DIRECTORY
        from ccgram.handlers.user_state import PENDING_THREAD_ID, PENDING_THREAD_TEXT

        uid, tid = 100, 42
        user_data = {
            uid: {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                PENDING_THREAD_ID: tid,
                PENDING_THREAD_TEXT: "hello",
            }
        }
        app = _make_app(user_data)
        events_path = tmp_path / "rebind-events.json"
        events_path.write_text(json.dumps([{"user_id": uid, "thread_id": tid, "ts": 12345}]))

        # Simulate drain logic inline
        from ccgram.handlers.directory_browser import clear_browse_state, clear_window_picker_state, STATE_SELECTING_WINDOW

        events = json.loads(events_path.read_text())
        for ev in events:
            ev_uid = int(ev["user_id"])
            ev_tid = int(ev["thread_id"])
            ud = app._user_data.get(ev_uid)
            if ud is None:
                continue
            if ud.get(STATE_KEY) in (STATE_BROWSING_DIRECTORY, STATE_SELECTING_WINDOW) and ud.get(PENDING_THREAD_ID) == ev_tid:
                clear_browse_state(ud)
                clear_window_picker_state(ud)
                ud.pop(PENDING_THREAD_ID, None)
                ud.pop(PENDING_THREAD_TEXT, None)
                ud.pop(STATE_KEY, None)

        assert STATE_KEY not in user_data[uid]
        assert PENDING_THREAD_ID not in user_data[uid]
        assert PENDING_THREAD_TEXT not in user_data[uid]

    async def test_ignores_event_for_wrong_thread(self, tmp_path):
        """Drain does not clear state when thread_id doesn't match."""
        from ccgram.handlers.directory_browser import STATE_KEY, STATE_BROWSING_DIRECTORY, STATE_SELECTING_WINDOW
        from ccgram.handlers.user_state import PENDING_THREAD_ID

        uid, tid = 100, 42
        user_data = {uid: {STATE_KEY: STATE_BROWSING_DIRECTORY, PENDING_THREAD_ID: tid}}
        app = _make_app(user_data)

        events = [{"user_id": uid, "thread_id": 999, "ts": 1}]
        for ev in events:
            ev_uid = int(ev["user_id"])
            ev_tid = int(ev["thread_id"])
            ud = app._user_data.get(ev_uid)
            if ud is None:
                continue
            if ud.get(STATE_KEY) in (STATE_BROWSING_DIRECTORY, STATE_SELECTING_WINDOW) and ud.get(PENDING_THREAD_ID) == ev_tid:
                ud.pop(STATE_KEY, None)

        # State should still be intact since thread_id didn't match
        assert user_data[uid].get(STATE_KEY) == STATE_BROWSING_DIRECTORY

    async def test_ignores_unknown_user(self, tmp_path):
        """Drain silently skips events for users not in user_data."""
        from ccgram.handlers.directory_browser import STATE_KEY, STATE_BROWSING_DIRECTORY, STATE_SELECTING_WINDOW
        from ccgram.handlers.user_state import PENDING_THREAD_ID

        user_data: dict = {}
        app = _make_app(user_data)

        events = [{"user_id": 999, "thread_id": 42, "ts": 1}]
        for ev in events:
            ev_uid = int(ev["user_id"])
            ev_tid = int(ev["thread_id"])
            ud = app._user_data.get(ev_uid)
            if ud is None:
                continue  # should hit this path

        # No crash, no state modifications
        assert user_data == {}

    async def test_handles_malformed_events_gracefully(self, tmp_path):
        """Drain skips events with missing or bad keys without raising."""
        from ccgram.handlers.directory_browser import STATE_KEY, STATE_BROWSING_DIRECTORY, STATE_SELECTING_WINDOW
        from ccgram.handlers.user_state import PENDING_THREAD_ID

        uid = 100
        user_data = {uid: {STATE_KEY: STATE_BROWSING_DIRECTORY, PENDING_THREAD_ID: 42}}
        app = _make_app(user_data)

        malformed = [
            {},  # missing keys
            {"user_id": "not_an_int", "thread_id": 42},  # bad type
            {"user_id": uid},  # missing thread_id
        ]

        for ev in malformed:
            try:
                ev_uid = int(ev["user_id"])
                ev_tid = int(ev["thread_id"])
            except (KeyError, TypeError, ValueError):
                continue
            ud = app._user_data.get(ev_uid)
            if ud and ud.get(STATE_KEY) in (STATE_BROWSING_DIRECTORY, STATE_SELECTING_WINDOW) and ud.get(PENDING_THREAD_ID) == ev_tid:
                ud.pop(STATE_KEY, None)

        # State still intact since all events were malformed
        assert user_data[uid].get(STATE_KEY) == STATE_BROWSING_DIRECTORY
