"""Usage report — /usage Telegram command.

Reads context usage directly from JSONL transcripts for all active Claude
sessions, and parses the tmux pane for Codex rate-limit info.

Public API:
  generate_usage_report() -> str   — async, Telegram-ready output
"""

from __future__ import annotations

import asyncio
import re
import structlog

from .context_usage import MODEL_CONTEXT_LIMITS, get_latest_usage
from .session import session_manager
from . import store

logger = structlog.get_logger()

# Tokens above this fraction of the model limit get a warning emoji.
_WARN_THRESHOLD = 0.60
_CRIT_THRESHOLD = 0.80

# Maximum number of sessions to display (top by usage + all over threshold).
_MAX_SESSIONS = 5


def _format_tokens(n: int) -> str:
    """Format token count as e.g. '420K' or '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _session_display_name(window_id: str, topic_title: str | None) -> str:
    """Return a human-readable name for a session."""
    if topic_title:
        return topic_title
    return window_id


async def _read_session_usage(
    window_id: str,
    transcript_path: str,
) -> tuple[float | None, str | None, int | None, int | None]:
    """Return (usage_pct, model_label, context_tokens, limit) for a session.

    Runs the transcript read in a thread pool to avoid blocking the event loop.
    Returns (None, None, None, None) on any failure.
    """
    try:
        usage, model = await asyncio.to_thread(get_latest_usage, transcript_path)
    except Exception:
        logger.debug("usage_report: get_latest_usage failed", window_id=window_id)
        return None, None, None, None

    if not usage:
        return None, None, None, None

    model_key = model or "sonnet"
    limit = MODEL_CONTEXT_LIMITS.get(model_key, MODEL_CONTEXT_LIMITS["sonnet"])

    context_tokens = (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
    )
    if context_tokens <= 0 or limit <= 0:
        return None, model, None, None

    pct = context_tokens / limit
    return pct, model, context_tokens, limit


# ── Codex pane parsing ────────────────────────────────────────────────────────

# Patterns for lines in the Codex status bar:
#   Context window:  42% left
#   5h limit:  [██░░░] 78% left (resets 22:44)
#   Weekly limit:  [█░░░░] 56% left (resets Thursday on Apr 22)
_RE_CONTEXT = re.compile(r"Context window[:\s]+(\d+)%\s+left", re.IGNORECASE)
_RE_5H = re.compile(
    r"5h limit[:\s]+.*?(\d+)%\s+left(?:\s+\(resets\s+([^)]+)\))?", re.IGNORECASE
)
_RE_WEEKLY = re.compile(
    r"Weekly limit[:\s]+.*?(\d+)%\s+left(?:\s+\(resets\s+([^)]+)\))?", re.IGNORECASE
)


def _parse_codex_pane(pane_text: str) -> dict[str, str]:
    """Extract Codex rate-limit info from pane capture.

    Returns a dict with zero or more of:
      'context_pct_left', '5h_pct_left', '5h_resets',
      'weekly_pct_left', 'weekly_resets'
    """
    result: dict[str, str] = {}
    for line in pane_text.splitlines():
        m = _RE_CONTEXT.search(line)
        if m:
            result["context_pct_left"] = m.group(1)
        m = _RE_5H.search(line)
        if m:
            result["5h_pct_left"] = m.group(1)
            if m.group(2):
                result["5h_resets"] = m.group(2).strip()
        m = _RE_WEEKLY.search(line)
        if m:
            result["weekly_pct_left"] = m.group(1)
            if m.group(2):
                result["weekly_resets"] = m.group(2).strip()
    return result


async def _get_codex_info(window_id: str) -> dict[str, str]:
    """Capture the Codex pane and parse its rate-limit display."""
    try:
        from .tmux_manager import tmux_manager
        pane_text = await tmux_manager.capture_pane(window_id)
        if not pane_text:
            return {}
        return _parse_codex_pane(pane_text)
    except Exception:
        logger.debug("usage_report: codex pane capture failed", window_id=window_id)
        return {}


# ── Main report ───────────────────────────────────────────────────────────────


async def generate_usage_report() -> str:
    """Generate a usage report across all active sessions.

    Returns a Telegram-ready string.
    """
    with store.connect() as conn:
        active_sessions = store.list_sessions(conn, status="active")
        # Build session_id -> topic_title map from bindings
        bindings = store.list_topic_bindings(conn)

    binding_by_session: dict[str, str] = {
        b.session_id: b.topic_title for b in bindings
    }

    # Split sessions into claude vs codex
    claude_sessions = []
    codex_sessions = []
    for session in active_sessions:
        if not session.window_id:
            continue
        if session.agent == "codex":
            codex_sessions.append(session)
        else:
            claude_sessions.append(session)

    # ── Claude sessions ───────────────────────────────────────────────────────

    # Collect usage data (run all transcript reads concurrently)
    async def _collect(session):
        state = session_manager.get_window_state(session.window_id)
        transcript_path = state.transcript_path if state else ""
        if not transcript_path:
            return None
        pct, model, ctx_tokens, limit = await _read_session_usage(
            session.window_id, transcript_path
        )
        if pct is None:
            return None
        display_name = _session_display_name(
            session.window_id,
            binding_by_session.get(session.session_id),
        )
        return {
            "name": display_name,
            "pct": pct,
            "model": model or "?",
            "ctx_tokens": ctx_tokens,
            "limit": limit,
        }

    results = await asyncio.gather(*[_collect(s) for s in claude_sessions])
    session_data = [r for r in results if r is not None]

    # Sort descending by usage percentage
    session_data.sort(key=lambda x: x["pct"], reverse=True)

    # Select sessions to display: top N or any over threshold
    shown = []
    for item in session_data:
        if len(shown) < _MAX_SESSIONS or item["pct"] >= _WARN_THRESHOLD:
            shown.append(item)

    lines: list[str] = ["📊 Claude Usage"]

    if shown:
        lines.append("")
        lines.append("Context per session:")
        for item in shown:
            pct_int = int(item["pct"] * 100)
            model_label = (item["model"] or "?").capitalize()
            ctx_fmt = _format_tokens(item["ctx_tokens"] or 0)
            lim_fmt = _format_tokens(item["limit"] or 0)
            if item["pct"] >= _CRIT_THRESHOLD:
                flag = " 🔴"
            elif item["pct"] >= _WARN_THRESHOLD:
                flag = " ⚠️"
            else:
                flag = ""
            lines.append(
                f"  {item['name']} — {pct_int}%"
                f" ({model_label}, {ctx_fmt}/{lim_fmt}){flag}"
            )
    else:
        lines.append("")
        lines.append("No active Claude sessions with usage data.")

    # ── Codex sessions ────────────────────────────────────────────────────────

    for session in codex_sessions:
        window_id = session.window_id
        if not window_id:
            continue

        display_name = _session_display_name(
            window_id,
            binding_by_session.get(session.session_id),
        )
        codex_info = await _get_codex_info(window_id)

        lines.append("")
        lines.append(f"🟢 Codex ({display_name})")

        if codex_info.get("5h_pct_left"):
            resets = f" (resets {codex_info['5h_resets']})" if codex_info.get("5h_resets") else ""
            lines.append(f"  5h: {codex_info['5h_pct_left']}% left{resets}")

        if codex_info.get("weekly_pct_left"):
            resets = f" (resets {codex_info['weekly_resets']})" if codex_info.get("weekly_resets") else ""
            lines.append(f"  Weekly: {codex_info['weekly_pct_left']}% left{resets}")

        if codex_info.get("context_pct_left"):
            lines.append(f"  Context: {codex_info['context_pct_left']}% left")

        if not any(k in codex_info for k in ("5h_pct_left", "weekly_pct_left", "context_pct_left")):
            lines.append("  (no rate-limit data in pane)")

    return "\n".join(lines)
