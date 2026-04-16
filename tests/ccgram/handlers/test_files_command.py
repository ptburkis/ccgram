"""Tests for /files command — verifies cwd-based URL generation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.session import WindowState


@pytest.fixture()
def _mock_update():
    update = MagicMock()
    update.effective_user.id = 42
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture()
def _patch_bot(monkeypatch):
    with (
        patch("ccgram.bot.is_user_allowed", return_value=True),
        patch("ccgram.bot._get_thread_id", return_value=1001),
        patch("ccgram.bot.thread_router") as mock_tr,
        patch("ccgram.bot.store") as mock_store,
        patch("ccgram.bot.session_manager") as mock_sm,
        patch("ccgram.bot.tmux_manager") as mock_tm,
        patch("ccgram.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_tr.get_window_for_thread.return_value = "@1"
        mock_sm.get_window_state.return_value = WindowState()
        mock_tm.list_windows = AsyncMock(return_value=[])
        yield mock_tr, mock_store, mock_sm, mock_tm, mock_reply


class TestFilesCommand:
    async def test_db_session_cwd_used(self, _mock_update, _patch_bot) -> None:
        """DB session cwd takes priority — URL should contain the project path."""
        _, mock_store, _, _, mock_reply = _patch_bot
        db_session = MagicMock()
        db_session.cwd = "/home/peter/projects/bulugo_lead_gen"
        mock_store.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_store.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.get_session_by_window.return_value = db_session

        from ccgram.bot import files_command
        await files_command(_mock_update, MagicMock())

        args = mock_reply.call_args[0]
        assert "/files/bulugo_lead_gen" in args[1]

    async def test_window_state_fallback(self, _mock_update, _patch_bot) -> None:
        """Falls back to window_state.cwd when DB has no session."""
        _, mock_store, mock_sm, _, mock_reply = _patch_bot
        mock_store.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_store.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.get_session_by_window.return_value = None
        mock_sm.get_window_state.return_value = WindowState(
            cwd="/home/peter/projects/myapp"
        )

        from ccgram.bot import files_command
        await files_command(_mock_update, MagicMock())

        args = mock_reply.call_args[0]
        assert "/files/myapp" in args[1]

    async def test_no_cwd_mentions_reconcile(self, _mock_update, _patch_bot) -> None:
        """When cwd cannot be resolved, error message mentions reconcile."""
        _, mock_store, _, _, mock_reply = _patch_bot
        mock_store.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_store.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.get_session_by_window.return_value = None

        from ccgram.bot import files_command
        await files_command(_mock_update, MagicMock())

        args = mock_reply.call_args[0]
        assert "reconcile" in args[1]
