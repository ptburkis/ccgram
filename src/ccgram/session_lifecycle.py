"""Transactional session lifecycle — create_session / delete_session.

The canonical, single entry point for spawning or retiring an agent session.
Three independent side-effects must be kept in sync:

1. **DB** — a ``sessions`` row plus a ``topic_bindings`` row in ``store.py``.
2. **Telegram** — a forum topic (new-created or reused) in the target group.
3. **tmux** — a window running the agent CLI, tagged with ``CCGRAM_SESSION_ID``.

If any step fails, every side-effect created *by this call* is rolled back.

Rollback matrix
---------------
Steps are numbered in execution order. On failure at step N, undo steps 1..N-1
(in reverse) that created new side-effects. Reused pre-existing resources
(e.g. ``existing_topic_id``) are **never** deleted.

==============================  ========================================
Failing step                    Rollback actions (in order)
==============================  ========================================
1. insert pending session row   none — nothing external created yet
2. create/verify Telegram topic none — tmux untouched; mark session
                                ``errored``
3. tmux new-window              if topic was newly created, delete it;
                                mark session ``errored``
4. launch agent in pane         kill the tmux window; if topic was
                                newly created, delete it; mark session
                                ``errored``
5. insert topic_binding row     kill the tmux window; if topic was
                                newly created, delete it; mark session
                                ``errored`` (this is how UNIQUE
                                ``session_id`` violations surface —
                                nothing leaks)
6. update session to active     unreachable in practice — same row
                                already written; best-effort mark
                                ``errored``
==============================  ========================================

All rollback sub-steps are ``try`` / ``except`` wrapped so a cleanup failure
cannot mask the original error. The original exception is always re-raised.

Dependency injection
--------------------
To keep the unit-test surface offline, this module references three **module-
level callables** rather than importing the bot / mtproto / tmux clients
directly from inside ``create_session``. Production wires them once at
startup; tests overwrite them with ``unittest.mock`` stubs.

    _create_forum_topic_fn    async (group_id, name) -> int (topic_id)
    _delete_forum_topic_fn    async (group_id, topic_id) -> None
    _verify_forum_topic_fn    async (group_id, topic_id) -> (exists, title)
    _tmux_create_window_fn    async (cwd, window_name) -> window_id (str)
    _tmux_send_keys_fn        async (window_id, text) -> None
    _tmux_kill_window_fn      async (window_id) -> None
    _resolve_launch_fn        (agent, mode) -> str (shell command)

Use :func:`configure` to wire them. The defaults raise if invoked — forcing
every caller to configure or stub.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Telegram user ID that owns the routing user_prefs rows.
# Override via CCGRAM_DEFAULT_USER_ID env var.
_DEFAULT_USER_ID: int = int(os.environ.get("CCGRAM_DEFAULT_USER_ID", "1219959327"))

import structlog

from ccgram import store

logger = structlog.get_logger(__name__)


def _log_mutation(event: str, **kwargs) -> None:
    import traceback
    logger.warning("MUTATION: %s %s caller=%s", event, kwargs, traceback.extract_stack(limit=4)[-2:])


# ---- Types -------------------------------------------------------------------

Agent = Literal["claude", "codex", "gemini"]

CreateTopicFn = Callable[[int, str], Awaitable[int]]
"""(group_id, topic_name) -> topic_id (message_thread_id)."""

DeleteTopicFn = Callable[[int, int], Awaitable[None]]
"""(group_id, topic_id) -> None. Best-effort; swallow 'already gone' errors."""

VerifyTopicFn = Callable[[int, int], Awaitable[tuple[bool, str]]]
"""(group_id, topic_id) -> (exists, current_title)."""

TmuxCreateFn = Callable[[str, str], Awaitable[str]]
"""(cwd, window_name) -> window_id. Must raise on failure, NOT return empty."""

TmuxSendFn = Callable[[str, str], Awaitable[None]]
"""(window_id, text) -> None. Used to export env and launch the agent."""

TmuxKillFn = Callable[[str], Awaitable[None]]
"""(window_id) -> None. Best-effort; swallow 'already gone' errors."""

ResolveLaunchFn = Callable[[str, str | None], str]
"""(agent, mode) -> shell command string (e.g. 'claude --dangerously...')."""


# ---- Exceptions --------------------------------------------------------------


class SessionLifecycleError(RuntimeError):
    """Base class for create_session / delete_session failures."""


class TopicVerificationError(SessionLifecycleError):
    """Raised when ``existing_topic_id`` fails MTProto verification.

    Either the topic no longer exists in Telegram or its title no longer
    matches the requested ``topic_name``.
    """


class AgentLaunchError(SessionLifecycleError):
    """Raised when sending the launch command to the pane fails."""


class NotConfiguredError(SessionLifecycleError):
    """Raised when a session_lifecycle dependency was never wired.

    Either call :func:`configure` at startup, or patch the module-level
    callables directly in tests.
    """


# ---- Dependency injection ----------------------------------------------------


async def _not_configured(*_: object, **__: object) -> None:
    raise NotConfiguredError(
        "session_lifecycle dependency not configured — call configure() "
        "or patch the module-level callables before use"
    )


def _sync_not_configured(*_: object, **__: object) -> str:
    raise NotConfiguredError("session_lifecycle resolve_launch_fn not configured")


# Module-level injectable functions. Tests replace these with mocks; production
# wires them once via configure().
_create_forum_topic_fn: CreateTopicFn = _not_configured  # type: ignore[assignment]
_delete_forum_topic_fn: DeleteTopicFn = _not_configured  # type: ignore[assignment]
_verify_forum_topic_fn: VerifyTopicFn = _not_configured  # type: ignore[assignment]
_tmux_create_window_fn: TmuxCreateFn = _not_configured  # type: ignore[assignment]
_tmux_send_keys_fn: TmuxSendFn = _not_configured  # type: ignore[assignment]
_tmux_kill_window_fn: TmuxKillFn = _not_configured  # type: ignore[assignment]
_resolve_launch_fn: ResolveLaunchFn = _sync_not_configured  # type: ignore[assignment]

# Optional capture_pane for readiness checks — defaults to tmux_manager at runtime.
# Tests may patch this directly.  None means fall back to tmux_manager.capture_pane.
_tmux_capture_pane_fn: Callable[[str], Awaitable[str | None]] | None = None


@dataclass(slots=True)
class LifecycleDeps:
    """Bundle of injectable dependencies — convenience holder for :func:`configure`."""

    create_forum_topic: CreateTopicFn
    delete_forum_topic: DeleteTopicFn
    verify_forum_topic: VerifyTopicFn
    tmux_create_window: TmuxCreateFn
    tmux_send_keys: TmuxSendFn
    tmux_kill_window: TmuxKillFn
    resolve_launch_command: ResolveLaunchFn


def configure(deps: LifecycleDeps) -> None:
    """Wire the module-level dependencies in one shot.

    Production startup does this once with real bot/MTProto/tmux adapters.
    Tests may either call this with stubs or patch the module-level
    attributes directly.
    """
    global _create_forum_topic_fn, _delete_forum_topic_fn, _verify_forum_topic_fn
    global _tmux_create_window_fn, _tmux_send_keys_fn, _tmux_kill_window_fn
    global _resolve_launch_fn

    _create_forum_topic_fn = deps.create_forum_topic
    _delete_forum_topic_fn = deps.delete_forum_topic
    _verify_forum_topic_fn = deps.verify_forum_topic
    _tmux_create_window_fn = deps.tmux_create_window
    _tmux_send_keys_fn = deps.tmux_send_keys
    _tmux_kill_window_fn = deps.tmux_kill_window
    _resolve_launch_fn = deps.resolve_launch_command


# ---- Public API --------------------------------------------------------------


async def _accept_bypass_permissions(window_id: str, *, timeout: float = 8.0) -> None:
    """Detect and accept Claude Code's bypass permissions confirmation prompt.

    When launched with --dangerously-skip-permissions, Claude Code shows a
    TUI confirmation where 'No, exit' is the default selection.  Sends
    Down+Enter to select the 'Yes' option so the session can start.
    Uses the injected capture fn or falls back to tmux_manager.
    """
    import asyncio

    async def _capture(wid: str) -> str | None:
        if _tmux_capture_pane_fn is not None:
            return await _tmux_capture_pane_fn(wid)
        from .tmux_manager import tmux_manager as _tm
        return await _tm.capture_pane(wid)

    async def _send(wid: str, key: str) -> None:
        from .tmux_manager import tmux_manager as _tm
        await _tm.send_keys(wid, key, enter=False, literal=False)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        text = await _capture(window_id)
        if text and "bypass permissions" in text.lower():
            await asyncio.sleep(0.3)
            await _send(window_id, "Down")
            await asyncio.sleep(0.15)
            await _send(window_id, "Enter")
            logger.info(
                "session_lifecycle.bypass_permissions_accepted", window_id=window_id
            )
            return
        await asyncio.sleep(0.5)
    logger.warning(
        "session_lifecycle.bypass_permissions_not_detected",
        window_id=window_id,
        timeout=timeout,
    )


async def _verify_agent_ready(
    window_id: str,
    agent: str,
    mode: str | None,
    *,
    timeout: float = 30.0,
    poll_interval: float = 1.5,
) -> bool:
    """Poll pane content until agent shows signs of being ready.

    Returns True when a success signal is found, False on timeout or failure.
    """
    import asyncio

    # Use injected capture fn, or fall back to tmux_manager at runtime.
    async def _capture(wid: str) -> str | None:
        if _tmux_capture_pane_fn is not None:
            return await _tmux_capture_pane_fn(wid)
        from .tmux_manager import tmux_manager
        return await tmux_manager.capture_pane(wid)

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            pane = await _capture(window_id)
            if not pane:
                await asyncio.sleep(poll_interval)
                continue

            # Failure signals — abort early
            if "command not found" in pane or "No such file" in pane:
                return False

            # Success signals per provider
            if agent == "claude":
                if "bypass permissions" in pane or "\u276f" in pane or "Claude Code" in pane:
                    return True
            elif agent == "codex":
                if "gpt-" in pane or "\u203a" in pane or "OpenAI Codex" in pane:
                    return True
            else:
                if "\u276f" in pane or "\u203a" in pane:
                    return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(poll_interval)
    return False


async def create_session(
    *,
    cwd: str,
    topic_name: str,
    agent: Agent,
    group_id: int,
    mode: str | None = None,
    existing_topic_id: int | None = None,
    verify_ready: bool = True,
    ready_timeout: float = 30.0,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Spawn a new agent session, binding it to a Telegram forum topic.

    See module docstring for the rollback matrix.

    Args:
        cwd: Working directory for the tmux window.
        topic_name: Desired Telegram topic title. If ``existing_topic_id``
            is given, this is the title the topic is verified to already
            have; otherwise it is the title passed to ``createForumTopic``.
        agent: Which agent CLI to launch.
        group_id: Telegram group ID hosting the forum topic.
        mode: Optional provider mode (e.g. ``'yolo'``).
        existing_topic_id: If set, reuse this topic (verify via MTProto)
            instead of creating a new one.
        verify_ready: If True, poll pane content to verify agent started.
        ready_timeout: Seconds to wait for readiness signal.
        on_progress: Optional async callback for progress messages.

    Returns:
        The minted ``session_id`` (UUID string).

    Raises:
        TopicVerificationError: ``existing_topic_id`` doesn't exist or
            its current title differs from ``topic_name``.
        AgentLaunchError: tmux was created but the launch command failed.
        sqlite3.IntegrityError: If another session is already bound to the
            resolved ``(group_id, topic_id)`` — double-bind caught by
            ``UNIQUE(session_id)`` / ``PRIMARY KEY(group_id, topic_id)``.
            All side-effects rolled back before re-raising.
        SessionLifecycleError: any other step failure; rollback attempted.
    """
    session_id = str(uuid.uuid4())

    if on_progress:
        await on_progress(f"\U0001f680 Spawning {topic_name} ({agent})...")

    # Step 1: insert pending row.
    with store.connect() as conn:
        store.upsert_session(
            conn,
            session_id=session_id,
            cwd=cwd,
            agent=agent,
            mode=mode,
            status="pending",
            window_id=None,
        )

    topic_id: int | None = None
    topic_was_created_here = False
    window_id: str | None = None

    try:
        # Step 2: resolve the topic — either verify or create.
        if existing_topic_id is not None:
            exists, current_title = await _verify_forum_topic_fn(
                group_id, existing_topic_id
            )
            if not exists:
                raise TopicVerificationError(
                    f"Topic {existing_topic_id} not found in group {group_id}"
                )
            if current_title != topic_name:
                raise TopicVerificationError(
                    f"Topic {existing_topic_id} title mismatch: "
                    f"expected {topic_name!r}, got {current_title!r}"
                )
            topic_id = existing_topic_id
        else:
            topic_id = await _create_forum_topic_fn(group_id, topic_name)
            topic_was_created_here = True

        # Step 3: tmux window.
        window_id = await _tmux_create_window_fn(cwd, topic_name)
        if not window_id:
            raise SessionLifecycleError("tmux_create_window returned empty window_id")

        # Step 3b: bootstrap write-path hooks into the project's settings.json.
        _bootstrap_hook_config(cwd)

        if on_progress:
            await on_progress(f"\u23f3 Starting {agent}, waiting for readiness...")

        # Step 4: launch agent with CCGRAM_SESSION_ID marker.
        try:
            launch_cmd = _resolve_launch_fn(agent, mode)
            # Prefix with inline env so child process inherits the marker.
            # ``foo=bar cmd args`` works in bash/zsh; keeps the single
            # send_keys roundtrip cheap.
            full_cmd = f"CCGRAM_SESSION_ID={shlex.quote(session_id)} {launch_cmd}"
            await _tmux_send_keys_fn(window_id, full_cmd)
            # Write per-window session_id marker file (atomic replace) so
            # the transcript watcher can resolve window -> session_id even
            # when /proc env is unavailable (e.g. short-lived child procs
            # inside the pane). See Chunk F.
            _write_session_marker_file(window_id, session_id)
        except Exception as exc:
            raise AgentLaunchError(
                f"Failed to launch {agent} in window {window_id}: {exc}"
            ) from exc

        # Step 4b: verify agent started.
        if verify_ready:
            ready = await _verify_agent_ready(window_id, agent, mode, timeout=ready_timeout)
            if not ready:
                logger.warning(
                    "session_lifecycle.agent_readiness_timeout",
                    window_id=window_id,
                    agent=agent,
                )
                if on_progress:
                    await on_progress(f"\u26a0\ufe0f {agent} may not have started correctly")

        # Step 4c: accept Claude YOLO bypass-permissions prompt if needed.
        if agent == "claude" and mode and "yolo" in mode.lower() and window_id:
            try:
                await _accept_bypass_permissions(window_id)
            except Exception as _yolo_exc:  # noqa: BLE001
                logger.warning(
                    "session_lifecycle.bypass_accept_failed",
                    window_id=window_id,
                    error=str(_yolo_exc),
                )

        # Step 5: bind topic → session in the DB. Atomic with the session
        # status update below via the same connect() context.
        with store.connect() as conn:
            _assert_binding_free(conn, group_id, topic_id, session_id)
            store.upsert_topic_binding(
                conn,
                group_id=group_id,
                topic_id=topic_id,
                session_id=session_id,
                topic_title=topic_name,
            )
            # Write the user_prefs routing row required by
            # session._load_state_from_db (scope='group_chat',
            # key='<user_id>:<topic_id>', value=group_id).  Must be in the
            # same transaction as the topic_binding write so the two stay
            # in sync.
            store.set_pref(
                conn,
                scope="group_chat",
                scope_id="",
                key=f"{_DEFAULT_USER_ID}:{topic_id}",
                value=group_id,
            )

        # Step 6: promote to active via session_repo (retire-before-insert in one TX).
        # Separated from the step 5 block so session_repo manages its own connection.
        # Note: session_repo.create_session_for_window also deletes the pending row
        # seeded at step 1 (same session_id) before inserting the active row.
        from . import session_repo as _session_repo
        _session_repo.create_session_for_window(
            session_id=session_id,
            window_id=window_id,
            cwd=cwd,
            agent=agent,
            mode=mode,
        )

        # After DB writes: update thread_router in-memory so the running bot
        # knows about the new binding without a restart.  Without this, the
        # binding exists in DB but text_handler._handle_unbound_topic shows
        # the picker again because the in-memory router is stale.
        try:
            from .thread_router import thread_router as _thread_router
            from .config import config as _config

            admin_uid = next(iter(_config.allowed_users), 0)
            if admin_uid and topic_id:
                _thread_router.bind_thread(admin_uid, topic_id, window_id, window_name=topic_name)
                _thread_router.set_group_chat_id(admin_uid, topic_id, group_id)
                logger.info(
                    "create_session: bound thread %d -> %s in-memory",
                    topic_id,
                    window_id,
                )
        except Exception as _tr_exc:  # noqa: BLE001
            logger.warning(
                "session_lifecycle.thread_router_update_failed",
                error=str(_tr_exc),
            )

        if on_progress:
            await on_progress(f"\u2705 {topic_name} ready")

        logger.info(
            "session_lifecycle.create_ok",
            session_id=session_id,
            topic_id=topic_id,
            window_id=window_id,
            agent=agent,
        )
        return session_id

    except Exception as exc:
        await _rollback_create(
            session_id=session_id,
            group_id=group_id,
            topic_id=topic_id if topic_was_created_here else None,
            window_id=window_id,
            original_error=exc,
        )
        raise


async def delete_session(
    session_id: str, *, close_telegram_topic: bool = False
) -> None:
    """Retire a session — mark DB row ``retired``, kill tmux window.

    Idempotent: calling on an already-retired or unknown session_id is a
    no-op and does not raise.

    Args:
        session_id: UUID of the session to retire.
        close_telegram_topic: If True, also delete the Telegram forum
            topic via the bot. Defaults to False so history is preserved
            (topics can be re-bound later).
    """
    with store.connect() as conn:
        session = store.get_session(conn, session_id)
        if session is None:
            logger.debug("session_lifecycle.delete_noop_unknown", session_id=session_id)
            return
        binding = store.get_binding_for_session(conn, session_id)

    # If already retired and window gone, nothing to do.
    if session.status == "retired" and session.window_id is None:
        logger.debug("session_lifecycle.delete_noop_retired", session_id=session_id)
        return

    # Kill tmux window (best-effort — it may already be gone).
    if session.window_id:
        try:
            await _tmux_kill_window_fn(session.window_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_lifecycle.kill_window_failed",
                session_id=session_id,
                window_id=session.window_id,
                error=str(exc),
            )

    # Optionally close/delete the Telegram topic.
    if close_telegram_topic and binding is not None:
        try:
            await _delete_forum_topic_fn(binding.group_id, binding.topic_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_lifecycle.delete_topic_failed",
                session_id=session_id,
                group_id=binding.group_id,
                topic_id=binding.topic_id,
                error=str(exc),
            )

    # Mark retired. FK ON DELETE CASCADE only fires on session DELETE; we
    # keep the session row for audit and let the binding linger until the
    # row is deleted elsewhere. To mirror the spec's "FK cascade deletes
    # topic_binding row", explicitly drop the binding here.
    if binding is not None:
        with store.connect() as conn:
            _log_mutation("binding_delete", window="-", session=session_id, details=f"group={binding.group_id} topic={binding.topic_id}")
            store.delete_topic_binding(conn, binding.group_id, binding.topic_id)

    # Retire the session via session_repo (force=True: user-initiated, bypasses liveness check).
    _log_mutation("session_retire", window="-", session=session_id, details="delete_session (via session_repo)")
    from . import session_repo as _session_repo
    if session.window_id:
        _session_repo.retire_window(session.window_id, reason="delete_session", force=True)
    else:
        # No window_id — session is already window-less; retire by session_id directly.
        _session_repo.retire_by_session_id(session_id, reason="delete_session_no_window")

    logger.info("session_lifecycle.delete_ok", session_id=session_id)


# ---- Internal: helpers -------------------------------------------------------


def _bootstrap_hook_config(cwd: str) -> None:
    """Write/merge write-path hooks into <cwd>/.claude/settings.json.

    Idempotent:
    - If the hook commands are already present, skip (no clobber).
    - If other hooks exist and ours are absent, merge by appending.
    - On any error, log a WARNING and continue -- never block session creation.

    Also copies scripts/hooks/*.py to ~/.ccgram/hooks/ if stale or absent.
    """
    _deploy_hook_scripts()

    settings_path = Path(cwd) / ".claude" / "settings.json"
    pre_cmd = "python3 ~/.ccgram/hooks/check-write-path.py"
    post_cmd = "python3 ~/.ccgram/hooks/rewrite-output-url.py"

    desired_pre = {
        "matcher": "Write|Edit|NotebookEdit",
        "hooks": [{"type": "command", "command": pre_cmd}],
    }
    desired_post = {
        "matcher": "Write|Edit|NotebookEdit|Read|Bash",
        "hooks": [{"type": "command", "command": post_cmd}],
    }

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if settings_path.exists():
            try:
                existing = json.loads(settings_path.read_text())
            except json.JSONDecodeError as exc:
                logger.warning(
                    "session_lifecycle.bootstrap_hooks_bad_json",
                    path=str(settings_path),
                    error=str(exc),
                )
                return
        else:
            existing = {}

        hooks = existing.setdefault("hooks", {})
        pre_list: list = hooks.setdefault("PreToolUse", [])
        post_list: list = hooks.setdefault("PostToolUse", [])

        def _has_command(hook_list: list, cmd: str) -> bool:
            for entry in hook_list:
                for h in entry.get("hooks", []):
                    if h.get("command") == cmd:
                        return True
            return False

        def _has_conflicting(hook_list: list, matcher: str, our_cmd: str) -> bool:
            for entry in hook_list:
                if entry.get("matcher") == matcher:
                    for h in entry.get("hooks", []):
                        if h.get("type") == "command" and h.get("command") != our_cmd:
                            return True
            return False

        modified = False

        if not _has_command(pre_list, pre_cmd):
            if _has_conflicting(pre_list, desired_pre["matcher"], pre_cmd):
                logger.warning(
                    "session_lifecycle.bootstrap_hooks_conflict",
                    path=str(settings_path),
                    hook="PreToolUse",
                    reason="conflicting matcher entry exists; skipping",
                )
            else:
                pre_list.append(desired_pre)
                modified = True

        if not _has_command(post_list, post_cmd):
            if _has_conflicting(post_list, desired_post["matcher"], post_cmd):
                logger.warning(
                    "session_lifecycle.bootstrap_hooks_conflict",
                    path=str(settings_path),
                    hook="PostToolUse",
                    reason="conflicting matcher entry exists; skipping",
                )
            else:
                post_list.append(desired_post)
                modified = True

        if modified:
            tmp = settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(existing, indent=2))
            tmp.replace(settings_path)
            logger.info(
                "session_lifecycle.bootstrap_hooks_written",
                path=str(settings_path),
            )

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "session_lifecycle.bootstrap_hooks_failed",
            cwd=cwd,
            error=str(exc),
        )


def _deploy_hook_scripts() -> None:
    """Copy scripts/hooks/*.py to ~/.ccgram/hooks/ if absent or stale."""
    repo_hooks = Path(__file__).parent.parent.parent / "scripts" / "hooks"
    dest_dir = Path.home() / ".ccgram" / "hooks"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not repo_hooks.is_dir():
            return
        for src in repo_hooks.glob("*.py"):
            dst = dest_dir / src.name
            if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                shutil.copy2(src, dst)
                dst.chmod(0o755)
                logger.info("session_lifecycle.hook_script_deployed", script=str(dst))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_lifecycle.deploy_hook_scripts_failed", error=str(exc))


def _write_session_marker_file(window_id: str, session_id: str) -> None:
    """Atomically write session_id to ~/.ccgram/debug/terminal-<window_id>.sid."""
    target = Path.home() / ".ccgram" / "debug" / f"terminal-{window_id}.sid"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(session_id)
        tmp.replace(target)
        with contextlib.suppress(OSError):
            target.chmod(0o600)
    except OSError as exc:
        logger.warning(
            "session_lifecycle.marker_write_failed",
            window_id=window_id,
            session_id=session_id,
            error=str(exc),
        )


def _assert_binding_free(
    conn: object, group_id: int, topic_id: int, session_id: str
) -> None:
    """Raise IntegrityError if another session already owns this topic."""
    existing = store.get_topic_binding(conn, group_id, topic_id)  # type: ignore[arg-type]
    if existing is not None and existing.session_id != session_id:
        raise sqlite3.IntegrityError(
            f"Topic ({group_id}, {topic_id}) already bound to "
            f"session {existing.session_id}"
        )


# ---- Internal: rollback ------------------------------------------------------


async def _rollback_create(
    *,
    session_id: str,
    group_id: int,
    topic_id: int | None,
    window_id: str | None,
    original_error: BaseException,
) -> None:
    """Best-effort cleanup of side-effects created during a failed create_session.

    ``topic_id`` is passed in **only** when the topic was created by this
    call — reused topics are never deleted here.
    """
    # tmux window first (so we don't leave a pane spewing while we clean up).
    if window_id:
        try:
            await _tmux_kill_window_fn(window_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_lifecycle.rollback_kill_failed",
                session_id=session_id,
                window_id=window_id,
                error=str(exc),
            )

    # Telegram topic.
    if topic_id is not None:
        try:
            await _delete_forum_topic_fn(group_id, topic_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_lifecycle.rollback_delete_topic_failed",
                session_id=session_id,
                group_id=group_id,
                topic_id=topic_id,
                error=str(exc),
            )

    # Mark DB row errored (not deleted — keeps audit trail).
    try:
        with store.connect() as conn:
            existing = store.get_session(conn, session_id)
            if existing is not None:
                store.upsert_session(
                    conn,
                    session_id=existing.session_id,
                    cwd=existing.cwd,
                    agent=existing.agent,
                    mode=existing.mode,
                    status="errored",
                    window_id=None,
                    created_at=existing.created_at,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "session_lifecycle.rollback_mark_errored_failed",
            session_id=session_id,
            error=str(exc),
        )

    logger.error(
        "session_lifecycle.create_failed",
        session_id=session_id,
        error=str(original_error),
        error_type=type(original_error).__name__,
    )


# ---- Adapter helpers (production wiring) -------------------------------------


def build_default_deps(  # noqa: C901 — adapter factory with N thin closures
    *,
    bot: object,
    mtproto_client: object,
    tmux_manager_obj: object,
) -> LifecycleDeps:
    """Build :class:`LifecycleDeps` from production objects.

    Lightweight adapters over the real bot / MTProto / tmux helpers so
    :func:`configure` can be called at startup:

        from ccgram import session_lifecycle
        session_lifecycle.configure(session_lifecycle.build_default_deps(
            bot=bot, mtproto_client=mtproto, tmux_manager_obj=tmux_manager,
        ))

    The adapters are inline so callers don't have to author them, but they
    are deliberately thin — no retry/backoff policy lives here. That lives
    in the higher-level handlers already (see ``topic_orchestration``).
    """
    from ccgram.providers import resolve_launch_command

    async def _create_topic(group_id: int, name: str) -> int:
        topic = await bot.create_forum_topic(  # type: ignore[attr-defined]
            chat_id=group_id, name=name
        )
        return int(topic.message_thread_id)

    async def _delete_topic(group_id: int, topic_id: int) -> None:
        try:
            await bot.delete_forum_topic(  # type: ignore[attr-defined]
                chat_id=group_id, message_thread_id=topic_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_lifecycle.adapter_delete_topic_failed",
                group_id=group_id,
                topic_id=topic_id,
                error=str(exc),
            )

    async def _verify_topic(group_id: int, topic_id: int) -> tuple[bool, str]:
        try:
            if mtproto_client is None:
                return True, ""  # assume topic exists if MTProto unavailable
            topics = await mtproto_client.get_forum_topics_by_id(  # type: ignore[attr-defined]
                group_id, [topic_id]
            )
            for t in topics:
                if t.topic_id == topic_id:
                    return True, t.title
        except Exception:
            logger.debug("_verify_topic: MTProto failed, assuming topic exists")
            return True, ""
        return False, ""

    async def _tmux_create(cwd: str, window_name: str) -> str:
        success, msg, _, window_id = await tmux_manager_obj.create_window(  # type: ignore[attr-defined]
            work_dir=cwd,
            window_name=window_name,
            start_agent=False,
            launch_command=None,
        )
        if not success:
            raise SessionLifecycleError(f"tmux create_window failed: {msg}")
        return window_id

    async def _tmux_send(window_id: str, text: str) -> None:
        ok = await tmux_manager_obj.send_keys(  # type: ignore[attr-defined]
            window_id, text, raw=True
        )
        if not ok:
            raise SessionLifecycleError(f"tmux send_keys failed for window {window_id}")

    async def _tmux_kill(window_id: str) -> None:
        await tmux_manager_obj.kill_window(window_id)  # type: ignore[attr-defined]

    def _resolve(agent: str, mode: str | None) -> str:
        mode_str = mode or ""
        return resolve_launch_command(agent, approval_mode=mode_str)

    return LifecycleDeps(
        create_forum_topic=_create_topic,
        delete_forum_topic=_delete_topic,
        verify_forum_topic=_verify_topic,
        tmux_create_window=_tmux_create,
        tmux_send_keys=_tmux_send,
        tmux_kill_window=_tmux_kill,
        resolve_launch_command=_resolve,
    )
