"""Usage report — /usage Telegram command.

Fetches account-level rate limits from one Claude Code window and one Codex
window via scrape-usage.sh. One answer per provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import structlog

logger = structlog.get_logger()

_SCRAPE_SCRIPT = os.environ.get(
    "CCGRAM_SCRAPE_SCRIPT",
    "/home/peter/ccgram-dashboard/scrape-usage.sh",
)
_SCRAPE_CLAUDE_WINDOW = os.environ.get("CCGRAM_SCRAPE_CLAUDE_WINDOW", "usage-scraper")
_SCRAPE_CODEX_WINDOW = os.environ.get("CCGRAM_SCRAPE_CODEX_WINDOW", "usage-scraper-codex")


def _run_scraper() -> dict:
    """Run scrape-usage.sh and return parsed JSON. Blocking."""
    try:
        tmux_session = os.environ.get("TMUX_SESSION_NAME", "ccgram")
        result = subprocess.run(
            [_SCRAPE_SCRIPT, _SCRAPE_CLAUDE_WINDOW, _SCRAPE_CODEX_WINDOW],
            capture_output=True, text=True, timeout=25,
            env={**os.environ, "TMUX_SESSION_NAME": tmux_session},
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        return json.loads(result.stdout.strip())
    except Exception:
        logger.debug("usage_report: scraper failed", exc_info=True)
        return {}


async def generate_usage_report() -> str:
    """Generate a usage report: Claude rate limits + Codex rate limits."""
    data = await asyncio.to_thread(_run_scraper)
    lines: list[str] = []

    session = data.get("session", {})
    week = data.get("weekAll", {})
    if session or week:
        lines.append("Claude")
        if session:
            pct = session["percent"]
            resets = session.get("resets", "?")
            lines.append(f"  5h: {pct}% used (resets {resets})")
        if week:
            pct = week["percent"]
            resets = week.get("resets", "?")
            lines.append(f"  Weekly: {pct}% used (resets {resets})")
    else:
        lines.append("Claude — no rate limit data")

    lines.append("")

    codex = data.get("codex", {})
    if codex:
        lines.append("Codex")
        remaining = codex.get("remaining", "?")
        resets = codex.get("resets", "?")
        lines.append(f"  Weekly: {remaining}% left (resets {resets})")
    else:
        lines.append("Codex — no data")

    return "\n".join(lines)
