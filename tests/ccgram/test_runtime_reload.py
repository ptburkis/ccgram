"""Tests for SessionMonitor._reload_topic_bindings_from_db."""

import sqlite3
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from ccgram.session_monitor import SessionMonitor
from ccgram.store import Session, TopicBinding


@pytest.fixture
def monitor(tmp_path) -> SessionMonitor:
    return SessionMonitor(
        projects_path=tmp_path / "projects",
        poll_interval=0.1,
        state_file=tmp_path / "state.json",
    )


def _binding(group_id: int = 100, topic_id: int = 10, session_id: str = "sess1") -> TopicBinding:
    return TopicBinding(
        group_id=group_id,
        topic_id=topic_id,
        session_id=session_id,
        topic_title="Test Topic",
        bound_at=0,
    )


def _session(session_id: str = "sess1", window_id: str = "win1") -> Session:
    return Session(
        session_id=session_id,
        cwd="/tmp/test",
        agent="claude",
        mode=None,
        status="active",
        window_id=window_id,
        created_at=0,
        updated_at=0,
    )


@contextmanager
def _store_connect_ctx():
    yield MagicMock()


class TestRuntimeReload:
    async def test_new_binding_added(self, monitor: SessionMonitor) -> None:
        """DB returns a new binding not in thread_router -> bind_thread called."""
        binding = _binding(group_id=100, topic_id=10, session_id="sess1")
        session = _session(session_id="sess1", window_id="win1")
        # gchat_rows: (scope_id, key="uid:tid", value=group_id)
        # uid=1, tid=10, gid=100 → gid_tid_to_uid[(100, 10)] = 1
        gchat_rows = [("", "1:10", 100)]

        mock_tr = MagicMock()
        mock_tr.thread_bindings = {}

        with (
            patch("ccgram.store.connect", _store_connect_ctx),
            patch("ccgram.store.list_topic_bindings", return_value=[binding]),
            patch("ccgram.store.list_prefs", return_value=gchat_rows),
            patch("ccgram.store.list_sessions", return_value=[session]),
            patch("ccgram.thread_router.thread_router", mock_tr),
        ):
            monitor._last_db_reload = 0.0
            await monitor._reload_topic_bindings_from_db()

        mock_tr.bind_thread.assert_called_once_with(1, 10, "win1")

    async def test_existing_binding_unchanged(self, monitor: SessionMonitor) -> None:
        """DB returns binding already in thread_router -> bind_thread NOT called."""
        binding = _binding(group_id=100, topic_id=10, session_id="sess1")
        session = _session(session_id="sess1", window_id="win1")
        gchat_rows = [("", "1:10", 100)]

        mock_tr = MagicMock()
        # thread_router already has (uid=1, tid=10) -> "win1"
        mock_tr.thread_bindings = {1: {10: "win1"}}

        with (
            patch("ccgram.store.connect", _store_connect_ctx),
            patch("ccgram.store.list_topic_bindings", return_value=[binding]),
            patch("ccgram.store.list_prefs", return_value=gchat_rows),
            patch("ccgram.store.list_sessions", return_value=[session]),
            patch("ccgram.thread_router.thread_router", mock_tr),
        ):
            monitor._last_db_reload = 0.0
            await monitor._reload_topic_bindings_from_db()

        mock_tr.bind_thread.assert_not_called()

    async def test_stale_binding_dropped(self, monitor: SessionMonitor) -> None:
        """thread_router has binding (user=1, topic=99) not in DB desired -> unbind_thread called."""
        mock_tr = MagicMock()
        # In-memory has (uid=1, tid=99) but DB has nothing
        mock_tr.thread_bindings = {1: {99: "some_win"}}

        with (
            patch("ccgram.store.connect", _store_connect_ctx),
            patch("ccgram.store.list_topic_bindings", return_value=[]),
            patch("ccgram.store.list_prefs", return_value=[]),
            patch("ccgram.store.list_sessions", return_value=[]),
            patch("ccgram.thread_router.thread_router", mock_tr),
        ):
            monitor._last_db_reload = 0.0
            await monitor._reload_topic_bindings_from_db()

        mock_tr.unbind_thread.assert_called_once_with(1, 99)

    async def test_db_error_is_swallowed(self, monitor: SessionMonitor) -> None:
        """store.connect raising DatabaseError -> no exception propagates."""
        def failing_connect(*args, **kwargs):
            raise sqlite3.DatabaseError("connection refused")

        mock_tr = MagicMock()
        mock_tr.thread_bindings = {}

        with (
            patch("ccgram.store.connect", failing_connect),
            patch("ccgram.thread_router.thread_router", mock_tr),
        ):
            monitor._last_db_reload = 0.0
            # Must not raise
            await monitor._reload_topic_bindings_from_db()

        mock_tr.bind_thread.assert_not_called()
        mock_tr.unbind_thread.assert_not_called()

    async def test_interval_respected(self, monitor: SessionMonitor) -> None:
        """Called twice within the reload interval -> DB queried only once."""
        binding = _binding()
        session = _session()
        gchat_rows = [("", "1:10", 100)]

        mock_tr = MagicMock()
        mock_tr.thread_bindings = {}
        list_tb = MagicMock(return_value=[binding])

        with (
            patch("ccgram.store.connect", _store_connect_ctx),
            patch("ccgram.store.list_topic_bindings", list_tb),
            patch("ccgram.store.list_prefs", return_value=gchat_rows),
            patch("ccgram.store.list_sessions", return_value=[session]),
            patch("ccgram.thread_router.thread_router", mock_tr),
        ):
            # First call: _last_db_reload=0 so interval check passes, DB is queried
            monitor._last_db_reload = 0.0
            await monitor._reload_topic_bindings_from_db()

            # Second call: _last_db_reload was just set to ~now, so interval not elapsed
            await monitor._reload_topic_bindings_from_db()

        # DB should only have been hit once despite two method calls
        list_tb.assert_called_once()
