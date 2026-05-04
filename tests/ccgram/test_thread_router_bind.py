"""Tests for _db_write_binding upsert fix in ThreadRouter.

Bug: _db_write_binding was UPDATE-only. If no topic_bindings row existed
for the topic_id, the UPDATE matched 0 rows and the binding was never
persisted. 60s hydrate would then wipe the in-memory binding because no
DB row existed.

Fix: upsert — if rowcount==0, INSERT using session lookup + group_id derivation.
"""

from __future__ import annotations

import logging
import time

import pytest
import structlog

from ccgram import store
from ccgram.thread_router import ThreadRouter


@pytest.fixture(autouse=True)
def _configure_structlog_for_caplog():
    """Route structlog through stdlib logging so caplog can capture it."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    yield
    structlog.reset_defaults()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Temporary DB wired into store.db_path()."""
    path = tmp_path / "state.db"
    store.init_db(path)
    monkeypatch.setattr(store, "db_path", lambda: path)
    return path


def _seed_session(db, session_id: str, window_id: str, status: str = "active") -> None:
    """Insert a session row without any topic_binding."""
    with store.connect(db) as c:
        store.upsert_session(
            c,
            session_id=session_id,
            cwd="/test",
            agent="claude",
            status=status,
            window_id=window_id,
            created_at=int(time.time()),
        )


def _count_bindings(db) -> int:
    with store.connect(db) as c:
        return c.execute("SELECT COUNT(*) FROM topic_bindings").fetchone()[0]


class TestBindThreadCreatesRowWhenMissing:
    """Test 1: empty topic_bindings — INSERT path creates a new row."""

    def test_bind_thread_creates_row_when_missing(self, db):
        _seed_session(db, "sid-new-1", "@10")

        router = ThreadRouter()
        router.bind_thread(user_id=1, thread_id=99, window_id="@10", window_name="foo")

        with store.connect(db) as c:
            row = c.execute(
                "SELECT * FROM topic_bindings WHERE topic_id = ?", (99,)
            ).fetchone()

        assert row is not None, "Expected a topic_bindings row to be created"
        assert row["topic_id"] == 99
        assert row["window_id"] == "@10"
        assert row["user_id"] == 1
        assert row["topic_title"] == "foo"

        # session_id on the new row must match the active session at @10
        with store.connect(db) as c:
            s = c.execute(
                "SELECT session_id FROM sessions "
                "WHERE window_id = '@10' AND status = 'active'",
            ).fetchone()
        assert s is not None
        assert row["session_id"] == s["session_id"]


class TestBindThreadUpdatesExistingRow:
    """Test 2: pre-existing row — UPDATE path fires, no duplicate row created."""

    def test_bind_thread_updates_existing_row(self, db):
        # Seed a session at @20 and a pre-existing topic_binding for topic 99
        with store.connect(db) as c:
            store.upsert_session(
                c,
                session_id="sid-old",
                cwd="/t",
                agent="claude",
                status="active",
                window_id="@20",
                created_at=int(time.time()),
            )
            store.upsert_topic_binding_full(
                c, -100, 99, "sid-old", 9999, "@OLD", "old-title", int(time.time())
            )

        router = ThreadRouter()
        router.bind_thread(user_id=1, thread_id=99, window_id="@20", window_name="bar")

        # Exactly one row — no insert happened
        assert _count_bindings(db) == 1, "Should still be exactly 1 row"

        with store.connect(db) as c:
            row = c.execute(
                "SELECT window_id, user_id FROM topic_bindings WHERE topic_id = 99"
            ).fetchone()
        assert row["window_id"] == "@20"
        assert row["user_id"] == 1


class TestBindThreadSkipsWhenNoActiveSession:
    """Test 3: no sessions, no topic_bindings — bail with WARNING, no crash."""

    def test_bind_thread_skips_when_no_active_session(self, db, caplog):
        router = ThreadRouter()
        with caplog.at_level(logging.WARNING, logger="ccgram.thread_router"):
            router.bind_thread(user_id=1, thread_id=55, window_id="@99", window_name="x")

        assert _count_bindings(db) == 0, "No row should be created"
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "no active session" in m.lower() for m in warning_msgs
        ), f"Expected WARNING about no active session, got: {warning_msgs}"


class TestBindThreadHandlesUniqueSessionIdCollision:
    """Test 4: session S1 already bound to topic 50; rebinding S1 to topic 99 must not crash."""

    def test_bind_thread_handles_unique_session_id_collision(self, db, caplog):
        # Session S1 at @30, already bound to topic 50
        with store.connect(db) as c:
            store.upsert_session(
                c,
                session_id="S1",
                cwd="/t",
                agent="claude",
                status="active",
                window_id="@30",
                created_at=int(time.time()),
            )
            store.upsert_topic_binding_full(
                c, -100, 50, "S1", 1, "@30", "existing-topic", int(time.time())
            )

        router = ThreadRouter()
        with caplog.at_level(logging.WARNING, logger="ccgram.thread_router"):
            # Attempt to bind topic 99 to @30 — S1 already bound to topic 50,
            # so INSERT would violate UNIQUE(session_id)
            router.bind_thread(user_id=1, thread_id=99, window_id="@30", window_name="new-topic")

        # No crash, no new row for topic 99
        with store.connect(db) as c:
            row = c.execute(
                "SELECT * FROM topic_bindings WHERE topic_id = 99"
            ).fetchone()
        assert row is None, "Topic 99 row should NOT have been created"

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "unique" in m.lower() or "violation" in m.lower()
            for m in warning_msgs
        ), f"Expected WARNING about UNIQUE violation, got: {warning_msgs}"
