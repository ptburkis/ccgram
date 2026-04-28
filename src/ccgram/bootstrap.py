from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import structlog

from .config import config
from .pty_markers import list_active_markers, read_marker_for_window
from .session_lifecycle import create_session

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class BootstrapResult:
    success: bool
    session_id: str
    window_id: str
    topic_id: int
    errors: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)


def ensure_provider_settings() -> None:
    """Ensures Claude and Codex config files have required trust/permission settings."""
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        data = {}
    data["bypassPermissions"] = True
    data["skipDangerousModePermissionPrompt"] = True
    settings_path.write_text(json.dumps(data, indent=2))

    user = Path.home().name
    codex_config = Path.home() / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-reuse-imports]
        except ImportError:
            log.debug("bootstrap.codex_skip", reason="no toml reader available")
            return

    try:
        import tomli_w
    except ImportError:
        log.debug("bootstrap.codex_skip", reason="tomli_w not available")
        return

    existing: dict = {}
    if codex_config.exists():
        try:
            existing = tomllib.loads(codex_config.read_text())
        except Exception:
            existing = {}

    project_key = f"/home/{user}/projects"
    existing.setdefault("projects", {}).setdefault(project_key, {})["trust_level"] = "trusted"
    codex_config.write_bytes(tomli_w.dumps(existing))


async def _wait_for_pty_marker(window_id: str, timeout: int = 20) -> dict | None:
    """Polls every 2s for a PTY marker matching window_id; returns it or None."""
    elapsed = 0
    while elapsed < timeout:
        marker = await asyncio.to_thread(read_marker_for_window, window_id)
        if marker:
            return marker
        await asyncio.sleep(2)
        elapsed += 2
    return None


async def _heal_stuck_prompts(window_id: str) -> list[str]:
    """Detects and resolves common stuck prompts in the tmux pane."""
    healed: list[str] = []
    result = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "capture-pane", "-t", f"ccgram:{window_id}", "-p", "-S", "-10"],
        capture_output=True, text=True, timeout=5,
    )
    pane = result.stdout.lower()

    if "trust this folder" in pane or "yes, i trust" in pane:
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", f"ccgram:{window_id}", "", "Enter"],
            capture_output=True, timeout=5,
        )
        healed.append("trust_folder_prompt")
    elif "no, exit" in pane or "yes, i accept" in pane:
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", f"ccgram:{window_id}", "2", "Enter"],
            capture_output=True, timeout=5,
        )
        healed.append("bypass_prompt")
    elif "rate limit" in pane or "/upgrade" in pane or "Stop and wait" in pane:
        log.warning("bootstrap.rate_limit_cleared", window_id=window_id)
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", f"ccgram:{window_id}", "Enter"],
            capture_output=True, timeout=5,
        )
        healed.append("rate_limit_prompt")

    return healed


async def _ensure_session_map_entry(window_id: str, marker: dict) -> None:
    """Writes a session_map entry for window_id if missing or empty."""
    path = config.session_map_file
    try:
        data: dict = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}

    key = f"ccgram:{window_id}"
    if not data.get(key, {}).get("session_id"):
        data[key] = {
            "session_id": marker.get("session_id", ""),
            "cwd": marker.get("cwd", ""),
            "transcript_path": marker.get("transcript_path", ""),
            "window_name": marker.get("window_name", ""),
            "provider": marker.get("provider", ""),
        }
        path.write_text(json.dumps(data, indent=2))


async def _heal_session_map() -> None:
    """Rebuilds session_map.json entirely from all active PTY markers."""
    markers = await asyncio.to_thread(list_active_markers)
    data = {}
    for m in markers:
        wid = m.get("window_id", "")
        if wid:
            data[f"ccgram:{wid}"] = {
                "session_id": m.get("session_id", ""),
                "cwd": m.get("cwd", ""),
                "transcript_path": m.get("transcript_path", ""),
                "window_name": m.get("window_name", ""),
                "provider": m.get("provider", ""),
            }
    config.session_map_file.write_text(json.dumps(data, indent=2))


async def _verify_outbound(window_id: str, session_id: str, timeout: int = 30) -> bool:
    """Sends a probe string and checks if the transcript file grows within timeout."""
    marker = await asyncio.to_thread(read_marker_for_window, window_id)
    if not marker or not marker.get("transcript_path"):
        return False

    transcript = Path(marker["transcript_path"])
    initial_size = transcript.stat().st_size if transcript.exists() else 0

    probe = uuid4().hex
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", f"ccgram:{window_id}", probe, "Enter"],
        capture_output=True, timeout=5,
    )

    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(2)
        elapsed += 2
        if transcript.exists() and transcript.stat().st_size > initial_size:
            return True
    return False


async def bootstrap_session(
    window_name: str,
    cwd: str,
    provider: str = "claude",
    topic_id: int | None = None,
    group_id: int | None = None,
    verify: bool = True,
    on_progress=None,
) -> BootstrapResult:
    errors: list[str] = []
    healed: list[str] = []
    session_id = ""
    window_id = ""
    resolved_topic_id = topic_id or 0
    resolved_group_id = group_id if group_id is not None else config.group_id

    def _progress(msg: str) -> None:
        log.info("bootstrap.progress", msg=msg, window=window_name)
        if on_progress:
            asyncio.ensure_future(on_progress(msg))

    _progress("ensuring provider settings")
    try:
        await asyncio.to_thread(ensure_provider_settings)
    except Exception as exc:
        log.warning("bootstrap.settings_error", error=str(exc))
        errors.append(f"settings_error:{exc}")

    _progress("creating session")
    try:
        session_id = await create_session(
            cwd=cwd,
            topic_name=window_name,
            agent=provider,
            group_id=resolved_group_id,
            existing_topic_id=topic_id,
            verify_ready=False,
        )
    except Exception as exc:
        log.error("bootstrap.create_session_failed", error=str(exc))
        errors.append(f"create_session_failed:{exc}")
        return BootstrapResult(
            success=False, session_id="", window_id="",
            topic_id=resolved_topic_id, errors=errors, healed=healed,
        )

    _progress("waiting for pty marker")
    marker = await _wait_for_pty_marker(window_id=window_name, timeout=20)
    if not marker:
        healed.extend(await _heal_stuck_prompts(window_name))
        marker = await _wait_for_pty_marker(window_id=window_name, timeout=15)
    if not marker:
        errors.append("pty_marker_not_found")
    else:
        window_id = marker.get("window_id", "")
        if marker.get("topic_id"):
            resolved_topic_id = int(marker["topic_id"])

    if marker and window_id:
        await _ensure_session_map_entry(window_id, marker)

    if verify and window_id and session_id:
        _progress("verifying outbound")
        if not await _verify_outbound(window_id, session_id):
            await _heal_session_map()
            errors.append("outbound_verification_failed")

    success = not any(e for e in errors if e != "outbound_verification_failed")
    log.info(
        "bootstrap.complete",
        window=window_name, session_id=session_id, window_id=window_id,
        topic_id=resolved_topic_id, errors=errors, healed=healed, success=success,
    )
    return BootstrapResult(
        success=success, session_id=session_id, window_id=window_id,
        topic_id=resolved_topic_id, errors=errors, healed=healed,
    )
