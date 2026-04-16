"""SQLite state store for CCGram — single authoritative source for session/topic state.

DB location: ``ccgram_dir() / "state.db"`` (expose via :func:`db_path`).

Schema overview:
- ``sessions``       — agent sessions keyed on UUID session_id
- ``topic_bindings`` — Telegram (group_id, topic_id) → session_id mapping (1-to-1)
- ``orphaned_topics``— topics known but not yet bound to a session
- ``heartbeats``     — liveness beacons from long-running components
- ``crons``          — scheduled message definitions
- ``user_prefs``     — generic KV store for per-scope user preferences

Schema decision for prefs — single KV table instead of per-feature tables:
The data is heterogeneous (display flags, directory favorites, read offsets), rarely
queried relationally, and grows organically as new per-window/per-group features are
added. A single ``user_prefs(scope, scope_id, key, value)`` table covers all of these
without premature schema commits. If query patterns later demand relational access,
individual fields can be promoted to structured tables in a follow-up migration.

Schema versions:
  1 — initial schema (sessions, topic_bindings, orphaned_topics, heartbeats, crons, user_prefs)
  2 — crons: added target_session_id, target_topic_id, target_group_id columns
"""

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccgram.utils import ccgram_dir

logger = logging.getLogger(__name__)

# ---- Schema ------------------------------------------------------------------

_SCHEMA_VERSION = "2"

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


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations not yet present.  Idempotent."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    current = int(row[0]) if row else 1
    if current >= 2:  # noqa: PLR2004
        return
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(crons)").fetchall()}
    for col, stmt in _V2_ALTERS:
        if col not in existing_cols:
            conn.execute(stmt)
            logger.info("schema migration v2: added crons.%s", col)
    conn.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
    conn.commit()


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
        (session_id, cwd, agent, mode, status, window_id, c_at, now),
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


def _row_to_binding(row: sqlite3.Row) -> TopicBinding:
    return TopicBinding(
        group_id=row["group_id"],
        topic_id=row["topic_id"],
        session_id=row["session_id"],
        topic_title=row["topic_title"],
        bound_at=row["bound_at"],
    )


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
