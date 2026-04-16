#!/usr/bin/env python3
"""PreToolUse hook -- blocks writes outside the session cwd.

Receives tool invocation JSON on stdin. Exits 2 (block) if the target path
is outside the session cwd and not on the allow-list. Exits 0 otherwise.

On internal error: logs to ~/.ccgram/hooks/errors.log and exits 0 (fail open).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path


def _error_log(msg: str) -> None:
    log = Path.home() / ".ccgram" / "hooks" / "errors.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] check-write-path: {msg}\n")
    except Exception:
        pass


def _load_config() -> dict:
    cfg_path = Path.home() / ".ccgram" / "hooks" / "write-path-config.json"
    try:
        if cfg_path.exists():
            return json.loads(cfg_path.read_text())
    except Exception as exc:
        _error_log(f"config load error: {exc}")
    return {}


def _resolve(p: str, cwd: str) -> Path:
    path = Path(p)
    if not path.is_absolute():
        path = Path(cwd) / path
    return path.resolve()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)

        tool_name = payload.get("tool_name", "")
        if tool_name not in {"Write", "Edit", "NotebookEdit"}:
            return 0

        tool_input = payload.get("tool_input") or payload.get("input") or {}
        target_str = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        )
        if not target_str:
            return 0

        cwd = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()
        target = _resolve(target_str, cwd)
        session_cwd = Path(cwd).resolve()

        config = _load_config()
        extra_allow = [
            Path(p).expanduser().resolve() for p in config.get("allow_paths", [])
        ]

        home = Path.home()
        allow_prefixes = [
            home / ".ccgram",
            home / ".claude",
            Path("/tmp"),
            home / "projects" / "shared",
        ] + extra_allow

        try:
            target.relative_to(session_cwd)
            return 0
        except ValueError:
            pass

        for prefix in allow_prefixes:
            try:
                target.relative_to(prefix)
                return 0
            except ValueError:
                continue

        msg = (
            f"WRITE BLOCKED: '{target}' is outside the session cwd '{session_cwd}'.\n"
            f"Agents must write only within their own project directory.\n"
            f"Correct action: write to a path under {session_cwd}\n"
            f"Allowed exceptions: ~/.ccgram/, ~/.claude/, /tmp/\n"
        )
        sys.stderr.write(msg)
        return 2

    except Exception as exc:
        _error_log(f"unhandled error: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
