"""Tests for the polling coordinator loop and orchestration functions."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Bot
from telegram.error import BadRequest, TelegramError

from ccgram.handlers.topic_lifecycle import (
    check_autoclose_timers,
    check_unbound_window_ttl,
    probe_topic_existence,
    prune_stale_state,
)
from ccgram.handlers.polling_coordinator import (
    _BACKOFF_MAX,
    _BACKOFF_MIN,
    _apply_effort_suffix,
    _fetch_live_topic_title,
    _handle_dead_window_notification,
    _strip_effort_suffix,
)
from ccgram.handlers.polling_strategies import (
    lifecycle_strategy,
    terminal_strategy,
)


@pytest.fixture(autouse=True)
def _clean_strategy_state():
    """Reset all strategy state between tests."""
    terminal_strategy._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()
    yield
    terminal_strategy._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()


# ── _strip_effort_suffix ────────────────────────────────────────────────


class TestStripEffortSuffix:
    def test_strips_medium(self):
        assert _strip_effort_suffix("james-2 [M]") == "james-2"

    def test_strips_low(self):
        assert _strip_effort_suffix("my-project [L]") == "my-project"

    def test_strips_high(self):
        assert _strip_effort_suffix("bulugo-dev [H]") == "bulugo-dev"

    def test_strips_lightning(self):
        assert _strip_effort_suffix("james ⚡") == "james"

    def test_strips_snail(self):
        assert _strip_effort_suffix("james 🐚") == "james"

    def test_strips_multiple_suffixes(self):
        assert _strip_effort_suffix("proj 🐚 ⚡ [H]") == "proj"

    def test_no_suffix_unchanged(self):
        assert _strip_effort_suffix("clean") == "clean"

    def test_empty_string(self):
        assert _strip_effort_suffix("") == ""

    def test_strips_trailing_whitespace(self):
        assert _strip_effort_suffix("name   ") == "name"

    def test_user_title_with_spaces_preserved(self):
        # A title like "James Dev" should not lose internal spaces
        assert _strip_effort_suffix("James Dev [M]") == "James Dev"


# ── _fetch_live_topic_title ─────────────────────────────────────────────


class TestFetchLiveTopicTitle:
    # MTProtoClient is lazily imported inside _fetch_live_topic_title, so we
    # patch it at its source module rather than at the caller's namespace.
    _PATCH_TARGET = "ccgram.mtproto_client.MTProtoClient"

    @pytest.mark.asyncio
    async def test_returns_title_on_success(self):
        mock_topic = MagicMock()
        mock_topic.title = "James"
        mock_client = AsyncMock()
        mock_client.get_forum_topics_by_id = AsyncMock(return_value=[mock_topic])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(self._PATCH_TARGET, return_value=mock_client):
            result = await _fetch_live_topic_title(-100123, 69)
        assert result == "James"

    @pytest.mark.asyncio
    async def test_returns_none_on_credentials_error(self):
        from ccgram.mtproto_client import MTProtoCredentialsError

        with patch(self._PATCH_TARGET, side_effect=MTProtoCredentialsError("no creds")):
            result = await _fetch_live_topic_title(-100123, 69)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_response(self):
        mock_client = AsyncMock()
        mock_client.get_forum_topics_by_id = AsyncMock(return_value=[])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(self._PATCH_TARGET, return_value=mock_client):
            result = await _fetch_live_topic_title(-100123, 69)
        assert result is None


# ── _apply_effort_suffix ────────────────────────────────────────────────


def _make_bot() -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.edit_forum_topic = AsyncMock()
    return bot


class TestApplyEffortSuffixAutoManaged:
    """When live base == window name → standard behaviour, no user-title logic."""

    @pytest.mark.asyncio
    async def test_applies_suffix_when_base_matches_window(self):
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value="james-2"),  # base == window_name
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "james-2")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "james-2"

            await _apply_effort_suffix(bot, "james-2", 69, "M")

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "james-2 [M]"

    @pytest.mark.asyncio
    async def test_removes_suffix_on_none_level(self):
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value="james-2 [M]"),
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "james-2")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "james-2"

            await _apply_effort_suffix(bot, "james-2", 69, None)

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "james-2"


class TestApplyEffortSuffixUserTitle:
    """When live base != window name → preserve user's custom title base."""

    @pytest.mark.asyncio
    async def test_preserves_user_custom_base(self):
        """'James [L]' + window 'james-2' + effort M → 'James [M]'."""
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value="James [L]"),
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "james-2")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "james-2"

            await _apply_effort_suffix(bot, "james-2", 69, "M")

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "James [M]"

    @pytest.mark.asyncio
    async def test_preserves_user_title_removes_suffix(self):
        """'James [M]' + window 'james-2' + effort None → 'James'."""
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value="James [M]"),
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "james-2")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "james-2"

            await _apply_effort_suffix(bot, "james-2", 69, None)

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "James"

    @pytest.mark.asyncio
    async def test_case_insensitive_comparison(self):
        """'JAMES' (same base as 'james-2' after strip? — no, it's different)
        → preserves 'JAMES' because JAMES != james-2."""
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value="JAMES"),
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "james-2")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "james-2"

            await _apply_effort_suffix(bot, "james-2", 69, "H")

        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "JAMES [H]"


class TestApplyEffortSuffixMTProtoFallback:
    """MTProto failure → fall back to locally-stored display name."""

    @pytest.mark.asyncio
    async def test_fallback_on_mtproto_failure(self):
        """When MTProto returns None, use locally-stored display name."""
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value=None),  # MTProto unavailable
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "james-2")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "james-2"

            await _apply_effort_suffix(bot, "james-2", 69, "M")

        # Falls back to window name behaviour
        bot.edit_forum_topic.assert_awaited_once()
        _, kwargs = bot.edit_forum_topic.call_args
        assert kwargs["name"] == "james-2 [M]"

    @pytest.mark.asyncio
    async def test_noop_when_no_chat_id(self):
        bot = _make_bot()
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value=None),
            ),
        ):
            mock_router.iter_thread_bindings.return_value = []
            mock_router.resolve_chat_id.return_value = None

            await _apply_effort_suffix(bot, "ghost", 99, "L")

        bot.edit_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_error_silently_ignored(self):
        bot = _make_bot()
        bot.edit_forum_topic = AsyncMock(side_effect=TelegramError("oops"))
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager"),
            patch(
                "ccgram.handlers.polling_coordinator._fetch_live_topic_title",
                new=AsyncMock(return_value=None),
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 69, "w")]
            mock_router.resolve_chat_id.return_value = -100123
            mock_router.get_display_name.return_value = "w"

            # Must not raise
            await _apply_effort_suffix(bot, "w", 69, "L")


# ── Original tests kept intact ──────────────────────────────────────────


class TestCheckAutocloseTimers:
    @pytest.mark.asyncio
    async def test_no_topics_is_noop(self):
        bot = AsyncMock(spec=Bot)
        await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_done_topic_gets_closed(self):
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topic_lifecycle.config") as mock_config,
            patch("ccgram.handlers.topic_lifecycle.thread_router") as mock_router,
            patch(
                "ccgram.handlers.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_yet_expired_topic_stays(self):
        bot = AsyncMock(spec=Bot)
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic()
        )
        with patch("ccgram.handlers.topic_lifecycle.config") as mock_config:
            mock_config.autoclose_done_minutes = 60
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()


class TestCheckUnboundWindowTtl:
    @pytest.mark.asyncio
    async def test_no_timeout_is_noop(self):
        with patch("ccgram.handlers.topic_lifecycle.config") as mock_config:
            mock_config.autoclose_done_minutes = 0
            await check_unbound_window_ttl([])

    @pytest.mark.asyncio
    async def test_bound_window_timer_cleared(self):
        ws = terminal_strategy.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topic_lifecycle.config") as mock_config,
            patch("ccgram.handlers.topic_lifecycle.thread_router") as mock_router,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await check_unbound_window_ttl([mock_window])
        assert ws.unbound_timer is None


class TestHandleDeadWindowNotification:
    @pytest.mark.asyncio
    async def test_sends_notification_once(self):
        bot = AsyncMock(spec=Bot)
        bot.send_message = AsyncMock(return_value=MagicMock())
        with (
            patch("ccgram.handlers.polling_coordinator.thread_router") as mock_router,
            patch("ccgram.handlers.polling_coordinator.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.polling_coordinator.update_topic_emoji",
                new_callable=AsyncMock,
            ),
            patch("ccgram.handlers.polling_coordinator.clear_tool_msg_ids_for_topic"),
            patch(
                "ccgram.handlers.polling_coordinator.rate_limit_send_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_display_name.return_value = "test"
            mock_sm.get_window_state.return_value = MagicMock(cwd="/tmp")
            mock_send.return_value = MagicMock()

            await _handle_dead_window_notification(bot, 1, 100, "@0")
            assert (1, 100, "@0") in lifecycle_strategy._dead_notified

            mock_send.reset_mock()
            await _handle_dead_window_notification(bot, 1, 100, "@0")
            mock_send.assert_not_called()


class TestPruneStaleState:
    @pytest.mark.asyncio
    async def test_syncs_display_names(self):
        mock_window = MagicMock(window_id="@0", window_name="test")
        with patch("ccgram.handlers.topic_lifecycle.session_manager") as mock_sm:
            await prune_stale_state([mock_window])
            mock_sm.sync_display_names.assert_called_once_with([("@0", "test")])
            mock_sm.prune_stale_state.assert_called_once_with({"@0"})


class TestProbeTopicExistence:
    @pytest.mark.asyncio
    async def test_deleted_topic_unbinds(self):
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Topic_id_invalid")
        )
        with (
            patch("ccgram.handlers.topic_lifecycle.thread_router") as mock_router,
            patch("ccgram.handlers.topic_lifecycle.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.kill_window = AsyncMock()
            await probe_topic_existence(bot)
            mock_router.unbind_thread.assert_called_once_with(1, 100)

    @pytest.mark.asyncio
    async def test_suspended_probe_skipped(self):
        bot = AsyncMock(spec=Bot)
        ws = terminal_strategy.get_state("@0")
        ws.probe_failures = 999
        with patch("ccgram.handlers.topic_lifecycle.thread_router") as mock_router:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await probe_topic_existence(bot)
        bot.unpin_all_forum_topic_messages.assert_not_called()


class TestBackoffConstants:
    def test_backoff_bounds(self):
        assert _BACKOFF_MIN == 2.0
        assert _BACKOFF_MAX == 30.0
