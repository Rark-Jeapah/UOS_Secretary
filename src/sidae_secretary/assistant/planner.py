from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from sidae_secretary.assistant.registry import (
    ACTION_SCHEMA_VERSION,
    CAPABILITY_NAMES,
    CAPABILITY_REGISTRY_VERSION,
    serialize_action_schema,
    serialize_capability_registry,
    validate_action_payload,
)
from sidae_secretary.connectors.llm import LLMClient, LLMConfig


PLANNER_INTENT_VALUES: tuple[str, ...] = CAPABILITY_NAMES + (
    "multi_action",
    "needs_clarification",
)
PLANNER_REPLY_CHAR_LIMIT = 800
_INTENT_RE = re.compile(r"^[a-z0-9_]+$")
_AMBIGUOUS_REQUEST_PATTERNS = (
    re.compile(r"^\s*$"),
    re.compile(r"^(?:도와줘|help|뭐해|뭐 할 수 있어\??|무엇을 할 수 있어\??)\s*$", re.IGNORECASE),
)

ASSISTANT_PLANNER_SYSTEM_PROMPT = (
    "You are the /bot planner for the UOS secretary assistant. "
    "Return exactly one JSON object and nothing else. "
    "Do not use markdown fences. "
    "The JSON object must contain exactly these keys: "
    "intent, confidence, actions, reply, needs_clarification. "
    "intent must be one of the allowed intent values provided by the user payload. "
    "confidence must be a number between 0 and 1. "
    f"Each item in actions must be a valid {ACTION_SCHEMA_VERSION} action object. "
    f"Use only capabilities from registry version {CAPABILITY_REGISTRY_VERSION}. "
    "Requests about tomorrow's weather or forecast must use query_tomorrow_weather. "
    "Requests about tomorrow's schedule, classes, or deadlines must use query_tomorrow. "
    "If the request is ambiguous, unsupported, or missing required details, "
    "set intent to needs_clarification, confidence to 0.4 or lower, actions to [], "
    "needs_clarification to true, and reply to one concise Korean clarification question. "
    "If needs_clarification is false, actions must contain the concrete next action objects. "
    "Reply in Korean."
)


def _planner_client(settings: Any) -> LLMClient:
    return LLMClient(
        LLMConfig(
            provider=getattr(settings, "llm_provider", "local"),
            model=getattr(settings, "llm_model", "gemma4"),
            timeout_sec=max(int(getattr(settings, "llm_timeout_sec", 30) or 30), 1),
            local_endpoint=getattr(
                settings,
                "llm_local_endpoint",
                "http://127.0.0.1:11434/api/chat",
            ),
        )
    )


def _strip_markdown_fence(text: str) -> str:
    body = str(text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    return body


def _parse_llm_json_object(text: str) -> dict[str, Any] | None:
    stripped = _strip_markdown_fence(text)
    if not stripped:
        return None
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _looks_ambiguous(text: str) -> bool:
    snippet = str(text or "").strip()
    if not snippet:
        return True
    return any(pattern.fullmatch(snippet) for pattern in _AMBIGUOUS_REQUEST_PATTERNS)


def _fallback_plan(*, reason: str) -> dict[str, Any]:
    if reason == "ambiguous":
        reply = (
            "무엇을 도와드릴지 조금 더 구체적으로 말씀해 주세요. "
            "예: 오늘 일정, 내일 일정, 날씨, 날씨 지역 설정, 리마인더 생성."
        )
    elif reason == "disabled":
        reply = "지금은 자연어 planner를 사용할 수 없습니다. 요청을 더 구체적으로 적어 주세요."
    else:
        reply = (
            "요청을 안전하게 해석하지 못했습니다. "
            "예: 오늘 일정 보여줘, 내일 날씨 알려줘, 내일 오전 8시에 과제 제출 알림해줘."
        )
    return {
        "intent": "needs_clarification",
        "confidence": 0.0,
        "actions": [],
        "reply": reply,
        "needs_clarification": True,
        "mode": f"fallback_{reason}",
    }


def _normalize_planner_output(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(payload, dict):
        return None, ["planner output must be an object"]

    errors: list[str] = []
    allowed_keys = {
        "intent",
        "confidence",
        "actions",
        "reply",
        "needs_clarification",
    }
    extras = sorted(set(payload.keys()) - allowed_keys)
    for key in extras:
        errors.append(f"{key} is not allowed in planner output")

    intent = str(payload.get("intent") or "").strip()
    if not intent:
        errors.append("intent is required")
    elif not _INTENT_RE.fullmatch(intent):
        errors.append("intent must be snake_case")
    elif intent not in PLANNER_INTENT_VALUES:
        errors.append(f"intent must be one of {list(PLANNER_INTENT_VALUES)}")

    confidence_raw = payload.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        errors.append("confidence must be a number")
        confidence = 0.0
    else:
        confidence = float(confidence_raw)
        if confidence < 0 or confidence > 1:
            errors.append("confidence must be between 0 and 1")

    reply = str(payload.get("reply") or "").strip()
    if not reply:
        errors.append("reply is required")

    needs_clarification = payload.get("needs_clarification")
    if not isinstance(needs_clarification, bool):
        errors.append("needs_clarification must be a boolean")

    actions_raw = payload.get("actions")
    normalized_actions: list[dict[str, Any]] = []
    if not isinstance(actions_raw, list):
        errors.append("actions must be an array")
    else:
        for idx, item in enumerate(actions_raw):
            result = validate_action_payload(item)
            if not result.ok:
                detail = "; ".join(result.errors)
                errors.append(f"actions[{idx}] is invalid: {detail}")
                continue
            normalized_actions.append(result.normalized_action or {})

    if isinstance(needs_clarification, bool) and needs_clarification and normalized_actions:
        errors.append("actions must be empty when needs_clarification is true")

    if errors:
        return None, errors

    return {
        "intent": intent,
        "confidence": round(confidence, 4),
        "actions": normalized_actions,
        "reply": reply[:PLANNER_REPLY_CHAR_LIMIT],
        "needs_clarification": needs_clarification,
    }, []


def validate_planner_output(payload: Any) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    normalized, errors = _normalize_planner_output(payload)
    return normalized, tuple(errors)


def plan_assistant_request(
    settings: Any,
    *,
    text: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if _looks_ambiguous(text):
        return _fallback_plan(reason="ambiguous")

    if not bool(getattr(settings, "llm_enabled", False)):
        return _fallback_plan(reason="disabled")

    timezone_name = str(getattr(settings, "timezone", "Asia/Seoul") or "Asia/Seoul").strip()
    now_local = (now or datetime.now(ZoneInfo(timezone_name))).replace(microsecond=0)
    prompt = json.dumps(
        {
            "timezone": timezone_name,
            "now_local_iso": now_local.isoformat(),
            "request_text": str(text or "").strip(),
            "allowed_intents": list(PLANNER_INTENT_VALUES),
            "capability_registry": serialize_capability_registry(),
            "action_schema": serialize_action_schema(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        raw = _planner_client(settings).generate_text(
            system_prompt=ASSISTANT_PLANNER_SYSTEM_PROMPT,
            prompt=prompt,
        )
    except Exception:
        return _fallback_plan(reason="llm_error")

    parsed = _parse_llm_json_object(raw)
    if parsed is None:
        return _fallback_plan(reason="invalid_json")

    normalized, errors = validate_planner_output(parsed)
    if errors or normalized is None:
        return _fallback_plan(reason="invalid_plan")

    return {
        **normalized,
        "mode": "llm",
    }
