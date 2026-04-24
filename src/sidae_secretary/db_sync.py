from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


SYNC_DASHBOARD_SPECS = (
    ("sync_uos_portal_timetable", "portal", "공식 시간표"),
    ("sync_uclass", "uclass", "UClass"),
    ("sync_weather", "weather", "Weather"),
    ("sync_telegram", "telegram", "Telegram"),
    ("publish_dashboard", "dashboard", "Dashboard"),
)


def _normalize_datetime(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        from dateutil import parser as dt_parser

        dt = dt_parser.isoparse(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_user_id(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _sync_dashboard_status(cursor: dict[str, Any]) -> str:
    meta = cursor.get("_sync_dashboard") if isinstance(cursor.get("_sync_dashboard"), dict) else {}
    status = str(meta.get("status") or "").strip().lower()
    if status in {"success", "skipped", "error"}:
        return status
    if cursor.get("error") or cursor.get("last_error"):
        return "error"
    if cursor.get("skipped"):
        return "skipped"
    if cursor:
        return "success"
    return "never"


def _infer_sync_dashboard_counts(job_name: str, cursor: dict[str, Any]) -> tuple[int, int]:
    if job_name == "sync_uos_portal_timetable":
        return int(cursor.get("upserted") or cursor.get("upserted_events") or 0), 0
    if job_name == "sync_uclass":
        semantic_warnings = cursor.get("semantic_warnings")
        warning_count = len(semantic_warnings) if isinstance(semantic_warnings, list) else int(semantic_warnings or 0)
        return (
            int(cursor.get("notifications") or 0)
            + int(cursor.get("tasks") or 0)
            + int(cursor.get("events") or 0)
            + int(cursor.get("artifacts") or 0),
            int(cursor.get("material_download_failures") or 0)
            + int(cursor.get("material_extract_failures") or 0)
            + warning_count,
        )
    if job_name == "sync_weather":
        return (1 if isinstance(cursor.get("current"), dict) else 0, 1 if _sync_dashboard_status(cursor) == "error" else 0)
    if job_name == "sync_telegram":
        commands = cursor.get("commands") if isinstance(cursor.get("commands"), dict) else {}
        reminders = cursor.get("reminders") if isinstance(cursor.get("reminders"), dict) else {}
        return (
            int(cursor.get("stored") or cursor.get("stored_messages") or 0),
            int(commands.get("failed") or 0)
            + int(commands.get("blocked_sends") or 0)
            + int(reminders.get("failed") or 0),
        )
    if job_name == "publish_dashboard":
        return (1 if str(cursor.get("output") or "").strip() else 0), 0
    return 0, 0


def _sync_dashboard_card(
    job_name: str,
    label: str,
    source_key: str,
    state: Any | None,
) -> dict[str, Any]:
    cursor = _json_load(state.last_cursor_json if state else None)
    meta = cursor.get("_sync_dashboard") if isinstance(cursor.get("_sync_dashboard"), dict) else {}
    status = _sync_dashboard_status(cursor) if state else "never"
    inferred_new_items, inferred_action_required = _infer_sync_dashboard_counts(job_name, cursor)
    last_error = str(meta.get("last_error") or cursor.get("error") or "").strip() or None
    last_success_at = _normalize_datetime(meta.get("last_success_at"))
    if status == "success" and not last_success_at and state:
        last_success_at = _normalize_datetime(state.last_run_at)
    new_items = int(meta.get("new_items") or inferred_new_items or 0)
    action_required = int(meta.get("action_required") or inferred_action_required or 0)
    return {
        "job_name": job_name,
        "key": source_key,
        "label": label,
        "status": status,
        "last_run_at": state.last_run_at if state else None,
        "last_success_at": last_success_at,
        "last_error": last_error,
        "new_items": new_items,
        "action_required": action_required,
    }


def sync_dashboard_snapshot(db: Any, *, user_id: int | None = None) -> dict[str, Any]:
    counts = db.counts(user_id=user_id)
    sync_states = {row.job_name: row for row in db.list_sync_states(user_id=user_id)}
    source_cards = [
        _sync_dashboard_card(
            job_name=job_name,
            label=label,
            source_key=source_key,
            state=sync_states.get(job_name),
        )
        for job_name, source_key, label in SYNC_DASHBOARD_SPECS
    ]
    last_successful_sync_at = None
    last_error_card: dict[str, Any] | None = None
    for card in source_cards:
        success_at = _normalize_datetime(card.get("last_success_at"))
        if success_at and (
            last_successful_sync_at is None
            or success_at > str(last_successful_sync_at)
        ):
            last_successful_sync_at = success_at
        if card["last_error"] and card["status"] == "error":
            if last_error_card is None:
                last_error_card = card
                continue
            current_run = _normalize_datetime(card.get("last_run_at"))
            previous_run = _normalize_datetime(last_error_card.get("last_run_at"))
            if current_run and (previous_run is None or current_run > previous_run):
                last_error_card = card

    low_confidence_tasks = 0
    owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
    with db.connection() as conn:
        if owner_id is None:
            task_rows = conn.execute(
                """
                SELECT source, metadata_json
                FROM tasks
                WHERE status = 'open'
                """
            ).fetchall()
            inbox_row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM inbox
                WHERE processed = 0
                """
            ).fetchone()
            conflict_row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM notifications
                WHERE source = 'conflict'
                """
            ).fetchone()
        else:
            task_rows = conn.execute(
                """
                SELECT source, metadata_json
                FROM tasks
                WHERE status = 'open'
                  AND user_id = ?
                """,
                (owner_id,),
            ).fetchall()
            inbox_row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM inbox
                WHERE processed = 0
                  AND user_id = ?
                """,
                (owner_id,),
            ).fetchone()
            conflict_row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM notifications
                WHERE source = 'conflict'
                  AND user_id = ?
                """,
                (owner_id,),
            ).fetchone()
    for row in task_rows:
        provenance = db._dashboard_normalize_provenance(
            row["metadata_json"],
            fallback_source=str(row["source"] or ""),
        )
        if provenance["confidence"] == "low":
            low_confidence_tasks += 1

    sync_errors = sum(1 for card in source_cards if card["status"] == "error")
    inbox_unprocessed = int(inbox_row["count"]) if inbox_row else 0
    conflict_warnings = int(conflict_row["count"]) if conflict_row else 0
    return {
        "last_successful_sync_at": last_successful_sync_at,
        "last_error": (
            {
                "source": last_error_card["label"],
                "message": last_error_card["last_error"],
                "last_run_at": last_error_card["last_run_at"],
            }
            if last_error_card
            else None
        ),
        "sources": source_cards,
        "counts": counts,
        "pending_inbox_count": inbox_unprocessed,
        "low_confidence_task_count": low_confidence_tasks,
        "conflict_warning_count": conflict_warnings,
        "sync_error_count": sync_errors,
        "action_required_count": inbox_unprocessed + low_confidence_tasks + conflict_warnings + sync_errors,
    }


def latest_weather_snapshot(
    db: Any,
    *,
    user_id: int | None = 0,
    allow_global_fallback: bool = True,
) -> dict[str, Any] | None:
    owner_id = _normalize_user_id(user_id, default=0) or 0
    state = db.get_sync_state("sync_weather", user_id=owner_id)
    cursor = _json_load(state.last_cursor_json if state else None)
    if cursor and isinstance(cursor.get("current"), dict):
        snapshot = dict(cursor)
        snapshot.setdefault("last_run_at", state.last_run_at if state else None)
        return snapshot
    if owner_id > 0 and allow_global_fallback:
        return latest_weather_snapshot(db, user_id=0, allow_global_fallback=False)
    return None
