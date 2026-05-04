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


# ---------------------------------------------------------------------------
# Tests for cwd-based resolution (Bug B guards) — added 2026-05-04
# ---------------------------------------------------------------------------

import json
import time
from pathlib import Path
from unittest.mock import patch


def _make_hook_marker_content(window_id: str, window_name: str, session_id: str) -> bytes:
    """Produce minimal JSONL content that contains the hook marker twice.

    jsonl_has_hook_marker requires >=2 occurrences to avoid false positives.
    The marker format: tmux key=ccgram:<window_id>, window_name=<wname>, session_id=<stem>
    """
    marker_line = (
        f"tmux key=ccgram:{window_id}, window_name={window_name}, session_id={session_id}"
    )
    line = json.dumps({"type": "system", "content": marker_line})
    return (line + "\n" + line + "\n").encode()


def _write_session_map(path: Path, entries: list[dict]) -> None:
    """Write a session_map.json from a list of dicts with keys:
    window_id, window_name, session_id, cwd
    """
    sm = {}
    for e in entries:
        key = f"ccgram:{e['window_id']}"
        sm[key] = {
            "session_id": e.get("session_id", ""),
            "window_name": e.get("window_name", ""),
            "cwd": e.get("cwd", "/home/peter/projects/testproj"),
            "provider": "claude",
            "transcript_path": "",
        }
    path.write_text(json.dumps(sm))


def _make_jsonl(tmp_path: Path, session_id: str, content: bytes = b"") -> Path:
    """Create a fake JSONL file under a claude project slug directory."""
    slug = "-home-peter-projects-testproj"
    proj_dir = tmp_path / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    p = proj_dir / f"{session_id}.jsonl"
    p.write_bytes(content)
    return p


def test_cwd_single_refuses_displacement_without_hook_marker(tmp_path, monkeypatch):
    """cwd-single: refuses to displace a live binding when JSONL lacks hook marker."""
    from ccgram import session_watcher, config as cfg

    # Point config at tmp_path
    monkeypatch.setattr(cfg.config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(cfg.config, "tmux_session_name", "ccgram")

    existing_sid = "sid-existing-abc"
    new_sid = "sid-new-xyz"
    window_id = "@7"
    window_name = "james"

    # One entry, cwd matches, has a live current_sid
    _write_session_map(tmp_path / "session_map.json", [
        {"window_id": window_id, "window_name": window_name,
         "session_id": existing_sid, "cwd": "/home/peter/projects/testproj"},
    ])

    # New JSONL has NO hook marker
    jsonl_path = _make_jsonl(tmp_path, new_sid, b"no marker here\n")
    content = jsonl_path.read_bytes()

    # Disable PTY marker path and env-marker path
    monkeypatch.setattr(session_watcher, "resolve_session_identity", lambda wid: None)
    with patch("ccgram.session_watcher._find_window_for_jsonl",
               wraps=session_watcher._find_window_for_jsonl) as _wrapped:
        # Disable pty_markers import inside the function
        import ccgram.pty_markers as _pty
        monkeypatch.setattr(_pty, "list_active_markers", lambda: [])
        result = session_watcher._find_window_for_jsonl(jsonl_path, content)

    assert result is None, "must refuse displacement when JSONL lacks hook marker"


def test_cwd_single_allows_legit_rotation_with_hook_marker(tmp_path, monkeypatch):
    """cwd-single: allows rotation when JSONL contains valid hook marker for the window."""
    from ccgram import session_watcher, config as cfg

    monkeypatch.setattr(cfg.config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(cfg.config, "tmux_session_name", "ccgram")

    existing_sid = "sid-existing-abc"
    new_sid = "sid-new-xyz"
    window_id = "@7"
    window_name = "james"

    _write_session_map(tmp_path / "session_map.json", [
        {"window_id": window_id, "window_name": window_name,
         "session_id": existing_sid, "cwd": "/home/peter/projects/testproj"},
    ])

    # New JSONL HAS the hook marker (>=2 occurrences)
    marker_content = _make_hook_marker_content(window_id, window_name, new_sid)
    jsonl_path = _make_jsonl(tmp_path, new_sid, marker_content)
    content = jsonl_path.read_bytes()

    monkeypatch.setattr(session_watcher, "resolve_session_identity", lambda wid: None)
    import ccgram.pty_markers as _pty
    monkeypatch.setattr(_pty, "list_active_markers", lambda: [])

    result = session_watcher._find_window_for_jsonl(jsonl_path, content)

    assert result is not None, "should match when hook marker is present"
    assert result[0] == window_id
    assert result[1] == window_name


def test_cwd_ambiguous_filters_to_marker_carriers(tmp_path, monkeypatch):
    """cwd-ambiguous: two windows share cwd; only the one with the marker is returned."""
    from ccgram import session_watcher, config as cfg

    monkeypatch.setattr(cfg.config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(cfg.config, "tmux_session_name", "ccgram")

    existing_sid_a = "sid-win-a"
    existing_sid_b = "sid-win-b"
    new_sid = "sid-new-qrs"
    window_id_a = "@8"
    window_id_b = "@9"
    window_name_a = "james"
    window_name_b = "bulugo"

    # Two entries, same cwd slug
    _write_session_map(tmp_path / "session_map.json", [
        {"window_id": window_id_a, "window_name": window_name_a,
         "session_id": existing_sid_a, "cwd": "/home/peter/projects/testproj"},
        {"window_id": window_id_b, "window_name": window_name_b,
         "session_id": existing_sid_b, "cwd": "/home/peter/projects/testproj"},
    ])

    # JSONL has marker for window_id_a only
    marker_content = _make_hook_marker_content(window_id_a, window_name_a, new_sid)
    jsonl_path = _make_jsonl(tmp_path, new_sid, marker_content)
    content = jsonl_path.read_bytes()

    monkeypatch.setattr(session_watcher, "resolve_session_identity", lambda wid: None)
    import ccgram.pty_markers as _pty
    monkeypatch.setattr(_pty, "list_active_markers", lambda: [])

    result = session_watcher._find_window_for_jsonl(jsonl_path, content)

    assert result is not None, "should find the marker-carrying candidate"
    assert result[0] == window_id_a, "should pick @8 (has the marker)"
