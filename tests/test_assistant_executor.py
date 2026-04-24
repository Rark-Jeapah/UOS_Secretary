from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.assistant.executor import execute_assistant_plan
from sidae_secretary.assistant.registry import ACTION_SCHEMA_VERSION
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Seoul",
        llm_enabled=True,
        llm_provider="local",
        llm_model="gemma4",
        llm_timeout_sec=30,
        llm_local_endpoint="http://127.0.0.1:11434/api/chat",
        weather_location_label="서울특별시",
        weather_lat=37.5665,
        weather_lon=126.9780,
    )


def test_execute_assistant_plan_rejects_invalid_action_payload(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    result = execute_assistant_plan(
        _settings(),
        db,
        plan={
            "intent": "set_notification_policy",
            "confidence": 0.8,
            "actions": [
                {
                    "version": ACTION_SCHEMA_VERSION,
                    "capability": "set_notification_policy",
                    "arguments": {
                        "policy_kind": "daily_digest",
                        "enabled": "yes",
                    },
                }
            ],
            "reply": "알림 정책을 바꿀게요.",
            "needs_clarification": False,
        },
        chat_id="12345",
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_plan"
    assert db.list_notification_policies(chat_id="12345") == []


def test_execute_assistant_plan_delegates_query_reply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    monkeypatch.setattr(
        pipeline,
        "_format_telegram_today",
        lambda settings, db, user_id=None: "[Sidae] 오늘 일정\n\n- 테스트 응답",
    )

    result = execute_assistant_plan(
        _settings(),
        db,
        plan={
            "intent": "query_today",
            "confidence": 0.95,
            "actions": [
                {
                    "version": ACTION_SCHEMA_VERSION,
                    "capability": "query_today",
                    "arguments": {},
                }
            ],
            "reply": "오늘 일정을 확인할게요.",
            "needs_clarification": False,
            "mode": "llm",
        },
        chat_id="12345",
    )

    assert result["ok"] is True
    assert result["reply"] == "[Sidae] 오늘 일정\n\n- 테스트 응답"
    assert result["action_results"][0]["capability"] == "query_today"


def test_execute_assistant_plan_delegates_tomorrow_weather_reply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    monkeypatch.setattr(
        pipeline,
        "_format_telegram_tomorrowweather",
        lambda settings, db, user_id=None, chat_id=None: "[Sidae] 내일 날씨\n\n- 테스트 예보",
    )

    result = execute_assistant_plan(
        _settings(),
        db,
        plan={
            "intent": "query_tomorrow_weather",
            "confidence": 0.95,
            "actions": [
                {
                    "version": ACTION_SCHEMA_VERSION,
                    "capability": "query_tomorrow_weather",
                    "arguments": {},
                }
            ],
            "reply": "내일 날씨를 확인할게요.",
            "needs_clarification": False,
            "mode": "llm",
        },
        chat_id="12345",
    )

    assert result["ok"] is True
    assert result["reply"] == "[Sidae] 내일 날씨\n\n- 테스트 예보"
    assert result["action_results"][0]["capability"] == "query_tomorrow_weather"


def test_execute_assistant_plan_updates_weather_region_and_notification_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    monkeypatch.setattr(
        pipeline,
        "resolve_weather_location_query",
        lambda query: {
            "label": "동대문구",
            "lat": 37.5744,
            "lon": 127.0396,
            "air_quality_district_code": "111152",
            "source": "test",
        },
    )

    result = execute_assistant_plan(
        _settings(),
        db,
        plan={
            "intent": "multi_action",
            "confidence": 0.88,
            "actions": [
                {
                    "version": ACTION_SCHEMA_VERSION,
                    "capability": "set_weather_region",
                    "arguments": {"region_query": "동대문구"},
                },
                {
                    "version": ACTION_SCHEMA_VERSION,
                    "capability": "set_notification_policy",
                    "arguments": {
                        "policy_kind": "daily_digest",
                        "enabled": True,
                        "days_of_week": ["mon", "wed"],
                        "time_local": "08:30",
                        "timezone": "Asia/Seoul",
                    },
                },
            ],
            "reply": "설정을 바꿀게요.",
            "needs_clarification": False,
        },
        user_id=int(user["id"]),
        chat_id="12345",
    )

    preferences = db.get_user_preferences(chat_id="12345")
    policy = db.get_notification_policy("daily_digest", chat_id="12345")

    assert result["ok"] is True
    assert preferences["weather_location_label"] == "동대문구"
    assert preferences["weather_air_quality_district_code"] == "111152"
    assert policy["enabled"] is True
    assert policy["days_of_week_json"] == ["mon", "wed"]
    assert policy["time_local"] == "08:30"


def test_execute_assistant_plan_creates_one_time_reminder(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    result = execute_assistant_plan(
        _settings(),
        db,
        plan={
            "intent": "create_one_time_reminder",
            "confidence": 0.93,
            "actions": [
                {
                    "version": ACTION_SCHEMA_VERSION,
                    "capability": "create_one_time_reminder",
                    "arguments": {
                        "run_at_local": "2026-04-02T08:30",
                        "message": "과제 제출",
                        "timezone": "Asia/Seoul",
                    },
                }
            ],
            "reply": "리마인더를 만들게요.",
            "needs_clarification": False,
        },
        chat_id="12345",
        now=datetime(2026, 4, 1, 12, 0, 0),
    )

    reminders = db.list_telegram_reminders(status="pending", limit=10)

    assert result["ok"] is True
    assert len(reminders) == 1
    assert reminders[0]["chat_id"] == "12345"
    assert reminders[0]["message"] == "과제 제출"
    assert "[Sidae] 리마인더 예약" in result["reply"]
