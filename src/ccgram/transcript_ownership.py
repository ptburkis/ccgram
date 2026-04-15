"""Shared helper for determining JSONL transcript ownership.

This module centralises the hook-marker check that binds a JSONL file to a
specific tmux window.  Both session_autoheal and session_watcher import from
here; the logic must never be duplicated.

The marker written by hook.py on SessionStart looks like::

    tmux key=ccgram:@N, window_name=<wname>, session_id=<stem>

where <stem> is the JSONL filename without the ``.jsonl`` extension (the
Claude session UUID).
"""

from __future__ import annotations

from pathlib import Path

# Maximum bytes to read when scanning for the hook marker.  50 MB is generous —
# real hook markers appear in the first few kilobytes of a fresh transcript.
_MAX_SCAN_BYTES = 50 * 1024 * 1024


def jsonl_has_hook_marker(path: Path, window_id: str, window_name: str) -> bool:
    """Return True if *path* contains the hook marker for *window_id* / *window_name*.

    Reads at most :data:`_MAX_SCAN_BYTES` bytes to avoid OOM on runaway files.
    Returns False on any :exc:`OSError`.

    Args:
        path: Absolute path to the ``.jsonl`` file to inspect.
        window_id: tmux window ID string, e.g. ``"@19"``.
        window_name: tmux window name, e.g. ``"james"``.

    Returns:
        ``True`` if the marker is present, ``False`` otherwise.
    """
    try:
        import re as _re
        stem = path.stem
        marker = (
            f"tmux key=ccgram:{window_id}, window_name={window_name}, session_id={stem}"
        ).encode()
        with open(path, "rb") as fh:
            content = fh.read(_MAX_SCAN_BYTES)
        # Require >=2 occurrences: real hook entries fire multiple times
        # (SessionStart + Stop + Notification, etc.) so the marker appears
        # repeatedly. A single occurrence is likely incidental — e.g. a
        # tool output or prompt that contains the literal string (bit us
        # Apr 14 when @19 got wired to @4 via one stray match).
        if content.count(marker) >= 2:
            return True
        # Tmux window IDs (@N) change when the tmux server restarts, but
        # window_name and session_id (stem) are stable across restarts.
        # Accept markers written under any window_id as long as window_name
        # AND session_id match — this prevents legitimate continuations from
        # being flagged as bleed bugs after a tmux restart.
        name_pattern = (
            rb'tmux key=ccgram:@\w+, window_name='
            + _re.escape(window_name.encode())
            + rb', session_id='
            + _re.escape(stem.encode())
        )
        return len(_re.findall(name_pattern, content)) >= 2
    except OSError:
        return False
