"""Tests for sync_check module."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.sync_check import (
    SyncCheckItem,
    SyncCheckReport,
    _strip_all_suffixes,
    run_sync_check,
)


class TestStripAllSuffixes:
    def test_strips_bolt(self):
        assert _strip_all_suffixes("bulugo-dev ⚡") == "bulugo-dev"

    def test_strips_shell(self):
        assert _strip_all_suffixes("paint-my-room 🐚") == "paint-my-room"

    def test_strips_both(self):
        assert _strip_all_suffixes("proj 🐚 ⚡") == "proj"

    def test_strips_effort(self):
        assert _strip_all_suffixes("project [H]") == "project"
        assert _strip_all_suffixes("project [M]") == "project"
        assert _strip_all_suffixes("project [L]") == "project"

    def test_strips_all_combined(self):
        assert _strip_all_suffixes("proj 🐚 ⚡ [H]") == "proj"

    def test_clean_name_unchanged(self):
        assert _strip_all_suffixes("clean-project") == "clean-project"


class TestSyncCheckItem:
    def _make_item(self, **kwargs):
        defaults = dict(
            window_id="@2",
            topic_id=42,
            telegram_title="bulugo-dev",
            correct_name="bulugo-dev",
            has_bolt=False,
            should_bolt=False,
            has_shell=False,
            should_shell=False,
            has_effort=False,
            name_match=True,
        )
        defaults.update(kwargs)
        return SyncCheckItem(**defaults)

    def test_not_drifted_when_all_ok(self):
        item = self._make_item()
        assert not item.drifted

    def test_drifted_bolt_mismatch(self):
        item = self._make_item(has_bolt=True, should_bolt=False)
        assert item.drifted

    def test_drifted_shell_mismatch(self):
        item = self._make_item(has_shell=False, should_shell=True)
        assert item.drifted

    def test_drifted_effort_present(self):
        item = self._make_item(has_effort=True)
        assert item.drifted

    def test_drifted_name_mismatch(self):
        item = self._make_item(name_match=False)
        assert item.drifted

    def test_not_drifted_bolt_both_true(self):
        item = self._make_item(has_bolt=True, should_bolt=True)
        assert not item.drifted


class TestSyncCheckReport:
    def test_empty_report(self):
        r = SyncCheckReport()
        assert r.total == 0
        assert r.drifted_count == 0

    def test_counts_correctly(self):
        r = SyncCheckReport()
        r.items.append(
            SyncCheckItem(
                window_id="@1", topic_id=1, telegram_title="ok", correct_name="ok",
                has_bolt=False, should_bolt=False, has_shell=False, should_shell=False,
                has_effort=False, name_match=True,
            )
        )
        r.items.append(
            SyncCheckItem(
                window_id="@2", topic_id=2, telegram_title="drifted ⚡", correct_name="drifted",
                has_bolt=True, should_bolt=False, has_shell=False, should_shell=False,
                has_effort=False, name_match=True,
            )
        )
        assert r.total == 2
        assert r.drifted_count == 1


class TestRunSyncCheck:
    def test_empty_bindings(self):
        """With no thread bindings, report should be empty."""
        with patch("ccgram.sync_check.asyncio.run", return_value=SyncCheckReport()) as mock_run:
            report = run_sync_check(fix=False)
        assert report.total == 0
        assert report.drifted_count == 0


class TestIsAgentActive:
    def test_returns_false_when_no_subagents_and_no_tmux(self):
        from ccgram.sync_check import _is_agent_active

        with (
            patch("ccgram.sync_check.subprocess.run") as mock_run,
            patch.dict("ccgram.handlers.hook_events._active_subagents", {}, clear=True),
        ):
            mock_run.return_value = MagicMock(returncode=1)
            result = _is_agent_active("@99")
        assert not result

    def test_returns_true_when_subagent_tracked(self):
        from ccgram.sync_check import _is_agent_active
        from ccgram.handlers.hook_events import _active_subagents

        _active_subagents["@5"] = {"sub1": "MyTool"}
        try:
            result = _is_agent_active("@5")
            assert result
        finally:
            _active_subagents.pop("@5", None)


class TestCliSyncCheck:
    def test_sync_check_help(self):
        from click.testing import CliRunner
        from ccgram.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-check", "--help"])
        assert result.exit_code == 0
        assert "--fix" in result.output
        assert "--json" in result.output

    def test_sync_check_empty_json(self):
        """sync-check --json with no bindings should produce valid JSON."""
        import json
        from click.testing import CliRunner
        from ccgram.cli import cli

        runner = CliRunner()
        with patch("ccgram.sync_check.asyncio.run", return_value=SyncCheckReport()):
            result = runner.invoke(cli, ["sync-check", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 0
        assert data["drifted"] == 0
        assert data["items"] == []

    def test_sync_check_table_output(self):
        """sync-check table output should show header and summary."""
        from click.testing import CliRunner
        from ccgram.cli import cli

        runner = CliRunner()
        report = SyncCheckReport()
        report.items.append(
            SyncCheckItem(
                window_id="@2",
                topic_id=100,
                telegram_title="bulugo-dev",
                correct_name="bulugo-dev",
                has_bolt=False,
                should_bolt=False,
                has_shell=False,
                should_shell=False,
                has_effort=False,
                name_match=True,
            )
        )
        with patch("ccgram.sync_check.asyncio.run", return_value=report):
            result = runner.invoke(cli, ["sync-check"])
        assert result.exit_code == 0
        assert "bulugo-dev" in result.output
        assert "0 drifted / 1 total" in result.output
