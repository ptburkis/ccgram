"""Reboot regression: events_offset must survive DB-first reload.

Simulates the post-reboot scenario where monitor_state.json has been wiped
(or never existed because we are migrating to DB-only) and verifies that
events_offset is correctly restored from the DB so events.jsonl is not
replayed from offset 0.

See: project_ccgram_reboot_postmortem_20260503.md (Bug 1).
"""

from __future__ import annotations

import pytest

from ccgram import store
from ccgram.monitor_state import MonitorState, TrackedSession


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    store.init_db(path)
    return path


def test_events_offset_roundtrip_via_db(tmp_path, db, monkeypatch):
    """events_offset persists to DB and is recovered on a fresh load."""
    monkeypatch.setattr(store, "db_path", lambda: db)

    state_file = tmp_path / "monitor_state.json"
    state = MonitorState(state_file=state_file)
    state.update_session(
        TrackedSession(
            session_id="sess-1",
            file_path="/tmp/sess-1.jsonl",
            last_byte_offset=42,
        )
    )
    state.events_offset = 12345
    state.save()

    state2 = MonitorState(state_file=state_file)
    state2.load()
    assert state2.events_offset == 12345


def test_events_offset_survives_json_wipe(tmp_path, db, monkeypatch):
    """Simulates reboot: DB has state, JSON file is gone — must recover offset."""
    monkeypatch.setattr(store, "db_path", lambda: db)

    state_file = tmp_path / "monitor_state.json"

    # Write some bytes to a fake events.jsonl so offset is meaningful.
    events_jsonl = tmp_path / "events.jsonl"
    events_jsonl.write_text("\n".join(["{}", "{}", "{}"]) + "\n")
    file_size = events_jsonl.stat().st_size

    state = MonitorState(state_file=state_file)
    state.update_session(
        TrackedSession(
            session_id="sess-A",
            file_path="/tmp/foo.jsonl",
            last_byte_offset=7,
        )
    )
    state.events_offset = file_size
    state.save()

    # Simulate reboot: wipe the JSON state file. DB is authoritative.
    if state_file.exists():
        state_file.unlink()

    state2 = MonitorState(state_file=state_file)
    state2.load()

    assert state2.events_offset == file_size, (
        "events_offset not recovered from DB after JSON wipe — "
        "this would replay the entire events.jsonl on restart"
    )
    assert state2.get_session("sess-A") is not None


def test_events_offset_default_zero_when_db_empty(tmp_path, db, monkeypatch):
    """Empty DB and no JSON: events_offset defaults to 0 (no crash)."""
    monkeypatch.setattr(store, "db_path", lambda: db)

    state_file = tmp_path / "monitor_state.json"
    state = MonitorState(state_file=state_file)
    state.load()
    assert state.events_offset == 0
