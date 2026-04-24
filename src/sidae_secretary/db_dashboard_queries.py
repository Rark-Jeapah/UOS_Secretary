from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def dashboard_snapshot(db: Any, now_iso: str | None = None, *, user_id: int | None = None) -> dict[str, Any]:
    now = now_iso or _now_utc_iso()
    owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
    with db.connection() as conn:
        if owner_id is None:
            raw_events = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, start_at, end_at, title, location, rrule, metadata_json
                    FROM events
                    WHERE end_at >= ?
                    ORDER BY start_at ASC
                    LIMIT 200
                    """,
                    (now,),
                ).fetchall()
            ]
        else:
            raw_events = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, start_at, end_at, title, location, rrule, metadata_json
                    FROM events
                    WHERE end_at >= ?
                      AND user_id = ?
                    ORDER BY start_at ASC
                    LIMIT 200
                    """,
                    (now, owner_id),
                ).fetchall()
            ]
        events = [
            item
            for item in raw_events
            if db._dashboard_event_is_active_for_lists(
                source=str(item.get("source") or ""),
                external_id=str(item.get("external_id") or ""),
                metadata_json=str(item.get("metadata_json") or ""),
            )
        ][:50]
        if owner_id is None:
            tasks = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                    ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                    LIMIT 50
                    """
                ).fetchall()
            ]
            done_today = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, due_at, title, status, metadata_json, updated_at
                    FROM tasks
                    WHERE status IN ('done', 'ignored')
                      AND date(updated_at) = date('now')
                    ORDER BY updated_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
            notifications = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, created_at, title, body, url
                    FROM notifications
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                ).fetchall()
            ]
            conflict_warnings = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, created_at, title, body, url
                    FROM notifications
                    WHERE source = 'conflict'
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
            artifacts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                    FROM artifacts
                    ORDER BY updated_at DESC
                    LIMIT 50
                    """
                ).fetchall()
            ]
            sync = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT job_name, last_run_at, last_cursor_json
                    FROM sync_state
                    ORDER BY last_run_at DESC
                    """
                ).fetchall()
            ]
            inbox = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, received_at, title, body, item_type, draft_json
                    FROM inbox
                    WHERE processed = 0
                    ORDER BY received_at DESC
                    LIMIT 50
                    """
                ).fetchall()
            ]
            summaries = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, created_at, title, body, action_item, metadata_json
                    FROM summaries
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
        else:
            tasks = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                      AND user_id = ?
                    ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                    LIMIT 50
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            done_today = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, due_at, title, status, metadata_json, updated_at
                    FROM tasks
                    WHERE status IN ('done', 'ignored')
                      AND date(updated_at) = date('now')
                      AND user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 20
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            notifications = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, created_at, title, body, url
                    FROM notifications
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            conflict_warnings = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, created_at, title, body, url
                    FROM notifications
                    WHERE source = 'conflict'
                      AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            artifacts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                    FROM artifacts
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 50
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            sync = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT job_name, last_run_at, last_cursor_json
                    FROM user_sync_state
                    WHERE user_id = ?
                    ORDER BY last_run_at DESC
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            inbox = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, received_at, title, body, item_type, draft_json
                    FROM inbox
                    WHERE processed = 0
                      AND user_id = ?
                    ORDER BY received_at DESC
                    LIMIT 50
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            summaries = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT external_id, source, created_at, title, body, action_item, metadata_json
                    FROM summaries
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (owner_id,),
                ).fetchall()
            ]
    for collection in (events, tasks, done_today, artifacts, summaries):
        for item in collection:
            provenance = db._dashboard_normalize_provenance(
                item.get("metadata_json"),
                fallback_source=str(item.get("source") or ""),
            )
            item["provenance"] = provenance
            if "metadata_json" in item and not item.get("metadata_json"):
                item["metadata_json"] = "{}"
            if collection is artifacts:
                metadata = db._dashboard_parse_metadata_json(item.get("metadata_json"))
                brief = metadata.get("brief")
                if isinstance(brief, dict):
                    item["brief_provenance"] = db._dashboard_normalize_provenance(
                        brief,
                        fallback_source=str(item.get("source") or ""),
                    )
    sync_dashboard = db.sync_dashboard_snapshot(user_id=user_id)
    weather_snapshot = db.latest_weather_snapshot(user_id=user_id)
    auth_monitor = db.auth_attempt_dashboard_snapshot(now_iso=now)
    last_sync = sync_dashboard.get("last_successful_sync_at") or (sync[0]["last_run_at"] if sync else None)
    return {
        "last_sync_at": last_sync,
        "upcoming_events": events,
        "due_tasks": tasks,
        "done_today": done_today,
        "new_notifications": notifications,
        "conflict_warnings": conflict_warnings,
        "recent_materials": artifacts,
        "inbox_unprocessed": inbox,
        "summaries": summaries,
        "sync_state": sync,
        "sync_dashboard": sync_dashboard,
        "weather_snapshot": weather_snapshot,
        "auth_monitor": auth_monitor,
    }
