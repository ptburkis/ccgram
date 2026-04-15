"""Tests for topic_orchestration — Phase 5 Chunk J behaviour.

Auto-create is retired.  Unbound windows now either:
  - Post an operator alert (Path A, no existing_topic_id)
  - Route through create_session (Path B, existing_topic_id supplied)

Retained tests: _is_window_already_bound, collect_target_chats,
handle_new_window (skip cases), adopt_unbound_windows.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.topic_orchestration import (
    _is_window_already_bound,
    _last_alert_sent,
    adopt_unbound_windows,
    collect_target_chats,
    create_topic_in_chat,
    handle_new_window,
)
from ccgram.session_monitor import NewWindowEvent


@pytest.fixture(autouse=True)
def _clear_alert_state():
    _last_alert_sent.clear()
    yield
    _last_alert_sent.clear()


@pytest.fixture(autouse=True)
def _mock_tmux():
    mock_window = MagicMock()
    mock_window.pane_current_command = ""
    with patch("ccgram.handlers.topic_orchestration.tmux_manager") as mock_tmux:
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        yield mock_tmux


def _make_event(
    window_id: str = "@10",
    session_id: str = "sess-1",
    window_name: str = "my-project",
    cwd: str = "/home/user/my-project",
) -> NewWindowEvent:
    return NewWindowEvent(
        window_id=window_id,
        session_id=session_id,
        window_name=window_name,
        cwd=cwd,
    )


# ---- _is_window_already_bound ------------------------------------------------


class TestIsWindowAlreadyBound:
    def test_bound_window(self):
        with patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router:
            mock_router.has_window.return_value = True
            assert _is_window_already_bound("@5") is True

    def test_unbound_window(self):
        with patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router:
            mock_router.has_window.return_value = False
            assert _is_window_already_bound("@5") is False

    def test_no_bindings(self):
        with patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router:
            mock_router.has_window.return_value = False
            assert _is_window_already_bound("@0") is False


# ---- collect_target_chats ----------------------------------------------------


class TestCollectTargetChats:
    def test_from_bindings(self):
        with patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router:
            mock_router.iter_thread_bindings.return_value = [
                (1, 100, "@0"),
            ]
            mock_router.resolve_chat_id.return_value = -1001
            result = collect_target_chats("@5")
            assert result == {-1001}

    def test_fallback_to_group_chat_ids(self):
        with patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router:
            mock_router.iter_thread_bindings.return_value = []
            mock_router.group_chat_ids = {1: -2002}
            result = collect_target_chats("@5")
            assert result == {-2002}

    def test_fallback_to_config_group_id(self):
        with (
            patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_router.iter_thread_bindings.return_value = []
            mock_router.group_chat_ids = {}
            mock_config.group_id = -3003
            result = collect_target_chats("@5")
            assert result == {-3003}

    def test_no_chats_available(self):
        with (
            patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_router.iter_thread_bindings.return_value = []
            mock_router.group_chat_ids = {}
            mock_config.group_id = None
            result = collect_target_chats("@5")
            assert result == set()

    def test_skips_positive_ids(self):
        with (
            patch("ccgram.handlers.topic_orchestration.thread_router") as mock_router,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_router.iter_thread_bindings.return_value = []
            mock_router.group_chat_ids = {"100:5": 100}
            mock_config.group_id = None
            result = collect_target_chats("@5")
            assert result == set()


# ---- create_topic_in_chat (Path A & B) ---------------------------------------


class TestCreateTopicInChat:
    async def test_path_a_posts_alert(self):
        """Unbound + no topic_id => operator alert sent."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with (
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/peter/proj"
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            await create_topic_in_chat(
                bot=mock_bot,
                chat_id=-1001234567890,
                window_id="@42",
                topic_name="my-topic",
            )

        mock_bot.send_message.assert_awaited_once()
        kwargs = mock_bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == -1001234567890
        assert kwargs["message_thread_id"] == 529
        assert "@42" in kwargs["text"]
        assert "Suppressed auto-create" in kwargs["text"]

    async def test_path_a_no_create_forum_topic(self):
        """Path A must never call bot.create_forum_topic."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.create_forum_topic = AsyncMock()

        with (
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/peter/proj"
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            await create_topic_in_chat(
                bot=mock_bot,
                chat_id=-1001234567890,
                window_id="@42",
                topic_name="my-topic",
            )

        mock_bot.create_forum_topic.assert_not_called()

    async def test_path_a_debounce_suppresses_second_alert(self):
        """Two calls within 30 min for the same window => one send_message."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with (
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/peter/proj"
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            await create_topic_in_chat(
                bot=mock_bot, chat_id=-1001, window_id="@42", topic_name="t"
            )
            await create_topic_in_chat(
                bot=mock_bot, chat_id=-1001, window_id="@42", topic_name="t"
            )

        assert mock_bot.send_message.await_count == 1

    async def test_path_a_different_windows_each_alert(self):
        """Different window_ids are tracked independently."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        with (
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/peter/proj"
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            await create_topic_in_chat(
                bot=mock_bot, chat_id=-1001, window_id="@1", topic_name="a"
            )
            await create_topic_in_chat(
                bot=mock_bot, chat_id=-1001, window_id="@2", topic_name="b"
            )

        assert mock_bot.send_message.await_count == 2

    async def test_path_b_calls_create_session_with_existing_topic_id(self):
        """Path B: existing_topic_id => create_session called, no alert."""
        create_session_mock = AsyncMock(return_value=None)
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.create_forum_topic = AsyncMock()

        with (
            patch("ccgram.session_lifecycle.create_session", create_session_mock),
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/peter/proj"
            mock_ws.provider_name = "claude"
            mock_ws.approval_mode = "normal"
            mock_sm.get_window_state.return_value = mock_ws

            await create_topic_in_chat(
                bot=mock_bot,
                chat_id=-1001234567890,
                window_id="@42",
                topic_name="my-topic",
                existing_topic_id=77,
            )

        create_session_mock.assert_awaited_once()
        kwargs = create_session_mock.call_args.kwargs
        assert kwargs["existing_topic_id"] == 77
        assert kwargs["cwd"] == "/home/peter/proj"
        assert kwargs["agent"] == "claude"
        mock_bot.send_message.assert_not_called()
        mock_bot.create_forum_topic.assert_not_called()

    async def test_path_b_failure_logged_not_raised(self):
        """Path B create_session failure is swallowed."""
        create_session_mock = AsyncMock(side_effect=RuntimeError("db gone"))
        mock_bot = MagicMock()

        with (
            patch("ccgram.session_lifecycle.create_session", create_session_mock),
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/peter/proj"
            mock_ws.provider_name = "claude"
            mock_ws.approval_mode = "normal"
            mock_sm.get_window_state.return_value = mock_ws

            # Must not raise.
            await create_topic_in_chat(
                bot=mock_bot,
                chat_id=-1001,
                window_id="@42",
                topic_name="my-topic",
                existing_topic_id=77,
            )

    async def test_path_a_alert_failure_logged_not_raised(self):
        """Alert send_message failure is swallowed."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=Exception("network"))

        with (
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = ""
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            # Must not raise.
            await create_topic_in_chat(
                bot=mock_bot, chat_id=-1001, window_id="@42", topic_name="t"
            )


# ---- handle_new_window -------------------------------------------------------


class TestHandleNewWindow:
    async def test_skips_already_bound(self):
        event = _make_event()
        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch(
            "ccgram.handlers.topic_orchestration._is_window_already_bound",
            return_value=True,
        ):
            await handle_new_window(event, bot)
        bot.send_message.assert_not_called()

    async def test_skips_when_no_chats(self):
        event = _make_event()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        with (
            patch(
                "ccgram.handlers.topic_orchestration._is_window_already_bound",
                return_value=False,
            ),
            patch(
                "ccgram.handlers.topic_orchestration._auto_detect_provider",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.topic_orchestration.collect_target_chats",
                return_value=set(),
            ),
        ):
            await handle_new_window(event, bot)

        bot.send_message.assert_not_called()

    async def test_skips_web_terminal_mirror(self):
        event = _make_event(window_id="web-james-abc123:@5")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await handle_new_window(event, bot)
        bot.send_message.assert_not_called()

    async def test_sends_alert_for_unbound_window(self):
        event = _make_event()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        with (
            patch(
                "ccgram.handlers.topic_orchestration._is_window_already_bound",
                return_value=False,
            ),
            patch(
                "ccgram.handlers.topic_orchestration._auto_detect_provider",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.topic_orchestration.collect_target_chats",
                return_value={-100500},
            ),
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/user/my-project"
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            await handle_new_window(event, bot)

        bot.send_message.assert_awaited_once()

    async def test_topic_name_falls_back_to_cwd_dirname(self):
        event = _make_event(window_name="", cwd="/home/user/cool-project")
        bot = MagicMock()
        bot.send_message = AsyncMock()

        with (
            patch(
                "ccgram.handlers.topic_orchestration._is_window_already_bound",
                return_value=False,
            ),
            patch(
                "ccgram.handlers.topic_orchestration._auto_detect_provider",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.topic_orchestration.collect_target_chats",
                return_value={-100500},
            ),
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch("ccgram.handlers.topic_orchestration.config") as mock_config,
        ):
            mock_ws = MagicMock()
            mock_ws.cwd = "/home/user/cool-project"
            mock_sm.get_window_state.return_value = mock_ws
            mock_config.alert_thread_id = 529

            await handle_new_window(event, bot)

        # Alert should contain the cwd dirname as the window_name
        call_text = bot.send_message.call_args.kwargs["text"]
        assert "cool-project" in call_text


# ---- adopt_unbound_windows ---------------------------------------------------


class TestAdoptUnboundWindows:
    async def test_adopts_orphaned_windows(self):
        bot = AsyncMock()
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.window_name = "test"

        mock_audit = MagicMock()
        mock_issue = MagicMock()
        mock_issue.category = "orphaned_window"
        mock_audit.issues = [mock_issue]

        with (
            patch("ccgram.handlers.topic_orchestration.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.topic_orchestration._adopt_orphaned_windows",
                new_callable=AsyncMock,
                create=True,
            ),
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[mock_window])
            mock_sm.audit_state.return_value = mock_audit

            with patch(
                "ccgram.handlers.sync_command._adopt_orphaned_windows",
                new_callable=AsyncMock,
            ) as mock_adopt:
                await adopt_unbound_windows(bot)
                mock_adopt.assert_called_once()

    async def test_no_orphans_skips(self):
        bot = AsyncMock()
        mock_audit = MagicMock()
        mock_audit.issues = []

        with (
            patch("ccgram.handlers.topic_orchestration.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            mock_sm.audit_state.return_value = mock_audit
            await adopt_unbound_windows(bot)
