"""Handle unbound-window events by alerting the operator or reusing a known topic.

Auto-creation of Telegram forum topics is retired (Phase 5, Chunk J).  The only
two things this module now does when a window has no binding:

  Path A — no topic hint: post a single operator alert to
      ``settings.CCGRAM_ALERT_THREAD_ID`` (default 529, the james-claude-hub
      topic).  Alerts are debounced per window_id (one per 30 minutes) so
      noisy unbound windows don't spam the operator channel.

  Path B — caller supplies ``existing_topic_id``: route through
      ``session_lifecycle.create_session(existing_topic_id=...)`` so the
      window gets bound on the canonical path without creating a new topic.

Core responsibilities (retained):
  - handle_new_window(): entry point from session_monitor
  - adopt_unbound_windows(): post-restart recovery of orphaned windows
"""

from __future__ import annotations

import time
from pathlib import Path

import structlog
from telegram import Bot

from ..config import config
from ..providers import (
    detect_provider_from_pane,
    detect_provider_from_runtime,
    should_probe_pane_title_for_provider_detection,
)
from ..session import session_manager
from ..session_monitor import NewWindowEvent
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Operator-alert debounce
# One alert per window_id per _ALERT_DEBOUNCE_SECONDS (30 min).
# Keyed on window_id; value is the monotonic timestamp of the last alert sent.
# ---------------------------------------------------------------------------
_ALERT_DEBOUNCE_SECONDS: int = 30 * 60
_last_alert_sent: dict[str, float] = {}
_boot_time: float = __import__("time").monotonic()

_ALERT_TEMPLATE = (
    "⚠️ Unbound window {window_id!r} ({window_name!r}, cwd={cwd!r}) emitted output.\n"
    "Run `claude-hub reconcile` or "
    "`claude-hub spawn --cwd \u2026 --topic \u2026 --agent \u2026 --group {chat_id}` to bind a session.\n"
    "(Suppressed auto-create to prevent topic corruption.)"
)


def _is_window_already_bound(window_id: str) -> bool:
    """Check if a window is already bound to any topic (in-memory or DB)."""
    if thread_router.has_window(window_id):
        return True
    try:
        from ..store import connect
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM topic_bindings WHERE window_id = ?",
                (window_id,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


async def _auto_detect_provider(window_id: str) -> None:
    """Auto-detect provider from the running process if not already set.

    detect_provider_from_command returns "" for unrecognized commands (shells),
    so we only persist when a known CLI is confidently identified.
    """
    existing_provider = session_manager.get_window_state(window_id).provider_name
    if existing_provider:
        return

    w = await tmux_manager.find_window_by_id(window_id)
    if not w or not w.pane_current_command:
        return

    detected = await detect_provider_from_pane(
        w.pane_current_command,
        pane_tty=w.pane_tty,
        window_id=window_id,
    )
    if not detected and should_probe_pane_title_for_provider_detection(
        w.pane_current_command
    ):
        pane_title = await tmux_manager.get_pane_title(window_id)
        detected = detect_provider_from_runtime(
            w.pane_current_command,
            pane_title=pane_title,
        )
    if detected:
        session_manager.set_window_provider(window_id, detected)
        logger.info(
            "Auto-detected provider %r for window %s (command=%s)",
            detected,
            window_id,
            w.pane_current_command,
        )


def collect_target_chats(window_id: str) -> set[int]:
    """Collect unique group chat IDs for alert dispatch."""
    seen_chats: set[int] = set()
    for user_id, thread_id, _ in thread_router.iter_thread_bindings():
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if isinstance(chat_id, int) and chat_id < 0:
            seen_chats.add(chat_id)

    if not seen_chats:
        seen_chats.update(
            cid for cid in thread_router.group_chat_ids.values() if cid < 0
        )

    if not seen_chats:
        if config.group_id:
            seen_chats.add(config.group_id)
            logger.info(
                "Cold-start: using CCGRAM_GROUP_ID=%d for alert (window %s)",
                config.group_id,
                window_id,
            )
        else:
            logger.debug(
                "No group chats found for unbound-window alert (window %s)",
                window_id,
            )

    return seen_chats


async def create_topic_in_chat(
    bot: Bot,
    chat_id: int,
    window_id: str,
    topic_name: str,
    *,
    existing_topic_id: int | None = None,
) -> None:
    """Handle an unbound window that emitted output.

    Path A (existing_topic_id is None):
        Post a single operator alert to the configured alert thread, debounced
        to one message per window per 30 minutes.  No topic is created.

    Path B (existing_topic_id is not None):
        Route through ``session_lifecycle.create_session(existing_topic_id=...)``
        to bind the window to the pre-existing topic on the canonical path.
    """
    if existing_topic_id is not None:
        # Path B — reuse a known topic via the canonical lifecycle.
        try:
            from ccgram import session_lifecycle as _sl

            ws = session_manager.get_window_state(window_id)
            cwd = ws.cwd or ""
            agent = ws.provider_name or "claude"
            mode = ws.approval_mode if hasattr(ws, "approval_mode") else None
            if cwd:
                await _sl.create_session(
                    cwd=cwd,
                    topic_name=topic_name,
                    agent=agent,
                    mode=mode,
                    group_id=chat_id,
                    existing_topic_id=existing_topic_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "topic_orchestration: create_session (Path B) failed: %s", exc
            )
        return

    # Path A — no hint; alert the operator (debounced).
    now = time.monotonic()
    last_sent = _last_alert_sent.get(window_id, 0.0)
    if now - last_sent < _ALERT_DEBOUNCE_SECONDS:
        logger.debug(
            "unbound_window_alert debounced window=%s (next in %.0fs)",
            window_id,
            _ALERT_DEBOUNCE_SECONDS - (now - last_sent),
        )
        return

    ws = session_manager.get_window_state(window_id)
    cwd = ws.cwd or ""

    alert_text = _ALERT_TEMPLATE.format(
        window_id=window_id,
        window_name=topic_name,
        cwd=cwd,
        chat_id=chat_id,
    )

    alert_thread_id = config.alert_thread_id
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=alert_text,
            message_thread_id=alert_thread_id,
        )
        _last_alert_sent[window_id] = now
        logger.warning(
            "unbound_window_alert_sent window=%s chat=%d thread=%d",
            window_id,
            chat_id,
            alert_thread_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "topic_orchestration: failed to send unbound-window alert: %s", exc
        )


async def handle_new_window(event: NewWindowEvent, bot: Bot) -> None:
    """Handle a newly detected tmux window that has no topic binding.

    Skips if the window is already bound to a topic.  Skips web-terminal
    mirror windows (transient; no canonical topic).
    """
    # Defensive: never alert for web-terminal grouped mirror sessions.
    if ":" in event.window_id and event.window_id.split(":", 1)[0].startswith("web-"):
        logger.debug(
            "Skipping alert for web-terminal mirror window %s",
            event.window_id,
        )
        return

    if config.is_system_window(event.window_name or "") or config.is_system_window_id(event.window_id):
        logger.debug("Skipping alert for system window %s", event.window_id)
        return

    if _is_window_already_bound(event.window_id):
        logger.debug("New window %s already bound, skipping alert", event.window_id)
        return

    # Suppress alerts during startup grace period (DB may not be loaded yet)
    import time as _time
    if _time.monotonic() - _boot_time < 60:
        logger.debug("Startup grace: suppressing alert for %s", event.window_id)
        return

    await _auto_detect_provider(event.window_id)

    import re as _re
    raw_topic = event.window_name or Path(event.cwd).name or event.window_id
    topic_name = raw_topic if not _re.match(r'^@\d+$', raw_topic) else (Path(event.cwd).name or event.window_id)
    seen_chats = collect_target_chats(event.window_id)
    if not seen_chats:
        return

    for chat_id in seen_chats:
        await create_topic_in_chat(bot, chat_id, event.window_id, topic_name)


async def adopt_unbound_windows(bot: Bot) -> None:
    """Auto-adopt known-but-unbound windows (post-restart recovery)."""
    all_windows = await tmux_manager.list_windows()
    live_ids = {w.window_id for w in all_windows}
    live_pairs = [(w.window_id, w.window_name) for w in all_windows]
    audit = session_manager.audit_state(live_ids, live_pairs)
    orphaned = [i for i in audit.issues if i.category == "orphaned_window"]
    if orphaned:
        from .sync_command import _adopt_orphaned_windows

        await _adopt_orphaned_windows(bot, orphaned)
        logger.info("Startup: adopted %d unbound window(s)", len(orphaned))


# ---------------------------------------------------------------------------
# Compatibility shims — retained so callers that imported these names
# (e.g. test_cleanup_gaps.py, TopicStateRegistry) continue to work without
# changes during the Phase 5 soak period.
# _topic_create_retry_until is now an alias for the alert debounce dict;
# clear_topic_create_retry clears the entry for the given chat_id (window_id
# in the new model, but chat_id is the legacy key convention used by callers).
# ---------------------------------------------------------------------------

_topic_create_retry_until: dict[int, float] = {}  # type: ignore[assignment]
"""Legacy compat alias — the old per-chat flood-control backoff dict.

Auto-create is retired; this dict is kept empty.  Callers that cleared it
(e.g. TopicStateRegistry) continue to compile and run without error.
"""


def clear_topic_create_retry(chat_id: int, _thread_id: int = 0) -> None:
    """Compat shim — clear the legacy backoff entry for this chat.

    Auto-create is retired; the dict is always empty, so this is a no-op.
    Retained to avoid import errors in callers that still reference it.
    """
    _topic_create_retry_until.pop(chat_id, None)
