"""Single liveness oracle for CCGram tmux windows.

All decisions about whether a window is alive route through this module.
No other code should perform liveness checks directly.

Two signals are combined:
  1. PTY marker file — confirms this is a tracked agent window (not a random shell).
  2. tmux list-windows — confirms the window still exists in the ccgram session.

Design choices:
  - Tmux unreachable (subprocess error / OSError) -> fail-safe True.
    Matches existing ``_has_live_pty_marker`` semantics: don't retire if we can't confirm dead.
  - Marker timestamp (last_seen_at) is deliberately ignored.
    It only updates while the agent is processing tokens; idle sessions look stale
    even though the window is alive.
  - These are plain (sync) functions despite the module being imported from async
    contexts -- the underlying operations are all synchronous I/O.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


class WindowDeadError(Exception):
    """Raised by ``assert_alive_or_raise`` when the window is confirmed dead."""


def is_window_alive(window_id: str) -> bool:
    """Return True iff a PTY marker exists for ``window_id`` AND tmux still has it.

    Tmux unreachable (subprocess error / OSError) -> fail-safe True.
    """
    # Step 1: check for PTY marker
    from .pty_markers import read_marker_for_window
    marker = read_marker_for_window(window_id)
    if marker is None:
        return False

    # Step 2: confirm tmux still has the window
    try:
        out = subprocess.check_output(
            ["tmux", "list-windows", "-t", "ccgram", "-F", "#{window_id}"],
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        # tmux unreachable -- fail safe, don't retire
        return True

    return window_id in out.split()


def confirm_dead_or_skip(window_id: str, reason: str) -> bool:
    """Re-check liveness before retiring.

    Returns True only when both signals confirm dead.
    If the window is still alive, logs a WARNING with ``reason`` and returns False
    so the caller skips the retirement write.

    Use this in any path that would write ``status='retired'`` based on a transient
    signal (e.g. a single failed ``find_window_by_id`` call).
    """
    alive = is_window_alive(window_id)
    if alive:
        logger.warning(
            "confirm_dead_or_skip: skipped retirement for window %s -- window alive per authority. reason=%s",
            window_id,
            reason,
        )
        return False
    return True


def assert_alive_or_raise(window_id: str) -> None:
    """Assert the window is alive; raise ``WindowDeadError`` if not.

    For paths that must abort immediately when a window is dead (e.g. attempting
    to send a message to a window that no longer exists).
    """
    if not is_window_alive(window_id):
        raise WindowDeadError(f"Window {window_id!r} is not alive")
