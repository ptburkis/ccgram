"""In-process Telegram polling watchdog.

Tracks the timestamp of the last inbound Telegram update. If no update is
received within CCGRAM_WATCHDOG_TIMEOUT seconds (default 600), the bot is
considered to have lost its polling connection and exits so the external
watchdog (cron + ccgram-watchdog.sh) can restart it.

Usage:
  - Call record_inbound() on every inbound Telegram update.
  - Call check_watchdog() periodically from the status poll loop.
"""

import os
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

# How often to run the check (seconds). We don't need sub-minute resolution.
_WATCHDOG_INTERVAL = 600  # 10 minutes

# How long without an inbound update before we consider the connection dead.
# Overridable via env var for testing.
_WATCHDOG_TIMEOUT = int(os.environ.get("CCGRAM_WATCHDOG_TIMEOUT", "600"))

# Heartbeat file: touched on every inbound update so the external watchdog
# can independently verify the bot is alive and receiving Telegram messages.
_HEARTBEAT_FILE = Path.home() / ".ccgram" / "watchdog-last-inbound"

# Exit-reason file written just before os._exit so the log is informative.
_EXIT_REASON_FILE = Path.home() / ".ccgram" / "watchdog-exit-reason.txt"

# Module-level state (process-global, intentionally simple).
_last_inbound_ts: float = time.monotonic()
_last_check_ts: float = 0.0


def record_inbound() -> None:
    """Record that a Telegram update was just received.

    Call this from any handler that fires on inbound updates. The TypeHandler
    catch-all in bot.py ensures this covers every update type.
    """
    global _last_inbound_ts
    _last_inbound_ts = time.monotonic()
    try:
        _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT_FILE.touch()
    except OSError:
        pass


async def check_watchdog() -> None:
    """Check whether inbound Telegram updates have stopped.

    Called from the status poll loop. If no update has been received for
    longer than _WATCHDOG_TIMEOUT seconds, write a reason file and exit.
    The external watchdog (ccgram-watchdog.sh, run by cron) will restart
    the process.

    Uses os._exit(1) rather than a clean asyncio shutdown because a hung
    polling connection may block normal shutdown paths.
    """
    global _last_check_ts
    now = time.monotonic()

    if now - _last_check_ts < _WATCHDOG_INTERVAL:
        return
    _last_check_ts = now

    elapsed = now - _last_inbound_ts
    if elapsed <= _WATCHDOG_TIMEOUT:
        return

    reason = (
        f"No inbound Telegram updates for {elapsed:.0f}s "
        f"at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
    )
    logger.error(
        "polling_watchdog: no inbound updates — forcing exit for restart",
        elapsed_seconds=round(elapsed),
        timeout=_WATCHDOG_TIMEOUT,
    )
    try:
        _EXIT_REASON_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EXIT_REASON_FILE.write_text(reason)
    except OSError:
        pass

    os._exit(1)  # Hard exit — asyncio cleanup would block on hung connections
