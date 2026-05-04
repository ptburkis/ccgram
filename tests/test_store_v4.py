"""Tests for store.py v4 migration -- partial unique index and dedupe_active_sessions."""

from __future__ import annotations

import sqlite3
import time

import pytest

from ccgram import store


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    store.init_db(path)
    return path


@pytest.fixture()
def conn(db):
    with store.connect(db) as c:
        yield c


# ---- Helpers -----------------------------------------------------------------


def _insert_session(conn, session_id, window_id, status="active", updated_at=None):
    """Low-level insert bypassing upsert_session window-steal guard."""
    now = updated_at if updated_at is not None else int(time.time())
    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (session_id, cwd, agent, status, window_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, "/cwd", "claude", status, window_id, 1000, now),
    )


# ---- v4 migration tests ------------------------------------------------------


class TestV4Migration:
    def test_v4_migration_adds_columns_idempotent(self, tmp_path):
        """Applying v4 migration twice raises no error."""
        path = tmp_path / "state.db"
        store.init_db(path)
        # init_db already ran migrations; call again explicitly
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            store._apply_v4_schema_additions(conn)
            store._apply_v4_schema_additions(conn)  # second call must be idempotent
            conn.commit()
        finally:
            conn.close()
        # Verify columns exist
        with store.connect(path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "provider_session_id" in cols
        assert "transcript_offset" in cols
        assert "transcript_path" in cols

    def test_schema_version_is_4(self, conn):
        """init_db should write schema_version=4."""
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "4"

    def test_v4_migration_creates_unique_index(self, tmp_path):
        """Partial unique index exists; inserting two active rows for same window fails."""
        path = tmp_path / "state.db"
        store.init_db(path)

        # Verify index exists in sqlite_master
        with store.connect(path) as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='idx_sessions_one_active_per_window'"
            ).fetchone()
        assert row is not None, "unique index not found"

        # Inserting a second active row for the same window_id should raise
        with store.connect(path) as c:
            _insert_session(c, "sess-A", "@10", status="active")
            # Direct insert (not via upsert which guards against stealing)
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO sessions"
                    " (session_id, cwd, agent, status, window_id, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("sess-B", "/cwd", "claude", "active", "@10", 1000, int(time.time())),
                )


# ---- dedupe_active_sessions tests --------------------------------------------


class TestDedupeActiveSessions:
    def test_dedupe_active_sessions(self, tmp_path):
        """Seed 3 rows for same window_id; most-recent kept, others retired w/ window_id=NULL."""
        path = tmp_path / "state.db"
        # Use a fresh DB but bypass the unique index so we can seed dupes
        # (the index was added in v4; we'll seed on a v3-style connection then apply v4)
        raw = sqlite3.connect(str(path))
        raw.row_factory = sqlite3.Row
        raw.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                cwd TEXT NOT NULL,
                agent TEXT NOT NULL,
                mode TEXT,
                status TEXT NOT NULL,
                window_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                provider_session_id TEXT,
                transcript_offset INTEGER NOT NULL DEFAULT 0,
                transcript_path TEXT
            );
        """)
        # Insert 3 active sessions for same window_id, different updated_at
        now = int(time.time())
        raw.execute(
            "INSERT INTO sessions (session_id, cwd, agent, status, window_id, created_at, updated_at)"
            " VALUES ('s-old', '/cwd', 'claude', 'active', '@7', 1000, ?)", (now - 100,)
        )
        raw.execute(
            "INSERT INTO sessions (session_id, cwd, agent, status, window_id, created_at, updated_at)"
            " VALUES ('s-mid', '/cwd', 'claude', 'active', '@7', 1000, ?)", (now - 50,)
        )
        raw.execute(
            "INSERT INTO sessions (session_id, cwd, agent, status, window_id, created_at, updated_at)"
            " VALUES ('s-new', '/cwd', 'claude', 'active', '@7', 1000, ?)", (now,)
        )
        raw.commit()
        raw.close()

        # Now run dedupe (uses raw connection to avoid re-triggering init_db)
        conn2 = sqlite3.connect(str(path))
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA foreign_keys=ON")
        result = store.dedupe_active_sessions(conn2)
        conn2.commit()

        # s-new should be kept, s-old and s-mid retired
        assert "s-new" in result
        retired = set(result["s-new"])
        assert "s-old" in retired
        assert "s-mid" in retired

        # Verify DB state
        rows = conn2.execute(
            "SELECT session_id, status, window_id FROM sessions ORDER BY session_id"
        ).fetchall()
        state = {r["session_id"]: (r["status"], r["window_id"]) for r in rows}
        assert state["s-new"] == ("active", "@7")
        assert state["s-old"][0] == "retired"
        assert state["s-old"][1] is None  # window_id cleared
        assert state["s-mid"][0] == "retired"
        assert state["s-mid"][1] is None
        conn2.close()

    def test_dedupe_no_op_when_clean(self, conn):
        """dedupe_active_sessions is a no-op when no duplicates exist."""
        _insert_session(conn, "sess-1", "@1")
        _insert_session(conn, "sess-2", "@2")
        result = store.dedupe_active_sessions(conn)
        assert result == {}
