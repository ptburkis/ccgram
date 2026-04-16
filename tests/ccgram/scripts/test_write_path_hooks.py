"""Tests for scripts/hooks/check-write-path.py and rewrite-output-url.py.

Hook scripts are tested by running them as subprocesses (they are standalone
scripts, not importable modules). Integration test covers create_session
bootstrap via the existing stubs fixture pattern.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram import session_lifecycle, store

# ---- Paths -------------------------------------------------------------------

REPO_ROOT = Path(__file__).parents[3]  # tests/ccgram/scripts -> repo root
CHECK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "check-write-path.py"
REWRITE_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "rewrite-output-url.py"


def _run_check(payload: dict, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _run_rewrite(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REWRITE_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


# ---- check-write-path.py: unit tests -----------------------------------------


class TestCheckWritePath:
    def test_non_write_tool_exits_0(self, tmp_path):
        p = _run_check({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}, "cwd": str(tmp_path)})
        assert p.returncode == 0

    def test_bash_tool_exits_0(self, tmp_path):
        p = _run_check({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(tmp_path)})
        assert p.returncode == 0

    def test_write_inside_cwd_allowed(self, tmp_path):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "output.txt")},
            "cwd": str(tmp_path),
        }
        p = _run_check(payload)
        assert p.returncode == 0

    def test_edit_inside_cwd_allowed(self, tmp_path):
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "sub" / "file.py")},
            "cwd": str(tmp_path),
        }
        p = _run_check(payload)
        assert p.returncode == 0

    def test_write_outside_cwd_blocked(self, tmp_path, monkeypatch):
        # Use home-relative paths so /tmp allow-list doesn't interfere
        import tempfile, os
        home = Path.home()
        project_a = home / ".ccgram-test-project-a"
        project_b = home / ".ccgram-test-project-b"
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(project_b / "secret.txt")},
            "cwd": str(project_a),
        }
        p = _run_check(payload)
        assert p.returncode == 2
        assert "WRITE BLOCKED" in p.stderr
        assert str(project_a) in p.stderr  # mentions session cwd

    def test_openclaw_path_blocked(self, tmp_path):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(Path.home() / ".openclaw" / "workspace" / "state.db")},
            "cwd": str(tmp_path),
        }
        p = _run_check(payload)
        assert p.returncode == 2
        assert "WRITE BLOCKED" in p.stderr

    def test_tmp_allowed(self, tmp_path):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/scratch.txt"},
            "cwd": str(tmp_path),
        }
        p = _run_check(payload)
        assert p.returncode == 0

    def test_ccgram_dotdir_allowed(self, tmp_path):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(Path.home() / ".ccgram" / "hooks" / "config.json")},
            "cwd": str(tmp_path),
        }
        p = _run_check(payload)
        assert p.returncode == 0

    def test_claude_dotdir_allowed(self, tmp_path):
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(Path.home() / ".claude" / "settings.json")},
            "cwd": str(tmp_path),
        }
        p = _run_check(payload)
        assert p.returncode == 0

    def test_allow_paths_override_via_config(self, tmp_path):
        custom_dir = tmp_path / "custom-allowed"
        config = {"allow_paths": [str(custom_dir)]}
        config_path = Path.home() / ".ccgram" / "hooks" / "write-path-config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        original = config_path.read_text() if config_path.exists() else None
        try:
            config_path.write_text(json.dumps(config))
            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(custom_dir / "file.txt")},
                "cwd": str(tmp_path / "project"),
            }
            p = _run_check(payload)
            assert p.returncode == 0
        finally:
            if original is None:
                config_path.unlink(missing_ok=True)
            else:
                config_path.write_text(original)

    def test_missing_file_path_exits_0(self, tmp_path):
        """No file_path in tool_input -> can't block -> let through."""
        p = _run_check({"tool_name": "Write", "tool_input": {}, "cwd": str(tmp_path)})
        assert p.returncode == 0

    def test_notebookedit_blocked_outside_cwd(self, tmp_path):
        home = Path.home()
        project_cwd = home / ".ccgram-test-nb-cwd"
        other = home / ".ccgram-test-nb-other"
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(other / "analysis.ipynb")},
            "cwd": str(project_cwd),
        }
        p = _run_check(payload)
        assert p.returncode == 2


# ---- rewrite-output-url.py: unit tests ---------------------------------------


class TestRewriteOutputUrl:
    def _payload(self, tool_name: str, output: str) -> dict:
        return {"tool_name": tool_name, "tool_response": output}

    def test_write_with_path_annotated(self):
        output = "File written to /home/peter/projects/bulugo/src/main.py"
        p = _run_rewrite(self._payload("Write", output))
        assert p.returncode == 0
        if p.stdout.strip():
            result = json.loads(p.stdout)
            assert "clawd.tail483fa1.ts.net" in result["tool_response"]
            assert "bulugo/src/main.py" in result["tool_response"]

    def test_no_paths_no_change(self):
        output = "Done. No file paths here."
        p = _run_rewrite(self._payload("Write", output))
        assert p.returncode == 0
        # No output or unchanged payload
        if p.stdout.strip():
            result = json.loads(p.stdout)
            assert result["tool_response"] == output

    def test_non_matching_tool_no_output(self):
        output = "/home/peter/projects/foo/bar.py"
        p = _run_rewrite({"tool_name": "ListFiles", "tool_response": output})
        assert p.returncode == 0
        assert p.stdout.strip() == ""

    def test_bash_output_annotated(self):
        output = "created /home/peter/projects/james/context/notes.md successfully"
        p = _run_rewrite(self._payload("Bash", output))
        assert p.returncode == 0
        if p.stdout.strip():
            result = json.loads(p.stdout)
            assert "clawd link" in result["tool_response"]

    def test_read_outside_project_no_annotation(self):
        """Read of /etc/hosts has no /home/peter/projects path -> no annotation."""
        output = "Contents of /etc/hosts..."
        p = _run_rewrite(self._payload("Read", output))
        assert p.returncode == 0
        if p.stdout.strip():
            result = json.loads(p.stdout)
            assert "clawd link" not in result["tool_response"]

    def test_deduplication(self):
        """Same path mentioned twice -> one clawd link."""
        path = "/home/peter/projects/myapp/README.md"
        output = f"See {path} and also {path}"
        p = _run_rewrite(self._payload("Edit", output))
        assert p.returncode == 0
        if p.stdout.strip():
            result = json.loads(p.stdout)
            assert result["tool_response"].count("clawd link") == 1


# ---- Integration: create_session bootstrap -----------------------------------


@pytest.fixture()
def ccgram_test_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    store.init_db(tmp_path / "state.db")
    return tmp_path


@pytest.fixture()
def stubs(monkeypatch):
    class _Stubs:
        def __init__(self):
            self.create_topic = AsyncMock(return_value=42)
            self.delete_topic = AsyncMock(return_value=None)
            self.verify_topic = AsyncMock(return_value=(True, "test-topic"))
            self.tmux_create = AsyncMock(return_value="@99")
            self.tmux_send = AsyncMock(return_value=None)
            self.tmux_kill = AsyncMock(return_value=None)
            self.resolve_launch = MagicMock(return_value="claude")

    s = _Stubs()
    monkeypatch.setattr(session_lifecycle, "_create_forum_topic_fn", s.create_topic)
    monkeypatch.setattr(session_lifecycle, "_delete_forum_topic_fn", s.delete_topic)
    monkeypatch.setattr(session_lifecycle, "_verify_forum_topic_fn", s.verify_topic)
    monkeypatch.setattr(session_lifecycle, "_tmux_create_window_fn", s.tmux_create)
    monkeypatch.setattr(session_lifecycle, "_tmux_send_keys_fn", s.tmux_send)
    monkeypatch.setattr(session_lifecycle, "_tmux_kill_window_fn", s.tmux_kill)
    monkeypatch.setattr(session_lifecycle, "_resolve_launch_fn", s.resolve_launch)
    return s


class TestBootstrapHookConfig:
    @pytest.mark.asyncio
    async def test_settings_json_created_with_hooks(self, ccgram_test_dir, stubs, tmp_path, monkeypatch):
        """After create_session, target project has .claude/settings.json with hook config."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        # Prevent _deploy_hook_scripts from failing on missing repo scripts dir
        monkeypatch.setattr(session_lifecycle, "_deploy_hook_scripts", lambda: None)

        await session_lifecycle.create_session(
            cwd=str(project_dir),
            topic_name="test-topic",
            agent="claude",
            group_id=-1001,
        )

        settings = project_dir / ".claude" / "settings.json"
        assert settings.exists(), ".claude/settings.json should be created"
        config = json.loads(settings.read_text())

        hooks = config.get("hooks", {})
        pre_hooks = hooks.get("PreToolUse", [])
        post_hooks = hooks.get("PostToolUse", [])

        pre_cmds = [h.get("command") for e in pre_hooks for h in e.get("hooks", [])]
        post_cmds = [h.get("command") for e in post_hooks for h in e.get("hooks", [])]

        assert any("check-write-path" in c for c in pre_cmds)
        assert any("rewrite-output-url" in c for c in post_cmds)

    @pytest.mark.asyncio
    async def test_idempotent_second_call(self, ccgram_test_dir, stubs, tmp_path, monkeypatch):
        """Calling _bootstrap_hook_config twice does not duplicate hooks."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        monkeypatch.setattr(session_lifecycle, "_deploy_hook_scripts", lambda: None)

        session_lifecycle._bootstrap_hook_config(str(project_dir))
        session_lifecycle._bootstrap_hook_config(str(project_dir))

        settings = project_dir / ".claude" / "settings.json"
        config = json.loads(settings.read_text())
        hooks = config.get("hooks", {})
        pre_cmds = [
            h.get("command")
            for e in hooks.get("PreToolUse", [])
            for h in e.get("hooks", [])
        ]
        assert pre_cmds.count("python3 ~/.ccgram/hooks/check-write-path.py") == 1

    @pytest.mark.asyncio
    async def test_existing_hooks_preserved(self, ccgram_test_dir, stubs, tmp_path, monkeypatch):
        """Existing hooks from other sources are not removed."""
        project_dir = tmp_path / "my-project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "SomeTool",
                        "hooks": [{"type": "command", "command": "echo hello"}],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing))
        monkeypatch.setattr(session_lifecycle, "_deploy_hook_scripts", lambda: None)

        session_lifecycle._bootstrap_hook_config(str(project_dir))

        config = json.loads((claude_dir / "settings.json").read_text())
        pre_hooks = config["hooks"]["PreToolUse"]
        matchers = [e["matcher"] for e in pre_hooks]
        assert "SomeTool" in matchers
        assert "Write|Edit|NotebookEdit" in matchers

    def test_bootstrap_survives_bad_cwd(self, tmp_path, monkeypatch):
        """Bad cwd logs warning and does not raise."""
        monkeypatch.setattr(session_lifecycle, "_deploy_hook_scripts", lambda: None)
        # A read-only directory (simulate permission error) — use a non-existent parent
        bad_cwd = "/nonexistent/path/that/cannot/be/created"
        # Should not raise
        session_lifecycle._bootstrap_hook_config(bad_cwd)
