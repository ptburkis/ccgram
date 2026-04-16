"""Periodic scanner that writes PTY markers for Codex processes and sweeps stale markers.

Runs as an asyncio background task started from bot.py post_init.
Every 5 seconds:
  1. Sweeps stale markers (dead pids / mismatched ptys).
  2. Scans /proc for codex rust processes and writes markers for each.
"""

from __future__ import annotations

import asyncio
import os
import structlog
from pathlib import Path

logger = structlog.get_logger()

_SCAN_INTERVAL = 5.0


def _find_codex_processes() -> list[tuple[int, str, str]]:
    """Walk /proc looking for processes whose comm is 'codex'.

    Returns list of (pid, pty, jsonl_path) tuples.  Skips any process where
    any step fails (missing fd, non-pty stdin, no open jsonl, etc.).
    """
    results: list[tuple[int, str, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return results

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm != "codex":
            continue

        try:
            pty = os.readlink(f"/proc/{pid}/fd/0")
        except OSError:
            continue
        if not pty.startswith("/dev/pts/"):
            continue

        jsonl_path = ""
        try:
            fd_dir = entry / "fd"
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(str(fd))
                except OSError:
                    continue
                if target.endswith(".jsonl"):
                    jsonl_path = target
                    break
        except OSError:
            continue

        if jsonl_path:
            results.append((pid, pty, jsonl_path))

    return results


def _extract_codex_session_id(jsonl_path: str) -> str:
    """Extract session_id from a Codex jsonl filename.

    Codex convention: rollout-<timestamp>-<uuid>.jsonl — use the trailing UUID.
    Falls back to the full stem if no UUID found.
    """
    import re
    stem = Path(jsonl_path).stem
    uuid_pattern = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I
    )
    matches = uuid_pattern.findall(stem)
    if matches:
        return matches[-1]
    return stem


def _pty_to_window(pty: str) -> tuple[str, str]:
    """Resolve pty to (window_id, window_name) via tmux list-panes.

    Returns ('', '') on any failure.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_tty}\t#{window_id}\t#{window_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return "", ""
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0] == pty:
                return parts[1], parts[2]
    except Exception:
        pass
    return "", ""


async def _scan_and_update() -> None:
    """One scan cycle: sweep stale markers + write Codex markers."""
    from . import pty_markers

    try:
        deleted = await asyncio.to_thread(pty_markers.sweep_stale_markers)
        if deleted:
            logger.debug("session_marker_scanner: swept %d stale marker(s)", deleted)
    except Exception:
        logger.debug("session_marker_scanner: sweep failed", exc_info=True)

    try:
        processes = await asyncio.to_thread(_find_codex_processes)
    except Exception:
        logger.debug("session_marker_scanner: codex scan failed", exc_info=True)
        return

    for pid, pty, jsonl_path in processes:
        try:
            session_id = _extract_codex_session_id(jsonl_path)
            window_id, window_name = await asyncio.to_thread(_pty_to_window, pty)
            pty_markers.write_marker(
                pty=pty,
                window_id=window_id,
                window_name=window_name,
                pid=pid,
                provider="codex",
                session_id=session_id,
                transcript_path=jsonl_path,
                cwd="",
            )
        except Exception:
            logger.debug(
                "session_marker_scanner: failed to write marker for pid %d", pid, exc_info=True
            )


async def _scanner_loop() -> None:
    """Top-level scanner loop — catches all exceptions to protect the main loop."""
    try:
        while True:
            await _scan_and_update()
            await asyncio.sleep(_SCAN_INTERVAL)
    except asyncio.CancelledError:
        logger.debug("session_marker_scanner: cancelled, exiting cleanly")
    except Exception:
        logger.exception("session_marker_scanner: unhandled exception — exiting")


def _task_done_cb(task: asyncio.Task) -> None:
    """Log unexpected task exit."""
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.error("session_marker_scanner: task exited with exception", exc_info=exc)
    elif task.cancelled():
        logger.debug("session_marker_scanner: task cancelled")
    else:
        logger.warning("session_marker_scanner: task exited cleanly (unexpected)")


async def start_session_marker_scanner() -> asyncio.Task:
    """Start the marker scanner as a background asyncio task.

    Returns the Task.  Caller should not await it.
    """
    task = asyncio.create_task(_scanner_loop(), name="session-marker-scanner")
    task.add_done_callback(_task_done_cb)
    logger.info("session_marker_scanner: started")
    return task
