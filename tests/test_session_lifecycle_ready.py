"""Tests for session_lifecycle readiness verification and thread_router update.

Covers:
- _verify_agent_ready: success/timeout/failure signals
- create_session: bind_thread called in-memory after DB write
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram import session_lifecycle, store


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def ccgram_test_dir(tmp_path, monkeypatch):
    """Redirect ccgram_dir() to a fresh tmp directory for this test."""
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    store.init_db(tmp_path / "state.db")
    return tmp_path


@pytest.fixture()
def stubs(monkeypatch):
    """Wire stub dependencies into session_lifecycle module-level callables."""

    class _Stubs:
        def __init__(self) -> None:
            self.create_topic = AsyncMock(return_value=42)
            self.delete_topic = AsyncMock(return_value=None)
            self.verify_topic = AsyncMock(return_value=(True, "test-topic"))
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


# ---- _verify_agent_ready tests -----------------------------------------------


class TestVerifyAgentReady:
    @pytest.mark.asyncio
    async def test_verify_agent_ready_claude_success(self, monkeypatch):
        """Claude pane with 'bypass permissions' signal → returns True."""
        capture = AsyncMock(return_value="Welcome to Claude Code\nbypass permissions\n❯ ")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", "yolo", timeout=5.0, poll_interval=0.1
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_agent_ready_claude_prompt_signal(self, monkeypatch):
        """Claude pane with '❯' prompt signal → returns True."""
        capture = AsyncMock(return_value="Claude Code\n❯ ")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", None, timeout=5.0, poll_interval=0.1
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_agent_ready_codex_success(self, monkeypatch):
        """Codex pane with 'gpt-' model name → returns True."""
        capture = AsyncMock(return_value="OpenAI Codex CLI — model gpt-5.4\n›")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@77", "codex", None, timeout=5.0, poll_interval=0.1
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_agent_ready_timeout(self, monkeypatch):
        """Empty pane output throughout → returns False after timeout."""
        capture = AsyncMock(return_value="")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", None, timeout=0.2, poll_interval=0.05
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_agent_ready_command_not_found(self, monkeypatch):
        """'command not found' in pane → returns False immediately."""
        capture = AsyncMock(return_value="bash: claude: command not found")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", None, timeout=5.0, poll_interval=0.1
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_agent_ready_no_such_file(self, monkeypatch):
        """'No such file' in pane → returns False immediately."""
        capture = AsyncMock(return_value="/usr/local/bin/claude: No such file or directory")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", None, timeout=5.0, poll_interval=0.1
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_agent_ready_none_pane(self, monkeypatch):
        """None pane → keeps polling until timeout."""
        capture = AsyncMock(return_value=None)
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", None, timeout=0.2, poll_interval=0.05
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_agent_ready_exception_in_capture(self, monkeypatch):
        """Exception during capture → swallowed, keeps polling until timeout."""
        capture = AsyncMock(side_effect=RuntimeError("tmux dead"))
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        result = await session_lifecycle._verify_agent_ready(
            "@99", "claude", None, timeout=0.2, poll_interval=0.05
        )
        assert result is False


# ---- create_session: thread_router in-memory update -------------------------


class TestCreateSessionUpdatesThreadRouter:
    @pytest.mark.asyncio
    async def test_create_session_updates_thread_router(self, ccgram_test_dir, stubs, monkeypatch):
        """After DB write, bind_thread is called on the in-memory thread_router."""
        # Disable readiness verification so the test doesn't wait
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", AsyncMock(return_value=""))

        mock_router = MagicMock()
        mock_config = MagicMock()
        mock_config.allowed_users = {1219959327}

        with (
            patch("ccgram.session_lifecycle.thread_router", mock_router, create=True),
            patch("ccgram.session_lifecycle.config", mock_config, create=True),
        ):
            # Patch the imports inside the function body
            import ccgram.thread_router as tr_module
            import ccgram.config as cfg_module

            real_thread_router = tr_module.thread_router
            real_config = cfg_module.config

            # We need to patch the imports inside session_lifecycle's create_session
            # by temporarily patching the modules it imports from
            with (
                patch.object(tr_module, "thread_router", mock_router),
                patch.object(cfg_module, "config", mock_config),
            ):
                session_id = await session_lifecycle.create_session(
                    cwd=str(ccgram_test_dir),
                    topic_name="test-topic",
                    agent="claude",
                    mode="yolo",
                    group_id=-1001,
                    verify_ready=False,  # skip polling
                )

        assert isinstance(session_id, str)
        # bind_thread should have been called with the admin user_id, topic_id, window_id
        mock_router.bind_thread.assert_called_once_with(
            1219959327, 42, "@99", window_name="test-topic"
        )
        mock_router.set_group_chat_id.assert_called_once_with(1219959327, 42, -1001)

    @pytest.mark.asyncio
    async def test_create_session_thread_router_error_does_not_block(
        self, ccgram_test_dir, stubs, monkeypatch
    ):
        """thread_router update failure is swallowed — session still returns session_id."""
        monkeypatch.setattr(
            session_lifecycle, "_tmux_capture_pane_fn", AsyncMock(return_value="")
        )

        import ccgram.thread_router as tr_module
        import ccgram.config as cfg_module

        mock_router = MagicMock()
        mock_router.bind_thread.side_effect = RuntimeError("router exploded")
        mock_config = MagicMock()
        mock_config.allowed_users = {1219959327}

        with (
            patch.object(tr_module, "thread_router", mock_router),
            patch.object(cfg_module, "config", mock_config),
        ):
            session_id = await session_lifecycle.create_session(
                cwd=str(ccgram_test_dir),
                topic_name="test-topic",
                agent="claude",
                group_id=-1001,
                verify_ready=False,
            )

        # Session must still succeed despite router failure
        assert isinstance(session_id, str) and len(session_id) == 36
        with store.connect() as conn:
            sess = store.get_session(conn, session_id)
        assert sess is not None and sess.status == "active"


# ---- create_session: progress callbacks -------------------------------------


class TestCreateSessionProgressCallbacks:
    @pytest.mark.asyncio
    async def test_progress_callbacks_fired(self, ccgram_test_dir, stubs, monkeypatch):
        """on_progress is called at spawn, wait, and ready milestones."""
        monkeypatch.setattr(
            session_lifecycle, "_tmux_capture_pane_fn", AsyncMock(return_value="❯ ")
        )

        progress_msgs: list[str] = []

        async def on_progress(msg: str) -> None:
            progress_msgs.append(msg)

        await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="prog-topic",
            agent="claude",
            group_id=-1001,
            on_progress=on_progress,
            verify_ready=True,
            ready_timeout=3.0,
        )

        # Should have at least: spawning, waiting, ready
        assert any("Spawning" in m or "\U0001f680" in m for m in progress_msgs)
        assert any("waiting" in m.lower() or "\u23f3" in m for m in progress_msgs)
        assert any("ready" in m.lower() or "\u2705" in m for m in progress_msgs)

    @pytest.mark.asyncio
    async def test_verify_ready_false_skips_polling(self, ccgram_test_dir, stubs, monkeypatch):
        """verify_ready=False skips the readiness poll entirely."""
        capture = AsyncMock(return_value="❯ ")
        monkeypatch.setattr(session_lifecycle, "_tmux_capture_pane_fn", capture)

        await session_lifecycle.create_session(
            cwd=str(ccgram_test_dir),
            topic_name="no-verify",
            agent="claude",
            group_id=-1001,
            verify_ready=False,
        )

        # Capture should NOT have been called (no polling)
        capture.assert_not_called()
