"""Window state storage — per-window mode and session metadata.

Owns the WindowState dataclass and all window-scoped mode settings
(approval, batch, notification). Extracted from SessionManager so that
providers, handlers, and tests can import window state without pulling in
the full session management stack.

Key class: WindowStateStore (singleton instantiated as ``window_store``).
Key types: WindowState, APPROVAL_MODES, BATCH_MODES, NOTIFICATION_MODES.
"""

from __future__ import annotations

import os
import sqlite3
import structlog
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

logger = structlog.get_logger()

APPROVAL_MODES: frozenset[str] = frozenset({"normal", "yolo"})
DEFAULT_APPROVAL_MODE = os.environ.get("CCGRAM_DEFAULT_APPROVAL_MODE", "yolo")
YOLO_APPROVAL_MODE = "yolo"

BATCH_MODES: frozenset[str] = frozenset({"batched", "verbose"})
DEFAULT_BATCH_MODE = "batched"

NOTIFICATION_MODES: tuple[str, ...] = ("summary", "all")
# Legacy mode names that have been collapsed into "summary" — used for migration
# of existing window_states loaded from state.json.
_LEGACY_NOTIFICATION_MODES: frozenset[str] = frozenset({"errors_only", "muted"})


@dataclass
class WindowState:
    """Persistent state for a tmux window.

    Attributes:
        session_id: ccgram DB session_id — ROUTING identity. Matches
            sessions.session_id and topic_bindings.session_id. For Claude
            equals provider_session_id.
        provider_session_id: Provider-internal session UUID for FILE TRACKING.
            For Claude equals session_id. For hookless providers (Codex,
            Gemini) differs — this is the UUID from the provider's own
            session metadata (e.g. Codex rollout JSONL session_meta.id).
        cwd: Working directory for direct file path construction
        window_name: Display name of the window
        transcript_path: Direct path to JSONL transcript file (from hook payload)
        notification_mode: "summary" | "all"
        provider_name: Name of the agent provider for this window
        approval_mode: "normal" | "yolo"
        batch_mode: "batched" | "verbose"
        external: True for windows owned by external tools (emdash) — never killed by ccgram
    """

    session_id: str = ""
    provider_session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    transcript_path: str = ""
    notification_mode: str = "summary"
    provider_name: str = ""
    approval_mode: str = DEFAULT_APPROVAL_MODE
    batch_mode: str = DEFAULT_BATCH_MODE
    external: bool = False
    origin: str = "manual_discovered"  # manual_discovered | ccgram_created | external

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        # Only serialize provider_session_id when it differs from session_id
        # (saves space for Claude sessions where they're always equal).
        if self.provider_session_id and self.provider_session_id != self.session_id:
            d["provider_session_id"] = self.provider_session_id
        if self.window_name:
            d["window_name"] = self.window_name
        if self.transcript_path:
            d["transcript_path"] = self.transcript_path
        if self.notification_mode != "summary":
            d["notification_mode"] = self.notification_mode
        if self.provider_name:
            d["provider_name"] = self.provider_name
        if self.approval_mode != DEFAULT_APPROVAL_MODE:
            d["approval_mode"] = self.approval_mode
        if self.batch_mode != DEFAULT_BATCH_MODE:
            d["batch_mode"] = self.batch_mode
        if self.external:
            d["external"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        # Migrate legacy notification modes (errors_only, muted) → summary.
        notif = data.get("notification_mode", "summary")
        if notif in _LEGACY_NOTIFICATION_MODES:
            notif = "summary"
        session_id = data.get("session_id", "")
        # Back-compat: absent provider_session_id falls back to session_id
        # (old serialized state without provider_session_id field).
        provider_session_id = data.get("provider_session_id", "") or session_id
        return cls(
            session_id=session_id,
            provider_session_id=provider_session_id,
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            transcript_path=data.get("transcript_path", ""),
            notification_mode=notif,
            provider_name=data.get("provider_name", ""),
            approval_mode=data.get("approval_mode", DEFAULT_APPROVAL_MODE),
            batch_mode=data.get("batch_mode", DEFAULT_BATCH_MODE),
            external=data.get("external", False),
        )


def _db_path() -> Path:
    """Return the canonical path of the SQLite state DB."""
    from .utils import ccgram_dir
    return ccgram_dir() / "state.db"


@dataclass
class WindowStateStore:
    """Per-window mode and session metadata store.

    Owns the window_states dict and all methods for reading/writing
    per-window settings: notification mode, approval mode, batch mode,
    provider name, and session/cwd association.

    Persistence is delegated: the ``_schedule_save`` callback (set by
    SessionManager) triggers a debounced save after mutations.

    The ``_on_hookless_provider_switch`` callback (also set by
    SessionManager) is called when switching to a hookless provider so
    session_map.json can be cleaned up without a circular dependency.

    The ``window_states`` dict is a runtime cache. On cache miss,
    ``get_window_state`` builds the state from DB sources (PTY markers +
    window_modes table + sessions table) and populates the cache.
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._schedule_save: Callable[[], None] = lambda: None
        self._on_hookless_provider_switch: Callable[[str], None] = lambda _wid: None

    def reset(self) -> None:
        """Clear all state. Used for test isolation."""
        self.window_states.clear()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """No-op serialization — window_states are no longer written to state.json."""
        return {}

    def from_dict(self, data: dict[str, Any]) -> None:
        """Load window_states from state.json data (called at startup to seed cache)."""
        self.window_states = {
            k: WindowState.from_dict(v) for k, v in data.items() if isinstance(v, dict)
        }

    # ------------------------------------------------------------------
    # DB write-through helpers
    # ------------------------------------------------------------------

    def _upsert_window_modes_to_db(self, window_id: str, **kwargs: Any) -> None:
        """Write window mode fields through to the window_modes DB table.

        Never raises — all errors are caught and logged at debug level.
        """
        try:
            db = _db_path()
            if not db.exists():
                return
            from . import store
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            try:
                store.upsert_window_modes(conn, window_id, **kwargs)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.debug(
                "_upsert_window_modes_to_db failed for window_id %s", window_id, exc_info=True
            )

    # ------------------------------------------------------------------
    # DB-sourced state construction
    # ------------------------------------------------------------------

    def _build_window_state_from_sources(self, window_id: str) -> WindowState:
        """Build a WindowState from DB sources when the cache misses.

        Priority:
        1. PTY marker (ground truth for live sessions)
        2. sessions table fallback (for windows without an active marker)
        3. window_modes table (for mode settings — overlaid on top)
        """
        state = WindowState()

        # 1. PTY marker — highest priority for session identity fields.
        try:
            from . import pty_markers
            marker = pty_markers.read_marker_for_window(window_id)
            if marker:
                state.session_id = marker.get("session_id", "")
                state.provider_session_id = marker.get("session_id", "")
                state.cwd = marker.get("cwd", "")
                state.transcript_path = marker.get("transcript_path", "")
                state.window_name = marker.get("window_name", "")
                prov = marker.get("provider", "")
                if prov:
                    state.provider_name = prov
        except Exception:
            logger.debug(
                "_build_window_state_from_sources: PTY marker lookup failed for %s",
                window_id, exc_info=True,
            )

        # 2. sessions table fallback when no marker found session_id.
        if not state.session_id:
            try:
                db = _db_path()
                if db.exists():
                    conn = sqlite3.connect(str(db))
                    conn.row_factory = sqlite3.Row
                    try:
                        row = conn.execute(
                            "SELECT session_id, cwd FROM sessions "
                            "WHERE window_id=? AND status='active' "
                            "ORDER BY updated_at DESC LIMIT 1",
                            (window_id,),
                        ).fetchone()
                        if row:
                            state.session_id = row["session_id"]
                            state.provider_session_id = row["session_id"]
                            if not state.cwd:
                                state.cwd = row["cwd"]
                    finally:
                        conn.close()
            except Exception:
                logger.debug(
                    "_build_window_state_from_sources: sessions DB lookup failed for %s",
                    window_id, exc_info=True,
                )

        # 3. window_modes table — overlaid for mode settings.
        try:
            db = _db_path()
            if db.exists():
                conn = sqlite3.connect(str(db))
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        "SELECT approval_mode, batch_mode, notification_mode, "
                        "provider_name, external FROM window_modes WHERE window_id=?",
                        (window_id,),
                    ).fetchone()
                    if row:
                        state.approval_mode = row["approval_mode"] or DEFAULT_APPROVAL_MODE
                        state.batch_mode = row["batch_mode"] or DEFAULT_BATCH_MODE
                        notif = row["notification_mode"] or "summary"
                        if notif in _LEGACY_NOTIFICATION_MODES:
                            notif = "summary"
                        state.notification_mode = notif
                        if row["provider_name"] and not state.provider_name:
                            state.provider_name = row["provider_name"]
                        state.external = bool(row["external"])
                finally:
                    conn.close()
        except Exception:
            logger.debug(
                "_build_window_state_from_sources: window_modes DB lookup failed for %s",
                window_id, exc_info=True,
            )

        return state

    # ------------------------------------------------------------------
    # Core get/create
    # ------------------------------------------------------------------

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state, building from DB sources on cache miss."""
        if window_id not in self.window_states:
            self.window_states[window_id] = self._build_window_state_from_sources(window_id)
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        state.provider_session_id = ""
        state.notification_mode = "summary"
        self._schedule_save()
        logger.info("Cleared session for window_id %s", window_id)

    def get_session_id_for_window(self, window_id: str) -> str | None:
        """Look up session_id for a window from window_states."""
        state = self.window_states.get(window_id)
        return state.session_id if state and state.session_id else None

    # ------------------------------------------------------------------
    # Provider management
    # ------------------------------------------------------------------

    def set_window_provider(
        self,
        window_id: str,
        provider_name: str,
        *,
        cwd: str | None = None,
    ) -> None:
        """Set the provider for a window. Empty string resets to config default.

        Always saves state unconditionally. When *cwd* is provided, persists it
        in the same write so provider/cwd updates stay atomic.

        When switching to a hookless provider (e.g. shell), invokes the
        ``_on_hookless_provider_switch`` callback so the caller can clear the
        stale session_map.json entry without a circular import.
        """
        state = self.get_window_state(window_id)
        old_provider = state.provider_name
        state.provider_name = provider_name
        if cwd:
            state.cwd = cwd

        # When switching away from a hook-based provider to a hookless one,
        # clear stale session data and notify caller to update session_map.json.
        if old_provider != provider_name and provider_name:
            from .providers import registry

            new_prov = registry.get(provider_name)
            if not new_prov.capabilities.supports_hook:
                if state.session_id:
                    state.session_id = ""
                    state.provider_session_id = ""
                    state.transcript_path = ""
                self._on_hookless_provider_switch(window_id)

        self._upsert_window_modes_to_db(window_id, provider_name=provider_name)
        self._schedule_save()

    # ------------------------------------------------------------------
    # Notification mode
    # ------------------------------------------------------------------

    _NOTIFICATION_MODES = NOTIFICATION_MODES

    def get_notification_mode(self, window_id: str) -> str:
        """Get notification mode for a window (default: 'summary')."""
        state = self.window_states.get(window_id)
        return state.notification_mode if state else "summary"

    def set_notification_mode(self, window_id: str, mode: str) -> None:
        """Set notification mode for a window."""
        if mode not in self._NOTIFICATION_MODES:
            raise ValueError(f"Invalid notification mode: {mode!r}")
        state = self.get_window_state(window_id)
        if state.notification_mode != mode:
            state.notification_mode = mode
            self._upsert_window_modes_to_db(window_id, notification_mode=mode)
            self._schedule_save()

    def cycle_notification_mode(self, window_id: str) -> str:
        """Cycle notification mode: summary ↔ all. Returns new mode."""
        current = self.get_notification_mode(window_id)
        modes = self._NOTIFICATION_MODES
        idx = modes.index(current) if current in modes else 0
        new_mode = modes[(idx + 1) % len(modes)]
        self.set_notification_mode(window_id, new_mode)
        return new_mode

    # ------------------------------------------------------------------
    # Approval mode
    # ------------------------------------------------------------------

    def get_approval_mode(self, window_id: str) -> str:
        """Get approval mode for a window (default: 'normal')."""
        state = self.window_states.get(window_id)
        mode = state.approval_mode if state else DEFAULT_APPROVAL_MODE
        return mode if mode in APPROVAL_MODES else DEFAULT_APPROVAL_MODE

    def set_window_approval_mode(self, window_id: str, mode: str) -> None:
        """Set approval mode for a window."""
        normalized = mode.lower()
        if normalized not in APPROVAL_MODES:
            raise ValueError(f"Invalid approval mode: {mode!r}")
        state = self.get_window_state(window_id)
        state.approval_mode = normalized
        self._upsert_window_modes_to_db(window_id, approval_mode=normalized)
        self._schedule_save()

    # ------------------------------------------------------------------
    # Batch mode
    # ------------------------------------------------------------------

    def get_batch_mode(self, window_id: str) -> str:
        """Get batch mode for a window (default: 'batched')."""
        state = self.window_states.get(window_id)
        mode = state.batch_mode if state else DEFAULT_BATCH_MODE
        return mode if mode in BATCH_MODES else DEFAULT_BATCH_MODE

    def set_batch_mode(self, window_id: str, mode: str) -> None:
        """Set batch mode for a window."""
        if mode not in BATCH_MODES:
            raise ValueError(f"Invalid batch mode: {mode!r}")
        state = self.get_window_state(window_id)
        if state.batch_mode != mode:
            state.batch_mode = mode
            self._upsert_window_modes_to_db(window_id, batch_mode=mode)
            self._schedule_save()

    def cycle_batch_mode(self, window_id: str) -> str:
        """Toggle batch mode: batched ↔ verbose. Returns new mode."""
        current = self.get_batch_mode(window_id)
        new_mode = "verbose" if current == "batched" else "batched"
        self.set_batch_mode(window_id, new_mode)
        return new_mode

    # ------------------------------------------------------------------
    # Stale state pruning
    # ------------------------------------------------------------------

    def prune_stale_window_states(
        self,
        live_window_ids: set[str],
        session_map_wids: set[str],
        bound_window_ids: set[str],
    ) -> bool:
        """Remove window_states not in session_map, not bound, and not live.

        Returns True if any changes were made.
        """
        stale = [
            wid
            for wid in self.window_states
            if (
                wid not in session_map_wids
                and wid not in bound_window_ids
                and wid not in live_window_ids
            )
        ]
        if not stale:
            return False
        for wid in stale:
            logger.info("Pruning stale window_state: %s", wid)
            del self.window_states[wid]
            try:
                db = _db_path()
                if db.exists():
                    from . import store
                    conn = sqlite3.connect(str(db))
                    conn.row_factory = sqlite3.Row
                    try:
                        store.delete_window_modes(conn, wid)
                        conn.commit()
                    finally:
                        conn.close()
            except Exception:
                logger.debug(
                    "prune_stale_window_states: DB delete failed for %s", wid, exc_info=True
                )
        self._schedule_save()
        return True


window_store = WindowStateStore()
