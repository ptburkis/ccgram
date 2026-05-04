"""Tests for ccgram.session_repo -- single-writer repository for sessions/topic_bindings."""

from __future__ import annotations

import sqlite3
import time

import pytest

from ccgram import session_repo, store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ccgram_test_dir(tmp_path, monkeypatch):
    """Point CCGRAM_DIR at a fresh tmp path and pre-create the DB."""
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    store.init_db(tmp_path / "state.db")
    return tmp_path


# ---------------------------------------------------------------------------
# Helper -- low-level raw insert bypassing session_repo (for seeding)
# ---------------------------------------------------------------------------


def _raw_insert_session(
    db_path,
    session_id,
    window_id,
    *,
    status="active",
    cwd="/cwd",
    agent="claude",
    updated_at=None,
):
    now = updated_at if updated_at is not None else int(time.time())
    with store.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO sessions
               (session_id, cwd, agent, status, window_id, created_at, updated_at,
                transcript_offset)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (session_id, cwd, agent, status, window_id, 1000, now),
        )


# ---------------------------------------------------------------------------
# create_session_for_window
# ---------------------------------------------------------------------------


class TestCreateSessionForWindow:
    def test_create_session_for_window_inserts_row(self, ccgram_test_dir):
        session_repo.create_session_for_window(
            session_id="sess-new",
            window_id="@1",
            cwd="/home/peter",
            agent="claude",
        )
        row = session_repo.get_by_session_id("sess-new")
        assert row is not None
        assert row["status"] == "active"
        assert row["window_id"] == "@1"
        assert row["cwd"] == "/home/peter"
        assert row["agent"] == "claude"

    def test_create_session_for_window_retires_prior_active(self, ccgram_test_dir):
        db = ccgram_test_dir / "state.db"
        # Seed an existing active row for @1
        _raw_insert_session(db, "sess-old", "@1")

        session_repo.create_session_for_window(
            session_id="sess-new",
            window_id="@1",
            cwd="/cwd",
            agent="claude",
        )

        old = session_repo.get_by_session_id("sess-old")
        assert old["status"] == "retired"
        assert old["window_id"] is None  # window_id cleared on retire

        new = session_repo.get_by_session_id("sess-new")
        assert new["status"] == "active"
        assert new["window_id"] == "@1"

    def test_create_session_atomic_under_unique_index(self, ccgram_test_dir):
        """retire-before-insert means no IntegrityError even with the partial unique index."""
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "sess-a", "@5")

        # This would raise IntegrityError if retire didn't happen atomically first
        session_repo.create_session_for_window(
            session_id="sess-b",
            window_id="@5",
            cwd="/cwd",
            agent="claude",
        )

        assert session_repo.get_by_window_id("@5")["session_id"] == "sess-b"


# ---------------------------------------------------------------------------
# retire_window
# ---------------------------------------------------------------------------


class TestRetireWindow:
    def test_retire_window_skips_when_alive(self, ccgram_test_dir, monkeypatch, caplog):
        import logging

        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "sess-live", "@2")

        # Patch at the name used inside session_repo (imported as confirm_dead_or_skip)
        monkeypatch.setattr("ccgram.session_repo.confirm_dead_or_skip", lambda wid, reason: False)

        result = session_repo.retire_window("@2", reason="test_alive")

        assert result is False
        # Row should be unchanged
        row = session_repo.get_by_session_id("sess-live")
        assert row["status"] == "active"
        assert row["window_id"] == "@2"

    def test_retire_window_acts_when_dead(self, ccgram_test_dir, monkeypatch):
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "sess-dead", "@3")

        monkeypatch.setattr("ccgram.session_repo.confirm_dead_or_skip", lambda wid, reason: True)

        result = session_repo.retire_window("@3", reason="test_dead")

        assert result is True
        row = session_repo.get_by_session_id("sess-dead")
        assert row["status"] == "retired"
        assert row["window_id"] is None

    def test_retire_window_force_skips_authority_check(self, ccgram_test_dir, monkeypatch):
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "sess-force", "@4")

        # Record whether confirm_dead_or_skip was called
        called = []
        monkeypatch.setattr(
            "ccgram.session_repo.confirm_dead_or_skip",
            lambda wid, reason: called.append((wid, reason)) or True,
        )

        result = session_repo.retire_window("@4", reason="user_kill", force=True)

        assert result is True
        assert called == []  # authority NOT consulted
        row = session_repo.get_by_session_id("sess-force")
        assert row["status"] == "retired"

    def test_retire_window_no_active_returns_false(self, ccgram_test_dir, monkeypatch):
        monkeypatch.setattr("ccgram.session_repo.confirm_dead_or_skip", lambda wid, reason: True)

        # No row seeded for @99
        result = session_repo.retire_window("@99", reason="no_row_exists")
        assert result is False


# ---------------------------------------------------------------------------
# update_* round-trips
# ---------------------------------------------------------------------------


class TestUpdateHelpers:
    def test_update_offset(self, ccgram_test_dir):
        session_repo.create_session_for_window(
            session_id="s1", window_id="@10", cwd="/cwd", agent="claude"
        )
        session_repo.update_offset("s1", 99999)
        row = session_repo.get_by_session_id("s1")
        assert row["transcript_offset"] == 99999

    def test_update_transcript_path(self, ccgram_test_dir):
        session_repo.create_session_for_window(
            session_id="s2", window_id="@11", cwd="/cwd", agent="claude"
        )
        session_repo.update_transcript_path("s2", "/tmp/transcript.jsonl")
        row = session_repo.get_by_session_id("s2")
        assert row["transcript_path"] == "/tmp/transcript.jsonl"

    def test_update_provider_session_id(self, ccgram_test_dir):
        session_repo.create_session_for_window(
            session_id="s3", window_id="@12", cwd="/cwd", agent="claude"
        )
        session_repo.update_provider_session_id("s3", "prov-abc-123")
        row = session_repo.get_by_session_id("s3")
        assert row["provider_session_id"] == "prov-abc-123"


# ---------------------------------------------------------------------------
# bind_topic / unbind_topic
# ---------------------------------------------------------------------------


class TestTopicBindings:
    def test_bind_topic_inserts_or_replaces(self, ccgram_test_dir):
        session_repo.create_session_for_window(
            session_id="s-bind", window_id="@20", cwd="/cwd", agent="claude"
        )

        session_repo.bind_topic(
            group_id=-1001,
            topic_id=42,
            session_id="s-bind",
            topic_title="My Topic",
            user_id=12345,
            window_id="@20",
        )

        db = ccgram_test_dir / "state.db"
        with store.connect(db) as conn:
            row = conn.execute(
                "SELECT * FROM topic_bindings WHERE group_id=? AND topic_id=?",
                (-1001, 42),
            ).fetchone()
        assert row is not None
        assert row["session_id"] == "s-bind"
        assert row["topic_title"] == "My Topic"

        # Second call with same (group_id, topic_id) replaces
        session_repo.create_session_for_window(
            session_id="s-bind-2", window_id="@21", cwd="/cwd", agent="claude"
        )
        session_repo.bind_topic(
            group_id=-1001,
            topic_id=42,
            session_id="s-bind-2",
            topic_title="Renamed Topic",
            user_id=12345,
            window_id="@21",
        )

        db = ccgram_test_dir / "state.db"
        with store.connect(db) as conn:
            row2 = conn.execute(
                "SELECT * FROM topic_bindings WHERE group_id=? AND topic_id=?",
                (-1001, 42),
            ).fetchone()
        assert row2["session_id"] == "s-bind-2"
        assert row2["topic_title"] == "Renamed Topic"

    def test_unbind_topic_returns_true_when_deleted(self, ccgram_test_dir):
        session_repo.create_session_for_window(
            session_id="s-unbind", window_id="@30", cwd="/cwd", agent="claude"
        )
        session_repo.bind_topic(
            group_id=-1002,
            topic_id=7,
            session_id="s-unbind",
            topic_title="T",
        )

        result = session_repo.unbind_topic(-1002, 7)
        assert result is True

    def test_unbind_topic_returns_false_when_absent(self, ccgram_test_dir):
        result = session_repo.unbind_topic(-9999, 9999)
        assert result is False


# ---------------------------------------------------------------------------
# list_active_sessions
# ---------------------------------------------------------------------------


class TestListActiveSessions:
    def test_list_active_sessions_filters_status(self, ccgram_test_dir):
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "s-active-1", "@40", status="active")
        _raw_insert_session(db, "s-active-2", "@41", status="active")
        _raw_insert_session(db, "s-retired", "@42", status="retired")
        _raw_insert_session(db, "s-errored", None, status="errored")

        rows = session_repo.list_active_sessions()
        sids = {r["session_id"] for r in rows}
        assert "s-active-1" in sids
        assert "s-active-2" in sids
        assert "s-retired" not in sids
        assert "s-errored" not in sids


# ---------------------------------------------------------------------------
# get_by_window_id
# ---------------------------------------------------------------------------


class TestGetByWindowId:
    def test_get_by_window_id_returns_active_only(self, ccgram_test_dir):
        db = ccgram_test_dir / "state.db"
        # Seed a retired row first (simulate a prior session for this window)
        _raw_insert_session(db, "s-old-retired", "@50", status="retired")
        _raw_insert_session(db, "s-current-active", "@50", status="active")

        result = session_repo.get_by_window_id("@50")
        assert result is not None
        assert result["session_id"] == "s-current-active"
        assert result["status"] == "active"

    def test_get_by_window_id_none_when_absent(self, ccgram_test_dir):
        result = session_repo.get_by_window_id("@nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# get_by_session_id
# ---------------------------------------------------------------------------


class TestGetBySessionId:
    def test_get_by_session_id_any_status(self, ccgram_test_dir):
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "s-ret", None, status="retired")

        result = session_repo.get_by_session_id("s-ret")
        assert result is not None
        assert result["status"] == "retired"

    def test_get_by_session_id_returns_none_when_absent(self, ccgram_test_dir):
        assert session_repo.get_by_session_id("no-such-sid") is None


# ---------------------------------------------------------------------------
# hydrate_in_memory
# ---------------------------------------------------------------------------


class TestHydrateInMemory:
    """Tests for session_repo.hydrate_in_memory."""

    def test_hydrate_in_memory_router_only(self, ccgram_test_dir):
        """hydrate_in_memory rebuilds thread_bindings from DB topic_bindings."""
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "s-hyd", "@60", status="active")

        # Insert a topic binding and a group_chat pref so the router can map it
        session_repo.bind_topic(
            group_id=-1001,
            topic_id=99,
            session_id="s-hyd",
            topic_title="Test",
            user_id=42,
            window_id="@60",
        )
        with store.connect(db) as conn:
            store.set_pref(conn, "group_chat", "42:99", -1001, scope_id="")

        # Create a mock thread_router with empty state
        class _MockRouter:
            def __init__(self):
                self.thread_bindings: dict = {}
                self._window_to_thread: dict = {}

            def _rebuild_reverse_index(self):
                self._window_to_thread = {}
                for uid, bindings in self.thread_bindings.items():
                    for tid, wid in bindings.items():
                        self._window_to_thread[(uid, wid)] = tid

        router = _MockRouter()
        result = session_repo.hydrate_in_memory(thread_router=router)

        assert result["sessions"] == 1
        assert result["bindings"] == 1
        # topic 99 should be bound for user 42
        assert router.thread_bindings.get(42, {}).get(99) == "@60"

    def test_hydrate_in_memory_atomic_swap(self, ccgram_test_dir):
        """Verifies the dict is replaced wholesale (stale entries removed), not merged."""
        db = ccgram_test_dir / "state.db"

        # Session + binding
        _raw_insert_session(db, "s-swap", "@70", status="active")
        session_repo.bind_topic(
            group_id=-2002,
            topic_id=88,
            session_id="s-swap",
            topic_title="Swap",
            user_id=100,
            window_id="@70",
        )
        with store.connect(db) as conn:
            store.set_pref(conn, "group_chat", "100:88", -2002, scope_id="")

        class _MockRouter:
            def __init__(self):
                # Pre-populate with stale entry that should be evicted
                self.thread_bindings: dict = {999: {77: "@stale"}}
                self._window_to_thread: dict = {}

            def _rebuild_reverse_index(self):
                self._window_to_thread = {}
                for uid, bindings in self.thread_bindings.items():
                    for tid, wid in bindings.items():
                        self._window_to_thread[(uid, wid)] = tid

        router = _MockRouter()
        session_repo.hydrate_in_memory(thread_router=router)

        # Stale user 999 should be gone
        assert 999 not in router.thread_bindings
        # New binding should be present
        assert router.thread_bindings.get(100, {}).get(88) == "@70"

    def test_hydrate_in_memory_excludes_retired_sessions(self, ccgram_test_dir):
        """Retired sessions should not appear in rebuilt caches."""
        db = ccgram_test_dir / "state.db"
        _raw_insert_session(db, "s-ret-hyd", "@80", status="retired")

        class _MockRouter:
            def __init__(self):
                self.thread_bindings: dict = {}
                self._window_to_thread: dict = {}

            def _rebuild_reverse_index(self):
                pass

        router = _MockRouter()
        result = session_repo.hydrate_in_memory(thread_router=router)

        assert result["sessions"] == 0
        assert router.thread_bindings == {}
