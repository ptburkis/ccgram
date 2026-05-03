"""Thread routing — Telegram topic to tmux window binding.

Maps Telegram topics (user_id + thread_id) to tmux windows (window_id)
bidirectionally.  Manages group chat IDs for multi-group forum topic
routing and display names for windows.

Phase 4: All mutations are written synchronously to the SQLite DB via
store helpers.  The in-memory dicts are retained as a fast in-process
cache and for backward compat with callers that read them directly.
``to_dict()`` returns an empty dict (session.py no longer serialises
routing state to state.json).  ``from_dict()`` is a no-op (startup
uses _load_state_from_db via session.py instead).

Key class: ThreadRouter (singleton instantiated as ``thread_router``).
Key data:
  - thread_bindings  (user_id -> {thread_id -> window_id})
  - _window_to_thread (reverse index for O(1) inbound lookups)
  - group_chat_ids   (composite key -> chat_id)
  - window_display_names (window_id -> display name)
"""

from __future__ import annotations

import structlog
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

logger = structlog.get_logger()


def _get_conn():
    """Open a short-lived DB connection via store.connect()."""
    from . import store
    return store.connect()


@dataclass
class ThreadRouter:
    """Bidirectional mapping between Telegram topics and tmux windows.

    Owns thread_bindings, group_chat_ids, window_display_names, and
    the reverse index _window_to_thread.  All mutations write through
    to the SQLite DB synchronously (Phase 4).  In-memory dicts remain
    as a fast cache for the current process lifetime.

    ``_schedule_save`` is a no-op callback kept for API compat — DB
    writes are synchronous, no debounce needed.
    """

    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    # "user_id:thread_id" -> chat_id (supports multiple groups per user)
    group_chat_ids: dict[str, int] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)

    # Reverse index: (user_id, window_id) -> thread_id for O(1) inbound lookups
    _window_to_thread: dict[tuple[int, str], int] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        # Instance attributes (not fields) — avoids descriptor protocol binding
        self._schedule_save: Callable[[], None] = lambda: None
        self._has_window_state: Callable[[str], bool] = lambda _wid: False

    def reset(self) -> None:
        """Clear all state.  Used for test isolation."""
        self.thread_bindings.clear()
        self.group_chat_ids.clear()
        self.window_display_names.clear()
        self._window_to_thread.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_reverse_index(self) -> None:
        """Rebuild _window_to_thread from thread_bindings."""
        self._window_to_thread = {}
        for uid, bindings in self.thread_bindings.items():
            for tid, wid in bindings.items():
                self._window_to_thread[(uid, wid)] = tid

    def _dedup_thread_bindings(self) -> None:
        """Enforce 1 window = 1 thread.  Keep highest thread_id per window."""
        for _uid, bindings in self.thread_bindings.items():
            window_threads: dict[str, list[int]] = {}
            for tid, wid in bindings.items():
                window_threads.setdefault(wid, []).append(tid)
            for wid, tids in window_threads.items():
                if len(tids) > 1:
                    keep = max(tids)
                    for tid in tids:
                        if tid != keep:
                            del bindings[tid]
                            logger.warning(
                                "Startup: removed duplicate binding "
                                "thread %d -> window %s (keeping %d)",
                                tid,
                                wid,
                                keep,
                            )

    # ------------------------------------------------------------------
    # Serialization (Phase 4: no-op — DB is authoritative)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return empty dict — routing state is now persisted in SQLite.

        session.py calls this during state serialisation; returning an
        empty dict means thread routing data is no longer written to
        state.json.
        """
        return {}

    def from_dict(self, data: dict[str, Any]) -> None:
        """No-op — startup state is loaded from DB by session.py.

        The in-memory dicts (thread_bindings etc.) are populated by
        SessionManager._load_state_from_db(), which reads from the
        topic_bindings and user_prefs tables.  This method is kept for
        API compatibility but does nothing in Phase 4.
        """

    # ------------------------------------------------------------------
    # Thread binding operations
    # ------------------------------------------------------------------

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        """Bind a Telegram topic thread to a tmux window.

        Writes through to DB (topic_bindings.user_id + window_id) and
        maintains in-memory cache.  Enforces 1 topic = 1 window.
        """
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}

        # Enforce 1:1 — unbind any OTHER thread pointing to this window
        stale = [
            tid
            for tid, wid in self.thread_bindings[user_id].items()
            if wid == window_id and tid != thread_id
        ]
        for tid in stale:
            del self.thread_bindings[user_id][tid]
            logger.info(
                "Evicted stale binding: thread %d -> window_id %s "
                "(replaced by thread %d)",
                tid,
                window_id,
                thread_id,
            )

        # Clean up stale reverse index if this thread was previously bound elsewhere
        old_window = self.thread_bindings[user_id].get(thread_id)
        if old_window is not None and old_window != window_id:
            self._window_to_thread.pop((user_id, old_window), None)

        self.thread_bindings[user_id][thread_id] = window_id
        self._window_to_thread[(user_id, window_id)] = thread_id
        if window_name:
            self.window_display_names[window_id] = window_name

        # Write through to DB: update user_id and window_id on the binding row
        self._db_write_binding(user_id, thread_id, window_id)
        # Store display name in user_prefs
        if window_name:
            self._db_write_display_name(window_id, window_name)

        self._schedule_save()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def _db_write_binding(self, user_id: int, thread_id: int, window_id: str) -> None:
        """Update topic_bindings.user_id and window_id for the given topic_id."""
        try:
            with _get_conn() as conn:
                conn.execute(
                    """UPDATE topic_bindings
                       SET user_id = ?, window_id = ?
                       WHERE topic_id = ?""",
                    (user_id, window_id, thread_id),
                )
        except Exception:
            logger.debug("bind_thread: DB write failed for thread %d", thread_id, exc_info=True)

    def _db_clear_binding(self, thread_id: int) -> None:
        """Clear user_id and window_id on the topic_binding for topic_id."""
        try:
            with _get_conn() as conn:
                conn.execute(
                    """UPDATE topic_bindings
                       SET user_id = NULL, window_id = NULL
                       WHERE topic_id = ?""",
                    (thread_id,),
                )
        except Exception:
            logger.debug("unbind_thread: DB write failed for thread %d", thread_id, exc_info=True)

    def _db_write_display_name(self, window_id: str, name: str) -> None:
        """Persist display name in user_prefs(scope='window_name')."""
        try:
            from . import store
            with _get_conn() as conn:
                store.set_pref(conn, "window_name", "display_name", name, scope_id=window_id)
        except Exception:
            logger.debug("display_name: DB write failed for %s", window_id, exc_info=True)

    def _db_write_group_chat_id(self, key: str, chat_id: int) -> None:
        """Persist group_chat_id in user_prefs(scope='group_chat')."""
        try:
            from . import store
            with _get_conn() as conn:
                store.set_pref(conn, "group_chat", "chat_id", chat_id, scope_id=key)
        except Exception:
            logger.debug("group_chat_id: DB write failed for key %s", key, exc_info=True)

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Remove a thread binding.  Returns the previously bound window_id.

        Clears user_id and window_id on the topic_binding row (keeps
        session_id for history).  Cleans up in-memory caches.
        """
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        window_id = bindings.pop(thread_id)
        self._window_to_thread.pop((user_id, window_id), None)
        if not bindings:
            del self.thread_bindings[user_id]
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id,
            user_id,
        )

        # Clean up group_chat_id for the unbound thread
        chat_key = f"{user_id}:{thread_id}"
        self.group_chat_ids.pop(chat_key, None)

        # Clean up orphaned display name if nothing references this window
        still_bound = any(
            wid == window_id
            for ub in self.thread_bindings.values()
            for wid in ub.values()
        )
        if not still_bound and not self._has_window_state(window_id):
            self.window_display_names.pop(window_id, None)

        # Write through to DB
        self._db_clear_binding(thread_id)

        self._schedule_save()
        return window_id

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a thread.

        Falls back to DB if not in memory (e.g. binding created externally
        by a recovery script or dashboard).  On hit, populates the in-memory
        cache so subsequent lookups are fast.
        """
        bindings = self.thread_bindings.get(user_id)
        if bindings:
            wid = bindings.get(thread_id)
            if wid is not None:
                return wid

        # DB fallback: binding may exist but not yet loaded in-memory
        try:
            with _get_conn() as conn:
                row = conn.execute(
                    "SELECT window_id FROM topic_bindings "
                    "WHERE topic_id = ? AND window_id IS NOT NULL",
                    (thread_id,),
                ).fetchone()
                if row:
                    db_wid = row[0] if isinstance(row, tuple) else row["window_id"]
                    if user_id not in self.thread_bindings:
                        self.thread_bindings[user_id] = {}
                    self.thread_bindings[user_id][thread_id] = db_wid
                    self._window_to_thread[(user_id, db_wid)] = thread_id
                    logger.info(
                        "DB fallback: loaded binding thread %d -> %s for user %d",
                        thread_id, db_wid, user_id,
                    )
                    conn.execute(
                        "UPDATE topic_bindings SET user_id = ? "
                        "WHERE topic_id = ? AND user_id IS NULL",
                        (user_id, thread_id),
                    )
                    return db_wid
        except Exception:
            logger.debug("DB fallback lookup failed for thread %d", thread_id, exc_info=True)

        return None

    def get_thread_for_window(self, user_id: int, window_id: str) -> int | None:
        """Reverse lookup: get thread_id for a window (O(1) via reverse index)."""
        return self._window_to_thread.get((user_id, window_id))

    def get_all_thread_windows(self, user_id: int) -> dict[int, str]:
        """Get all thread bindings for a user."""
        return dict(self.thread_bindings.get(user_id, {}))

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
    ) -> str | None:
        """Resolve the tmux window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def has_window(self, window_id: str) -> bool:
        """Check if any user has a binding to this window_id."""
        return any(wid == window_id for (_, wid) in self._window_to_thread)

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
        """Iterate all thread bindings as (user_id, thread_id, window_id)."""
        for user_id, bindings in list(self.thread_bindings.items()):
            for thread_id, window_id in list(bindings.items()):
                yield user_id, thread_id, window_id

    # ------------------------------------------------------------------
    # Group chat ID management
    # ------------------------------------------------------------------

    def set_group_chat_id(self, user_id: int, thread_id: int, chat_id: int) -> None:
        """Store the group chat ID for a user's thread.

        Uses composite key ``user_id:thread_id`` to support multiple
        groups per user.  Writes through to user_prefs DB.
        """
        key = f"{user_id}:{thread_id}"
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            self._db_write_group_chat_id(key, chat_id)
            self._schedule_save()
            logger.info(
                "Stored group chat_id %d for user %d, thread %d",
                chat_id,
                user_id,
                thread_id,
            )

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve the chat_id for sending messages.

        In forum topics (thread_id is set), returns the stored group chat_id
        for that specific thread (user_id:thread_id).
        Falls back to user_id for direct messages or if no group_id stored.
        """
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
        return user_id

    def get_window_for_chat_thread(self, chat_id: int, thread_id: int) -> str | None:
        """Resolve window_id for a specific Telegram chat/thread pair."""
        for user_id, bindings in self.thread_bindings.items():
            window_id = bindings.get(thread_id)
            if not window_id:
                continue
            key = f"{user_id}:{thread_id}"
            resolved_chat = self.group_chat_ids.get(key, user_id)
            if resolved_chat == chat_id:
                return window_id
        return None

    # ------------------------------------------------------------------
    # Display name management
    # ------------------------------------------------------------------

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def set_display_name(self, window_id: str, window_name: str) -> None:
        """Update display name for a window_id."""
        if self.window_display_names.get(window_id) != window_name:
            self.window_display_names[window_id] = window_name
            self._db_write_display_name(window_id, window_name)
            self._schedule_save()

    def sync_display_names(self, live_windows: list[tuple[str, str]]) -> bool:
        """Sync display names from live tmux windows.  Returns True if changed.

        Saves state internally when changes are detected.
        """
        changed = False
        for window_id, window_name in live_windows:
            old = self.window_display_names.get(window_id)
            if old and old != window_name:
                self.window_display_names[window_id] = window_name
                changed = True
                logger.info(
                    "Synced display name: %s %s → %s", window_id, old, window_name
                )
        if changed:
            self._schedule_save()
        return changed


# Module-level singleton — wired by SessionManager.__post_init__()
thread_router = ThreadRouter()
