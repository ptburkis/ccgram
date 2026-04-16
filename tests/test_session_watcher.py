"""Tests for ccgram.session_watcher — session identity resolution (Chunk F)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccgram import session_watcher
from ccgram.session_watcher import (
    DuplicateSessionIdError,
    read_session_id_from_marker_file,
    read_session_id_from_pane_env,
    resolve_session_identity,
    scan_all_pane_identities,
)


@pytest.fixture()
def home_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _make_marker(home: Path, window_id: str, content: str) -> Path:
    marker_dir = home / ".ccgram" / "debug"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"terminal-{window_id}.sid"
    marker.write_text(content)
    return marker


def test_marker_file_hit(home_dir):
    _make_marker(home_dir, "@5", "sid-abc")
    assert read_session_id_from_marker_file("@5") == "sid-abc"


def test_marker_file_miss(home_dir):
    assert read_session_id_from_marker_file("@5") is None


def test_marker_file_empty(home_dir):
    _make_marker(home_dir, "@5", "")
    assert read_session_id_from_marker_file("@5") is None


def test_env_resolve_finds_in_immediate_pane_pid(monkeypatch):
    env = b"CCGRAM_SESSION_ID=sid-direct\x00PATH=/usr/bin"
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: 12345)
    monkeypatch.setattr(
        session_watcher, "_read_proc_environ", lambda pid: env if pid == 12345 else None
    )
    monkeypatch.setattr(session_watcher, "_get_child_pids", lambda pid: [])
    assert read_session_id_from_pane_env("@0") == "sid-direct"


def test_env_resolve_bfs_walks_one_level(monkeypatch):
    env_child = b"CCGRAM_SESSION_ID=sid-xyz\x00"

    def mock_environ(pid: int) -> bytes | None:
        return b"" if pid == 100 else env_child if pid == 200 else None

    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: 100)
    monkeypatch.setattr(session_watcher, "_read_proc_environ", mock_environ)
    monkeypatch.setattr(
        session_watcher, "_get_child_pids", lambda pid: [200] if pid == 100 else []
    )
    assert read_session_id_from_pane_env("@0") == "sid-xyz"


def test_env_resolve_bfs_walks_up_to_4_levels(monkeypatch):
    def mock_environ(pid: int) -> bytes | None:
        return b"CCGRAM_SESSION_ID=sid-deep\x00" if pid == 5 else b""

    def mock_children(pid: int) -> list[int]:
        return {1: [2], 2: [3], 3: [4], 4: [5], 5: []}.get(pid, [])

    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: 1)
    monkeypatch.setattr(session_watcher, "_read_proc_environ", mock_environ)
    monkeypatch.setattr(session_watcher, "_get_child_pids", mock_children)
    assert read_session_id_from_pane_env("@0") == "sid-deep"


def test_env_resolve_bfs_stops_at_depth_5(monkeypatch):
    def mock_environ(pid: int) -> bytes | None:
        return b"CCGRAM_SESSION_ID=sid-toodeep\x00" if pid == 6 else b""

    def mock_children(pid: int) -> list[int]:
        return {1: [2], 2: [3], 3: [4], 4: [5], 5: [6], 6: []}.get(pid, [])

    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: 1)
    monkeypatch.setattr(session_watcher, "_read_proc_environ", mock_environ)
    monkeypatch.setattr(session_watcher, "_get_child_pids", mock_children)
    assert read_session_id_from_pane_env("@0") is None


def test_env_resolve_tmux_call_fails(monkeypatch):
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: None)
    assert read_session_id_from_pane_env("@0") is None


def test_env_resolve_no_pane_pid(monkeypatch):
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: None)
    assert read_session_id_from_pane_env("@0") is None


def test_resolve_prefers_marker_file_over_env(home_dir, monkeypatch):
    _make_marker(home_dir, "@5", "sid-from-marker")
    env = b"CCGRAM_SESSION_ID=sid-from-env\x00"
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: 12345)
    monkeypatch.setattr(session_watcher, "_read_proc_environ", lambda pid: env)
    monkeypatch.setattr(session_watcher, "_get_child_pids", lambda pid: [])
    assert resolve_session_identity("@5") == "sid-from-marker"


def test_resolve_falls_back_to_env_when_marker_missing(home_dir, monkeypatch):
    env = b"CCGRAM_SESSION_ID=sid-from-env\x00"
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: 99)
    monkeypatch.setattr(session_watcher, "_read_proc_environ", lambda pid: env)
    monkeypatch.setattr(session_watcher, "_get_child_pids", lambda pid: [])
    assert resolve_session_identity("@5") == "sid-from-env"


def test_resolve_both_miss_returns_none(home_dir, monkeypatch):
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: None)
    assert resolve_session_identity("@5") is None


def test_scan_empty_input_returns_empty_dict():
    assert scan_all_pane_identities([]) == {}


def test_scan_returns_resolved_dict(home_dir):
    _make_marker(home_dir, "@1", "sid-a")
    _make_marker(home_dir, "@2", "sid-b")
    result = scan_all_pane_identities(["@1", "@2"])
    assert result == {"@1": "sid-a", "@2": "sid-b"}


def test_scan_raises_on_duplicate(home_dir):
    _make_marker(home_dir, "@1", "same-sid")
    _make_marker(home_dir, "@2", "same-sid")
    with pytest.raises(DuplicateSessionIdError) as exc_info:
        scan_all_pane_identities(["@1", "@2"])
    assert "@1" in str(exc_info.value)
    assert "@2" in str(exc_info.value)


def test_scan_omits_windows_with_no_identity(home_dir, monkeypatch):
    _make_marker(home_dir, "@1", "sid-x")
    monkeypatch.setattr(session_watcher, "_get_pane_pid", lambda wid: None)
    result = scan_all_pane_identities(["@1", "@2"])
    assert result == {"@1": "sid-x"}
