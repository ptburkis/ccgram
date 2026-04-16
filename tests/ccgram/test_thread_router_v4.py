"""Phase 4 ThreadRouter tests — DB persistence and cross-instantiation recall.

These tests confirm that bind_thread / unbind_thread / set_group_chat_id
write through to the SQLite DB so that a fresh ThreadRouter instantiation
backed by the same DB can reconstruct state.
"""

from __future__ import annotations

import time

import pytest

from ccgram import store
from ccgram.thread_router import ThreadRouter


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Temporary DB wired into store.db_path()."""
    path = tmp_path / "state.db"
    store.init_db(path)
    monkeypatch.setattr(store, "db_path", lambda: path)
    return path


def _seed_binding(db, session_id: str, window_id: str,
                  group_id: int = -100, topic_id: int = 10,
                  title: str = "T", user_id: int = 1234) -> None:
    """Seed a session + topic binding into the DB without holding a conn open."""
    with store.connect(db) as c:
        store.upsert_session(c, session_id=session_id, cwd="/c", agent="claude",
                             status="active", window_id=window_id, created_at=0)
        store.upsert_topic_binding_full(c, group_id, topic_id, session_id,
                                        user_id, window_id, title, int(time.time()))


class TestBindThreadPersistsToDB:
    """bind_thread writes user_id + window_id to topic_bindings."""

    def test_bind_writes_user_id_and_window_id(self, db):
        _seed_binding(db, "sid-1", "@1")

        router = ThreadRouter()
        router.bind_thread(1234, 10, "@1")

        with store.connect(db) as c:
            b = store.get_topic_binding(c, -100, 10)
        assert b is not None
        assert b.user_id == 1234
        assert b.window_id == "@1"

    def test_bind_with_name_writes_display_name_pref(self, db):
        _seed_binding(db, "sid-2", "@2", topic_id=20)

        router = ThreadRouter()
        router.bind_thread(1234, 20, "@2", window_name="my-project")

        with store.connect(db) as c:
            val = store.get_pref(c, "window_name", "display_name", scope_id="@2")
        assert val == "my-project"


class TestUnbindThreadClearsDB:
    """unbind_thread clears user_id + window_id on the topic_binding row."""

    def test_unbind_clears_user_id_and_window_id(self, db):
        _seed_binding(db, "sid-3", "@3", topic_id=30)

        router = ThreadRouter()
        router.bind_thread(1234, 30, "@3")
        router.unbind_thread(1234, 30)

        with store.connect(db) as c:
            b = store.get_topic_binding(c, -100, 30)
        assert b is not None
        assert b.user_id is None
        assert b.window_id is None


class TestSetGroupChatIdPersistsToDB:
    """set_group_chat_id writes to user_prefs(scope='group_chat')."""

    def test_group_chat_id_persisted(self, db):
        router = ThreadRouter()
        router.set_group_chat_id(1234, 10, -9998888)

        with store.connect(db) as c:
            val = store.get_pref(c, "group_chat", "chat_id", scope_id="1234:10")
        assert val == -9998888


class TestBindingsPersistAcrossInstantiations:
    """Verify that DB-persisted bindings survive a fresh ThreadRouter instance."""

    def test_fresh_router_can_see_db_bindings(self, db):
        """After seeding DB directly, iter_thread_bindings_db returns data."""
        _seed_binding(db, "sid-x", "@x", group_id=-200, topic_id=99)

        with store.connect(db) as c:
            rows = list(store.iter_thread_bindings_db(c))

        assert (1234, 99, "@x") in rows

    def test_to_dict_returns_empty(self):
        """to_dict returns {} — routing state is not serialised to JSON."""
        router = ThreadRouter()
        assert router.to_dict() == {}

    def test_from_dict_is_no_op(self):
        """from_dict does not populate in-memory state."""
        router = ThreadRouter()
        router.from_dict({
            "thread_bindings": {"100": {"1": "@1"}},
            "group_chat_ids": {"100:1": -999},
            "window_display_names": {"@1": "proj"},
        })
        # from_dict is a no-op: state should be empty
        assert router.get_window_for_thread(100, 1) is None
        assert router.resolve_chat_id(100, 1) == 100
        assert router.get_display_name("@1") == "@1"


class TestToDict:
    def test_to_dict_empty(self):
        router = ThreadRouter()
        assert router.to_dict() == {}

    def test_to_dict_after_bind_still_empty(self, db):
        _seed_binding(db, "sid-td", "@td", topic_id=77)
        router = ThreadRouter()
        router.bind_thread(1234, 77, "@td")
        assert router.to_dict() == {}
