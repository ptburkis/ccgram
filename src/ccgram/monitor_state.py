"""Monitor state persistence — tracks byte offsets for each session.

Persists TrackedSession records (session_id, file_path, last_byte_offset)
to ~/.ccgram/monitor_state.json so the session monitor can resume
incremental reading after restarts without re-sending old messages.

Key classes: MonitorState, TrackedSession.
"""

import json
import sqlite3
import structlog
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_PERIODIC_SAVE_INTERVAL = 30  # seconds

logger = structlog.get_logger()


@dataclass
class TrackedSession:
    """State for a tracked Claude Code session."""

    session_id: str
    file_path: str  # Path to .jsonl file
    last_byte_offset: int = 0  # Byte offset for incremental reading

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackedSession":
        """Create from dict."""
        return cls(
            session_id=data.get("session_id", ""),
            file_path=data.get("file_path", ""),
            last_byte_offset=data.get("last_byte_offset", 0),
        )


@dataclass
class MonitorState:
    """Persistent state for the session monitor.

    Stores tracking information for all monitored sessions
    and the events.jsonl byte offset to prevent replaying
    historical hook events after restarts.
    """

    state_file: Path
    tracked_sessions: dict[str, TrackedSession] = field(default_factory=dict)
    events_offset: int = 0
    _dirty: bool = field(default=False, repr=False)

    def load(self) -> None:
        """Load state from file.

        # DB-first since Chunk H follow-up; legacy JSON path retained for fallback
        # until write-retirement (docs/plans/state-unification-runbook.md).
        """
        # Attempt DB read first
        from . import store

        try:
            with store.connect() as conn:
                prefs = store.list_prefs(conn, "monitor")
        except (sqlite3.DatabaseError, FileNotFoundError, ModuleNotFoundError):
            prefs = []
        if prefs:
            # Rehydrate tracked_sessions from (scope_id=session_id, key, value)
            tmp: dict[str, dict] = {}
            for session_id, key, value in prefs:
                tmp.setdefault(session_id, {})[key] = value
            self.tracked_sessions = {
                sid: TrackedSession(
                    session_id=sid,
                    file_path=d.get("file_path", ""),
                    # Coerce to int: values come as JSON-decoded numbers but
                    # may arrive as float or str if stored by older code.
                    last_byte_offset=int(d.get("last_byte_offset", 0)),
                )
                for sid, d in tmp.items()
                if d.get("file_path")
            }
            logger.info(
                "Loaded %d tracked sessions from DB",
                len(self.tracked_sessions),
            )
            return
        logger.warning(
            "falling back to legacy monitor_state.json — DB is empty or unavailable"
        )
        if not self.state_file.exists():
            logger.debug("State file does not exist: %s", self.state_file)
            return

        try:
            data = json.loads(self.state_file.read_text())
            sessions = data.get("tracked_sessions", {})
            self.tracked_sessions = {
                k: TrackedSession.from_dict(v) for k, v in sessions.items()
            }
            self.events_offset = data.get("events_offset", 0)
            logger.info(
                "Loaded %d tracked sessions from state", len(self.tracked_sessions)
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to load state file: %s", e)
            self.tracked_sessions = {}

    def save(self) -> None:
        """Save state to file atomically."""
        from .utils import atomic_write_json

        data = {
            "tracked_sessions": {
                k: v.to_dict() for k, v in self.tracked_sessions.items()
            },
            "events_offset": self.events_offset,
        }

        try:
            atomic_write_json(self.state_file, data)
            self._dirty = False
        except OSError:
            logger.exception("Failed to save state file")

        # Also persist to DB so restarts can reload without relying on JSON.
        from . import store

        try:
            with store.connect() as conn:
                for session in self.tracked_sessions.values():
                    store.set_pref(
                        conn,
                        "monitor",
                        "last_byte_offset",
                        session.last_byte_offset,
                        scope_id=session.session_id,
                    )
                    store.set_pref(
                        conn,
                        "monitor",
                        "file_path",
                        session.file_path,
                        scope_id=session.session_id,
                    )
            logger.debug(
                "Saved %d tracked session offsets to DB",
                len(self.tracked_sessions),
            )
        except (sqlite3.DatabaseError, FileNotFoundError, ModuleNotFoundError) as exc:
            logger.warning("Failed to save monitor state to DB: %s", exc)

    def get_session(self, session_id: str) -> TrackedSession | None:
        """Get tracked session by ID."""
        return self.tracked_sessions.get(session_id)

    def update_session(self, session: TrackedSession) -> None:
        """Update or add a tracked session."""
        self.tracked_sessions[session.session_id] = session
        self._dirty = True

    def remove_session(self, session_id: str) -> None:
        """Remove a tracked session."""
        if session_id in self.tracked_sessions:
            del self.tracked_sessions[session_id]
            self._dirty = True

    def save_if_dirty(self) -> None:
        """Save state only if it has been modified."""
        if self._dirty:
            self.save()
