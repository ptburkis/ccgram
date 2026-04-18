"""Tests for MonitorState DB persistence round-trip.

Verifies that save() writes byte offsets to the DB and that a fresh
MonitorState.load() can recover them without falling back to JSON.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccgram import store
from ccgram.monitor_state import MonitorState, TrackedSession


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    store.init_db(path)
    return path


def test_save_and_load_roundtrip(tmp_path, db, monkeypatch):
    """save() writes offsets to DB; a new MonitorState.load() recovers them."""
    # Patch db_path so store.connect() uses our tmp DB
    monkeypatch.setattr(store, "db_path", lambda: db)

    state_file = tmp_path / "monitor_state.json"
    state = MonitorState(state_file=state_file)
    state.load()  # nothing in DB yet — should be empty

    assert state.get_session("test-sess") is None

    # Update a session and save
    state.update_session(
        TrackedSession(
            session_id="test-sess",
            file_path="/tmp/foo.jsonl",
            last_byte_offset=12345,
        )
    )
    state.save()

    # Create a fresh MonitorState and reload from DB
    state2 = MonitorState(state_file=state_file)
    state2.load()

    recovered = state2.get_session("test-sess")
    assert recovered is not None, "Session not recovered from DB"
    assert recovered.last_byte_offset == 12345
    assert recovered.file_path == "/tmp/foo.jsonl"


def test_load_coerces_offset_to_int(tmp_path, db, monkeypatch):
    """last_byte_offset is coerced to int even if stored as float."""
    monkeypatch.setattr(store, "db_path", lambda: db)

    # Manually insert a float value into the DB
    with store.connect(db) as conn:
        store.set_pref(conn, "monitor", "last_byte_offset", 99.0, scope_id="float-sess")
        store.set_pref(conn, "monitor", "file_path", "/tmp/bar.jsonl", scope_id="float-sess")

    state_file = tmp_path / "monitor_state.json"
    state = MonitorState(state_file=state_file)
    state.load()

    recovered = state.get_session("float-sess")
    assert recovered is not None
    assert isinstance(recovered.last_byte_offset, int)
    assert recovered.last_byte_offset == 99
