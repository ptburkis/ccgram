"""Tests for ccgram.store — SQLite state store.

Covers: schema creation, CRUD for all tables, FK cascades, uniqueness
constraints, and migration from synthetic JSON fixtures.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

from ccgram import store


# ---- Fixtures ----------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "ccgram-state"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    store.init_db(path)
    return path


@pytest.fixture()
def conn(db):
    with store.connect(db) as c:
        yield c


# ---- TestSchema --------------------------------------------------------------


class TestSchema:
    def test_init_creates_file(self, tmp_path):
        path = tmp_path / "state.db"
        assert not path.exists()
        store.init_db(path)
        assert path.exists()

    def test_init_is_idempotent(self, tmp_path):
        path = tmp_path / "state.db"
        store.init_db(path)
        store.init_db(path)  # second call must not raise or corrupt
        with store.connect(path) as c:
            row = c.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        assert row is not None

    def test_tables_exist(self, conn):
        names = store.table_names(conn)
        required = {
            "sessions", "topic_bindings", "orphaned_topics",
            "heartbeats", "crons", "user_prefs", "schema_meta",
        }
        assert required <= set(names)

    def test_indices_exist(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        index_names = {r["name"] for r in rows}
        required = {
            "idx_sessions_window_id",
            "idx_topic_bindings_session",
            "idx_crons_enabled",
            "idx_user_prefs_scope",
        }
        assert required <= index_names

    def test_foreign_keys_enabled(self, conn):
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_wal_mode(self, conn):
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0].lower() == "wal"

    def test_status_check_constraint(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions "
                "(session_id,cwd,agent,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                ("s1", "/cwd", "claude", "bogus", 0, 0),
            )


# ---- TestSessions ------------------------------------------------------------


class TestSessions:
    def _insert(self, conn, suffix="1", status="active", window_id="@1"):
        store.upsert_session(
            conn,
            session_id=f"sess-{suffix}",
            cwd=f"/cwd-{suffix}",
            agent="claude",
            status=status,
            window_id=window_id,
            created_at=1000,
        )

    def test_upsert_get_roundtrip(self, conn):
        self._insert(conn)
        s = store.get_session(conn, "sess-1")
        assert s is not None
        assert s.session_id == "sess-1"
        assert s.cwd == "/cwd-1"
        assert s.agent == "claude"
        assert s.status == "active"
        assert s.window_id == "@1"
        assert s.created_at == 1000

    def test_upsert_preserves_created_at(self, conn):
        self._insert(conn)
        s1 = store.get_session(conn, "sess-1")
        import time
        time.sleep(0.01)
        store.upsert_session(
            conn, session_id="sess-1", cwd="/cwd-new", agent="codex",
            status="retired", window_id=None, created_at=999,
        )
        s2 = store.get_session(conn, "sess-1")
        assert s2 is not None
        assert s2.cwd == "/cwd-new"
        assert s2.created_at == s1.created_at  # preserved on re-upsert
        assert s2.updated_at >= s1.updated_at

    def test_list_sessions_filter(self, conn):
        self._insert(conn, "a", status="active", window_id="@10")
        self._insert(conn, "b", status="retired", window_id=None)
        active = store.list_sessions(conn, status="active")
        assert len(active) == 1
        assert active[0].session_id == "sess-a"

    def test_get_session_by_window_most_recent(self, conn):
        conn.execute(
            "INSERT INTO sessions (session_id,cwd,agent,status,window_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("old", "/c", "claude", "retired", "@9", 1, 1000),
        )
        conn.execute(
            "INSERT INTO sessions (session_id,cwd,agent,status,window_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("new", "/c", "claude", "active", "@9", 2, 2000),
        )
        s = store.get_session_by_window(conn, "@9")
        assert s is not None
        assert s.session_id == "new"

    def test_delete_session(self, conn):
        self._insert(conn)
        count = store.delete_session(conn, "sess-1")
        assert count == 1
        assert store.get_session(conn, "sess-1") is None


# ---- TestTopicBindings -------------------------------------------------------


class TestTopicBindings:
    def _session(self, conn, sid, window_id):
        store.upsert_session(conn, session_id=sid, cwd="/c", agent="claude",
                             status="active", window_id=window_id, created_at=0)

    def test_unique_session_id_violation(self, conn):
        self._session(conn, "s1", "@1")
        self._session(conn, "s2", "@2")
        store.upsert_topic_binding(conn, group_id=1, topic_id=10,
                                   session_id="s1", topic_title="T1")
        # Same session_id in a different PK row — UNIQUE constraint fires
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO topic_bindings "
                "(group_id,topic_id,session_id,topic_title,bound_at) "
                "VALUES (?,?,?,?,?)",
                (1, 20, "s1", "T2", 0),
            )

    def test_pk_conflict_updates_in_place(self, conn):
        self._session(conn, "s1", "@1")
        self._session(conn, "s2", "@2")
        store.upsert_topic_binding(conn, group_id=1, topic_id=10,
                                   session_id="s1", topic_title="Old")
        store.upsert_topic_binding(conn, group_id=1, topic_id=10,
                                   session_id="s2", topic_title="New")
        rows = conn.execute("SELECT * FROM topic_bindings").fetchall()
        assert len(rows) == 1
        assert rows[0]["topic_title"] == "New"

    def test_fk_cascade_on_session_delete(self, conn):
        self._session(conn, "s1", "@1")
        store.upsert_topic_binding(conn, group_id=1, topic_id=10,
                                   session_id="s1", topic_title="T1")
        store.delete_session(conn, "s1")
        assert store.get_topic_binding(conn, 1, 10) is None

    def test_orphan_session_fk_rejected(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO topic_bindings "
                "(group_id,topic_id,session_id,topic_title,bound_at) "
                "VALUES (?,?,?,?,?)",
                (1, 10, "ghost-session", "Ghost", 0),
            )


# ---- TestOrphanedTopics ------------------------------------------------------


class TestOrphanedTopics:
    def test_upsert_list_delete(self, conn):
        store.upsert_orphaned_topic(conn, group_id=5, topic_id=99,
                                    topic_title="Orphan", first_seen=100)
        rows = store.list_orphaned_topics(conn)
        assert len(rows) == 1
        assert rows[0].topic_title == "Orphan"

        n = store.delete_orphaned_topic(conn, 5, 99)
        assert n == 1
        assert store.list_orphaned_topics(conn) == []

    def test_filter_by_group(self, conn):
        store.upsert_orphaned_topic(conn, group_id=1, topic_id=1,
                                    topic_title="A", first_seen=0)
        store.upsert_orphaned_topic(conn, group_id=2, topic_id=2,
                                    topic_title="B", first_seen=0)
        assert len(store.list_orphaned_topics(conn, group_id=1)) == 1
        assert len(store.list_orphaned_topics(conn, group_id=2)) == 1


# ---- TestHeartbeats ----------------------------------------------------------


class TestHeartbeats:
    def test_record_get_roundtrip_with_details(self, conn):
        details = {"status": "OK", "wid": "@5"}
        store.record_heartbeat(conn, "test-comp", last_beat=12345, details=details)
        h = store.get_heartbeat(conn, "test-comp")
        assert h is not None
        assert h.component == "test-comp"
        assert h.last_beat == 12345
        assert h.details == details

    def test_overwrite_by_component(self, conn):
        store.record_heartbeat(conn, "c1", last_beat=1, details={"x": 1})
        store.record_heartbeat(conn, "c1", last_beat=2, details={"x": 2})
        h = store.get_heartbeat(conn, "c1")
        assert h.last_beat == 2
        assert h.details == {"x": 2}

    def test_list_heartbeats(self, conn):
        store.record_heartbeat(conn, "a")
        store.record_heartbeat(conn, "b")
        assert len(store.list_heartbeats(conn)) == 2


# ---- TestCrons ---------------------------------------------------------------


class TestCrons:
    def _cron(self, conn, cid=1, enabled=True):
        store.upsert_cron(
            conn, id=cid, name=f"Cron-{cid}", schedule="0 * * * *",
            target_window="@1", message="hello", enabled=enabled,
            created_at=1000,
        )

    def test_upsert_get(self, conn):
        self._cron(conn, 1)
        c = store.get_cron(conn, 1)
        assert c is not None
        assert c.name == "Cron-1"
        assert c.enabled is True

    def test_list_filter_enabled(self, conn):
        self._cron(conn, 1, enabled=True)
        self._cron(conn, 2, enabled=False)
        enabled = store.list_crons(conn, enabled=True)
        disabled = store.list_crons(conn, enabled=False)
        all_crons = store.list_crons(conn)
        assert len(enabled) == 1
        assert len(disabled) == 1
        assert len(all_crons) == 2

    def test_delete(self, conn):
        self._cron(conn)
        n = store.delete_cron(conn, 1)
        assert n == 1
        assert store.get_cron(conn, 1) is None


# ---- TestUserPrefs -----------------------------------------------------------


class TestUserPrefs:
    def test_set_get_dict_value(self, conn):
        val = {"a": 1, "b": [1, 2, 3]}
        store.set_pref(conn, "test", "mykey", val)
        result = store.get_pref(conn, "test", "mykey")
        assert result == val

    def test_different_scope_ids(self, conn):
        store.set_pref(conn, "group", "k", "v1", scope_id="100")
        store.set_pref(conn, "group", "k", "v2", scope_id="200")
        assert store.get_pref(conn, "group", "k", scope_id="100") == "v1"
        assert store.get_pref(conn, "group", "k", scope_id="200") == "v2"

    def test_default_returned_when_absent(self, conn):
        result = store.get_pref(conn, "scope", "missing", default="fallback")
        assert result == "fallback"

    def test_list_prefs_all_scope_ids(self, conn):
        store.set_pref(conn, "window", "display_name", "Alpha", scope_id="@1")
        store.set_pref(conn, "window", "display_name", "Beta", scope_id="@2")
        rows = store.list_prefs(conn, "window")
        assert len(rows) == 2

    def test_list_prefs_narrow_scope_id(self, conn):
        store.set_pref(conn, "window", "display_name", "Alpha", scope_id="@1")
        store.set_pref(conn, "window", "display_name", "Beta", scope_id="@2")
        rows = store.list_prefs(conn, "window", scope_id="@1")
        assert len(rows) == 1
        assert rows[0][2] == "Alpha"

    def test_delete_pref(self, conn):
        store.set_pref(conn, "window", "k", "v", scope_id="@1")
        n = store.delete_pref(conn, "window", "k", scope_id="@1")
        assert n == 1
        assert store.get_pref(conn, "window", "k", scope_id="@1") is None


# ---- TestMigration -----------------------------------------------------------


def _add_scripts_to_path():
    scripts_dir = Path(__file__).parent.parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _import_migrate():
    _add_scripts_to_path()
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migrate_to_sqlite",
        Path(__file__).parent.parent / "scripts" / "migrate_to_sqlite.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration:
    @pytest.fixture()
    def mig(self):
        return _import_migrate()

    @pytest.fixture()
    def migrated_db(self, tmp_path, mig):
        db_path = tmp_path / "state.db"
        mig.migrate(FIXTURES, db_path, dry_run=False, verbose=False)
        return db_path

    def test_migration_populates_db(self, migrated_db):
        with store.connect(migrated_db) as c:
            sessions = store.list_sessions(c)
            assert len(sessions) >= 2
            s = store.get_session(c, "aaa00000-0000-0000-0000-000000000001")
            assert s is not None
            assert s.cwd == "/home/user/project-alpha"
            assert s.window_id == "@1"

            bindings = store.list_topic_bindings(c)
            assert len(bindings) >= 1

            crons = store.list_crons(c)
            assert len(crons) >= 1
            assert crons[0].name == "Nightly Sync"

            heartbeats = store.list_heartbeats(c)
            assert len(heartbeats) >= 1

            # user_prefs should have been populated
            prefs = store.list_prefs(c, "window")
            assert len(prefs) > 0

    def test_migration_is_idempotent(self, tmp_path, mig):
        db_path = tmp_path / "state.db"
        mig.migrate(FIXTURES, db_path, dry_run=False, verbose=False)

        with store.connect(db_path) as c:
            snap1 = store.dump_all(c)

        mig.migrate(FIXTURES, db_path, dry_run=False, verbose=False)

        with store.connect(db_path) as c:
            snap2 = store.dump_all(c)

        # Compare table by table ignoring updated_at timestamps on user_prefs
        for tbl in snap1:
            if tbl == "user_prefs":
                rows1 = sorted(
                    [{k: v for k, v in r.items() if k != "updated_at"} for r in snap1[tbl]],
                    key=lambda r: (r["scope"], r["scope_id"], r["key"]),
                )
                rows2 = sorted(
                    [{k: v for k, v in r.items() if k != "updated_at"} for r in snap2[tbl]],
                    key=lambda r: (r["scope"], r["scope_id"], r["key"]),
                )
                assert rows1 == rows2, f"Table {tbl} differs after second migration"
            else:
                assert snap1[tbl] == snap2[tbl], f"Table {tbl} differs after second migration"

    def test_migration_duplicate_session_binding_skipped(self, tmp_path, mig, caplog):
        fixture_dir = tmp_path / "fixtures"
        fixture_dir.mkdir()

        session_map = {
            "ccgram:@10": {
                "session_id": "dup-session-0000-0000-0000000000",
                "cwd": "/dup",
                "window_name": "dup",
                "provider_name": "claude",
            }
        }
        state = {
            "window_states": {},
            "thread_bindings": {
                "200": {
                    "20": "@10",
                    "21": "@10",
                }
            },
            "window_display_names": {"@10": "dup"},
            "group_chat_ids": {},
            "user_window_offsets": {},
            "user_dir_favorites": {},
        }
        (fixture_dir / "session_map.json").write_text(json.dumps(session_map))
        (fixture_dir / "state.json").write_text(json.dumps(state))

        db_path = tmp_path / "dup.db"
        with caplog.at_level(logging.WARNING, logger="migrate_to_sqlite"):
            mig.migrate(fixture_dir, db_path, dry_run=False, verbose=False)

        with store.connect(db_path) as c:
            bindings = store.list_topic_bindings(c)
        assert len(bindings) == 1
        assert any("DUPLICATE" in r.message for r in caplog.records)

    def test_migration_topic_binding_uses_real_chat_id(self, migrated_db):
        with store.connect(migrated_db) as c:
            bindings = store.list_topic_bindings(c)
        assert len(bindings) >= 1
        assert bindings[0].group_id == 9876543210
        assert bindings[0].topic_id == 10

    def test_migration_dry_run_writes_nothing(self, tmp_path, mig):
        db_path = tmp_path / "dry.db"
        mig.migrate(FIXTURES, db_path, dry_run=True, verbose=False)

        with store.connect(db_path) as c:
            sessions = store.list_sessions(c)
            crons = store.list_crons(c)
        assert sessions == []
        assert crons == []


# ---- TestSchemaV2Migration ---------------------------------------------------


class TestSchemaV2Migration:
    """Verify that a v1 database gets migrated to v2 on init_db()."""

    def test_v1_db_gets_new_columns(self, tmp_path):
        db = tmp_path / "v1.db"
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
                last_run INTEGER, last_result TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE user_prefs (scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '', key TEXT NOT NULL,
                value TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY (scope, scope_id, key));
            INSERT INTO schema_meta VALUES ('schema_version', '1');
        """)
        conn.close()

        store.init_db(db)

        with store.connect(db) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(crons)").fetchall()}
            assert "target_session_id" in cols
            assert "target_topic_id" in cols
            assert "target_group_id" in cols
            version = c.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            assert version[0] == "2"

    def test_migration_is_idempotent(self, tmp_path):
        db = tmp_path / "v2.db"
        store.init_db(db)
        store.init_db(db)
        with store.connect(db) as c:
            version = c.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            assert version[0] == "2"


# ---- TestCronV2Fields --------------------------------------------------------


class TestCronV2Fields:
    def test_upsert_and_retrieve_new_fields(self, conn):
        store.upsert_cron(
            conn, id=99, name="Test", schedule="0 * * * *",
            target_window="old-window", message="msg", enabled=True,
            created_at=0,
            target_session_id="sess-abc",
            target_topic_id=42,
            target_group_id=-1001234,
        )
        c = store.get_cron(conn, 99)
        assert c is not None
        assert c.target_session_id == "sess-abc"
        assert c.target_topic_id == 42
        assert c.target_group_id == -1001234

    def test_new_fields_default_to_none(self, conn):
        store.upsert_cron(
            conn, id=100, name="Legacy", schedule="0 * * * *",
            target_window="win", message="msg", enabled=True, created_at=0,
        )
        c = store.get_cron(conn, 100)
        assert c is not None
        assert c.target_session_id is None
        assert c.target_topic_id is None
        assert c.target_group_id is None


# ---- TestResolveCronTarget ---------------------------------------------------


class TestResolveCronTarget:
    def _session(self, conn, sid, window_id):
        store.upsert_session(conn, session_id=sid, cwd="/c", agent="claude",
                             status="active", window_id=window_id, created_at=0)

    def _binding(self, conn, sid, group_id, topic_id):
        store.upsert_topic_binding(conn, group_id=group_id, topic_id=topic_id,
                                   session_id=sid, topic_title="T", bound_at=0)

    def _cron(self, **kwargs) -> store.Cron:
        defaults = dict(
            id=1, name="t", schedule="*", target_window="", message="m",
            enabled=True, last_run=None, last_result=None, created_at=0,
            target_session_id=None, target_topic_id=None, target_group_id=None,
        )
        defaults.update(kwargs)
        return store.Cron(**defaults)

    def test_resolves_via_session_id(self, conn):
        self._session(conn, "s1", "@1")
        cron = self._cron(target_session_id="s1")
        result = store.resolve_cron_target(cron, conn)
        assert result is not None
        assert result["source"] == "session_id"
        assert result["window_id"] == "@1"
        assert result["session_id"] == "s1"

    def test_resolves_via_topic_id(self, conn):
        self._session(conn, "s2", "@2")
        self._binding(conn, "s2", 9, 55)
        cron = self._cron(target_group_id=9, target_topic_id=55)
        result = store.resolve_cron_target(cron, conn)
        assert result is not None
        assert result["source"] == "topic_id"
        assert result["window_id"] == "@2"
        assert result["topic_id"] == 55

    def test_session_id_wins_over_topic_id(self, conn):
        self._session(conn, "s1", "@1")
        self._session(conn, "s2", "@2")
        self._binding(conn, "s2", 9, 55)
        cron = self._cron(target_session_id="s1", target_group_id=9, target_topic_id=55)
        result = store.resolve_cron_target(cron, conn)
        assert result["source"] == "session_id"
        assert result["window_id"] == "@1"

    def test_resolves_via_legacy_window(self, conn):
        cron = self._cron(target_window="my-win")
        result = store.resolve_cron_target(cron, conn)
        assert result is not None
        assert result["source"] == "legacy_window"
        assert result["window_id"] == "my-win"

    def test_returns_none_when_nothing_resolves(self, conn):
        cron = self._cron(target_window="")
        result = store.resolve_cron_target(cron, conn)
        assert result is None
