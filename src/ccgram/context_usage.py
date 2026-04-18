"""Context usage tracker — proactive compaction and memory flush for Claude Code sessions.

Reads the tail of a JSONL transcript to determine how much of the model's
context window has been consumed, then triggers a memory-flush prompt or
``/compact`` command when usage crosses configured thresholds.

Key components:
  - get_latest_usage(transcript_path) -> (usage_dict | None, model_label | None)
  - determine_action(usage, model, state) -> "flush" | "compact" | None
  - ContextUsageTracker  — per-window state + cooldown logic
"""

import json
import time
from dataclasses import dataclass, field

import structlog

from .topic_state_registry import topic_state

logger = structlog.get_logger()

# ── Model context window sizes (input tokens) ─────────────────────────────

MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "opus": 1_000_000,
    "sonnet": 200_000,
    "haiku": 200_000,
}

# ── Default thresholds / timing ───────────────────────────────────────────

FLUSH_THRESHOLD: float = 0.65    # Send memory flush prompt at 65%
COMPACT_THRESHOLD: float = 0.75  # Send /compact at 75%
COOLDOWN_SECS: float = 1800.0     # Minimum gap between successive actions
CHECK_INTERVAL_SECS: float = 30.0  # Minimum gap between transcript reads

# ── Transcript tail read size ─────────────────────────────────────────────

_TAIL_BYTES = 32 * 1024

# ── Memory flush prompt ───────────────────────────────────────────────────

MEMORY_FLUSH_PROMPT = (
    "Before continuing, please save your progress to persistent memory. "
    "Write a concise session log entry (to your session-log or MEMORY.md as "
    "configured in CLAUDE.md) covering: (1) key decisions made this session, "
    "(2) current implementation state and what's in progress, "
    "(3) any blockers or open questions, "
    "(4) what should be done next. "
    "Keep it brief but complete enough to resume cold. "
    "After writing, confirm with a short summary of what you saved."
)


# ── Model normalisation ───────────────────────────────────────────────────

_MODEL_MAP: dict[str, str] = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
}


def _normalise_model(model: str | None) -> str | None:
    if not model or model == "<synthetic>":
        return None
    for prefix, label in _MODEL_MAP.items():
        if model.startswith(prefix):
            return label
    return None


# ── Per-window state ──────────────────────────────────────────────────────


@dataclass
class ContextUsageState:
    """Mutable state tracked per tmux window."""

    last_check: float = 0.0        # monotonic time of last transcript read
    last_flush_action: float = 0.0  # monotonic time of last flush sent
    last_compact_action: float = 0.0  # monotonic time of last /compact sent
    last_usage_pct: float | None = None  # last measured usage fraction (0-1)
    model_label: str | None = None  # last detected model label


# ── Tracker ───────────────────────────────────────────────────────────────


class ContextUsageTracker:
    """Per-window context usage tracker with cooldown logic."""

    def __init__(
        self,
        flush_threshold: float = FLUSH_THRESHOLD,
        compact_threshold: float = COMPACT_THRESHOLD,
        cooldown_secs: float = COOLDOWN_SECS,
        check_interval_secs: float = CHECK_INTERVAL_SECS,
    ) -> None:
        self._states: dict[str, ContextUsageState] = {}
        self.flush_threshold = flush_threshold
        self.compact_threshold = compact_threshold
        self.cooldown_secs = cooldown_secs
        self.check_interval_secs = check_interval_secs

    def get_state(self, window_id: str) -> ContextUsageState:
        if window_id not in self._states:
            self._states[window_id] = ContextUsageState()
        return self._states[window_id]

    def clear_window(self, window_id: str) -> None:
        self._states.pop(window_id, None)

    def should_check(self, window_id: str) -> bool:
        state = self.get_state(window_id)
        return (time.monotonic() - state.last_check) >= self.check_interval_secs

    def is_in_cooldown(self, window_id: str) -> bool:
        state = self.get_state(window_id)
        now = time.monotonic()
        last_action = max(state.last_flush_action, state.last_compact_action)
        return (now - last_action) < self.cooldown_secs

    def _flushed_within_cooldown(self, window_id: str) -> bool:
        state = self.get_state(window_id)
        return (time.monotonic() - state.last_flush_action) < self.cooldown_secs

    def record_check(self, window_id: str, usage_pct: float | None, model: str | None) -> None:
        state = self.get_state(window_id)
        state.last_check = time.monotonic()
        state.last_usage_pct = usage_pct
        state.model_label = model

    def record_flush(self, window_id: str) -> None:
        state = self.get_state(window_id)
        state.last_flush_action = time.monotonic()

    def record_compact(self, window_id: str) -> None:
        state = self.get_state(window_id)
        state.last_compact_action = time.monotonic()

    def determine_action(
        self,
        usage: dict | None,
        model: str | None,
        window_id: str,
    ) -> str | None:
        """Return "flush", "compact", or None based on usage vs thresholds."""
        if not usage:
            return None

        limit = MODEL_CONTEXT_LIMITS.get(model or "", MODEL_CONTEXT_LIMITS["sonnet"])
        context_size = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        if context_size <= 0 or limit <= 0:
            return None

        pct = context_size / limit

        if pct >= self.compact_threshold:
            if self._flushed_within_cooldown(window_id):
                return "compact"
            return "flush"

        if pct >= self.flush_threshold:
            return "flush"

        return None


# ── Module-level singleton ────────────────────────────────────────────────

context_usage_tracker = ContextUsageTracker()


# ── Cleanup registration ──────────────────────────────────────────────────


@topic_state.register("window")
def _clear_context_usage_state(window_id: str) -> None:
    context_usage_tracker.clear_window(window_id)


# ── Transcript parser ─────────────────────────────────────────────────────


def get_latest_usage(transcript_path: str) -> tuple[dict | None, str | None]:
    """Return (usage_dict, model_label) from the tail of a JSONL transcript.

    Reads the last 32 KB of the file, splits into lines, and scans in REVERSE
    for the first type=="assistant" entry that has a non-zero input_tokens.
    Context size = input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
    Never raises — returns (None, None) on any error.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            read_size = min(_TAIL_BYTES, size)
            fh.seek(-read_size, 2)
            raw = fh.read(read_size)

        lines = raw.decode("utf-8", errors="replace").splitlines()

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue

            if entry.get("type") != "assistant":
                continue

            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            if (usage.get("input_tokens") or 0) <= 0:
                continue

            raw_model = message.get("model") or entry.get("model")
            model_label = _normalise_model(raw_model)

            return usage, model_label

    except OSError:
        pass
    except Exception:
        logger.debug("context_usage.get_latest_usage failed", exc_info=True)

    return None, None
