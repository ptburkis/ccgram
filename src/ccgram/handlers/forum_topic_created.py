"""Handler for the FORUM_TOPIC_CREATED status update.

Fires when a user manually creates a new forum topic in Telegram.
Immediately presents a directory browser so the user can bind a working
directory + agent provider, or send /archive to leave the topic unbound.

Core responsibilities:
  - forum_topic_created_handler: entry-point registered in create_bot()
  - Auth and duplicate-binding guards
  - Prompt message + directory browser launch (mirrors text_handler unbound flow)
"""

from __future__ import annotations

from pathlib import Path

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..config import config
from ..thread_router import thread_router
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    build_directory_browser,
)
from .message_sender import safe_reply
from .user_state import PENDING_THREAD_ID

logger = structlog.get_logger(__name__)


async def forum_topic_created_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle FORUM_TOPIC_CREATED: show directory picker for fresh topics.

    Fires when a user manually creates a new forum topic in Telegram. The
    handler presents a directory browser so the user can bind a working
    directory and agent provider to the newly created topic.

    Early-return conditions (no action taken):
      - update.effective_user, update.message, or message.chat is None
      - User is not in the allowed list (config.is_user_allowed)
      - message.forum_topic_created is None (not the expected status update)
      - message.message_thread_id is None (cannot identify the topic)
      - Topic already has a window binding (already bound)
    """
    user = update.effective_user
    message = update.message
    chat = message.chat if message else None

    if user is None or message is None or chat is None:
        return
    if not config.is_user_allowed(user.id):
        return
    if message.forum_topic_created is None:
        return
    thread_id = message.message_thread_id
    if thread_id is None:
        return
    if thread_router.get_window_for_thread(user.id, thread_id) is not None:
        return  # Already bound.

    topic_name = message.forum_topic_created.name or f"topic-{thread_id}"

    await safe_reply(
        message,
        f"New topic **{topic_name}** created.\n\n"
        "Pick a working directory + agent to bind a session, "
        "or send /archive to leave it unbound.",
    )

    start_path = str(Path.cwd())
    msg_text, keyboard, subdirs = build_directory_browser(start_path, user_id=user.id)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
        context.user_data[PENDING_THREAD_ID] = thread_id

    await safe_reply(message, msg_text, reply_markup=keyboard)
    logger.info(
        "forum_topic_created: posted picker",
        user_id=user.id,
        thread_id=thread_id,
        topic_name=topic_name,
    )
