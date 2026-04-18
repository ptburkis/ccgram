"""Hook event dispatcher — routes structured events to handlers.

Receives HookEvent objects from the session monitor's event reader and
dispatches them to the appropriate handler based on event type. This
provides instant, structured notification of agent state changes instead
of relying solely on terminal scraping.

Key function: dispatch_hook_event().
"""

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path

import structlog

from telegram import Bot

from ..claude_task_state import claude_task_state, classify_wait_message
from ..providers.base import HookEvent
from ..session import session_manager
from ..thread_router import thread_router
from ..topic_state_registry import topic_state

logger = structlog.get_logger()

# Debounce for session-rotation notices: one per window per N seconds.
_ROTATION_NOTICE_DEBOUNCE_SECS = 30.0
# window_id -> last-notice timestamp
_rotation_notice_last: dict[str, float] = {}

# Set CCGRAM_ROTATION_NOTICES=1 to re-enable Telegram rotation notices for debugging.
_ROTATION_NOTICES_ENABLED = os.environ.get("CCGRAM_ROTATION_NOTICES", "0").strip() == "1"

# File-based IPC: dashboard writes intentional-stop markers here before pkill.
# Format: { "<window_id>": <epoch_float> }  — value is the "suppress until" time.
_INTENTIONAL_STOP_FILE = Path.home() / ".ccgram" / "intentional-stops.json"

_WINDOW_KEY_PARTS = 2


def _resolve_users_for_window_key(
    window_key: str,
) -> list[tuple[int, int, str]]:
    """Resolve window_key to list of (user_id, thread_id, window_id).

    The window_key format is "tmux_session:window_id" (e.g. "ccgram:@0").
    We extract the window_id part and look up thread bindings.
    """
    # Extract window_id from key (e.g. "ccgram:@0" -> "@0")
    parts = window_key.rsplit(":", 1)
    if len(parts) < _WINDOW_KEY_PARTS:
        return []
    window_id = parts[1]

    results: list[tuple[int, int, str]] = []
    for user_id, thread_id, bound_wid in thread_router.iter_thread_bindings():
        if bound_wid == window_id:
            results.append((user_id, thread_id, window_id))
    return results


async def _handle_notification(event: HookEvent, bot: Bot) -> None:
    """Handle a Notification event — render interactive UI."""
    from .interactive_ui import (
        clear_interactive_mode,
        get_interactive_window,
        handle_interactive_ui,
        set_interactive_mode,
    )
    from .message_queue import enqueue_status_update

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        logger.debug(
            "No users bound for notification event window_key=%s", event.window_key
        )
        return

    tool_name = event.data.get("tool_name", "")
    logger.debug(
        "Hook notification: tool_name=%s, window_key=%s",
        tool_name,
        event.window_key,
    )
    if not tool_name:
        # Empty tool_name = housekeeping notification, not a real interactive
        # prompt. Skip in summary mode to avoid phantom UI captures.
        all_summary = all(
            session_manager.get_notification_mode(wid) != "all" for _, _, wid in users
        )
        if all_summary:
            return
    wait_header = classify_wait_message(event.data.get("message", ""))

    for user_id, thread_id, window_id in users:
        if wait_header:
            claude_task_state.set_wait_header(window_id, wait_header)
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )

        # Skip if already in interactive mode for this window
        existing = get_interactive_window(user_id, thread_id)
        if existing == window_id:
            logger.debug(
                "Interactive mode already set for user=%d window=%s, skipping",
                user_id,
                window_id,
            )
            continue

        # Set interactive mode before rendering to prevent racing with terminal scraping
        set_interactive_mode(user_id, window_id, thread_id)

        # Wait briefly for Claude Code to render the UI in the terminal
        import asyncio

        await asyncio.sleep(0.3)

        handled = await handle_interactive_ui(bot, user_id, window_id, thread_id)
        if not handled:
            clear_interactive_mode(user_id, thread_id)


async def _enhance_with_llm_summary(
    bot: Bot,
    users: Sequence[tuple[int, int | None, str]],
    window_id: str,
    transcript_path: str,
    num_turns: int,
    session_id: str = "",
) -> None:
    """Async enhancement: replace Ready header with LLM summary via status queue."""
    try:
        from ..llm.summarizer import summarize_completion

        summary = await summarize_completion(transcript_path)
        if not summary:
            return

        from .message_queue import enqueue_status_update

        enhanced = claude_task_state.format_completion_text(
            window_id, num_turns=num_turns, session_id=session_id
        )
        enhanced = enhanced.replace("\u2713 Ready", f"\u2713 Done \u2014 {summary}", 1)

        for user_id, thread_id, _window_id in users:
            notif_mode = session_manager.get_notification_mode(window_id)
            if notif_mode == "all":
                await enqueue_status_update(
                    bot, user_id, window_id, enhanced, thread_id=thread_id
                )
    except (RuntimeError, OSError, ValueError):
        logger.debug("LLM summary enhancement failed", exc_info=True)


async def _handle_stop(event: HookEvent, bot: Bot) -> None:
    """Handle a Stop event — transition status directly to idle.

    Topic emoji remains poller-owned. Hook-driven idle flips can fight the
    transcript/activity heuristic and cause active/idle rename churn on quiet
    topics, so Stop only updates the status bubble and broker delivery state.
    Non-"all" notification mode windows get their status cleared instead.
    """
    from .message_queue import enqueue_status_update

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    stop_reason = event.data.get("stop_reason", "")
    logger.debug(
        "Hook stop: window_key=%s, stop_reason=%s",
        event.window_key,
        stop_reason,
    )

    num_turns = event.data.get("num_turns", 0)
    for user_id, thread_id, window_id in users:
        claude_task_state.clear_wait_header(window_id)
        notif_mode = session_manager.get_notification_mode(window_id)
        if notif_mode != "all":
            status_text = None
        else:
            status_text = claude_task_state.format_completion_text(
                window_id, num_turns=num_turns, session_id=event.session_id
            )
        await enqueue_status_update(
            bot, user_id, window_id, status_text, thread_id=thread_id
        )

    # Trigger immediate broker delivery for the idle window
    from .periodic_tasks import run_broker_cycle

    await run_broker_cycle(bot, idle_windows=frozenset({event.window_key}))

    # Fire async LLM summary enhancement (non-blocking)
    first_window_id = users[0][2]
    if first_window_id:
        transcript_path = session_manager.get_window_state(
            first_window_id
        ).transcript_path
        if transcript_path:
            import asyncio

            asyncio.create_task(
                _enhance_with_llm_summary(
                    bot,
                    users,
                    first_window_id,
                    transcript_path,
                    num_turns,
                    session_id=event.session_id,
                )
            )


# ── Subagent topic-name suffix (⚡) ───────────────────────────────────────
# ⚡ = inline subagent (Task tool) running.  🐚 = bg shell task (polling_coordinator.py).
# Both can coexist: "name 🐚 ⚡". Order: 🐚 first, then ⚡.

_SUBAGENT_SUFFIX = " \u26a1"  # ⚡
_BG_WORK_SUFFIX_EXT = (
    " \U0001f41a"  # 🐚 — owned by polling_coordinator, stripped here too
)


def _strip_both_suffixes(name: str) -> str:
    """Strip 🐚, ⚡, and [H/M/L] suffixes to get the clean base name."""
    result = name.rstrip()
    for suffix in (_SUBAGENT_SUFFIX.strip(), _BG_WORK_SUFFIX_EXT.strip()):
        result = result.removesuffix(suffix).rstrip()
    for effort_sfx in ("[H]", "[M]", "[L]"):
        result = result.removesuffix(effort_sfx).rstrip()
    return result


# Track active subagents per window: window_id -> {subagent_id -> name}
_active_subagents: dict[str, dict[str, str]] = {}

_MAX_DISPLAYED_NAMES = 3


def get_subagent_names(window_id: str) -> list[str]:
    """Return names of active subagents for a window."""
    return list(_active_subagents.get(window_id, {}).values())


def build_subagent_label(names: list[str]) -> str | None:
    """Build a display label for active subagents.

    Returns None if no subagents are active.
    """
    if not names:
        return None
    if len(names) == 1:
        return f"\U0001f916 {names[0]}"
    joined = ", ".join(names[:_MAX_DISPLAYED_NAMES])
    return f"\U0001f916 {len(names)} subagents: {joined}"


@topic_state.register("window")
def clear_subagents(window_id: str) -> None:
    """Clear all subagent tracking for a window."""
    _active_subagents.pop(window_id, None)


async def _apply_subagent_suffix(bot: Bot, users: list, *, add: bool) -> None:
    """Add/remove ⚡ suffix; preserves 🐚 if present. Order: 🐚 then ⚡."""
    from telegram.error import TelegramError as _TelegramError

    from .topic_emoji import _topic_names as _topic_names_cache

    for user_id, thread_id, window_id in users:
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if not chat_id:
            continue
        # Prefer the topic_emoji cache (reflects actual Telegram title) over
        # the DB-stored display_name which may be stale after a restart.
        cached = _topic_names_cache.get((chat_id, thread_id))
        display = thread_router.get_display_name(window_id) or ""
        live = cached if cached is not None else display
        clean = _strip_both_suffixes(live)
        has_bg = _BG_WORK_SUFFIX_EXT.strip() in live
        new_name = f"{clean}{_BG_WORK_SUFFIX_EXT}" if has_bg else clean
        if add:
            new_name = f"{new_name}{_SUBAGENT_SUFFIX}"
        if new_name == live:
            continue
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
                name=new_name,
            )
            session_manager.set_display_name(window_id, new_name)
            logger.debug(
                "Subagent suffix %s: %s -> %r",
                "added" if add else "removed",
                window_id,
                new_name,
            )
        except _TelegramError as e:
            logger.debug("Failed to edit topic for subagent suffix: %s", e)


async def _handle_subagent_start(event: HookEvent, bot: Bot) -> None:
    """Handle SubagentStart — track active subagent and notify."""
    from .message_queue import enqueue_status_update

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    window_id = users[0][2]  # all users share the same window_id
    subagent_id = event.data.get("subagent_id", "")
    name = (
        (event.data.get("name") or "").strip()
        or (event.data.get("description") or "").strip()
        or subagent_id[:12]
        or "subagent"
    )

    _active_subagents.setdefault(window_id, {})[subagent_id] = name

    logger.debug(
        "Subagent started: window=%s, count=%d, name=%s",
        window_id,
        len(_active_subagents[window_id]),
        name,
    )

    # Subagent start notices are noisy (one per Task tool call). Silent by
    # default; opt-in via CCGRAM_SUBAGENT_NOTICES=1. The ⚡ topic-name
    # suffix below still runs so users have a passive signal of activity.
    if os.environ.get("CCGRAM_SUBAGENT_NOTICES"):
        for user_id, thread_id, _ in users:
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                f"\U0001f916 Subagent started: {name}",
                thread_id=thread_id,
            )

    # Append ⚡ to topic name to indicate inline subagent is running.
    await _apply_subagent_suffix(bot, users, add=True)


async def _handle_subagent_stop(event: HookEvent, bot: Bot) -> None:
    """Handle SubagentStop — remove subagent from tracking and notify."""
    from .message_queue import enqueue_status_update

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    window_id = users[0][2]
    subagent_id = event.data.get("subagent_id", "")

    agents = _active_subagents.get(window_id)
    if not agents:
        return
    name = agents.pop(subagent_id, subagent_id[:12] or "subagent")
    if not agents:
        _active_subagents.pop(window_id, None)

    logger.debug(
        "Subagent stopped: window=%s, remaining=%d, name=%s",
        window_id,
        len(_active_subagents.get(window_id, {})),
        name,
    )

    # Silent by default; paired with the start-notice opt-in flag.
    if os.environ.get("CCGRAM_SUBAGENT_NOTICES"):
        for user_id, thread_id, _ in users:
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                f"\U0001f916 Subagent done: {name}",
                thread_id=thread_id,
            )

    # Remove ⚡ from topic name when all subagents for this window are done.
    if not _active_subagents.get(window_id):
        await _apply_subagent_suffix(bot, users, add=False)


async def _handle_teammate_idle(event: HookEvent, bot: Bot) -> None:
    """Handle TeammateIdle — notify topic that a teammate went idle."""
    from .message_queue import enqueue_status_update

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    teammate_name = event.data.get("teammate_name", "unknown")
    logger.info(
        "Teammate idle: window_key=%s, teammate=%s",
        event.window_key,
        teammate_name,
    )

    for user_id, thread_id, window_id in users:
        # Skip status updates in summary mode.
        if session_manager.get_notification_mode(window_id) != "all":
            continue
        text = f"\U0001f4a4 Teammate '{teammate_name}' went idle"
        await enqueue_status_update(bot, user_id, window_id, text, thread_id=thread_id)


def _is_intentional_stop(window_id: str) -> bool:
    """Return True if this window had an intentional stop marked recently."""
    try:
        data = json.loads(_INTENTIONAL_STOP_FILE.read_text())
        until = data.get(window_id, 0.0)
        return time.time() < until
    except (OSError, ValueError, KeyError):
        return False


async def _handle_stop_failure(event: HookEvent, bot: Bot) -> None:
    """Handle a StopFailure event — alert on unexpected agent termination."""
    from .message_sender import rate_limit_send_message

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    window_id = users[0][2]

    if _is_intentional_stop(window_id):
        logger.info("Hook StopFailure suppressed (intentional stop): window=%s", window_id)
        return

    error = event.data.get("error", "")
    error_details = event.data.get("error_details", "")
    logger.warning(
        "Hook StopFailure: window_key=%s, error=%s, details=%s",
        event.window_key,
        error or "(empty)",
        error_details,
    )

    # Empty/unknown error with no detail = nothing actionable for the user,
    # just noise. Log only. Only post to the topic when there's a real
    # actionable error string or details to show.
    _NOISE_ERRORS = {"", "unknown", "error", "none", "null"}
    if (error or "").strip().lower() in _NOISE_ERRORS and not error_details:
        logger.info(
            "StopFailure suppressed (no actionable detail): window=%s error=%r",
            window_id, error,
        )
        return
    if error:
        detail = f": {error_details}" if error_details else ""
        text = f"\u26a0 API error \u2014 {error}{detail}"
    else:
        text = f"\u26a0 Agent terminated: {error_details}"

    for user_id, thread_id, _window_id in users:
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        await rate_limit_send_message(bot, chat_id, text, message_thread_id=thread_id)


async def _handle_session_end(event: HookEvent, bot: Bot) -> None:
    """Handle a SessionEnd event — clean up session lifecycle."""
    from .message_queue import enqueue_status_update
    from .polling_strategies import clear_seen_status
    from .topic_emoji import update_topic_emoji

    data = event.data

    # Skip subagent SessionEnd — only primary sessions should clear state.
    if data.get('parent_session_id'):
        logger.debug('SessionEnd: skipping subagent event (parent_session_id present)')
        return

    transcript_path = data.get('transcript_path', '')
    if transcript_path and 'agents/' in transcript_path:
        logger.debug(
            'SessionEnd: skipping subagent event (transcript in agents/ dir: %s)',
            transcript_path,
        )
        return

    try:
        from ..pty_markers import read_marker_for_window as _read_marker
        window_id_for_check = event.window_key.rsplit(':', 1)[-1] if ':' in event.window_key else event.window_key
        marker = _read_marker(window_id_for_check)
        if marker and marker.get('session_id') and marker['session_id'] != event.session_id:
            logger.debug(
                'SessionEnd: PTY marker session_id %s != event session_id %s '
                '— subagent exit, skipping',
                marker['session_id'],
                event.session_id,
            )
            return
    except Exception:
        pass  # marker unavailable — allow through

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    reason = event.data.get("reason", "")
    logger.info(
        "Hook SessionEnd: window_key=%s, reason=%s",
        event.window_key,
        reason,
    )

    # Clear session association and subagent tracking so next launch starts fresh
    if users:
        window_id = users[0][2]
        claude_task_state.clear_window(window_id)
        session_manager.clear_window_session(window_id)
        clear_subagents(window_id)

    for user_id, thread_id, window_id in users:
        clear_seen_status(window_id)
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        display = thread_router.get_display_name(window_id)
        await update_topic_emoji(bot, chat_id, thread_id, "done", display)
        await enqueue_status_update(bot, user_id, window_id, None, thread_id=thread_id)


async def _handle_task_completed(event: HookEvent, bot: Bot) -> None:
    """Handle TaskCompleted — notify topic that a task was completed."""
    from .message_queue import enqueue_status_update

    users = _resolve_users_for_window_key(event.window_key)
    if not users:
        return

    task_subject = event.data.get("task_subject", "")
    teammate_name = event.data.get("teammate_name", "")
    logger.info(
        "Task completed: window_key=%s, task=%s, by=%s",
        event.window_key,
        task_subject,
        teammate_name,
    )

    for user_id, thread_id, window_id in users:
        task_id = event.data.get("task_id", "")
        tracked = False
        if task_id:
            tracked = claude_task_state.mark_task_completed(
                window_id,
                event.session_id,
                task_id,
                subject=task_subject,
            )
        if tracked or claude_task_state.has_snapshot(window_id):
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
            continue

        # Skip free-form task completion notifications in summary mode.
        if session_manager.get_notification_mode(window_id) != "all":
            continue

        text = f"\u2705 Task completed: {task_subject}"
        if teammate_name:
            text += f" (by '{teammate_name}')"
        await enqueue_status_update(bot, user_id, window_id, text, thread_id=thread_id)


async def _handle_session_start(event: HookEvent, bot: Bot) -> None:  # noqa: C901
    """Handle a SessionStart event — update session_map + monitor_state + DB.

    Resolution order for window_id:
    1. ``window_key`` field on the event (authoritative — hook sets it directly).
    2. session_id reverse lookup in window_store (edge case: hook arrives with
       previous session_id still in-flight; the window_key already has the
       correct @N so this path is only a safety net).

    After updating persistent state, ships a debounced rotation notice to the
    bound Telegram topic.
    """
    from ..session_autoheal import apply_session_update
    from ..store import (
        connect,
        get_binding_for_session,
        upsert_session,
    )
    from ..window_state_store import window_store
    from .message_sender import rate_limit_send_message

    window_id = _window_id_from_key(event.window_key)
    if not window_id:
        logger.warning(
            "SessionStart: cannot resolve window_id from window_key=%s",
            event.window_key,
        )
        return

    # Skip subagent SessionStart events — only handle primary-session rotations.
    if not _is_primary_session_start(event):
        logger.debug(
            "SessionStart: skipping subagent event for window %s (transcript=%s, parent=%s)",
            window_id,
            event.data.get("transcript_path", ""),
            event.data.get("parent_session_id", ""),
        )
        return

    new_sid = event.session_id

    # Guard A — skip if window_id is not live in tmux
    try:
        import subprocess as _subprocess
        result = _subprocess.run(
            ['tmux', 'list-windows', '-t', 'ccgram', '-F', '#{window_id}'],
            capture_output=True, text=True, timeout=2
        )
        live_windows = result.stdout.strip().splitlines()
        if window_id not in live_windows:
            logger.debug(
                'SessionStart: window %s not live in tmux — skipping stale hook',
                window_id,
            )
            return
    except Exception:
        pass  # tmux unavailable — allow through

    # Guard B — skip if PTY marker session_id doesn't match event.session_id
    try:
        from ..pty_markers import read_marker_for_window as read_marker
        marker = read_marker(window_id)
        if marker and marker.get('session_id') and marker['session_id'] != new_sid:
            logger.debug(
                'SessionStart: PTY marker session_id %s != event session_id %s '
                'for window %s — subagent mismatch, skipping',
                marker['session_id'],
                new_sid,
                window_id,
            )
            return
    except Exception:
        pass  # marker unavailable — allow through

    cwd = event.data.get("cwd", "")
    new_transcript = event.data.get("transcript_path", "")

    # Look up the old session_id from in-memory window_store for the CAS guard.
    state = window_store.get_window_state(window_id)
    old_sid = state.session_id or ""

    if not new_sid:
        logger.debug(
            "SessionStart: empty session_id for window %s — skipping", window_id
        )
        return

    if old_sid == new_sid:
        logger.debug(
            "SessionStart: session_id unchanged (%s) for window %s — idempotent",
            new_sid,
            window_id,
        )
        return

    logger.info(
        "SessionStart: rotation detected for %s — %s -> %s",
        window_id,
        old_sid or "(none)",
        new_sid,
    )

    # Update session_map.json + monitor_state.json + in-memory session_manager.
    await apply_session_update(
        window_id=window_id,
        old_sid=old_sid,
        new_sid=new_sid,
        new_transcript=new_transcript,
        source="SessionStart-hook",
    )

    # Update DB: upsert new session row + re-point any existing topic binding.
    try:
        with connect() as conn:
            upsert_session(
                conn,
                session_id=new_sid,
                cwd=cwd,
                agent="claude",
                status="active",
                window_id=window_id,
            )
            # Routing is done via PTY marker window_id lookup, not session_id.
            # Rebinding on session rotation caused churn from Task subagent spawns.
            pass
    except (OSError, RuntimeError, ValueError):
        logger.debug(
            "SessionStart: DB update failed for %s", window_id, exc_info=True
        )

    # Log rotation at INFO level. Only post to Telegram if explicitly enabled
    # (CCGRAM_ROTATION_NOTICES=1) — by default these are silent to avoid
    # flooding topics during subagent-heavy sessions.
    now = time.monotonic()
    last = _rotation_notice_last.get(window_id, 0.0)
    if now - last >= _ROTATION_NOTICE_DEBOUNCE_SECS:
        _rotation_notice_last[window_id] = now
        if _ROTATION_NOTICES_ENABLED:
            users = _resolve_users_for_window_key(event.window_key)
            for user_id, thread_id, _wid in users:
                chat_id = thread_router.resolve_chat_id(user_id, thread_id)
                if chat_id:
                    short_old = (old_sid[:8] + "\u2026") if old_sid else "(new)"
                    short_new = new_sid[:8] + "\u2026"
                    notice = (
                        f"\U0001f504 Session rotated: {short_old} \u2192 {short_new}"
                    )
                    try:
                        await rate_limit_send_message(
                            bot, chat_id, notice, message_thread_id=thread_id
                        )
                    except (OSError, RuntimeError):
                        logger.debug(
                            "SessionStart: rotation notice failed for %s",
                            window_id,
                            exc_info=True,
                        )


# Hook events that indicate the agent is actively working on something,
# even if the main jsonl transcript isn't being written to right now
# (e.g. during a long subagent run, or while a Bash call is in flight).
# These reset the activity timestamp so the typing indicator keeps firing
# and /busy reports the session as working.
# NOTE: "Notification" is deliberately EXCLUDED. Claude Code fires a
# Notification with empty tool_name ~60s after every Stop event as a
# housekeeping signal. Including it here would make every window look
# "recently active" permanently, causing the typing indicator to fire
# on ALL topics even when they're idle.
_BUSY_HOOK_EVENTS: frozenset[str] = frozenset(
    {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "SubagentStart",
        "PermissionRequest",
    }
)


def _is_primary_session_start(event: HookEvent) -> bool:
    """Return True only if this SessionStart is for a primary (non-subagent) session.

    Claude Code fires SessionStart on subagent spawns too. We filter them out by:
    1. Checking for a ``parent_session_id`` field in the payload — subagents carry one.
    2. Checking whether the transcript_path lives under an ``agents/`` subdirectory —
       e.g. ``~/.claude/projects/<slug>/agents/<parent_sid>/<child_sid>.jsonl``.
    """
    # Explicit parent_session_id in payload → subagent
    if event.data.get("parent_session_id"):
        return False

    transcript_path = event.data.get("transcript_path", "")
    if transcript_path:
        p = Path(transcript_path)
        # Check any ancestor directory named "agents"
        if "agents" in p.parts:
            return False

    return True


def _window_id_from_key(window_key: str) -> str:
    """Strip the tmux session prefix from a window_key ("ccgram:@16" → "@16")."""
    if ":" in window_key:
        return window_key.rsplit(":", 1)[1]
    return window_key


async def dispatch_hook_event(event: HookEvent, bot: Bot) -> None:
    """Route hook events to appropriate handlers."""
    # Mark activity for any event that indicates the agent is in a turn.
    # This keeps the typing indicator firing and /busy accurate even when
    # the main jsonl isn't being written (long subagent runs, long Bash
    # calls). Cleanly ignored if the session isn't tracked yet.
    if event.event_type in _BUSY_HOOK_EVENTS:
        try:
            from ..session_monitor import get_active_monitor

            mon = get_active_monitor()
            if mon is not None:
                wid = _window_id_from_key(event.window_key)
                if wid:
                    mon.record_hook_activity(wid)
        except (ImportError, AttributeError):
            pass

    match event.event_type:
        case "SessionStart":
            await _handle_session_start(event, bot)
        case "Notification":
            await _handle_notification(event, bot)
        case "Stop":
            await _handle_stop(event, bot)
        case "StopFailure":
            await _handle_stop_failure(event, bot)
        case "SessionEnd":
            await _handle_session_end(event, bot)
        case "SubagentStart":
            await _handle_subagent_start(event, bot)
        case "SubagentStop":
            await _handle_subagent_stop(event, bot)
        case "TeammateIdle":
            await _handle_teammate_idle(event, bot)
        case "TaskCompleted":
            await _handle_task_completed(event, bot)
        case (
            "UserPromptSubmit"
            | "PreToolUse"
            | "PostToolUse"
            | "PostToolUseFailure"
            | "PermissionRequest"
            | "ConfigChange"
            | "WorktreeCreate"
            | "WorktreeRemove"
            | "PreCompact"
        ):
            pass  # Not actionable beyond activity tracking above
        case _:
            logger.debug("Ignoring unknown hook event type: %s", event.event_type)
