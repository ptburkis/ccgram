"""Telegram bot handlers — the main UI layer of CCGram.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window (Claude session).

Core responsibilities:
  - Command handlers: /new (+ /start alias), /history, /sessions, /resume,
    /screenshot, /panes, /toolbar, /restore, plus forwarding unknown /commands to Claude Code via tmux.
  - Callback query handler: thin dispatcher routing to dedicated handler modules.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create a new session.
  - Topic lifecycle: closing a topic unbinds the window (kept alive for
    rebinding). Unbound windows are auto-killed after TTL by status polling.
    Unsupported content (images, stickers, etc.) is rejected with a warning.
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import contextlib
import structlog
import os
import re
import signal
from pathlib import Path

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.error import BadRequest, Conflict, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from .cc_commands import (
    discover_provider_commands,
    register_commands,
)
from .providers import (
    get_provider,
    get_provider_for_window,
)
from .config import config
from .handlers.topic_orchestration import (
    adopt_unbound_windows as _adopt_unbound_windows,
    handle_new_window as _handle_new_window,
)
from .handlers.command_orchestration import (
    forward_command_handler,
    sync_scoped_menu_for_text_context as _sync_scoped_menu_for_text_context,
    sync_scoped_provider_menu as _sync_scoped_provider_menu,
    setup_menu_refresh_job,
)
from .handlers.callback_data import (
    CB_PANE_SCREENSHOT,
)
from .handlers.callback_helpers import get_thread_id as _get_thread_id
from .handlers.callback_registry import dispatch as _dispatch_callback
from .handlers.callback_registry import load_handlers as _load_callback_handlers
from .handlers.restore_command import restore_command
from .handlers.resume_command import resume_command
from .handlers.directory_browser import clear_browse_state
from .handlers.cleanup import clear_topic_state
from .handlers.topic_emoji import strip_emoji_prefix, update_stored_topic_name
from .handlers.history import send_history
from .handlers.sessions_dashboard import sessions_command
from .handlers.sync_command import sync_command
from .handlers.upgrade import upgrade_command
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    handle_interactive_ui,
    set_interactive_mode,
)
from .handlers.message_queue import (
    enqueue_content_message,
    enqueue_status_update,
    get_message_queue,
    shutdown_workers,
)
from .handlers.message_sender import safe_reply
from .handlers.response_builder import build_response_parts
from .handlers.polling_coordinator import status_poll_loop, _strip_effort_suffix
from .handlers.file_handler import handle_document_message, handle_photo_message
from .handlers.forum_topic_created import (
    forum_topic_created_handler as _forum_topic_created_handler,
)
from .handlers.voice_handler import handle_voice_message
from .handlers.text_handler import handle_text_message
from .session import session_manager
from .user_preferences import user_preferences
from . import store
from .session_monitor import NewMessage, NewWindowEvent, SessionMonitor
from .thread_router import thread_router
from .telegram_request import ResilientPollingHTTPXRequest
from .tmux_manager import tmux_manager
from .utils import handle_general_topic_message, is_general_topic, task_done_callback

logger = structlog.get_logger()

# Error keyword pattern for errors_only notification mode (word boundaries)
_ERROR_KEYWORDS_RE = re.compile(
    r"\b(?:error|exception|failed|traceback|stderr|assertion)\b", re.IGNORECASE
)

# Max label length for /recall command buttons (wider than status bar buttons)
_RECALL_LABEL_MAX = 40
# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


# Group filter: when CCBOT_GROUP_ID is set, only process updates from that group.
# filters.ALL is a no-op — single-instance backward compat.
_group_filter: filters.BaseFilter = (
    filters.Chat(chat_id=config.group_id) if config.group_id else filters.ALL
)


# --- Command handlers ---


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            "\U0001f916 *Claude Code Monitor*\n\n"
            "Each topic is a session. Create a new topic to start.",
        )


async def history_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    window_id = thread_router.resolve_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(update.message, "\u274c No session bound to this topic.")
        return

    provider = get_provider_for_window(window_id)
    if not provider.capabilities.supports_structured_transcript:
        await safe_reply(update.message, "No transcript available for this provider.")
        return

    await send_history(update.message, window_id)


async def commands_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show provider-specific slash commands for the current topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    window_id = thread_router.resolve_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(update.message, "\u274c No session bound to this topic.")
        return

    provider = get_provider_for_window(window_id)
    await _sync_scoped_provider_menu(update.message, user.id, provider)
    commands = discover_provider_commands(provider)
    if not commands:
        await safe_reply(
            update.message,
            f"Provider: `{provider.capabilities.name}`\nNo discoverable commands.",
        )
        return

    lines = [f"Provider: `{provider.capabilities.name}`", "Supported commands:"]
    for cmd in sorted(commands, key=lambda c: c.telegram_name):
        if not cmd.telegram_name:
            continue
        original = cmd.name if cmd.name.startswith("/") else f"/{cmd.name}"
        lines.append(f"- `/{cmd.telegram_name}` \u2192 `{original}`")
    await safe_reply(update.message, "\n".join(lines))


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — unbind thread but keep the tmux window alive.

    The window becomes "unbound" and is available for rebinding via the window
    picker when a new topic is created. Unbound windows are auto-killed after
    the configured TTL (autoclose_done_minutes) by the status polling loop.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if window_id:
        display = thread_router.get_display_name(window_id)
        # Clean up BEFORE unbind — resolve_chat_id needs group_chat_ids
        # which unbind_thread deletes.  window_dead=False because the
        # window stays alive for rebinding.
        await clear_topic_state(
            user.id,
            thread_id,
            context.bot,
            context.user_data,
            window_id=window_id,
            window_dead=False,
        )
        thread_router.unbind_thread(user.id, thread_id)
        logger.info(
            "Topic closed: window %s unbound (kept alive for rebinding, user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def topic_edited_handler(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and emoji cache.

    Ignores icon-only edits (name is None) and emoji-only changes from the bot
    itself (clean name unchanged after stripping prefixes).
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message or not update.message.forum_topic_edited:
        return

    new_name = update.message.forum_topic_edited.name
    if not new_name:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    window_id = thread_router.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        logger.debug("Topic edited: no binding (thread=%d)", thread_id)
        return

    clean_name = strip_emoji_prefix(new_name)

    # Loop guard: if clean name matches current display name, this was a
    # bot-originated emoji/mode change — skip to prevent rename loops.
    current_display = thread_router.get_display_name(window_id)
    if current_display and strip_emoji_prefix(current_display) == clean_name:
        logger.debug(
            "Topic edited: name unchanged after strip, skipping (thread=%d)", thread_id
        )
        return

    # Strip effort/bg suffixes before renaming tmux; keep full name in DB.
    window_name = _strip_effort_suffix(clean_name)
    renamed = await tmux_manager.rename_window(window_id, window_name)
    if renamed:
        session_manager.set_display_name(window_id, window_name)
        update_stored_topic_name(chat_id, thread_id, window_name)
        with store.connect() as conn:
            store.update_topic_binding_title(conn, chat_id, thread_id, clean_name)
        logger.info(
            "Topic renamed: window %s → %r (thread=%d)",
            window_id,
            window_name,
            thread_id,
        )


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnect a topic from its tmux window without killing the session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return

    display = thread_router.get_display_name(window_id)
    # Enqueue a status clear to actually delete the Telegram status message
    # (clear_topic_state only clears the tracking dict, leaving a ghost)
    await enqueue_status_update(context.bot, user.id, window_id, None, thread_id)
    await clear_topic_state(
        user.id,
        thread_id,
        context.bot,
        context.user_data,
        window_id=window_id,
        window_dead=False,
    )
    thread_router.unbind_thread(user.id, thread_id)
    await safe_reply(
        update.message,
        f"\u2702 Unbound from window `{display}`. The session is still running.\n"
        "Send a message in this topic to rebind or create a new session.",
    )


async def screenshot_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture and send a terminal screenshot for the current topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return

    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        await safe_reply(update.message, "\u274c Window no longer exists.")
        return

    pane_text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not pane_text:
        await safe_reply(update.message, "\u274c Failed to capture terminal.")
        return

    import io

    from .handlers.screenshot_callbacks import build_screenshot_keyboard
    from .screenshot import text_to_image

    png_bytes = await text_to_image(pane_text, with_ansi=True)
    keyboard = build_screenshot_keyboard(window_id)
    chat_id = thread_router.resolve_chat_id(user.id, thread_id)
    try:
        await update.message.get_bot().send_document(
            chat_id=chat_id,
            document=io.BytesIO(png_bytes),
            filename="screenshot.png",
            reply_markup=keyboard,
            message_thread_id=thread_id,
        )
    except TelegramError as e:
        logger.error("Failed to send screenshot: %s", e)
        await safe_reply(update.message, "\u274c Failed to send screenshot.")


async def panes_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all panes in the current topic's window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return

    panes = await tmux_manager.list_panes(window_id)
    if len(panes) <= 1:
        await safe_reply(
            update.message,
            "\U0001f4d0 Single pane \u2014 no multi-pane layout detected.",
        )
        return

    from .handlers.polling_strategies import has_pane_alert

    lines = [f"\U0001f4d0 {len(panes)} panes in window\n"]
    buttons: list[InlineKeyboardButton] = []
    for pane in panes:
        prefix = "\U0001f4cd" if pane.active else "  "
        label = f"Pane {pane.index} ({pane.command})"
        suffix_parts: list[str] = []
        if pane.active:
            suffix_parts.append("active")
        if has_pane_alert(pane.pane_id):
            prefix = "\u26a0\ufe0f"
            suffix_parts.append("blocked")
        elif not pane.active:
            suffix_parts.append("running")
        suffix = f" \u2014 {', '.join(suffix_parts)}" if suffix_parts else ""
        lines.append(f"{prefix} {label}{suffix}")
        buttons.append(
            InlineKeyboardButton(
                f"\U0001f4f7 {pane.index}",
                callback_data=f"{CB_PANE_SCREENSHOT}{window_id}:{pane.pane_id}"[:64],
            )
        )

    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    await safe_reply(update.message, "\n".join(lines), reply_markup=keyboard)


async def recall_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent command history for the current topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return

    from .handlers.command_history import (
        INLINE_QUERY_MAX,
        get_history,
        truncate_for_display,
    )

    history = get_history(user.id, thread_id, limit=10)
    if not history:
        await safe_reply(update.message, "\U0001f4cb No command history yet.")
        return

    rows = []
    for cmd in history:
        label = truncate_for_display(cmd, _RECALL_LABEL_MAX)
        query = cmd[:INLINE_QUERY_MAX]
        rows.append(
            [InlineKeyboardButton(label, switch_inline_query_current_chat=query)]
        )
    keyboard = InlineKeyboardMarkup(rows)
    await safe_reply(
        update.message, "\U0001f4cb Recent commands:", reply_markup=keyboard
    )


async def toolbar_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show persistent action toolbar with inline keyboard buttons."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return

    from .handlers.screenshot_callbacks import build_toolbar_keyboard

    keyboard = build_toolbar_keyboard(window_id)
    display = thread_router.get_display_name(window_id)
    await safe_reply(
        update.message,
        f"\U0001f39b `{display}` toolbar",
        reply_markup=keyboard,
    )


async def verbose_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle tool call batching for this topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        if (
            update.message
            and update.effective_chat
            and is_general_topic(update.message)
        ):
            await handle_general_topic_message(
                update.get_bot(), update.message, update.effective_chat.id
            )
        else:
            await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return

    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return

    new_mode = session_manager.cycle_batch_mode(window_id)
    if new_mode == "batched":
        await safe_reply(
            update.message,
            "\u26a1 Tool calls will be *batched* into a single message.",
        )
    else:
        await safe_reply(
            update.message,
            "\U0001f4ac Tool calls will be sent *individually* (verbose mode).",
        )


async def inline_query_handler(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Echo query text as a sendable inline result."""
    if not update.inline_query:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    text = update.inline_query.query.strip()
    if not text:
        await update.inline_query.answer([])
        return

    result = InlineQueryResultArticle(
        id="cmd",
        title=text,
        description="Tap to send",
        input_message_content=InputTextMessageContent(message_text=text),
    )
    await update.inline_query.answer([result], cache_time=0, is_personal=True)


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (images, stickers, voice, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    # Omit "voice" from the list when whisper is configured (has its own handler)
    media_list = (
        "Stickers, voice, video" if not config.whisper_provider else "Stickers, video"
    )
    await safe_reply(
        update.message,
        f"\u26a0 {media_list}, and similar media are not supported. Use text, photos, or documents.",
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    await _sync_scoped_menu_for_text_context(update, user.id)
    await handle_text_message(update, context)


# --- Streaming response / notifications ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        "handle_new_message [%s]: session=%s, text_len=%d",
        status,
        msg.session_id,
        len(msg.text),
    )

    # Find users whose thread-bound window matches this session
    active_users = session_manager.find_users_for_session(
        msg.session_id, window_id_hint=msg.window_id
    )

    if not active_users:
        logger.info("No active users for session %s", msg.session_id)
        return

    for user_id, window_id, thread_id in active_users:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            window_id=window_id, session_id=msg.session_id
        )
        # Notification mode filter.
        # In "summary" mode (the default), only prose text reaches the user — tool
        # calls, tool results, thinking blocks, and other non-text content are
        # dropped. Both halves of a tool_use/tool_result pair are dropped together,
        # so the message queue's in-place edit logic never sees an orphan.
        # Interactive UI tools (AskUserQuestion / ExitPlanMode / request_user_input)
        # are exempted from filtering — they need to reach the early-exit handler
        # below so the user can answer them.
        notif_mode = session_manager.get_notification_mode(window_id)
        is_interactive_tool = msg.tool_name in INTERACTIVE_TOOL_NAMES
        if (
            notif_mode == "summary"
            and not is_interactive_tool
            and msg.content_type != "text"
        ):
            continue

        # Drop heartbeat acknowledgements: when an agent receives a periodic
        # heartbeat poll and has nothing to report, it replies with the literal
        # token HEARTBEAT_OK. These should never reach the user's chat.
        if msg.content_type == "text" and (msg.text or "").strip() == "HEARTBEAT_OK":
            continue

        # Don't echo user messages back in summary mode — the user already
        # sees their own message in Telegram. The 👤 echo is redundant noise.
        if notif_mode == "summary" and msg.role == "user":
            continue

        # Handle interactive tools specially - capture terminal and send UI
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, window_id, thread_id)
            # Flush pending messages (e.g. plan content) before sending interactive UI
            queue = get_message_queue(user_id)
            if queue:
                await queue.join()
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, window_id, thread_id)
            if handled:
                # Update user's read offset
                session = await session_manager.resolve_session_for_window(window_id)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        user_preferences.update_user_window_offset(
                            user_id, window_id, file_size
                        )
                    except OSError:
                        pass
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete the UI message
        if get_interactive_msg_id(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)

        parts = build_response_parts(
            msg.text,
            msg.is_complete,
            msg.content_type,
            msg.role,
        )

        if msg.is_complete:
            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=window_id,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                tool_name=msg.tool_name,
                content_type=msg.content_type,
                text=msg.text,
                thread_id=thread_id,
            )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            session = await session_manager.resolve_session_for_window(window_id)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    user_preferences.update_user_window_offset(
                        user_id, window_id, file_size
                    )
                except OSError:
                    pass


# --- App lifecycle ---


def _global_exception_handler(
    _loop: asyncio.AbstractEventLoop, context: dict[str, object]
) -> None:
    """Last-resort handler for uncaught exceptions in asyncio tasks."""
    exc = context.get("exception")
    msg = context.get("message", "Unhandled exception in event loop")
    if isinstance(exc, BaseException):
        logger.error(
            "asyncio exception handler: %s",
            msg,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        logger.error("asyncio exception handler: %s", msg)


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    # Install global asyncio exception handler as safety net
    asyncio.get_running_loop().set_exception_handler(_global_exception_handler)

    default_provider = get_provider()
    try:
        await register_commands(application.bot, provider=default_provider)
    except TelegramError:
        logger.warning("Failed to register bot commands at startup, will retry later")
    setup_menu_refresh_job(application)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # State unification: wire session_lifecycle with production deps.
    from ccgram import session_lifecycle as _session_lifecycle
    from ccgram.mtproto_client import MTProtoClient as _MTProtoClient
    from ccgram.mtproto_client import (
        MTProtoCredentialsError as _MTProtoCredentialsError,
    )

    try:
        _mtproto_client = _MTProtoClient()
    except _MTProtoCredentialsError as _exc:
        logger.warning(
            "MTProto credentials missing — session_lifecycle running without "
            "topic verification (shadow writes will fail silently): %s",
            _exc,
        )

        class _StubMTProtoClient:
            async def get_forum_topics_by_id(self, *_: object, **__: object) -> list:
                return []

        _mtproto_client = _StubMTProtoClient()  # type: ignore[assignment]

    _session_lifecycle.configure(
        _session_lifecycle.build_default_deps(
            bot=application.bot,
            mtproto_client=_mtproto_client,
            tmux_manager_obj=tmux_manager,
        )
    )
    logger.info("session_lifecycle configured")

    await _adopt_unbound_windows(application.bot)

    # Warn if Claude Code hooks are not installed (provider-aware, non-blocking)
    provider = get_provider()
    if provider.capabilities.supports_hook:
        from .hook import _claude_settings_file, get_installed_events

        settings_file = _claude_settings_file()
        import json

        if settings_file.exists():
            try:
                settings = json.loads(settings_file.read_text())
                events = get_installed_events(settings)
                missing = [e for e, ok in events.items() if not ok]
                if missing:
                    logger.warning(
                        "Claude Code hooks incomplete — %d missing: %s. "
                        "Run: ccgram hook --install",
                        len(missing),
                        ", ".join(missing),
                    )
            except (json.JSONDecodeError, OSError):  # fmt: skip
                logger.warning(
                    "Claude Code hooks not installed. Run: ccgram hook --install"
                )
        else:
            logger.warning(
                "Claude Code hooks not installed (%s missing). "
                "Run: ccgram hook --install",
                settings_file,
            )

    monitor = SessionMonitor()
    # Expose to other modules (status_polling activity heuristic)
    from ccgram.session_monitor import set_active_monitor

    set_active_monitor(monitor)

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)

    async def new_window_callback(event: NewWindowEvent) -> None:
        await _handle_new_window(event, application.bot)

    monitor.set_new_window_callback(new_window_callback)

    # Wire hook event dispatcher for structured Claude Code events
    from ccgram.providers.base import HookEvent
    from ccgram.handlers.hook_events import dispatch_hook_event

    async def hook_event_callback(event: HookEvent) -> None:
        await dispatch_hook_event(event, application.bot)

    monitor.set_hook_event_callback(hook_event_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Debug timeline: cleanup old files and start pane captures
    from .debug_timeline import get_timeline as _get_timeline

    _tl = _get_timeline()
    await _tl.cleanup_old()
    from . import __version__ as _ccgram_version

    await _tl.log("system.startup", "", "", {"version": _ccgram_version})

    # Start pane captures for all bound windows
    from .thread_router import thread_router as _tr

    for _uid, _tid, _wid in _tr.iter_thread_bindings():
        if _wid:
            _wname = _tr.get_display_name(_wid)
            await tmux_manager.start_pane_capture(_wid, _wname)

    # Start status polling task (routed through PTB error handler)
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    _status_poll_task.add_done_callback(task_done_callback)
    logger.info("Status polling task started")

    # Start inotify session watcher (event-driven session rotation detection)
    from .session_watcher import start_session_watcher

    await start_session_watcher()


async def _send_shutdown_notification(application: Application) -> None:
    """Send a shutdown notification to the General topic if a group is configured."""
    from .main import _shutdown_signal

    if not config.group_id:
        return

    sig = _shutdown_signal
    reason = f"Received {signal.Signals(sig).name}" if sig else "Clean exit"

    from . import __version__

    text = f"🔌 ccgram stopped — {reason} (v{__version__})"
    try:
        await application.bot.send_message(
            chat_id=config.group_id,
            text=text,
            message_thread_id=1,  # General topic
        )
    except (TelegramError, RuntimeError) as exc:
        logger.debug("Shutdown notification skipped: %s", exc)


async def post_stop(application: Application) -> None:
    """Send shutdown notification while HTTP transport is still alive."""
    await _send_shutdown_notification(application)


async def post_shutdown(_application: Application) -> None:
    global _status_poll_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _status_poll_task
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop session monitor first (it may enqueue messages to workers)
    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    # Stop all queue workers after monitor is stopped
    await shutdown_workers()

    # Sweep expired mailbox messages before final state flush
    from .mailbox import Mailbox

    Mailbox(config.mailbox_dir).sweep()

    # Flush debounced state to disk AFTER workers/monitor stop (captures final mutations)
    session_manager.flush_state()


async def _error_handler(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bot-level errors from updater and handlers."""
    if isinstance(context.error, Conflict):
        logger.critical(
            "Another bot instance is polling with the same token. "
            "Shutting down to avoid conflicts."
        )
        os.kill(os.getpid(), signal.SIGINT)
        return
    if isinstance(context.error, BadRequest) and "too old" in str(context.error):
        logger.debug("Callback query expired (query too old)")
        return
    if isinstance(context.error, NetworkError) and not isinstance(
        context.error, BadRequest
    ):
        logger.warning("Transient network error (PTB will retry): %s", context.error)
        return
    logger.error("Unhandled bot error", exc_info=context.error)


# ── Bot-native convenience commands ─────────────────────────────────────
# /terminal  /dashboard  /cwd  /busy  /fleet
#
# These are thin informational commands that expose ambient state about
# sessions without requiring the user to leave Telegram. They all share
# the same topic-guard pattern and rely on the active SessionMonitor for
# activity timestamps.

# Web-terminal / dashboard base URL. Overridable via env for mrsclawd etc.
_CCGRAM_WEB_BASE = os.environ.get(
    "CCGRAM_WEB_BASE", "https://clawd.tail483fa1.ts.net:8443"
)


def _format_activity_age(elapsed_secs: float) -> str:
    """Human-friendly '5s' / '2m' / '1h' from a seconds-elapsed value."""
    if elapsed_secs < 60:
        return f"{int(elapsed_secs)}s"
    if elapsed_secs < 3600:
        return f"{int(elapsed_secs // 60)}m"
    return f"{int(elapsed_secs // 3600)}h"


async def terminal_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a tappable URL to the web terminal for this topic's bound window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return
    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return
    all_windows = await tmux_manager.list_windows()
    window = next((w for w in all_windows if w.window_id == window_id), None)
    if not window:
        await safe_reply(update.message, "\u274c Window not found.")
        return
    url = f"{_CCGRAM_WEB_BASE}/terminal/terminal/{window.window_name}"
    await safe_reply(update.message, f"\U0001f5a5 Web terminal: {url}")


async def dashboard_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Return a tappable URL to the Claude Hub dashboard."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    await safe_reply(update.message, f"\U0001f4ca Dashboard: {_CCGRAM_WEB_BASE}/hub/")


async def files_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a tappable URL to the file browser for this topic's project directory."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return
    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return
    # Resolve cwd: DB session first (most accurate), then window state, then tmux
    cwd = ""
    with store.connect() as conn:
        db_session = store.get_session_by_window(conn, window_id)
        if db_session and db_session.cwd:
            cwd = db_session.cwd
    if not cwd:
        state = session_manager.get_window_state(window_id)
        cwd = state.cwd if state and state.cwd else ""
    if not cwd:
        all_windows = await tmux_manager.list_windows()
        window = next((w for w in all_windows if w.window_id == window_id), None)
        cwd = window.cwd if window else ""
    if not cwd:
        await safe_reply(
            update.message,
            "\u274c Could not determine project directory. "
            "Try running `claude-hub reconcile` to re-bind this session.",
        )
        return
    # Build relative path from ~/projects/
    from pathlib import Path

    projects_root = Path.home() / "projects"
    try:
        rel = Path(cwd).relative_to(projects_root)
        url = f"{_CCGRAM_WEB_BASE}/files/{rel}/"
    except ValueError:
        url = f"{_CCGRAM_WEB_BASE}/files/"
    await safe_reply(update.message, f"\U0001f4c1 Files: {url}")


async def cwd_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the working directory of the current topic's session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return
    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return
    state = session_manager.get_window_state(window_id)
    cwd = state.cwd if state and state.cwd else ""
    if not cwd:
        all_windows = await tmux_manager.list_windows()
        window = next((w for w in all_windows if w.window_id == window_id), None)
        cwd = window.cwd if window else "(unknown)"
    await safe_reply(update.message, f"\U0001f4c2 `{cwd}`")


async def busy_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the current topic's session is actively processing."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "\u274c Use this command inside a topic.")
        return
    window_id = thread_router.get_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            update.message, "\u274c This topic is not bound to any session."
        )
        return
    from .session_monitor import get_active_monitor

    mon = get_active_monitor()
    if mon is None:
        await safe_reply(update.message, "\u26a0\ufe0f Monitor not active.")
        return
    state = session_manager.get_window_state(window_id)
    session_id = state.session_id if state else ""
    if not session_id:
        await safe_reply(update.message, "\U0001f4a4 No active session.")
        return
    last_active = mon.get_last_activity(session_id)
    if last_active is None:
        await safe_reply(update.message, "\U0001f4a4 Idle (no activity recorded).")
        return
    import time as _time

    elapsed = _time.monotonic() - last_active
    # Using the same 10s ACTIVITY_THRESHOLD polling_strategies.py uses for
    # "recently active" — keeps this command's answer consistent with what
    # the typing-indicator heuristic thinks.
    if elapsed < 10.0:
        await safe_reply(
            update.message,
            f"\u26a1 Processing \u2014 last activity {int(elapsed)}s ago",
        )
    else:
        await safe_reply(
            update.message,
            f"\U0001f4a4 Idle for {_format_activity_age(elapsed)}",
        )


async def fleet_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-line-per-session summary of all bound topics for this user.

    Shows each bound window's name, whether it's currently active, and the
    age of its last recorded activity. Useful for the 'what's everyone
    doing right now' question before you pick a topic to message.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    from .session_monitor import get_active_monitor

    mon = get_active_monitor()
    all_windows = await tmux_manager.list_windows()
    win_by_id = {w.window_id: w for w in all_windows}

    bound: list[tuple[int, str]] = []
    for uid, tid, wid in thread_router.iter_thread_bindings():
        if uid != user.id:
            continue
        if not wid or ":" in wid:
            continue  # skip qualified / mirror bindings
        bound.append((tid, wid))
    if not bound:
        await safe_reply(update.message, "\U0001f4a4 No bound sessions.")
        return

    import time as _time

    now = _time.monotonic()
    lines: list[str] = []
    for tid, wid in sorted(bound, key=lambda x: x[0]):
        window = win_by_id.get(wid)
        if window is None:
            lines.append(f"  \U0001f480 <dead> {wid} (thread {tid})")
            continue
        state = session_manager.get_window_state(wid)
        sid = state.session_id if state else ""
        if sid and mon is not None:
            last = mon.get_last_activity(sid)
            if last is None:
                icon, age = "\U0001f4a4", "no data"
            elif (now - last) < 10.0:
                icon, age = "\u26a1", f"{int(now - last)}s"
            else:
                icon, age = "\U0001f4a4", _format_activity_age(now - last)
        else:
            icon, age = "\u2753", "unknown"
        lines.append(f"  {icon} `{window.window_name}` \u2014 {age}")

    body = "\U0001f916 **Fleet**\n" + "\n".join(lines)
    await safe_reply(update.message, body)


async def usage_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Claude + Codex usage limits."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    import subprocess as _sp

    try:
        result = _sp.run(
            [
                "/home/peter/ccgram-dashboard/scrape-usage.sh",
                "usage-scraper",
                "usage-scraper-codex",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            await safe_reply(update.message, "⚠️ Could not fetch usage data.")
            return

        import json as _json

        data = _json.loads(result.stdout.strip())
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Usage scrape failed: {e}")
        return

    from datetime import datetime, timedelta, timezone

    # Calculate weekly pace (Thursday 11:00 UTC reset)
    now = datetime.now(timezone.utc)
    days_since = (now.weekday() - 3) % 7
    if days_since == 0 and now.hour < 11:
        days_since = 7
    last_reset = (now - timedelta(days=days_since)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    elapsed = now - last_reset
    expected_pct = (elapsed / timedelta(days=7)) * 100

    lines = []

    # Session
    s = data.get("session", {})
    if s:
        lines.append(f"⏱ Session: {s['percent']}% · resets {s.get('resets', '?')}")

    # Weekly all models + pace
    w = data.get("weekAll", {})
    if w:
        pct = w["percent"]
        diff = pct - expected_pct
        if diff < -2:
            pace = f"▼ {abs(diff):.0f}% under"
        elif diff > 2:
            pace = f"▲ {diff:.0f}% over"
        else:
            pace = "≈ on pace"
        lines.append(f"📊 Weekly: {pct}% · {pace} · resets {w.get('resets', '?')}")

    # Weekly Sonnet
    ws = data.get("weekSonnet", {})
    if ws:
        pct = ws["percent"]
        diff = pct - expected_pct
        if diff < -2:
            pace = f"▼ {abs(diff):.0f}% under"
        elif diff > 2:
            pace = f"▲ {diff:.0f}% over"
        else:
            pace = "≈ on pace"
        lines.append(f"🔵 Sonnet: {pct}% · {pace} · resets {ws.get('resets', '?')}")

    # Codex
    cx = data.get("codex", {})
    if cx:
        parts = [f"🟢 Codex: {cx['percent']}% used · {cx.get('remaining', '?')}% left"]
        if cx.get("resets"):
            parts.append(f"resets {cx['resets']}")
        lines.append(" · ".join(parts))

    if not lines:
        await safe_reply(update.message, "⚠️ No usage data available.")
        return

    await safe_reply(update.message, "\n".join(lines))


async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or change the effort level for the window bound to this topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    message = update.message
    chat_id = message.chat_id
    thread_id = message.message_thread_id

    if thread_id is None:
        await safe_reply(message, "⚠️ /effort only works inside a topic")
        return

    window_id = thread_router.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        await safe_reply(message, "⚠️ No window bound to this topic")
        return

    args = context.args or []
    if not args:
        # Show current stored level
        from .handlers.polling_coordinator import _effort_shown

        current = _effort_shown.get(window_id)
        labels = {"H": "high", "M": "medium", "L": "low"}
        label = labels.get(current, "not yet set") if current else "not yet set"
        await safe_reply(
            message,
            f"Effort for {window_id}: **{label}**\nUsage: /effort <high|medium|low|auto>",
        )
        return

    level = args[0].lower()
    if level == "med":
        level = "medium"
    if level not in ("high", "medium", "low", "auto"):
        await safe_reply(message, "⚠️ Invalid level. Use: high, medium, low, or auto")
        return

    # Inject the command into the tmux window
    from .tmux_manager import send_to_window

    cmd = f"/effort {level}"
    success, err = await send_to_window(window_id, cmd)
    if success:
        await safe_reply(message, f"✓ Sent: `{cmd}`")
    else:
        await safe_reply(message, f"⚠️ Failed: {err}")


def create_bot() -> Application:
    # Suppress PTBUserWarning about JobQueue (we intentionally don't use it for core tasks)
    import warnings

    warnings.filterwarnings("ignore", message=".*JobQueue.*", category=UserWarning)
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .request(ResilientPollingHTTPXRequest())
        .get_updates_request(ResilientPollingHTTPXRequest(connection_pool_size=1))
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_error_handler(_error_handler)
    application.add_handler(CommandHandler("new", new_command, filters=_group_filter))
    application.add_handler(
        CommandHandler("start", new_command, filters=_group_filter)  # compat alias
    )
    application.add_handler(
        CommandHandler("history", history_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("commands", commands_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("sessions", sessions_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("resume", resume_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("unbind", unbind_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("upgrade", upgrade_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("recall", recall_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("screenshot", screenshot_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("panes", panes_command, filters=_group_filter)
    )
    application.add_handler(CommandHandler("sync", sync_command, filters=_group_filter))
    application.add_handler(
        CommandHandler("toolbar", toolbar_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("verbose", verbose_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("restore", restore_command, filters=_group_filter)
    )
    # Bot-native convenience commands (Apr 2026)
    application.add_handler(
        CommandHandler("terminal", terminal_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("dashboard", dashboard_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("files", files_command, filters=_group_filter)
    )
    application.add_handler(CommandHandler("cwd", cwd_command, filters=_group_filter))
    application.add_handler(CommandHandler("busy", busy_command, filters=_group_filter))
    application.add_handler(
        CommandHandler("fleet", fleet_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("usage", usage_command, filters=_group_filter)
    )
    application.add_handler(
        CommandHandler("effort", effort_command, filters=_group_filter)
    )
    _load_callback_handlers()
    application.add_handler(CallbackQueryHandler(_dispatch_callback))
    # Topic closed event — unbind window (kept alive for rebinding)
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED & _group_filter,
            topic_closed_handler,
        )
    )
    # Topic renamed event — sync name to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED & _group_filter,
            topic_edited_handler,
        )
    )
    # Topic created event — show directory browser for new topics
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED & _group_filter,
            _forum_topic_created_handler,
        )
    )
    # Forward any other /command to the topic's provider CLI
    application.add_handler(
        MessageHandler(filters.COMMAND & _group_filter, forward_command_handler)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & _group_filter, text_handler)
    )
    # Photos
    application.add_handler(
        MessageHandler(filters.PHOTO & _group_filter, handle_photo_message)
    )
    # Documents
    application.add_handler(
        MessageHandler(filters.Document.ALL & _group_filter, handle_document_message)
    )
    # Voice messages (transcription when configured)
    application.add_handler(
        MessageHandler(filters.VOICE & _group_filter, handle_voice_message)
    )
    # Catch-all: unsupported content (stickers, voice, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND
            & ~filters.TEXT
            & ~filters.PHOTO
            & ~filters.Document.ALL
            & ~filters.VOICE
            & ~filters.StatusUpdate.ALL
            & _group_filter,
            unsupported_content_handler,
        )
    )
    # Inline query handler (serves switch_inline_query_current_chat from history buttons)
    application.add_handler(InlineQueryHandler(inline_query_handler))

    return application
