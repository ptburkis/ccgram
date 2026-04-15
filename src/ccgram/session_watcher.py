"""Inotify-based session rotation watcher.

Watches ~/.claude/projects/ for new jsonl files created by Claude Code on
SessionStart (session rotation via /compact, restart, etc.).  When a new file
appears whose hook marker matches a bound window, session_map and monitor_state
are updated automatically — no polling, no lag.

Design contract:
- Runs as an isolated asyncio background task (never kills the main loop).
- All exceptions caught at task root — failures degrade gracefully.
- Feature-flagged via CCGRAM_INOTIFY_ENABLED (default "1"; set "0" to disable).
- Independent fallbacks (autoheal on inbound + doctor cron) continue working.
- Uses inotify-simple (Linux-native, minimal dep).
- Codex jsonls: logged only — PTY-based discovery handles those.
- File size cap: never reads more than 64 KB from a jsonl for marker detection.
- Skips non-.jsonl files and dirs under .ccgram/debug/.
"""

from __future__ import annotations

import asyncio
import os
import structlog
from pathlib import Path

logger = structlog.get_logger()

# Maximum bytes to read for hook marker detection (64 KB — marker is always near top).
_MARKER_READ_CAP = 64 * 1024

# Brief delay after IN_CREATE before reading — gives Claude time to write the first line.
_CREATE_SETTLE_SECS = 0.3


def _is_enabled() -> bool:
    return os.environ.get("CCGRAM_INOTIFY_ENABLED", "1") != "0"


def _inotify_available() -> bool:
    try:
        import inotify_simple  # noqa: F401

        return True
    except ImportError:
        return False


async def start_session_watcher() -> asyncio.Task | None:
    """Start the inotify watcher as a background task.

    Returns the Task, or None if disabled/unavailable (inotify-simple not installed
    or CCGRAM_INOTIFY_ENABLED=0).  Caller should not await the task.
    """
    if not _is_enabled():
        logger.info("session watcher: disabled via CCGRAM_INOTIFY_ENABLED=0")
        return None
    if not _inotify_available():
        logger.warning(
            "session watcher: inotify-simple not installed — falling back to "
            "autoheal-on-inbound + doctor cron. Install with: uv add inotify-simple"
        )
        return None
    task = asyncio.create_task(_watcher_loop(), name="session-watcher")
    task.add_done_callback(_task_done_cb)
    logger.info("session watcher: started (inotify)")
    return task


def _task_done_cb(task: asyncio.Task) -> None:
    """Log unexpected task exit (should never happen under normal operation)."""
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.error("session watcher: task exited with exception", exc_info=exc)
    elif task.cancelled():
        logger.debug("session watcher: task cancelled")
    else:
        logger.warning("session watcher: task exited cleanly (unexpected)")


async def _watcher_loop() -> None:
    """Top-level watcher loop — catches all exceptions to protect the main loop."""
    try:
        await _run_watcher()
    except asyncio.CancelledError:
        logger.debug("session watcher: cancelled, exiting cleanly")
    except Exception:
        logger.exception(
            "session watcher: unhandled exception in watcher loop — exiting"
        )


async def _run_watcher() -> None:
    """Core inotify loop: watch Claude projects dir, react to new jsonl files."""
    import inotify_simple

    from .config import config

    claude_projects = config.claude_projects_path
    if not claude_projects.is_dir():
        logger.warning(
            "session watcher: Claude projects dir %s does not exist — watcher inactive",
            claude_projects,
        )
        return

    inotify = inotify_simple.INotify()
    IN_CREATE = inotify_simple.flags.CREATE
    IN_CLOSE_WRITE = inotify_simple.flags.CLOSE_WRITE
    IN_ISDIR = inotify_simple.flags.ISDIR
    flags = IN_CREATE | IN_CLOSE_WRITE

    # wd_to_path: inotify watch descriptor -> Path
    wd_to_path: dict[int, Path] = {}

    def add_watch(directory: Path) -> None:
        """Add an inotify watch for directory, ignoring ENOSPC (max_user_watches)."""
        try:
            wd = inotify.add_watch(str(directory), flags)
            wd_to_path[wd] = directory
            logger.debug("session watcher: watching %s (wd=%d)", directory, wd)
        except OSError as exc:
            logger.warning(
                "session watcher: could not add watch for %s: %s — continuing with partial coverage",
                directory,
                exc,
            )

    # Walk existing dirs under claude_projects and add watches.
    add_watch(claude_projects)
    for child in claude_projects.iterdir():
        if child.is_dir():
            add_watch(child)

    logger.info(
        "session watcher: inotify watches established under %s", claude_projects
    )

    loop = asyncio.get_running_loop()

    while True:
        # inotify_simple.read() blocks — run in thread to stay non-blocking.
        events = await loop.run_in_executor(None, inotify.read, 1000)  # 1s timeout
        for event in events:
            try:
                await _handle_inotify_event(
                    event, wd_to_path, IN_CREATE, IN_ISDIR, add_watch
                )
            except Exception:
                logger.exception("session watcher: error handling event %s", event)


async def _handle_inotify_event(
    event: object,
    wd_to_path: dict[int, Path],
    IN_CREATE: int,
    IN_ISDIR: int,
    add_watch_fn,
) -> None:
    """Process a single inotify event."""
    import inotify_simple

    wd = event.wd  # type: ignore[attr-defined]
    mask = event.mask  # type: ignore[attr-defined]
    name = event.name  # type: ignore[attr-defined]

    parent_dir = wd_to_path.get(wd)
    if parent_dir is None:
        return

    # If a new sub-directory was created, start watching it.
    if mask & IN_CREATE and mask & IN_ISDIR and name:
        new_dir = parent_dir / name
        add_watch_fn(new_dir)
        return

    # Only care about .jsonl files.
    if not name or not name.endswith(".jsonl"):
        return

    # Skip temp/editor swap files.
    if name.endswith((".tmp", ".swp")):
        return

    jsonl_path = parent_dir / name

    # Wait briefly for Claude to write the first line (hook marker).
    await asyncio.sleep(_CREATE_SETTLE_SECS)

    await _process_new_jsonl(jsonl_path)


async def _process_new_jsonl(jsonl_path: Path) -> None:
    """Try to attribute a new jsonl to a window and apply a session update."""
    if not jsonl_path.exists():
        return

    # Read a bounded chunk for marker scanning.
    try:
        raw = await asyncio.to_thread(_read_limited, jsonl_path)
    except OSError:
        return

    # Find which window this jsonl belongs to.
    result = await asyncio.to_thread(_find_window_for_jsonl, jsonl_path, raw)
    if result is None:
        logger.debug("session watcher: no window match for %s", jsonl_path.name)
        return

    window_id, window_name, old_sid = result
    new_sid = jsonl_path.stem

    if new_sid == old_sid:
        logger.debug(
            "session watcher: %s already tracked for @%s — skipping",
            new_sid,
            window_id,
        )
        return

    logger.info(
        "session watcher: detected rotation for %s (%s) — %s -> %s",
        window_id,
        window_name,
        old_sid or "(none)",
        new_sid,
    )

    from .session_autoheal import apply_session_update

    await apply_session_update(
        window_id=window_id,
        old_sid=old_sid or "",
        new_sid=new_sid,
        new_transcript=str(jsonl_path),
        source="inotify",
    )


def _read_limited(path: Path) -> bytes:
    """Read at most _MARKER_READ_CAP bytes from path synchronously."""
    with open(path, "rb") as fh:
        return fh.read(_MARKER_READ_CAP)


def _find_window_for_jsonl(
    jsonl_path: Path, content: bytes
) -> tuple[str, str, str | None] | None:
    """Scan session_map for a window whose hook marker is present in content.

    Returns (window_id, window_name, current_session_id) if found, else None.
    Runs synchronously — call via asyncio.to_thread.
    """
    import json
    from .config import config

    map_file = config.session_map_file
    if not map_file.exists():
        return None

    try:
        session_map: dict = json.loads(map_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    stem = jsonl_path.stem

    for key, entry in session_map.items():
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider", "")
        if provider and provider != "claude":
            continue

        # key format: "tmux_session:@N"
        parts = key.split(":")
        if len(parts) < 2:
            continue
        window_id = parts[-1]  # "@N"
        window_name = entry.get("window_name", "") or ""

        marker = (
            f"tmux key=ccgram:{window_id}, window_name={window_name}, session_id={stem}"
        ).encode()
        # Require >=2 occurrences (real hooks fire multiple times);
        # single-occurrence is incidental (tool output / prompt literal).
        if content.count(marker) >= 2:
            current_sid = entry.get("session_id") or None
            return window_id, window_name, current_sid

    return None
