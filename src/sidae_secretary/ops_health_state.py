from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

from dateutil import parser as dt_parser

from sidae_secretary.config import Settings
from sidae_secretary.connectors.uos_openapi import (
    UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
    UOS_OPENAPI_TIMETABLE_SOURCE,
    uos_openapi_timetable_configured,
    uos_openapi_uses_official_catalog_mode,
)
from sidae_secretary.db import Database
from sidae_secretary.telegram_setup_state import sync_dashboard_source_card


OPS_TELEGRAM_STALE_MINUTES = 120
OPS_UCLASS_STALE_MINUTES = 24 * 60
OPS_WEATHER_STALE_MINUTES = 90


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_dt(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt_parser.isoparse(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ops_age_minutes(value: str | None) -> float | None:
    parsed = _parse_dt(str(value or "").strip())
    if parsed is None:
        return None
    age_seconds = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0:
        age_seconds = 0
    return round(age_seconds / 60, 2)


def _ops_surface_payload(
    *,
    component: str,
    ready: bool,
    status: str,
    reason: str | None = None,
    job_name: str | None = None,
    last_run_at: str | None = None,
    last_success_at: str | None = None,
    last_error: str | None = None,
    age_minutes: float | None = None,
    stale_after_minutes: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "component": component,
        "ready": bool(ready),
        "status": str(status or "unknown").strip().lower() or "unknown",
        "reason": str(reason or "").strip() or None,
        "job_name": str(job_name or "").strip() or None,
        "last_run_at": str(last_run_at or "").strip() or None,
        "last_success_at": str(last_success_at or "").strip() or None,
        "last_error": str(last_error or "").strip() or None,
        "age_minutes": age_minutes,
        "stale_after_minutes": int(stale_after_minutes) if stale_after_minutes else None,
        "details": dict(details or {}),
    }
    payload["stale"] = bool(
        payload["stale_after_minutes"]
        and isinstance(age_minutes, (int, float))
        and age_minutes > float(payload["stale_after_minutes"])
    )
    return payload


def _sync_dashboard_status(cursor: dict[str, Any]) -> str:
    meta = cursor.get("_sync_dashboard") if isinstance(cursor.get("_sync_dashboard"), dict) else {}
    status = str(meta.get("status") or cursor.get("status") or "").strip().lower()
    return status or "never"


def ops_surface_state(
    db: Database,
    job_name: str,
    *,
    user_id: int | None = None,
    allow_global_fallback: bool = True,
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    state = db.get_sync_state(job_name, user_id=user_id)
    cursor = _json_load(state.last_cursor_json)
    if user_id and allow_global_fallback and not state.last_run_at and not cursor:
        state = db.get_sync_state(job_name)
        cursor = _json_load(state.last_cursor_json)
    source_key_map = {
        "sync_uos_portal_timetable": "portal",
        "sync_uclass": "uclass",
        "sync_weather": "weather",
        "sync_telegram": "telegram",
    }
    source_key = source_key_map.get(job_name)
    if source_key:
        card = sync_dashboard_source_card(
            db,
            source_key,
            user_id=user_id,
            allow_global_fallback=allow_global_fallback,
        )
    else:
        card = {
            "job_name": job_name,
            "status": _sync_dashboard_status(cursor) if cursor else "never",
            "last_run_at": state.last_run_at,
            "last_success_at": None,
            "last_error": str(cursor.get("error") or cursor.get("last_error") or "").strip() or None,
            "action_required": 0,
        }
    return dict(card), state, cursor


def _normalize_timetable_source_attempts(value: Any) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return attempts
    for item in value:
        if not isinstance(item, dict):
            continue
        attempts.append(
            {
                "source": str(item.get("source") or "").strip() or None,
                "status": str(item.get("status") or "").strip() or None,
                "reason": str(item.get("reason") or "").strip() or None,
                "source_url": str(item.get("source_url") or "").strip() or None,
            }
        )
    return attempts


def _portal_notice_snapshot_job(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    return f"uos_notice_snapshot_{normalized or 'unknown'}"


def _portal_notice_snapshot_from_cursor(cursor: dict[str, Any]) -> dict[str, Any]:
    raw_snapshot = cursor.get("snapshot") if isinstance(cursor.get("snapshot"), dict) else {}
    notices = list(raw_snapshot.get("notices") or []) if isinstance(raw_snapshot.get("notices"), list) else []
    return {
        "fetched_at": str(raw_snapshot.get("fetched_at") or "").strip() or None,
        "notice_count": len(notices),
        "notices": notices,
        "empty": bool(raw_snapshot.get("empty")),
    }


def _ops_scope_summary(
    settings: Settings,
    db: Database,
    *,
    chat_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    requested_user_id = _safe_int(user_id)
    requested_chat_id = str(chat_id or "").strip() or None
    user = None
    if requested_user_id is not None and requested_user_id > 0:
        user = db.get_user(requested_user_id)
    if user is None and requested_chat_id:
        user = db.get_user_by_chat_id(requested_chat_id)
    resolved_user_id = int(user.get("id") or user.get("user_id") or 0) if user else 0
    return {
        "requested_user_id": requested_user_id,
        "requested_chat_id": requested_chat_id,
        "user_id": resolved_user_id,
        "chat_id": str(
            (user or {}).get("telegram_chat_id")
            or requested_chat_id
            or ""
        ).strip()
        or None,
        "status": str((user or {}).get("status") or "unknown").strip() or "unknown",
        "found": resolved_user_id > 0,
        "timezone": str(
            (user or {}).get("timezone")
            or getattr(settings, "timezone", "Asia/Seoul")
            or "Asia/Seoul"
        ).strip()
        or "Asia/Seoul",
    }


def _build_uos_official_api_health(
    settings: Settings,
    db: Database,
    *,
    user_id: int | None = None,
    resolve_uos_portal_timetable_targets: Callable[[Settings, Database], list[dict[str, Any]]],
) -> dict[str, Any]:
    configured_url = str(getattr(settings, "uos_openapi_timetable_url", "") or "").strip()
    configured_key = str(getattr(settings, "uos_openapi_timetable_api_key", "") or "").strip()
    configured = uos_openapi_timetable_configured(configured_url, configured_key)
    official_catalog_mode = uos_openapi_uses_official_catalog_mode(configured_url)
    card, state, cursor = ops_surface_state(
        db,
        "sync_uos_portal_timetable",
        user_id=user_id,
        allow_global_fallback=False,
    )
    timetable_targets = [
        item
        for item in resolve_uos_portal_timetable_targets(settings, db)
        if user_id is None or int(item.get("user_id") or 0) == int(user_id or 0)
    ]
    payload_sources = list(cursor.get("payload_sources") or []) if isinstance(cursor.get("payload_sources"), list) else []
    payload_source = str(cursor.get("payload_source") or "").strip() or None
    source_attempts = _normalize_timetable_source_attempts(cursor.get("source_attempts"))
    fallback_used = bool(cursor.get("fallback_used"))
    selected_targets = sum(
        1
        for item in payload_sources
        if str(item.get("payload_source") or "").strip() == UOS_OPENAPI_TIMETABLE_SOURCE
    )
    if payload_source == UOS_OPENAPI_TIMETABLE_SOURCE:
        selected_targets = max(selected_targets, 1)
    fallback_targets = sum(1 for item in payload_sources if bool(item.get("fallback_used")))
    if fallback_used:
        fallback_targets = max(fallback_targets, 1)
    status = "never_checked"
    reason = None
    ready = False
    last_error = str(card.get("last_error") or cursor.get("reason") or "").strip() or None
    if not configured:
        status = "not_configured"
        reason = "UOS_OPENAPI_TIMETABLE_URL or UOS_OPENAPI_TIMETABLE_API_KEY missing"
    elif not timetable_targets:
        status = "no_targets"
        reason = (
            "No active UClass connections for official timetable matching"
            if official_catalog_mode
            else "No active UOS timetable targets"
        )
    elif str(card.get("status") or "").strip().lower() == "error":
        status = "error"
        reason = last_error or "UOS official API stage failed"
    elif fallback_targets > 0:
        status = "degraded"
        reason = "UOS official API fell back to browser portal sync"
    elif selected_targets > 0:
        status = "ready"
        ready = True
    elif official_catalog_mode and str(card.get("status") or "").strip().lower() == "success":
        status = "ready"
        ready = True
    elif str(card.get("status") or "").strip().lower() == "skipped":
        status = "skipped"
        reason = str(cursor.get("reason") or last_error or "UOS official API not exercised").strip() or None
    details = {
        "configured": configured,
        "api_url": configured_url or (UOS_OPENAPI_OFFICIAL_TIMETABLE_URL if official_catalog_mode else None),
        "catalog_mode": official_catalog_mode,
        "active_timetable_targets": len(timetable_targets),
        "official_api_selected_targets": selected_targets,
        "fallback_targets": fallback_targets,
        "payload_source": payload_source,
        "fallback_used": fallback_used,
        "source_attempts": source_attempts,
    }
    return _ops_surface_payload(
        component="uos_official_api",
        ready=ready,
        status=status,
        reason=reason,
        job_name="sync_uos_portal_timetable",
        last_run_at=state.last_run_at or card.get("last_run_at"),
        last_success_at=card.get("last_success_at"),
        last_error=last_error,
        details=details,
    )


def _build_uclass_sync_health(
    settings: Settings,
    db: Database,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    card, state, cursor = ops_surface_state(db, "sync_uclass", user_id=user_id)
    if user_id:
        active_targets = db.list_moodle_connections(user_id=user_id, status="active", limit=50)
        legacy_ws_base_configured = False
    else:
        active_targets = db.list_moodle_connections(status="active", limit=500)
        legacy_ws_base_configured = bool(str(getattr(settings, "uclass_ws_base", "") or "").strip())
    wsfunctions = cursor.get("wsfunctions") if isinstance(cursor.get("wsfunctions"), dict) else {}
    ws_failures = [
        {
            "wsfunction": name,
            "failed": int(status_item.get("failed") or 0),
            "last_error": str(status_item.get("last_error") or "").strip() or None,
        }
        for name, status_item in wsfunctions.items()
        if isinstance(status_item, dict)
        and (int(status_item.get("failed") or 0) > 0 or str(status_item.get("last_error") or "").strip())
    ]
    html_material_error = str(cursor.get("html_material_error") or "").strip() or None
    material_download_failures = int(cursor.get("material_download_failures") or 0)
    material_extract_failures = int(cursor.get("material_extract_failures") or 0)
    semantic_warnings = (
        len(cursor.get("semantic_warnings"))
        if isinstance(cursor.get("semantic_warnings"), list)
        else int(cursor.get("semantic_warnings") or 0)
    )
    age_minutes = _ops_age_minutes(card.get("last_success_at") or card.get("last_run_at"))
    stale = bool(age_minutes is not None and age_minutes > OPS_UCLASS_STALE_MINUTES)
    status = "never_checked"
    reason = None
    ready = False
    card_status = str(card.get("status") or "").strip().lower()
    last_error = str(card.get("last_error") or "").strip() or None
    if not active_targets and not legacy_ws_base_configured:
        status = "no_targets"
        reason = "No active moodle connections and UCLASS_WS_BASE missing"
    elif card_status == "error":
        status = "error"
        reason = last_error or "UClass sync failed"
    elif card_status == "skipped":
        status = "skipped"
        reason = str(cursor.get("reason") or last_error or "UClass sync skipped").strip() or None
    elif card_status == "never":
        status = "never_checked"
    elif stale:
        status = "stale"
        reason = "UClass sync is older than 24 hours"
    elif (
        ws_failures
        or html_material_error
        or material_download_failures > 0
        or material_extract_failures > 0
        or semantic_warnings > 0
        or int(card.get("action_required") or 0) > 0
    ):
        status = "degraded"
        reason = last_error or "UClass sync completed with follow-up required"
    else:
        status = "ready"
        ready = True
    details = {
        "active_targets": len(active_targets),
        "legacy_ws_base_configured": legacy_ws_base_configured,
        "site_name": str(cursor.get("site") or "").strip() or None,
        "ws_failures": ws_failures,
        "html_material_error": html_material_error,
        "material_download_failures": material_download_failures,
        "material_extract_failures": material_extract_failures,
        "semantic_warnings": semantic_warnings,
    }
    return _ops_surface_payload(
        component="uclass_sync",
        ready=ready,
        status=status,
        reason=reason,
        job_name="sync_uclass",
        last_run_at=state.last_run_at or card.get("last_run_at"),
        last_success_at=card.get("last_success_at"),
        last_error=last_error,
        age_minutes=age_minutes,
        stale_after_minutes=OPS_UCLASS_STALE_MINUTES,
        details=details,
    )


def _build_telegram_listener_health(
    settings: Settings,
    db: Database,
    *,
    effective_telegram_allowed_chat_ids: Callable[[Settings, Database], list[str]],
) -> dict[str, Any]:
    card, state, cursor = ops_surface_state(db, "sync_telegram")
    allowed_chat_count = len(effective_telegram_allowed_chat_ids(settings, db))
    menu = cursor.get("menu") if isinstance(cursor.get("menu"), dict) else {}
    age_minutes = _ops_age_minutes(card.get("last_success_at") or card.get("last_run_at"))
    stale = bool(age_minutes is not None and age_minutes > OPS_TELEGRAM_STALE_MINUTES)
    status = "never_checked"
    reason = None
    ready = False
    last_error = str(card.get("last_error") or "").strip() or None
    if not bool(getattr(settings, "telegram_enabled", False)):
        status = "disabled"
        reason = "TELEGRAM_ENABLED is false"
    elif not str(getattr(settings, "telegram_bot_token", "") or "").strip():
        status = "not_configured"
        reason = "TELEGRAM_BOT_TOKEN missing"
    elif allowed_chat_count <= 0:
        status = "not_configured"
        reason = "No Telegram chats are allowed for beta operations"
    elif str(card.get("status") or "").strip().lower() == "error":
        status = "error"
        reason = last_error or "Telegram polling failed"
    elif str(card.get("status") or "").strip().lower() == "never":
        status = "never_checked"
    elif stale:
        status = "stale"
        reason = "Telegram listener has not updated recently"
    elif menu and menu.get("ok") is False:
        status = "degraded"
        reason = str(menu.get("error") or menu.get("reason") or "Telegram bot menu registration failed").strip()
    else:
        status = "ready"
        ready = True
    details = {
        "allowed_chat_count": allowed_chat_count,
        "commands_enabled": bool(getattr(settings, "telegram_commands_enabled", False)),
        "fetched_updates": int(cursor.get("fetched") or 0),
        "stored_messages": int(cursor.get("stored") or 0),
        "next_offset": cursor.get("next_offset"),
        "menu_ok": menu.get("ok") if isinstance(menu, dict) else None,
        "menu_error": str(menu.get("error") or menu.get("reason") or "").strip() or None,
    }
    return _ops_surface_payload(
        component="telegram_listener",
        ready=ready,
        status=status,
        reason=reason,
        job_name="sync_telegram",
        last_run_at=state.last_run_at or card.get("last_run_at"),
        last_success_at=card.get("last_success_at"),
        last_error=last_error,
        age_minutes=age_minutes,
        stale_after_minutes=OPS_TELEGRAM_STALE_MINUTES,
        details=details,
    )


def _build_telegram_send_health(
    settings: Settings,
    db: Database,
    *,
    effective_telegram_allowed_chat_ids: Callable[[Settings, Database], list[str]],
) -> dict[str, Any]:
    card, state, cursor = ops_surface_state(db, "sync_telegram")
    allowed_chat_count = len(effective_telegram_allowed_chat_ids(settings, db))
    commands = cursor.get("commands") if isinstance(cursor.get("commands"), dict) else {}
    reminders = cursor.get("reminders") if isinstance(cursor.get("reminders"), dict) else {}
    command_failures = int(commands.get("failed") or 0)
    blocked_sends = int(commands.get("blocked_sends") or 0)
    reminder_failures = int(reminders.get("failed") or 0)
    age_minutes = _ops_age_minutes(card.get("last_success_at") or card.get("last_run_at"))
    stale = bool(age_minutes is not None and age_minutes > OPS_TELEGRAM_STALE_MINUTES)
    status = "never_checked"
    reason = None
    ready = False
    last_error = str(card.get("last_error") or "").strip() or None
    if not bool(getattr(settings, "telegram_enabled", False)):
        status = "disabled"
        reason = "TELEGRAM_ENABLED is false"
    elif not str(getattr(settings, "telegram_bot_token", "") or "").strip():
        status = "not_configured"
        reason = "TELEGRAM_BOT_TOKEN missing"
    elif allowed_chat_count <= 0:
        status = "not_configured"
        reason = "No Telegram chats are allowed for beta operations"
    elif str(card.get("status") or "").strip().lower() == "error":
        status = "error"
        reason = last_error or "Telegram send pipeline failed"
    elif str(card.get("status") or "").strip().lower() == "never":
        status = "never_checked"
    elif blocked_sends > 0 or reminder_failures > 0 or command_failures > 0:
        status = "degraded"
        reason = "Telegram sends had recent failures"
    elif stale:
        status = "stale"
        reason = "Telegram send path has not been exercised recently"
    else:
        status = "ready"
        ready = True
    details = {
        "allowed_chat_count": allowed_chat_count,
        "command_failures": command_failures,
        "blocked_sends": blocked_sends,
        "reminder_failures": reminder_failures,
        "reminders_sent": int(reminders.get("sent") or 0),
    }
    return _ops_surface_payload(
        component="telegram_send",
        ready=ready,
        status=status,
        reason=reason,
        job_name="sync_telegram",
        last_run_at=state.last_run_at or card.get("last_run_at"),
        last_success_at=card.get("last_success_at"),
        last_error=last_error,
        age_minutes=age_minutes,
        stale_after_minutes=OPS_TELEGRAM_STALE_MINUTES,
        details=details,
    )


def _build_weather_sync_health(
    settings: Settings,
    db: Database,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    card, state, cursor = ops_surface_state(db, "sync_weather", user_id=user_id)
    current = cursor.get("current") if isinstance(cursor.get("current"), dict) else {}
    air_quality = cursor.get("air_quality") if isinstance(cursor.get("air_quality"), dict) else {}
    observed_at = str(cursor.get("observed_at") or current.get("observed_at") or "").strip() or None
    age_minutes = _ops_age_minutes(observed_at or state.last_run_at or card.get("last_run_at"))
    stale = bool(age_minutes is not None and age_minutes > OPS_WEATHER_STALE_MINUTES)
    air_error = None
    if air_quality:
        air_error = str(air_quality.get("error") or "").strip() or None if air_quality.get("ok") is False else None
    last_error = str(cursor.get("error") or card.get("last_error") or "").strip() or None
    status = "never_checked"
    reason = None
    ready = False
    if not bool(getattr(settings, "weather_enabled", True)):
        status = "disabled"
        reason = "WEATHER_ENABLED is false"
    elif str(card.get("status") or "").strip().lower() == "error" or last_error:
        status = "error"
        reason = last_error or "Weather sync failed"
    elif str(card.get("status") or "").strip().lower() == "never":
        status = "never_checked"
    elif stale:
        status = "stale"
        reason = "Weather snapshot is older than the freshness window"
    elif air_error:
        status = "degraded"
        reason = air_error
    else:
        status = "ready"
        ready = True
    details = {
        "location_label": str(cursor.get("location_label") or "").strip() or None,
        "observed_at": observed_at,
        "temperature_c": current.get("temperature_c"),
        "air_quality_ok": air_quality.get("ok") if isinstance(air_quality, dict) else None,
        "air_quality_error": air_error,
    }
    return _ops_surface_payload(
        component="weather_sync",
        ready=ready,
        status=status,
        reason=reason,
        job_name="sync_weather",
        last_run_at=state.last_run_at or card.get("last_run_at"),
        last_success_at=card.get("last_success_at"),
        last_error=last_error,
        age_minutes=age_minutes,
        stale_after_minutes=OPS_WEATHER_STALE_MINUTES,
        details=details,
    )


def _build_notice_feed_health(db: Database, kind: str) -> dict[str, Any]:
    job_name = _portal_notice_snapshot_job(kind)
    state = db.get_sync_state(job_name)
    cursor = _json_load(state.last_cursor_json)
    attempt = cursor.get("last_attempt") if isinstance(cursor.get("last_attempt"), dict) else {}
    snapshot = _portal_notice_snapshot_from_cursor(cursor)
    last_run_at = str(attempt.get("attempted_at") or state.last_run_at or "").strip() or None
    last_error = str(attempt.get("error") or "").strip() or None
    status = "never_checked"
    reason = None
    ready = False
    if not last_run_at:
        status = "never_checked"
    elif attempt.get("ok") is False and snapshot.get("fetched_at"):
        status = "degraded"
        reason = last_error or "Notice fetch fell back to cached snapshot"
    elif attempt.get("ok") is False:
        status = "error"
        reason = last_error or "Notice fetch failed"
    else:
        status = "ready"
        ready = True
    return _ops_surface_payload(
        component=f"notice_fetch:{kind}",
        ready=ready,
        status=status,
        reason=reason,
        job_name=job_name,
        last_run_at=last_run_at,
        last_success_at=str(snapshot.get("fetched_at") or "").strip() or None,
        last_error=last_error,
        details={
            "kind": kind,
            "attempted_at": str(attempt.get("attempted_at") or "").strip() or None,
            "http_status": _safe_int(attempt.get("http_status")),
            "snapshot_fetched_at": str(snapshot.get("fetched_at") or "").strip() or None,
            "notice_count": int(snapshot.get("notice_count") or 0),
            "empty": bool(snapshot.get("empty")),
        },
    )


def build_beta_ops_health_report(
    settings: Settings,
    db: Database,
    *,
    chat_id: str | None = None,
    user_id: int | None = None,
    effective_telegram_allowed_chat_ids: Callable[[Settings, Database], list[str]],
    resolve_uos_portal_timetable_targets: Callable[[Settings, Database], list[dict[str, Any]]],
) -> dict[str, Any]:
    scope = _ops_scope_summary(settings, db, chat_id=chat_id, user_id=user_id)
    owner_id = int(scope.get("user_id") or 0) or None
    surfaces = {
        "uos_official_api": _build_uos_official_api_health(
            settings,
            db,
            user_id=owner_id,
            resolve_uos_portal_timetable_targets=resolve_uos_portal_timetable_targets,
        ),
        "uclass_sync": _build_uclass_sync_health(settings, db, user_id=owner_id),
        "telegram_listener": _build_telegram_listener_health(
            settings,
            db,
            effective_telegram_allowed_chat_ids=effective_telegram_allowed_chat_ids,
        ),
        "telegram_send": _build_telegram_send_health(
            settings,
            db,
            effective_telegram_allowed_chat_ids=effective_telegram_allowed_chat_ids,
        ),
        "weather_sync": _build_weather_sync_health(settings, db, user_id=owner_id),
    }
    notice_general = _build_notice_feed_health(db, "general")
    notice_academic = _build_notice_feed_health(db, "academic")
    notice_status = "ready"
    notice_reason = None
    notice_ready = notice_general["ready"] and notice_academic["ready"]
    if not notice_general["last_run_at"] and not notice_academic["last_run_at"]:
        notice_status = "never_checked"
    elif any(item["status"] == "error" for item in (notice_general, notice_academic)):
        notice_status = "error"
        notice_reason = (
            notice_general.get("reason")
            or notice_academic.get("reason")
            or "One or more notice feeds failed"
        )
    elif any(item["status"] == "degraded" for item in (notice_general, notice_academic)):
        notice_status = "degraded"
        notice_reason = (
            notice_general.get("reason")
            or notice_academic.get("reason")
            or "One or more notice feeds are serving cached data"
        )
    elif any(item["status"] == "never_checked" for item in (notice_general, notice_academic)):
        notice_status = "partial"
        notice_reason = "One or more notice feeds have not been exercised yet"
    surfaces["notice_fetch"] = _ops_surface_payload(
        component="notice_fetch",
        ready=notice_ready,
        status=notice_status,
        reason=notice_reason,
        last_run_at=max(
            [item["last_run_at"] for item in (notice_general, notice_academic) if item["last_run_at"]],
            default=None,
        ),
        last_success_at=max(
            [item["last_success_at"] for item in (notice_general, notice_academic) if item["last_success_at"]],
            default=None,
        ),
        last_error=notice_general.get("last_error") or notice_academic.get("last_error"),
        details={
            "feeds": {
                "general": notice_general,
                "academic": notice_academic,
            }
        },
    )
    ready_count = sum(1 for item in surfaces.values() if bool(item.get("ready")))
    return {
        "scope": scope,
        "overall_ready": ready_count == len(surfaces),
        "ready_count": ready_count,
        "not_ready_count": len(surfaces) - ready_count,
        "surfaces": surfaces,
    }
