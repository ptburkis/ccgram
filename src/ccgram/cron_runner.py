"""Cron dispatch helpers — resolve and fire scheduled cron messages.

The actual scheduling loop lives in the external ``claude-hub`` CLI.  This
module provides the store-aware target resolution and tmux dispatch that the
scheduler calls, so the logic can be tested independently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3

import structlog

from .store import Cron, resolve_cron_target

logger = structlog.get_logger()
_std_logger = logging.getLogger(__name__)


async def fire_cron(
    cron: Cron,
    conn: sqlite3.Connection,
    tmux_session: str,
    *,
    send_keys_fn=None,
) -> dict:
    """Resolve *cron*'s target and dispatch its message via tmux send-keys.

    Args:
        cron: The Cron row to fire.
        conn: Open SQLite connection (read-only usage).
        tmux_session: The tmux session name (e.g. ``"ccgram"``).
        send_keys_fn: Optional async callable ``(target: str, message: str) ->
            None`` -- injected for testing.  Defaults to a subprocess call.

    Returns:
        A result dict with keys ``fired`` (bool), ``source``, ``window_id``,
        ``error`` (str or None).
    """
    target_info = resolve_cron_target(cron, conn)

    if target_info is None:
        _std_logger.error(
            "cron %d (%r): no target resolved -- skipping", cron.id, cron.name
        )
        return {"fired": False, "source": None, "window_id": None, "error": "no_target"}

    source = target_info["source"]
    window_id = target_info["window_id"]

    if source == "legacy_window":
        _std_logger.warning(
            "cron %d (%r): firing via legacy window=%r -- migrate to session/topic targeting",
            cron.id,
            cron.name,
            window_id,
        )

    if not window_id:
        _std_logger.error(
            "cron %d (%r): resolved via %s but window_id is None -- skipping",
            cron.id,
            cron.name,
            source,
        )
        return {
            "fired": False,
            "source": source,
            "window_id": None,
            "error": "no_window_id",
        }

    tmux_target = f"{tmux_session}:{window_id}"

    try:
        if send_keys_fn is not None:
            await send_keys_fn(tmux_target, cron.message)
        else:
            await _default_send_keys(tmux_target, cron.message)
    except Exception as exc:  # noqa: BLE001
        _std_logger.error("cron %d (%r): send_keys failed: %s", cron.id, cron.name, exc)
        return {
            "fired": False,
            "source": source,
            "window_id": window_id,
            "error": str(exc),
        }

    return {"fired": True, "source": source, "window_id": window_id, "error": None}


async def _default_send_keys(target: str, message: str) -> None:
    """Submit *message* to a Claude Code TUI pane.

    Uses the same proven pattern as `claude-hub send`: one send-keys call
    with body + Enter, then a second Enter 0.5s later if the message is
    multi-line (Claude Code's TUI requires a confirm-Enter after a
    multi-line paste).
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", target, message, "Enter",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"tmux send-keys failed (rc={proc.returncode}): {stderr.decode()[:200]}"
        )

    if "\n" in message:
        await asyncio.sleep(0.5)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", target, "Enter",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
