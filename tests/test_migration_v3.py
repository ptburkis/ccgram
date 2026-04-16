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
            assert version[0] == "3"

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
