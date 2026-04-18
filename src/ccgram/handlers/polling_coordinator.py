"""Polling coordinator for terminal status monitoring.

Orchestrates the per-topic polling cycle: iterates thread bindings, delegates
to strategy classes for state, and handles terminal status parsing, interactive
UI detection, shell relay, and dead window notification.

Periodic tasks (broker delivery, autoclose, topic probing) are in
periodic_tasks.py. Transcript discovery is in transcript_discovery.py.

Key components:
  - status_poll_loop: Background polling task (entry point for bot.py)
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError

from ..claude_task_state import claude_task_state
from ..providers import get_provider_for_window
from ..providers.base import StatusUpdate
from ..session import session_manager
from ..session_monitor import get_active_monitor
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..utils import log_throttled
from .cleanup import clear_topic_state
from .interactive_ui import (
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
    set_interactive_mode,
)
from .message_queue import (
    clear_tool_msg_ids_for_topic,
    enqueue_status_update,
    get_message_queue,
)
from .message_sender import rate_limit_send_message
from .periodic_tasks import run_lifecycle_tasks, run_periodic_tasks
from .polling_strategies import (
    interactive_strategy,
    is_shell_prompt,
    lifecycle_strategy,
    terminal_strategy,
)
from .recovery_callbacks import build_recovery_keyboard
from .topic_emoji import update_topic_emoji
from .transcript_discovery import discover_and_register_transcript

if TYPE_CHECKING:
    from ..tmux_manager import TmuxWindow

logger = structlog.get_logger()

# ── Timing constants ──────────────────────────────────────────────────────

STATUS_POLL_INTERVAL = 1.0  # seconds


# Exponential backoff bounds for loop errors (seconds)
_BACKOFF_MIN = 2.0
_BACKOFF_MAX = 30.0

# Top-level loop resilience: catch any error to keep polling alive
_LoopError = (TelegramError, OSError, RuntimeError, ValueError)


# ── Background-work topic indicator ─────────────────────────────────────
#
# Parses Claude Code's status bar for "N local agent(s)" / "N task(s)"
# counts and renames the Telegram topic with a 🐚 suffix when background
# shell/task work is active (option C: one rename on transition, not on
# count change).
#
# Topic name transitions:
#   idle → busy:  "bulugo-dev"  →  "bulugo-dev 🐚"
#   busy → idle:  "bulugo-dev 🐚"  →  "bulugo-dev"
#
# ⚡ is now reserved for inline subagents (Task tool) — see hook_events.py.
# Both suffixes can coexist: "bulugo-dev 🐚 ⚡"
#
# Debounced at _BG_WORK_DEBOUNCE_SECS so rapid start/stop doesn't flicker.

import re as _re

_RE_LOCAL_AGENTS = _re.compile(r"(\d+)\s+local\s+agents?", _re.IGNORECASE)
_RE_BG_TASKS = _re.compile(
    r"(\d+)\s+(?:background\s+)?tasks?\s+(?:running|active|pending)", _re.IGNORECASE
)
# Broader fallback: just "N task(s)" in the status bar area
_RE_TASKS_SIMPLE = _re.compile(r"(\d+)\s+tasks?(?:\s|$)", _re.IGNORECASE)

_BG_WORK_SUFFIX = " \U0001f41a"  # 🐚
_SUBAGENT_SUFFIX_EXT = " \u26a1"  # ⚡ — owned by hook_events.py, stripped here too
_BG_WORK_DEBOUNCE_SECS = 5.0
_BG_WORK_STATE_FILE = Path.home() / ".ccgram" / "bg_work_shown.json"

# Per-window state: is background work currently reflected in the topic name?
_bg_work_shown: dict[str, bool] = {}  # window_id -> True if suffix is on
# Per-window: when did the detected state last change? (monotonic)
_bg_work_changed_at: dict[str, float] = {}
# Per-window: what state was last detected? (True = has work)
_bg_work_detected: dict[str, bool] = {}


# ── Effort indicator topic suffix [H/M/L] ───────────────────────────────
#
# Parses Claude Code's TUI footer for the current effort symbol:
#   ○ = low → [L]   ◐ = medium → [M]   ● = high → [H]
# Suffix is appended AFTER any 🐚/⚡ suffixes so the order is:
#   "project-name 🐚 ⚡ [H]"
#
# Debounced at _EFFORT_DEBOUNCE_SECS to avoid flicker.

_EFFORT_SUFFIX_L = " [L]"
_EFFORT_SUFFIX_M = " [M]"
_EFFORT_SUFFIX_H = " [H]"
_EFFORT_DEBOUNCE_SECS = 5.0
_EFFORT_STATE_FILE = Path.home() / ".ccgram" / "effort_shown.json"
_RE_EFFORT = _re.compile(r"([○●◐])\s+(low|medium|high)\b", _re.IGNORECASE)
_EFFORT_CHAR_MAP = {"○": "L", "◐": "M", "●": "H"}
_EFFORT_SUFFIX_MAP = {"L": " [L]", "M": " [M]", "H": " [H]"}

# Per-window: what effort level is currently shown in topic name? ('H'|'M'|'L'|None)
_effort_shown: dict[str, str | None] = {}
# Per-window: what effort level was last detected from pane?
_effort_detected: dict[str, str | None] = {}
# Per-window: when did the detected effort level last change? (monotonic)
_effort_changed_at: dict[str, float] = {}


def _save_bg_work_state() -> None:
    try:
        import json

        _BG_WORK_STATE_FILE.write_text(json.dumps(_bg_work_shown))
    except OSError:
        pass


def _load_bg_work_state() -> None:
    global _bg_work_shown
    try:
        import json

        if _BG_WORK_STATE_FILE.exists():
            _bg_work_shown = json.loads(_BG_WORK_STATE_FILE.read_text())
    except OSError, json.JSONDecodeError:
        pass


def _save_effort_state() -> None:
    try:
        import json

        _EFFORT_STATE_FILE.write_text(json.dumps(_effort_shown))
    except OSError:
        pass


def _load_effort_state() -> None:
    global _effort_shown
    try:
        import json

        if _EFFORT_STATE_FILE.exists():
            _effort_shown = json.loads(_EFFORT_STATE_FILE.read_text())
    except OSError, ValueError:
        pass


_RE_ANSI_STRIP = _re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _parse_bg_work_counts(pane_text: str) -> tuple[int, int]:
    """Extract (agent_count, task_count) from a Claude Code status bar.

    Scans only the last 5 lines of the pane (the status bar area).
    Strips ANSI escape sequences first — Claude Code's status bar wraps
    each word in its own color code, which would break whitespace-based
    regex matching.
    Returns (0, 0) if neither pattern is found.
    """
    tail = _RE_ANSI_STRIP.sub("", "\n".join(pane_text.split("\n")[-5:]))
    agent_match = _RE_LOCAL_AGENTS.search(tail)
    agents = int(agent_match.group(1)) if agent_match else 0
    task_match = _RE_BG_TASKS.search(tail) or _RE_TASKS_SIMPLE.search(tail)
    tasks = int(task_match.group(1)) if task_match else 0
    return agents, tasks


def _parse_effort(pane_text: str) -> str | None:
    """Extract effort level from Claude Code's TUI footer.

    Scans only the last 5 lines (the status bar area).
    Returns 'H', 'M', 'L', or None.
    """
    tail = _RE_ANSI_STRIP.sub("", "\n".join(pane_text.split("\n")[-5:]))
    match = _RE_EFFORT.search(tail)
    if not match:
        return None
    char = match.group(1)
    return _EFFORT_CHAR_MAP.get(char)


_RE_EFFORT_SUFFIX = _re.compile(r"(?:\s+(?:\[H\]|\[M\]|\[L\]|\u26a1|\U0001f41a))+$")


def _strip_effort_suffix(name: str) -> str:
    """Remove trailing effort/status suffixes from a topic name.

    Strips any combination of trailing: [H], [M], [L], ⚡, 🐚 — in any order.
    Returns the cleaned base, with trailing whitespace also removed.

    Examples:
        "james-2 [M]"       → "james-2"
        "James [L]"         → "James"
        "proj 🐚 ⚡ [H]"  → "proj"
        "clean"             → "clean"
    """
    return _RE_EFFORT_SUFFIX.sub("", name).rstrip()


def _strip_bg_suffix(name: str) -> str:
    """Remove 🐚, ⚡, and [H/M/L] suffixes from a topic name.

    bg-work (🐚), subagent (⚡), and effort ([H/M/L]) suffixes may be present
    simultaneously. This strips all so callers get a clean base name to
    reattach desired suffixes. Order of removal doesn't matter — we strip
    iteratively.
    """
    result = name.rstrip()
    for suffix in (_SUBAGENT_SUFFIX_EXT.strip(), _BG_WORK_SUFFIX.strip()):
        result = result.removesuffix(suffix).rstrip()
    for effort_sfx in ("[H]", "[M]", "[L]"):
        result = result.removesuffix(effort_sfx).rstrip()
    return result


async def _check_background_work(
    bot: Bot,
    window_id: str,
    thread_id: int | None,
    pane_text: str,
) -> None:
    """Check for background agent/task counts and update topic name if needed.

    Called from update_status_message on every poll cycle. Uses debouncing
    to avoid rapid renames.
    """
    if thread_id is None:
        return

    agents, tasks = _parse_bg_work_counts(pane_text)
    has_work = agents > 0 or tasks > 0

    now = time.monotonic()
    prev_detected = _bg_work_detected.get(window_id)

    if prev_detected is None or prev_detected != has_work:
        # State changed — start the debounce timer
        _bg_work_detected[window_id] = has_work
        _bg_work_changed_at[window_id] = now
        return  # wait for debounce

    # State stable — check if debounce period has elapsed
    changed_at = _bg_work_changed_at.get(window_id, now)
    if (now - changed_at) < _BG_WORK_DEBOUNCE_SECS:
        return  # still debouncing

    currently_shown = _bg_work_shown.get(window_id, False)
    if has_work == currently_shown:
        return  # already reflected in topic name — no-op

    # Time to rename
    chat_id = thread_router.resolve_chat_id(
        next(
            (
                uid
                for uid, tid, wid in thread_router.iter_thread_bindings()
                if wid == window_id
            ),
            0,
        ),
        thread_id,
    )
    if not chat_id:
        return

    display = thread_router.get_display_name(window_id) or ""
    clean_name = _strip_bg_suffix(display)

    # Preserve any ⚡ subagent suffix that may already be appended.
    # Order: 🐚 (bg shell work) first, then ⚡ (inline subagent).
    has_subagent_suffix = _SUBAGENT_SUFFIX_EXT.strip() in display
    if has_work:
        new_name = f"{clean_name}{_BG_WORK_SUFFIX}"
    else:
        new_name = clean_name
    if has_subagent_suffix:
        new_name = f"{new_name}{_SUBAGENT_SUFFIX_EXT}"

    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=new_name,
        )
        _bg_work_shown[window_id] = has_work
        _save_bg_work_state()
        session_manager.set_display_name(window_id, new_name)
        logger.debug(
            "Background work indicator: %s -> %r",
            window_id,
            new_name,
        )
    except TelegramError:
        pass  # non-critical, silently degrade


async def _fetch_live_topic_title(chat_id: int, thread_id: int) -> str | None:
    """Fetch the current live topic title from Telegram via MTProto.

    Returns the title string, or None when MTProto is unavailable (missing
    credentials, session file, network error). Never raises.
    """
    try:
        from ..mtproto_client import MTProtoClient  # lazy import — avoids hard dep

        client = MTProtoClient()
        async with client:
            topics = await client.get_forum_topics_by_id(chat_id, [thread_id])
        return topics[0].title if topics else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "effort_suffix.mtproto_unavailable",
            thread_id=thread_id,
            error=str(exc),
        )
        return None


async def _apply_effort_suffix(
    bot: Bot,
    window_id: str,
    thread_id: int,
    level: str | None,
) -> None:
    """Apply (or remove) an effort suffix on a Telegram topic.

    Preserves user-chosen titles: fetches the live Telegram topic title via
    MTProto, strips known suffixes to get the base, and compares it to the
    window name (case-insensitive). If they differ, the user renamed the topic —
    keep their base and only update the suffix. Falls back to window-name
    behaviour when MTProto is unavailable. Never blocks the poll loop.
    """
    user_id = next(
        (
            uid
            for uid, tid, wid in thread_router.iter_thread_bindings()
            if wid == window_id
        ),
        0,
    )
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    if not chat_id:
        return

    # Prefer authoritative live title; fall back to locally-stored display name.
    live_title = await _fetch_live_topic_title(chat_id, thread_id)
    display = (
        live_title
        if live_title is not None
        else (thread_router.get_display_name(window_id) or "")
    )

    live_base = _strip_effort_suffix(display)
    window_name = thread_router.get_display_name(window_id) or window_id
    window_base = _strip_effort_suffix(window_name)

    if live_base and live_base.lower() != window_base.lower():
        # User renamed the topic — preserve their chosen base, update suffix only.
        clean_name = live_base
        logger.debug(
            "effort_suffix.user_title_preserved",
            window_id=window_id,
            user_base=live_base,
            window_base=window_base,
        )
    else:
        # Auto-managed title — use window base (or live_base as last resort).
        clean_name = window_base if window_base else live_base

    new_name = f"{clean_name}{_EFFORT_SUFFIX_MAP[level]}" if level else clean_name

    if new_name == display:
        _effort_shown[window_id] = level
        return

    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=new_name,
        )
        _effort_shown[window_id] = level
        _save_effort_state()
        session_manager.set_display_name(window_id, new_name)
        logger.debug("Effort indicator: %s -> %r", window_id, new_name)
    except TelegramError:
        pass  # non-critical, silently degrade


async def _check_effort_suffix(
    bot: Bot,
    window_id: str,
    thread_id: int | None,
    pane_text: str,
) -> None:
    """Track detected effort level but do NOT apply it to the topic name.

    Effort level belongs in the status bubble only — not the topic title.
    This function tracks what Claude Code reports (for status display) and
    strips any stale effort suffix that somehow crept into the topic name.
    """
    if thread_id is None:
        return

    detected = _parse_effort(pane_text)
    # Track for status bubble consumption; never add to topic name.
    if detected is not None:
        _effort_detected[window_id] = detected

    # If a suffix was previously applied (e.g. from before this fix), strip it.
    currently_shown = _effort_shown.get(window_id)
    if currently_shown:
        await _apply_effort_suffix(bot, window_id, thread_id, None)
        _effort_shown[window_id] = None


# ── Typing throttle ─────────────────────────────────────────────────────


async def _send_typing_throttled(bot: Bot, user_id: int, thread_id: int | None) -> None:
    """Send typing indicator if enough time has elapsed since the last one."""
    if thread_id is None:
        return
    if lifecycle_strategy.is_typing_throttled(user_id, thread_id):
        return
    lifecycle_strategy.record_typing_sent(user_id, thread_id)
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    with contextlib.suppress(TelegramError):
        await bot.send_chat_action(
            chat_id=chat_id,
            message_thread_id=thread_id,
            action=ChatAction.TYPING,
        )


# ── RC state / pyte parsing ─────────────────────────────────────────────


def _parse_with_pyte(
    window_id: str,
    pane_text: str,
    columns: int = 0,
    rows: int = 0,
) -> StatusUpdate | None:
    """Parse terminal via pyte screen buffer for status and interactive UI."""
    return terminal_strategy.parse_with_pyte(window_id, pane_text, columns, rows)


# ── Transcript activity check ───────────────────────────────────────────


def _check_transcript_activity(window_id: str) -> bool:
    """Check if recent transcript writes indicate an active agent."""
    session_id = session_manager.get_session_id_for_window(window_id)
    if not session_id:
        return False

    mon = get_active_monitor()
    if not mon:
        return False
    last_activity = mon.get_last_activity(session_id)
    return terminal_strategy.is_recently_active(window_id, last_activity)


# ── Idle / no-status transitions ────────────────────────────────────────


async def _transition_to_idle(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int,
    chat_id: int,
    display: str,
    notif_mode: str,
) -> None:
    """Transition a window to idle state (emoji, autoclose, typing, status)."""
    terminal_strategy.cancel_startup_timer(window_id)
    await update_topic_emoji(bot, chat_id, thread_id, "idle", display)
    lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
    lifecycle_strategy.clear_typing_state(user_id, thread_id)
    if notif_mode == "all":
        from .callback_data import IDLE_STATUS_TEXT

        await enqueue_status_update(
            bot, user_id, window_id, IDLE_STATUS_TEXT, thread_id=thread_id
        )
    else:
        await enqueue_status_update(bot, user_id, window_id, None, thread_id=thread_id)


async def _handle_no_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    pane_current_command: str,
    notif_mode: str,
) -> None:
    """Handle a window with no provider-detected terminal status."""
    now = time.monotonic()
    is_active = _check_transcript_activity(window_id)

    if is_active:
        claude_task_state.clear_wait_header(window_id)
        await _send_typing_throttled(bot, user_id, thread_id)
        if thread_id is not None:
            chat_id = thread_router.resolve_chat_id(user_id, thread_id)
            display = thread_router.get_display_name(window_id)
            await update_topic_emoji(bot, chat_id, thread_id, "active", display)
            lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
        return

    if thread_id is None:
        return

    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    display = thread_router.get_display_name(window_id)

    if is_shell_prompt(pane_current_command):
        terminal_strategy.cancel_startup_timer(window_id)
        state = session_manager.get_window_state(window_id)
        raw_provider = getattr(state, "provider_name", "")
        provider_name = raw_provider.lower() if isinstance(raw_provider, str) else ""
        if provider_name in ("codex", "gemini", "shell"):
            terminal_strategy.mark_seen_status(window_id)
            await _transition_to_idle(
                bot, user_id, window_id, thread_id, chat_id, display, notif_mode
            )
            return

        await update_topic_emoji(bot, chat_id, thread_id, "done", display)
        lifecycle_strategy.start_autoclose_timer(user_id, thread_id, "done", now)
        lifecycle_strategy.clear_typing_state(user_id, thread_id)
        await enqueue_status_update(bot, user_id, window_id, None, thread_id=thread_id)
    elif terminal_strategy.check_seen_status(window_id):
        await _transition_to_idle(
            bot, user_id, window_id, thread_id, chat_id, display, notif_mode
        )
    elif terminal_strategy.get_state(window_id).startup_time is None:
        terminal_strategy.begin_startup_timer(window_id, now)
        await _send_typing_throttled(bot, user_id, thread_id)
        await update_topic_emoji(bot, chat_id, thread_id, "active", display)
        lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
    elif terminal_strategy.is_startup_expired(window_id):
        terminal_strategy.mark_seen_status(window_id)
        await _transition_to_idle(
            bot, user_id, window_id, thread_id, chat_id, display, notif_mode
        )
    else:
        await _send_typing_throttled(bot, user_id, thread_id)
        await update_topic_emoji(bot, chat_id, thread_id, "active", display)
        lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)


# ── Multi-pane scanning (agent teams) ─────────────────────────────────


async def _scan_window_panes(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int,
) -> None:
    """Scan non-active panes for interactive prompts and surface alerts."""
    if terminal_strategy.is_single_pane_cached(window_id):
        return

    now = time.monotonic()
    panes = await tmux_manager.list_panes(window_id)
    terminal_strategy.update_pane_count_cache(window_id, len(panes))
    live_pane_ids = {p.pane_id for p in panes}

    interactive_strategy.prune_stale_pane_alerts(window_id, live_pane_ids)

    if len(panes) <= 1:
        return

    now = time.monotonic()

    for pane in panes:
        if pane.active:
            continue

        pane_text = await tmux_manager.capture_pane_by_id(
            pane.pane_id, window_id=window_id
        )
        if not pane_text:
            continue

        provider = get_provider_for_window(window_id)
        status = provider.parse_terminal_status(pane_text, pane_title="")
        if status is None or not status.is_interactive:
            interactive_strategy.remove_pane_alert(pane.pane_id)
            continue

        prompt_text = status.raw_text or ""

        existing = interactive_strategy.get_pane_alert(pane.pane_id)
        if existing and existing[0] == prompt_text:
            continue

        interactive_strategy.set_pane_alert(pane.pane_id, prompt_text, now, window_id)
        logger.info(
            "Pane %s in window %s has interactive UI, surfacing alert",
            pane.pane_id,
            window_id,
        )
        await handle_interactive_ui(
            bot, user_id, window_id, thread_id, pane_id=pane.pane_id
        )


# ── Interactive-only check ───────────────────────────────────────────────


async def _check_interactive_only(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int,
    *,
    _window: "TmuxWindow | None" = None,
) -> None:
    """Check for interactive UI without enqueuing status updates."""
    w = _window or await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    if get_interactive_window(user_id, thread_id) == window_id:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not pane_text:
        return

    status = _parse_with_pyte(
        window_id, pane_text, columns=w.pane_width, rows=w.pane_height
    )

    if status is None:
        clean_text = terminal_strategy.get_rendered_text(window_id, pane_text)
        provider = get_provider_for_window(window_id)
        pane_title = ""
        if provider.capabilities.uses_pane_title:
            pane_title = await tmux_manager.get_pane_title(w.window_id)
        status = provider.parse_terminal_status(clean_text, pane_title=pane_title)

    if status is not None and status.is_interactive:
        set_interactive_mode(user_id, window_id, thread_id)
        handled = await handle_interactive_ui(bot, user_id, window_id, thread_id)
        if not handled:
            clear_interactive_mode(user_id, thread_id)


# ── Passive shell relay ──────────────────────────────────────────────────


async def _maybe_check_passive_shell(
    bot: Bot, user_id: int, window_id: str, thread_id: int
) -> None:
    """Relay shell output from direct tmux interaction to Telegram."""
    state = session_manager.get_window_state(window_id)
    if not state or state.provider_name != "shell":
        return
    ws = terminal_strategy.get_state(window_id)
    rendered = ws.last_rendered_text
    if rendered is None:
        raw = await tmux_manager.capture_pane(window_id)
        if not raw:
            return
        rendered = raw
    from .shell_capture import check_passive_shell_output

    await check_passive_shell_output(bot, user_id, thread_id, window_id, rendered)


# ── Dead window notification ─────────────────────────────────────────────


async def _handle_dead_window_notification(
    bot: Bot, user_id: int, thread_id: int, wid: str
) -> None:
    """Send proactive recovery notification for a dead window (once per death)."""
    if lifecycle_strategy.is_dead_notified(user_id, thread_id, wid):
        return
    terminal_strategy.clear_seen_status(wid)

    clear_tool_msg_ids_for_topic(user_id, thread_id)
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    display = thread_router.get_display_name(wid)
    await update_topic_emoji(bot, chat_id, thread_id, "dead", display)
    lifecycle_strategy.start_autoclose_timer(
        user_id, thread_id, "dead", time.monotonic()
    )

    window_state = session_manager.get_window_state(wid)
    cwd = window_state.cwd or ""
    try:
        dir_exists = bool(cwd) and await asyncio.to_thread(Path(cwd).is_dir)
    except OSError:
        dir_exists = False
    if dir_exists:
        keyboard = build_recovery_keyboard(wid)
        text = (
            f"\u26a0 Session `{display}` ended.\n"
            f"\U0001f4c2 `{cwd}`\n\n"
            "Tap a button or send a message to recover."
        )
    else:
        text = f"\u26a0 Session `{display}` ended."
        keyboard = None
    sent = await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        reply_markup=keyboard,
    )
    if sent is None:
        try:
            await bot.unpin_all_forum_topic_messages(
                chat_id=chat_id, message_thread_id=thread_id
            )
        except BadRequest as probe_err:
            if (
                "thread not found" in probe_err.message.lower()
                or "topic_id_invalid" in probe_err.message.lower()
            ):
                terminal_strategy.reset_probe_failures(wid)
                await clear_topic_state(
                    user_id,
                    thread_id,
                    bot,
                    window_id=wid,
                    window_dead=True,
                )
                thread_router.unbind_thread(user_id, thread_id)
                logger.info(
                    "Topic deleted: unbound window %s for thread %d, user %d",
                    wid,
                    thread_id,
                    user_id,
                )
        except TelegramError:
            pass
    lifecycle_strategy.mark_dead_notified(user_id, thread_id, wid)

    # Also persist: retire the DB session so subsequent polls don't rediscover
    # this dead window and re-alert after a daemon restart (in-memory
    # is_dead_notified state doesn't survive restart).
    try:
        from .. import store
        with store.connect() as _c:
            _c.execute(
                "UPDATE sessions SET status='retired', window_id=NULL WHERE window_id=? AND status='active'",
                (wid,),
            )
    except Exception:  # noqa: BLE001
        pass


# ── Main orchestration ──────────────────────────────────────────────────


async def _check_context_compaction(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int,
) -> None:
    """Proactively flush memory or compact context when usage is high.

    Called from status_poll_loop when the agent appears idle. Checks the
    transcript tail for context usage and sends a memory flush prompt or
    /compact command when thresholds are crossed.
    """
    from ..config import config as _cfg
    if not _cfg.context_compact_enabled:
        return

    from ..context_usage import (
        MEMORY_FLUSH_PROMPT,
        context_usage_tracker,
        get_latest_usage,
    )
    from ..tmux_manager import send_to_window

    tracker = context_usage_tracker

    if not tracker.should_check(window_id):
        return

    if tracker.is_in_cooldown(window_id):
        return

    if _check_transcript_activity(window_id):
        return

    state = session_manager.get_window_state(window_id)
    transcript_path = state.transcript_path if state else ""
    if not transcript_path:
        return

    try:
        usage, model = await asyncio.to_thread(get_latest_usage, transcript_path)
    except Exception:
        return

    tracker.record_check(window_id, None, model)

    action = tracker.determine_action(usage, model, window_id)
    if not action:
        return

    logger.info(
        "context_compaction.trigger",
        window_id=window_id,
        action=action,
        model=model,
    )

    if action == "flush":
        success, _ = await send_to_window(window_id, MEMORY_FLUSH_PROMPT)
        if success:
            tracker.record_flush(window_id)
    elif action == "compact":
        success, _ = await send_to_window(window_id, "/compact")
        if success:
            tracker.record_compact(window_id)


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    _window: "TmuxWindow | None" = None,
) -> None:
    """Poll terminal and enqueue status update for user's active window."""
    w = _window or await tmux_manager.find_window_by_id(window_id)
    if not w:
        await enqueue_status_update(bot, user_id, window_id, None, thread_id=thread_id)
        return

    pane_text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)

    # Check for background agent/task counts and update topic ⚡ suffix.
    try:
        await _check_background_work(bot, window_id, thread_id, pane_text or "")
    except Exception:
        pass  # non-critical — never let this break the main poll
    try:
        await _check_effort_suffix(bot, window_id, thread_id, pane_text or "")
    except Exception:
        pass
    if not pane_text:
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    status = _parse_with_pyte(
        window_id, pane_text, columns=w.pane_width, rows=w.pane_height
    )

    # Passive vim INSERT mode tracking
    from ..tmux_manager import _has_insert_indicator, notify_vim_insert_seen

    vim_text = terminal_strategy.get_rendered_text(window_id, pane_text)
    if _has_insert_indicator(vim_text):
        notify_vim_insert_seen(w.window_id)

    if status is None:
        clean_text = terminal_strategy.get_rendered_text(window_id, pane_text)
        provider = get_provider_for_window(window_id)
        pane_title = ""
        if provider.capabilities.uses_pane_title:
            pane_title = await tmux_manager.get_pane_title(w.window_id)
        status = provider.parse_terminal_status(clean_text, pane_title=pane_title)

    if interactive_window == window_id:
        if status is not None and status.is_interactive:
            return
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        await clear_interactive_msg(user_id, bot, thread_id)

    if should_check_new_ui and status is not None and status.is_interactive:
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    status_line = None
    if status and not status.is_interactive:
        if "\n" in status.raw_text:
            status_line = status.raw_text
        else:
            from ..terminal_parser import status_emoji_prefix

            emoji = status_emoji_prefix(status.raw_text)
            status_line = f"{emoji} {status.raw_text}"

    if status_line:
        try:
            from ..model_detector import get_current_model

            window_state = session_manager.get_window_state(window_id)
            transcript_path = window_state.transcript_path if window_state else None
            if transcript_path:
                model_label = get_current_model(transcript_path)
                if model_label:
                    status_line = f"{status_line} \u00b7 {model_label}"
        except Exception:
            pass

    notif_mode = session_manager.get_notification_mode(window_id)

    if status_line:
        claude_task_state.clear_wait_header(window_id)
        claude_task_state.set_last_status(window_id, status_line)
        terminal_strategy.mark_seen_status(window_id)
        await _send_typing_throttled(bot, user_id, thread_id)
        if notif_mode == "all":
            from .hook_events import build_subagent_label, get_subagent_names

            subagent_names = get_subagent_names(window_id)
            display_status = status_line
            if subagent_names:
                label = build_subagent_label(subagent_names)
                display_status = f"{status_line} ({label})"
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                display_status,
                thread_id=thread_id,
            )
        if thread_id is not None:
            chat_id = thread_router.resolve_chat_id(user_id, thread_id)
            display = thread_router.get_display_name(window_id)
            await update_topic_emoji(bot, chat_id, thread_id, "active", display)
            lifecycle_strategy.clear_autoclose_timer(user_id, thread_id)
    else:
        await _handle_no_status(
            bot, user_id, window_id, thread_id, w.pane_current_command, notif_mode
        )


# ── Startup cleanup ──────────────────────────────────────────────────────


async def _clear_stale_bg_indicators(bot: Bot) -> None:
    """Clear stale 🐚 bg-work suffixes and re-apply persisted effort suffixes on startup.

    On restart _bg_work_shown and _effort_shown reset to {}, so we load the
    persisted state and reconcile each topic:
      - bg-work (🐚): clear it (the work is no longer running)
      - effort ([H/M/L]): re-apply it (the level is still valid — sticky badge)
    """
    _load_bg_work_state()
    _load_effort_state()
    for user_id, thread_id, window_id in list(thread_router.iter_thread_bindings()):
        has_bg = _bg_work_shown.get(window_id, False)
        has_effort = bool(_effort_shown.get(window_id))
        if not has_bg and not has_effort:
            continue
        display = thread_router.get_display_name(window_id) or ""
        # Strip bg suffix (stale on restart); effort suffix handled below
        clean_name = _strip_bg_suffix(display)
        # Re-add effort suffix if persisted (it's sticky — don't remove it)
        effort_level = _effort_shown.get(window_id)
        if effort_level:
            clean_name = _strip_effort_suffix(clean_name)
            new_name = f"{clean_name.rstrip()}{_EFFORT_SUFFIX_MAP[effort_level]}"
        else:
            clean_name = _strip_effort_suffix(clean_name)
            new_name = clean_name
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if not chat_id:
            continue
        if new_name == display and not has_bg:
            continue  # nothing to do
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
                name=new_name,
            )
            session_manager.set_display_name(window_id, new_name)
            logger.info(
                "Startup indicator reconcile for %s: %r -> %r",
                window_id,
                display,
                new_name,
            )
        except TelegramError as exc:
            logger.warning(
                "Could not reconcile startup indicator for %s: %s",
                window_id,
                exc,
            )
    _bg_work_shown.update({k: False for k in _bg_work_shown})
    _save_bg_work_state()
    # effort_shown is preserved as-is (sticky)


# ── Main loop ─────────────────────────────────────────────────────────────


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    await _clear_stale_bg_indicators(bot)
    timers = {"topic_check": 0.0, "broker": 0.0, "sweep": 0.0}
    _error_streak = 0
    while True:
        try:
            all_windows = await tmux_manager.list_windows()
            external_windows = await tmux_manager.discover_external_sessions()
            all_windows.extend(external_windows)
            window_lookup: dict[str, "TmuxWindow"] = {
                w.window_id: w for w in all_windows
            }

            await run_periodic_tasks(bot, all_windows, timers)

            for user_id, thread_id, wid in list(thread_router.iter_thread_bindings()):
                structlog.contextvars.clear_contextvars()
                structlog.contextvars.bind_contextvars(window_id=wid)
                try:
                    if lifecycle_strategy.is_dead_notified(user_id, thread_id, wid):
                        continue

                    w = window_lookup.get(wid)
                    if not w:
                        await _handle_dead_window_notification(
                            bot, user_id, thread_id, wid
                        )
                        continue

                    await discover_and_register_transcript(
                        wid,
                        _window=w,
                        bot=bot,
                        user_id=user_id,
                        thread_id=thread_id,
                    )

                    queue = get_message_queue(user_id)
                    if queue and not queue.empty():
                        await _check_interactive_only(
                            bot, user_id, wid, thread_id, _window=w
                        )
                        await _scan_window_panes(bot, user_id, wid, thread_id)
                        await _maybe_check_passive_shell(bot, user_id, wid, thread_id)
                        continue
                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        _window=w,
                    )
                    # Proactive context compaction — only when idle
                    if not _check_transcript_activity(wid):
                        try:
                            await _check_context_compaction(bot, user_id, wid, thread_id)
                        except Exception:
                            pass
                    await _scan_window_panes(bot, user_id, wid, thread_id)
                    await _maybe_check_passive_shell(bot, user_id, wid, thread_id)
                except (TelegramError, OSError) as e:
                    log_throttled(
                        logger,
                        f"status-update:{user_id}:{thread_id}",
                        "Status update error for user %s thread %s: %s",
                        user_id,
                        thread_id,
                        e,
                    )

            await run_lifecycle_tasks(bot, all_windows)

        except _LoopError:
            logger.exception("Status poll loop error")
            backoff_delay = min(_BACKOFF_MAX, _BACKOFF_MIN * (2**_error_streak))
            _error_streak += 1
            await asyncio.sleep(backoff_delay)
            continue
        except Exception:
            logger.exception("Unexpected error in status poll loop")
            backoff_delay = min(_BACKOFF_MAX, _BACKOFF_MIN * (2**_error_streak))
            _error_streak += 1
            await asyncio.sleep(backoff_delay)
            continue

        _error_streak = 0
        await asyncio.sleep(STATUS_POLL_INTERVAL)
