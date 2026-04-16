"""Tests for migrate_to_v3 backfill of user_id=NULL topic_bindings."""

import json
import sqlite3
import time

import pytest

from ccgram.store import init_db, migrate_to_v3


def _seed_null_topic_bindings(db_path, count=3):
    """Insert `count` topic_binding rows with user_id IS NULL after init_db."""
    conn = sqlite3.connect(str(db_path))
    try:
        # Need a session row for FK constraint
        now = int(time.time())
        for i in range(count):
            sid = f"sess-{i:04d}"
            conn.execute(
                "INSERT INTO sessions (session_id, cwd, agent, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, "/tmp", "claude", "active", now, now),
            )
            conn.execute(
                "INSERT INTO topic_bindings (group_id, topic_id, session_id, topic_title, bound_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (-100, 100 + i, sid, f"Topic {i}", now),
            )
        conn.commit()
    finally:
        conn.close()


def _count_null_user_ids(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM topic_bindings WHERE user_id IS NULL"
        ).fetchone()
        return row[0]
    finally:
        conn.close()


@pytest.fixture
def empty_state_json(tmp_path):
    """Minimal valid state.json — empty dict."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({}))
    return p


class TestBackfillNullUserIds:
    def test_null_rows_backfilled_single_admin(
        self, tmp_path, empty_state_json, monkeypatch
    ):
        db = tmp_path / "state.db"
        init_db(db)
        _seed_null_topic_bindings(db, count=3)
        assert _count_null_user_ids(db) == 3

        monkeypatch.setenv("ALLOWED_USERS", "111111")
        summary = migrate_to_v3(db, empty_state_json)

        assert _count_null_user_ids(db) == 0
        assert summary["user_ids_set"] == 3

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT user_id FROM topic_bindings"
            ).fetchall()
            assert all(r[0] == 111111 for r in rows)
        finally:
            conn.close()

    def test_null_rows_stay_null_no_allowed_users(
        self, tmp_path, empty_state_json, monkeypatch
    ):
        db = tmp_path / "state.db"
        init_db(db)
        _seed_null_topic_bindings(db, count=3)

        monkeypatch.setenv("ALLOWED_USERS", "")
        migrate_to_v3(db, empty_state_json)

        assert _count_null_user_ids(db) == 3

    def test_null_rows_stay_null_multiple_allowed_users(
        self, tmp_path, empty_state_json, monkeypatch
    ):
        db = tmp_path / "state.db"
        init_db(db)
        _seed_null_topic_bindings(db, count=3)

        monkeypatch.setenv("ALLOWED_USERS", "111111,222222")
        migrate_to_v3(db, empty_state_json)

        assert _count_null_user_ids(db) == 3
