"""Single-writer repository for ``sessions`` and ``topic_bindings`` tables.

All raw SQL writes to these two tables must route through this module.
No other code should directly INSERT/UPDATE/DELETE ``sessions`` or
``topic_bindings`` rows.

Design notes:
- Sync (not async) to match existing call-site conventions in
  ``session_lifecycle.py`` and ``polling_coordinator.py``.
- Every public function opens exactly one ``with store.connect() as conn:``
  context -- one connection, one transaction, one commit.
- ``retire_window`` is the ONLY path that writes ``status='retired'``
  outside an explicit user-initiated force-kill.  It always consults
  ``window_authority.confirm_dead_or_skip`` first unless ``force=True``.
- ``create_session_for_window`` atomically retires any existing active
  session for the same ``window_id`` before inserting the new row,
  satisfying the partial unique index
  ``idx_sessions_one_active_per_window``.
"""

from __future__ import annotations

import sqlite3
import time

import structlog

from . import store
from .window_authority import confirm_dead_or_skip

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def create_session_for_window(
    *,
    session_id: str,
    window_id: str,
    cwd: str,
    agent: str,
    mode: str | None = "normal",
    transcript_path: str | None = None,
    provider_session_id: str | None = None,
    transcript_offset: int = 0,
) -> None:
    """Atomically retire any prior active session for ``window_id``, then insert the new one.

    Both operations happen in a single transaction so the partial unique index
    ``idx_sessions_one_active_per_window`` is always satisfied -- there is never a
    moment where two active rows coexist for the same ``window_id``.

    Raises ``sqlite3.IntegrityError`` only if something outside this module
    inserted a second active row concurrently (should be impossible once all
    writers route here).
    """
    now = int(time.time())
    with store.connect() as conn:
        # Step 1 -- retire any existing active row for this window_id.
        # This removes the row that would conflict with the unique partial index.
        conn.execute(
            "UPDATE sessions SET status='retired', window_id=NULL, updated_at=?"
            " WHERE window_id=? AND status='active'",
            (now, window_id),
        )
        retired_count = conn.execute("SELECT changes()").fetchone()[0]
        if retired_count:
            logger.info(
                "create_session_for_window: retired prior active session",
                window_id=window_id,
                new_session_id=session_id,
            )

        # Step 1b -- if a pending row already exists for this session_id (the
        # promote-from-pending path), UPDATE it in place rather than deleting it.
        # Deleting would cascade to topic_bindings rows already inserted in step 5.
        # The UPDATE preserves FK relationships while promoting status to active.
        conn.execute(
            """UPDATE sessions
               SET status='active', window_id=?, mode=?, cwd=?, agent=?,
                   provider_session_id=?, transcript_offset=?, transcript_path=?,
                   updated_at=?
               WHERE session_id=? AND status='pending'""",
            (window_id, mode, cwd, agent,
             provider_session_id, transcript_offset, transcript_path,
             now, session_id),
        )
        promoted = conn.execute("SELECT changes()").fetchone()[0]

        if not promoted:
            # No pending row -- plain insert (the normal new-session path).
            conn.execute(
                """
                INSERT INTO sessions
                    (session_id, cwd, agent, mode, status, window_id,
                     created_at, updated_at,
                     provider_session_id, transcript_offset, transcript_path)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    cwd,
                    agent,
                    mode,
                    window_id,
                    now,
                    now,
                    provider_session_id,
                    transcript_offset,
                    transcript_path,
                ),
            )
    logger.info(
        "create_session_for_window: inserted active session",
        session_id=session_id,
        window_id=window_id,
        agent=agent,
    )


def retire_window(window_id: str, *, reason: str, force: bool = False) -> bool:
    """Retire the active session for ``window_id`` if the window is confirmed dead.

    When ``force=False`` (default):
        Calls ``window_authority.confirm_dead_or_skip(window_id, reason)``.
        If the window is still alive that function logs a WARNING and returns
        ``False``; ``retire_window`` returns ``False`` without writing.

    When ``force=True``:
        Skips the authority check.  Use ONLY for explicit user-initiated kills
        (i.e. ``session_lifecycle.delete_session``).

    Returns ``True`` if a row was retired, ``False`` otherwise.
    """
    if not force:
        confirmed_dead = confirm_dead_or_skip(window_id, reason)
        if not confirmed_dead:
            logger.warning(
                "retire_window: skipped -- window alive per authority",
                window_id=window_id,
                reason=reason,
            )
            return False

    now = int(time.time())
    with store.connect() as conn:
        conn.execute(
            "UPDATE sessions SET status='retired', window_id=NULL, updated_at=?"
            " WHERE window_id=? AND status='active'",
            (now, window_id),
        )
        retired = conn.execute("SELECT changes()").fetchone()[0]

    if retired:
        logger.info(
            "retire_window: retired session",
            window_id=window_id,
            reason=reason,
            force=force,
        )
        return True

    logger.debug(
        "retire_window: no active session found for window_id",
        window_id=window_id,
        reason=reason,
    )
    return False

def retire_by_session_id(session_id: str, *, reason: str) -> bool:
    """Retire a session by session_id, bypassing the window liveness check.

    Used when the session has no window_id (e.g. window already cleared
    before delete_session was called).  Always force-retires: caller is
    responsible for ensuring the session is actually dead.

    Returns ``True`` if a row was retired, ``False`` if no active row found.
    """
    now = int(time.time())
    with store.connect() as conn:
        conn.execute(
            "UPDATE sessions SET status='retired', window_id=NULL, updated_at=?"
            " WHERE session_id=? AND status='active'",
            (now, session_id),
        )
        retired = conn.execute("SELECT changes()").fetchone()[0]

    if retired:
        logger.info(
            "retire_by_session_id: retired session",
            session_id=session_id,
            reason=reason,
        )
        return True

    logger.debug(
        "retire_by_session_id: no active row found",
        session_id=session_id,
        reason=reason,
    )
    return False


def update_offset(session_id: str, offset: int) -> None:
    """Update ``transcript_offset`` for the given session."""
    now = int(time.time())
    with store.connect() as conn:
        conn.execute(
            "UPDATE sessions SET transcript_offset=?, updated_at=? WHERE session_id=?",
            (offset, now, session_id),
        )


def update_transcript_path(session_id: str, path: str) -> None:
    """Update ``transcript_path`` for the given session."""
    now = int(time.time())
    with store.connect() as conn:
        conn.execute(
            "UPDATE sessions SET transcript_path=?, updated_at=? WHERE session_id=?",
            (path, now, session_id),
        )


def update_provider_session_id(session_id: str, provider_session_id: str) -> None:
    """Update ``provider_session_id`` for the given session."""
    now = int(time.time())
    with store.connect() as conn:
        conn.execute(
            "UPDATE sessions SET provider_session_id=?, updated_at=? WHERE session_id=?",
            (provider_session_id, now, session_id),
        )


# ---------------------------------------------------------------------------
# Topic binding helpers
# ---------------------------------------------------------------------------


def bind_topic(
    *,
    group_id: int,
    topic_id: int,
    session_id: str,
    topic_title: str,
    user_id: int | None = None,
    window_id: str | None = None,
) -> None:
    """INSERT OR REPLACE a topic binding (full v3 schema)."""
    now = int(time.time())
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO topic_bindings
                (group_id, topic_id, session_id, topic_title, bound_at, user_id, window_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, topic_id) DO UPDATE SET
                session_id  = excluded.session_id,
                topic_title = excluded.topic_title,
                bound_at    = excluded.bound_at,
                user_id     = excluded.user_id,
                window_id   = excluded.window_id
            """,
            (group_id, topic_id, session_id, topic_title, now, user_id, window_id),
        )


def unbind_topic(group_id: int, topic_id: int) -> bool:
    """DELETE the binding for ``(group_id, topic_id)``.

    Returns ``True`` if a row was deleted, ``False`` if the binding was absent.
    """
    with store.connect() as conn:
        conn.execute(
            "DELETE FROM topic_bindings WHERE group_id=? AND topic_id=?",
            (group_id, topic_id),
        )
        deleted = conn.execute("SELECT changes()").fetchone()[0]
    return bool(deleted)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def list_active_sessions() -> list[dict]:
    """Return all active sessions as plain dicts."""
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status='active'"
        ).fetchall()
        return [dict(r) for r in rows]


def get_by_window_id(window_id: str) -> dict | None:
    """Return the single active session for ``window_id``, or ``None``."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE window_id=? AND status='active'",
            (window_id,),
        ).fetchone()
        return dict(row) if row else None


def get_by_session_id(session_id: str) -> dict | None:
    """Return any session by ``session_id`` regardless of status, or ``None``."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

# ---------------------------------------------------------------------------
# In-memory hydration
# ---------------------------------------------------------------------------


def hydrate_in_memory(
    *,
    thread_router=None,
    window_store=None,
    monitor_state=None,
) -> dict:
    """Rebuild in-memory caches from DB authoritative state.

    Reads (sessions, topic_bindings) in a single TX, atomically rebuilds
    caller-provided in-memory dicts. Each cache is rebuilt to be a strict
    projection of DB state — no stale entries survive.

    ``thread_router`` must be a ``ThreadRouter`` instance (or duck-typed
    equivalent with a ``thread_bindings`` dict attribute).

    ``window_store`` must be a ``WindowStateStore`` instance with a
    ``window_states`` dict attribute.

    ``monitor_state`` must be a ``MonitorState`` instance with a
    ``tracked_sessions`` dict attribute.

    Returns counts: {'sessions': N, 'bindings': M, 'updated_router_bindings': K}
    """
    import sqlite3 as _sqlite3

    try:
        with store.connect() as conn:
            sessions_rows = conn.execute(
                "SELECT session_id, window_id, status, cwd, agent, mode, "
                "transcript_path, transcript_offset "
                "FROM sessions WHERE status='active'"
            ).fetchall()
            bindings_rows = conn.execute(
                "SELECT group_id, topic_id, session_id, user_id, window_id "
                "FROM topic_bindings"
            ).fetchall()
            # list_prefs deserializes JSON values — value is int/str/etc, not raw JSON
            gchat_rows = store.list_prefs(conn, "group_chat")
    except (_sqlite3.DatabaseError, FileNotFoundError) as exc:
        logger.warning("hydrate_in_memory: DB read failed: %s", exc)
        return {"sessions": 0, "bindings": 0, "updated_router_bindings": 0}

    sessions_count = len(sessions_rows)
    bindings_count = len(bindings_rows)
    sid_to_wid: dict[str, str] = {
        r["session_id"]: r["window_id"]
        for r in sessions_rows
        if r["window_id"]
    }

    # ------------------------------------------------------------------ #
    # Rebuild thread_router.thread_bindings                               #
    # ------------------------------------------------------------------ #
    updated_router = 0
    if thread_router is not None:
        # Build uid lookup from group_chat prefs: key="uid:tid" -> uid
        gid_tid_to_uid: dict[tuple[int, int], int] = {}
        for _scope_id, key, value in gchat_rows:
            try:
                uid_s, tid_s = key.split(":", 1)
                gid_tid_to_uid[(int(value), int(tid_s))] = int(uid_s)
            except (ValueError, TypeError):
                continue

        from .config import config as _cfg
        _fallback_uid = next(
            (u for u in getattr(_cfg, "allowed_users", set()) if isinstance(u, int)),
            None,
        )

        desired: dict[int, dict[int, str]] = {}
        for b in bindings_rows:
            uid = (
                gid_tid_to_uid.get((b["group_id"], b["topic_id"]))
                or b["user_id"]
                or _fallback_uid
            )
            wid = b["window_id"] or sid_to_wid.get(b["session_id"])
            if uid is None or wid is None:
                continue
            desired.setdefault(uid, {})[b["topic_id"]] = wid

        # Multi-user: replicate desired bindings for all allowed users
        all_uids = set(desired.keys())
        for _au in getattr(_cfg, "allowed_users", set()):
            if isinstance(_au, int):
                all_uids.add(_au)
        all_topic_bindings: dict[int, str] = {}
        for _d in desired.values():
            all_topic_bindings.update(_d)
        if all_topic_bindings:
            for _au in all_uids:
                desired.setdefault(_au, {}).update(all_topic_bindings)

        # Atomic swap: replace dict contents rather than the dict object itself
        new_bindings: dict[int, dict[int, str]] = {}
        for uid, topics in desired.items():
            new_bindings[uid] = dict(topics)

        thread_router.thread_bindings.clear()
        thread_router.thread_bindings.update(new_bindings)
        thread_router._rebuild_reverse_index()
        updated_router = sum(len(t) for t in new_bindings.values())
        logger.info(
            "hydrate_in_memory: rebuilt thread_bindings",
            bindings=updated_router,
        )

    # ------------------------------------------------------------------ #
    # Rebuild window_store.window_states session_id mapping               #
    # ------------------------------------------------------------------ #
    if window_store is not None:
        from .window_state_store import WindowState

        new_states: dict[str, "WindowState"] = {}
        for r in sessions_rows:
            wid = r["window_id"]
            if not wid:
                continue
            existing = window_store.window_states.get(wid)
            if existing is not None:
                # Refresh session_id only — preserve mode settings already cached
                if existing.session_id != r["session_id"]:
                    from dataclasses import replace as _replace
                    new_states[wid] = _replace(existing, session_id=r["session_id"])
                else:
                    new_states[wid] = existing
            else:
                new_states[wid] = WindowState(
                    session_id=r["session_id"],
                    cwd=r["cwd"] or "",
                )
        window_store.window_states.clear()
        window_store.window_states.update(new_states)
        logger.info(
            "hydrate_in_memory: rebuilt window_states",
            count=len(new_states),
        )

    # ------------------------------------------------------------------ #
    # Rebuild monitor_state.tracked_sessions                              #
    # ------------------------------------------------------------------ #
    if monitor_state is not None:
        from .monitor_state import TrackedSession

        new_tracked: dict[str, "TrackedSession"] = {}
        for r in sessions_rows:
            tp = r["transcript_path"]
            offset = r["transcript_offset"]
            if not tp or not (offset and offset > 0):
                continue
            new_tracked[r["session_id"]] = TrackedSession(
                session_id=r["session_id"],
                file_path=tp,
                last_byte_offset=offset,
            )
        monitor_state.tracked_sessions.clear()
        monitor_state.tracked_sessions.update(new_tracked)
        logger.info(
            "hydrate_in_memory: rebuilt tracked_sessions",
            count=len(new_tracked),
        )

    return {
        "sessions": sessions_count,
        "bindings": bindings_count,
        "updated_router_bindings": updated_router,
    }
