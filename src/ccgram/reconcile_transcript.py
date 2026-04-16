"""Transcript reconciliation tool — compare JSONL assistant messages vs Telegram delivery.

Reads the session's JSONL from ``--since`` forward, extracts assistant text turns,
fetches the same window from Telegram via MTProto, and fuzzy-matches to identify
messages that the transcript watcher never shipped.

Optionally re-sends missing messages via the Bot API with a ``[recovered] `` prefix.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class JsonlAssistantMessage:
    """An assistant text message extracted from the JSONL transcript."""

    timestamp: datetime
    text: str


@dataclass
class ReconcileReport:
    """Result of a reconcile run."""

    window: str
    since: datetime
    matched: list[JsonlAssistantMessage] = field(default_factory=list)
    missing: list[JsonlAssistantMessage] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)  # texts from Telegram not in jsonl

    def to_dict(self) -> dict[str, Any]:
        def _fmt_dt(dt: datetime) -> str:
            return dt.isoformat()

        return {
            "window": self.window,
            "since": _fmt_dt(self.since),
            "matched_count": len(self.matched),
            "missing": [
                {"timestamp": _fmt_dt(m.timestamp), "text_preview": m.text[:80]}
                for m in self.missing
            ],
            "spurious": self.spurious,
        }


# ---------------------------------------------------------------------------
# JSONL reader
# ---------------------------------------------------------------------------

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RE_EFFORT = re.compile(r"\s*\[(?:low|medium|high|bg)\]\s*$", re.IGNORECASE)


def _normalise(text: str) -> str:
    """Whitespace-normalise and strip noise suffixes for fuzzy comparison."""
    text = _RE_ANSI.sub("", text)
    text = _RE_EFFORT.sub("", text)
    return " ".join(text.split())


def read_jsonl_assistant_messages(
    jsonl_path: Path,
    since: datetime,
) -> list[JsonlAssistantMessage]:
    """Parse ``jsonl_path`` and return assistant text-only messages >= ``since``.

    Skips tool_use, thinking, and tool_result blocks.  Only extracts content
    blocks of type ``text`` from ``type=assistant`` entries.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    results: list[JsonlAssistantMessage] = []
    try:
        raw_lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return results

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("type") != "assistant":
            continue

        ts_raw = entry.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < since:
            continue

        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "").strip()
                if t:
                    text_parts.append(_RE_ANSI.sub("", t))

        if text_parts:
            combined = "\n".join(text_parts).strip()
            if combined:
                results.append(JsonlAssistantMessage(timestamp=ts, text=combined))

    return results


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------


def _fuzzy_ratio(a: str, b: str) -> float:
    """Simple character-overlap ratio using the first 100 normalised chars."""
    import difflib

    na = _normalise(a)[:100]
    nb = _normalise(b)[:100]
    if not na or not nb:
        return 0.0
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    return sm.ratio()


def match_messages(
    jsonl_msgs: list[JsonlAssistantMessage],
    telegram_texts: list[str],
    *,
    threshold: float = 0.90,
) -> tuple[list[JsonlAssistantMessage], list[JsonlAssistantMessage], list[str]]:
    """Fuzzy-match jsonl messages against Telegram texts.

    Returns:
        (matched, missing, spurious) where:
          matched  — jsonl messages found in Telegram
          missing  — jsonl messages NOT found in Telegram
          spurious — Telegram texts not matched to any jsonl message
    """
    unmatched_tg = list(telegram_texts)
    matched: list[JsonlAssistantMessage] = []
    missing: list[JsonlAssistantMessage] = []

    for jmsg in jsonl_msgs:
        best_ratio = 0.0
        best_idx = -1
        for i, tg_text in enumerate(unmatched_tg):
            r = _fuzzy_ratio(jmsg.text, tg_text)
            if r > best_ratio:
                best_ratio = r
                best_idx = i
        if best_ratio >= threshold and best_idx >= 0:
            matched.append(jmsg)
            unmatched_tg.pop(best_idx)
        else:
            missing.append(jmsg)

    return matched, missing, unmatched_tg


# ---------------------------------------------------------------------------
# Bot API sender
# ---------------------------------------------------------------------------


def send_recovered_message(
    token: str,
    chat_id: int | str,
    thread_id: int | str,
    text: str,
    prefix: str = "[recovered] ",
) -> bool:
    """Send a single message via Bot API sendMessage.

    Returns True on success, False on failure (logs the error).
    Tries with Markdown parse_mode first; falls back to plain text.
    """
    full_text = prefix + text
    max_len = 4096
    if len(full_text) > max_len:
        full_text = full_text[: max_len - 1] + "\u2026"

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for parse_mode in ("Markdown", None):
        params: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_thread_id": str(thread_id),
            "text": full_text,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=15) as resp:
                result = json.loads(resp.read())
            if result.get("ok"):
                return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            logger.warning("reconcile_transcript.send_failed", error=str(exc))
            if parse_mode is not None:
                continue  # retry without markdown
            return False

    return False
