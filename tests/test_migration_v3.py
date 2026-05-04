"""Tests for CCGram Phase 4 state.json → DB migration (migration v3).

Tests the migrate_to_v3() function and the _apply_v3_schema_additions()
path that runs automatically on init_db() against a v2 database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from ccgram import store


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


def _make_state_json(
    tmp_path: Path,
    *,
    thread_bindings: dict | None = None,
    window_states: dict | None = None,
    group_chat_ids: dict | None = None,
    window_display_names: dict | None = None,
    user_window_offsets: dict | None = None,
    user_dir_favorites: dict | None = None,
) -> Path:
    """Write a synthetic state.json to tmp_path and return the path."""
    data = {
        "thread_bindings": thread_bindings or {},
        "window_states": window_states or {},
        "group_chat_ids": group_chat_ids or {},
        "window_display_names": window_display_names or {},
        "user_window_offsets": user_window_offsets or {},
        "user_dir_favorites": user_dir_favorites or {},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    return p


# ---- Schema v3 auto-migration tests -----------------------------------------


class TestSchemaV3Migration:
    """Verify that a v2 database gets v3 additions on init_db()."""

    def test_v2_db_gets_v3_columns(self, tmp_path):
        """A v2 DB gets user_id and window_id added to topic_bindings."""
        db = tmp_path / "v2.db"
        # Build a minimal v2 DB
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL,
                agent TEXT NOT NULL, mode TEXT, status TEXT NOT NULL, window_id TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE topic_bindings (group_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL, session_id TEXT NOT NULL UNIQUE,
                topic_title TEXT NOT NULL, bound_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, topic_id));
            CREATE TABLE orphaned_topics (group_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL, topic_title TEXT NOT NULL,
                first_seen INTEGER NOT NULL, PRIMARY KEY (group_id, topic_id));
            CREATE TABLE heartbeats (component TEXT PRIMARY KEY,
                last_beat INTEGER NOT NULL, details TEXT);
            CREATE TABLE crons (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                schedule TEXT NOT NULL, target_window TEXT NOT NULL,
                message TEXT NOT NULL, enabled INTEGER NOT NULL,
                last_run INTEGER, last_result TEXT, created_at INTEGER NOT NULL,
                target_session_id TEXT, target_topic_id INTEGER,
                target_group_id INTEGER);
            CREATE TABLE user_prefs (scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '', key TEXT NOT NULL,
                value TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY (scope, scope_id, key));
            INSERT INTO schema_meta VALUES ('schema_version', '2');
        """)
        conn.close()

        store.init_db(db)

        with store.connect(db) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(topic_bindings)").fetchall()}
            assert "user_id" in cols
            assert "window_id" in cols

            tables = store.table_names(c)
            assert "window_modes" in tables

            version = c.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            assert version[0] == "4"  # v2 gets fully migrated to current version

    def test_v3_migration_window_id_populated_from_sessions(self, tmp_path):
        """Migration populates window_id from sessions table."""
        db = tmp_path / "v2.db"
        conn = sqlite3.connect(str(db))
        now = int(time.time())
        conn.executescript(f"""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL,
                agent TEXT NOT NULL, mode TEXT, status TEXT NOT NULL, window_id TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE topic_bindings (group_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL, session_id TEXT NOT NULL UNIQUE,
                topic_title TEXT NOT NULL, bound_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, topic_id));
            CREATE TABLE orphaned_topics (group_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL, topic_title TEXT NOT NULL,
                first_seen INTEGER NOT NULL, PRIMARY KEY (group_id, topic_id));
            CREATE TABLE heartbeats (component TEXT PRIMARY KEY,
                last_beat INTEGER NOT NULL, details TEXT);
            CREATE TABLE crons (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                schedule TEXT NOT NULL, target_window TEXT NOT NULL,
                message TEXT NOT NULL, enabled INTEGER NOT NULL,
                last_run INTEGER, last_result TEXT, created_at INTEGER NOT NULL,
                target_session_id TEXT, target_topic_id INTEGER,
                target_group_id INTEGER);
            CREATE TABLE user_prefs (scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '', key TEXT NOT NULL,
                value TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY (scope, scope_id, key));
            INSERT INTO schema_meta VALUES ('schema_version', '2');
            INSERT INTO sessions VALUES ('sid-1', '/cwd', 'claude', NULL, 'active', '@5', {now}, {now});
            INSERT INTO topic_bindings VALUES (-100, 10, 'sid-1', 'mytopic', {now});
        """)
        conn.close()

        store.init_db(db)

        with store.connect(db) as c:
            row = c.execute("SELECT window_id FROM topic_bindings WHERE topic_id=10").fetchone()
            assert row["window_id"] == "@5"

    def test_tables_and_indexes_exist(self, db, conn):
        """v3 DB has required tables and indexes."""
        names = store.table_names(conn)
        assert "window_modes" in names
        assert "topic_bindings" in names

        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        idx_names = {r["name"] for r in idx_rows}
        assert "idx_topic_bindings_user_topic" in idx_names
        assert "idx_topic_bindings_window" in idx_names


# ---- migrate_to_v3 tests -----------------------------------------------------


class TestMigrateToV3:
    """Tests for store.migrate_to_v3() standalone migration function."""

    def test_missing_state_json_is_no_op(self, tmp_path):
        """migrate_to_v3 with missing state.json returns empty summary."""
        db = tmp_path / "state.db"
        store.init_db(db)
        summary = store.migrate_to_v3(db, tmp_path / "nonexistent.json")
        assert summary["user_ids_set"] == 0
        assert summary["window_ids_set"] == 0

    def test_thread_bindings_populate_user_id_and_window_id(self, tmp_path):
        """thread_bindings in state.json → topic_bindings.user_id + window_id."""
        db = tmp_path / "state.db"
        now = int(time.time())
        store.init_db(db)
        # Seed a session + topic_binding without user_id/window_id
        with store.connect(db) as c:
            store.upsert_session(c, session_id="sid-1", cwd="/c", agent="claude",
                                 status="active", window_id="@7", created_at=now)
            store.upsert_topic_binding(c, group_id=-100, topic_id=42,
                                       session_id="sid-1", topic_title="T", bound_at=now)

        state_json = _make_state_json(
            tmp_path,
            thread_bindings={"1234": {"42": "@7"}},
        )
        summary = store.migrate_to_v3(db, state_json)
        assert summary["user_ids_set"] > 0

        with store.connect(db) as c:
            b = store.get_topic_binding(c, -100, 42)
        assert b is not None
        assert b.user_id == 1234
        assert b.window_id == "@7"

    def test_window_states_migrate_to_window_modes(self, tmp_path):
        """window_states in state.json → window_modes table rows."""
        db = tmp_path / "state.db"
        store.init_db(db)
        state_json = _make_state_json(
            tmp_path,
            window_states={
                "@1": {
                    "session_id": "sid-1",
                    "cwd": "/c",
                    "approval_mode": "normal",
                    "batch_mode": "verbose",
                    "notification_mode": "all",
                    "provider_name": "claude",
                    "external": False,
                },
                "@2": {
                    "session_id": "sid-2",
                    "cwd": "/d",
                },
            },
        )
        summary = store.migrate_to_v3(db, state_json)
        assert summary["window_modes_inserted"] == 2

        with store.connect(db) as c:
            modes = store.list_window_modes(c)
        wids = {m["window_id"] for m in modes}
        assert "@1" in wids
        assert "@2" in wids
        m1 = next(m for m in modes if m["window_id"] == "@1")
        assert m1["approval_mode"] == "normal"
        assert m1["batch_mode"] == "verbose"
        assert m1["notification_mode"] == "all"

    def test_legacy_notification_mode_collapsed(self, tmp_path):
        """Legacy 'errors_only'/'muted' modes are collapsed to 'summary'."""
        db = tmp_path / "state.db"
        store.init_db(db)
        state_json = _make_state_json(
            tmp_path,
            window_states={
                "@3": {"session_id": "", "cwd": "", "notification_mode": "errors_only"},
                "@4": {"session_id": "", "cwd": "", "notification_mode": "muted"},
            },
        )
        store.migrate_to_v3(db, state_json)

        with store.connect(db) as c:
            modes = {m["window_id"]: m for m in store.list_window_modes(c)}
        assert modes["@3"]["notification_mode"] == "summary"
        assert modes["@4"]["notification_mode"] == "summary"

    def test_group_chat_ids_migrate_to_user_prefs(self, tmp_path):
        """group_chat_ids in state.json → user_prefs(scope='group_chat')."""
        db = tmp_path / "state.db"
        store.init_db(db)
        state_json = _make_state_json(
            tmp_path,
            group_chat_ids={
                "1234:10": -9999001,
                "1234:11": -9999002,
            },
        )
        summary = store.migrate_to_v3(db, state_json)
        assert summary["group_chat_prefs_inserted"] == 2

        with store.connect(db) as c:
            prefs = store.list_prefs(c, "group_chat")
        keys = {p[1] for p in prefs}
        assert "1234:10" in keys
        assert "1234:11" in keys

    def test_window_display_names_migrate_to_user_prefs(self, tmp_path):
        """window_display_names → user_prefs(scope='window_name')."""
        db = tmp_path / "state.db"
        store.init_db(db)
        state_json = _make_state_json(
            tmp_path,
            window_display_names={"@1": "proj-alpha", "@2": "proj-beta"},
        )
        summary = store.migrate_to_v3(db, state_json)
        assert summary["display_name_prefs_inserted"] == 2

        with store.connect(db) as c:
            prefs = store.list_prefs(c, "window_name")
        wids = {p[0] for p in prefs}
        assert "@1" in wids
        assert "@2" in wids

    def test_migration_is_idempotent(self, tmp_path):
        """Running migrate_to_v3 twice doesn't duplicate data."""
        db = tmp_path / "state.db"
        store.init_db(db)
        state_json = _make_state_json(
            tmp_path,
            window_states={"@1": {"session_id": "", "cwd": ""}},
            group_chat_ids={"1234:10": -9999},
            window_display_names={"@1": "myproj"},
        )
        store.migrate_to_v3(db, state_json)
        store.migrate_to_v3(db, state_json)

        with store.connect(db) as c:
            modes = store.list_window_modes(c)
            gchat = store.list_prefs(c, "group_chat")
            names = store.list_prefs(c, "window_name")

        assert len(modes) == 1
        assert len(gchat) == 1
        assert len(names) == 1

    def test_marker_data_preferred_over_thread_bindings(self, tmp_path):
        """Bug 1: PTY marker window_id/session_id wins over stale thread_bindings.

        Seed:
          - state.json thread_bindings maps topic_id=99 to stale '@160'
          - A PTY marker file for 'paint-my-room' with window_id='@7', session_id='live-sess'
          - A topic_binding row with topic_id=99, topic_title='paint-my-room'

        After migration the binding must have window_id='@7' and session_id='live-sess',
        not the stale '@160' from thread_bindings.
        """
        db = tmp_path / "state.db"
        now = int(time.time())
        store.init_db(db)

        with store.connect(db) as c:
            store.upsert_session(c, session_id="old-sess", cwd="/c", agent="claude",
                                 status="active", window_id="@160", created_at=now)
            store.upsert_topic_binding(c, group_id=-100, topic_id=99,
                                       session_id="old-sess", topic_title="paint-my-room",
                                       bound_at=now)

        # Write a PTY marker file that reflects the live window (overrides stale data)
        markers_dir = tmp_path / "active-sessions"
        markers_dir.mkdir()
        marker_data = {
            "pty": "/dev/pts/5",
            "window_id": "@7",
            "window_name": "paint-my-room",
            "pid": 12345,
            "provider": "claude",
            "session_id": "live-sess",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/home/peter",
            "last_seen_at": now,
        }
        (markers_dir / "pts5.json").write_text(json.dumps(marker_data))

        state_json = _make_state_json(
            tmp_path,
            # Stale thread_bindings say @160 — must be overridden by marker
            thread_bindings={"1234": {"99": "@160"}},
        )
        summary = store.migrate_to_v3(db, state_json, markers_dir=markers_dir)

        with store.connect(db) as c:
            b = store.get_topic_binding(c, -100, 99)
        assert b is not None, "topic_binding row not found"
        assert b.window_id == "@7", (
            f"Expected @7 (from marker), got {b.window_id!r} (stale @160 from thread_bindings)"
        )
        assert b.session_id == "live-sess", (
            f"Expected live-sess (from marker), got {b.session_id!r}"
        )
        assert summary["window_ids_set"] >= 1

    def test_live_bot_backfill_uses_explicit_allowed_users(self, tmp_path):
        """Bug 2: passing allowed_users explicitly (live-bot path) backfills NULL rows.

        Simulates the live bot path where config.allowed_users is a set — previously
        the env-var-only path would hit the 'ambiguous' branch when multiple entries
        were set and skip the backfill.  With allowed_users={single_id} passed
        explicitly the backfill must run regardless of the env var.
        """
        db = tmp_path / "state.db"
        now = int(time.time())
        store.init_db(db)

        # Seed 5 topic_bindings with user_id=NULL (simulating topics not in thread_bindings)
        raw_conn = sqlite3.connect(str(db))
        try:
            for i in range(5):
                sid = f"bot-sess-{i}"
                raw_conn.execute(
                    "INSERT INTO sessions (session_id, cwd, agent, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, "/tmp", "claude", "active", now, now),
                )
                raw_conn.execute(
                    "INSERT INTO topic_bindings (group_id, topic_id, session_id, topic_title, bound_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (-100, 200 + i, sid, f"Bot Topic {i}", now),
                )
            raw_conn.commit()
        finally:
            raw_conn.close()

        state_json = _make_state_json(tmp_path)  # empty thread_bindings

        # Live bot path: pass allowed_users explicitly as a single-element set.
        # Pre-fix: env-var path with ALLOWED_USERS=111,222 would hit 'ambiguous' and skip.
        summary = store.migrate_to_v3(db, state_json, allowed_users={777777})

        assert summary["user_ids_set"] == 5, (
            f"Expected backfill to set 5 user_ids, got {summary['user_ids_set']}"
        )
        raw_conn2 = sqlite3.connect(str(db))
        try:
            rows = raw_conn2.execute(
                "SELECT user_id FROM topic_bindings WHERE group_id=-100"
            ).fetchall()
            assert all(r[0] == 777777 for r in rows), "Some user_ids not backfilled"
            null_count = raw_conn2.execute(
                "SELECT COUNT(*) FROM topic_bindings WHERE user_id IS NULL"
            ).fetchone()[0]
            assert null_count == 0, f"{null_count} rows still have user_id=NULL"
        finally:
            raw_conn2.close()
