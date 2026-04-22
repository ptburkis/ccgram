"""Tests for startup_cleanup.cleanup_stale_topic_suffixes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from telegram import Bot
from telegram.error import TelegramError


def _make_bot() -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.edit_forum_topic = AsyncMock()
    return bot


class TestCleanupStaleSuffixes:
    @pytest.mark.asyncio
    async def test_strips_lightning_and_renames(self):
        """Topic with ⚡ suffix is cleaned and topic is renamed."""
        from ccgram.handlers.startup_cleanup import cleanup_stale_topic_suffixes

        bot = _make_bot()
        with (
            patch("ccgram.handlers.startup_cleanup.thread_router") as mock_router,
            patch("ccgram.handlers.startup_cleanup.session_manager") as mock_sess,
            patch.dict("ccgram.handlers.polling_coordinator._effort_shown", {}),
            patch.dict("ccgram.handlers.polling_coordinator._bg_work_shown", {}),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 10, "@0")]
            mock_router.get_display_name.return_value = "james ⚡"
            mock_router.resolve_chat_id.return_value = -100123

            await cleanup_stale_topic_suffixes(bot)

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "james"
        mock_sess.set_display_name.assert_called_once_with("@0", "james")

    @pytest.mark.asyncio
    async def test_skips_clean_topic(self):
        """Topic with no suffix is not renamed."""
        from ccgram.handlers.startup_cleanup import cleanup_stale_topic_suffixes

        bot = _make_bot()
        with (
            patch("ccgram.handlers.startup_cleanup.thread_router") as mock_router,
            patch("ccgram.handlers.startup_cleanup.session_manager"),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 10, "@0")]
            mock_router.get_display_name.return_value = "james"
            mock_router.resolve_chat_id.return_value = -100123

            await cleanup_stale_topic_suffixes(bot)

        bot.edit_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_strips_effort_suffix(self):
        """Topic with [M] effort suffix is cleaned."""
        from ccgram.handlers.startup_cleanup import cleanup_stale_topic_suffixes

        bot = _make_bot()
        with (
            patch("ccgram.handlers.startup_cleanup.thread_router") as mock_router,
            patch("ccgram.handlers.startup_cleanup.session_manager"),
            patch.dict("ccgram.handlers.polling_coordinator._effort_shown", {}),
            patch.dict("ccgram.handlers.polling_coordinator._bg_work_shown", {}),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 20, "@1")]
            mock_router.get_display_name.return_value = "bulugo-dev [M]"
            mock_router.resolve_chat_id.return_value = -100456

            await cleanup_stale_topic_suffixes(bot)

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "bulugo-dev"

    @pytest.mark.asyncio
    async def test_telegram_error_is_non_fatal(self):
        """TelegramError during rename does not abort cleanup of other topics."""
        from ccgram.handlers.startup_cleanup import cleanup_stale_topic_suffixes

        bot = _make_bot()
        bot.edit_forum_topic = AsyncMock(side_effect=TelegramError("boom"))
        with (
            patch("ccgram.handlers.startup_cleanup.thread_router") as mock_router,
            patch("ccgram.handlers.startup_cleanup.session_manager"),
            patch.dict("ccgram.handlers.polling_coordinator._effort_shown", {}),
            patch.dict("ccgram.handlers.polling_coordinator._bg_work_shown", {}),
        ):
            mock_router.iter_thread_bindings.return_value = [
                (1, 10, "@0"),
                (1, 11, "@1"),
            ]
            mock_router.get_display_name.side_effect = ["james ⚡", "proj 🐚"]
            mock_router.resolve_chat_id.return_value = -100123

            # Must not raise
            await cleanup_stale_topic_suffixes(bot)

        # Both renames were attempted
        assert bot.edit_forum_topic.await_count == 2

    @pytest.mark.asyncio
    async def test_no_bindings_is_noop(self):
        """No thread bindings → nothing to do."""
        from ccgram.handlers.startup_cleanup import cleanup_stale_topic_suffixes

        bot = _make_bot()
        with patch("ccgram.handlers.startup_cleanup.thread_router") as mock_router:
            mock_router.iter_thread_bindings.return_value = []
            await cleanup_stale_topic_suffixes(bot)

        bot.edit_forum_topic.assert_not_called()
