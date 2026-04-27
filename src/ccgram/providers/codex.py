"""Codex CLI provider — OpenAI's terminal agent behind AgentProvider protocol.

Codex CLI uses a similar tmux-based launch model but differs in hook mechanism
(no SessionStart hook) and resume syntax (``resume`` subcommand, not a flag).

Transcript format: JSONL with entries ``{timestamp, type, payload}``.
Entry types: ``session_meta``, ``response_item``, ``input_item``, ``event_msg``,
``turn_context``.

Modern Codex ``response_item`` payloads use typed shapes:
  - ``type=message`` with ``role`` + content blocks
  - ``type=function_call`` with ``name``, ``arguments``, ``call_id``
  - ``type=function_call_output`` with ``call_id``, ``output``
"""

import json
from pathlib import Path
from typing import Any, cast

from ccgram.providers.codex_format import format_codex_interactive_prompt
from ccgram.providers._jsonl import JsonlProvider
from ccgram.providers.base import (
    RESUME_ID_RE,
    AgentMessage,
    MessageRole,
    ProviderCapabilities,
    SessionStartEvent,
    StatusUpdate,
    format_expandable_quote,
)
from ccgram.terminal_parser import extract_interactive_content

# Codex CLI known slash commands
# NOTE: /new excluded — collides with bot-native /new (create session)
_CODEX_BUILTINS: dict[str, str] = {
    "/clear": "Clear conversation history",
    "/compact": "Summarize context to save tokens",
    "/init": "Initialize project configuration",
    "/mcp": "List MCP tools",
    "/mention": "Attach files to conversation",
    "/mode": "Switch approval mode (suggest/auto-edit/full-auto)",
    "/model": "Switch model",
    "/permissions": "Adjust approval requirements",
    "/plan": "Enter plan mode",
    "/status": "Show session config and token usage",
}

_MAX_TOOL_SUMMARY = 200
_TOOL_NAME_ALIASES: dict[str, str] = {
    "request_user_input": "AskUserQuestion",
    "apply_patch": "Edit",
}

# Minimum line count to trigger stats + expandable quote for tool results.
_TOOL_RESULT_QUOTE_THRESHOLD = 3


def _format_codex_tool_result(raw_tool_name: str, output_text: str) -> str:
    """Format a Codex tool result with stats summary and expandable quote.

    Mirrors Claude's ``TranscriptParser._format_tool_result_text`` behaviour:
    shell/exec output gets ``N lines`` + expandable quote; short outputs stay inline.
    """
    if not output_text:
        return "Done"
    line_count = output_text.count("\n") + 1

    if raw_tool_name in ("exec_command", "shell"):
        stats = f"  \u23bf  {line_count} lines"
        return stats + "\n" + format_expandable_quote(output_text)

    if raw_tool_name == "apply_patch":
        try:
            parsed = json.loads(output_text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            result_text = parsed.get("output", "") or parsed.get("result", "")
            if isinstance(result_text, str) and result_text:
                return result_text
        return output_text

    if line_count > _TOOL_RESULT_QUOTE_THRESHOLD:
        stats = f"  \u23bf  {line_count} lines"
        return stats + "\n" + format_expandable_quote(output_text)

    return output_text


def _canonical_tool_name(name: str) -> str:
    """Map provider-native tool names to ccgram canonical names."""
    return _TOOL_NAME_ALIASES.get(name, name)


def _extract_text_blocks(content: Any) -> str:
    """Extract visible text from Codex message content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("output_text", "input_text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Parse tool arguments from stringified JSON or dict."""
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_nonempty_string(data: dict[str, Any]) -> str:
    """Return first non-empty string value in a dict."""
    for value in data.values():
        if isinstance(value, str) and value:
            return value
    return ""


def _summarize_exec_command(args: dict[str, Any]) -> str:
    """Extract a concise command preview from exec_command/shell args."""
    cmd = args.get("cmd")
    if isinstance(cmd, str) and cmd:
        return cmd

    command = args.get("command")
    if isinstance(command, list):
        joined = " ".join(part for part in command if isinstance(part, str))
        if joined:
            return joined
    if isinstance(command, str) and command:
        return command
    return ""


def _format_tool_use_text(raw_tool_name: str, args: dict[str, Any]) -> str:
    """Build display text for a Codex tool_use item."""
    tool_name = _canonical_tool_name(raw_tool_name)
    summary = _summarize_tool_use(raw_tool_name, tool_name, args)

    if summary:
        if len(summary) > _MAX_TOOL_SUMMARY:
            summary = summary[:_MAX_TOOL_SUMMARY] + "..."
        return f"**{tool_name}** `{summary}`"
    return f"**{tool_name}**"


def _summarize_tool_use(
    raw_tool_name: str,
    tool_name: str,
    args: dict[str, Any],
) -> str:
    """Create a short tool-use summary string."""
    if tool_name == "AskUserQuestion":
        return _summarize_question(args)
    if raw_tool_name in ("exec_command", "shell"):
        return _summarize_exec_command(args)
    if raw_tool_name == "write_stdin":
        chars = args.get("chars")
        return chars if isinstance(chars, str) else ""
    if raw_tool_name == "update_plan":
        plan = args.get("plan")
        if isinstance(plan, list):
            return f"{len(plan)} step(s)"
        return ""
    return _first_nonempty_string(args)


def _summarize_question(args: dict[str, Any]) -> str:
    """Extract the first question prompt for request_user_input."""
    questions = args.get("questions")
    if not (isinstance(questions, list) and questions):
        return ""
    first = questions[0]
    if not isinstance(first, dict):
        return ""
    question = first.get("question")
    return question if isinstance(question, str) else ""


def _extract_tool_output_text(output: Any) -> str:
    """Extract the useful output section from Codex function_call_output."""
    if isinstance(output, str):
        marker = "\nOutput:\n"
        if marker in output:
            return output.split(marker, 1)[1].strip()
        return output.strip()
    if isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False)
    return ""


def _format_request_user_input_result(output_text: str) -> str:
    """Summarize request_user_input answers."""
    if not output_text:
        return output_text
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError:
        return output_text
    if not isinstance(parsed, dict):
        return output_text

    answers = parsed.get("answers")
    if not isinstance(answers, dict):
        return output_text

    selected: list[str] = []
    for answer in answers.values():
        if not isinstance(answer, dict):
            continue
        items = answer.get("answers")
        if isinstance(items, list):
            selected.extend(item for item in items if isinstance(item, str) and item)

    if selected:
        return "Selected: " + ", ".join(selected)
    return output_text


def _parse_custom_tool_call(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse a custom_tool_call payload (e.g. apply_patch)."""
    raw_name_value = payload.get("name", "unknown")
    raw_name = (
        raw_name_value if isinstance(raw_name_value, str) else str(raw_name_value)
    )
    tool_name = _canonical_tool_name(raw_name)
    call_id = payload.get("call_id", "")
    if isinstance(call_id, str) and call_id:
        pending[call_id] = (raw_name, tool_name)

    # For apply_patch, summarize by counting file updates in the input string.
    input_text = payload.get("input", "")
    summary = ""
    if raw_name == "apply_patch" and isinstance(input_text, str):
        file_count = input_text.count("*** Update File:")
        file_count += input_text.count("*** Add File:")
        file_count += input_text.count("*** Delete File:")
        if file_count:
            summary = f"{file_count} file(s)"
    if not summary and isinstance(input_text, str) and input_text:
        summary = input_text[:_MAX_TOOL_SUMMARY]
        if len(input_text) > _MAX_TOOL_SUMMARY:
            summary += "..."

    text = f"**{tool_name}** `{summary}`" if summary else f"**{tool_name}**"
    return (
        [
            AgentMessage(
                text=text,
                role="assistant",
                content_type="tool_use",
                tool_use_id=call_id or None,
                tool_name=tool_name,
            )
        ],
        pending,
    )


def _resolve_pending(
    call_id: Any,
    pending: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Pop a pending tool entry and return (raw_name, tool_name).

    Handles both current tuple format and legacy string values.
    """
    resolved = (
        pending.pop(call_id, None) if isinstance(call_id, str) and call_id else None
    )
    if isinstance(resolved, tuple):
        return resolved[0], resolved[1]
    if isinstance(resolved, str):
        return resolved, resolved
    return None, None


def _parse_custom_tool_call_output(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse a custom_tool_call_output payload."""
    call_id = payload.get("call_id", "")
    raw_name, tool_name = _resolve_pending(call_id, pending)

    # Output is typically JSON-wrapped: {"output": "..."}.
    raw_output = payload.get("output", "")
    output_text = ""
    if isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and "output" in parsed:
            output_text = str(parsed["output"]).strip()
        else:
            output_text = raw_output.strip()
    elif isinstance(raw_output, dict) and "output" in raw_output:
        output_text = str(raw_output["output"]).strip()

    if raw_name and output_text:
        output_text = _format_codex_tool_result(raw_name, output_text)
    if not output_text:
        output_text = "Done"

    return (
        [
            AgentMessage(
                text=output_text,
                role="assistant",
                content_type="tool_result",
                tool_use_id=call_id if isinstance(call_id, str) else None,
                tool_name=tool_name,
            )
        ],
        pending,
    )


def _parse_codex_response_item(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse a Codex response_item payload."""
    payload_type = payload.get("type", "")
    if payload_type == "function_call":
        return _parse_function_call(payload, pending)
    if payload_type == "function_call_output":
        return _parse_function_call_output(payload, pending)
    if payload_type == "custom_tool_call":
        return _parse_custom_tool_call(payload, pending)
    if payload_type == "custom_tool_call_output":
        return _parse_custom_tool_call_output(payload, pending)
    return _parse_response_message(payload, pending)


def _parse_function_call(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse a function_call payload into a tool_use AgentMessage."""
    raw_name_value = payload.get("name", "unknown")
    raw_name = (
        raw_name_value if isinstance(raw_name_value, str) else str(raw_name_value)
    )
    tool_name = _canonical_tool_name(raw_name)
    call_id = payload.get("call_id", "")
    if isinstance(call_id, str) and call_id:
        pending[call_id] = (raw_name, tool_name)
    args = _parse_tool_arguments(payload.get("arguments", {}))
    return (
        [
            AgentMessage(
                text=_format_tool_use_text(raw_name, args),
                role="assistant",
                content_type="tool_use",
                tool_use_id=call_id or None,
                tool_name=tool_name,
            )
        ],
        pending,
    )


def _parse_function_call_output(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse a function_call_output payload into a tool_result AgentMessage."""
    call_id = payload.get("call_id", "")
    raw_name, tool_name = _resolve_pending(call_id, pending)

    output_text = _extract_tool_output_text(payload.get("output", ""))
    if tool_name == "AskUserQuestion":
        output_text = _format_request_user_input_result(output_text)
    elif raw_name and output_text:
        output_text = _format_codex_tool_result(raw_name, output_text)
    if not output_text:
        output_text = "Done"

    return (
        [
            AgentMessage(
                text=output_text,
                role="assistant",
                content_type="tool_result",
                tool_use_id=call_id if isinstance(call_id, str) else None,
                tool_name=tool_name,
            )
        ],
        pending,
    )


def _parse_response_message(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse message payloads (assistant/user text)."""
    role = payload.get("role", "")
    if role not in ("user", "assistant"):
        return [], pending
    text = _extract_text_blocks(payload.get("content", ""))
    if not text:
        return [], pending
    return (
        [
            AgentMessage(
                text=text,
                role=cast(MessageRole, role),
                content_type="text",
            )
        ],
        pending,
    )


def _parse_event_message(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse Codex event_msg payloads that carry assistant-visible text."""
    payload_type = payload.get("type", "")
    if payload_type != "agent_message":
        return [], pending
    text = payload.get("message", "")
    if not isinstance(text, str) or not text:
        return [], pending
    return (
        [AgentMessage(text=text, role="assistant", content_type="text")],
        pending,
    )


def _parse_input_item(
    payload: dict[str, Any],
    pending: dict[str, Any],
) -> tuple[list[AgentMessage], dict[str, Any]]:
    """Parse Codex input_item payloads."""
    if payload.get("role", "") != "user":
        return [], pending
    content = payload.get("content", "")
    if not isinstance(content, str) or not content:
        return [], pending
    return ([AgentMessage(text=content, role="user", content_type="text")], pending)


def _append_unique_messages(
    dest: list[AgentMessage],
    candidates: list[AgentMessage],
    last_signature: tuple[str, str, str] | None,
) -> tuple[str, str, str] | None:
    """Append messages while dropping exact adjacent duplicates."""
    current = last_signature
    for message in candidates:
        signature = (message.role, message.content_type, message.text)
        if signature == current:
            continue
        dest.append(message)
        current = signature
    return current


# Transcripts older than this are considered stale and skipped during discovery.
# Prevents matching a finished session when a new Codex window opens in the same cwd.
_TRANSCRIPT_MAX_AGE_SECS = 120.0


def _collect_codex_sessions(sessions_dir: Path) -> list[tuple[float, Path]]:
    """Collect all JSONL files under sessions_dir, sorted newest-first by mtime."""
    result: list[tuple[float, Path]] = []
    for fpath in sessions_dir.rglob("*.jsonl"):
        try:
            result.append((fpath.stat().st_mtime, fpath))
        except OSError:
            continue
    result.sort(reverse=True)
    return result


def _read_codex_session_meta(fpath: Path) -> dict[str, Any] | None:
    """Read the session_meta payload from the first line of a Codex JSONL file."""
    try:
        with open(fpath, encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return None
    if not first_line:
        return None
    try:
        data = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if data.get("type") != "session_meta":
        return None
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else None


def _resolve_jsonl_for_pty(pane_tty: str) -> Path | None:
    """Find the Codex transcript JSONL currently open by the process on *pane_tty*.

    On Linux, walks ``/proc/*/fd/`` looking for a process whose stdin (fd 0)
    is the given tty AND that has a ``~/.codex/sessions/...jsonl`` open in
    any other fd. Used to disambiguate between multiple Codex processes in
    the same project directory — different processes have different ttys
    even if they have the same cwd. Returns None on non-Linux or if no
    matching process / open transcript is found.

    Note: only the directly-attached process (e.g. ``node /usr/bin/codex``)
    has the tty as fd 0; the actual rust ``codex`` binary that writes the
    JSONL is its child. We walk to find children whose parent is on that tty.
    """
    if not pane_tty:
        return None
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None  # not Linux

    # Step 1: find the pid whose fd 0 is pane_tty.
    # Note: Path.readlink() returns a Path, not a str — must convert with str()
    # for the comparison or it silently never matches.
    parent_pid: int | None = None
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fd0 = (entry / "fd" / "0").readlink()
            except OSError:
                continue
            if str(fd0) == pane_tty:
                parent_pid = int(entry.name)
                break
    except OSError:
        return None

    if parent_pid is None:
        return None

    # Step 2: collect parent and all descendant pids (codex spawns a child
    # rust binary that holds the JSONL fd).
    pids_to_check: list[int] = [parent_pid]
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_data = (entry / "stat").read_text()
            except OSError:
                continue
            # /proc/<pid>/stat fields after comm: state, ppid, ...
            # comm is in parens and may contain spaces, so split from the right.
            try:
                rparen = stat_data.rindex(")")
                fields = stat_data[rparen + 2 :].split()
                ppid = int(fields[1])
            except (ValueError, IndexError):
                continue
            if ppid == parent_pid:
                pids_to_check.append(int(entry.name))
    except OSError:
        return None

    # Recurse one more level for grandchildren (covers node → wrapper → rust).
    extra: list[int] = []
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_data = (entry / "stat").read_text()
            except OSError:
                continue
            try:
                rparen = stat_data.rindex(")")
                fields = stat_data[rparen + 2 :].split()
                ppid = int(fields[1])
            except (ValueError, IndexError):
                continue
            if ppid in pids_to_check and int(entry.name) not in pids_to_check:
                extra.append(int(entry.name))
    except OSError:
        pass
    pids_to_check.extend(extra)

    # Step 3: scan fds for any open Codex JSONL transcript.
    sessions_dir = str(Path.home() / ".codex" / "sessions")
    for pid in pids_to_check:
        try:
            fd_dir = proc_root / str(pid) / "fd"
            for fd in fd_dir.iterdir():
                try:
                    target = str(fd.readlink())
                except OSError:
                    continue
                if target.startswith(sessions_dir) and target.endswith(".jsonl"):
                    return Path(target)
        except OSError:
            continue
    return None


class CodexProvider(JsonlProvider):
    """AgentProvider implementation for OpenAI Codex CLI."""

    _CAPS = ProviderCapabilities(
        name="codex",
        launch_command="codex",
        supports_hook=False,
        supports_resume=True,
        supports_continue=True,
        supports_structured_transcript=True,
        transcript_format="jsonl",
        builtin_commands=tuple(_CODEX_BUILTINS.keys()),
        supports_user_command_discovery=True,
        supports_status_snapshot=True,
    )

    _BUILTINS = _CODEX_BUILTINS

    def make_launch_args(
        self,
        resume_id: str | None = None,
        use_continue: bool = False,
    ) -> str:
        """Build Codex CLI args for launching or resuming a session.

        Resume uses ``resume <id>`` subcommand syntax.
        Continue uses ``resume --last`` to pick up the most recent session.
        """
        if resume_id:
            if not RESUME_ID_RE.match(resume_id):
                raise ValueError(f"Invalid resume_id: {resume_id!r}")
            return f"resume {resume_id}"
        if use_continue:
            return "resume --last"
        return ""

    # ── Codex-specific transcript parsing ─────────────────────────────

    def parse_transcript_entries(
        self,
        entries: list[dict[str, Any]],
        pending_tools: dict[str, Any],
        cwd: str | None = None,  # noqa: ARG002
    ) -> tuple[list[AgentMessage], dict[str, Any]]:
        """Parse Codex JSONL entries into AgentMessages."""
        messages: list[AgentMessage] = []
        pending = dict(pending_tools)
        last_signature: tuple[str, str, str] | None = None

        for entry in entries:
            entry_type = entry.get("type", "")
            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                continue

            if entry_type == "response_item":
                parsed, pending = _parse_codex_response_item(payload, pending)
                last_signature = _append_unique_messages(
                    messages, parsed, last_signature
                )
                continue

            if entry_type == "event_msg":
                parsed, pending = _parse_event_message(payload, pending)
                last_signature = _append_unique_messages(
                    messages, parsed, last_signature
                )
                continue

            if entry_type == "input_item":
                parsed, pending = _parse_input_item(payload, pending)
                last_signature = _append_unique_messages(
                    messages, parsed, last_signature
                )

        return messages, pending

    def is_user_transcript_entry(self, entry: dict[str, Any]) -> bool:
        """Check if this Codex entry is a human turn."""
        entry_type = entry.get("type", "")
        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            return False
        if entry_type == "response_item" and payload.get("role") == "user":
            # Skip system/developer messages that look like user
            content = payload.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "input_text":
                        text = block.get("text", "")
                        if text.startswith(("<permissions", "<environment_context")):
                            return False
            return True
        return entry_type == "input_item" and payload.get("role") == "user"

    def parse_history_entry(self, entry: dict[str, Any]) -> AgentMessage | None:
        """Parse a single Codex transcript entry for history display."""
        entry_type = entry.get("type", "")
        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            return None

        if entry_type == "response_item":
            role = payload.get("role", "")
            if role not in ("user", "assistant"):
                return None
            content = payload.get("content", "")
            text = _extract_text_blocks(content)
            if not text:
                return None
            return AgentMessage(
                text=text,
                role=cast(MessageRole, role),
                content_type="text",
            )
        if entry_type == "input_item" and payload.get("role") == "user":
            content = payload.get("content", "")
            text = content if isinstance(content, str) else ""
            if not text:
                return None
            return AgentMessage(text=text, role="user", content_type="text")

        return None

    def parse_terminal_status(
        self,
        pane_text: str,
        *,
        pane_title: str = "",  # noqa: ARG002
    ) -> StatusUpdate | None:
        """Parse Codex pane content for interactive prompts."""
        interactive = extract_interactive_content(pane_text)
        if interactive:
            formatted = format_codex_interactive_prompt(
                interactive.content, interactive.name
            )
            return StatusUpdate(
                raw_text=formatted,
                display_label=interactive.name,
                is_interactive=True,
                ui_type=interactive.name,
            )
        return None

    def discover_transcript(
        self,
        cwd: str,
        window_key: str,
        *,
        max_age: float | None = None,
        pane_tty: str = "",
    ) -> SessionStartEvent | None:
        """Scan ~/.codex/sessions/ for the transcript belonging to this pane.

        Codex transcript path: ~/.codex/sessions/YYYY/MM/DD/<name>-<ts>-<uuid>.jsonl
        First line: {"type": "session_meta", "payload": {"id": "<uuid>", "cwd": "..."}}

        Resolution strategy:
          1. **pty-based** (Linux only): if ``pane_tty`` is provided, find the
             specific Codex process using that tty and look up the JSONL it
             has open in /proc/<pid>/fd/. This correctly disambiguates between
             multiple Codex processes running in the same project directory.
          2. **cwd-based fallback**: if pty resolution fails or yields nothing,
             scan recent transcripts and pick the newest one whose ``session_meta``
             cwd matches. This is racy when two Codex sessions share a cwd but
             is the only option on macOS / when /proc isn't available.

        Args:
            max_age: Maximum transcript age in seconds. ``None`` uses the
                default ``_TRANSCRIPT_MAX_AGE_SECS`` (120s). Pass ``0`` or
                negative to disable the age check entirely.
            pane_tty: Controlling tty of the tmux pane (e.g. ``/dev/pts/14``).
                When set, used for pty-based disambiguation.
        """
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.is_dir():
            return None

        import time

        # Strategy 1: pty-based disambiguation.
        if pane_tty:
            jsonl_path = _resolve_jsonl_for_pty(pane_tty)
            if jsonl_path is not None:
                meta = _read_codex_session_meta(jsonl_path)
                if meta:
                    session_id = meta.get("id", "")
                    file_cwd = meta.get("cwd", "") or cwd
                    if session_id:
                        return SessionStartEvent(
                            session_id=session_id,
                            cwd=file_cwd,
                            transcript_path=str(jsonl_path),
                            window_key=window_key,
                            pty_resolved=True,
                        )

        # Strategy 2: cwd-based fallback (legacy behavior).
        age_limit = _TRANSCRIPT_MAX_AGE_SECS if max_age is None else max_age

        jsonl_files = _collect_codex_sessions(sessions_dir)
        now = time.time()
        resolved_cwd = str(Path(cwd).resolve())
        for mtime, fpath in jsonl_files[:20]:
            if age_limit > 0 and now - mtime > age_limit:
                break  # sorted newest-first; remaining are all older
            meta = _read_codex_session_meta(fpath)
            if not meta:
                continue
            file_cwd = meta.get("cwd", "")
            if file_cwd and str(Path(file_cwd).resolve()) == resolved_cwd:
                session_id = meta.get("id", "")
                if session_id:
                    return SessionStartEvent(
                        session_id=session_id,
                        cwd=file_cwd,
                        transcript_path=str(fpath),
                        window_key=window_key,
                    )
        return None

    # ── Status snapshot (Codex-specific) ─────────────────────────────

    def build_status_snapshot(
        self,
        transcript_path: str,
        *,
        display_name: str = "",
        session_id: str = "",
        cwd: str = "",
    ) -> str | None:
        from ccgram.providers.codex_status import build_codex_status_snapshot

        return build_codex_status_snapshot(
            transcript_path,
            display_name=display_name,
            session_id=session_id,
            cwd=cwd,
        )

    def has_output_since(self, transcript_path: str, offset: int) -> bool:
        from ccgram.providers.codex_status import has_codex_assistant_output_since

        return has_codex_assistant_output_since(transcript_path, offset)
