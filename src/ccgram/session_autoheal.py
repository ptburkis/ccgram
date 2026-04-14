"""Inline auto-heal for stale session_map entries on the inbound message path.

When Claude Code rotates a session (via /compact, restart, etc.) it writes a new
JSONL file and (via the SessionStart hook) should update session_map.json.  If the
hook fires but the file-level write is lost, or if the hook itself fails, the
session_map keeps pointing at the old (now-dead) JSONL.

This module provides ``maybe_refresh_session_map(window_id)``, called on every
inbound Telegram message just before the text is injected into tmux.  If the
currently-tracked transcript no longer exists on disk AND a newer JSONL is
available in the same Claude project directory, the session_map and
monitor_state are updated atomically, in-place, without requiring a restart.

Design constraints:
  - NEVER raise — catch everything, log at debug on unexpected errors.
  - Filesystem scan runs in asyncio.to_thread to avoid blocking the event loop.
  - File writes are atomic (tmp + rename via atomic_write_json).
  - Only acts on Claude windows; hookless providers discover sessions themselves.
  - Does NOT conflict with the existing reconciler in session_monitor.py — that
    reconciler handles windows with a MISSING session_id; this one handles windows
    with a STALE (no-longer-valid) session_id.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import structlog
from pathlib import Path
from typing import Any

from .config import config
from .utils import atomic_write_json

logger = structlog.get_logger()


def _cwd_to_project_slug(cwd: str) -> str:
    """Convert a cwd path to the Claude project directory slug.

    Claude uses the path with leading '/' stripped and all '/' and '_'
    replaced by '-'.  For example:
        /home/peter/projects/james  ->  -home-peter-projects-james
        /home/peter/projects/bulugo_lead_gen  ->  -home-peter-projects-bulugo-lead-gen

    The leading '-' comes from replacing the leading '/' with '-'.
    """
    return "-" + cwd.lstrip("/").replace("/", "-").replace("_", "-")


def _find_newer_jsonl_sync(
    project_dir: Path,
    current_transcript: str,
    current_sid: str,
) -> tuple[str, str] | None:
    """Scan project_dir for a JSONL newer than the currently-tracked one.

    Returns (new_session_id, new_transcript_path) if a newer candidate is
    found, or None if nothing beats the current entry.

    This runs synchronously — call it via asyncio.to_thread.
    """
    try:
        jsonl_files = list(project_dir.glob("*.jsonl"))
    except OSError:
        return None

    if not jsonl_files:
        return None

    # Get mtime of the current transcript (if it exists) for comparison.
    current_mtime: float = 0.0
    if current_transcript:
        try:
            current_mtime = Path(current_transcript).stat().st_mtime
        except OSError:
            # File doesn't exist — any existing JSONL is a candidate.
            current_mtime = 0.0

    best_path: Path | None = None
    best_mtime: float = current_mtime  # must beat this to qualify

    for jf in jsonl_files:
        sid = jf.stem
        # Skip the currently-tracked session — we already know about it.
        if sid == current_sid:
            continue
        try:
            mtime = jf.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best_path = jf

    if best_path is None:
        return None

    return best_path.stem, str(best_path)


def _update_session_map_sync(
    window_id: str,
    old_sid: str,
    new_sid: str,
    new_transcript: str,
) -> bool:
    """Atomically update session_map.json with the new session_id and transcript_path.

    Returns True on success.
    Acquires the same .lock file used by hook.py to prevent concurrent writers.
    """
    map_file = config.session_map_file
    lock_path = map_file.with_suffix(".lock")
    window_key = f"{config.tmux_session_name}:{window_id}"

    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                session_map: dict[str, Any] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass

                entry = session_map.get(window_key)
                if not isinstance(entry, dict):
                    # Entry vanished between our check and the lock — bail out.
                    logger.debug(
                        "auto-heal: session_map entry for %s disappeared before write",
                        window_key,
                    )
                    return False

                # Guard: if another writer already updated us, don't regress.
                current_sid_in_map = entry.get("session_id", "")
                if current_sid_in_map and current_sid_in_map != old_sid:
                    logger.debug(
                        "auto-heal: session_map entry for %s was already updated "
                        "(expected %s, found %s) — skipping",
                        window_key,
                        old_sid,
                        current_sid_in_map,
                    )
                    return False

                entry["session_id"] = new_sid
                entry["transcript_path"] = new_transcript
                session_map[window_key] = entry
                atomic_write_json(map_file, session_map)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError:
        logger.debug(
            "auto-heal: failed to acquire lock or write session_map for %s",
            window_id,
            exc_info=True,
        )
        return False

    return True


def _update_monitor_state_sync(
    old_sid: str,
    new_sid: str,
    new_transcript: str,
) -> None:
    """Update monitor_state.json: remove old tracked session, add new one at EOF.

    Setting offset to the file size causes the monitor to skip the transcript
    backlog and only forward new messages from this point forward — the same
    behaviour as when the hook fires fresh on a SessionStart.
    """
    state_file = config.monitor_state_file
    try:
        data: dict[str, Any] = {}
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        tracked = data.get("tracked_sessions", {})

        # Remove old session.
        tracked.pop(old_sid, None)

        # Add new session at EOF so we don't replay the backlog.
        try:
            file_size = Path(new_transcript).stat().st_size
        except OSError:
            file_size = 0

        tracked[new_sid] = {
            "session_id": new_sid,
            "file_path": new_transcript,
            "last_byte_offset": file_size,
        }
        data["tracked_sessions"] = tracked
        atomic_write_json(state_file, data)
    except OSError:
        logger.debug(
            "auto-heal: failed to update monitor_state for %s -> %s",
            old_sid,
            new_sid,
            exc_info=True,
        )


async def maybe_refresh_session_map(window_id: str) -> bool:
    """Check if session_map entry for window_id is stale; refresh if so.

    Called on the inbound message path just before send_to_window(), so any
    session rotation is healed inline without manual intervention.

    Returns True if a refresh was applied, False if no action was needed.

    Claude windows only — hookless providers (Codex, Gemini, shell) do their
    own session discovery and don't use session_map in the same way.

    Silent on error: never raises; always returns False on unexpected failure.
    """
    try:
        return await _maybe_refresh_session_map_inner(window_id)
    except Exception:
        logger.debug(
            "auto-heal: unexpected error for %s", window_id, exc_info=True
        )
        return False


async def _maybe_refresh_session_map_inner(window_id: str) -> bool:
    """Inner implementation — may raise; caller wraps in try/except."""
    from .window_state_store import window_store

    state = window_store.get_window_state(window_id)

    # Only act on Claude windows — hookless providers manage themselves.
    if state.provider_name and state.provider_name != "claude":
        return False

    cwd = state.cwd
    if not cwd:
        return False

    current_sid = state.session_id
    current_transcript = state.transcript_path

    # Fast-path: transcript exists → nothing to heal.
    if current_transcript and Path(current_transcript).exists():
        return False

    # Compute the Claude project directory for this cwd.
    slug = _cwd_to_project_slug(cwd)
    project_dir = config.claude_projects_path / slug

    if not project_dir.is_dir():
        logger.debug(
            "auto-heal: project dir %s does not exist for window %s",
            project_dir,
            window_id,
        )
        return False

    # Scan for a newer JSONL — do it in a thread to avoid blocking the loop.
    result = await asyncio.to_thread(
        _find_newer_jsonl_sync,
        project_dir,
        current_transcript,
        current_sid,
    )

    if result is None:
        logger.debug(
            "auto-heal: no newer JSONL found for window %s in %s",
            window_id,
            project_dir,
        )
        return False

    new_sid, new_transcript = result

    # Update session_map.json on disk (atomic, locked).
    ok = await asyncio.to_thread(
        _update_session_map_sync,
        window_id,
        current_sid,
        new_sid,
        new_transcript,
    )
    if not ok:
        return False

    # Update monitor_state.json so the session_monitor picks up the new file
    # at EOF (skip backlog) rather than replaying the whole transcript.
    await asyncio.to_thread(
        _update_monitor_state_sync,
        current_sid,
        new_sid,
        new_transcript,
    )

    # Refresh in-memory session_manager / window_store state so that
    # subsequent calls in this request see the correct session_id and path.
    from .session import session_manager

    try:
        await session_manager.load_session_map()
    except Exception:
        logger.debug(
            "auto-heal: load_session_map failed after heal for %s", window_id, exc_info=True
        )

    logger.info(
        "auto-healed session_map for %s: %s -> %s",
        window_id,
        current_sid or "(none)",
        new_sid,
    )
    return True
