"""Startup cleanup: strip stale topic-name suffixes after bot restart.

On restart all in-memory subagent/effort/bg-work state is lost, but the
Telegram topic titles may still carry ⚡, 🐚, or [H/M/L] badges from the
previous session. This module provides a one-shot cleanup that runs after
thread_bindings are pre-populated from DB and before adopt_unbound_windows.
"""
from __future__ import annotations

import structlog
from telegram import Bot
from telegram.error import TelegramError

from ..session import session_manager
from ..thread_router import thread_router
from .hook_events import _strip_both_suffixes

logger = structlog.get_logger()


async def cleanup_stale_topic_suffixes(bot: Bot) -> None:
    """Strip stale suffixes (⚡, 🐚, [H/M/L]) from all known topics."""
    from .polling_coordinator import _bg_work_shown as _bg_shown
    from .polling_coordinator import _effort_shown as _eff_shown
    from .polling_coordinator import _fetch_live_topic_title

    cleaned = 0
    for user_id, thread_id, window_id in thread_router.iter_thread_bindings():
        if not window_id:
            continue
        display = thread_router.get_display_name(window_id) or ""
        clean = _strip_both_suffixes(display)
        if clean == display:
            continue
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if not chat_id:
            continue
        # Fetch the actual current Telegram title to avoid a no-op rename
        # (which generates a visible notification even when nothing changed).
        current_title = await _fetch_live_topic_title(chat_id, thread_id)
        if current_title is not None and current_title == clean:
            logger.debug(
                "startup_cleanup: %s already clean on Telegram, skipping rename",
                window_id,
            )
            session_manager.set_display_name(window_id, clean)
            _eff_shown[window_id] = None
            _bg_shown[window_id] = False
            continue
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
                name=clean,
            )
            session_manager.set_display_name(window_id, clean)
            _eff_shown[window_id] = None
            _bg_shown[window_id] = False
            cleaned += 1
            logger.debug(
                "startup_cleanup: %s %r -> %r", window_id, display, clean
            )
        except TelegramError as exc:
            logger.debug("startup_cleanup: failed for %s: %s", window_id, exc)
    logger.info("Startup: cleaned stale suffixes from %d topic(s)", cleaned)
