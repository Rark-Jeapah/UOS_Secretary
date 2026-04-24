from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from sidae_secretary.assistant import planner
from sidae_secretary.assistant.registry import ACTION_SCHEMA_VERSION


def _settings(**overrides):
    base = {
        "llm_enabled": True,
        "llm_provider": "local",
        "llm_model": "gemma4",
        "llm_timeout_sec": 30,
        "llm_local_endpoint": "http://127.0.0.1:11434/api/chat",
        "timezone": "Asia/Seoul",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_plan_assistant_request_accepts_valid_llm_json(monkeypatch) -> None:
    called: dict[str, str] = {}

    class FakeClient:
        def generate_text(self, *, system_prompt: str, prompt: str) -> str:
            called["system_prompt"] = system_prompt
            called["prompt"] = prompt
            return json.dumps(
                {
                    "intent": "create_one_time_reminder",
                    "confidence": 0.91,
                    "actions": [
                        {
                            "version": ACTION_SCHEMA_VERSION,
                            "capability": "create_one_time_reminder",
                            "arguments": {
                                "run_at_local": "2026-04-01T08:30",
                                "message": "과제 제출",
                                "timezone": "Asia/Seoul",
                            },
                        }
                    ],
                    "reply": "내일 오전 8시 30분 리마인더를 준비할게요.",
                    "needs_clarification": False,
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(planner, "_planner_client", lambda settings: FakeClient())

    result = planner.plan_assistant_request(
        _settings(),
        text="내일 오전 8시 30분에 과제 제출 알림해줘",
        now=datetime(2026, 3, 31, 19, 0, 0),
    )

    assert result["mode"] == "llm"
    assert result["intent"] == "create_one_time_reminder"
    assert result["needs_clarification"] is False
    assert result["actions"][0]["capability"] == "create_one_time_reminder"
    assert "Return exactly one JSON object" in called["system_prompt"]
    assert ACTION_SCHEMA_VERSION in called["prompt"]


def test_plan_assistant_request_accepts_tomorrow_weather_intent(monkeypatch) -> None:
    called: dict[str, str] = {}

    class FakeClient:
        def generate_text(self, *, system_prompt: str, prompt: str) -> str:
            called["system_prompt"] = system_prompt
            called["prompt"] = prompt
            return json.dumps(
                {
                    "intent": "query_tomorrow_weather",
                    "confidence": 0.88,
                    "actions": [
                        {
                            "version": ACTION_SCHEMA_VERSION,
                            "capability": "query_tomorrow_weather",
                            "arguments": {},
                        }
                    ],
                    "reply": "내일 날씨를 보여드릴게요.",
                    "needs_clarification": False,
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(planner, "_planner_client", lambda settings: FakeClient())

    result = planner.plan_assistant_request(
        _settings(),
        text="내일 날씨 어때?",
        now=datetime(2026, 4, 1, 19, 0, 0),
    )

    assert result["mode"] == "llm"
    assert result["intent"] == "query_tomorrow_weather"
    assert result["actions"][0]["capability"] == "query_tomorrow_weather"
    assert "tomorrow's weather or forecast must use query_tomorrow_weather" in called["system_prompt"]


def test_plan_assistant_request_falls_back_on_invalid_json(monkeypatch) -> None:
    class FakeClient:
        def generate_text(self, *, system_prompt: str, prompt: str) -> str:
            return "내 생각에는 내일 일정과 날씨를 함께 보면 좋겠어요."

    monkeypatch.setattr(planner, "_planner_client", lambda settings: FakeClient())

    result = planner.plan_assistant_request(
        _settings(),
        text="내일 뭐 챙겨야 해?",
    )

    assert result["mode"] == "fallback_invalid_json"
    assert result["intent"] == "needs_clarification"
    assert result["actions"] == []
    assert result["needs_clarification"] is True


def test_plan_assistant_request_short_circuits_ambiguous_input(monkeypatch) -> None:
    def _unexpected_client(settings):
        raise AssertionError("LLM should not be called for ambiguous requests")

    monkeypatch.setattr(planner, "_planner_client", _unexpected_client)

    result = planner.plan_assistant_request(
        _settings(),
        text="뭐 할 수 있어?",
    )

    assert result["mode"] == "fallback_ambiguous"
    assert result["intent"] == "needs_clarification"
    assert result["actions"] == []
    assert result["needs_clarification"] is True
