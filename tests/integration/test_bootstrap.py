"""Integration tests for the bootstrap module.

Covers BootstrapResult dataclass, provider settings, stuck-prompt healing,
session map management, and outbound verification.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccgram.bootstrap import (
    BootstrapResult,
    _ensure_session_map_entry,
    _heal_session_map,
    _heal_stuck_prompts,
    _verify_outbound,
    ensure_provider_settings,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# BootstrapResult dataclass
# ---------------------------------------------------------------------------


def test_bootstrap_result_dataclass():
    r = BootstrapResult(success=True, session_id="s1", window_id="w1", topic_id=42)
    assert r.success is True
    assert r.session_id == "s1"
    assert r.window_id == "w1"
    assert r.topic_id == 42
    assert r.errors == []
    assert r.healed == []


# ---------------------------------------------------------------------------
# ensure_provider_settings
# ---------------------------------------------------------------------------


def test_ensure_provider_settings_claude_creates_new(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ensure_provider_settings()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["bypassPermissions"] is True
    assert settings["skipDangerousModePermissionPrompt"] is True


def test_ensure_provider_settings_claude_merges_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(json.dumps({"theme": "dark", "model": "opus"}))
    ensure_provider_settings()
    settings = json.loads((claude_dir / "settings.json").read_text())
    assert settings["theme"] == "dark"
    assert settings["model"] == "opus"
    assert settings["bypassPermissions"] is True
    assert settings["skipDangerousModePermissionPrompt"] is True


def test_ensure_provider_settings_preserves_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}
    (claude_dir / "settings.json").write_text(json.dumps(existing))
    ensure_provider_settings()
    settings = json.loads((claude_dir / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert settings["bypassPermissions"] is True


# ---------------------------------------------------------------------------
# _heal_stuck_prompts
# ---------------------------------------------------------------------------


def _make_pane_mock(text: str) -> MagicMock:
    m = MagicMock()
    m.stdout = text
    return m


async def test_heal_stuck_prompts_trust_prompt():
    send_args: list[list] = []

    def fake_run(cmd, **kw):
        if "capture-pane" in cmd:
            return _make_pane_mock("Do you want to trust this folder?\nYes, I trust the authors")
        send_args.append(cmd)
        return MagicMock()

    with patch("ccgram.bootstrap.subprocess.run", side_effect=fake_run):
        healed = await _heal_stuck_prompts("@5")

    assert "trust_folder_prompt" in healed
    assert any("Enter" in c for c in send_args)


async def test_heal_stuck_prompts_bypass_prompt():
    send_args: list[list] = []

    def fake_run(cmd, **kw):
        if "capture-pane" in cmd:
            return _make_pane_mock("No, exit\nYes, I accept the terms of service")
        send_args.append(cmd)
        return MagicMock()

    with patch("ccgram.bootstrap.subprocess.run", side_effect=fake_run):
        healed = await _heal_stuck_prompts("@6")

    assert "bypass_prompt" in healed
    assert any("2" in c for c in send_args)


async def test_heal_stuck_prompts_clean_prompt():
    send_args: list[list] = []

    def fake_run(cmd, **kw):
        if "capture-pane" in cmd:
            return _make_pane_mock("\u276f some normal claude prompt output here")
        send_args.append(cmd)
        return MagicMock()

    with patch("ccgram.bootstrap.subprocess.run", side_effect=fake_run):
        healed = await _heal_stuck_prompts("@7")

    assert healed == []
    assert send_args == []


async def test_heal_stuck_prompts_rate_limit():
    send_args: list[list] = []

    def fake_run(cmd, **kw):
        if "capture-pane" in cmd:
            return _make_pane_mock("You have hit your limit. Visit /upgrade to continue.")
        send_args.append(cmd)
        return MagicMock()

    with patch("ccgram.bootstrap.subprocess.run", side_effect=fake_run):
        healed = await _heal_stuck_prompts("@8")

    assert "rate_limit_prompt" in healed
    assert len(send_args) == 1  # sends Enter to clear the prompt


# ---------------------------------------------------------------------------
# _ensure_session_map_entry
# ---------------------------------------------------------------------------


async def test_ensure_session_map_creates_entry(state_dir):
    marker = {
        "session_id": "abc-123",
        "cwd": "/home/peter/projects/foo",
        "transcript_path": "/tmp/t.jsonl",
        "window_name": "foo",
        "provider": "claude",
    }
    await _ensure_session_map_entry("@10", marker)
    data = json.loads((state_dir / "session_map.json").read_text())
    assert "ccgram:@10" in data
    assert data["ccgram:@10"]["session_id"] == "abc-123"
    assert data["ccgram:@10"]["cwd"] == "/home/peter/projects/foo"


async def test_ensure_session_map_skips_valid_entry(state_dir):
    existing = {"ccgram:@11": {"session_id": "existing-id", "cwd": "/original"}}
    (state_dir / "session_map.json").write_text(json.dumps(existing))
    marker = {"session_id": "new-id", "cwd": "/replacement", "transcript_path": "", "window_name": "", "provider": ""}
    await _ensure_session_map_entry("@11", marker)
    data = json.loads((state_dir / "session_map.json").read_text())
    assert data["ccgram:@11"]["session_id"] == "existing-id"


# ---------------------------------------------------------------------------
# _heal_session_map
# ---------------------------------------------------------------------------


async def test_heal_session_map_from_markers(state_dir):
    markers = [
        {"window_id": "@1", "session_id": "s1", "cwd": "/a", "transcript_path": "/t1", "window_name": "a", "provider": "claude"},
        {"window_id": "@2", "session_id": "s2", "cwd": "/b", "transcript_path": "/t2", "window_name": "b", "provider": "claude"},
        {"window_id": "@3", "session_id": "s3", "cwd": "/c", "transcript_path": "/t3", "window_name": "c", "provider": "codex"},
    ]
    with patch("ccgram.bootstrap.list_active_markers", return_value=markers):
        await _heal_session_map()
    data = json.loads((state_dir / "session_map.json").read_text())
    assert len(data) == 3
    assert data["ccgram:@1"]["session_id"] == "s1"
    assert data["ccgram:@3"]["provider"] == "codex"


# ---------------------------------------------------------------------------
# _verify_outbound
# ---------------------------------------------------------------------------


async def test_verify_outbound_success(state_dir):
    transcript = state_dir / "probe.jsonl"
    transcript.write_text("initial line\n")
    marker = {"transcript_path": str(transcript)}

    def fake_run(cmd, **kw):
        transcript.write_text("initial line\nextra content after probe\n")
        return MagicMock()

    with patch("ccgram.bootstrap.read_marker_for_window", return_value=marker):
        with patch("ccgram.bootstrap.subprocess.run", side_effect=fake_run):
            result = await _verify_outbound("@20", "s-abc", timeout=6)

    assert result is True


async def test_verify_outbound_timeout(state_dir):
    transcript = state_dir / "probe2.jsonl"
    transcript.write_text("unchanged content")
    marker = {"transcript_path": str(transcript)}

    with patch("ccgram.bootstrap.read_marker_for_window", return_value=marker):
        with patch("ccgram.bootstrap.subprocess.run", return_value=MagicMock()):
            result = await _verify_outbound("@21", "s-def", timeout=2)

    assert result is False
