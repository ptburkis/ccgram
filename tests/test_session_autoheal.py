"""Tests for ccgram.session_autoheal — focused on the topic_binding no-steal guard."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ccgram import store
from ccgram.session_autoheal import _update_session_map_sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ccgram_dir(tmp_path, monkeypatch):
    """Point CCGRAM_DIR at a fresh tmp path, pre-create DB, and stub config."""
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    db_path = tmp_path / "state.db"
    store.init_db(db_path)

    # Stub config so _update_session_map_sync writes its session_map here.
    from ccgram import config as cfg_module
    monkeypatch.setattr(cfg_module.config, "config_dir", tmp_path)
    monkeypatch.setattr(cfg_module.config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(cfg_module.config, "tmux_session_name", "ccgram")

    return tmp_path


def _seed_session_map(ccgram_dir: Path, window_id: str, session_id: str, cwd: str = "/cwd") -> None:
    """Write a minimal session_map.json so _update_session_map_sync finds the entry."""
    sm = {
        f"ccgram:{window_id}": {
            "session_id": session_id,
            "transcript_path": f"{cwd}/.claude/projects/proj/{session_id}.jsonl",
            "cwd": cwd,
            "window_name": "james",
            "provider": "claude",
        }
    }
    (ccgram_dir / "session_map.json").write_text(json.dumps(sm))


def _seed_topic_binding(db_path: Path, window_id: str, session_id: str, topic_id: int = 1) -> None:
    with store.connect(db_path) as conn:
        # Need a sessions row first (FK).
        conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, cwd, agent, status, window_id, created_at, updated_at, transcript_offset)"
            " VALUES (?, '/cwd', 'claude', 'active', ?, ?, ?, 0)",
            (session_id, window_id, int(time.time()), int(time.time())),
        )
        conn.execute(
            "INSERT OR IGNORE INTO topic_bindings"
            " (group_id, topic_id, session_id, topic_title, bound_at, window_id)"
            " VALUES (100, ?, ?, 'test-topic', ?, ?)",
            (topic_id, session_id, int(time.time()), window_id),
        )


def _get_binding_session(db_path: Path, topic_id: int) -> str | None:
    with store.connect(db_path) as conn:
        row = conn.execute(
            "SELECT session_id FROM topic_bindings WHERE topic_id=?", (topic_id,)
        ).fetchone()
        return row["session_id"] if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_autoheal_does_not_steal_topic_binding(ccgram_dir):
    """_update_session_map_sync must NOT overwrite a binding for a DIFFERENT session."""
    db_path = ccgram_dir / "state.db"

    # Window @1: old session "sid-old" should be migrated to "sid-new"
    _seed_topic_binding(db_path, "@1", "sid-old", topic_id=1)
    _seed_session_map(ccgram_dir, "@1", "sid-old")

    # Window @2: "sid-other" belongs to an unrelated window — must stay untouched.
    # Its topic_binding points at window_id=@2 with session sid-other.
    _seed_topic_binding(db_path, "@2", "sid-other", topic_id=2)

    # Patch Path.home() so _update_session_map_sync finds the right state.db
    import ccgram.session_autoheal as _mod
    monkeypatch_home = ccgram_dir.parent
    # The autoheal uses Path.home() / ".ccgram" / "state.db"; replicate that.
    ccgram_subdir = ccgram_dir.parent / ".ccgram"
    ccgram_subdir.mkdir(exist_ok=True)
    import shutil
    shutil.copy(db_path, ccgram_subdir / "state.db")

    # Also write a dummy new transcript so the function proceeds.
    new_transcript = str(ccgram_dir / "sid-new.jsonl")
    Path(new_transcript).write_text("")

    # We need to patch Path.home to point to ccgram_dir.parent so the autoheal
    # uses ccgram_dir.parent/.ccgram/state.db.
    original_home = Path.home

    class _FakePathHome:
        @staticmethod
        def __call__():
            return ccgram_dir.parent

    import unittest.mock as mock
    with mock.patch.object(Path, "home", staticmethod(lambda: ccgram_dir.parent)):
        result = _update_session_map_sync("@1", "sid-old", "sid-new", new_transcript)

    assert result is True

    # @1's topic binding should now point at sid-new
    patched_db = ccgram_subdir / "state.db"
    with store.connect(patched_db) as conn:
        row1 = conn.execute(
            "SELECT session_id FROM topic_bindings WHERE topic_id=1"
        ).fetchone()
        row2 = conn.execute(
            "SELECT session_id FROM topic_bindings WHERE topic_id=2"
        ).fetchone()

    assert row1["session_id"] == "sid-new", "topic 1 should have been updated"
    assert row2["session_id"] == "sid-other", "topic 2 (unrelated) must not be stolen"


def test_autoheal_no_op_when_old_sid_empty(ccgram_dir):
    """When old_sid is empty string, no UPDATE should be issued."""
    db_path = ccgram_dir / "state.db"
    _seed_topic_binding(db_path, "@3", "sid-existing", topic_id=3)
    _seed_session_map(ccgram_dir, "@3", "sid-existing")

    # Create a fake new transcript
    new_transcript = str(ccgram_dir / "sid-replacement.jsonl")
    Path(new_transcript).write_text("")

    # Override session_map to have NO session_id so old_sid == ""
    sm = {
        "ccgram:@3": {
            "session_id": "",
            "transcript_path": "",
            "cwd": "/cwd",
            "window_name": "james",
            "provider": "claude",
        }
    }
    (ccgram_dir / "session_map.json").write_text(json.dumps(sm))

    ccgram_subdir = ccgram_dir.parent / ".ccgram"
    ccgram_subdir.mkdir(exist_ok=True)
    import shutil
    shutil.copy(db_path, ccgram_subdir / "state.db")

    import unittest.mock as mock
    with mock.patch.object(Path, "home", staticmethod(lambda: ccgram_dir.parent)):
        result = _update_session_map_sync("@3", "", "sid-replacement", new_transcript)

    assert result is True

    patched_db = ccgram_subdir / "state.db"
    with store.connect(patched_db) as conn:
        row = conn.execute(
            "SELECT session_id FROM topic_bindings WHERE topic_id=3"
        ).fetchone()

    # Binding must remain at "sid-existing" — no UPDATE was allowed
    assert row["session_id"] == "sid-existing", "no-steal: old_sid='' must not overwrite"
