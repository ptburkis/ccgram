"""Sync-check: compare Telegram topic state vs actual state and report drift.

Compares every topic binding against the live Telegram title (via MTProto)
and the expected state (⚡ subagent activity, 🐚 bg-work, base name).
"""

from __future__ import annotations

import asyncio
import subprocess
import re
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

# ── Suffix constants (same as polling_coordinator) ─────────────────────────

_BOLT_SUFFIX = " \u26a1"   # ⚡ — subagent active
_SHELL_SUFFIX = " \U0001f41a"  # 🐚 — bg work
_RE_SUFFIXES = re.compile(
    r"(?:\s+(?:\u26a1|\U0001f41a|\[H\]|\[M\]|\[L\]))+$"
)


def _strip_all_suffixes(name: str) -> str:
    """Strip ⚡, 🐚, [H], [M], [L] suffixes from a topic title."""
    return _RE_SUFFIXES.sub("", name).rstrip()


@dataclass
class SyncCheckItem:
    window_id: str
    topic_id: int
    telegram_title: str
    correct_name: str
    has_bolt: bool
    should_bolt: bool
    has_shell: bool
    should_shell: bool
    has_effort: bool
    name_match: bool

    @property
    def drifted(self) -> bool:
        return (
            self.has_bolt != self.should_bolt
            or self.has_shell != self.should_shell
            or self.has_effort
            or not self.name_match
        )


@dataclass
class SyncCheckReport:
    items: list[SyncCheckItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def drifted_count(self) -> int:
        return sum(1 for it in self.items if it.drifted)


def _is_agent_active(window_id: str) -> bool:
    """Return True if this window has active subagent or inline tool activity."""
    try:
        from .handlers.hook_events import _active_subagents
        if _active_subagents.get(window_id):
            return True
    except Exception:
        pass

    try:
        from .config import config as _cfg
        tmux_session = _cfg.tmux_session_name
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{tmux_session}:{window_id}", "-p", "-S", "-15"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            lines = r.stdout.strip().split("\n")
            from .terminal_parser import detect_inline_tool_activity
            activity = detect_inline_tool_activity(lines)
            if activity is not None:
                return True
    except Exception:
        pass

    return False


def _is_bg_work_shown(window_id: str) -> bool:
    """Return True if bg-work (🐚) is currently tracked as shown for window."""
    try:
        from .handlers.polling_coordinator import _bg_work_shown
        return bool(_bg_work_shown.get(window_id, False))
    except Exception:
        return False


async def _fetch_telegram_title(chat_id: int, thread_id: int) -> str | None:
    """Fetch live topic title from Telegram via MTProto. Returns None on failure."""
    try:
        from .mtproto_client import MTProtoClient
        client = MTProtoClient()
        async with client:
            topics = await client.get_forum_topics_by_id(chat_id, [thread_id])
        return topics[0].title if topics else None
    except Exception as exc:
        logger.debug("sync_check.mtproto_unavailable", thread_id=thread_id, error=str(exc))
        return None


async def _run_sync_check_async(fix: bool = False) -> SyncCheckReport:
    """Async implementation of run_sync_check."""
    from .thread_router import thread_router
    from .config import config as _cfg

    report = SyncCheckReport()

    bot = None
    if fix:
        try:
            from telegram import Bot
            bot = Bot(token=_cfg.bot_token)
        except Exception as exc:
            logger.warning("sync_check.bot_init_failed", error=str(exc))

    for user_id, thread_id, window_id in list(thread_router.iter_thread_bindings()):
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if not chat_id:
            continue

        tg_title = await _fetch_telegram_title(chat_id, thread_id)
        if tg_title is None:
            tg_title = thread_router.get_display_name(window_id) or ""

        has_bolt = _BOLT_SUFFIX.strip() in tg_title
        has_shell = _SHELL_SUFFIX.strip() in tg_title
        has_effort = bool(re.search(r"\[\s*[HML]\s*\]", tg_title))
        base_from_tg = _strip_all_suffixes(tg_title)

        correct_name = ""
        try:
            from .pty_markers import read_marker_for_window
            marker = read_marker_for_window(window_id)
            if marker:
                correct_name = marker.get("window_name", "")
        except Exception:
            pass

        if not correct_name:
            correct_name = thread_router.get_display_name(window_id) or window_id
            correct_name = _strip_all_suffixes(correct_name)

        name_match = base_from_tg.lower() == correct_name.lower()

        should_bolt = _is_agent_active(window_id)
        should_shell = _is_bg_work_shown(window_id)

        item = SyncCheckItem(
            window_id=window_id,
            topic_id=thread_id,
            telegram_title=tg_title,
            correct_name=correct_name,
            has_bolt=has_bolt,
            should_bolt=should_bolt,
            has_shell=has_shell,
            should_shell=should_shell,
            has_effort=has_effort,
            name_match=name_match,
        )
        report.items.append(item)

        if fix and item.drifted and bot is not None:
            correct_title = correct_name
            if should_shell:
                correct_title += _SHELL_SUFFIX
            if should_bolt:
                correct_title += _BOLT_SUFFIX
            try:
                async with bot:
                    await bot.edit_forum_topic(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        name=correct_title,
                    )
                logger.info(
                    "sync_check.fixed",
                    window_id=window_id,
                    from_title=tg_title,
                    to_title=correct_title,
                )
            except Exception as exc:
                logger.debug("sync_check.fix_failed", window_id=window_id, error=str(exc))

    return report


def run_sync_check(fix: bool = False) -> SyncCheckReport:
    """Run a synchronous sync-check (safe to call from any thread)."""
    return asyncio.run(_run_sync_check_async(fix=fix))
