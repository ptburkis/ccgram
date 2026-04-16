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
import subprocess
import structlog
from pathlib import Path

logger = structlog.get_logger()

# Maximum bytes to read for hook marker detection (64 KB — marker is always near top).
_MARKER_READ_CAP = 64 * 1024

# Brief delay after IN_CREATE before reading — gives Claude time to write the first line.
_CREATE_SETTLE_SECS = 0.3


# ---- Session identity resolution (Chunk F) -----------------------------------


class DuplicateSessionIdError(Exception):
    """Raised when two tmux windows claim the same CCGRAM_SESSION_ID."""


def _get_pane_pid(window_id: str) -> int | None:
    """Return the foreground pane PID for window_id via tmux display-message."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", window_id, "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        stripped = result.stdout.strip()
        if not stripped:
            return None
        return int(stripped)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        ValueError,
    ):
        return None


def _read_proc_environ(pid: int) -> bytes | None:
    """Read /proc/<pid>/environ as bytes, or None on any error."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            return fh.read()
    except FileNotFoundError, PermissionError, ProcessLookupError, OSError:
        return None


def _get_child_pids(pid: int) -> list[int]:
    """Return immediate child PIDs of pid by scanning /proc/*/status."""
    children: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                content = (entry / "status").read_text()
            except FileNotFoundError, PermissionError, OSError:
                continue
            for line in content.splitlines():
                if line.startswith("PPid:"):
                    ppid_str = line.split(":", 1)[1].strip()
                    try:
                        if int(ppid_str) == pid:
                            children.append(int(entry.name))
                    except ValueError:
                        pass
                    break
    except OSError:
        pass
    return children


def _extract_session_id_from_environ(environ_bytes: bytes) -> str | None:
    """Extract CCGRAM_SESSION_ID value from null-delimited environ bytes."""
    prefix = b"CCGRAM_SESSION_ID="
    for entry in environ_bytes.split(b"\x00"):
        if entry.startswith(prefix):
            value = entry[len(prefix) :].decode("utf-8", errors="replace")
            return value if value else None
    return None


def read_session_id_from_marker_file(window_id: str) -> str | None:
    """Read session_id from ~/.ccgram/debug/terminal-<window_id>.sid.

    Returns None if the file is missing, unreadable, or empty.
    """
    path = Path.home() / ".ccgram" / "debug" / f"terminal-{window_id}.sid"
    try:
        text = path.read_text().strip()
        return text if text else None
    except OSError:
        return None


def read_session_id_from_pane_env(window_id: str) -> str | None:
    """Look up CCGRAM_SESSION_ID in the tmux pane's process tree.

    Resolves the pane's pid via tmux display-message -p -t <wid> '#{pane_pid}',
    then BFS-walks up to 4 levels of child processes reading /proc/<pid>/environ
    for CCGRAM_SESSION_ID=.  Uses _get_child_pids for child discovery.

    Returns the session_id or None if not found (pid gone, env var absent,
    tmux call failed, etc.).
    """
    pane_pid = _get_pane_pid(window_id)
    if pane_pid is None:
        return None

    frontier = [pane_pid]
    for _ in range(5):  # depths 0..4 inclusive from pane_pid
        if not frontier:
            break
        next_frontier: list[int] = []
        for pid in frontier:
            env_bytes = _read_proc_environ(pid)
            if env_bytes is not None:
                sid = _extract_session_id_from_environ(env_bytes)
                if sid:
                    return sid
            next_frontier.extend(_get_child_pids(pid))
        frontier = next_frontier
    return None


def resolve_session_identity(window_id: str) -> str | None:
    """Marker file first; env-var fallback second.  None if both miss."""
    sid = read_session_id_from_marker_file(window_id)
    if sid is not None:
        return sid
    return read_session_id_from_pane_env(window_id)


def scan_all_pane_identities(window_ids: list[str]) -> dict[str, str]:
    """Resolve every window's session_id.  Omit windows with no identity.

    Raises DuplicateSessionIdError if two distinct windows resolve to the same
    session_id — this is the loud-failure signal for the reconcile path.
    """
    if not window_ids:
        return {}

    result: dict[str, str] = {}
    reverse: dict[str, list[str]] = {}

    for window_id in window_ids:
        sid = resolve_session_identity(window_id)
        if sid is None:
            continue
        result[window_id] = sid
        if sid not in reverse:
            reverse[sid] = []
        reverse[sid].append(window_id)

    for sid, windows in reverse.items():
        if len(windows) > 1:
            raise DuplicateSessionIdError(
                f"session_id {sid!r} claimed by multiple windows: {sorted(windows)}"
            )

    return result


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
    """Find which window should own a new jsonl, using layered resolution.

    Resolution order:
    1. CCGRAM_SESSION_ID env var in pane's process tree — unambiguous.
    2. cwd-based lookup in session_map: derive slug from jsonl parent dir,
       find all entries whose cwd slug matches.  If exactly one → use it.
       If multiple → warn + pick the one with the most-recently-rotated sid.
    3. Legacy hook-marker scan (requires >=2 occurrences in file content).

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

    new_sid = jsonl_path.stem
    project_slug = jsonl_path.parent.name  # e.g. "-home-peter-projects-james"

    # Build candidate list: all claude entries whose cwd maps to this slug.
    claude_entries: list[tuple[str, str, str | None]] = []
    for key, entry in session_map.items():
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider_name", "") or entry.get("provider", "")
        if provider and provider != "claude":
            continue
        parts = key.split(":")
        if len(parts) < 2:
            continue
        window_id = parts[-1]
        window_name = entry.get("window_name", "") or ""
        cwd = entry.get("cwd", "") or ""
        if cwd:
            entry_slug = "-" + cwd.lstrip("/").replace("/", "-").replace("_", "-")
            if entry_slug == project_slug:
                current_sid = entry.get("session_id") or None
                claude_entries.append((window_id, window_name, current_sid))

    # ── Step 1: env-marker resolution (unambiguous) ─────────────────────────
    if claude_entries:
        for window_id, window_name, current_sid in claude_entries:
            env_sid = resolve_session_identity(window_id)
            if env_sid == new_sid:
                logger.info(
                    "session watcher [env-marker]: matched %s -> %s (%s)",
                    new_sid, window_id, window_name,
                )
                return window_id, window_name, current_sid

    # ── Step 2: cwd-based resolution ────────────────────────────────────────
    if len(claude_entries) == 1:
        window_id, window_name, current_sid = claude_entries[0]
        logger.info(
            "session watcher [cwd-single]: matched %s -> %s (%s)",
            new_sid, window_id, window_name,
        )
        return window_id, window_name, current_sid

    if len(claude_entries) > 1:
        logger.warning(
            "session watcher [cwd-ambiguous]: %d windows share cwd slug %s for %s "
            "— picking most-recent; consider using CCGRAM_SESSION_ID",
            len(claude_entries), project_slug, new_sid,
        )
        best = max(claude_entries, key=lambda t: (t[2] or ""))
        window_id, window_name, current_sid = best
        return window_id, window_name, current_sid

    # ── Step 3: legacy hook-marker scan ─────────────────────────────────────
    for key, entry in session_map.items():
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider_name", "") or entry.get("provider", "")
        if provider and provider != "claude":
            continue
        parts = key.split(":")
        if len(parts) < 2:
            continue
        window_id = parts[-1]
        window_name = entry.get("window_name", "") or ""
        marker = (
            f"tmux key=ccgram:{window_id}, window_name={window_name}, session_id={new_sid}"
        ).encode()
        if content.count(marker) >= 2:
            current_sid = entry.get("session_id") or None
            logger.warning(
                "session_fallback_legacy_window",
                window_id=window_id,
                cwd=entry.get("cwd", ""),
                reason="no env/cwd match — using legacy hook-marker scan",
            )
            return window_id, window_name, current_sid

    return None
