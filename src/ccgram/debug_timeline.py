"""Debug timeline logger — structured JSONL event recording for observability.

Records every significant event touching a CCGram window (sends, transcript
messages, hook events, injections, pane output) to a date-rolled JSONL file
at ~/.ccgram/debug/timeline-YYYY-MM-DD.jsonl.

Disabled entirely when CCGRAM_DEBUG_ENABLED=0.

Usage:
    from .debug_timeline import get_timeline

    await get_timeline().log("send.attempt", "@19", "james", {"text": "..."})

Module-level singleton: ``timeline`` — use ``get_timeline()`` accessor.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import structlog

logger = structlog.get_logger()


class TimelineLogger:
    """Async, date-rolling JSONL timeline logger.

    Thread-safe via asyncio.Lock. Silent no-op when disabled.
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialise the timeline logger.

        Args:
            base_dir: Directory for timeline-YYYY-MM-DD.jsonl files.
        """
        self._base_dir = base_dir
        self._enabled = os.environ.get("CCGRAM_DEBUG_ENABLED", "1") not in (
            "0",
            "false",
            "False",
        )
        self._lock = asyncio.Lock()

    def _today_path(self) -> Path:
        """Return today's timeline file path (UTC date)."""
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self._base_dir / f"timeline-{date_str}.jsonl"

    async def log(
        self,
        event_type: str,
        window_id: str,
        window_name: str,
        data: dict[str, Any],
    ) -> None:
        """Append an event line to today's timeline file.

        Never raises — all exceptions are swallowed to prevent observability
        from breaking the main flow.

        Args:
            event_type: Event type string, e.g. "send.attempt", "transcript", "hook".
            window_id: Tmux window ID, e.g. "@19". Empty string if unknown.
            window_name: Human-readable window name, e.g. "james". Empty if unknown.
            data: Arbitrary dict of event-specific fields.
        """
        if not self._enabled:
            return

        try:
            import json

            ts = (
                datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{datetime.now(tz=timezone.utc).microsecond // 1000:03d}Z"
            )

            line = json.dumps(
                {
                    "ts": ts,
                    "window_id": window_id,
                    "window_name": window_name,
                    "event_type": event_type,
                    "data": data,
                },
                ensure_ascii=False,
            )

            async with self._lock:
                self._base_dir.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(
                    self._today_path(), "a", encoding="utf-8"
                ) as f:
                    await f.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.debug("timeline.log error (suppressed): %s", exc)

    async def cleanup_old(self, retention_days: int = 2) -> None:
        """Delete timeline-*.jsonl and terminal-*.log files older than retention_days.

        Called on CCGram startup. Silently no-ops if directory doesn't exist.

        Args:
            retention_days: Delete files with mtime older than this many days.
        """
        if not self._base_dir.exists():
            return

        import time

        cutoff = time.time() - retention_days * 86400

        try:
            for pattern in ("timeline-*.jsonl", "terminal-*.log"):
                for f in self._base_dir.glob(pattern):
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink(missing_ok=True)
                            logger.debug("timeline: deleted old file %s", f.name)
                    except OSError as exc:
                        logger.debug("timeline: could not delete %s: %s", f.name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("timeline.cleanup_old error (suppressed): %s", exc)


# Module-level singleton
timeline = TimelineLogger(Path.home() / ".ccgram" / "debug")


def get_timeline() -> TimelineLogger:
    """Return the module-level TimelineLogger singleton."""
    return timeline
