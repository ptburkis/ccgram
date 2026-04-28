"""Tests for cron_runner.fire_cron -- dispatch with all four target branches."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from ccgram import store
from ccgram.cron_runner import fire_cron


# ---- Helpers -----------------------------------------------------------------


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    store.init_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _add_session(conn, sid="sid-1", window_id="@1"):
    store.upsert_session(
        conn,
        session_id=sid,
        cwd="/cwd",
        agent="claude",
        status="active",
        window_id=window_id,
        created_at=0,
    )
    conn.commit()


def _add_binding(conn, sid="sid-1", group_id=1, topic_id=10):
    store.upsert_topic_binding(
        conn,
        group_id=group_id,
        topic_id=topic_id,
        session_id=sid,
        topic_title="T",
        bound_at=0,
    )
    conn.commit()


def _cron(**kwargs) -> store.Cron:
    defaults = dict(
        id=1,
        name="test",
        schedule="0 * * * *",
        target_window="",
        message="hello",
        enabled=True,
        last_run=None,
        last_result=None,
        created_at=0,
        target_session_id=None,
        target_topic_id=None,
        target_group_id=None,
    )
    defaults.update(kwargs)
    return store.Cron(**defaults)


def _run(coro):
    return asyncio.run(coro)


_sent: list[tuple[str, str]] = []


async def _mock_send(target: str, message: str) -> None:
    _sent.append((target, message))


# ---- Tests -------------------------------------------------------------------


class TestFireCronBySessionId:
    def test_dispatches_via_session_id(self, tmp_path):
        conn = _make_conn(tmp_path)
        _add_session(conn, "sid-1", "@1")
        cron = _cron(target_session_id="sid-1")
        _sent.clear()
        result = _run(fire_cron(cron, conn, "ccgram", send_keys_fn=_mock_send))
        assert result["fired"] is True
        assert result["source"] == "session_id"
        assert result["window_id"] == "@1"
        assert len(_sent) == 1
        assert _sent[0] == ("ccgram:@1", "hello")


class TestFireCronByTopicId:
    def test_dispatches_via_topic_id_when_no_session_id(self, tmp_path):
        conn = _make_conn(tmp_path)
        _add_session(conn, "sid-2", "@2")
        _add_binding(conn, "sid-2", group_id=5, topic_id=99)
        cron = _cron(target_group_id=5, target_topic_id=99)
        _sent.clear()
        result = _run(fire_cron(cron, conn, "ccgram", send_keys_fn=_mock_send))
        assert result["fired"] is True
        assert result["source"] == "topic_id"
        assert result["window_id"] == "@2"

    def test_session_id_takes_priority_over_topic_id(self, tmp_path):
        conn = _make_conn(tmp_path)
        _add_session(conn, "sid-1", "@1")
        _add_session(conn, "sid-2", "@2")
        _add_binding(conn, "sid-2", group_id=5, topic_id=99)
        cron = _cron(target_session_id="sid-1", target_group_id=5, target_topic_id=99)
        _sent.clear()
        result = _run(fire_cron(cron, conn, "ccgram", send_keys_fn=_mock_send))
        assert result["source"] == "session_id"
        assert result["window_id"] == "@1"


class TestFireCronLegacyWindow:
    def test_legacy_fires_with_warning(self, tmp_path, caplog):
        conn = _make_conn(tmp_path)
        cron = _cron(target_window="my-window")
        _sent.clear()
        with caplog.at_level(logging.WARNING, logger="ccgram.cron_runner"):
            result = _run(fire_cron(cron, conn, "ccgram", send_keys_fn=_mock_send))
        assert result["fired"] is True
        assert result["source"] == "legacy_window"
        assert result["window_id"] == "my-window"
        assert any("legacy" in r.message for r in caplog.records)


class TestFireCronNoTarget:
    def test_returns_no_target_when_all_empty(self, tmp_path):
        conn = _make_conn(tmp_path)
        cron = _cron(target_window="")
        result = _run(fire_cron(cron, conn, "ccgram", send_keys_fn=_mock_send))
        assert result["fired"] is False
        assert result["error"] == "no_target"


# ---- TestDefaultSendKeys -----------------------------------------------------


class TestDefaultSendKeys:
    """Bracketed-paste path: load-buffer -> paste-buffer -p -d -> Enter."""

    @staticmethod
    def _proc(returncode=0):
        from unittest.mock import AsyncMock
        p = AsyncMock()
        p.communicate = AsyncMock(return_value=(b"", b""))
        p.returncode = returncode
        return p

    def test_calls_load_then_paste_then_enter(self):
        """Single-line message: load-buffer, paste-buffer -p -d, send-keys Enter."""
        from unittest.mock import patch, AsyncMock
        from ccgram.cron_runner import _default_send_keys

        procs = []

        async def fake_exec(*args, **kwargs):
            p = self._proc()
            procs.append((args, kwargs, p))
            return p

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            asyncio.run(_default_send_keys("ccgram:@1", "hello"))

        assert len(procs) == 3

        load_args, load_kwargs, load_proc = procs[0]
        assert load_args[:2] == ("tmux", "load-buffer")
        assert load_args[2] == "-b"
        buf_name = load_args[3]
        assert buf_name.startswith("ccgram-cron-")
        assert load_args[4] == "-"
        assert load_kwargs.get("stdin") == asyncio.subprocess.PIPE
        load_proc.communicate.assert_awaited_once_with(input=b"hello")

        paste_args, _, _ = procs[1]
        assert paste_args == ("tmux", "paste-buffer", "-t", "ccgram:@1", "-b", buf_name, "-p", "-d")

        enter_args, _, _ = procs[2]
        assert enter_args == ("tmux", "send-keys", "-t", "ccgram:@1", "Enter")

    def test_multi_line_uses_same_path(self):
        """Multi-line message uses the same 3-step sequence, no extra Enter."""
        from unittest.mock import patch, AsyncMock
        from ccgram.cron_runner import _default_send_keys

        procs = []

        async def fake_exec(*args, **kwargs):
            p = self._proc()
            procs.append((args, kwargs, p))
            return p

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            asyncio.run(_default_send_keys("ccgram:@1", "line1\nline2\nline3"))

        assert len(procs) == 3

        load_args, load_kwargs, load_proc = procs[0]
        buf_name = load_args[3]
        assert buf_name.startswith("ccgram-cron-")
        load_proc.communicate.assert_awaited_once_with(input=b"line1\nline2\nline3")

        paste_args, _, _ = procs[1]
        assert paste_args == ("tmux", "paste-buffer", "-t", "ccgram:@1", "-b", buf_name, "-p", "-d")

        enter_args, _, _ = procs[2]
        assert enter_args == ("tmux", "send-keys", "-t", "ccgram:@1", "Enter")
