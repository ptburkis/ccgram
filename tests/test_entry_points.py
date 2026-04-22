"""Tests for forum_topic_created handler and shadow-write entry points.

All offline: real tmux, bot, and MTProto clients are replaced by
``unittest.mock`` stubs. Uses the root conftest env (TELEGRAM_BOT_TOKEN,
ALLOWED_USERS=12345, CCGRAM_DIR set to a temp dir).
"""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram import store
from ccgram.handlers.forum_topic_created import forum_topic_created_handler
from ccgram.handlers.user_state import PENDING_THREAD_ID


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def ccgram_test_dir(tmp_path, monkeypatch):
    """Redirect ccgram_dir() to a fresh tmp directory for this test."""
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    store.init_db(tmp_path / "state.db")
    return tmp_path


def _make_update(
    user_id: int = 12345, thread_id: int = 42, topic_name: str = "new-proj"
):
    message = MagicMock()
    message.message_thread_id = thread_id
    message.chat = MagicMock()
    message.chat.id = -1001234567890
    message.chat.type = "supergroup"
    message.forum_topic_created = MagicMock()
    message.forum_topic_created.name = topic_name
    message.reply_text = AsyncMock()

    user = MagicMock()
    user.id = user_id

    update = MagicMock()
    update.effective_user = user
    update.message = message
    return update


def _make_context(user_data=None):
    context = MagicMock()
    context.user_data = user_data if user_data is not None else {}
    return context


# ---- forum_topic_created_handler tests --------------------------------------


async def test_forum_topic_created_handler_posts_picker():
    update = _make_update(user_id=12345, thread_id=42, topic_name="new-proj")
    context = _make_context()

    with (
        patch("ccgram.handlers.forum_topic_created.thread_router") as mock_router,
        patch(
            "ccgram.handlers.forum_topic_created.safe_reply", new_callable=AsyncMock
        ) as mock_reply,
        patch(
            "ccgram.handlers.forum_topic_created.build_directory_browser",
            return_value=("pick a dir", MagicMock(), ["/home"]),
        ),
    ):
        mock_router.get_window_for_thread.return_value = None
        await forum_topic_created_handler(update, context)

    assert mock_reply.await_count == 2
    assert context.user_data[PENDING_THREAD_ID] == 42


async def test_forum_topic_created_handler_skips_bound_topic():
    update = _make_update(user_id=12345, thread_id=42)
    context = _make_context()

    with (
        patch("ccgram.handlers.forum_topic_created.thread_router") as mock_router,
        patch(
            "ccgram.handlers.forum_topic_created.safe_reply", new_callable=AsyncMock
        ) as mock_reply,
    ):
        mock_router.get_window_for_thread.return_value = "@5"
        await forum_topic_created_handler(update, context)

    mock_reply.assert_not_called()


async def test_forum_topic_created_handler_skips_disallowed_user():
    update = _make_update(user_id=99999, thread_id=42)
    context = _make_context()

    with patch(
        "ccgram.handlers.forum_topic_created.safe_reply", new_callable=AsyncMock
    ) as mock_reply:
        await forum_topic_created_handler(update, context)

    mock_reply.assert_not_called()


# ---- directory_callbacks shadow-write tests ---------------------------------


async def test_directory_callbacks_create_session_shadow_write(ccgram_test_dir):
    from ccgram.handlers import directory_callbacks

    create_session_mock = AsyncMock(return_value=None)

    query = MagicMock()
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = -1001234567890
    query.message.chat.type = "supergroup"

    context = _make_context({PENDING_THREAD_ID: 55})

    with (
        patch.object(
            directory_callbacks.tmux_manager,
            "create_window",
            new=AsyncMock(return_value=(True, "ok", "my-proj", "@99")),
        ),
        patch.object(
            directory_callbacks.tmux_manager,
            "stamp_pane_title",
            new=AsyncMock(),
        ),
        patch.object(
            directory_callbacks,
            "safe_edit",
            new=AsyncMock(),
        ),
        patch.object(
            directory_callbacks,
            "_try_install_messaging_skill",
        ),
        patch.object(
            directory_callbacks.thread_router,
            "bind_thread",
        ),
        patch.object(
            directory_callbacks.thread_router,
            "set_group_chat_id",
        ),
        patch.object(
            directory_callbacks.session_manager,
            "get_window_state",
            return_value=MagicMock(cwd=None),
        ),
        patch.object(
            directory_callbacks.session_manager,
            "set_window_provider",
        ),
        patch.object(
            directory_callbacks.session_manager,
            "set_window_approval_mode",
        ),
        patch.object(
            directory_callbacks.session_manager,
            "wait_for_session_map_entry",
            new=AsyncMock(),
        ),
        patch.object(
            directory_callbacks.provider_registry,
            "get",
            return_value=MagicMock(capabilities=MagicMock(supports_hook=False)),
        ),
        patch.object(
            directory_callbacks.user_preferences,
            "update_user_mru",
        ),
        patch("ccgram.session_lifecycle.create_session", create_session_mock),
    ):
        await directory_callbacks._create_window_and_bind(
            query=query,
            user_id=12345,
            selected_path="/home/peter/proj",
            provider_name="claude",
            approval_mode="normal",
            context=context,
        )

    create_session_mock.assert_awaited_once()
    call_kwargs = create_session_mock.call_args.kwargs
    assert call_kwargs["cwd"] == "/home/peter/proj"
    assert call_kwargs["topic_name"] == "proj-claude"  # derived from path+provider
    assert call_kwargs["agent"] == "claude"
    assert call_kwargs["existing_topic_id"] == 55


async def test_directory_callbacks_shadow_write_failure_is_logged_not_raised(
    ccgram_test_dir,
):
    from ccgram.handlers import directory_callbacks

    create_session_mock = AsyncMock(side_effect=RuntimeError("db gone"))

    query = MagicMock()
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = -1001234567890
    query.message.chat.type = "supergroup"

    context = _make_context({PENDING_THREAD_ID: 55})

    with (
        patch.object(
            directory_callbacks.tmux_manager,
            "create_window",
            new=AsyncMock(return_value=(True, "ok", "my-proj", "@99")),
        ),
        patch.object(
            directory_callbacks.tmux_manager, "stamp_pane_title", new=AsyncMock()
        ),
        patch.object(directory_callbacks, "safe_edit", new=AsyncMock()),
        patch.object(directory_callbacks, "_try_install_messaging_skill"),
        patch.object(directory_callbacks.thread_router, "bind_thread"),
        patch.object(directory_callbacks.thread_router, "set_group_chat_id"),
        patch.object(
            directory_callbacks.session_manager,
            "get_window_state",
            return_value=MagicMock(cwd=None),
        ),
        patch.object(directory_callbacks.session_manager, "set_window_provider"),
        patch.object(directory_callbacks.session_manager, "set_window_approval_mode"),
        patch.object(
            directory_callbacks.session_manager,
            "wait_for_session_map_entry",
            new=AsyncMock(),
        ),
        patch.object(
            directory_callbacks.provider_registry,
            "get",
            return_value=MagicMock(capabilities=MagicMock(supports_hook=False)),
        ),
        patch.object(directory_callbacks.user_preferences, "update_user_mru"),
        patch("ccgram.session_lifecycle.create_session", create_session_mock),
    ):
        # Should not raise even though create_session raises
        await directory_callbacks._create_window_and_bind(
            query=query,
            user_id=12345,
            selected_path="/home/peter/proj",
            provider_name="claude",
            approval_mode="normal",
            context=context,
        )


# ---- topic_orchestration: Phase 5 Chunk J tests ----------------------------


async def test_topic_orchestration_unbound_no_hint_posts_alert(
    ccgram_test_dir, monkeypatch
):
    """Path A: unbound window with no topic_id hint => alert sent, no topic created."""
    from ccgram.handlers import topic_orchestration

    # Reset debounce state so the alert fires.
    topic_orchestration._last_alert_sent.clear()

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm:
        mock_ws = MagicMock()
        mock_ws.cwd = "/home/peter/myproject"
        mock_ws.provider_name = "claude"
        mock_sm.get_window_state.return_value = mock_ws

        await topic_orchestration.create_topic_in_chat(
            bot=mock_bot,
            chat_id=-1001234567890,
            window_id="@42",
            topic_name="my-topic",
        )

    mock_bot.send_message.assert_awaited_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == -1001234567890
    assert "@42" in call_kwargs["text"]
    assert "Suppressed auto-create" in call_kwargs["text"]
    # No create_forum_topic call — topic creation is retired.
    assert (
        not hasattr(mock_bot, "create_forum_topic")
        or not mock_bot.create_forum_topic.called
    )


async def test_topic_orchestration_unbound_with_existing_topic_id_uses_create_session(
    ccgram_test_dir,
):
    """Path B: unbound window + explicit existing_topic_id => create_session called, no forum topic created."""
    from ccgram.handlers import topic_orchestration

    create_session_mock = AsyncMock(return_value=None)

    mock_bot = MagicMock()
    mock_bot.create_forum_topic = AsyncMock()

    with (
        patch("ccgram.session_lifecycle.create_session", create_session_mock),
        patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm,
    ):
        mock_ws = MagicMock()
        mock_ws.cwd = "/home/peter/myproject"
        mock_ws.provider_name = "claude"
        mock_ws.approval_mode = "normal"
        mock_sm.get_window_state.return_value = mock_ws

        await topic_orchestration.create_topic_in_chat(
            bot=mock_bot,
            chat_id=-1001234567890,
            window_id="@42",
            topic_name="my-topic",
            existing_topic_id=77,
        )

    create_session_mock.assert_awaited_once()
    call_kwargs = create_session_mock.call_args.kwargs
    assert call_kwargs["existing_topic_id"] == 77
    assert call_kwargs["cwd"] == "/home/peter/myproject"
    assert call_kwargs["agent"] == "claude"
    mock_bot.create_forum_topic.assert_not_called()


async def test_topic_orchestration_alert_debounce(ccgram_test_dir):
    """Two alerts for the same window within 30 min => only one send_message call."""
    from ccgram.handlers import topic_orchestration

    # Reset debounce state.
    topic_orchestration._last_alert_sent.clear()

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch("ccgram.handlers.topic_orchestration.session_manager") as mock_sm:
        mock_ws = MagicMock()
        mock_ws.cwd = "/home/peter/myproject"
        mock_sm.get_window_state.return_value = mock_ws

        # First call — should send.
        await topic_orchestration.create_topic_in_chat(
            bot=mock_bot,
            chat_id=-1001234567890,
            window_id="@42",
            topic_name="my-topic",
        )
        # Second call — should be suppressed by debounce.
        await topic_orchestration.create_topic_in_chat(
            bot=mock_bot,
            chat_id=-1001234567890,
            window_id="@42",
            topic_name="my-topic",
        )

    assert mock_bot.send_message.await_count == 1


# ---- CLI smoke test ---------------------------------------------------------


def test_cli_spawn_help_exits_zero():
    result = subprocess.run(
        ["python3", "/home/peter/ccgram-dashboard/claude-hub", "spawn", "--help"],
        capture_output=True,
        timeout=10,
        text=True,
    )
    assert result.returncode == 0
    assert "--cwd" in result.stdout
