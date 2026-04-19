"""Structured audit log for Telegram API calls that modify state.

Appends JSONL entries to ~/.ccgram/telegram-audit.jsonl before each
modifying Bot API call. Rotates at 10MB. Never raises.

Usage:
    from .telegram_audit import log_action

    log_action("edit_forum_topic", chat_id, thread_id,
               window_id=window_id,
               payload={"new_name": new_name, "old_name": old_name},
               reason="startup_cleanup")
"""

import inspect
import json
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

_AUDIT_PATH = Path.home() / ".ccgram" / "telegram-audit.jsonl"
_MAX_SIZE = 10 * 1024 * 1024  # 10MB — rotate when exceeded


def log_action(
    action: str,
    chat_id: int,
    thread_id: int | None,
    *,
    window_id: str = "",
    payload: dict | None = None,
    reason: str = "",
    caller: str = "",
) -> None:
    """Append an audit entry. Never raises.

    Args:
        action:    API action name, e.g. "edit_forum_topic", "send_message", "edit_message"
        chat_id:   Telegram chat ID
        thread_id: Forum topic thread ID (None for non-topic messages)
        window_id: tmux window ID that triggered the action
        payload:   Key fields from the API call (text preview, new name, etc.)
        reason:    Code path that triggered this, e.g. "startup_cleanup", "subagent_suffix"
        caller:    module:function that called (auto-detected via inspect if empty)
    """
    try:
        if not caller:
            frame = inspect.stack()[1]
            caller = f"{Path(frame.filename).stem}:{frame.function}"

        entry: dict = {
            "ts": time.time(),
            "action": action,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "window_id": window_id,
            "reason": reason,
            "caller": caller,
        }
        if payload:
            entry["payload"] = payload

        # Ensure directory exists
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Rotate if too large
        if _AUDIT_PATH.exists() and _AUDIT_PATH.stat().st_size > _MAX_SIZE:
            rotated = _AUDIT_PATH.with_suffix(".jsonl.1")
            if rotated.exists():
                rotated.unlink()
            _AUDIT_PATH.rename(rotated)

        with open(_AUDIT_PATH, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        logger.debug("telegram_audit: write failed", exc_info=True)
