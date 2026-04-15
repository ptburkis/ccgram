import inspect
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _clear_cred_env(monkeypatch):
    for var in ("TELEGRAM_API_ID", "TG_API_ID", "TELEGRAM_API_HASH", "TG_API_HASH"):
        monkeypatch.delenv(var, raising=False)


def test_session_file_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    session_file = tmp_path / "mtproto.session"
    session_file.write_text("dummy")

    from ccgram.mtproto_client import _secure_session_file
    _secure_session_file()

    assert stat.S_IMODE(os.stat(session_file).st_mode) == 0o600


def test_forum_topic_dataclass_shape():
    from ccgram.mtproto_client import ForumTopic

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ft = ForumTopic(
        topic_id=42,
        title="test",
        top_message_id=100,
        is_closed=False,
        is_hidden=False,
        created_date=now,
        raw={"x": 1},
    )
    assert ft.topic_id == 42
    assert ft.title == "test"
    assert ft.top_message_id == 100
    assert ft.created_date == now

    with pytest.raises(Exception):
        ft.topic_id = 99  # type: ignore[misc]

    assert not hasattr(ft, "__dict__")


def test_to_forum_topic_with_valid_object():
    from ccgram.mtproto_client import ForumTopic, _to_forum_topic

    raw = SimpleNamespace(
        id=5,
        title="hello",
        top_message=10,
        closed=True,
        hidden=False,
        date=1700000000,
    )
    result = _to_forum_topic(raw)
    assert isinstance(result, ForumTopic)
    assert result.topic_id == 5
    assert result.title == "hello"
    assert result.is_closed is True
    assert result.created_date is not None
    assert result.created_date.tzinfo is not None


def test_to_forum_topic_returns_none_for_deleted():
    from ccgram.mtproto_client import _to_forum_topic

    raw = SimpleNamespace(id=7)  # no title attr
    assert _to_forum_topic(raw) is None


def test_credentials_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    _clear_cred_env(monkeypatch)

    from ccgram.mtproto_client import MTProtoCredentialsError, MTProtoClient
    with pytest.raises(MTProtoCredentialsError) as exc_info:
        MTProtoClient()
    assert "my.telegram.org" in str(exc_info.value)


def test_credentials_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc123")

    from ccgram.mtproto_client import MTProtoClient
    client = MTProtoClient()
    assert client._api_id == 12345
    assert client._api_hash == "abc123"


def test_credentials_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    _clear_cred_env(monkeypatch)
    (tmp_path / ".env").write_text("TELEGRAM_API_ID=99999\nTELEGRAM_API_HASH=hashval\n")

    from ccgram.mtproto_client import MTProtoClient
    client = MTProtoClient()
    assert client._api_id == 99999
    assert client._api_hash == "hashval"


def test_api_id_non_integer_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "notanumber")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoCredentialsError, MTProtoClient
    with pytest.raises(MTProtoCredentialsError):
        MTProtoClient()


async def test_connect_missing_session_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient, MTProtoSessionMissingError
    client = MTProtoClient()
    with pytest.raises(MTProtoSessionMissingError):
        await client.connect()


def test_no_write_methods_on_public_api():
    from ccgram import mtproto_client
    from ccgram.mtproto_client import MTProtoClient

    forbidden_prefixes = ("send_", "create_", "edit_", "delete_")

    public_members = [
        name for name in dir(MTProtoClient)
        if not name.startswith("_")
    ]
    for name in public_members:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), (
                f"MTProtoClient has unexpected write method: {name}"
            )

    module_names = [
        name for name in dir(mtproto_client)
        if not name.startswith("_")
    ]
    for name in module_names:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), (
                f"mtproto_client module exports write symbol: {name}"
            )


async def test_list_forum_topics_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient

    topic1 = SimpleNamespace(id=1, title="General", top_message=10, closed=False, hidden=False, date=1700000000)
    topic2 = SimpleNamespace(id=2, title="Dev", top_message=20, closed=True, hidden=False, date=1700001000)
    deleted = SimpleNamespace(id=3)  # ForumTopicDeleted: no title

    mock_response = MagicMock()
    mock_response.topics = [topic1, topic2, deleted]

    mock_tg = AsyncMock()
    mock_tg.get_input_entity = AsyncMock(return_value="entity")
    mock_tg.return_value = mock_response

    client = MTProtoClient()
    client._client = mock_tg

    import ccgram.mtproto_client as _mod
    fake_request_cls = MagicMock(return_value="req_obj")

    async def run():
        with patch.dict("sys.modules", {
            "telethon.tl.functions.messages": MagicMock(
                GetForumTopicsRequest=fake_request_cls
            )
        }):
            return await client.list_forum_topics(group_id=-100123456)

    result = await run()
    assert len(result) == 2
    assert result[0].topic_id == 1
    assert result[1].topic_id == 2


async def test_list_forum_topics_pagination(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient

    page1 = [
        SimpleNamespace(id=i, title=f"T{i}", top_message=i*10, closed=False, hidden=False, date=1700000000+i)
        for i in range(1, 101)  # 100 items = full page
    ]
    page2 = [
        SimpleNamespace(id=i, title=f"T{i}", top_message=i*10, closed=False, hidden=False, date=1700000000+i)
        for i in range(101, 106)  # 5 items < page_size -> last page
    ]

    call_count = 0
    async def fake_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        r = MagicMock()
        r.topics = page1 if call_count == 1 else page2
        return r

    mock_tg = AsyncMock()
    mock_tg.get_input_entity = AsyncMock(return_value="entity")
    mock_tg.side_effect = fake_call

    client = MTProtoClient()
    client._client = mock_tg

    with patch.dict("sys.modules", {
        "telethon.tl.functions.messages": MagicMock(
            GetForumTopicsRequest=MagicMock(return_value="req")
        )
    }):
        result = await client.list_forum_topics(group_id=-100123456)

    assert call_count == 2
    assert len(result) == 105


async def test_get_forum_topics_by_id_empty_input(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")

    from ccgram.mtproto_client import MTProtoClient

    mock_tg = AsyncMock()
    client = MTProtoClient()
    client._client = mock_tg

    result = await client.get_forum_topics_by_id(-100123456, [])
    assert result == []
    mock_tg.get_input_entity.assert_not_called()
