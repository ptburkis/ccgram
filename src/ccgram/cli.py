"""Click-based CLI for ccgram.

Defines the top-level command group and the ``run`` subcommand with all
bot-configuration flags.  Precedence: CLI flag > env var > .env > default.
``apply_args_to_env()`` sets os.environ for explicitly provided flags so
Config reads the overridden values.
"""

import os
from pathlib import Path

import click

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _validate_positive_float(
    _ctx: click.Context, _param: click.Parameter, value: float | None
) -> float | None:
    if value is not None and value <= 0:
        raise click.BadParameter("must be positive")
    return value


def _validate_non_negative_int(
    _ctx: click.Context, _param: click.Parameter, value: int | None
) -> int | None:
    if value is not None and value < 0:
        raise click.BadParameter("must be non-negative")
    return value


class _DefaultToRun(click.Group):
    """Click group that runs the ``run`` command when invoked without a subcommand."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # If the first arg is not a known command and not --help/--version,
        # prepend "run" so flags like -v go to the run command.
        if args and args[0] not in self.commands and not args[0].startswith("--"):
            args = ["run", *args]
        return super().parse_args(ctx, args)


@click.group(
    cls=_DefaultToRun,
    invoke_without_command=True,
    help="Command & Control Bot — manage AI coding agents from Telegram via tmux.",
)
@click.version_option(package_name="ccgram", prog_name="ccgram")
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(run_cmd)


# --- run command -----------------------------------------------------------

# Mapping: click option name → environment variable name
_FLAG_TO_ENV: list[tuple[str, str]] = [
    ("config_dir", "CCGRAM_DIR"),
    ("allowed_users", "ALLOWED_USERS"),
    ("tmux_session", "TMUX_SESSION_NAME"),
    ("monitor_interval", "MONITOR_POLL_INTERVAL"),
    ("group_id", "CCGRAM_GROUP_ID"),
    ("instance_name", "CCGRAM_INSTANCE_NAME"),
    ("autoclose_done", "AUTOCLOSE_DONE_MINUTES"),
    ("autoclose_dead", "AUTOCLOSE_DEAD_MINUTES"),
    ("provider", "CCGRAM_PROVIDER"),
    ("show_hidden_dirs", "CCGRAM_SHOW_HIDDEN_DIRS"),
    ("claude_config_dir", "CLAUDE_CONFIG_DIR"),
    ("whisper_provider", "CCGRAM_WHISPER_PROVIDER"),
    ("ack_reaction", "CCGRAM_ACK_REACTION"),
]


def apply_args_to_env(**kwargs: object) -> None:
    """Set environment variables from explicitly provided CLI flags.

    Call BEFORE Config instantiation to ensure CLI flags take precedence.
    Only sets env vars for flags that were explicitly provided (not None).
    """
    verbose = kwargs.get("verbose", False)
    log_level = kwargs.get("log_level")

    if verbose:
        os.environ["CCGRAM_LOG_LEVEL"] = "DEBUG"
    elif log_level is not None:
        os.environ["CCGRAM_LOG_LEVEL"] = str(log_level).upper()

    for attr, env_var in _FLAG_TO_ENV:
        value = kwargs.get(attr)
        if value is None:
            continue
        if isinstance(value, Path):
            os.environ[env_var] = str(value.expanduser().resolve())
        else:
            os.environ[env_var] = str(value)


@cli.command("run")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.option(
    "--log-level",
    type=click.Choice(_LOG_LEVELS, case_sensitive=False),
    default=None,
    help="Logging level.",
)
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    envvar="CCGRAM_DIR",
    help="Config directory (default: ~/.ccgram).",
)
@click.option(
    "--allowed-users",
    default=None,
    envvar="ALLOWED_USERS",
    help="Comma-separated Telegram user IDs.",
)
@click.option(
    "--tmux-session",
    default=None,
    envvar="TMUX_SESSION_NAME",
    help="Tmux session name (default: ccgram).",
)
@click.option(
    "--monitor-interval",
    type=float,
    default=None,
    callback=_validate_positive_float,
    envvar="MONITOR_POLL_INTERVAL",
    help="Poll interval in seconds (default: 2.0).",
)
@click.option(
    "--group-id",
    type=int,
    default=None,
    envvar="CCGRAM_GROUP_ID",
    help="Restrict to one Telegram group.",
)
@click.option(
    "--instance-name",
    default=None,
    envvar="CCGRAM_INSTANCE_NAME",
    help="Display label for multi-instance.",
)
@click.option(
    "--autoclose-done",
    type=int,
    default=None,
    callback=_validate_non_negative_int,
    envvar="AUTOCLOSE_DONE_MINUTES",
    help="Auto-close done topics after N minutes (default: 30, 0=disabled).",
)
@click.option(
    "--autoclose-dead",
    type=int,
    default=None,
    callback=_validate_non_negative_int,
    envvar="AUTOCLOSE_DEAD_MINUTES",
    help="Auto-close dead sessions after N minutes (default: 10, 0=disabled).",
)
@click.option(
    "--provider",
    default=None,
    envvar="CCGRAM_PROVIDER",
    help="Agent provider name (default: claude).",
)
@click.option(
    "--show-hidden-dirs",
    is_flag=True,
    default=None,
    envvar="CCGRAM_SHOW_HIDDEN_DIRS",
    help="Show hidden (dot) directories in directory browser.",
)
@click.option(
    "--claude-config-dir",
    type=click.Path(path_type=Path),
    default=None,
    envvar="CLAUDE_CONFIG_DIR",
    help="Claude config directory (default: ~/.claude).",
)
@click.option(
    "--whisper-provider",
    default=None,
    envvar="CCGRAM_WHISPER_PROVIDER",
    help='Whisper transcription provider: "openai", "groq", or "" (disabled).',
)
@click.option(
    "--ack-reaction",
    default=None,
    envvar="CCGRAM_ACK_REACTION",
    help='React to forwarded messages with emoji (e.g., "👀"). Empty=disabled.',
)
def run_cmd(**kwargs: object) -> None:
    """Start the bot with optional overrides."""
    apply_args_to_env(**kwargs)

    from .main import run_bot

    run_bot()


# --- hook command ----------------------------------------------------------


@cli.command("hook")
@click.option(
    "--install", is_flag=True, help="Install hook into ~/.claude/settings.json."
)
@click.option(
    "--uninstall", is_flag=True, help="Remove hook from ~/.claude/settings.json."
)
@click.option("--status", is_flag=True, help="Check if hook is installed.")
def hook_cmd(install: bool, uninstall: bool, status: bool) -> None:
    """Claude Code session tracking hook."""
    from .hook import hook_main

    hook_main(install=install, uninstall=uninstall, status=status)


# --- status command --------------------------------------------------------


@cli.command("status")
def status_cmd() -> None:
    """Show running state."""
    from .status_cmd import status_main

    status_main()


# --- doctor command --------------------------------------------------------


# --- msg command group -----------------------------------------------------


def _register_msg_group() -> None:
    from .msg_cmd import msg_group

    cli.add_command(msg_group, "msg")


_register_msg_group()


# --- doctor command --------------------------------------------------------


@cli.command("doctor")
@click.option("--fix", is_flag=True, help="Auto-fix issues where possible.")
def doctor_cmd(fix: bool) -> None:
    """Validate setup and diagnose issues."""
    from .doctor_cmd import doctor_main

    doctor_main(fix=fix)


# --- sync-check command ----------------------------------------------------


@cli.command("sync-check")
@click.option("--fix", is_flag=True, help="Auto-fix drifted topics.")
@click.option("--json", "json_output", is_flag=True, help="JSON output.")
def sync_check_cmd(fix: bool, json_output: bool) -> None:
    """Compare Telegram topic state vs actual state and report drift."""
    from .sync_check import run_sync_check

    report = run_sync_check(fix=fix)

    if json_output:
        import json
        import dataclasses

        def _item_to_dict(it):
            d = dataclasses.asdict(it)
            d["drifted"] = it.drifted
            return d

        print(
            json.dumps(
                {
                    "total": report.total,
                    "drifted": report.drifted_count,
                    "items": [_item_to_dict(it) for it in report.items],
                },
                indent=2,
            )
        )
        return

    # Human-readable table
    header = (
        f"{'Window':<8} {'Telegram Title':<30} {'Correct':<22} "
        f"{'⚡ TG/Act':<12} {'🐚 TG/Act':<12} Match"
    )
    print(header)
    print("-" * 90)
    for it in report.items:
        bolt_col = f"{'yes' if it.has_bolt else 'no'}/{'yes' if it.should_bolt else 'no'}"
        shell_col = f"{'yes' if it.has_shell else 'no'}/{'yes' if it.should_shell else 'no'}"
        status = "DRIFT" if it.drifted else "OK"
        tg = it.telegram_title[:28] + ".." if len(it.telegram_title) > 30 else it.telegram_title
        cn = it.correct_name[:20] + ".." if len(it.correct_name) > 22 else it.correct_name
        print(
            f"{it.window_id:<8} {tg:<30} {cn:<22} {bolt_col:<12} {shell_col:<12} {status}"
        )
    print(f"\n{report.drifted_count} drifted / {report.total} total")


# --- audit command ------------------------------------------------------------


@cli.command("audit")
@click.option("--last", default=50, type=int, help="Show last N entries (default: 50).")
@click.option("--action", default="", help="Filter by action type.")
@click.option("--window", default="", help="Filter by window_id.")
@click.option("--reason", default="", help="Filter by reason.")
def audit_cmd(last: int, action: str, window: str, reason: str) -> None:
    """Show recent Telegram audit log entries."""
    import json
    import time as _time
    from datetime import datetime
    from pathlib import Path

    audit_path = Path.home() / ".ccgram" / "telegram-audit.jsonl"
    if not audit_path.exists():
        click.echo("No audit log found at ~/.ccgram/telegram-audit.jsonl")
        return

    entries = []
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Apply filters
    if action:
        entries = [e for e in entries if e.get("action", "") == action]
    if window:
        entries = [e for e in entries if e.get("window_id", "") == window]
    if reason:
        entries = [e for e in entries if e.get("reason", "") == reason]

    # Last N entries
    entries = entries[-last:]

    if not entries:
        click.echo("No entries match the filters.")
        return

    # Header
    header = f"{'TIME':<10} {'ACTION':<20} {'WINDOW':<8} {'THREAD':<8} {'REASON':<22} {'PAYLOAD'}"
    click.echo(header)
    click.echo("-" * 90)

    for e in entries:
        ts = e.get("ts", 0)
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        act = e.get("action", "")[:19]
        wid = e.get("window_id", "")[:7]
        tid = str(e.get("thread_id") or "")[:7]
        rsn = e.get("reason", "")[:21]
        payload = e.get("payload", {})
        # Render payload as compact key=value
        pstr = " ".join(f"{k}: {str(v)[:30]}" for k, v in payload.items()) if payload else ""
        click.echo(f"{t:<10} {act:<20} {wid:<8} {tid:<8} {rsn:<22} {pstr}")
