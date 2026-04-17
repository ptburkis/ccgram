import pytest

from ccgram.claude_task_state import (
    classify_wait_message,
    claude_task_state,
    get_claude_task_snapshot,
)


def _assistant_tool_use(tool_id: str, name: str, input_data: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": input_data,
                }
            ]
        },
    }


def _user_tool_result(
    tool_use_id: str,
    *,
    content: str = "",
    tool_use_result: dict | None = None,
) -> dict:
    entry: dict = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ]
        },
    }
    if tool_use_result is not None:
        entry["toolUseResult"] = tool_use_result
    return entry


class TestClaudeTaskStateStore:
    def test_task_create_then_result_creates_snapshot(self) -> None:
        changed = claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-1",
                    "TaskCreate",
                    {
                        "subject": "Review architecture",
                        "description": "Inspect current layering",
                        "activeForm": "Reviewing architecture",
                    },
                ),
                _user_tool_result(
                    "tool-1",
                    content="Task #1 created successfully",
                    tool_use_result={
                        "task": {"id": "1", "subject": "Review architecture"}
                    },
                ),
            ],
        )

        assert changed is True
        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert snapshot.total_count == 1
        assert snapshot.open_count == 1
        assert snapshot.items[0].task_id == "1"
        assert snapshot.items[0].subject == "Review architecture"
        assert snapshot.items[0].active_form == "Reviewing architecture"

    def test_task_update_changes_status_and_blockers(self) -> None:
        claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-1",
                    "TaskCreate",
                    {
                        "subject": "Task one",
                        "description": "Desc one",
                        "activeForm": "Doing task one",
                    },
                ),
                _user_tool_result(
                    "tool-1",
                    tool_use_result={"task": {"id": "1", "subject": "Task one"}},
                ),
            ],
        )

        changed = claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-2",
                    "TaskUpdate",
                    {
                        "taskId": "1",
                        "status": "in_progress",
                        "addBlockedBy": ["7"],
                    },
                )
            ],
        )

        assert changed is True
        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert snapshot.active_task_id == "1"
        assert snapshot.items[0].status == "in_progress"
        assert snapshot.items[0].blocked_by == ("7",)

        claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-3",
                    "TaskUpdate",
                    {
                        "taskId": "1",
                        "status": "completed",
                        "removeBlockedBy": ["7"],
                    },
                )
            ],
        )

        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert snapshot.done_count == 1
        assert snapshot.open_count == 0
        assert snapshot.items[0].blocked_by == ()

    def test_task_list_replaces_existing_snapshot(self) -> None:
        claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-1",
                    "TaskCreate",
                    {"subject": "Old task", "description": "", "activeForm": ""},
                ),
                _user_tool_result(
                    "tool-1",
                    tool_use_result={"task": {"id": "1", "subject": "Old task"}},
                ),
            ],
        )

        changed = claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use("tool-list", "TaskList", {}),
                _user_tool_result(
                    "tool-list",
                    content="#2 [pending] Aggregate findings [blocked by #1]",
                    tool_use_result={
                        "tasks": [
                            {
                                "id": "1",
                                "subject": "Collect findings",
                                "status": "completed",
                                "blockedBy": [],
                            },
                            {
                                "id": "2",
                                "subject": "Aggregate findings",
                                "status": "pending",
                                "blockedBy": ["1"],
                                "owner": "reviewer",
                            },
                        ]
                    },
                ),
            ],
        )

        assert changed is True
        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert [item.task_id for item in snapshot.items] == ["1", "2"]
        assert snapshot.done_count == 1
        assert snapshot.items[1].blocked_by == ("1",)
        assert snapshot.items[1].owner == "reviewer"

    def test_todowrite_replaces_snapshot(self) -> None:
        changed = claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "todo-1",
                    "TodoWrite",
                    {
                        "todos": [
                            {
                                "content": "Investigate regression",
                                "status": "completed",
                            },
                            {
                                "content": "Write tests",
                                "status": "in_progress",
                                "activeForm": "Writing tests",
                            },
                        ]
                    },
                )
            ],
        )

        assert changed is True
        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert snapshot.total_count == 2
        assert snapshot.done_count == 1
        assert snapshot.active_task_id == "2"
        assert snapshot.items[1].active_form == "Writing tests"

    def test_session_change_replaces_old_snapshot(self) -> None:
        claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-1",
                    "TaskCreate",
                    {"subject": "Old task", "description": "", "activeForm": ""},
                ),
                _user_tool_result(
                    "tool-1",
                    tool_use_result={"task": {"id": "1", "subject": "Old task"}},
                ),
            ],
        )

        claude_task_state.apply_entries(
            "@0",
            "session-2",
            [
                _assistant_tool_use(
                    "tool-2",
                    "TaskCreate",
                    {"subject": "New task", "description": "", "activeForm": ""},
                ),
                _user_tool_result(
                    "tool-2",
                    tool_use_result={"task": {"id": "9", "subject": "New task"}},
                ),
            ],
        )

        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert [item.task_id for item in snapshot.items] == ["9"]

    def test_mark_task_completed_requires_matching_session(self) -> None:
        claude_task_state.apply_entries(
            "@0",
            "session-1",
            [
                _assistant_tool_use(
                    "tool-1",
                    "TaskCreate",
                    {"subject": "Task one", "description": "", "activeForm": ""},
                ),
                _user_tool_result(
                    "tool-1",
                    tool_use_result={"task": {"id": "1", "subject": "Task one"}},
                ),
            ],
        )

        assert claude_task_state.mark_task_completed("@0", "session-2", "1") is False
        assert claude_task_state.mark_task_completed("@0", "session-1", "1") is True

        snapshot = get_claude_task_snapshot("@0")
        assert snapshot is not None
        assert snapshot.done_count == 1


class TestLastStatus:
    @pytest.fixture(autouse=True)
    def _reset(self):
        claude_task_state.reset()
        yield
        claude_task_state.reset()

    def test_set_and_get(self) -> None:
        claude_task_state.set_last_status("@0", "Running tests")
        assert claude_task_state.get_last_status("@0") == "Running tests"

    def test_get_returns_none_when_unset(self) -> None:
        assert claude_task_state.get_last_status("@99") is None

    def test_clear_window_clears_last_status(self) -> None:
        claude_task_state.set_last_status("@0", "Working")
        claude_task_state.clear_window("@0")
        assert claude_task_state.get_last_status("@0") is None

    def test_reset_clears_last_status(self) -> None:
        claude_task_state.set_last_status("@0", "Working")
        claude_task_state.reset()
        assert claude_task_state.get_last_status("@0") is None

    def test_overwrite(self) -> None:
        claude_task_state.set_last_status("@0", "Reading")
        claude_task_state.set_last_status("@0", "Writing")
        assert claude_task_state.get_last_status("@0") == "Writing"


class TestFormatCompletionText:
    @pytest.fixture(autouse=True)
    def _reset(self):
        claude_task_state.reset()
        yield
        claude_task_state.reset()

    def test_bare_ready_when_nothing_available(self) -> None:
        result = claude_task_state.format_completion_text("@0")
        assert result == "\u2713 Ready"

    def test_with_last_status_only(self) -> None:
        claude_task_state.set_last_status("@0", "Running make test")
        result = claude_task_state.format_completion_text("@0")
        assert "\u2713 Ready" in result
        assert "Last: Running make test" in result

    def test_with_last_status_and_turns(self) -> None:
        claude_task_state.set_last_status("@0", "Running make test")
        result = claude_task_state.format_completion_text("@0", num_turns=12)
        assert "Last: Running make test" in result
        assert "12 turns" in result

    def test_with_task_checklist(self) -> None:
        claude_task_state.apply_entries(
            "@0",
            "s1",
            [
                _assistant_tool_use(
                    "tu1", "TaskCreate", {"subject": "write tests", "description": ""}
                ),
                _user_tool_result(
                    "tu1",
                    content="ok",
                    tool_use_result={"task": {"id": "1", "subject": "write tests"}},
                ),
                _assistant_tool_use(
                    "tu2", "TaskCreate", {"subject": "run linter", "description": ""}
                ),
                _user_tool_result(
                    "tu2",
                    content="ok",
                    tool_use_result={"task": {"id": "2", "subject": "run linter"}},
                ),
            ],
        )
        claude_task_state.apply_entries(
            "@0",
            "s1",
            [
                _assistant_tool_use(
                    "tu3",
                    "TaskUpdate",
                    {"taskId": "1", "status": "completed"},
                ),
            ],
        )
        result = claude_task_state.format_completion_text("@0", num_turns=5)
        assert "\u2713 Ready" in result
        assert "\u2714 write tests" in result
        assert "run linter" in result
        assert "1/2 tasks done" in result
        assert "5 turns" in result

    def test_with_task_checklist_no_turns(self) -> None:
        claude_task_state.apply_entries(
            "@0",
            "s1",
            [
                _assistant_tool_use(
                    "tu1", "TaskCreate", {"subject": "write tests", "description": ""}
                ),
                _user_tool_result(
                    "tu1",
                    content="ok",
                    tool_use_result={"task": {"id": "1", "subject": "write tests"}},
                ),
            ],
        )
        result = claude_task_state.format_completion_text("@0")
        assert "0/1 tasks done" in result
        assert "turns" not in result

    def test_task_checklist_takes_priority_over_last_status(self) -> None:
        claude_task_state.set_last_status("@0", "Some status")
        claude_task_state.apply_entries(
            "@0",
            "s1",
            [
                _assistant_tool_use(
                    "tu1", "TaskCreate", {"subject": "do thing", "description": ""}
                ),
                _user_tool_result(
                    "tu1",
                    content="ok",
                    tool_use_result={"task": {"id": "1", "subject": "do thing"}},
                ),
            ],
        )
        result = claude_task_state.format_completion_text("@0")
        assert "do thing" in result
        assert "Last:" not in result


class TestClassifyWaitMessage:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Claude is waiting for your input", "Waiting for input"),
            ("Claude needs your permission to use Bash", "Approval needed: Bash"),
            (
                "Claude needs your permission to use Updated plan",
                "Plan approval needed",
            ),
            ("something else", None),
        ],
    )
    def test_classifies_wait_messages(self, message: str, expected: str | None) -> None:
        assert classify_wait_message(message) == expected



class TestRebuildFromEntries:
    @pytest.fixture(autouse=True)
    def _reset(self):
        claude_task_state.reset()
        yield
        claude_task_state.reset()

    def test_stale_marker_skips_rebuild(self) -> None:
        """When PTY marker shows a different session_id, rebuild is skipped."""
        import unittest.mock as mock
        entries = [
            _assistant_tool_use(
                'tool-1',
                'TaskCreate',
                {'subject': 'Stale task', 'description': '', 'activeForm': ''},
            ),
            _user_tool_result(
                'tool-1',
                tool_use_result={'task': {'id': '1', 'subject': 'Stale task'}},
            ),
        ]
        with mock.patch('ccgram.pty_markers.read_marker_for_window') as mock_marker:
            mock_marker.return_value = {'session_id': 'live-session-99'}
            result = claude_task_state.rebuild_from_entries('@stale', 'old-session-1', entries)

        assert result is False
        snapshot = get_claude_task_snapshot('@stale')
        assert snapshot is None

    def test_matching_marker_proceeds(self) -> None:
        """When PTY marker session_id matches, rebuild proceeds normally."""
        import unittest.mock as mock
        entries = [
            _assistant_tool_use(
                'tool-1',
                'TaskCreate',
                {'subject': 'Current task', 'description': '', 'activeForm': ''},
            ),
            _user_tool_result(
                'tool-1',
                tool_use_result={'task': {'id': '1', 'subject': 'Current task'}},
            ),
        ]
        with mock.patch('ccgram.pty_markers.read_marker_for_window') as mock_marker:
            mock_marker.return_value = {'session_id': 'live-session-1'}
            result = claude_task_state.rebuild_from_entries('@match', 'live-session-1', entries)

        assert result is True
        snapshot = get_claude_task_snapshot('@match')
        assert snapshot is not None
        assert snapshot.total_count == 1

    def test_none_marker_proceeds(self) -> None:
        """When PTY marker returns None, rebuild proceeds normally."""
        import unittest.mock as mock
        entries = [
            _assistant_tool_use(
                'tool-1',
                'TaskCreate',
                {'subject': 'No marker task', 'description': '', 'activeForm': ''},
            ),
            _user_tool_result(
                'tool-1',
                tool_use_result={'task': {'id': '1', 'subject': 'No marker task'}},
            ),
        ]
        with mock.patch('ccgram.pty_markers.read_marker_for_window') as mock_marker:
            mock_marker.return_value = None
            result = claude_task_state.rebuild_from_entries('@nomarker', 'some-session', entries)

        assert result is True
        snapshot = get_claude_task_snapshot('@nomarker')
        assert snapshot is not None
        assert snapshot.total_count == 1

    def test_import_exception_proceeds(self) -> None:
        """When pty_markers import raises, exception is swallowed and rebuild proceeds."""
        import unittest.mock as mock
        entries = [
            _assistant_tool_use(
                'tool-1',
                'TaskCreate',
                {'subject': 'Exception task', 'description': '', 'activeForm': ''},
            ),
            _user_tool_result(
                'tool-1',
                tool_use_result={'task': {'id': '1', 'subject': 'Exception task'}},
            ),
        ]
        with mock.patch('ccgram.pty_markers.read_marker_for_window', side_effect=RuntimeError('boom')):
            result = claude_task_state.rebuild_from_entries('@exc', 'exc-session', entries)

        assert result is True
        snapshot = get_claude_task_snapshot('@exc')
        assert snapshot is not None
        assert snapshot.total_count == 1
