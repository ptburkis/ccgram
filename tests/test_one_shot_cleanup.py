"""Tests for scripts/one_shot_cleanup.py — dry-run and apply semantics."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


# ---- Load the cleanup module via importlib -----------------------------------


def _load_cleanup():
    scripts_dir = Path(__file__).parent.parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "one_shot_cleanup", scripts_dir / "one_shot_cleanup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cleanup_mod():
    return _load_cleanup()


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def raw_db(tmp_path):
    """Return (conn, path) for a raw sqlite3 connection to a minimal v3 DB.

    FK enforcement is OFF so tests can insert freely.
    """
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, agent TEXT NOT NULL,
            mode TEXT, status TEXT NOT NULL,
            window_id TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            provider_session_id TEXT,
            transcript_offset INTEGER NOT NULL DEFAULT 0,
            transcript_path TEXT
        );
        CREATE TABLE topic_bindings (
            group_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
            session_id TEXT NOT NULL, topic_title TEXT NOT NULL,
            bound_at INTEGER NOT NULL, PRIMARY KEY (group_id, topic_id)
        );
    """)
    conn.commit()
    return conn, path


def _ins_session(conn, sid, window_id, status="active", updated_at=1000):
    conn.execute(
        "INSERT INTO sessions (session_id,cwd,agent,status,window_id,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (sid, "/c", "claude", status, window_id, 1000, updated_at),
    )


def _ins_binding(conn, group_id, topic_id, sid):
    conn.execute(
        "INSERT INTO topic_bindings (group_id,topic_id,session_id,topic_title,bound_at)"
        " VALUES (?,?,?,?,?)",
        (group_id, topic_id, sid, "T", 0),
    )


# ---- Tests -------------------------------------------------------------------


def test_dry_run_makes_no_changes(raw_db, cleanup_mod, monkeypatch):
    conn, _ = raw_db
    monkeypatch.setattr(cleanup_mod, "find_live_tmux_windows", lambda: None)
    for i in range(3):
        _ins_session(conn, f"s{i}", "@5", updated_at=1000 + i)
    conn.commit()
    cleanup_mod.cleanup(conn, apply=False)
    rows = conn.execute("SELECT status FROM sessions").fetchall()
    assert all(r["status"] == "active" for r in rows)
    assert len(rows) == 3


def test_apply_retires_duplicates_keeps_most_recent(raw_db, cleanup_mod, monkeypatch):
    conn, _ = raw_db
    monkeypatch.setattr(cleanup_mod, "find_live_tmux_windows", lambda: None)
    _ins_session(conn, "old1", "@5", updated_at=1001)
    _ins_session(conn, "old2", "@5", updated_at=1002)
    _ins_session(conn, "newest", "@5", updated_at=1003)
    conn.commit()
    cleanup_mod.cleanup(conn, apply=True)
    conn.commit()
    rows = conn.execute("SELECT session_id, status FROM sessions").fetchall()
    statuses = {r["session_id"]: r["status"] for r in rows}
    assert statuses["newest"] == "active"
    assert statuses["old1"] == "retired"
    assert statuses["old2"] == "retired"


def test_apply_removes_stale_bindings(raw_db, cleanup_mod, monkeypatch):
    conn, _ = raw_db
    monkeypatch.setattr(cleanup_mod, "find_live_tmux_windows", lambda: None)
    _ins_session(conn, "ret1", "@5", status="retired")
    _ins_binding(conn, 1, 10, "ret1")
    conn.commit()
    cleanup_mod.cleanup(conn, apply=True)
    conn.commit()
    bindings = conn.execute("SELECT * FROM topic_bindings").fetchall()
    assert bindings == []


def test_tmux_unavailable_skips_dead_window_check(raw_db, cleanup_mod, monkeypatch):
    conn, _ = raw_db
    monkeypatch.setattr(cleanup_mod, "find_live_tmux_windows", lambda: None)
    _ins_session(conn, "live1", "@5", updated_at=1000)
    conn.commit()
    cleanup_mod.cleanup(conn, apply=True)
    conn.commit()
    row = conn.execute("SELECT status FROM sessions WHERE session_id='live1'").fetchone()
    # No duplicate, no tmux check — should remain active
    assert row["status"] == "active"


def test_cleanup_returns_counts(raw_db, cleanup_mod, monkeypatch):
    conn, _ = raw_db
    monkeypatch.setattr(cleanup_mod, "find_live_tmux_windows", lambda: None)
    _ins_session(conn, "dup-a", "@5", updated_at=1001)
    _ins_session(conn, "dup-b", "@5", updated_at=1002)
    conn.commit()
    counts, _ = cleanup_mod.cleanup(conn, apply=True)
    assert counts["dup_retired"] == 1
    assert counts["stale_bindings"] == 0
