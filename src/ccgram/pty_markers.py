"""PTY-keyed session marker helpers.

Each live agent process writes a small JSON file to ~/.ccgram/active-sessions/<pts-basename>.json.
These files are the ground truth for which JSONL file each agent is writing to.

The PTY is stable for the lifetime of the agent process — it is process-attached,
not a heuristic, so marker files beat session_map.json when the two diverge.
"""

from __future__ import annotations

import json
import os
import time
import tempfile
import structlog
from pathlib import Path

logger = structlog.get_logger()


def _markers_dir() -> Path:
    """Return ~/.ccgram/active-sessions/, creating if needed."""
    from .utils import ccgram_dir
    d = ccgram_dir() / "active-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pty_to_filename(pty: str) -> str:
    """Convert '/dev/pts/19' -> 'pts19.json'."""
    return pty.replace("/dev/pts/", "pts") + ".json"


def write_marker(
    pty: str,
    window_id: str,
    window_name: str,
    pid: int,
    provider: str,
    session_id: str,
    transcript_path: str,
    cwd: str,
) -> None:
    """Atomically write a PTY marker file for a live agent process.

    Safe to call frequently (every hook event). Uses tmp+rename for atomicity.
    Never raises — all errors are logged at debug level.
    """
    try:
        if not pty.startswith("/dev/pts/"):
            return
        markers_dir = _markers_dir()
        marker = {
            "pty": pty,
            "window_id": window_id,
            "window_name": window_name,
            "pid": pid,
            "provider": provider,
            "session_id": session_id,
            "transcript_path": transcript_path,
            "cwd": cwd,
            "last_seen_at": time.time(),
        }
        dest = markers_dir / _pty_to_filename(pty)
        # Atomic write: write to tmp then rename
        tmp_fd, tmp_path = tempfile.mkstemp(dir=markers_dir, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(marker, fh, separators=(",", ":"))
            os.replace(tmp_path, dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        logger.debug("pty_markers.write_marker failed for %s", pty, exc_info=True)


def read_marker_for_pty(pty: str) -> dict | None:
    """Return the marker dict for the given PTY (e.g. '/dev/pts/19'), or None."""
    try:
        markers_dir = _markers_dir()
        path = markers_dir / _pty_to_filename(pty)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        logger.debug("pty_markers.read_marker_for_pty failed for %s", pty, exc_info=True)
        return None


def read_marker_for_window(window_id: str) -> dict | None:
    """Scan all markers and return the first one matching window_id, or None."""
    try:
        markers_dir = _markers_dir()
        for p in markers_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text())
                if d.get("window_id") == window_id:
                    return d
            except Exception:
                continue
    except Exception:
        logger.debug("pty_markers.read_marker_for_window failed for %s", window_id, exc_info=True)
    return None


def read_marker_for_session(session_id: str) -> dict | None:
    """Scan all markers and return the first one matching session_id, or None."""
    try:
        markers_dir = _markers_dir()
        for p in markers_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text())
                if d.get("session_id") == session_id:
                    return d
            except Exception:
                continue
    except Exception:
        logger.debug("pty_markers.read_marker_for_session failed for %s", session_id, exc_info=True)
    return None


def find_marker_by_session_id(session_id: str) -> dict | None:
    """Alias for read_marker_for_session — returns the marker dict for session_id, or None."""
    return read_marker_for_session(session_id)


def list_active_markers() -> list[dict]:
    """Return all marker dicts (no liveness filtering)."""
    result: list[dict] = []
    try:
        markers_dir = _markers_dir()
        for p in markers_dir.glob("*.json"):
            try:
                result.append(json.loads(p.read_text()))
            except Exception:
                continue
    except Exception:
        logger.debug("pty_markers.list_active_markers failed", exc_info=True)
    return result


def sweep_stale_markers() -> int:
    """Delete markers whose PID is dead or whose /proc/<pid>/fd/0 != recorded pty.

    Returns the count of files deleted. Idempotent.
    """
    deleted = 0
    try:
        markers_dir = _markers_dir()
        for p in markers_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text())
            except Exception:
                # Unreadable marker — delete it
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
                continue

            pid = d.get("pid", 0)
            pty = d.get("pty", "")

            stale = False
            if not pid or not Path(f"/proc/{pid}").exists():
                stale = True
            else:
                # Verify PTY is still attached to this pid
                try:
                    actual_pty = os.readlink(f"/proc/{pid}/fd/0")
                    if actual_pty != pty:
                        stale = True
                except OSError:
                    stale = True

            if stale:
                try:
                    p.unlink()
                    deleted += 1
                    logger.debug("pty_markers: swept stale marker %s (pid=%s)", p.name, pid)
                except OSError:
                    pass
    except Exception:
        logger.debug("pty_markers.sweep_stale_markers failed", exc_info=True)
    return deleted
