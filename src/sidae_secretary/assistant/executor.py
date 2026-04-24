from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sidae_secretary.assistant.planner import validate_planner_output
from sidae_secretary.assistant.registry import CapabilityDefinition, get_capability
from sidae_secretary.db import Database


def validate_assistant_plan(payload: Any) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    normalized_payload = payload
    if isinstance(payload, dict) and "mode" in payload:
        normalized_payload = {
            key: value
            for key, value in payload.items()
            if key != "mode"
        }
    return validate_planner_output(normalized_payload)


def _resolve_scope(
    db: Database,
    *,
    user_id: int | None = None,
    chat_id: str | None = None,
) -> tuple[int | None, str | None]:
    owner_id = int(user_id) if isinstance(user_id, int) and user_id > 0 else None
    chat = str(chat_id or "").strip() or None
    if owner_id is not None and not chat:
        user = db.get_user(owner_id)
        chat = (
            str(user.get("telegram_chat_id") or "").strip()
            if isinstance(user, dict)
            else None
        ) or None
    elif owner_id is None and chat:
        user = db.get_user_by_chat_id(chat)
        owner_id = int(user["user_id"]) if isinstance(user, dict) else None
    return owner_id, chat


def _parse_run_at_local(
    value: str,
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("run_at_local is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("run_at_local is invalid") from exc
    tz = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    localized = parsed.astimezone(tz).replace(microsecond=0)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    now_local = current.astimezone(tz).replace(microsecond=0)
    if localized <= now_local:
        raise ValueError("run_at_local must be in the future")
    return localized


def _format_capability_explanation(capability: CapabilityDefinition) -> str:
    lines = [
        "[Sidae] 기능 설명",
        "",
        f"- 기능: {capability.name}",
        f"- 종류: {capability.category}",
        f"- 설명: {capability.summary}",
        f"- 실행 영향: {'있음' if capability.side_effect else '없음'}",
    ]
    if not capability.fields:
        lines.append("- 인자: 없음")
        return "\n".join(lines)
    lines.append("- 인자:")
    for field in capability.fields:
        required = "필수" if field.required else "선택"
        lines.append(f"  - {field.name} ({required}, {field.field_type})")
        lines.append(f"    {field.description}")
    return "\n".join(lines)


def _execute_query_action(
    settings: Any,
    db: Database,
    *,
    capability: str,
    user_id: int | None,
    chat_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from sidae_secretary.jobs import pipeline

    if capability == "query_today":
        return {
            "ok": True,
            "capability": capability,
            "reply": pipeline._format_telegram_today(settings, db, user_id=user_id),
        }
    if capability == "query_tomorrow":
        return {
            "ok": True,
            "capability": capability,
            "reply": pipeline._format_telegram_tomorrow(settings, db, user_id=user_id),
        }
    if capability == "query_weather":
        return {
            "ok": True,
            "capability": capability,
            "reply": pipeline._format_telegram_todayweather(
                settings,
                db,
                user_id=user_id,
                chat_id=chat_id,
            ),
        }
    if capability == "query_tomorrow_weather":
        return {
            "ok": True,
            "capability": capability,
            "reply": pipeline._format_telegram_tomorrowweather(
                settings,
                db,
                user_id=user_id,
                chat_id=chat_id,
            ),
        }
    if capability == "explain_capability":
        target_name = str(arguments.get("capability_name") or "").strip()
        target = get_capability(target_name)
        if target is None:
            return {
                "ok": False,
                "capability": capability,
                "error": f"unsupported capability explanation target: {target_name}",
                "reply": "설명할 기능을 찾지 못했습니다.",
            }
        return {
            "ok": True,
            "capability": capability,
            "reply": _format_capability_explanation(target),
        }
    return {
        "ok": False,
        "capability": capability,
        "error": f"unsupported query capability: {capability}",
        "reply": "지원하지 않는 조회 요청입니다.",
    }


def _execute_mutation_action(
    settings: Any,
    db: Database,
    *,
    capability: str,
    user_id: int | None,
    chat_id: str | None,
    arguments: dict[str, Any],
    intent: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    from sidae_secretary.jobs import pipeline

    if capability == "set_weather_region":
        result = pipeline._handle_telegram_region_command(
            settings,
            db,
            query=str(arguments.get("region_query") or "").strip(),
            user_id=user_id,
            chat_id=chat_id,
        )
        return {
            "ok": bool(result.get("ok")),
            "capability": capability,
            "reply": str(result.get("message") or "").strip(),
            "error": result.get("error"),
        }

    if capability == "create_one_time_reminder":
        owner_id, resolved_chat = _resolve_scope(db, user_id=user_id, chat_id=chat_id)
        if not resolved_chat:
            return {
                "ok": False,
                "capability": capability,
                "error": "chat_id is required for reminder creation",
                "reply": "리마인더를 만들려면 Telegram 대화 범위가 필요합니다.",
            }
        timezone_name = (
            str(arguments.get("timezone") or "").strip()
            or str(getattr(settings, "timezone", "Asia/Seoul") or "Asia/Seoul").strip()
        )
        try:
            run_at_local = _parse_run_at_local(
                str(arguments.get("run_at_local") or ""),
                timezone_name=timezone_name,
                now=now,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "capability": capability,
                "error": str(exc),
                "reply": "리마인더 시각이 올바르지 않거나 이미 지난 시각입니다.",
            }
        reminder = db.upsert_telegram_reminder(
            external_id=f"assistant-reminder:{uuid4().hex}",
            chat_id=resolved_chat,
            run_at=run_at_local.isoformat(),
            message=str(arguments.get("message") or "").strip()[:500],
            metadata_json={"source": "assistant_executor", "intent": intent},
            status="pending",
            user_id=owner_id or 0,
        )
        reply = pipeline._format_telegram_plan_result(
            {
                "ok": True,
                "scheduled": True,
                "mode": "assistant",
                "reminder": reminder,
            },
            timezone_name=timezone_name,
        )
        return {
            "ok": True,
            "capability": capability,
            "reply": reply,
            "reminder": reminder,
        }

    if capability == "set_notification_policy":
        stored = db.upsert_notification_policy(
            user_id=user_id,
            chat_id=chat_id,
            policy_kind=str(arguments.get("policy_kind") or "").strip(),
            enabled=bool(arguments.get("enabled")),
            days_of_week_json=list(arguments.get("days_of_week") or []),
            time_local=arguments.get("time_local"),
            timezone=arguments.get("timezone"),
            metadata_json={"source": "assistant_executor", "intent": intent},
        )
        days = list(stored.get("days_of_week_json") or [])
        reply_lines = [
            "[Sidae] 알림 정책",
            "",
            f"- 정책: {stored['policy_kind']}",
            f"- 상태: {'켜짐' if stored['enabled'] else '꺼짐'}",
            f"- 요일: {', '.join(str(item) for item in days) if days else '기본값'}",
            f"- 시각: {str(stored.get('time_local') or '미설정')}",
            f"- 시간대: {str(stored.get('timezone') or getattr(settings, 'timezone', 'Asia/Seoul'))}",
        ]
        return {
            "ok": True,
            "capability": capability,
            "reply": "\n".join(reply_lines),
            "notification_policy": stored,
        }

    return {
        "ok": False,
        "capability": capability,
        "error": f"unsupported mutation capability: {capability}",
        "reply": "지원하지 않는 실행 요청입니다.",
    }


def _execute_action(
    settings: Any,
    db: Database,
    *,
    action: dict[str, Any],
    intent: str,
    user_id: int | None,
    chat_id: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    capability = str(action.get("capability") or "").strip()
    arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    definition = get_capability(capability)
    if definition is None:
        return {
            "ok": False,
            "capability": capability,
            "error": f"unknown capability: {capability}",
            "reply": "지원하지 않는 작업입니다.",
        }
    try:
        if definition.side_effect:
            return _execute_mutation_action(
                settings,
                db,
                capability=capability,
                user_id=user_id,
                chat_id=chat_id,
                arguments=arguments,
                intent=intent,
                now=now,
            )
        return _execute_query_action(
            settings,
            db,
            capability=capability,
            user_id=user_id,
            chat_id=chat_id,
            arguments=arguments,
        )
    except Exception as exc:
        return {
            "ok": False,
            "capability": capability,
            "error": str(exc),
            "reply": "요청 실행 중 오류가 발생했습니다.",
        }


def execute_assistant_plan(
    settings: Any,
    db: Database,
    *,
    plan: Any,
    user_id: int | None = None,
    chat_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized, errors = validate_assistant_plan(plan)
    if normalized is None:
        return {
            "ok": False,
            "error": "invalid_plan",
            "errors": list(errors),
            "reply": "요청을 안전하게 처리할 수 없습니다.",
            "action_results": [],
        }

    if normalized["needs_clarification"]:
        return {
            "ok": True,
            "needs_clarification": True,
            "reply": normalized["reply"],
            "action_results": [],
            "intent": normalized["intent"],
        }

    action_results: list[dict[str, Any]] = []
    messages: list[str] = []
    for action in normalized["actions"]:
        result = _execute_action(
            settings,
            db,
            action=action,
            intent=normalized["intent"],
            user_id=user_id,
            chat_id=chat_id,
            now=now,
        )
        action_results.append(result)
        reply = str(result.get("reply") or "").strip()
        if reply:
            messages.append(reply)
        if not bool(result.get("ok")):
            return {
                "ok": False,
                "error": result.get("error") or "action_failed",
                "reply": reply or normalized["reply"],
                "action_results": action_results,
                "intent": normalized["intent"],
            }

    final_reply = "\n\n".join(messages) if messages else normalized["reply"]
    return {
        "ok": True,
        "needs_clarification": False,
        "reply": final_reply,
        "action_results": action_results,
        "intent": normalized["intent"],
    }
