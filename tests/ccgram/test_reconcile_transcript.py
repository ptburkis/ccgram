"""Tests for the transcript reconciliation tool (reconcile_transcript module)."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour: int, minute: int = 0) -> str:
    return f"2026-04-16T{hour:02d}:{minute:02d}:00.000Z"


def _since() -> datetime:
    return datetime(2026, 4, 16, 6, 0, tzinfo=timezone.utc)


def _make_jsonl_line(text: str, hour: int, minute: int = 0) -> str:
    entry = {
        "type": "assistant",
        "timestamp": _ts(hour, minute),
        "message": {
            "content": [{"type": "text", "text": text}]
        },
    }
    return json.dumps(entry)


# ---------------------------------------------------------------------------
# read_jsonl_assistant_messages
# ---------------------------------------------------------------------------


def test_read_jsonl_filters_before_since(tmp_path: Path) -> None:
    from ccgram.reconcile_transcript import read_jsonl_assistant_messages

    lines = [
        _make_jsonl_line("before window", 5, 59),
        _make_jsonl_line("in window", 6, 1),
        _make_jsonl_line("also in window", 7, 0),
    ]
    jf = tmp_path / "session.jsonl"
    jf.write_text("\n".join(lines))

    msgs = read_jsonl_assistant_messages(jf, _since())
    assert len(msgs) == 2
    assert msgs[0].text == "in window"
    assert msgs[1].text == "also in window"


def test_read_jsonl_skips_non_text_blocks(tmp_path: Path) -> None:
    from ccgram.reconcile_transcript import read_jsonl_assistant_messages

    entry = {
        "type": "assistant",
        "timestamp": _ts(7),
        "message": {
            "content": [
                {"type": "tool_use", "id": "x", "name": "Read", "input": {}},
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "actual response"},
            ]
        },
    }
    jf = tmp_path / "session.jsonl"
    jf.write_text(json.dumps(entry))

    msgs = read_jsonl_assistant_messages(jf, _since())
    assert len(msgs) == 1
    assert msgs[0].text == "actual response"


def test_read_jsonl_skips_non_assistant(tmp_path: Path) -> None:
    from ccgram.reconcile_transcript import read_jsonl_assistant_messages

    lines = [
        json.dumps({"type": "user", "timestamp": _ts(7), "message": {"content": [{"type": "text", "text": "user msg"}]}}),
        _make_jsonl_line("assistant msg", 7, 5),
    ]
    jf = tmp_path / "session.jsonl"
    jf.write_text("\n".join(lines))

    msgs = read_jsonl_assistant_messages(jf, _since())
    assert len(msgs) == 1
    assert msgs[0].text == "assistant msg"


# ---------------------------------------------------------------------------
# match_messages
# ---------------------------------------------------------------------------


def test_match_happy_path_zero_gaps() -> None:
    from ccgram.reconcile_transcript import JsonlAssistantMessage, match_messages

    ts = datetime(2026, 4, 16, 7, tzinfo=timezone.utc)
    texts = [
        "Hello, here is the morning briefing.",
        "Task completed successfully.",
        "The build passed all checks.",
        "Memory updated.",
        "Done.",
    ]
    jsonl_msgs = [JsonlAssistantMessage(timestamp=ts, text=t) for t in texts]
    tg_texts = list(texts)

    matched, missing, spurious = match_messages(jsonl_msgs, tg_texts)
    assert len(matched) == 5
    assert len(missing) == 0
    assert len(spurious) == 0


def test_match_drift_path_two_missing() -> None:
    from ccgram.reconcile_transcript import JsonlAssistantMessage, match_messages

    ts = datetime(2026, 4, 16, 7, tzinfo=timezone.utc)
    all_texts = [
        "Morning briefing complete.",
        "Memory updated with new entries.",
        "Task completed.",
        "Build passed.",
        "Session log written.",
    ]
    jsonl_msgs = [JsonlAssistantMessage(timestamp=ts, text=t) for t in all_texts]
    tg_texts = [all_texts[0], all_texts[2], all_texts[4]]

    matched, missing, spurious = match_messages(jsonl_msgs, tg_texts)
    assert len(matched) == 3
    assert len(missing) == 2
    missing_texts = {m.text for m in missing}
    assert all_texts[1] in missing_texts
    assert all_texts[3] in missing_texts


def test_match_spurious_detection() -> None:
    from ccgram.reconcile_transcript import JsonlAssistantMessage, match_messages

    ts = datetime(2026, 4, 16, 7, tzinfo=timezone.utc)
    jsonl_msgs = [JsonlAssistantMessage(timestamp=ts, text="Real assistant message here.")]
    tg_texts = [
        "Real assistant message here.",
        "\U0001f514 Hook notification: build started",
    ]

    matched, missing, spurious = match_messages(jsonl_msgs, tg_texts)
    assert len(matched) == 1
    assert len(missing) == 0
    assert len(spurious) == 1
    assert "Hook notification" in spurious[0]


# ---------------------------------------------------------------------------
# send_recovered_message
# ---------------------------------------------------------------------------


def test_send_apply_calls_send_function(tmp_path: Path) -> None:
    from ccgram.reconcile_transcript import send_recovered_message

    sent_texts: list[str] = []

    def _fake_urlopen(url, *, data=None, timeout=15):
        body = urllib.parse.parse_qs(data.decode() if data else "")
        sent_texts.append(body.get("text", [""])[0])
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        ok = send_recovered_message(
            token="test_token",
            chat_id=-1001234567890,
            thread_id=42,
            text="Missing message one.",
        )

    assert ok is True
    assert len(sent_texts) == 1
    assert sent_texts[0].startswith("[recovered] Missing message one.")


def test_send_two_missing_sends_twice() -> None:
    from ccgram.reconcile_transcript import send_recovered_message

    call_count = 0

    def _fake_urlopen(url, *, data=None, timeout=15):
        nonlocal call_count
        call_count += 1
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        send_recovered_message("tok", -100123, 42, "msg one")
        send_recovered_message("tok", -100123, 42, "msg two")

    assert call_count == 2


# ---------------------------------------------------------------------------
# MTProto get_topic_history unit tests (parse path)
# ---------------------------------------------------------------------------


async def test_get_topic_history_happy_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient

    reply_to = SimpleNamespace(reply_to_msg_id=42)
    sender_bot = SimpleNamespace(bot=True)
    sender_user = SimpleNamespace(bot=False)

    msgs = [
        SimpleNamespace(id=100, date=1745000100, from_id=None, sender=sender_bot, message="Bot reply 1", reply_to=reply_to),
        SimpleNamespace(id=101, date=1745000200, from_id=None, sender=sender_user, message="User message", reply_to=None),
    ]

    mock_resp = MagicMock()
    mock_resp.messages = msgs

    mock_tg = AsyncMock()
    mock_tg.get_input_entity = AsyncMock(return_value="entity")
    mock_tg.return_value = mock_resp

    client = MTProtoClient()
    client._client = mock_tg

    fake_cls = MagicMock(return_value="req_obj")
    with patch.dict("sys.modules", {"telethon.tl.functions.messages": MagicMock(GetRepliesRequest=fake_cls)}):
        result = await client.get_topic_history(-100123456, topic_id=42, limit=10)

    assert len(result) == 2
    assert result[0].message_id == 100
    assert result[0].from_bot is True
    assert result[0].text == "Bot reply 1"
    assert result[0].reply_to_message_id == 42
    assert result[1].from_bot is False
    assert result[1].reply_to_message_id is None


async def test_get_topic_history_min_date_filter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient

    cutoff = datetime(2026, 4, 16, 6, 45, tzinfo=timezone.utc)
    after_ts = int(datetime(2026, 4, 16, 7, 0, tzinfo=timezone.utc).timestamp())
    before_ts = int(datetime(2026, 4, 16, 6, 30, tzinfo=timezone.utc).timestamp())

    msgs = [
        SimpleNamespace(id=200, date=after_ts, sender=None, message="after cutoff", reply_to=None),
        SimpleNamespace(id=201, date=before_ts, sender=None, message="before cutoff", reply_to=None),
    ]

    mock_resp = MagicMock()
    mock_resp.messages = msgs
    mock_tg = AsyncMock()
    mock_tg.get_input_entity = AsyncMock(return_value="entity")
    mock_tg.return_value = mock_resp

    client = MTProtoClient()
    client._client = mock_tg

    fake_cls = MagicMock(return_value="req_obj")
    with patch.dict("sys.modules", {"telethon.tl.functions.messages": MagicMock(GetRepliesRequest=fake_cls)}):
        result = await client.get_topic_history(-100123456, topic_id=42, min_date=cutoff)

    assert len(result) == 1
    assert result[0].message_id == 200


async def test_get_topic_history_empty_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient

    mock_resp = MagicMock()
    mock_resp.messages = []
    mock_tg = AsyncMock()
    mock_tg.get_input_entity = AsyncMock(return_value="entity")
    mock_tg.return_value = mock_resp

    client = MTProtoClient()
    client._client = mock_tg

    fake_cls = MagicMock(return_value="req_obj")
    with patch.dict("sys.modules", {"telethon.tl.functions.messages": MagicMock(GetRepliesRequest=fake_cls)}):
        result = await client.get_topic_history(-100123456, topic_id=42)

    assert result == []
