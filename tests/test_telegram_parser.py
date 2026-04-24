from __future__ import annotations

from sidae_secretary.connectors import telegram as telegram_connector
from sidae_secretary.connectors.telegram import (
    TelegramBotClient,
    classify_message,
    normalize_updates,
    parse_command_message,
)


def test_classify_message_event_draft_when_datetime_present() -> None:
    item_type, draft = classify_message("Project meeting 2026-03-10 14:00", "Asia/Seoul")
    assert item_type == "event_draft"
    assert draft["start_at"].startswith("2026-03-10")


def test_classify_message_task_draft_when_due_words_present() -> None:
    item_type, draft = classify_message("Essay due by Friday", "Asia/Seoul")
    assert item_type == "task_draft"
    assert draft["status"] == "open"


def test_classify_message_note_fallback() -> None:
    item_type, draft = classify_message("Remember to email professor", "Asia/Seoul")
    assert item_type == "note"
    assert "body" in draft


def test_classify_message_share_url_falls_back_to_note() -> None:
    item_type, draft = classify_message("https://example.invalid/shared-note", "Asia/Seoul")
    assert item_type == "note"
    assert draft["body"] == "https://example.invalid/shared-note"


def test_normalize_updates_honors_chat_filter() -> None:
    updates = [
        {
            "update_id": 1,
            "message": {
                "date": 1770000000,
                "text": "Hello 2026-03-10 14:00",
                "chat": {"id": 10, "type": "private"},
                "from": {"id": 100},
            },
        },
        {
            "update_id": 2,
            "message": {
                "date": 1770000001,
                "text": "Should be ignored",
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 101},
            },
        },
    ]

    items = normalize_updates(updates, timezone_name="Asia/Seoul", allowed_chat_ids=["10"])

    assert len(items) == 1
    assert items[0].external_id == "telegram:update:1"


def test_parse_command_message_done_task() -> None:
    parsed = parse_command_message("/done task inbox:123")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "done"
    assert parsed["target"] == "task"
    assert parsed["id"] == "inbox:123"


def test_classify_message_command() -> None:
    item_type, draft = classify_message("/status", "Asia/Seoul")
    assert item_type == "command"
    assert draft["command"] == "status"
    assert draft["ok"] is True


def test_parse_command_message_invalid_payload() -> None:
    parsed = parse_command_message("/done task")
    assert parsed is not None
    assert parsed["ok"] is False


def test_parse_command_message_rejects_review_target() -> None:
    parsed = parse_command_message("/done review 123")
    assert parsed is not None
    assert parsed["ok"] is False
    assert parsed["error"] == "target must be task"


def test_parse_command_message_plan() -> None:
    parsed = parse_command_message("/plan tomorrow 8am remind me to review")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "plan"
    assert "review" in parsed["instruction"]


def test_parse_command_message_assistant_aliases() -> None:
    for text in [
        "/bot 오늘 일정 알려줘",
        "/assistant 오늘 일정 알려줘",
        "/asis 오늘 일정 알려줘",
    ]:
        parsed = parse_command_message(text)
        assert parsed is not None
        assert parsed["ok"] is True
        assert parsed["command"] == "assistant"
        assert parsed["request"] == "오늘 일정 알려줘"


def test_parse_command_message_onboarding_commands() -> None:
    for text, expected in [
        ("/start", "start"),
        ("/help", "help"),
        ("/setup", "setup"),
        ("/connect 연세대학교", "connect_moodle"),
    ]:
        parsed = parse_command_message(text)
        assert parsed is not None
        assert parsed["ok"] is True
        assert parsed["command"] == expected


def test_parse_command_message_connect_with_school_name() -> None:
    parsed = parse_command_message("/connect 연세대학교")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "connect_moodle"
    assert parsed["school_query"] == "연세대학교"


def test_parse_command_message_todaysummary() -> None:
    parsed = parse_command_message("/todaysummary")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "today_summary"


def test_parse_command_message_tomorrow_commands() -> None:
    parsed = parse_command_message("/tomorrow")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "tomorrow"

    parsed = parse_command_message("/tomorrowsummary")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "tomorrow_summary"


def test_parse_command_message_todayweather() -> None:
    parsed = parse_command_message("/todayweather")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "weather"


def test_parse_command_message_weather() -> None:
    parsed = parse_command_message("/weather")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "weather"


def test_parse_command_message_region() -> None:
    parsed = parse_command_message("/region 동대문구")
    assert parsed is not None
    assert parsed["ok"] is True
    assert parsed["command"] == "region"
    assert parsed["query"] == "동대문구"

    reset = parse_command_message("/setregion reset")
    assert reset is not None
    assert reset["ok"] is True
    assert reset["command"] == "region"
    assert reset["query"] == "reset"


def test_parse_command_message_notice_commands() -> None:
    for text, expected in [
        ("/notice_general", "notice_general"),
        ("/generalnotice", "notice_general"),
        ("/notice_academic", "notice_academic"),
        ("/academicnotice", "notice_academic"),
        ("/notice_uclass", "notice_uclass"),
        ("/uclassnotice", "notice_uclass"),
    ]:
        parsed = parse_command_message(text)
        assert parsed is not None
        assert parsed["ok"] is True
        assert parsed["command"] == expected


def test_normalize_updates_allows_start_for_unapproved_chat() -> None:
    updates = [
        {
            "update_id": 3,
            "message": {
                "date": 1770000002,
                "text": "/start",
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 202},
            },
        }
    ]

    items = normalize_updates(updates, timezone_name="Asia/Seoul", allowed_chat_ids=["10"])

    assert len(items) == 1
    assert items[0].item_type == "command"
    assert items[0].draft["command"] == "start"


def test_get_updates_uses_read_timeout_longer_than_long_poll(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"ok": True, "result": []}

    def _fake_get(url: str, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params or {})
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram_connector.requests, "get", _fake_get)

    client = TelegramBotClient("token", timeout_sec=30)
    result = client.get_updates(limit=1, timeout=30)

    assert result == []
    assert captured["params"] == {"limit": 1, "timeout": 30}
    assert captured["timeout"] == (30, 40)


def test_get_updates_returns_empty_list_on_read_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_get(url: str, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params or {})
        captured["timeout"] = timeout
        raise telegram_connector.requests.exceptions.ReadTimeout("timed out")

    monkeypatch.setattr(telegram_connector.requests, "get", _fake_get)

    client = TelegramBotClient("token", timeout_sec=30)
    result = client.get_updates(limit=2, timeout=30)

    assert result == []
    assert captured["params"] == {"limit": 2, "timeout": 30}
    assert captured["timeout"] == (30, 40)
