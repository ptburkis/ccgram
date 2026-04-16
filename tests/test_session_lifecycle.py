"""Tests for ccgram.session_lifecycle — transactional session create/delete.

All tests run offline: real tmux, bot, and MTProto clients are replaced by
``unittest.mock`` stubs. Each test gets an isolated SQLite DB via the
``ccgram_test_dir`` fixture (sets ``CCGRAM_DIR`` to a tmp path).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccgram import session_lifecycle, store


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def ccgram_test_dir(tmp_path, monkeypatch):
    """Redirect ccgram_dir() to a fresh tmp directory for this test."""
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    # Pre-create the DB so store.connect() doesn't race.
    store.init_db(tmp_path / "state.db")
    return tmp_path


@pytest.fixture()
def stubs(monkeypatch):
    """Wire stub dependencies into session_lifecycle module-level callables.

    Returns a SimpleNamespace-like object exposing each mock so individual
    tests can assert call counts / override return values.
    """

    class _Stubs:
        def __init__(self) -> None:
            # Default happy-path stubs — tests override as needed.
            self.create_topic = AsyncMock(return_value=42)
            self.delete_topic = AsyncMock(return_value=None)
            self.verify_topic = AsyncMock(return_value=(True, "expected-title"))
            self.tmux_create = AsyncMock(return_value="@99")
            self.tmux_send = AsyncMock(return_value=None)
            self.tmux_kill = AsyncMock(return_value=None)
            self.resolve_launch = MagicMock(return_value="claude")

    s = _Stubs()
    monkeypatch.setattr(session_lifecycle, "_create_forum_topic_fn", s.create_topic)
    monkeypatch.setattr(session_lifecycle, "_delete_forum_topic_fn", s.delete_topic)
    monkeypatch.setattr(session_lifecycle, "_verify_forum_topic_fn", s.verify_topic)
    monkeypatch.setattr(session_lifecycle, "_tmux_create_window_fn", s.tmux_create)
    monkeypatch.setattr(session_lifecycle, "_tmux_send_keys_fn", s.tmux_send)
    monkeypatch.setattr(session_lifecycle, "_tmux_kill_window_fn", s.tmux_kill)
    monkeypatch.setattr(session_lifecycle, "_resolve_launch_fn", s.resolve_launch)
    return s


# ---- create_session: happy paths --------------------------------------------


class TestCreateSessionHappyPath:
    @pytest.mark.asyncio
    async def test_new_topic_flow(self, ccgram_test_dir, stubs):
        session_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="proj-x",
            agent="claude",
            mode="yolo",
            group_id=-1001,
        )

        assert isinstance(session_id, str) and len(session_id) == 36
        stubs.create_topic.assert_awaited_once_with(-1001, "proj-x")
        stubs.tmux_create.assert_awaited_once_with(str(ccgram_test_dir), "proj-x")
        stubs.tmux_send.assert_awaited_once()
        sent_cmd = stubs.tmux_send.await_args.args[1]
        assert f"CCGRAM_SESSION_ID={session_id}" in sent_cmd
        assert "claude" in sent_cmd
        stubs.verify_topic.assert_not_called()

        with store.connect() as conn:
            sess = store.get_session(conn, session_id)
            binding = store.get_binding_for_session(conn, session_id)

        assert sess is not None
        assert sess.status == "active"
        assert sess.window_id == "@99"
        assert sess.agent == "claude"
        assert sess.mode == "yolo"
        assert binding is not None
        assert binding.group_id == -1001
        assert binding.topic_id == 42
        assert binding.topic_title == "proj-x"

    @pytest.mark.asyncio
    async def test_existing_topic_reuse_title_match(self, ccgram_test_dir, stubs):
        stubs.verify_topic.return_value = (True, "reused-topic")

        session_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="reused-topic",
            agent="codex",
            group_id=-1002,
            existing_topic_id=77,
        )

        stubs.verify_topic.assert_awaited_once_with(-1002, 77)
        stubs.create_topic.assert_not_called()

        with store.connect() as conn:
            binding = store.get_binding_for_session(conn, session_id)
        assert binding is not None
        assert binding.topic_id == 77

    @pytest.mark.asyncio
    async def test_marker_file_written(
        self, ccgram_test_dir, stubs, monkeypatch, tmp_path
    ):
        monkeypatch.setattr("ccgram.session_lifecycle.Path.home", lambda: tmp_path)

        session_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="marker-test",
            agent="claude",
            group_id=-1001,
        )

        marker = tmp_path / ".ccgram" / "debug" / "terminal-@99.sid"
        assert marker.exists()
        assert marker.read_text() == session_id
        assert (marker.stat().st_mode & 0o777) == 0o600


# ---- create_session: existing-topic failures --------------------------------


class TestCreateSessionExistingTopicFailures:
    @pytest.mark.asyncio
    async def test_title_mismatch_raises_and_no_binding(self, ccgram_test_dir, stubs):
        stubs.verify_topic.return_value = (True, "different-title")

        with pytest.raises(session_lifecycle.TopicVerificationError):
            await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="expected-title",
                agent="claude",
                group_id=-1001,
                existing_topic_id=55,
            )

        stubs.create_topic.assert_not_called()
        stubs.tmux_create.assert_not_called()
        stubs.tmux_kill.assert_not_called()
        stubs.delete_topic.assert_not_called()  # we didn't create it; don't delete

        with store.connect() as conn:
            bindings = store.list_topic_bindings(conn)
            sessions = store.list_sessions(conn)
        assert bindings == []
        assert len(sessions) == 1
        assert sessions[0].status == "errored"

    @pytest.mark.asyncio
    async def test_topic_does_not_exist_raises(self, ccgram_test_dir, stubs):
        stubs.verify_topic.return_value = (False, "")

        with pytest.raises(session_lifecycle.TopicVerificationError):
            await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="whatever",
                agent="claude",
                group_id=-1001,
                existing_topic_id=9999,
            )

        stubs.delete_topic.assert_not_called()


# ---- create_session: mid-flow failures --------------------------------------


class TestCreateSessionRollback:
    @pytest.mark.asyncio
    async def test_tmux_create_fails_deletes_topic(self, ccgram_test_dir, stubs):
        stubs.tmux_create.side_effect = RuntimeError("tmux boom")

        with pytest.raises(RuntimeError, match="tmux boom"):
            await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="doomed",
                agent="claude",
                group_id=-1001,
            )

        # Topic was created by us → must be deleted.
        stubs.create_topic.assert_awaited_once()
        stubs.delete_topic.assert_awaited_once_with(-1001, 42)
        stubs.tmux_kill.assert_not_called()  # no window to kill

        with store.connect() as conn:
            sessions = store.list_sessions(conn)
            bindings = store.list_topic_bindings(conn)
        assert len(sessions) == 1
        assert sessions[0].status == "errored"
        assert sessions[0].window_id is None
        assert bindings == []

    @pytest.mark.asyncio
    async def test_tmux_create_fails_does_not_delete_reused_topic(
        self, ccgram_test_dir, stubs
    ):
        stubs.verify_topic.return_value = (True, "reused")
        stubs.tmux_create.side_effect = RuntimeError("tmux boom")

        with pytest.raises(RuntimeError):
            await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="reused",
                agent="claude",
                group_id=-1001,
                existing_topic_id=88,
            )

        # We didn't create the topic → must NOT delete it.
        stubs.delete_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_launch_fails_kills_window_and_deletes_topic(
        self, ccgram_test_dir, stubs
    ):
        stubs.tmux_send.side_effect = RuntimeError("send_keys boom")

        with pytest.raises(session_lifecycle.AgentLaunchError):
            await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="launch-fail",
                agent="claude",
                group_id=-1001,
            )

        stubs.tmux_kill.assert_awaited_once_with("@99")
        stubs.delete_topic.assert_awaited_once_with(-1001, 42)

        with store.connect() as conn:
            sessions = store.list_sessions(conn)
            bindings = store.list_topic_bindings(conn)
        assert sessions[0].status == "errored"
        assert bindings == []

    @pytest.mark.asyncio
    async def test_duplicate_topic_binding_rolls_back(self, ccgram_test_dir, stubs):
        """Two create_session() calls both resolve to the same (group_id, topic_id).

        The second attempt must raise ``sqlite3.IntegrityError`` (PK violation
        on topic_bindings) AND clean up the window + topic it created.
        """
        first_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="dup",
            agent="claude",
            group_id=-1001,
        )

        # Second call — same topic_id 42 returned by stub → PK violation on
        # (group_id, topic_id). Reset mocks so we only see the second call's
        # side-effects.
        stubs.tmux_create.return_value = "@100"
        stubs.delete_topic.reset_mock()
        stubs.tmux_kill.reset_mock()

        with pytest.raises(sqlite3.IntegrityError):
            await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="dup",
                agent="claude",
                group_id=-1001,
            )

        # Second call created a new topic & window → both cleaned up.
        stubs.tmux_kill.assert_awaited_once_with("@100")
        stubs.delete_topic.assert_awaited_once_with(-1001, 42)

        # First session remains intact.
        with store.connect() as conn:
            first = store.get_session(conn, first_id)
            all_sessions = store.list_sessions(conn)
            bindings = store.list_topic_bindings(conn)
        assert first is not None and first.status == "active"
        assert len(all_sessions) == 2
        errored = [s for s in all_sessions if s.status == "errored"]
        assert len(errored) == 1
        # Only one binding (the first one) should remain.
        assert len(bindings) == 1
        assert bindings[0].session_id == first_id


# ---- delete_session ----------------------------------------------------------


class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_happy_path_marks_retired_and_kills_window(
        self, ccgram_test_dir, stubs
    ):
        session_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="to-retire",
            agent="claude",
            group_id=-1001,
        )

        stubs.tmux_kill.reset_mock()
        stubs.delete_topic.reset_mock()

        await session_lifecycle.delete_session(session_id)

        stubs.tmux_kill.assert_awaited_once_with("@99")
        stubs.delete_topic.assert_not_called()  # default: don't close topic

        with store.connect() as conn:
            sess = store.get_session(conn, session_id)
            binding = store.get_binding_for_session(conn, session_id)
        assert sess is not None
        assert sess.status == "retired"
        assert sess.window_id is None
        assert binding is None  # explicit cascade delete

    @pytest.mark.asyncio
    async def test_close_telegram_topic_flag(self, ccgram_test_dir, stubs):
        session_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="to-retire",
            agent="claude",
            group_id=-1001,
        )
        stubs.delete_topic.reset_mock()

        await session_lifecycle.delete_session(session_id, close_telegram_topic=True)

        stubs.delete_topic.assert_awaited_once_with(-1001, 42)

    @pytest.mark.asyncio
    async def test_unknown_session_is_noop(self, ccgram_test_dir, stubs):
        await session_lifecycle.delete_session("nonexistent-id")
        stubs.tmux_kill.assert_not_called()
        stubs.delete_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_retired_session_is_idempotent(self, ccgram_test_dir, stubs):
        session_id = await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="to-retire",
            agent="claude",
            group_id=-1001,
        )
        await session_lifecycle.delete_session(session_id)

        stubs.tmux_kill.reset_mock()
        stubs.delete_topic.reset_mock()

        # Second delete — should be no-op.
        await session_lifecycle.delete_session(session_id)

        stubs.tmux_kill.assert_not_called()
        stubs.delete_topic.assert_not_called()


# ---- Integration smoke test (skipped by default) ----------------------------


@pytest.mark.integration
@pytest.mark.skip(
    reason="Manual-only: hits real tmux + Bot API + MTProto. "
    "Run with: pytest -m integration --no-skip"
)
@pytest.mark.asyncio
async def test_integration_real_tmux_real_telegram():
    """End-to-end smoke test — NOT run in CI.

    Preconditions:
    - CCGRAM_TEST_GROUP_ID env var set to a disposable Telegram supergroup.
    - MTProto session file exists (``~/.ccgram/mtproto.session``).
    - Bot token valid and bot is admin in the test group.
    - tmux server reachable.

    Verifies: create → bind → window running → delete → everything gone.
    """
    import os

    from ccgram import mtproto_client
    from ccgram.bot import get_bot  # type: ignore[attr-defined]
    from ccgram.tmux_manager import tmux_manager

    group_id = int(os.environ["CCGRAM_TEST_GROUP_ID"])
    bot = get_bot()
    mtproto = mtproto_client.MTProtoClient()
    async with mtproto:
        session_lifecycle.configure(
            session_lifecycle.build_default_deps(
                bot=bot,
                mtproto_client=mtproto,
                tmux_manager_obj=tmux_manager,
            )
        )
        sid = await session_lifecycle.create_session(
            cwd="/tmp",
            topic_name="ccgram-integration-smoke",
            agent="claude",
            group_id=group_id,
        )
        try:
            with store.connect() as conn:
                assert store.get_session(conn, sid) is not None
        finally:
            await session_lifecycle.delete_session(sid, close_telegram_topic=True)
