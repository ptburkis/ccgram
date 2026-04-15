"""Tests for ccgram.reconcile — injectable fetchers, no real tmux/MTProto/files."""

import time
from pathlib import Path

from ccgram import store
from ccgram.mtproto_client import ForumTopic
from ccgram.reconcile import (
    _check_duplicate_bindings,
    reconcile,
)


# ---- Helpers -----------------------------------------------------------------


def make_topic(topic_id: int, title: str) -> ForumTopic:
    return ForumTopic(
        topic_id=topic_id,
        title=title,
        top_message_id=0,
        is_closed=False,
        is_hidden=False,
        created_date=None,
    )


def make_binding(
    group_id: int,
    topic_id: int,
    session_id: str,
    topic_title: str,
) -> store.TopicBinding:
    return store.TopicBinding(
        group_id=group_id,
        topic_id=topic_id,
        session_id=session_id,
        topic_title=topic_title,
        bound_at=int(time.time()),
    )


def _setup_db(db_path: Path) -> None:
    store.init_db(db_path)


async def _tmux(windows):
    return windows


async def _topics(topics_list):
    return topics_list


async def _identity(mapping):
    return mapping


GROUP = 1001


# ---- Tests -------------------------------------------------------------------


async def test_clean_state_empty_report(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn,
            session_id="sid-1",
            cwd="/proj",
            agent="claude",
            status="active",
            window_id="@1",
        )
        store.upsert_topic_binding(
            conn,
            group_id=GROUP,
            topic_id=10,
            session_id="sid-1",
            topic_title="myTitle",
        )

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@1", "myname", "/proj")]),
        topic_fetcher=lambda gid: _topics([make_topic(10, "myTitle")]),
        session_identity_fetcher=lambda: _identity({"@1": "sid-1"}),
    )
    assert report.issues == []
    assert report.applied == []
    assert report.auto_fix_count == 0
    assert report.manual_review_count == 0


async def test_title_drift_auto_fix(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn, session_id="sid-1", cwd="/proj", agent="claude", status="active", window_id="@1"
        )
        store.upsert_topic_binding(
            conn, group_id=GROUP, topic_id=10, session_id="sid-1", topic_title="old"
        )

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@1", "w", "/proj")]),
        topic_fetcher=lambda gid: _topics([make_topic(10, "new")]),
        session_identity_fetcher=lambda: _identity({"@1": "sid-1"}),
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "title_drift"
    assert issue.severity == "auto_fix"
    assert issue.suggested_fix is not None
    assert issue.suggested_fix["new_title"] == "new"
    assert issue.suggested_fix["topic_id"] == 10
    assert issue.suggested_fix["group_id"] == GROUP


async def test_orphan_topic_manual_review(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([]),
        topic_fetcher=lambda gid: _topics([make_topic(99, "unbound-topic")]),
        session_identity_fetcher=lambda: _identity({}),
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "orphan_topic"
    assert issue.severity == "manual_review"
    assert issue.suggested_fix is None


async def test_topic_1_never_flagged_as_orphan(tmp_path):
    """Topic 1 (Forum General root) is always present and never bound."""
    db = tmp_path / "state.db"
    _setup_db(db)

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([]),
        topic_fetcher=lambda gid: _topics(
            [make_topic(1, "General"), make_topic(99, "unbound-topic")]
        ),
        session_identity_fetcher=lambda: _identity({}),
    )
    # Only topic 99 should flag as orphan; topic 1 is filtered.
    assert all(
        "topic 1 " not in i.detail and "topic 1(" not in i.detail
        for i in report.issues
    )
    orphans = [i for i in report.issues if i.kind == "orphan_topic"]
    assert len(orphans) == 1
    assert "99" in orphans[0].detail


async def test_orphan_binding_topic_gone(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn, session_id="sid-1", cwd="/proj", agent="claude", status="active", window_id="@1"
        )
        store.upsert_topic_binding(
            conn, group_id=GROUP, topic_id=10, session_id="sid-1", topic_title="gone-topic"
        )

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@1", "w", "/proj")]),
        topic_fetcher=lambda gid: _topics([]),
        session_identity_fetcher=lambda: _identity({"@1": "sid-1"}),
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "orphan_binding"
    assert issue.severity == "manual_review"
    assert issue.suggested_fix is not None
    assert issue.suggested_fix["action"] == "delete_binding"


async def test_orphan_binding_retired_session(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn, session_id="sid-r", cwd="/proj", agent="claude", status="retired", window_id="@2"
        )
        store.upsert_topic_binding(
            conn, group_id=GROUP, topic_id=20, session_id="sid-r", topic_title="retired-topic"
        )

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([]),
        topic_fetcher=lambda gid: _topics([make_topic(20, "retired-topic")]),
        session_identity_fetcher=lambda: _identity({}),
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "orphan_binding"
    assert issue.severity == "manual_review"
    assert "retired" in issue.detail


async def test_orphan_window_manual_review(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@9", "foo", "/some/path")]),
        topic_fetcher=lambda gid: _topics([]),
        session_identity_fetcher=lambda: _identity({}),
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "orphan_window"
    assert issue.severity == "manual_review"
    assert "@9" in issue.detail
    assert issue.suggested_fix is None


async def test_ambiguous_session_manual_review(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn, session_id="sid-x", cwd="/proj", agent="claude", status="active", window_id="@5"
        )

    report = await reconcile(
        group_id=GROUP,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@5", "w5", "/p"), ("@19", "w19", "/p")]),
        topic_fetcher=lambda gid: _topics([]),
        session_identity_fetcher=lambda: _identity({"@5": "sid-x", "@19": "sid-x"}),
    )
    ambiguous = [i for i in report.issues if i.kind == "ambiguous_session"]
    assert len(ambiguous) == 1
    issue = ambiguous[0]
    assert issue.severity == "manual_review"
    assert "@5" in issue.detail
    assert "@19" in issue.detail
    assert "sid-x" in issue.detail


async def test_dry_run_does_not_write(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn, session_id="sid-1", cwd="/proj", agent="claude", status="active", window_id="@1"
        )
        store.upsert_topic_binding(
            conn, group_id=GROUP, topic_id=10, session_id="sid-1", topic_title="old-title"
        )

    report = await reconcile(
        group_id=GROUP,
        dry_run=True,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@1", "w", "/proj")]),
        topic_fetcher=lambda gid: _topics([make_topic(10, "new-title")]),
        session_identity_fetcher=lambda: _identity({"@1": "sid-1"}),
    )

    assert any(i.kind == "title_drift" for i in report.issues)
    assert report.applied == []

    with store.connect(db) as conn:
        binding = store.get_topic_binding(conn, GROUP, 10)
    assert binding is not None
    assert binding.topic_title == "old-title"


async def test_apply_writes_only_auto_fix(tmp_path):
    db = tmp_path / "state.db"
    _setup_db(db)
    with store.connect(db) as conn:
        store.upsert_session(
            conn, session_id="sid-1", cwd="/proj", agent="claude", status="active", window_id="@1"
        )
        store.upsert_session(
            conn, session_id="sid-2", cwd="/other", agent="claude", status="active", window_id="@2"
        )
        store.upsert_topic_binding(
            conn, group_id=GROUP, topic_id=10, session_id="sid-1", topic_title="old"
        )
        store.upsert_topic_binding(
            conn, group_id=GROUP, topic_id=30, session_id="sid-2", topic_title="gone"
        )

    report = await reconcile(
        group_id=GROUP,
        dry_run=False,
        db_path=db,
        tmux_fetcher=lambda: _tmux([("@1", "w1", "/proj"), ("@2", "w2", "/other")]),
        topic_fetcher=lambda gid: _topics([make_topic(10, "new"), make_topic(99, "unbound")]),
        session_identity_fetcher=lambda: _identity({"@1": "sid-1", "@2": "sid-2"}),
    )

    assert len(report.applied) == 1
    assert report.applied[0].kind == "title_drift"
    assert any(i.kind == "title_drift" for i in report.issues)
    assert any(i.kind == "orphan_topic" for i in report.issues)
    assert any(i.kind == "orphan_binding" for i in report.issues)

    with store.connect(db) as conn:
        b10 = store.get_topic_binding(conn, GROUP, 10)
        b30 = store.get_topic_binding(conn, GROUP, 30)
    assert b10 is not None
    assert b10.topic_title == "new"
    assert b30 is not None
    assert b30.topic_title == "gone"


async def test_duplicate_binding_detected():
    b1 = make_binding(GROUP, 10, "same-sid", "topicA")
    b2 = make_binding(GROUP, 20, "same-sid", "topicB")
    issues = _check_duplicate_bindings([b1, b2])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "duplicate_binding"
    assert issue.severity == "manual_review"
    assert "same-sid" in issue.detail
    assert issue.suggested_fix is None
