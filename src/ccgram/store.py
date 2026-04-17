"""SQLite state store for CCGram — single authoritative source for session/topic state.

DB location: ``ccgram_dir() / "state.db"`` (expose via :func:`db_path`).

Schema overview:
- ``sessions``       — agent sessions keyed on UUID session_id
- ``topic_bindings`` — Telegram (group_id, topic_id) → session_id mapping (1-to-1)
- ``orphaned_topics``— topics known but not yet bound to a session
- ``heartbeats``     — liveness beacons from long-running components
- ``crons``          — scheduled message definitions
- ``user_prefs``     — generic KV store for per-scope user preferences
- ``window_modes``   — per-window mode settings (approval, batch, notification)

Schema decision for prefs — single KV table instead of per-feature tables:
The data is heterogeneous (display flags, directory favorites, read offsets), rarely
queried relationally, and grows organically as new per-window/per-group features are
added. A single ``user_prefs(scope, scope_id, key, value)`` table covers all of these
without premature schema commits. If query patterns later demand relational access,
individual fields can be promoted to structured tables in a follow-up migration.

Schema versions:
  1 — initial schema (sessions, topic_bindings, orphaned_topics, heartbeats, crons, user_prefs)
  2 — crons: added target_session_id, target_topic_id, target_group_id columns
  3 — topic_bindings: added user_id, window_id; new window_modes table
"""

import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccgram.utils import ccgram_dir

logger = logging.getLogger(__name__)

# ---- Schema ------------------------------------------------------------------

_SCHEMA_VERSION = "3"

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd        TEXT NOT NULL,
    agent      TEXT NOT NULL,
    mode       TEXT,
    status     TEXT NOT NULL CHECK (status IN ('pending','active','errored','retired')),
    window_id  TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_window_id ON sessions(window_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status    ON sessions(status);

CREATE TABLE IF NOT EXISTS topic_bindings (
    group_id    INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL,
    session_id  TEXT    NOT NULL UNIQUE,
    topic_title TEXT    NOT NULL,
    bound_at    INTEGER NOT NULL,
    user_id     INTEGER,
    window_id   TEXT,
    PRIMARY KEY (group_id, topic_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_topic_bindings_session ON topic_bindings(session_id);

CREATE TABLE IF NOT EXISTS orphaned_topics (
    group_id    INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL,
    topic_title TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    PRIMARY KEY (group_id, topic_id)
);

CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    last_beat INTEGER NOT NULL,
    details   TEXT
);

CREATE TABLE IF NOT EXISTS crons (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    schedule          TEXT NOT NULL,
    target_window     TEXT NOT NULL,  -- DEPRECATED: use target_session_id or target_topic_id
    message           TEXT NOT NULL,
    enabled           INTEGER NOT NULL CHECK (enabled IN (0,1)),
    last_run          INTEGER,
    last_result       TEXT,
    created_at        INTEGER NOT NULL,
    -- v2: canonical targeting columns (prefer over target_window)
    target_session_id TEXT,           -- canonical: session UUID from sessions table
    target_topic_id   INTEGER,        -- canonical: Telegram topic_id (pair with target_group_id)
    target_group_id   INTEGER         -- canonical: Telegram group_id (pair with target_topic_id)
);
CREATE INDEX IF NOT EXISTS idx_crons_enabled ON crons(enabled);

CREATE TABLE IF NOT EXISTS user_prefs (
    scope      TEXT    NOT NULL,
    scope_id   TEXT    NOT NULL DEFAULT '',
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope, scope_id, key)
);
CREATE INDEX IF NOT EXISTS idx_user_prefs_scope ON user_prefs(scope);

CREATE TABLE IF NOT EXISTS window_modes (
    window_id         TEXT    PRIMARY KEY,
    approval_mode     TEXT    NOT NULL DEFAULT 'yolo',
    batch_mode        TEXT    NOT NULL DEFAULT 'batched',
    notification_mode TEXT    NOT NULL DEFAULT 'summary',
    provider_name     TEXT    NOT NULL DEFAULT '',
    external          INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL
);
"""

# ---- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """Row from the ``sessions`` table."""

    session_id: str
    cwd: str
    agent: str
    mode: str | None
    status: str
    window_id: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class TopicBinding:
    """Row from the ``topic_bindings`` table."""

    group_id: int
    topic_id: int
    session_id: str
    topic_title: str
    bound_at: int
    # v3: routing fields (may be None for rows written before migration)
    user_id: int | None = None
    window_id: str | None = None


@dataclass(frozen=True, slots=True)
class WindowModes:
    """Row from the ``window_modes`` table."""

    window_id: str
    approval_mode: str
    batch_mode: str
    notification_mode: str
    provider_name: str
    external: bool
    updated_at: int


@dataclass(frozen=True, slots=True)
class OrphanedTopic:
    """Row from the ``orphaned_topics`` table."""

    group_id: int
    topic_id: int
    topic_title: str
    first_seen: int


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Row from the ``heartbeats`` table."""

    component: str
    last_beat: int
    details: dict | None


@dataclass(frozen=True, slots=True)
class Cron:
    """Row from the ``crons`` table."""

    id: int
    name: str
    schedule: str
    target_window: (
        str  # DEPRECATED — use target_session_id or (target_group_id, target_topic_id)
    )
    message: str
    enabled: bool
    last_run: int | None
    last_result: str | None
    created_at: int
    # v2: canonical targeting (prefer over target_window)
    target_session_id: str | None = None
    target_topic_id: int | None = None
    target_group_id: int | None = None


# ---- DB path + init ----------------------------------------------------------


def db_path() -> Path:
    """Return the canonical path of the SQLite database file."""
    return ccgram_dir() / "state.db"


_V2_ALTERS = [
    ("target_session_id", "ALTER TABLE crons ADD COLUMN target_session_id TEXT"),
    ("target_topic_id", "ALTER TABLE crons ADD COLUMN target_topic_id INTEGER"),
    ("target_group_id", "ALTER TABLE crons ADD COLUMN target_group_id INTEGER"),
]

_V3_TOPIC_ALTERS = [
    ("user_id", "ALTER TABLE topic_bindings ADD COLUMN user_id INTEGER"),
    ("window_id", "ALTER TABLE topic_bindings ADD COLUMN window_id TEXT"),
]

_V3_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_topic_bindings_user_topic ON topic_bindings(user_id, topic_id)",
    "CREATE INDEX IF NOT EXISTS idx_topic_bindings_window ON topic_bindings(window_id)",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations not yet present.  Idempotent."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    current = int(row[0]) if row else 1

    if current < 2:  # noqa: PLR2004
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(crons)").fetchall()}
        for col, stmt in _V2_ALTERS:
            if col not in existing_cols:
                conn.execute(stmt)
                logger.info("schema migration v2: added crons.%s", col)
        conn.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
        conn.commit()
        current = 2

    if current < 3:  # noqa: PLR2004
        _apply_v3_schema_additions(conn)
        conn.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
        conn.commit()
    else:
        # Always ensure v3 additions are present (idempotent) even for new DBs
        # that start at v3 and skip the migration block.
        _apply_v3_schema_additions(conn)
        conn.commit()


def _apply_v3_schema_additions(conn: sqlite3.Connection) -> None:
    """Apply v3 schema additions to topic_bindings and create window_modes.

    Idempotent — checks existing columns before altering.
    Does NOT migrate state.json data — that happens separately via migrate_to_v3().
    """
    existing_tb_cols = {r[1] for r in conn.execute("PRAGMA table_info(topic_bindings)").fetchall()}
    for col, stmt in _V3_TOPIC_ALTERS:
        if col not in existing_tb_cols:
            conn.execute(stmt)
            logger.info("schema migration v3: added topic_bindings.%s", col)

    # Populate topic_bindings.window_id from sessions table where possible.
    # Idempotent: WHERE window_id IS NULL means it's a no-op if already populated.
    conn.execute("""
        UPDATE topic_bindings
        SET window_id = (
            SELECT s.window_id FROM sessions s
            WHERE s.session_id = topic_bindings.session_id
              AND s.window_id IS NOT NULL
        )
        WHERE window_id IS NULL
    """)

    # Create window_modes table (DDL already includes IF NOT EXISTS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS window_modes (
            window_id         TEXT    PRIMARY KEY,
            approval_mode     TEXT    NOT NULL DEFAULT 'yolo',
            batch_mode        TEXT    NOT NULL DEFAULT 'batched',
            notification_mode TEXT    NOT NULL DEFAULT 'summary',
            provider_name     TEXT    NOT NULL DEFAULT '',
            external          INTEGER NOT NULL DEFAULT 0,
            updated_at        INTEGER NOT NULL
        )
    """)

    for stmt in _V3_INDEXES:
        conn.execute(stmt)

    logger.info("schema migration v3: schema additions applied")


def _resolve_binding_from_markers(
    markers_by_name: "dict[str, dict]",
    topic_title: str,
) -> "tuple[str, str] | None":
    """Look up (session_id, window_id) for *topic_title* in PTY marker data.

    *markers_by_name* is a dict keyed by ``window_name`` (as written in each
    marker file).  *topic_title* is stripped of a leading ``[M]`` prefix and
    any ``\u26a1`` (⚡) emoji before matching.

    Returns ``(session_id, window_id)`` if a live marker matches, else ``None``.
    """
    # Normalise the topic title: strip [M] prefix, ⚡ emoji, and whitespace.
    normalised = topic_title
    for prefix in ("[M] ", "[M]"):
        if normalised.startswith(prefix):
            normalised = normalised[len(prefix):]
    normalised = normalised.replace("\u26a1", "").strip()

    marker = markers_by_name.get(normalised)
    if marker is None:
        return None
    sid = marker.get("session_id", "")
    wid = marker.get("window_id", "")
    if sid and wid:
        return sid, wid
    return None


def _backfill_null_user_ids(
    conn: sqlite3.Connection,
    summary: dict[str, Any],
    allowed_users: "set[int] | None" = None,
) -> None:
    """Step 5 of migrate_to_v3: backfill user_id=NULL rows from ALLOWED_USERS.

    *allowed_users* — explicit set of allowed Telegram user IDs passed by the
    caller (e.g. from ``config.allowed_users``).  When *None* the function
    falls back to reading the ``ALLOWED_USERS`` environment variable directly,
    which preserves backwards-compatibility for the test and CLI paths.

    Emits a WARNING log whenever the backfill is skipped so the operator can
    see it clearly in bot logs.
    """
    null_rows = conn.execute(
        "SELECT topic_id FROM topic_bindings WHERE user_id IS NULL"
    ).fetchall()
    if not null_rows:
        return

    if allowed_users is not None:
        allowed_ids: list[int] = sorted(allowed_users)
    else:
        # Fallback: parse ALLOWED_USERS env var (test / CLI path)
        allowed_users_raw = os.getenv("ALLOWED_USERS", "")
        allowed_ids = []
        for part in allowed_users_raw.split(","):
            part = part.strip()
            if part:
                try:
                    allowed_ids.append(int(part))
                except ValueError:
                    pass

    null_topic_ids = [r["topic_id"] for r in null_rows]
    if len(allowed_ids) == 1:
        uid = allowed_ids[0]
        cur = conn.execute(
            "UPDATE topic_bindings SET user_id = ? WHERE user_id IS NULL", (uid,)
        )
        count = cur.rowcount
        logger.info(
            "migrate_to_v3: backfilled user_id=%d for %d topic_binding row(s)",
            uid, count,
        )
        summary["user_ids_set"] += count
    elif len(allowed_ids) == 0:
        logger.warning(
            "migrate_to_v3: BACKFILL SKIPPED — %d topic_binding row(s) still have "
            "user_id=NULL and ALLOWED_USERS is empty — topic_ids: %s",
            len(null_topic_ids), null_topic_ids,
        )
    else:
        logger.warning(
            "migrate_to_v3: BACKFILL SKIPPED — %d topic_binding row(s) still have "
            "user_id=NULL and ALLOWED_USERS is ambiguous (%d users) — topic_ids: %s. "
            "Pass allowed_users explicitly if there is a single primary admin.",
            len(null_topic_ids), len(allowed_ids), null_topic_ids,
        )


def migrate_to_v3(
    db: "str | Path",
    state_json: "str | Path",
    *,
    markers_dir: "str | Path | None" = None,
    allowed_users: "set[int] | None" = None,
) -> dict[str, Any]:
    """Migrate state.json routing data into the v3 DB schema.

    Reads thread_bindings, window_states, group_chat_ids, window_display_names
    from state.json and upserts them into the DB.  Returns a summary dict with
    counts of rows affected.  Safe to call on a backup DB.

    *markers_dir* — path to the PTY marker directory (``~/.ccgram/active-sessions/``
    by default when ``None``).  Each ``*.json`` file there is authoritative for
    the live ``window_id`` and ``session_id`` of a named window.  Marker data
    takes priority over thread_bindings and sessions table data when resolving
    window_id for topic_bindings.

    *allowed_users* — explicit set of allowed Telegram user IDs (from
    ``config.allowed_users``).  Passed through to the user_id backfill step so
    the live bot path does not rely solely on the ``ALLOWED_USERS`` env var.
    When ``None`` the backfill falls back to reading the env var directly
    (preserves test / CLI compatibility).

    This is a standalone function (not called automatically on init_db) so that
    the caller (session.py on first v3 boot, or the dry-run verification path)
    controls when migration happens.
    """
    db_path_resolved = Path(db)
    state_path = Path(state_json)

    summary: dict[str, Any] = {
        "user_ids_set": 0,
        "window_ids_set": 0,
        "window_modes_inserted": 0,
        "group_chat_prefs_inserted": 0,
        "display_name_prefs_inserted": 0,
    }

    if not state_path.exists():
        logger.info("migrate_to_v3: no state.json at %s — nothing to migrate", state_path)
        return summary

    try:
        raw = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("migrate_to_v3: could not read state.json: %s", exc)
        return summary

    # Ensure DB is at v3 schema
    init_db(db_path_resolved)

    # Load PTY marker files — keyed by window_name for fast lookup.
    # These are the authoritative source for live window_id / session_id.
    from pathlib import Path as _Path
    from ccgram.utils import ccgram_dir as _ccgram_dir
    _markers_root: _Path = _Path(markers_dir) if markers_dir is not None else (
        _ccgram_dir() / "active-sessions"
    )
    markers_by_name: dict[str, dict] = {}
    if _markers_root.is_dir():
        for _mf in _markers_root.glob("*.json"):
            try:
                _md = json.loads(_mf.read_text())
                _wname = _md.get("window_name", "")
                if _wname:
                    markers_by_name[_wname] = _md
            except Exception:
                pass
    logger.info(
        "migrate_to_v3: loaded %d live PTY marker(s) from %s",
        len(markers_by_name), _markers_root,
    )

    conn = sqlite3.connect(str(db_path_resolved))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        now = int(time.time())

        # 0. Populate window_id from sessions table where possible (fallback for
        #    rows not covered by thread_bindings or markers)
        conn.execute("""
            UPDATE topic_bindings
            SET window_id = (
                SELECT s.window_id FROM sessions s
                WHERE s.session_id = topic_bindings.session_id
                  AND s.window_id IS NOT NULL
            )
            WHERE window_id IS NULL
        """)

        # 1. Migrate thread_bindings → topic_bindings.user_id + window_id.
        #    For each topic_binding, prefer PTY marker data (authoritative live
        #    window_id/session_id) over the stale thread_bindings value.
        thread_bindings: dict[str, dict[str, str]] = raw.get("thread_bindings", {})
        for uid_str, bindings in thread_bindings.items():
            try:
                user_id = int(uid_str)
            except (ValueError, TypeError):
                continue
            for tid_str, window_id in bindings.items():
                try:
                    topic_id = int(tid_str)
                except (ValueError, TypeError):
                    continue
                # Find a matching topic_binding row by topic_id (no group_id in thread_bindings)
                rows = conn.execute(
                    "SELECT group_id, topic_id, topic_title, session_id FROM topic_bindings"
                    " WHERE topic_id = ?",
                    (topic_id,),
                ).fetchall()
                for row in rows:
                    # Prefer PTY marker: it has the live window_id + session_id.
                    marker_hit = _resolve_binding_from_markers(
                        markers_by_name, row["topic_title"]
                    )
                    if marker_hit is not None:
                        resolved_session_id, resolved_window_id = marker_hit
                        logger.info(
                            "migrate_to_v3: topic_id=%d '%s' — using PTY marker "
                            "window_id=%s session_id=%s (state.json had %s)",
                            topic_id, row["topic_title"],
                            resolved_window_id, resolved_session_id, window_id,
                        )
                        # Ensure the session from the marker exists in sessions table
                        # (it may be a new session not yet recorded during migration).
                        # Normalise title to look up the full marker dict.
                        _s1_norm = row["topic_title"]
                        for _s1p in ("[M] ", "[M]"):
                            if _s1_norm.startswith(_s1p):
                                _s1_norm = _s1_norm[len(_s1p):]
                        _s1_norm = _s1_norm.replace("⚡", "").strip()
                        _s1_marker = markers_by_name.get(_s1_norm, {})
                        conn.execute(
                            """INSERT OR IGNORE INTO sessions
                               (session_id, cwd, agent, status, window_id, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (resolved_session_id,
                             _s1_marker.get("cwd", "/"),
                             _s1_marker.get("provider", "claude"),
                             "active",
                             resolved_window_id,
                             now, now),
                        )
                        conn.execute(
                            """UPDATE topic_bindings
                               SET user_id = ?, window_id = ?, session_id = ?
                               WHERE group_id = ? AND topic_id = ?""",
                            (user_id, resolved_window_id, resolved_session_id,
                             row["group_id"], row["topic_id"]),
                        )
                    else:
                        conn.execute(
                            """UPDATE topic_bindings
                               SET user_id = ?, window_id = ?
                               WHERE group_id = ? AND topic_id = ?
                                 AND (user_id IS NULL OR window_id IS NULL)""",
                            (user_id, window_id, row["group_id"], row["topic_id"]),
                        )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        summary["user_ids_set"] += 1
                        summary["window_ids_set"] += 1

        # 1b. For topic_bindings NOT in thread_bindings, still try to resolve
        #     window_id (and session_id) from PTY markers using topic_title.
        unresolved_rows = conn.execute(
            "SELECT group_id, topic_id, topic_title, session_id"
            " FROM topic_bindings WHERE window_id IS NULL"
        ).fetchall()
        for row in unresolved_rows:
            marker_hit = _resolve_binding_from_markers(
                markers_by_name, row["topic_title"]
            )
            if marker_hit is not None:
                resolved_session_id, resolved_window_id = marker_hit
                logger.info(
                    "migrate_to_v3: topic_id=%d '%s' — PTY marker resolved "
                    "window_id=%s session_id=%s (not in thread_bindings)",
                    row["topic_id"], row["topic_title"],
                    resolved_window_id, resolved_session_id,
                )
                # Normalise topic_title to look up the full marker dict for cwd/provider
                _norm_title = row["topic_title"]
                for _pfx in ("[M] ", "[M]"):
                    if _norm_title.startswith(_pfx):
                        _norm_title = _norm_title[len(_pfx):]
                _norm_title = _norm_title.replace("⚡", "").strip()
                _full_marker = markers_by_name.get(_norm_title, {})
                conn.execute(
                    """INSERT OR IGNORE INTO sessions
                       (session_id, cwd, agent, status, window_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (resolved_session_id,
                     _full_marker.get("cwd", "/"),
                     _full_marker.get("provider", "claude"),
                     "active",
                     resolved_window_id,
                     now, now),
                )
                conn.execute(
                    """UPDATE topic_bindings
                       SET window_id = ?, session_id = ?
                       WHERE group_id = ? AND topic_id = ?""",
                    (resolved_window_id, resolved_session_id,
                     row["group_id"], row["topic_id"]),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    summary["window_ids_set"] += 1

        # 2. Migrate window_states → window_modes
        window_states: dict[str, dict[str, Any]] = raw.get("window_states", {})
        for wid, ws in window_states.items():
            if not isinstance(ws, dict):
                continue
            approval = ws.get("approval_mode", "yolo")
            batch = ws.get("batch_mode", "batched")
            notif = ws.get("notification_mode", "summary")
            # Collapse legacy notification modes
            if notif in ("errors_only", "muted"):
                notif = "summary"
            provider = ws.get("provider_name", "")
            external = int(bool(ws.get("external", False)))
            conn.execute(
                """INSERT INTO window_modes
                       (window_id, approval_mode, batch_mode, notification_mode,
                        provider_name, external, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(window_id) DO NOTHING""",
                (wid, approval, batch, notif, provider, external, now),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                summary["window_modes_inserted"] += 1

        # 3. Migrate group_chat_ids → user_prefs(scope='group_chat')
        group_chat_ids: dict[str, Any] = raw.get("group_chat_ids", {})
        for key, chat_id in group_chat_ids.items():
            # key is "user_id:thread_id", stored with scope_id='' and key=the composite key
            conn.execute(
                """INSERT INTO user_prefs (scope, scope_id, key, value, updated_at)
                   VALUES ('group_chat', '', ?, ?, ?)
                   ON CONFLICT(scope, scope_id, key) DO NOTHING""",
                (key, json.dumps(int(chat_id)), now),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                summary["group_chat_prefs_inserted"] += 1

        # 4. Migrate window_display_names → user_prefs(scope='window_name')
        display_names: dict[str, str] = raw.get("window_display_names", {})
        for wid, name in display_names.items():
            conn.execute(
                """INSERT INTO user_prefs (scope, scope_id, key, value, updated_at)
                   VALUES ('window_name', ?, 'display_name', ?, ?)
                   ON CONFLICT(scope, scope_id, key) DO NOTHING""",
                (wid, json.dumps(name), now),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                summary["display_name_prefs_inserted"] += 1

        # 5. Backfill user_id=NULL rows from ALLOWED_USERS env var
        _backfill_null_user_ids(conn, summary, allowed_users=allowed_users)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info("migrate_to_v3: %s", summary)
    return summary


def init_db(path: Path | None = None) -> Path:
    """Create the database file and schema idempotently.

    Safe to call multiple times — all DDL uses ``IF NOT EXISTS``.  Applies
    incremental migrations when an existing database is at a lower schema
    version.  Returns the resolved path so callers can chain:
    ``conn = sqlite3.connect(init_db())``.
    """
    resolved = path or db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    try:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", _SCHEMA_VERSION),
        )
        conn.commit()
        _apply_migrations(conn)
    finally:
        conn.close()
    return resolved


@contextmanager
def connect(path: Path | None = None):
    """Context manager that yields a configured ``sqlite3.Connection``.

    Runs ``init_db`` lazily if the file is missing.  On clean exit the
    transaction is committed; on exception it is rolled back.  The connection
    is always closed on exit.

    Yields:
        sqlite3.Connection with WAL journal mode, foreign keys enabled,
        synchronous=NORMAL, and ``row_factory = sqlite3.Row``.
    """
    resolved = path or db_path()
    if not resolved.exists():
        init_db(resolved)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---- Sessions ----------------------------------------------------------------


def upsert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    cwd: str,
    agent: str,
    status: str = "pending",
    mode: str | None = None,
    window_id: str | None = None,
    created_at: int | None = None,
) -> None:
    """Insert or update a session row.

    ``updated_at`` is always set to the current epoch second.  ``created_at``
    defaults to now on insert; an explicit value is used when the source data
    carries a timestamp (e.g. during migration).  On conflict the existing
    ``created_at`` is preserved.
    """
    now = int(time.time())
    c_at = created_at if created_at is not None else now
    # Don't steal window_id from an incumbent active session. Task subagent
    # SessionStart hooks arrive with a window_id that already belongs to the primary session.
    effective_window_id = window_id
    if window_id is not None and status == "active":
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE window_id=? AND status='active' AND session_id != ?",
            (window_id, session_id),
        ).fetchone()
        if row:
            logger.debug(
                "upsert_session: window_id %s already owned by active session %s — not stealing",
                window_id, row[0],
            )
            effective_window_id = None
    conn.execute(
        """
        INSERT INTO sessions (session_id, cwd, agent, mode, status, window_id,
                              created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            cwd        = excluded.cwd,
            agent      = excluded.agent,
            mode       = excluded.mode,
            status     = excluded.status,
            window_id  = excluded.window_id,
            updated_at = excluded.updated_at
        """,
        (session_id, cwd, agent, mode, status, effective_window_id, c_at, now),
    )


def get_session(conn: sqlite3.Connection, session_id: str) -> Session | None:
    """Return the session with the given ID, or ``None`` if not found."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return _row_to_session(row) if row else None


def get_session_by_window(conn: sqlite3.Connection, window_id: str) -> Session | None:
    """Return the most-recently-updated session for ``window_id``, or ``None``."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE window_id = ? ORDER BY updated_at DESC LIMIT 1",
        (window_id,),
    ).fetchone()
    return _row_to_session(row) if row else None


def list_sessions(conn: sqlite3.Connection, status: str | None = None) -> list[Session]:
    """Return all sessions, optionally filtered by ``status``."""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def delete_session(conn: sqlite3.Connection, session_id: str) -> int:
    """Delete a session by ID.  Returns row count (0 or 1)."""
    cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    return cur.rowcount


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        session_id=row["session_id"],
        cwd=row["cwd"],
        agent=row["agent"],
        mode=row["mode"],
        status=row["status"],
        window_id=row["window_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---- Topic bindings ----------------------------------------------------------


def upsert_topic_binding(
    conn: sqlite3.Connection,
    *,
    group_id: int,
    topic_id: int,
    session_id: str,
    topic_title: str,
    bound_at: int | None = None,
) -> None:
    """Insert or update a topic binding.

    If a *different* (group_id, topic_id) row already holds the same
    ``session_id``, the ``UNIQUE(session_id)`` constraint raises
    ``sqlite3.IntegrityError``.  The caller is responsible for catching that
    if needed — it signals a double-bind attempt and should surface loudly.
    """
    b_at = bound_at if bound_at is not None else int(time.time())
    conn.execute(
        """
        INSERT INTO topic_bindings
            (group_id, topic_id, session_id, topic_title, bound_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(group_id, topic_id) DO UPDATE SET
            session_id  = excluded.session_id,
            topic_title = excluded.topic_title,
            bound_at    = excluded.bound_at
        """,
        (group_id, topic_id, session_id, topic_title, b_at),
    )


def upsert_topic_binding_full(
    conn: sqlite3.Connection,
    group_id: int,
    topic_id: int,
    session_id: str,
    user_id: int,
    window_id: str,
    topic_title: str,
    bound_at: int,
) -> None:
    """Insert or update a topic binding with v3 user_id and window_id fields.

    Positional args (no keyword-only) to mirror the verb_noun(conn, ...) convention.
    """
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
        (group_id, topic_id, session_id, topic_title, bound_at, user_id, window_id),
    )


def get_window_id_for_topic(
    conn: sqlite3.Connection, group_id: int, topic_id: int
) -> str | None:
    """Return the window_id bound to (group_id, topic_id), or None."""
    row = conn.execute(
        "SELECT window_id FROM topic_bindings WHERE group_id = ? AND topic_id = ?",
        (group_id, topic_id),
    ).fetchone()
    return row["window_id"] if row else None


def find_topic_for_window(
    conn: sqlite3.Connection, user_id: int, window_id: str
) -> tuple[int, int] | None:
    """Return (group_id, topic_id) bound to this window for this user, or None."""
    row = conn.execute(
        "SELECT group_id, topic_id FROM topic_bindings "
        "WHERE user_id = ? AND window_id = ?",
        (user_id, window_id),
    ).fetchone()
    return (row["group_id"], row["topic_id"]) if row else None


def iter_thread_bindings_db(
    conn: sqlite3.Connection,
) -> Iterator[tuple[int, int, str]]:
    """Yield (user_id, topic_id, window_id) for all bindings with non-null window_id."""
    rows = conn.execute(
        "SELECT user_id, topic_id, window_id FROM topic_bindings "
        "WHERE user_id IS NOT NULL AND window_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        yield int(row["user_id"]), int(row["topic_id"]), str(row["window_id"])


def get_topic_binding(
    conn: sqlite3.Connection, group_id: int, topic_id: int
) -> TopicBinding | None:
    """Return the binding for ``(group_id, topic_id)``, or ``None``."""
    row = conn.execute(
        "SELECT * FROM topic_bindings WHERE group_id = ? AND topic_id = ?",
        (group_id, topic_id),
    ).fetchone()
    return _row_to_binding(row) if row else None


def get_binding_for_session(
    conn: sqlite3.Connection, session_id: str
) -> TopicBinding | None:
    """Return the topic binding that references ``session_id``, or ``None``."""
    row = conn.execute(
        "SELECT * FROM topic_bindings WHERE session_id = ?", (session_id,)
    ).fetchone()
    return _row_to_binding(row) if row else None


def list_topic_bindings(
    conn: sqlite3.Connection, group_id: int | None = None
) -> list[TopicBinding]:
    """Return all topic bindings, optionally filtered by ``group_id``."""
    if group_id is not None:
        rows = conn.execute(
            "SELECT * FROM topic_bindings WHERE group_id = ?", (group_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM topic_bindings").fetchall()
    return [_row_to_binding(r) for r in rows]


def delete_topic_binding(conn: sqlite3.Connection, group_id: int, topic_id: int) -> int:
    """Delete a topic binding.  Returns row count (0 or 1)."""
    cur = conn.execute(
        "DELETE FROM topic_bindings WHERE group_id = ? AND topic_id = ?",
        (group_id, topic_id),
    )
    return cur.rowcount


def update_topic_binding_title(
    conn: sqlite3.Connection, group_id: int, topic_id: int, topic_title: str
) -> int:
    """Update ``topic_title`` for an existing binding.  Returns row count (0 or 1)."""
    cur = conn.execute(
        "UPDATE topic_bindings SET topic_title = ? WHERE group_id = ? AND topic_id = ?",
        (topic_title, group_id, topic_id),
    )
    return cur.rowcount


def _row_to_binding(row: sqlite3.Row) -> TopicBinding:
    keys = row.keys() if hasattr(row, "keys") else []
    return TopicBinding(
        group_id=row["group_id"],
        topic_id=row["topic_id"],
        session_id=row["session_id"],
        topic_title=row["topic_title"],
        bound_at=row["bound_at"],
        user_id=row["user_id"] if "user_id" in keys else None,
        window_id=row["window_id"] if "window_id" in keys else None,
    )


# ---- Window modes ------------------------------------------------------------


def get_window_modes(conn: sqlite3.Connection, window_id: str) -> dict:
    """Return window mode settings as a dict, or an empty dict if not found."""
    row = conn.execute(
        "SELECT * FROM window_modes WHERE window_id = ?", (window_id,)
    ).fetchone()
    if not row:
        return {}
    return {
        "window_id": row["window_id"],
        "approval_mode": row["approval_mode"],
        "batch_mode": row["batch_mode"],
        "notification_mode": row["notification_mode"],
        "provider_name": row["provider_name"],
        "external": bool(row["external"]),
        "updated_at": row["updated_at"],
    }


def upsert_window_modes(
    conn: sqlite3.Connection,
    window_id: str,
    *,
    approval_mode: str | None = None,
    batch_mode: str | None = None,
    notification_mode: str | None = None,
    provider_name: str | None = None,
    external: bool | None = None,
) -> None:
    """Insert or update window mode settings.  Only provided kwargs are updated."""
    now = int(time.time())
    # Fetch existing row to merge
    existing = get_window_modes(conn, window_id)
    merged_approval = approval_mode if approval_mode is not None else existing.get("approval_mode", "yolo")
    merged_batch = batch_mode if batch_mode is not None else existing.get("batch_mode", "batched")
    merged_notif = notification_mode if notification_mode is not None else existing.get("notification_mode", "summary")
    merged_provider = provider_name if provider_name is not None else existing.get("provider_name", "")
    merged_external = int(external) if external is not None else int(existing.get("external", False))
    conn.execute(
        """
        INSERT INTO window_modes
            (window_id, approval_mode, batch_mode, notification_mode,
             provider_name, external, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(window_id) DO UPDATE SET
            approval_mode     = excluded.approval_mode,
            batch_mode        = excluded.batch_mode,
            notification_mode = excluded.notification_mode,
            provider_name     = excluded.provider_name,
            external          = excluded.external,
            updated_at        = excluded.updated_at
        """,
        (window_id, merged_approval, merged_batch, merged_notif,
         merged_provider, merged_external, now),
    )


def delete_window_modes(conn: sqlite3.Connection, window_id: str) -> int:
    """Delete window mode row.  Returns row count (0 or 1)."""
    cur = conn.execute("DELETE FROM window_modes WHERE window_id = ?", (window_id,))
    return cur.rowcount


def list_window_modes(conn: sqlite3.Connection) -> list[dict]:
    """Return all window_modes rows as dicts."""
    rows = conn.execute("SELECT * FROM window_modes").fetchall()
    return [
        {
            "window_id": r["window_id"],
            "approval_mode": r["approval_mode"],
            "batch_mode": r["batch_mode"],
            "notification_mode": r["notification_mode"],
            "provider_name": r["provider_name"],
            "external": bool(r["external"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


# ---- Orphaned topics ---------------------------------------------------------


def upsert_orphaned_topic(
    conn: sqlite3.Connection,
    *,
    group_id: int,
    topic_id: int,
    topic_title: str,
    first_seen: int | None = None,
) -> None:
    """Insert or update an orphaned topic entry."""
    f_seen = first_seen if first_seen is not None else int(time.time())
    conn.execute(
        """
        INSERT INTO orphaned_topics (group_id, topic_id, topic_title, first_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id, topic_id) DO UPDATE SET
            topic_title = excluded.topic_title
        """,
        (group_id, topic_id, topic_title, f_seen),
    )


def list_orphaned_topics(
    conn: sqlite3.Connection, group_id: int | None = None
) -> list[OrphanedTopic]:
    """Return all orphaned topics, optionally filtered by ``group_id``."""
    if group_id is not None:
        rows = conn.execute(
            "SELECT * FROM orphaned_topics WHERE group_id = ?", (group_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM orphaned_topics").fetchall()
    return [
        OrphanedTopic(
            group_id=r["group_id"],
            topic_id=r["topic_id"],
            topic_title=r["topic_title"],
            first_seen=r["first_seen"],
        )
        for r in rows
    ]


def delete_orphaned_topic(
    conn: sqlite3.Connection, group_id: int, topic_id: int
) -> int:
    """Delete an orphaned topic entry.  Returns row count (0 or 1)."""
    cur = conn.execute(
        "DELETE FROM orphaned_topics WHERE group_id = ? AND topic_id = ?",
        (group_id, topic_id),
    )
    return cur.rowcount


# ---- Heartbeats --------------------------------------------------------------


def record_heartbeat(
    conn: sqlite3.Connection,
    component: str,
    *,
    last_beat: int | None = None,
    details: dict | None = None,
) -> None:
    """Upsert a heartbeat for ``component``."""
    beat = last_beat if last_beat is not None else int(time.time())
    details_json = json.dumps(details) if details is not None else None
    conn.execute(
        """
        INSERT INTO heartbeats (component, last_beat, details)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            last_beat = excluded.last_beat,
            details   = excluded.details
        """,
        (component, beat, details_json),
    )


def get_heartbeat(conn: sqlite3.Connection, component: str) -> Heartbeat | None:
    """Return the heartbeat for ``component``, or ``None``."""
    row = conn.execute(
        "SELECT * FROM heartbeats WHERE component = ?", (component,)
    ).fetchone()
    return _row_to_heartbeat(row) if row else None


def list_heartbeats(conn: sqlite3.Connection) -> list[Heartbeat]:
    """Return all heartbeat rows."""
    rows = conn.execute("SELECT * FROM heartbeats").fetchall()
    return [_row_to_heartbeat(r) for r in rows]


def _row_to_heartbeat(row: sqlite3.Row) -> Heartbeat:
    details = json.loads(row["details"]) if row["details"] else None
    return Heartbeat(
        component=row["component"],
        last_beat=row["last_beat"],
        details=details,
    )


# ---- Crons -------------------------------------------------------------------


def upsert_cron(
    conn: sqlite3.Connection,
    *,
    id: int,
    name: str,
    schedule: str,
    target_window: str,
    message: str,
    enabled: bool,
    created_at: int | None = None,
    last_run: int | None = None,
    last_result: str | None = None,
    target_session_id: str | None = None,
    target_topic_id: int | None = None,
    target_group_id: int | None = None,
) -> None:
    """Insert or update a cron definition."""
    c_at = created_at if created_at is not None else int(time.time())
    conn.execute(
        """
        INSERT INTO crons
            (id, name, schedule, target_window, message, enabled,
             last_run, last_result, created_at,
             target_session_id, target_topic_id, target_group_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name              = excluded.name,
            schedule          = excluded.schedule,
            target_window     = excluded.target_window,
            message           = excluded.message,
            enabled           = excluded.enabled,
            last_run          = excluded.last_run,
            last_result       = excluded.last_result,
            target_session_id = excluded.target_session_id,
            target_topic_id   = excluded.target_topic_id,
            target_group_id   = excluded.target_group_id
        """,
        (
            id,
            name,
            schedule,
            target_window,
            message,
            int(enabled),
            last_run,
            last_result,
            c_at,
            target_session_id,
            target_topic_id,
            target_group_id,
        ),
    )


def list_crons(conn: sqlite3.Connection, enabled: bool | None = None) -> list[Cron]:
    """Return all cron rows, optionally filtered by ``enabled``."""
    if enabled is not None:
        rows = conn.execute(
            "SELECT * FROM crons WHERE enabled = ?", (int(enabled),)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM crons").fetchall()
    return [_row_to_cron(r) for r in rows]


def get_cron(conn: sqlite3.Connection, id: int) -> Cron | None:
    """Return a cron by ID, or ``None``."""
    row = conn.execute("SELECT * FROM crons WHERE id = ?", (id,)).fetchone()
    return _row_to_cron(row) if row else None


def delete_cron(conn: sqlite3.Connection, id: int) -> int:
    """Delete a cron by ID.  Returns row count (0 or 1)."""
    cur = conn.execute("DELETE FROM crons WHERE id = ?", (id,))
    return cur.rowcount


def resolve_cron_target(
    cron: "Cron", conn: sqlite3.Connection
) -> dict[str, Any] | None:
    """Resolve the dispatch target for *cron* using the priority ladder.

    Priority: target_session_id > (target_group_id, target_topic_id) > target_window (legacy).
    Returns dict with window_id/session_id/topic_id/source, or None.
    """
    # 1. Prefer target_session_id
    if cron.target_session_id:
        session = get_session(conn, cron.target_session_id)
        if session is not None:
            return {
                "window_id": session.window_id,
                "session_id": session.session_id,
                "topic_id": None,
                "source": "session_id",
            }
    # 2. Fallback to (target_group_id, target_topic_id)
    if cron.target_group_id is not None and cron.target_topic_id is not None:
        binding = get_topic_binding(conn, cron.target_group_id, cron.target_topic_id)
        if binding is not None:
            session = get_session(conn, binding.session_id)
            return {
                "window_id": session.window_id if session else None,
                "session_id": binding.session_id,
                "topic_id": cron.target_topic_id,
                "source": "topic_id",
            }
    # 3. Legacy target_window
    if cron.target_window:
        logger.warning(
            "cron %d (%r) uses deprecated target_window=%r — migrate to "
            "target_session_id or target_topic_id",
            cron.id,
            cron.name,
            cron.target_window,
        )
        return {
            "window_id": cron.target_window,
            "session_id": None,
            "topic_id": None,
            "source": "legacy_window",
        }
    return None


def _row_to_cron(row: sqlite3.Row) -> Cron:
    keys = row.keys() if hasattr(row, "keys") else []
    return Cron(
        id=row["id"],
        name=row["name"],
        schedule=row["schedule"],
        target_window=row["target_window"],
        message=row["message"],
        enabled=bool(row["enabled"]),
        last_run=row["last_run"],
        last_result=row["last_result"],
        created_at=row["created_at"],
        target_session_id=row["target_session_id"]
        if "target_session_id" in keys
        else None,
        target_topic_id=row["target_topic_id"] if "target_topic_id" in keys else None,
        target_group_id=row["target_group_id"] if "target_group_id" in keys else None,
    )


# ---- User prefs --------------------------------------------------------------


def set_pref(
    conn: sqlite3.Connection,
    scope: str,
    key: str,
    value: Any,
    *,
    scope_id: str = "",
) -> None:
    """Set a user preference, JSON-serialising ``value``."""
    conn.execute(
        """
        INSERT INTO user_prefs (scope, scope_id, key, value, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scope, scope_id, key) DO UPDATE SET
            value      = excluded.value,
            updated_at = excluded.updated_at
        """,
        (scope, scope_id, key, json.dumps(value), int(time.time())),
    )


def get_pref(
    conn: sqlite3.Connection,
    scope: str,
    key: str,
    *,
    scope_id: str = "",
    default: Any = None,
) -> Any:
    """Return a deserialised preference value, or ``default`` if absent."""
    row = conn.execute(
        "SELECT value FROM user_prefs WHERE scope = ? AND scope_id = ? AND key = ?",
        (scope, scope_id, key),
    ).fetchone()
    return json.loads(row["value"]) if row else default


def list_prefs(
    conn: sqlite3.Connection,
    scope: str,
    *,
    scope_id: str | None = None,
) -> list[tuple[str, str, Any]]:
    """Return all prefs for ``scope`` as ``(scope_id, key, value)`` triples.

    If ``scope_id`` is provided, only prefs matching that specific scope_id
    are returned.  If ``None`` (the default), all scope_ids are included.
    """
    if scope_id is not None:
        rows = conn.execute(
            "SELECT scope_id, key, value FROM user_prefs "
            "WHERE scope = ? AND scope_id = ?",
            (scope, scope_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT scope_id, key, value FROM user_prefs WHERE scope = ?",
            (scope,),
        ).fetchall()
    return [(r["scope_id"], r["key"], json.loads(r["value"])) for r in rows]


def delete_pref(
    conn: sqlite3.Connection,
    scope: str,
    key: str,
    *,
    scope_id: str = "",
) -> int:
    """Delete a preference entry.  Returns row count (0 or 1)."""
    cur = conn.execute(
        "DELETE FROM user_prefs WHERE scope = ? AND scope_id = ? AND key = ?",
        (scope, scope_id, key),
    )
    return cur.rowcount


# ---- Introspection -----------------------------------------------------------

_SQLITE_INTERNAL_PREFIXES = ("sqlite_",)


def table_names(conn: sqlite3.Connection) -> list[str]:
    """Return all user-defined table names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [
        r["name"]
        for r in rows
        if not any(r["name"].startswith(p) for p in _SQLITE_INTERNAL_PREFIXES)
    ]


def dump_all(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Return every row in every non-sqlite table as plain dicts."""
    result: dict[str, list[dict]] = {}
    for tbl in table_names(conn):
        rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()  # noqa: S608
        result[tbl] = [dict(r) for r in rows]
    return result
