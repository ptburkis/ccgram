#!/usr/bin/env python3
"""PostToolUse hook -- annotates local paths in tool output with clawd hub URLs.

Reads tool response JSON on stdin. For Write/Edit/NotebookEdit/Read/Bash,
scans text output for /home/peter/projects/<proj>/<rest> paths and appends
a clawd link annotation. Never blocks; always exits 0.

On internal error: logs to ~/.ccgram/hooks/errors.log and exits 0 (fail open).
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

CLAWD_BASE = "https://clawd.tail483fa1.ts.net:8443/files"
PATH_RE = re.compile(r"/home/peter/projects/([^/\s\"']+)/([^\s\"']*)")
ACT_TOOLS = {"Write", "Edit", "NotebookEdit", "Read", "Bash"}


def _error_log(msg: str) -> None:
    log = Path.home() / ".ccgram" / "hooks" / "errors.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(
                f"[{datetime.datetime.now().isoformat()}] rewrite-output-url: {msg}\n"
            )
    except Exception:
        pass


def _annotate(text: str) -> str:
    """Return text with clawd link lines appended for each unique path found."""
    matches = PATH_RE.findall(text)
    if not matches:
        return text
    seen: set[str] = set()
    links: list[str] = []
    for proj, rest in matches:
        rest = rest.rstrip("/")
        key = f"{proj}/{rest}"
        if key in seen:
            continue
        seen.add(key)
        url = f"{CLAWD_BASE}/{proj}/{rest}" if rest else f"{CLAWD_BASE}/{proj}"
        links.append(f"\n\U0001f4a1 clawd link: {url}")
    if not links:
        return text
    return text + "".join(links)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)

        tool_name = payload.get("tool_name", "")
        if tool_name not in ACT_TOOLS:
            return 0

        # Claude Code PostToolUse result lives under various keys depending on version
        result = payload.get("tool_response") or payload.get("output") or payload.get("result") or ""
        if not isinstance(result, str):
            result = json.dumps(result)

        annotated = _annotate(result)
        if annotated != result:
            out = dict(payload)
            key = next(
                (k for k in ("tool_response", "output", "result") if k in payload),
                "output",
            )
            out[key] = annotated
            print(json.dumps(out))
        # If no change, print nothing — hook output is optional

    except Exception as exc:
        _error_log(f"unhandled error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
