"""Usage report — /usage Telegram command.

Scrapes rate limits from one Claude Code window (/usage) and one Codex
window (/status). Returns formatted text for Telegram.
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
_CLAUDE_WINDOW = os.environ.get("CCGRAM_SCRAPE_CLAUDE_WINDOW", "usage-scraper")
_CODEX_WINDOW = os.environ.get("CCGRAM_SCRAPE_CODEX_WINDOW", "usage-scraper-codex")


def _run_scraper() -> dict:
    """Run scrape-usage.sh. Blocking, ~18s."""
    try:
        env = {**os.environ, "TMUX_SESSION_NAME": os.environ.get("TMUX_SESSION_NAME", "ccgram")}
        r = subprocess.run(
            [_SCRAPE_SCRIPT, _CLAUDE_WINDOW, _CODEX_WINDOW],
            capture_output=True, text=True, timeout=25, env=env,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout.strip())
    except Exception:
        logger.debug("usage_report: scraper failed", exc_info=True)
        return {}


async def generate_usage_report() -> str:
    """Scrape and format usage report."""
    data = await asyncio.to_thread(_run_scraper)
    lines: list[str] = []

    # Claude
    s = data.get("session")
    wa = data.get("weekAll")
    ws = data.get("weekSonnet")
    if s or wa or ws:
        lines.append("Claude")
        if s:
            lines.append(f"  Session: {s['percent']}% used (resets {s['resets']})")
        if wa:
            lines.append(f"  Week (all): {wa['percent']}% used (resets {wa['resets']})")
        if ws:
            lines.append(f"  Week (Sonnet): {ws['percent']}% used (resets {ws['resets']})")
    else:
        lines.append("Claude — no data (scraper may have timed out)")

    lines.append("")

    # Codex
    c5 = data.get("codex5h")
    cw = data.get("codexWeekly")
    if c5 or cw:
        lines.append("Codex")
        if c5:
            lines.append(f"  5h: {c5['percent_left']}% left (resets {c5['resets']})")
        if cw:
            lines.append(f"  Weekly: {cw['percent_left']}% left (resets {cw['resets']})")
    else:
        lines.append("Codex — no data")

    return "\n".join(lines)
