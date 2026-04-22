"""Usage report — /usage Telegram command."""
from __future__ import annotations
import asyncio, json, os, subprocess, structlog
logger = structlog.get_logger()

_SCRAPE_SCRIPT = os.environ.get("CCGRAM_SCRAPE_SCRIPT", "/home/peter/ccgram-dashboard/scrape-usage.sh")
_CODEX_WINDOW = os.environ.get("CCGRAM_SCRAPE_CODEX_WINDOW", "usage-scraper-codex")

def _run_scraper() -> dict:
    try:
        env = {**os.environ, "TMUX_SESSION_NAME": os.environ.get("TMUX_SESSION_NAME", "ccgram")}
        r = subprocess.run([_SCRAPE_SCRIPT, "", _CODEX_WINDOW], capture_output=True, text=True, timeout=10, env=env)
        if r.returncode != 0 or not r.stdout.strip(): return {}
        return json.loads(r.stdout.strip())
    except Exception:
        logger.debug("usage_report: scraper failed", exc_info=True)
        return {}

async def generate_usage_report() -> str:
    data = await asyncio.to_thread(_run_scraper)
    lines: list[str] = []
    s = data.get("session")
    w = data.get("weekAll")
    def _fmt_resets(r):
        if isinstance(r, (int, float)) and r > 1000000000:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(r, tz=timezone.utc).astimezone()
            return dt.strftime("%a %H:%M")
        return str(r)

    if s or w:
        lines.append("Claude")
        if s: lines.append(f"  5h: {s['percent']}% used (resets {_fmt_resets(s['resets'])})")
        if w: lines.append(f"  Weekly: {w['percent']}% used (resets {_fmt_resets(w['resets'])})")
    else:
        lines.append("Claude — no data (cache stale or sessions not restarted yet)")
    lines.append("")
    c5 = data.get("codex5h")
    cw = data.get("codexWeekly")
    if c5 or cw:
        lines.append("Codex")
        if c5: lines.append(f"  5h: {c5['percent_left']}% left (resets {c5['resets']})")
        if cw: lines.append(f"  Weekly: {cw['percent_left']}% left (resets {cw['resets']})")
    else:
        lines.append("Codex — no data")
    return "\n".join(lines)
