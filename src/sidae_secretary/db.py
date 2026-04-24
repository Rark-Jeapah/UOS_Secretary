from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Iterator

from dateutil import parser as dt_parser

from sidae_secretary import db_auth_attempts, db_connections, db_dashboard_queries, db_sync
from sidae_secretary.models import (
    Artifact,
    Course,
    CourseAlias,
    Event,
    InboxItem,
    Notification,
    Summary,
    SyncState,
    Task,
)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_datetime(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = dt_parser.isoparse(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _json_dump(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=True, sort_keys=True)


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_dump_list(payload: list[Any] | None) -> str:
    return json.dumps(payload or [], ensure_ascii=True)


def _json_load_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


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


def _db_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(int(value))


def _db_optional_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _db_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_json_list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


_PREFERENCE_UNSET = object()
USER_PREFERENCE_FIELDS = (
    "telegram_chat_allowed",
    "material_brief_push_enabled",
    "scheduled_briefings_enabled",
    "daily_digest_enabled",
)


PROVENANCE_SOURCE_LABELS = {
    "llm_inferred": "LLM inferred",
    "portal_csv": "Portal CSV",
    "portal_ics": "Portal ICS",
    "portal_ics_url": "Portal ICS URL",
    "portal_uos_timetable": "UOS Portal",
    "telegram_draft": "Telegram draft",
    "uclass_html": "UClass HTML",
    "uclass_ws": "UClass WS",
    "unknown": "Unknown",
}
PROVENANCE_OFFICIAL_SOURCES = {
    "portal_csv",
    "portal_ics",
    "portal_ics_url",
    "portal_uos_timetable",
    "uclass_html",
    "uclass_ws",
}
def parse_metadata_json(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return _json_load(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    elif value is None:
        items = []
    else:
        items = [value]
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def normalize_course_alias(value: str | None) -> str:
    parts = re.findall(r"[0-9A-Za-z\u3131-\u318E\uAC00-\uD7A3]+", str(value or ""))
    return "".join(part.lower() for part in parts)


def _metadata_raw_source_ids(metadata: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for key in (
        "uid",
        "course_id",
        "module_id",
        "artifact_external_id",
        "inbox_external_id",
        "payload_hash",
        "external_id",
    ):
        value = metadata.get(key)
        if value in (None, ""):
            continue
        output.append(f"{key}:{value}")
    raw = metadata.get("raw")
    if isinstance(raw, dict):
        for key in ("id", "eventid", "notificationid", "discussion"):
            value = raw.get(key)
            if value in (None, ""):
                continue
            output.append(f"raw.{key}:{value}")
    return _string_list(output)


def _metadata_evidence_links(metadata: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for key in ("url", "original_url", "article_url"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            output.append(value.strip())
    return _string_list(output)


def _fallback_provenance_source(
    metadata: dict[str, Any],
    fallback_source: str | None,
) -> str:
    source = str(fallback_source or "").strip().lower()
    if source == "llm":
        return "llm_inferred"
    if source == "inbox":
        return "telegram_draft"
    if source == "portal":
        if str(metadata.get("source") or "").strip().lower() == "csv":
            return "portal_csv"
        if str(metadata.get("import_origin") or "").strip().lower() == "ics_url":
            return "portal_ics_url"
        return "portal_ics"
    if source == "uclass":
        if str(metadata.get("detected_method") or "").strip().lower() == "llm":
            return "llm_inferred"
        if str(metadata.get("source_kind") or "").strip():
            return "uclass_html"
        return "uclass_ws"
    if source in PROVENANCE_SOURCE_LABELS:
        return source
    return source or "unknown"


def normalize_provenance(
    value: str | dict[str, Any] | None,
    *,
    fallback_source: str | None = None,
) -> dict[str, Any]:
    metadata = parse_metadata_json(value)
    raw = metadata.get("provenance") if isinstance(metadata.get("provenance"), dict) else {}
    source = str(raw.get("source") or _fallback_provenance_source(metadata, fallback_source)).strip().lower()
    if not source:
        source = "unknown"

    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        detected_method = str(metadata.get("detected_method") or "").strip().lower()
        if source in {"uclass_ws", "uclass_html", "portal_csv", "portal_ics", "portal_ics_url"}:
            confidence = "high"
        elif source == "telegram_draft":
            confidence = "low"
        elif source == "llm_inferred":
            confidence = "medium"
        else:
            confidence = "low"
        if detected_method == "heuristic":
            confidence = "low"
        elif detected_method == "llm":
            confidence = "medium"

    last_verified_at = normalize_datetime(
        raw.get("last_verified_at")
        or metadata.get("last_verified_at")
        or metadata.get("downloaded_at")
        or metadata.get("completed_at")
    )
    evidence_links = _string_list(raw.get("evidence_links")) or _metadata_evidence_links(metadata)
    raw_source_ids = _string_list(raw.get("raw_source_ids")) or _metadata_raw_source_ids(metadata)
    derivation = str(raw.get("derivation") or metadata.get("detected_method") or "").strip().lower()
    is_official = source in PROVENANCE_OFFICIAL_SOURCES and confidence == "high"
    is_estimate = not is_official or confidence != "high"
    source_label = PROVENANCE_SOURCE_LABELS.get(source, source.replace("_", " ").title())
    notice = "Official data"
    if confidence == "low":
        notice = "Estimated; not official"
    elif confidence == "medium":
        notice = "Derived or inferred"
    elif not is_official:
        notice = "Non-official source"
    return {
        "source": source,
        "source_label": source_label,
        "confidence": confidence,
        "confidence_label": confidence.title(),
        "last_verified_at": last_verified_at,
        "evidence_links": evidence_links,
        "raw_source_ids": raw_source_ids,
        "derivation": derivation or None,
        "is_official": is_official,
        "is_estimate": is_estimate,
        "notice": notice,
    }


def attach_provenance(
    metadata_json: dict[str, Any] | None,
    *,
    source: str,
    confidence: str,
    last_verified_at: str | None = None,
    evidence_links: list[str] | None = None,
    raw_source_ids: list[str] | None = None,
    derivation: str | None = None,
) -> dict[str, Any]:
    metadata = parse_metadata_json(metadata_json)
    provenance = {
        "source": str(source or "").strip().lower() or "unknown",
        "confidence": str(confidence or "").strip().lower() or "low",
        "last_verified_at": normalize_datetime(last_verified_at) or now_utc_iso(),
        "evidence_links": _string_list(evidence_links),
        "raw_source_ids": _string_list(raw_source_ids),
        "derivation": str(derivation or "").strip().lower() or None,
    }
    metadata["provenance"] = normalize_provenance(
        {**metadata, "provenance": provenance},
        fallback_source=source,
    )
    return metadata
def canonical_task_status(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"done", "completed", "closed", "complete"}:
        return "done"
    if raw in {"ignored", "skip", "skipped"}:
        return "ignored"
    return "open"


def _event_review_status(value: str | None) -> str:
    metadata = _json_load(value)
    raw = str(metadata.get("review_status") or "").strip().lower()
    if raw in {"done", "skipped"}:
        return raw
    return "scheduled"


def _is_event_active_for_lists(source: str, external_id: str, metadata_json: str | None) -> bool:
    if source == "review" or external_id.startswith("review:"):
        return _event_review_status(metadata_json) == "scheduled"
    return True


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT,
            rrule TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(external_id, source)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            due_at TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(external_id, source)
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            filename TEXT NOT NULL,
            icloud_path TEXT,
            content_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(external_id, source)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            ingested_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(external_id, source)
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            job_name TEXT PRIMARY KEY,
            last_run_at TEXT,
            last_cursor_json TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            received_at TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            item_type TEXT NOT NULL,
            draft_json TEXT NOT NULL DEFAULT '{}',
            processed INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(external_id, source)
        );

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            action_item TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(external_id, source)
        );

        CREATE INDEX IF NOT EXISTS idx_inbox_processed_received
            ON inbox(processed, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_summaries_created
            ON summaries(created_at DESC);
        """,
    ),
    (
        3,
        """
        UPDATE tasks
        SET status = CASE
            WHEN LOWER(TRIM(status)) IN ('done', 'completed', 'closed', 'complete') THEN 'done'
            WHEN LOWER(TRIM(status)) IN ('ignored', 'skip', 'skipped') THEN 'ignored'
            ELSE 'open'
        END;

        CREATE INDEX IF NOT EXISTS idx_tasks_status_due
            ON tasks(status, due_at);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS identity_ack (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            acknowledged_at TEXT NOT NULL,
            expires_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_identity_ack_expires_at
            ON identity_ack(expires_at);
        CREATE INDEX IF NOT EXISTS idx_identity_ack_acknowledged_at
            ON identity_ack(acknowledged_at DESC);
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS building_map (
            building_no TEXT PRIMARY KEY,
            building_name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telegram_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL UNIQUE,
            chat_id TEXT NOT NULL,
            run_at TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            sent_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_telegram_reminders_status_run_at
            ON telegram_reminders(status, run_at);
        """,
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS courses (
            canonical_course_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            external_course_id TEXT,
            display_name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, external_course_id)
        );

        CREATE TABLE IF NOT EXISTS course_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_course_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            source TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(canonical_course_id) REFERENCES courses(canonical_course_id) ON DELETE CASCADE,
            UNIQUE(canonical_course_id, normalized_alias, alias_type, source)
        );

        CREATE INDEX IF NOT EXISTS idx_course_aliases_normalized_alias
            ON course_aliases(normalized_alias);
        """,
    ),
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS onboarding_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_kind TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            chat_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_onboarding_sessions_chat_kind
            ON onboarding_sessions(chat_id, session_kind, expires_at DESC);

        CREATE TABLE IF NOT EXISTS moodle_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            school_slug TEXT NOT NULL,
            display_name TEXT NOT NULL,
            ws_base_url TEXT NOT NULL,
            username TEXT NOT NULL,
            secret_kind TEXT NOT NULL,
            secret_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            last_verified_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(chat_id, school_slug)
        );

        CREATE INDEX IF NOT EXISTS idx_moodle_connections_chat_status
            ON moodle_connections(chat_id, status, updated_at DESC);
        """,
    ),
    (
        8,
        """
        CREATE TABLE IF NOT EXISTS moodle_school_directory (
            school_slug TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            ws_base_url TEXT NOT NULL UNIQUE,
            login_url TEXT,
            homepage_url TEXT,
            source_url TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_moodle_school_directory_display_name
            ON moodle_school_directory(display_name COLLATE NOCASE ASC);
        """,
    ),
    (
        9,
        """
        CREATE TABLE IF NOT EXISTS lms_browser_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            school_slug TEXT NOT NULL,
            provider TEXT NOT NULL,
            display_name TEXT NOT NULL,
            login_url TEXT NOT NULL,
            profile_dir TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            last_opened_at TEXT,
            last_verified_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(chat_id, school_slug)
        );

        CREATE INDEX IF NOT EXISTS idx_lms_browser_sessions_chat_status
            ON lms_browser_sessions(chat_id, status, updated_at DESC);
        """,
    ),
    (10, "SELECT 1;"),
    (
        11,
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_chat_id TEXT UNIQUE,
            timezone TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_users_chat_status
            ON users(telegram_chat_id, status, updated_at DESC);

        ALTER TABLE events RENAME TO events_old_v11;
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT,
            rrule TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id, source)
        );
        INSERT INTO events(
            id, user_id, external_id, source, start_at, end_at, title, location, rrule,
            metadata_json, created_at, updated_at
        )
        SELECT
            id, 0, external_id, source, start_at, end_at, title, location, rrule,
            metadata_json, created_at, updated_at
        FROM events_old_v11;
        DROP TABLE events_old_v11;
        CREATE INDEX IF NOT EXISTS idx_events_user_start_at
            ON events(user_id, start_at);

        ALTER TABLE tasks RENAME TO tasks_old_v11;
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            due_at TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id, source)
        );
        INSERT INTO tasks(
            id, user_id, external_id, source, due_at, title, status, metadata_json, created_at, updated_at
        )
        SELECT
            id, 0, external_id, source, due_at, title, status, metadata_json, created_at, updated_at
        FROM tasks_old_v11;
        DROP TABLE tasks_old_v11;
        CREATE INDEX IF NOT EXISTS idx_tasks_user_status_due
            ON tasks(user_id, status, due_at);

        ALTER TABLE artifacts RENAME TO artifacts_old_v11;
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            filename TEXT NOT NULL,
            icloud_path TEXT,
            content_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id, source)
        );
        INSERT INTO artifacts(
            id, user_id, external_id, source, filename, icloud_path, content_hash,
            metadata_json, created_at, updated_at
        )
        SELECT
            id, 0, external_id, source, filename, icloud_path, content_hash,
            metadata_json, created_at, updated_at
        FROM artifacts_old_v11;
        DROP TABLE artifacts_old_v11;
        CREATE INDEX IF NOT EXISTS idx_artifacts_user_updated
            ON artifacts(user_id, updated_at DESC);

        ALTER TABLE notifications RENAME TO notifications_old_v11;
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            ingested_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id, source)
        );
        INSERT INTO notifications(
            id, user_id, external_id, source, created_at, title, body, url,
            metadata_json, ingested_at, updated_at
        )
        SELECT
            id, 0, external_id, source, created_at, title, body, url,
            metadata_json, ingested_at, updated_at
        FROM notifications_old_v11;
        DROP TABLE notifications_old_v11;
        CREATE INDEX IF NOT EXISTS idx_notifications_user_created
            ON notifications(user_id, created_at DESC);

        ALTER TABLE inbox RENAME TO inbox_old_v11;
        CREATE TABLE inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            received_at TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            item_type TEXT NOT NULL,
            draft_json TEXT NOT NULL DEFAULT '{}',
            processed INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id, source)
        );
        INSERT INTO inbox(
            id, user_id, external_id, source, received_at, title, body, item_type,
            draft_json, processed, metadata_json, created_at, updated_at
        )
        SELECT
            id, 0, external_id, source, received_at, title, body, item_type,
            draft_json, processed, metadata_json, created_at, updated_at
        FROM inbox_old_v11;
        DROP TABLE inbox_old_v11;
        CREATE INDEX IF NOT EXISTS idx_inbox_user_processed_received
            ON inbox(user_id, processed, received_at DESC);

        ALTER TABLE summaries RENAME TO summaries_old_v11;
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            action_item TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id, source)
        );
        INSERT INTO summaries(
            id, user_id, external_id, source, created_at, title, body, action_item, metadata_json, updated_at
        )
        SELECT
            id, 0, external_id, source, created_at, title, body, action_item, metadata_json, updated_at
        FROM summaries_old_v11;
        DROP TABLE summaries_old_v11;
        CREATE INDEX IF NOT EXISTS idx_summaries_user_created
            ON summaries(user_id, created_at DESC);

        ALTER TABLE telegram_reminders RENAME TO telegram_reminders_old_v11;
        CREATE TABLE telegram_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            external_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            run_at TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            sent_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id)
        );
        INSERT INTO telegram_reminders(
            id, user_id, external_id, chat_id, run_at, message, status, sent_at,
            metadata_json, created_at, updated_at
        )
        SELECT
            id, 0, external_id, chat_id, run_at, message, status, sent_at,
            metadata_json, created_at, updated_at
        FROM telegram_reminders_old_v11;
        DROP TABLE telegram_reminders_old_v11;
        CREATE INDEX IF NOT EXISTS idx_telegram_reminders_user_status_run_at
            ON telegram_reminders(user_id, status, run_at);
        """,
    ),
    (
        12,
        """
        CREATE TABLE IF NOT EXISTS schools (
            school_id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_slug TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_schools_slug_status
            ON schools(school_slug, status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS user_sync_state (
            user_id INTEGER NOT NULL,
            job_name TEXT NOT NULL,
            last_run_at TEXT,
            last_cursor_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, job_name)
        );

        CREATE INDEX IF NOT EXISTS idx_user_sync_state_job
            ON user_sync_state(job_name, last_run_at DESC);

        CREATE TABLE IF NOT EXISTS school_buildings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            building_code TEXT NOT NULL,
            building_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(school_id, building_code),
            FOREIGN KEY(school_id) REFERENCES schools(school_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_school_buildings_code
            ON school_buildings(school_id, building_code);

        ALTER TABLE moodle_connections ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_moodle_connections_user_status
            ON moodle_connections(user_id, status, updated_at DESC);

        ALTER TABLE lms_browser_sessions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_lms_browser_sessions_user_status
            ON lms_browser_sessions(user_id, status, updated_at DESC);

        ALTER TABLE course_aliases RENAME TO course_aliases_old_v12;
        ALTER TABLE courses RENAME TO courses_old_v12;

        CREATE TABLE courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            canonical_course_id TEXT NOT NULL,
            source TEXT NOT NULL,
            external_course_id TEXT,
            display_name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, canonical_course_id),
            UNIQUE(user_id, source, external_course_id)
        );

        INSERT INTO courses(
            user_id, canonical_course_id, source, external_course_id, display_name,
            metadata_json, created_at, updated_at
        )
        SELECT
            0, canonical_course_id, source, external_course_id, display_name,
            metadata_json, created_at, updated_at
        FROM courses_old_v12;

        CREATE INDEX IF NOT EXISTS idx_courses_user_display_name
            ON courses(user_id, display_name COLLATE NOCASE ASC, canonical_course_id ASC);

        CREATE TABLE course_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            canonical_course_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            source TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id, canonical_course_id)
                REFERENCES courses(user_id, canonical_course_id)
                ON DELETE CASCADE,
            UNIQUE(user_id, canonical_course_id, normalized_alias, alias_type, source)
        );

        INSERT INTO course_aliases(
            id, user_id, canonical_course_id, alias, normalized_alias, alias_type, source,
            metadata_json, created_at, updated_at
        )
        SELECT
            id, 0, canonical_course_id, alias, normalized_alias, alias_type, source,
            metadata_json, created_at, updated_at
        FROM course_aliases_old_v12;

        DROP TABLE course_aliases_old_v12;
        DROP TABLE courses_old_v12;

        CREATE INDEX IF NOT EXISTS idx_course_aliases_user_normalized_alias
            ON course_aliases(user_id, normalized_alias);
        """,
    ),
    (
        14,
        """
        SELECT 1;
        """,
    ),
    (
        15,
        """
        CREATE TABLE IF NOT EXISTS auth_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            chat_id TEXT,
            onboarding_session_id INTEGER,
            session_kind TEXT,
            school_slug TEXT,
            remote_addr TEXT,
            username TEXT,
            status TEXT NOT NULL,
            failure_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_auth_attempts_created
            ON auth_attempts(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_auth_attempts_session_created
            ON auth_attempts(onboarding_session_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_auth_attempts_chat_created
            ON auth_attempts(chat_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_auth_attempts_remote_created
            ON auth_attempts(remote_addr, status, created_at DESC);
        """,
    ),
    (
        16,
        """
        ALTER TABLE lms_browser_sessions ADD COLUMN secret_kind TEXT;
        ALTER TABLE lms_browser_sessions ADD COLUMN secret_ref TEXT;
        """,
    ),
    (
        17,
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            telegram_chat_allowed INTEGER,
            material_brief_push_enabled INTEGER,
            scheduled_briefings_enabled INTEGER,
            daily_digest_enabled INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_user_preferences_telegram_allowed
            ON user_preferences(telegram_chat_allowed, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_preferences_material_brief_push
            ON user_preferences(material_brief_push_enabled, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_preferences_scheduled_briefings
            ON user_preferences(scheduled_briefings_enabled, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_preferences_daily_digest
            ON user_preferences(daily_digest_enabled, updated_at DESC);
        """,
    ),
    (
        18,
        """
        ALTER TABLE user_preferences ADD COLUMN weather_location_label TEXT;
        ALTER TABLE user_preferences ADD COLUMN weather_lat REAL;
        ALTER TABLE user_preferences ADD COLUMN weather_lon REAL;
        ALTER TABLE user_preferences ADD COLUMN weather_air_quality_district_code TEXT;
        """,
    ),
    (
        19,
        """
        SELECT 1;
        """,
    ),
    (
        20,
        """
        ALTER TABLE moodle_connections ADD COLUMN login_secret_kind TEXT;
        ALTER TABLE moodle_connections ADD COLUMN login_secret_ref TEXT;
        """,
    ),
    (
        21,
        """
        CREATE TABLE IF NOT EXISTS notification_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            policy_kind TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            days_of_week_json TEXT NOT NULL DEFAULT '[]',
            time_local TEXT,
            timezone TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, policy_kind)
        );

        CREATE INDEX IF NOT EXISTS idx_notification_policies_user_updated
            ON notification_policies(user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_notification_policies_kind_enabled
            ON notification_policies(policy_kind, enabled, updated_at DESC);
        """,
    ),
    (
        22,
        """
        CREATE TABLE IF NOT EXISTS assistant_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id TEXT,
            request_raw TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}',
            planner_output_json TEXT NOT NULL DEFAULT '{}',
            executor_result_json TEXT NOT NULL DEFAULT '{}',
            final_reply TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_assistant_runs_user_created
            ON assistant_runs(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_assistant_runs_chat_created
            ON assistant_runs(chat_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_assistant_runs_status_created
            ON assistant_runs(status, created_at DESC);
        """,
    ),
]


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @property
    def init_lock_path(self) -> Path:
        suffix = self.db_path.suffix
        if suffix:
            return self.db_path.with_suffix(f"{suffix}.init.lock")
        return Path(f"{self.db_path}.init.lock")

    @contextmanager
    def migration_lock(self) -> Iterator[None]:
        lock_path = self.init_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fp:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.migration_lock():
            with self.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                current = int(row["version"])
                for version, sql in MIGRATIONS:
                    if version <= current:
                        continue
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (version, now_utc_iso()),
                    )
        self._seed_builtin_moodle_school_directory()
        self._seed_builtin_schools()
        self._seed_builtin_uos_buildings()
        self._backfill_multitenant_state()

    def _seed_builtin_moodle_school_directory(self) -> None:
        from sidae_secretary.moodle_school_directory import (
            BUILTIN_MOODLE_SCHOOL_DIRECTORY,
        )

        for item in BUILTIN_MOODLE_SCHOOL_DIRECTORY:
            if not isinstance(item, dict):
                continue
            self.upsert_moodle_school_directory(
                school_slug=str(item.get("school_slug") or ""),
                display_name=str(item.get("display_name") or ""),
                ws_base_url=str(item.get("ws_base_url") or ""),
                login_url=item.get("login_url"),
                homepage_url=item.get("homepage_url"),
                source_url=item.get("source_url"),
                aliases=item.get("aliases"),
                metadata_json=item.get("metadata_json"),
            )

    def _has_column(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(row["name"] or "") == str(column) for row in rows)

    def _seed_builtin_schools(self) -> None:
        from sidae_secretary.school_support import school_support_summary

        self.upsert_school(
            school_slug="uos_online_class",
            display_name="서울시립대학교 온라인강의실",
            metadata_json={"source": "builtin_default"},
        )
        for item in self.list_moodle_school_directory(limit=2000):
            school_slug = str(item.get("school_slug") or "").strip().lower()
            display_name = str(item.get("display_name") or school_slug).strip() or school_slug
            if not school_slug:
                continue
            support = school_support_summary(item)
            self.upsert_school(
                school_slug=school_slug,
                display_name=display_name,
                metadata_json={
                    "source": "moodle_school_directory",
                    "ws_base_url": str(item.get("ws_base_url") or "").strip() or None,
                    "homepage_url": str(item.get("homepage_url") or "").strip() or None,
                    "support_level": str(support.get("support_level") or ""),
                    "official_user_support": bool(
                        support.get("official_user_support")
                    ),
                    "capabilities": dict(support.get("capabilities") or {}),
                },
            )

    def _seed_builtin_uos_buildings(self) -> None:
        from sidae_secretary.buildings import UOS_BUILDING_MAP

        global_buildings = {
            str(item["building_no"]): str(item["building_name"])
            for item in self.list_buildings(limit=5000, school_slug="__missing_school__")
        }
        uos_online_buildings = {
            str(item["building_no"]): str(item["building_name"])
            for item in self.list_buildings(limit=5000, school_slug="uos_online_class")
        }
        uos_portal_exists = self.get_school_by_slug("uos_portal") is not None
        uos_portal_buildings = {
            str(item["building_no"]): str(item["building_name"])
            for item in self.list_buildings(limit=5000, school_slug="uos_portal")
        } if uos_portal_exists else {}

        for building_no, building_name in UOS_BUILDING_MAP.items():
            code = str(building_no).strip()
            name = str(building_name).strip()
            if not code or not name:
                continue
            if code not in global_buildings:
                self.upsert_building(
                    building_no=code,
                    building_name=name,
                    metadata_json={"source": "builtin_uos_seed"},
                    school_slug="uos_online_class",
                )
                global_buildings[code] = name
                uos_online_buildings[code] = name
            elif code not in uos_online_buildings:
                self.upsert_school_building(
                    school_slug="uos_online_class",
                    building_code=code,
                    building_name=global_buildings.get(code) or name,
                    metadata_json={"source": "builtin_uos_seed"},
                )
                uos_online_buildings[code] = global_buildings.get(code) or name
            if uos_portal_exists and code not in uos_portal_buildings:
                self.upsert_school_building(
                    school_slug="uos_portal",
                    building_code=code,
                    building_name=global_buildings.get(code) or name,
                    metadata_json={"source": "builtin_uos_seed"},
                )
                uos_portal_buildings[code] = global_buildings.get(code) or name

    def _backfill_multitenant_state(self) -> None:
        with self.connection() as conn:
            def ensure_user_row(chat_id: str, source_label: str) -> int:
                chat = str(chat_id or "").strip()
                if not chat:
                    return 0
                row = conn.execute(
                    """
                    SELECT id, metadata_json
                    FROM users
                    WHERE telegram_chat_id = ?
                    LIMIT 1
                    """,
                    (chat,),
                ).fetchone()
                ts = now_utc_iso()
                if row:
                    metadata = _json_load(row["metadata_json"])
                    metadata.setdefault("source", source_label)
                    conn.execute(
                        """
                        UPDATE users
                        SET metadata_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (_json_dump(metadata), ts, int(row["id"])),
                    )
                    return int(row["id"])
                existing_users_row = conn.execute(
                    "SELECT COUNT(*) AS count FROM users"
                ).fetchone()
                existing_users = int(existing_users_row["count"]) if existing_users_row else 0
                conn.execute(
                    """
                    INSERT INTO users(
                        telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
                    )
                    VALUES (?, NULL, 'active', ?, ?, ?)
                    """,
                    (chat, _json_dump({"source": source_label}), ts, ts),
                )
                user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                if existing_users == 0:
                    self._adopt_global_rows_for_user(conn, user_id)
                return user_id

            if self._has_column(conn, "moodle_connections", "user_id"):
                rows = conn.execute(
                    """
                    SELECT id, chat_id
                    FROM moodle_connections
                    WHERE user_id = 0
                    ORDER BY id ASC
                    """
                ).fetchall()
                for row in rows:
                    chat_id = str(row["chat_id"] or "").strip()
                    if not chat_id:
                        continue
                    user_id = ensure_user_row(chat_id, "moodle_connection_backfill")
                    conn.execute(
                        "UPDATE moodle_connections SET user_id = ? WHERE id = ?",
                        (user_id, int(row["id"])),
                    )

            if self._has_column(conn, "lms_browser_sessions", "user_id"):
                rows = conn.execute(
                    """
                    SELECT id, chat_id
                    FROM lms_browser_sessions
                    WHERE user_id = 0
                    ORDER BY id ASC
                    """
                ).fetchall()
                for row in rows:
                    chat_id = str(row["chat_id"] or "").strip()
                    if not chat_id:
                        continue
                    user_id = ensure_user_row(chat_id, "lms_browser_session_backfill")
                    conn.execute(
                        "UPDATE lms_browser_sessions SET user_id = ? WHERE id = ?",
                        (user_id, int(row["id"])),
                    )

            default_school = self.get_school_by_slug("uos_online_class")
            default_school_id = int(default_school["school_id"]) if default_school else 0
            if default_school_id > 0:
                rows = conn.execute(
                    """
                    SELECT building_no, building_name, metadata_json, updated_at
                    FROM building_map
                    ORDER BY building_no ASC
                    """
                ).fetchall()
                for row in rows:
                    building_code = str(row["building_no"] or "").strip()
                    building_name = str(row["building_name"] or "").strip()
                    if not building_code or not building_name:
                        continue
                    exists = conn.execute(
                        """
                        SELECT id
                        FROM school_buildings
                        WHERE school_id = ? AND building_code = ?
                        LIMIT 1
                        """,
                        (default_school_id, building_code),
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        """
                        INSERT INTO school_buildings(
                            school_id, building_code, building_name, aliases_json, metadata_json, updated_at
                        )
                        VALUES (?, ?, ?, '[]', ?, ?)
                        """,
                        (
                            default_school_id,
                            building_code,
                            building_name,
                            row["metadata_json"] or "{}",
                            row["updated_at"] or now_utc_iso(),
                        ),
                    )

    def _adopt_global_rows_for_user(self, conn: sqlite3.Connection, user_id: int) -> None:
        adopted_user_id = _normalize_user_id(user_id)
        if adopted_user_id is None or adopted_user_id <= 0:
            return
        for table in (
            "events",
            "tasks",
            "artifacts",
            "notifications",
            "inbox",
            "summaries",
            "telegram_reminders",
        ):
            conn.execute(
                f"UPDATE {table} SET user_id = ? WHERE user_id = 0",
                (adopted_user_id,),
            )

    def get_user_by_chat_id(self, chat_id: str) -> dict[str, Any] | None:
        chat = str(chat_id or "").strip()
        if not chat:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
                FROM users
                WHERE telegram_chat_id = ?
                LIMIT 1
                """,
                (chat,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "user_id": int(row["id"]),
            "telegram_chat_id": str(row["telegram_chat_id"] or ""),
            "timezone": str(row["timezone"] or "").strip() or None,
            "status": str(row["status"] or ""),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        owner_id = _normalize_user_id(user_id)
        if owner_id is None or owner_id <= 0:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
                FROM users
                WHERE id = ?
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "user_id": int(row["id"]),
            "telegram_chat_id": str(row["telegram_chat_id"] or ""),
            "timezone": str(row["timezone"] or "").strip() or None,
            "status": str(row["status"] or ""),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_users(
        self,
        *,
        status: str | None = "active",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
            FROM users
            WHERE 1 = 1
        """
        params: list[Any] = []
        state = str(status or "").strip().lower()
        if state:
            query += " AND status = ?"
            params.append(state)
        query += " ORDER BY updated_at DESC, id ASC LIMIT ?"
        params.append(max(int(limit), 1))
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            {
                "id": int(row["id"]),
                "user_id": int(row["id"]),
                "telegram_chat_id": str(row["telegram_chat_id"] or ""),
                "timezone": str(row["timezone"] or "").strip() or None,
                "status": str(row["status"] or ""),
                "metadata_json": _json_load(row["metadata_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def ensure_user_for_chat(
        self,
        *,
        chat_id: str,
        timezone_name: str | None = None,
        status: str = "active",
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chat = str(chat_id or "").strip()
        if not chat:
            raise ValueError("chat_id is required")
        timezone_value = str(timezone_name or "").strip() or None
        state = str(status or "active").strip().lower() or "active"
        metadata = _json_dump(metadata_json)
        ts = now_utc_iso()
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
                FROM users
                WHERE telegram_chat_id = ?
                LIMIT 1
                """,
                (chat,),
            ).fetchone()
            if row:
                merged_metadata = _json_load(row["metadata_json"])
                merged_metadata.update(metadata_json or {})
                conn.execute(
                    """
                    UPDATE users
                    SET timezone = COALESCE(?, timezone),
                        status = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        timezone_value,
                        state,
                        _json_dump(merged_metadata),
                        ts,
                        int(row["id"]),
                    ),
                )
            else:
                existing_users_row = conn.execute(
                    "SELECT COUNT(*) AS count FROM users"
                ).fetchone()
                existing_users = int(existing_users_row["count"]) if existing_users_row else 0
                conn.execute(
                    """
                    INSERT INTO users(
                        telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (chat, timezone_value, state, metadata, ts, ts),
                )
                row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                if existing_users == 0:
                    self._adopt_global_rows_for_user(conn, row_id)
            stored = conn.execute(
                """
                SELECT id, telegram_chat_id, timezone, status, metadata_json, created_at, updated_at
                FROM users
                WHERE telegram_chat_id = ?
                LIMIT 1
                """,
                (chat,),
            ).fetchone()
        if not stored:
            raise RuntimeError("failed to persist user")
        return {
            "id": int(stored["id"]),
            "user_id": int(stored["id"]),
            "telegram_chat_id": str(stored["telegram_chat_id"] or ""),
            "timezone": str(stored["timezone"] or "").strip() or None,
            "status": str(stored["status"] or ""),
            "metadata_json": _json_load(stored["metadata_json"]),
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
        }

    def _resolve_user_preference_target(
        self,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
        create_if_missing: bool = False,
        metadata_source: str = "user_preferences",
    ) -> tuple[int | None, str | None]:
        owner_id = _normalize_user_id(user_id)
        chat = str(chat_id or "").strip() or None
        if owner_id is not None and owner_id > 0:
            user = self.get_user(owner_id)
            if user is None:
                return None, chat
            resolved_chat = str(user.get("telegram_chat_id") or "").strip() or chat
            return owner_id, resolved_chat
        if not chat:
            return None, None
        user = (
            self.ensure_user_for_chat(
                chat_id=chat,
                metadata_json={"source": metadata_source},
            )
            if create_if_missing
            else self.get_user_by_chat_id(chat)
        )
        if user is None:
            return None, chat
        return int(user["id"]), str(user.get("telegram_chat_id") or chat).strip() or chat

    def _user_preferences_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": int(row["user_id"]),
            "chat_id": str(row["telegram_chat_id"] or "").strip() or None,
            "telegram_chat_allowed": _db_bool(row["telegram_chat_allowed"]),
            "material_brief_push_enabled": _db_bool(row["material_brief_push_enabled"]),
            "scheduled_briefings_enabled": _db_bool(row["scheduled_briefings_enabled"]),
            "daily_digest_enabled": _db_bool(row["daily_digest_enabled"]),
            "weather_location_label": _normalize_optional_text(row["weather_location_label"]),
            "weather_lat": _db_float(row["weather_lat"]),
            "weather_lon": _db_float(row["weather_lon"]),
            "weather_air_quality_district_code": _normalize_optional_text(
                row["weather_air_quality_district_code"]
            ),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_user_preferences(
        self,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any] | None:
        owner_id, _ = self._resolve_user_preference_target(
            user_id=user_id,
            chat_id=chat_id,
            create_if_missing=False,
        )
        if owner_id is None or owner_id <= 0:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    p.user_id,
                    u.telegram_chat_id,
                    p.telegram_chat_allowed,
                    p.material_brief_push_enabled,
                    p.scheduled_briefings_enabled,
                    p.daily_digest_enabled,
                    p.weather_location_label,
                    p.weather_lat,
                    p.weather_lon,
                    p.weather_air_quality_district_code,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM user_preferences p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = ?
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
        if not row:
            return None
        return self._user_preferences_payload(row)

    def upsert_user_preferences(
        self,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
        telegram_chat_allowed: bool | None | object = _PREFERENCE_UNSET,
        material_brief_push_enabled: bool | None | object = _PREFERENCE_UNSET,
        scheduled_briefings_enabled: bool | None | object = _PREFERENCE_UNSET,
        daily_digest_enabled: bool | None | object = _PREFERENCE_UNSET,
        weather_location_label: str | None | object = _PREFERENCE_UNSET,
        weather_lat: float | None | object = _PREFERENCE_UNSET,
        weather_lon: float | None | object = _PREFERENCE_UNSET,
        weather_air_quality_district_code: str | None | object = _PREFERENCE_UNSET,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner_id, resolved_chat = self._resolve_user_preference_target(
            user_id=user_id,
            chat_id=chat_id,
            create_if_missing=bool(chat_id),
        )
        if owner_id is None or owner_id <= 0:
            raise ValueError("user_id or chat_id is required")

        values = {
            "telegram_chat_allowed": telegram_chat_allowed,
            "material_brief_push_enabled": material_brief_push_enabled,
            "scheduled_briefings_enabled": scheduled_briefings_enabled,
            "daily_digest_enabled": daily_digest_enabled,
            "weather_location_label": (
                weather_location_label
                if weather_location_label is _PREFERENCE_UNSET
                else _normalize_optional_text(weather_location_label)
            ),
            "weather_lat": (
                weather_lat
                if weather_lat is _PREFERENCE_UNSET
                else _db_float(weather_lat)
            ),
            "weather_lon": (
                weather_lon
                if weather_lon is _PREFERENCE_UNSET
                else _db_float(weather_lon)
            ),
            "weather_air_quality_district_code": (
                weather_air_quality_district_code
                if weather_air_quality_district_code is _PREFERENCE_UNSET
                else _normalize_optional_text(weather_air_quality_district_code)
            ),
        }
        ts = now_utc_iso()
        with self.connection() as conn:
            existing = conn.execute(
                """
                SELECT
                    p.user_id,
                    u.telegram_chat_id,
                    p.telegram_chat_allowed,
                    p.material_brief_push_enabled,
                    p.scheduled_briefings_enabled,
                    p.daily_digest_enabled,
                    p.weather_location_label,
                    p.weather_lat,
                    p.weather_lon,
                    p.weather_air_quality_district_code,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM user_preferences p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = ?
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
            if existing:
                merged_metadata = _json_load(existing["metadata_json"])
                if metadata_json:
                    merged_metadata.update(metadata_json)
                assignments: list[str] = ["metadata_json = ?", "updated_at = ?"]
                params: list[Any] = [_json_dump(merged_metadata), ts]
                for field, value in values.items():
                    if value is _PREFERENCE_UNSET:
                        continue
                    assignments.append(f"{field} = ?")
                    if field in USER_PREFERENCE_FIELDS:
                        params.append(_db_optional_bool(value))
                    else:
                        params.append(value)
                params.append(owner_id)
                conn.execute(
                    f"""
                    UPDATE user_preferences
                    SET {", ".join(assignments)}
                    WHERE user_id = ?
                    """,
                    tuple(params),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_preferences(
                        user_id,
                        telegram_chat_allowed,
                        material_brief_push_enabled,
                        scheduled_briefings_enabled,
                        daily_digest_enabled,
                        weather_location_label,
                        weather_lat,
                        weather_lon,
                        weather_air_quality_district_code,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        None
                        if telegram_chat_allowed is _PREFERENCE_UNSET
                        else _db_optional_bool(telegram_chat_allowed),
                        None
                        if material_brief_push_enabled is _PREFERENCE_UNSET
                        else _db_optional_bool(material_brief_push_enabled),
                        None
                        if scheduled_briefings_enabled is _PREFERENCE_UNSET
                        else _db_optional_bool(scheduled_briefings_enabled),
                        None
                        if daily_digest_enabled is _PREFERENCE_UNSET
                        else _db_optional_bool(daily_digest_enabled),
                        None
                        if weather_location_label is _PREFERENCE_UNSET
                        else _normalize_optional_text(weather_location_label),
                        None if weather_lat is _PREFERENCE_UNSET else _db_float(weather_lat),
                        None if weather_lon is _PREFERENCE_UNSET else _db_float(weather_lon),
                        None
                        if weather_air_quality_district_code is _PREFERENCE_UNSET
                        else _normalize_optional_text(weather_air_quality_district_code),
                        _json_dump(metadata_json),
                        ts,
                        ts,
                    ),
                )
            stored = conn.execute(
                """
                SELECT
                    p.user_id,
                    u.telegram_chat_id,
                    p.telegram_chat_allowed,
                    p.material_brief_push_enabled,
                    p.scheduled_briefings_enabled,
                    p.daily_digest_enabled,
                    p.weather_location_label,
                    p.weather_lat,
                    p.weather_lon,
                    p.weather_air_quality_district_code,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM user_preferences p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = ?
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
        if not stored:
            raise RuntimeError("failed to persist user preferences")
        payload = self._user_preferences_payload(stored)
        if resolved_chat and not payload.get("chat_id"):
            payload["chat_id"] = resolved_chat
        return payload

    def list_user_preferences(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.user_id,
                    u.telegram_chat_id,
                    p.telegram_chat_allowed,
                    p.material_brief_push_enabled,
                    p.scheduled_briefings_enabled,
                    p.daily_digest_enabled,
                    p.weather_location_label,
                    p.weather_lat,
                    p.weather_lon,
                    p.weather_air_quality_district_code,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM user_preferences p
                JOIN users u ON u.id = p.user_id
                ORDER BY p.updated_at DESC, p.user_id ASC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        return [self._user_preferences_payload(row) for row in rows]

    def list_user_weather_locations(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        rows = self.list_user_preferences(limit=limit)
        return [
            row
            for row in rows
            if (
                row.get("weather_location_label")
                and row.get("weather_lat") is not None
                and row.get("weather_lon") is not None
            )
        ]

    def list_chat_ids_by_preference(
        self,
        preference: str,
        *,
        enabled: bool = True,
        limit: int = 500,
    ) -> list[str]:
        field = str(preference or "").strip()
        if field not in USER_PREFERENCE_FIELDS:
            raise ValueError(f"unknown user preference: {preference}")
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT u.telegram_chat_id
                FROM user_preferences p
                JOIN users u ON u.id = p.user_id
                WHERE p.{field} = ?
                  AND u.status = 'active'
                  AND u.telegram_chat_id IS NOT NULL
                  AND TRIM(u.telegram_chat_id) <> ''
                ORDER BY p.updated_at DESC, u.telegram_chat_id ASC
                LIMIT ?
                """,
                (_db_optional_bool(enabled), max(int(limit), 1)),
            ).fetchall()
        return [str(row["telegram_chat_id"] or "").strip() for row in rows if str(row["telegram_chat_id"] or "").strip()]

    def has_user_preference_value(self, preference: str) -> bool:
        field = str(preference or "").strip()
        if field not in USER_PREFERENCE_FIELDS:
            raise ValueError(f"unknown user preference: {preference}")
        with self.connection() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM user_preferences
                WHERE {field} IS NOT NULL
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def _notification_policy_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "chat_id": str(row["telegram_chat_id"] or "").strip() or None,
            "policy_kind": str(row["policy_kind"] or "").strip(),
            "enabled": bool(int(row["enabled"] or 0)),
            "days_of_week_json": _json_load_list(row["days_of_week_json"]),
            "time_local": _normalize_optional_text(row["time_local"]),
            "timezone": _normalize_optional_text(row["timezone"]),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_notification_policy(
        self,
        policy_kind: str,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any] | None:
        kind = str(policy_kind or "").strip().lower()
        if not kind:
            raise ValueError("policy_kind is required")
        owner_id, _ = self._resolve_user_preference_target(
            user_id=user_id,
            chat_id=chat_id,
            create_if_missing=False,
            metadata_source="notification_policy",
        )
        if owner_id is None or owner_id <= 0:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    p.id,
                    p.user_id,
                    u.telegram_chat_id,
                    p.policy_kind,
                    p.enabled,
                    p.days_of_week_json,
                    p.time_local,
                    p.timezone,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM notification_policies p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = ?
                  AND p.policy_kind = ?
                LIMIT 1
                """,
                (owner_id, kind),
            ).fetchone()
        if not row:
            return None
        return self._notification_policy_payload(row)

    def upsert_notification_policy(
        self,
        *,
        policy_kind: str,
        user_id: int | None = None,
        chat_id: str | None = None,
        enabled: bool | object = _PREFERENCE_UNSET,
        days_of_week_json: list[Any] | None | object = _PREFERENCE_UNSET,
        time_local: str | None | object = _PREFERENCE_UNSET,
        timezone: str | None | object = _PREFERENCE_UNSET,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = str(policy_kind or "").strip().lower()
        if not kind:
            raise ValueError("policy_kind is required")
        owner_id, resolved_chat = self._resolve_user_preference_target(
            user_id=user_id,
            chat_id=chat_id,
            create_if_missing=bool(chat_id),
            metadata_source="notification_policy",
        )
        if owner_id is None or owner_id <= 0:
            raise ValueError("user_id or chat_id is required")

        values = {
            "enabled": enabled,
            "days_of_week_json": (
                days_of_week_json
                if days_of_week_json is _PREFERENCE_UNSET
                else _json_dump_list(_normalize_json_list_value(days_of_week_json))
            ),
            "time_local": (
                time_local
                if time_local is _PREFERENCE_UNSET
                else _normalize_optional_text(time_local)
            ),
            "timezone": (
                timezone
                if timezone is _PREFERENCE_UNSET
                else _normalize_optional_text(timezone)
            ),
        }
        ts = now_utc_iso()
        with self.connection() as conn:
            existing = conn.execute(
                """
                SELECT
                    p.id,
                    p.user_id,
                    u.telegram_chat_id,
                    p.policy_kind,
                    p.enabled,
                    p.days_of_week_json,
                    p.time_local,
                    p.timezone,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM notification_policies p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = ?
                  AND p.policy_kind = ?
                LIMIT 1
                """,
                (owner_id, kind),
            ).fetchone()
            if existing:
                merged_metadata = _json_load(existing["metadata_json"])
                if metadata_json:
                    merged_metadata.update(metadata_json)
                assignments: list[str] = ["metadata_json = ?", "updated_at = ?"]
                params: list[Any] = [_json_dump(merged_metadata), ts]
                for field, value in values.items():
                    if value is _PREFERENCE_UNSET:
                        continue
                    assignments.append(f"{field} = ?")
                    if field == "enabled":
                        params.append(_db_optional_bool(value))
                    else:
                        params.append(value)
                params.extend([owner_id, kind])
                conn.execute(
                    f"""
                    UPDATE notification_policies
                    SET {", ".join(assignments)}
                    WHERE user_id = ?
                      AND policy_kind = ?
                    """,
                    tuple(params),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO notification_policies(
                        user_id,
                        policy_kind,
                        enabled,
                        days_of_week_json,
                        time_local,
                        timezone,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        kind,
                        _db_optional_bool(False if enabled is _PREFERENCE_UNSET else enabled),
                        (
                            _json_dump_list([])
                            if days_of_week_json is _PREFERENCE_UNSET
                            else _json_dump_list(
                                _normalize_json_list_value(days_of_week_json)
                            )
                        ),
                        None
                        if time_local is _PREFERENCE_UNSET
                        else _normalize_optional_text(time_local),
                        None
                        if timezone is _PREFERENCE_UNSET
                        else _normalize_optional_text(timezone),
                        _json_dump(metadata_json),
                        ts,
                        ts,
                    ),
                )
            stored = conn.execute(
                """
                SELECT
                    p.id,
                    p.user_id,
                    u.telegram_chat_id,
                    p.policy_kind,
                    p.enabled,
                    p.days_of_week_json,
                    p.time_local,
                    p.timezone,
                    p.metadata_json,
                    p.created_at,
                    p.updated_at
                FROM notification_policies p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = ?
                  AND p.policy_kind = ?
                LIMIT 1
                """,
                (owner_id, kind),
            ).fetchone()
        if not stored:
            raise RuntimeError("failed to persist notification policy")
        payload = self._notification_policy_payload(stored)
        if resolved_chat and not payload.get("chat_id"):
            payload["chat_id"] = resolved_chat
        return payload

    def list_notification_policies(
        self,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
        policy_kind: str | None = None,
        enabled: bool | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        owner_id: int | None = None
        if user_id is not None or str(chat_id or "").strip():
            owner_id, _ = self._resolve_user_preference_target(
                user_id=user_id,
                chat_id=chat_id,
                create_if_missing=False,
                metadata_source="notification_policy",
            )
            if owner_id is None or owner_id <= 0:
                return []
        kind = str(policy_kind or "").strip().lower() or None
        query = """
            SELECT
                p.id,
                p.user_id,
                u.telegram_chat_id,
                p.policy_kind,
                p.enabled,
                p.days_of_week_json,
                p.time_local,
                p.timezone,
                p.metadata_json,
                p.created_at,
                p.updated_at
            FROM notification_policies p
            JOIN users u ON u.id = p.user_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if owner_id is not None:
            query += " AND p.user_id = ?"
            params.append(owner_id)
        if kind:
            query += " AND p.policy_kind = ?"
            params.append(kind)
        if enabled is not None:
            query += " AND p.enabled = ?"
            params.append(_db_optional_bool(enabled))
        query += " ORDER BY p.updated_at DESC, p.id ASC LIMIT ?"
        params.append(max(int(limit), 1))
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._notification_policy_payload(row) for row in rows]

    def delete_notification_policy(
        self,
        policy_kind: str,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
    ) -> bool:
        kind = str(policy_kind or "").strip().lower()
        if not kind:
            raise ValueError("policy_kind is required")
        owner_id, _ = self._resolve_user_preference_target(
            user_id=user_id,
            chat_id=chat_id,
            create_if_missing=False,
            metadata_source="notification_policy",
        )
        if owner_id is None or owner_id <= 0:
            return False
        with self.connection() as conn:
            result = conn.execute(
                """
                DELETE FROM notification_policies
                WHERE user_id = ?
                  AND policy_kind = ?
                """,
                (owner_id, kind),
            )
        return bool(result.rowcount)

    def _assistant_run_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": _normalize_user_id(row["user_id"]),
            "chat_id": str(row["chat_id"] or "").strip() or None,
            "request_raw": str(row["request_raw"] or ""),
            "context_json": _json_load(row["context_json"]),
            "planner_output_json": _json_load(row["planner_output_json"]),
            "executor_result_json": _json_load(row["executor_result_json"]),
            "final_reply": row["final_reply"],
            "status": str(row["status"] or "").strip() or "pending",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_assistant_run(
        self,
        *,
        request_raw: str,
        user_id: int | None = None,
        chat_id: str | None = None,
        context_json: dict[str, Any] | None = None,
        planner_output_json: dict[str, Any] | None = None,
        executor_result_json: dict[str, Any] | None = None,
        final_reply: str | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        raw_request = "" if request_raw is None else str(request_raw)
        if not raw_request.strip():
            raise ValueError("request_raw is required")
        owner_id, resolved_chat = self._resolve_user_preference_target(
            user_id=user_id,
            chat_id=chat_id,
            create_if_missing=bool(chat_id),
            metadata_source="assistant_run",
        )
        if owner_id is None and not str(chat_id or "").strip():
            raise ValueError("user_id or chat_id is required")
        ts = now_utc_iso()
        status_value = str(status or "").strip().lower() or "pending"
        stored_chat = resolved_chat or (str(chat_id or "").strip() or None)
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO assistant_runs(
                    user_id,
                    chat_id,
                    request_raw,
                    context_json,
                    planner_output_json,
                    executor_result_json,
                    final_reply,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    stored_chat,
                    raw_request,
                    _json_dump(context_json),
                    _json_dump(planner_output_json),
                    _json_dump(executor_result_json),
                    None if final_reply is None else str(final_reply),
                    status_value,
                    ts,
                    ts,
                ),
            )
            row = conn.execute(
                """
                SELECT
                    id,
                    user_id,
                    chat_id,
                    request_raw,
                    context_json,
                    planner_output_json,
                    executor_result_json,
                    final_reply,
                    status,
                    created_at,
                    updated_at
                FROM assistant_runs
                WHERE id = ?
                LIMIT 1
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
        if not row:
            raise RuntimeError("failed to persist assistant run")
        return self._assistant_run_payload(row)

    def get_assistant_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    user_id,
                    chat_id,
                    request_raw,
                    context_json,
                    planner_output_json,
                    executor_result_json,
                    final_reply,
                    status,
                    created_at,
                    updated_at
                FROM assistant_runs
                WHERE id = ?
                LIMIT 1
                """,
                (int(run_id),),
            ).fetchone()
        if not row:
            return None
        return self._assistant_run_payload(row)

    def update_assistant_run(
        self,
        run_id: int,
        *,
        context_json: dict[str, Any] | None | object = _PREFERENCE_UNSET,
        planner_output_json: dict[str, Any] | None | object = _PREFERENCE_UNSET,
        executor_result_json: dict[str, Any] | None | object = _PREFERENCE_UNSET,
        final_reply: str | None | object = _PREFERENCE_UNSET,
        status: str | None | object = _PREFERENCE_UNSET,
    ) -> dict[str, Any] | None:
        assignments: list[str] = ["updated_at = ?"]
        params: list[Any] = [now_utc_iso()]
        if context_json is not _PREFERENCE_UNSET:
            assignments.append("context_json = ?")
            params.append(_json_dump(context_json))
        if planner_output_json is not _PREFERENCE_UNSET:
            assignments.append("planner_output_json = ?")
            params.append(_json_dump(planner_output_json))
        if executor_result_json is not _PREFERENCE_UNSET:
            assignments.append("executor_result_json = ?")
            params.append(_json_dump(executor_result_json))
        if final_reply is not _PREFERENCE_UNSET:
            assignments.append("final_reply = ?")
            params.append(None if final_reply is None else str(final_reply))
        if status is not _PREFERENCE_UNSET:
            assignments.append("status = ?")
            params.append(str(status or "").strip().lower() or "pending")
        params.append(int(run_id))
        with self.connection() as conn:
            result = conn.execute(
                f"""
                UPDATE assistant_runs
                SET {", ".join(assignments)}
                WHERE id = ?
                """,
                tuple(params),
            )
            if not result.rowcount:
                return None
            row = conn.execute(
                """
                SELECT
                    id,
                    user_id,
                    chat_id,
                    request_raw,
                    context_json,
                    planner_output_json,
                    executor_result_json,
                    final_reply,
                    status,
                    created_at,
                    updated_at
                FROM assistant_runs
                WHERE id = ?
                LIMIT 1
                """,
                (int(run_id),),
            ).fetchone()
        if not row:
            return None
        return self._assistant_run_payload(row)

    def list_assistant_runs(
        self,
        *,
        user_id: int | None = None,
        chat_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                id,
                user_id,
                chat_id,
                request_raw,
                context_json,
                planner_output_json,
                executor_result_json,
                final_reply,
                status,
                created_at,
                updated_at
            FROM assistant_runs
            WHERE 1 = 1
        """
        params: list[Any] = []
        owner_id = _normalize_user_id(user_id)
        if owner_id is not None:
            query += " AND user_id = ?"
            params.append(owner_id)
        normalized_chat = str(chat_id or "").strip()
        if normalized_chat:
            query += " AND chat_id = ?"
            params.append(normalized_chat)
        status_value = str(status or "").strip().lower()
        if status_value:
            query += " AND status = ?"
            params.append(status_value)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 1))
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._assistant_run_payload(row) for row in rows]

    def upsert_school(
        self,
        *,
        school_slug: str,
        display_name: str,
        status: str = "active",
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        slug = str(school_slug or "").strip().lower()
        if not slug:
            raise ValueError("school_slug is required")
        display = str(display_name or "").strip() or slug
        state = str(status or "active").strip().lower() or "active"
        ts = now_utc_iso()
        metadata = _json_dump(metadata_json)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO schools(
                    school_slug, display_name, status, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(school_slug)
                DO UPDATE SET
                    display_name = excluded.display_name,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (slug, display, state, metadata, ts, ts),
            )
            row = conn.execute(
                """
                SELECT school_id, school_slug, display_name, status, metadata_json, created_at, updated_at
                FROM schools
                WHERE school_slug = ?
                LIMIT 1
                """,
                (slug,),
            ).fetchone()
        if not row:
            raise RuntimeError("failed to persist school")
        return {
            "school_id": int(row["school_id"]),
            "school_slug": str(row["school_slug"] or ""),
            "display_name": str(row["display_name"] or ""),
            "status": str(row["status"] or ""),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_school_by_slug(self, school_slug: str) -> dict[str, Any] | None:
        slug = str(school_slug or "").strip().lower()
        if not slug:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT school_id, school_slug, display_name, status, metadata_json, created_at, updated_at
                FROM schools
                WHERE school_slug = ?
                LIMIT 1
                """,
                (slug,),
            ).fetchone()
        if not row:
            return None
        return {
            "school_id": int(row["school_id"]),
            "school_slug": str(row["school_slug"] or ""),
            "display_name": str(row["display_name"] or ""),
            "status": str(row["status"] or ""),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_schools(
        self,
        *,
        status: str | None = "active",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT school_id, school_slug, display_name, status, metadata_json, created_at, updated_at
            FROM schools
            WHERE 1 = 1
        """
        params: list[Any] = []
        state = str(status or "").strip().lower()
        if state:
            query += " AND status = ?"
            params.append(state)
        query += " ORDER BY display_name COLLATE NOCASE ASC, school_slug ASC LIMIT ?"
        params.append(max(int(limit), 1))
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            {
                "school_id": int(row["school_id"]),
                "school_slug": str(row["school_slug"] or ""),
                "display_name": str(row["display_name"] or ""),
                "status": str(row["status"] or ""),
                "metadata_json": _json_load(row["metadata_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def upsert_course(
        self,
        *,
        canonical_course_id: str,
        source: str,
        external_course_id: str | int | None,
        display_name: str,
        metadata_json: dict[str, Any] | None,
        user_id: int | None = 0,
    ) -> Course:
        course_id = str(canonical_course_id or "").strip()
        if not course_id:
            raise ValueError("canonical_course_id is required")
        source_name = str(source or "").strip() or "unknown"
        display = str(display_name or "").strip() or course_id
        external_value = str(external_course_id).strip() if external_course_id not in (None, "") else None
        metadata = _json_dump(metadata_json)
        ts = now_utc_iso()
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO courses(
                    user_id, canonical_course_id, source, external_course_id, display_name,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, canonical_course_id)
                DO UPDATE SET
                    source = excluded.source,
                    external_course_id = excluded.external_course_id,
                    display_name = excluded.display_name,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    course_id,
                    source_name,
                    external_value,
                    display,
                    metadata,
                    ts,
                    ts,
                ),
            )
        return Course(
            canonical_course_id=course_id,
            source=source_name,
            external_course_id=external_value,
            display_name=display,
            metadata_json=metadata,
            user_id=owner_id,
        )

    def get_course(self, canonical_course_id: str, *, user_id: int | None = 0) -> Course | None:
        course_id = str(canonical_course_id or "").strip()
        if not course_id:
            return None
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                row = conn.execute(
                    """
                    SELECT user_id, canonical_course_id, source, external_course_id, display_name, metadata_json
                    FROM courses
                    WHERE canonical_course_id = ?
                    ORDER BY user_id ASC
                    LIMIT 1
                    """,
                    (course_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT user_id, canonical_course_id, source, external_course_id, display_name, metadata_json
                    FROM courses
                    WHERE canonical_course_id = ?
                      AND user_id = ?
                    LIMIT 1
                    """,
                    (course_id, owner_id),
                ).fetchone()
        if not row:
            return None
        return Course(
            canonical_course_id=row["canonical_course_id"],
            source=row["source"],
            external_course_id=row["external_course_id"],
            display_name=row["display_name"],
            metadata_json=row["metadata_json"],
            user_id=int(row["user_id"]),
        )

    def list_courses(self, limit: int = 500, *, user_id: int | None = 0) -> list[Course]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, canonical_course_id, source, external_course_id, display_name, metadata_json
                    FROM courses
                    ORDER BY display_name COLLATE NOCASE ASC, canonical_course_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, canonical_course_id, source, external_course_id, display_name, metadata_json
                    FROM courses
                    WHERE user_id = ?
                    ORDER BY display_name COLLATE NOCASE ASC, canonical_course_id ASC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        return [
            Course(
                canonical_course_id=row["canonical_course_id"],
                source=row["source"],
                external_course_id=row["external_course_id"],
                display_name=row["display_name"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def upsert_course_alias(
        self,
        *,
        canonical_course_id: str,
        alias: str,
        alias_type: str,
        source: str,
        metadata_json: dict[str, Any] | None,
        user_id: int | None = 0,
    ) -> CourseAlias | None:
        course_id = str(canonical_course_id or "").strip()
        alias_value = str(alias or "").strip()
        if not course_id or not alias_value:
            return None
        normalized_alias = normalize_course_alias(alias_value)
        if not normalized_alias:
            return None
        alias_kind = str(alias_type or "").strip() or "alias"
        source_name = str(source or "").strip() or "unknown"
        metadata = _json_dump(metadata_json)
        ts = now_utc_iso()
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO course_aliases(
                    user_id, canonical_course_id, alias, normalized_alias, alias_type, source,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, canonical_course_id, normalized_alias, alias_type, source)
                DO UPDATE SET
                    alias = excluded.alias,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    course_id,
                    alias_value,
                    normalized_alias,
                    alias_kind,
                    source_name,
                    metadata,
                    ts,
                    ts,
                ),
            )
            row = conn.execute(
                """
                SELECT id, user_id, canonical_course_id, alias, normalized_alias, alias_type, source, metadata_json
                FROM course_aliases
                WHERE user_id = ?
                  AND canonical_course_id = ?
                  AND normalized_alias = ?
                  AND alias_type = ?
                  AND source = ?
                LIMIT 1
                """,
                (owner_id, course_id, normalized_alias, alias_kind, source_name),
            ).fetchone()
        if not row:
            return None
        return CourseAlias(
            id=int(row["id"]),
            canonical_course_id=row["canonical_course_id"],
            alias=row["alias"],
            normalized_alias=row["normalized_alias"],
            alias_type=row["alias_type"],
            source=row["source"],
            metadata_json=row["metadata_json"],
            user_id=int(row["user_id"]),
        )

    def list_course_aliases(
        self,
        *,
        canonical_course_id: str | None = None,
        normalized_alias: str | None = None,
        limit: int = 1000,
        user_id: int | None = 0,
    ) -> list[CourseAlias]:
        query = """
            SELECT id, user_id, canonical_course_id, alias, normalized_alias, alias_type, source, metadata_json
            FROM course_aliases
        """
        conditions: list[str] = []
        params: list[Any] = []
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        if owner_id is not None:
            conditions.append("user_id = ?")
            params.append(owner_id)
        course_id = str(canonical_course_id or "").strip()
        alias_key = str(normalized_alias or "").strip()
        if course_id:
            conditions.append("canonical_course_id = ?")
            params.append(course_id)
        if alias_key:
            conditions.append("normalized_alias = ?")
            params.append(alias_key)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY alias COLLATE NOCASE ASC, id ASC LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            CourseAlias(
                id=int(row["id"]),
                canonical_course_id=row["canonical_course_id"],
                alias=row["alias"],
                normalized_alias=row["normalized_alias"],
                alias_type=row["alias_type"],
                source=row["source"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def delete_course_alias(
        self,
        *,
        canonical_course_id: str,
        alias: str,
        alias_type: str | None = None,
        source: str | None = None,
        user_id: int | None = 0,
    ) -> int:
        course_id = str(canonical_course_id or "").strip()
        normalized_alias = normalize_course_alias(alias)
        if not course_id or not normalized_alias:
            return 0
        query = """
            DELETE FROM course_aliases
            WHERE canonical_course_id = ?
              AND normalized_alias = ?
        """
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        params: list[Any] = [course_id, normalized_alias]
        if owner_id is not None:
            query += " AND user_id = ?"
            params.append(owner_id)
        alias_kind = str(alias_type or "").strip()
        source_name = str(source or "").strip()
        if alias_kind:
            query += " AND alias_type = ?"
            params.append(alias_kind)
        if source_name:
            query += " AND source = ?"
            params.append(source_name)
        with self.connection() as conn:
            cursor = conn.execute(query, tuple(params))
            deleted = int(cursor.rowcount or 0)
        return deleted

    def course_alias_resolution_map(
        self,
        *,
        user_id: int | None = 0,
    ) -> dict[str, tuple[str, ...]]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT normalized_alias, canonical_course_id
                    FROM course_aliases
                    ORDER BY normalized_alias ASC, canonical_course_id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT normalized_alias, canonical_course_id
                    FROM course_aliases
                    WHERE user_id = ?
                    ORDER BY normalized_alias ASC, canonical_course_id ASC
                    """,
                    (owner_id,),
                ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            normalized_alias = str(row["normalized_alias"] or "").strip()
            canonical_course_id = str(row["canonical_course_id"] or "").strip()
            if not normalized_alias or not canonical_course_id:
                continue
            bucket = grouped.setdefault(normalized_alias, [])
            if canonical_course_id not in bucket:
                bucket.append(canonical_course_id)
        return {key: tuple(value) for key, value in grouped.items()}

    def resolve_course_alias(self, alias: str | None, *, user_id: int | None = 0) -> str | None:
        normalized_alias = normalize_course_alias(alias)
        if not normalized_alias:
            return None
        matches = self.course_alias_resolution_map(user_id=user_id).get(normalized_alias, ())
        if len(matches) != 1:
            return None
        return matches[0]

    def upsert_event(
        self,
        external_id: str,
        source: str,
        start: str | datetime,
        end: str | datetime,
        title: str,
        location: str | None,
        rrule: str | None,
        metadata_json: dict[str, Any] | None,
        user_id: int | None = 0,
    ) -> Event:
        ts = now_utc_iso()
        start_at = normalize_datetime(start)
        end_at = normalize_datetime(end)
        if not start_at or not end_at:
            raise ValueError("event start/end are required")
        metadata = _json_dump(metadata_json)
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO events(
                    user_id, external_id, source, start_at, end_at, title, location, rrule,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id, source)
                DO UPDATE SET
                    start_at = excluded.start_at,
                    end_at = excluded.end_at,
                    title = excluded.title,
                    location = excluded.location,
                    rrule = excluded.rrule,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external_id,
                    source,
                    start_at,
                    end_at,
                    title,
                    location,
                    rrule,
                    metadata,
                    ts,
                    ts,
                ),
            )
        return Event(
            external_id=external_id,
            source=source,
            start_at=start_at,
            end_at=end_at,
            title=title,
            location=location,
            rrule=rrule,
            metadata_json=metadata,
            user_id=owner_id,
        )

    def upsert_task(
        self,
        external_id: str,
        source: str,
        due_at: str | datetime | None,
        title: str,
        status: str,
        metadata_json: dict[str, Any] | None,
        user_id: int | None = 0,
    ) -> Task:
        ts = now_utc_iso()
        due_at_norm = normalize_datetime(due_at)
        canonical_status = canonical_task_status(status)
        metadata = _json_dump(metadata_json)
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    user_id, external_id, source, due_at, title, status, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id, source)
                DO UPDATE SET
                    due_at = excluded.due_at,
                    title = excluded.title,
                    status = CASE
                        WHEN tasks.status IN ('done', 'ignored') AND excluded.status = 'open' THEN tasks.status
                        ELSE excluded.status
                    END,
                    metadata_json = CASE
                        WHEN tasks.status IN ('done', 'ignored') AND excluded.status = 'open' THEN tasks.metadata_json
                        ELSE excluded.metadata_json
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external_id,
                    source,
                    due_at_norm,
                    title,
                    canonical_status,
                    metadata,
                    ts,
                    ts,
                ),
            )
        return Task(
            external_id=external_id,
            source=source,
            due_at=due_at_norm,
            title=title,
            status=canonical_status,
            metadata_json=metadata,
            user_id=owner_id,
        )

    def record_artifact(
        self,
        external_id: str,
        source: str,
        filename: str,
        icloud_path: str | None,
        content_hash: str | None,
        metadata_json: dict[str, Any] | None,
        user_id: int | None = 0,
    ) -> Artifact:
        ts = now_utc_iso()
        metadata = _json_dump(metadata_json)
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO artifacts(
                    user_id, external_id, source, filename, icloud_path, content_hash,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id, source)
                DO UPDATE SET
                    filename = excluded.filename,
                    icloud_path = excluded.icloud_path,
                    content_hash = excluded.content_hash,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external_id,
                    source,
                    filename,
                    icloud_path,
                    content_hash,
                    metadata,
                    ts,
                    ts,
                ),
            )
        return Artifact(
            external_id=external_id,
            source=source,
            filename=filename,
            icloud_path=icloud_path,
            content_hash=content_hash,
            metadata_json=metadata,
            updated_at=ts,
            user_id=owner_id,
        )

    def get_artifact(
        self,
        external_id: str,
        source: str,
        *,
        user_id: int | None = 0,
    ) -> Artifact | None:
        owner_id = _normalize_user_id(user_id, default=0)
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT user_id, external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                FROM artifacts
                WHERE external_id = ? AND source = ? AND user_id = ?
                """,
                (external_id, source, owner_id),
            ).fetchone()
        if not row:
            return None
        return Artifact(
            external_id=row["external_id"],
            source=row["source"],
            filename=row["filename"],
            icloud_path=row["icloud_path"],
            content_hash=row["content_hash"],
            metadata_json=row["metadata_json"],
            updated_at=row["updated_at"],
            user_id=int(row["user_id"]),
        )

    def upsert_notification(
        self,
        external_id: str,
        source: str,
        created_at: str | datetime,
        title: str,
        body: str | None,
        url: str | None,
        metadata_json: dict[str, Any] | None,
        user_id: int | None = 0,
    ) -> Notification:
        ts = now_utc_iso()
        created_at_norm = normalize_datetime(created_at)
        if not created_at_norm:
            created_at_norm = ts
        metadata = _json_dump(metadata_json)
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO notifications(
                    user_id, external_id, source, created_at, title, body, url,
                    metadata_json, ingested_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id, source)
                DO UPDATE SET
                    created_at = excluded.created_at,
                    title = excluded.title,
                    body = excluded.body,
                    url = excluded.url,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external_id,
                    source,
                    created_at_norm,
                    title,
                    body,
                    url,
                    metadata,
                    ts,
                    ts,
                ),
            )
        return Notification(
            external_id=external_id,
            source=source,
            created_at=created_at_norm,
            title=title,
            body=body,
            url=url,
            metadata_json=metadata,
            user_id=owner_id,
        )

    def list_notifications(
        self,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[Notification]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, created_at, title, body, url, metadata_json
                    FROM notifications
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, created_at, title, body, url, metadata_json
                    FROM notifications
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        return [
            Notification(
                external_id=row["external_id"],
                source=row["source"],
                created_at=row["created_at"],
                title=row["title"],
                body=row["body"],
                url=row["url"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def list_notifications_since(
        self,
        since_iso: str,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[Notification]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, created_at, title, body, url, metadata_json
                    FROM notifications
                    WHERE created_at > ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (since_iso, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, created_at, title, body, url, metadata_json
                    FROM notifications
                    WHERE created_at > ?
                      AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (since_iso, owner_id, limit),
                ).fetchall()
        return [
            Notification(
                external_id=row["external_id"],
                source=row["source"],
                created_at=row["created_at"],
                title=row["title"],
                body=row["body"],
                url=row["url"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def list_artifacts(
        self,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[Artifact]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                    FROM artifacts
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                    FROM artifacts
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        return [
            Artifact(
                external_id=row["external_id"],
                source=row["source"],
                filename=row["filename"],
                icloud_path=row["icloud_path"],
                content_hash=row["content_hash"],
                metadata_json=row["metadata_json"],
                updated_at=row["updated_at"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def list_artifacts_since(
        self,
        since_iso: str,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[Artifact]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                    FROM artifacts
                    WHERE updated_at > ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (since_iso, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, filename, icloud_path, content_hash, metadata_json, updated_at
                    FROM artifacts
                    WHERE updated_at > ?
                      AND user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (since_iso, owner_id, limit),
                ).fetchall()
        return [
            Artifact(
                external_id=row["external_id"],
                source=row["source"],
                filename=row["filename"],
                icloud_path=row["icloud_path"],
                content_hash=row["content_hash"],
                metadata_json=row["metadata_json"],
                updated_at=row["updated_at"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def upsert_inbox_item(
        self,
        external_id: str,
        source: str,
        received_at: str | datetime,
        title: str,
        body: str | None,
        item_type: str,
        draft_json: dict[str, Any] | None = None,
        processed: bool = False,
        metadata_json: dict[str, Any] | None = None,
        user_id: int | None = 0,
    ) -> InboxItem:
        ts = now_utc_iso()
        received_at_norm = normalize_datetime(received_at) or ts
        draft = _json_dump(draft_json)
        processed_value = 1 if processed else 0
        owner_id = _normalize_user_id(user_id, default=0) or 0
        metadata_payload = dict(metadata_json or {})
        if owner_id == 0:
            chat_id = str(metadata_payload.get("chat_id") or "").strip()
            if chat_id:
                user = self.ensure_user_for_chat(
                    chat_id=chat_id,
                    metadata_json={"source": "inbox_item"},
                )
                owner_id = int(user["id"])
        metadata = _json_dump(metadata_payload)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO inbox(
                    user_id, external_id, source, received_at, title, body, item_type, draft_json,
                    processed, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id, source)
                DO UPDATE SET
                    received_at = excluded.received_at,
                    title = excluded.title,
                    body = excluded.body,
                    item_type = excluded.item_type,
                    draft_json = excluded.draft_json,
                    processed = excluded.processed,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external_id,
                    source,
                    received_at_norm,
                    title,
                    body,
                    item_type,
                    draft,
                    processed_value,
                    metadata,
                    ts,
                    ts,
                ),
            )
        return InboxItem(
            external_id=external_id,
            source=source,
            received_at=received_at_norm,
            title=title,
            body=body,
            item_type=item_type,
            draft_json=draft,
            processed=processed,
            metadata_json=metadata,
            user_id=owner_id,
        )

    def list_inbox(
        self,
        processed: bool | None = None,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[InboxItem]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if processed is None:
                if owner_id is None:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, source, received_at, title, body, item_type,
                               draft_json, processed, metadata_json
                        FROM inbox
                        ORDER BY received_at DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, source, received_at, title, body, item_type,
                               draft_json, processed, metadata_json
                        FROM inbox
                        WHERE user_id = ?
                        ORDER BY received_at DESC
                        LIMIT ?
                        """,
                        (owner_id, limit),
                    ).fetchall()
            else:
                if owner_id is None:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, source, received_at, title, body, item_type,
                               draft_json, processed, metadata_json
                        FROM inbox
                        WHERE processed = ?
                        ORDER BY received_at DESC
                        LIMIT ?
                        """,
                        (1 if processed else 0, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, source, received_at, title, body, item_type,
                               draft_json, processed, metadata_json
                        FROM inbox
                        WHERE processed = ?
                          AND user_id = ?
                        ORDER BY received_at DESC
                        LIMIT ?
                        """,
                        (1 if processed else 0, owner_id, limit),
                    ).fetchall()
        return [
            InboxItem(
                external_id=row["external_id"],
                source=row["source"],
                received_at=row["received_at"],
                title=row["title"],
                body=row["body"],
                item_type=row["item_type"],
                draft_json=row["draft_json"],
                processed=bool(row["processed"]),
                metadata_json=row["metadata_json"],
                id=int(row["id"]),
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def list_unprocessed_inbox(
        self,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[InboxItem]:
        return self.list_inbox(processed=False, limit=limit, user_id=user_id)

    def list_unprocessed_inbox_commands(
        self,
        limit: int = 200,
        *,
        user_id: int | None = None,
    ) -> list[InboxItem]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, received_at, title, body, item_type,
                           draft_json, processed, metadata_json
                    FROM inbox
                    WHERE processed = 0
                      AND item_type = 'command'
                    ORDER BY received_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, received_at, title, body, item_type,
                           draft_json, processed, metadata_json
                    FROM inbox
                    WHERE processed = 0
                      AND item_type = 'command'
                      AND user_id = ?
                    ORDER BY received_at ASC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        return [
            InboxItem(
                external_id=row["external_id"],
                source=row["source"],
                received_at=row["received_at"],
                title=row["title"],
                body=row["body"],
                item_type=row["item_type"],
                draft_json=row["draft_json"],
                processed=bool(row["processed"]),
                metadata_json=row["metadata_json"],
                id=int(row["id"]),
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def get_inbox_item_by_id(
        self,
        item_id: int,
        *,
        user_id: int | None = None,
    ) -> InboxItem | None:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                row = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, received_at, title, body, item_type, draft_json,
                           processed, metadata_json
                    FROM inbox
                    WHERE id = ?
                    """,
                    (int(item_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, received_at, title, body, item_type, draft_json,
                           processed, metadata_json
                    FROM inbox
                    WHERE id = ?
                      AND user_id = ?
                    """,
                    (int(item_id), owner_id),
                ).fetchone()
        if not row:
            return None
        return InboxItem(
            external_id=row["external_id"],
            source=row["source"],
            received_at=row["received_at"],
            title=row["title"],
            body=row["body"],
            item_type=row["item_type"],
            draft_json=row["draft_json"],
            processed=bool(row["processed"]),
            metadata_json=row["metadata_json"],
            id=int(row["id"]),
            user_id=int(row["user_id"]),
        )

    def mark_inbox_processed(
        self,
        external_id: str,
        source: str,
        *,
        user_id: int | None = 0,
    ) -> None:
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE inbox
                SET processed = 1, updated_at = ?
                WHERE external_id = ? AND source = ? AND user_id = ?
                """,
                (now_utc_iso(), external_id, source, owner_id),
            )

    def mark_inbox_processed_by_id(
        self,
        item_id: int,
        *,
        user_id: int | None = None,
    ) -> bool:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                row = conn.execute(
                    """
                    UPDATE inbox
                    SET processed = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_utc_iso(), int(item_id)),
                )
            else:
                row = conn.execute(
                    """
                    UPDATE inbox
                    SET processed = 1, updated_at = ?
                    WHERE id = ?
                      AND user_id = ?
                    """,
                    (now_utc_iso(), int(item_id), owner_id),
                )
            return row.rowcount > 0

    def mark_inbox_ignored_by_id(
        self,
        item_id: int,
        *,
        user_id: int | None = None,
    ) -> bool:
        return self.mark_inbox_processed_by_id(item_id, user_id=user_id)

    def record_summary(
        self,
        external_id: str,
        source: str,
        created_at: str | datetime,
        title: str,
        body: str,
        action_item: str | None,
        metadata_json: dict[str, Any] | None = None,
        user_id: int | None = 0,
    ) -> Summary:
        ts = now_utc_iso()
        created = normalize_datetime(created_at) or ts
        metadata = _json_dump(metadata_json)
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO summaries(
                    user_id, external_id, source, created_at, title, body, action_item, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id, source)
                DO UPDATE SET
                    created_at = excluded.created_at,
                    title = excluded.title,
                    body = excluded.body,
                    action_item = excluded.action_item,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external_id,
                    source,
                    created,
                    title,
                    body,
                    action_item,
                    metadata,
                    ts,
                ),
            )
        return Summary(
            external_id=external_id,
            source=source,
            created_at=created,
            title=title,
            body=body,
            action_item=action_item,
            metadata_json=metadata,
            user_id=owner_id,
        )

    def has_summary(
        self,
        external_id: str,
        source: str = "llm",
        *,
        user_id: int | None = 0,
    ) -> bool:
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT 1 AS ok
                FROM summaries
                WHERE external_id = ? AND source = ? AND user_id = ?
                """,
                (external_id, source, owner_id),
            ).fetchone()
        return bool(row)

    def list_recent_summaries(
        self,
        limit: int = 20,
        *,
        user_id: int | None = 0,
    ) -> list[Summary]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, created_at, title, body, action_item, metadata_json
                    FROM summaries
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, created_at, title, body, action_item, metadata_json
                    FROM summaries
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        return [
            Summary(
                external_id=row["external_id"],
                source=row["source"],
                created_at=row["created_at"],
                title=row["title"],
                body=row["body"],
                action_item=row["action_item"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def get_sync_state(self, job_name: str, *, user_id: int | None = 0) -> SyncState:
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            if owner_id > 0:
                row = conn.execute(
                    """
                    SELECT job_name, last_run_at, last_cursor_json
                    FROM user_sync_state
                    WHERE user_id = ?
                      AND job_name = ?
                    """,
                    (owner_id, job_name),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT job_name, last_run_at, last_cursor_json
                    FROM sync_state
                    WHERE job_name = ?
                    """,
                    (job_name,),
                ).fetchone()
        if not row:
            return SyncState(job_name=job_name, last_run_at=None, last_cursor_json=None)
        return SyncState(
            job_name=row["job_name"],
            last_run_at=row["last_run_at"],
            last_cursor_json=row["last_cursor_json"],
        )

    def update_sync_state(
        self,
        job_name: str,
        last_run_at: str | None = None,
        last_cursor_json: dict[str, Any] | None = None,
        *,
        user_id: int | None = 0,
    ) -> SyncState:
        ts = now_utc_iso()
        run_at = last_run_at or ts
        cursor_json = _json_dump(last_cursor_json) if last_cursor_json else None
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            if owner_id > 0:
                conn.execute(
                    """
                    INSERT INTO user_sync_state(user_id, job_name, last_run_at, last_cursor_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, job_name)
                    DO UPDATE SET
                        last_run_at = excluded.last_run_at,
                        last_cursor_json = excluded.last_cursor_json,
                        updated_at = excluded.updated_at
                    """,
                    (owner_id, job_name, run_at, cursor_json, ts),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO sync_state(job_name, last_run_at, last_cursor_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(job_name)
                    DO UPDATE SET
                        last_run_at = excluded.last_run_at,
                        last_cursor_json = excluded.last_cursor_json,
                        updated_at = excluded.updated_at
                    """,
                    (job_name, run_at, cursor_json, ts),
                )
        return SyncState(job_name=job_name, last_run_at=run_at, last_cursor_json=cursor_json)

    def list_sync_states(self, *, user_id: int | None = 0) -> list[SyncState]:
        owner_id = _normalize_user_id(user_id, default=0) or 0
        with self.connection() as conn:
            if owner_id > 0:
                rows = conn.execute(
                    """
                    SELECT job_name, last_run_at, last_cursor_json
                    FROM user_sync_state
                    WHERE user_id = ?
                    ORDER BY COALESCE(last_run_at, '') DESC
                    """,
                    (owner_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT job_name, last_run_at, last_cursor_json
                    FROM sync_state
                    ORDER BY COALESCE(last_run_at, '') DESC
                    """
                ).fetchall()
        return [
            SyncState(
                job_name=row["job_name"],
                last_run_at=row["last_run_at"],
                last_cursor_json=row["last_cursor_json"],
            )
            for row in rows
        ]

    def record_identity_ack(
        self,
        token: str,
        expires_at: str | datetime | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        acknowledged_at = now_utc_iso()
        expires_at_norm = normalize_datetime(expires_at)
        metadata = _json_dump(metadata_json)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO identity_ack(token, acknowledged_at, expires_at, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (str(token), acknowledged_at, expires_at_norm, metadata),
            )
        return {
            "token": str(token),
            "acknowledged_at": acknowledged_at,
            "expires_at": expires_at_norm,
            "metadata_json": metadata,
        }

    def get_active_identity_ack(self, now_iso: str | None = None) -> dict[str, Any] | None:
        now_value = normalize_datetime(now_iso) if now_iso else now_utc_iso()
        if not now_value:
            now_value = now_utc_iso()
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, token, acknowledged_at, expires_at, metadata_json
                FROM identity_ack
                WHERE expires_at IS NULL OR expires_at > ?
                ORDER BY acknowledged_at DESC
                LIMIT 1
                """,
                (now_value,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "token": str(row["token"] or ""),
            "acknowledged_at": row["acknowledged_at"],
            "expires_at": row["expires_at"],
            "metadata_json": row["metadata_json"],
        }

    def has_active_identity_ack(self, now_iso: str | None = None) -> bool:
        return self.get_active_identity_ack(now_iso=now_iso) is not None

    def create_onboarding_session(
        self,
        *,
        session_kind: str,
        chat_id: str,
        expires_at: str | datetime,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = str(session_kind or "").strip().lower()
        chat = str(chat_id or "").strip()
        if not kind:
            raise ValueError("session_kind is required")
        if not chat:
            raise ValueError("chat_id is required")
        expires_at_norm = normalize_datetime(expires_at)
        if not expires_at_norm:
            raise ValueError("expires_at is required")
        raw_token = secrets.token_urlsafe(24)
        token_hash = sha256(raw_token.encode("utf-8")).hexdigest()
        metadata = _json_dump(metadata_json)
        ts = now_utc_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO onboarding_sessions(
                    session_kind, token_hash, chat_id, expires_at, used_at,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (kind, token_hash, chat, expires_at_norm, metadata, ts, ts),
            )
            row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        return {
            "id": row_id,
            "token": raw_token,
            "session_kind": kind,
            "chat_id": chat,
            "expires_at": expires_at_norm,
            "used_at": None,
            "metadata_json": metadata,
        }

    def get_active_onboarding_session(
        self,
        *,
        token: str,
        session_kind: str | None = None,
        now_iso: str | None = None,
    ) -> dict[str, Any] | None:
        raw_token = str(token or "").strip()
        if not raw_token:
            return None
        token_hash = sha256(raw_token.encode("utf-8")).hexdigest()
        now_value = normalize_datetime(now_iso) if now_iso else now_utc_iso()
        kind = str(session_kind or "").strip().lower()
        query = """
            SELECT id, session_kind, chat_id, expires_at, used_at, metadata_json, created_at, updated_at
            FROM onboarding_sessions
            WHERE token_hash = ?
              AND used_at IS NULL
              AND expires_at > ?
        """
        params: list[Any] = [token_hash, now_value]
        if kind:
            query += " AND session_kind = ?"
            params.append(kind)
        query += " ORDER BY id DESC LIMIT 1"
        with self.connection() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "session_kind": str(row["session_kind"] or ""),
            "chat_id": str(row["chat_id"] or ""),
            "expires_at": row["expires_at"],
            "used_at": row["used_at"],
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def mark_onboarding_session_used(
        self,
        *,
        session_id: int,
        metadata_json: dict[str, Any] | None = None,
    ) -> None:
        ts = now_utc_iso()
        metadata = _json_dump(metadata_json)
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE onboarding_sessions
                SET used_at = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (ts, metadata, ts, int(session_id)),
            )

    def record_auth_attempt(
        self,
        *,
        chat_id: str | None = None,
        user_id: int | None = None,
        onboarding_session_id: int | None = None,
        session_kind: str | None = None,
        school_slug: str | None = None,
        remote_addr: str | None = None,
        username: str | None = None,
        status: str,
        failure_reason: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return db_auth_attempts.record_auth_attempt(
            self,
            chat_id=chat_id,
            user_id=user_id,
            onboarding_session_id=onboarding_session_id,
            session_kind=session_kind,
            school_slug=school_slug,
            remote_addr=remote_addr,
            username=username,
            status=status,
            failure_reason=failure_reason,
            metadata_json=metadata_json,
        )

    def count_auth_attempts(
        self,
        *,
        chat_id: str | None = None,
        user_id: int | None = None,
        onboarding_session_id: int | None = None,
        session_kind: str | None = None,
        school_slug: str | None = None,
        remote_addr: str | None = None,
        status: str | None = None,
        since_iso: str | None = None,
    ) -> int:
        return db_auth_attempts.count_auth_attempts(
            self,
            chat_id=chat_id,
            user_id=user_id,
            onboarding_session_id=onboarding_session_id,
            session_kind=session_kind,
            school_slug=school_slug,
            remote_addr=remote_addr,
            status=status,
            since_iso=since_iso,
        )

    def list_auth_attempts(
        self,
        *,
        status: str | None = None,
        session_kind: str | None = None,
        since_iso: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return db_auth_attempts.list_auth_attempts(
            self,
            status=status,
            session_kind=session_kind,
            since_iso=since_iso,
            limit=limit,
        )

    def auth_attempt_dashboard_snapshot(
        self,
        now_iso: str | None = None,
        *,
        session_kind: str | None = None,
    ) -> dict[str, Any]:
        return db_auth_attempts.auth_attempt_dashboard_snapshot(
            self,
            now_iso=now_iso,
            session_kind=session_kind,
        )

    def upsert_moodle_connection(
        self,
        *,
        chat_id: str,
        school_slug: str,
        display_name: str,
        ws_base_url: str,
        username: str,
        secret_kind: str,
        secret_ref: str,
        login_secret_kind: str | None = None,
        login_secret_ref: str | None = None,
        last_verified_at: str | datetime | None = None,
        metadata_json: dict[str, Any] | None = None,
        status: str = "active",
        user_id: int | None = None,
    ) -> dict[str, Any]:
        return db_connections.upsert_moodle_connection(
            self,
            chat_id=chat_id,
            school_slug=school_slug,
            display_name=display_name,
            ws_base_url=ws_base_url,
            username=username,
            secret_kind=secret_kind,
            secret_ref=secret_ref,
            login_secret_kind=login_secret_kind,
            login_secret_ref=login_secret_ref,
            last_verified_at=last_verified_at,
            metadata_json=metadata_json,
            status=status,
            user_id=user_id,
        )

    def list_moodle_connections(
        self,
        *,
        chat_id: str | None = None,
        user_id: int | None = None,
        school_slug: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return db_connections.list_moodle_connections(
            self,
            chat_id=chat_id,
            user_id=user_id,
            school_slug=school_slug,
            status=status,
            limit=limit,
        )

    def get_moodle_connection(
        self,
        *,
        chat_id: str | None = None,
        user_id: int | None = None,
        school_slug: str,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        return db_connections.get_moodle_connection(
            self,
            chat_id=chat_id,
            user_id=user_id,
            school_slug=school_slug,
            status=status,
        )

    def upsert_lms_browser_session(
        self,
        *,
        chat_id: str,
        school_slug: str,
        provider: str,
        display_name: str,
        login_url: str,
        profile_dir: str | Path | None = None,
        secret_kind: str | None = None,
        secret_ref: str | None = None,
        status: str = "active",
        last_opened_at: str | datetime | None = None,
        last_verified_at: str | datetime | None = None,
        metadata_json: dict[str, Any] | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        return db_connections.upsert_lms_browser_session(
            self,
            chat_id=chat_id,
            school_slug=school_slug,
            provider=provider,
            display_name=display_name,
            login_url=login_url,
            profile_dir=profile_dir,
            secret_kind=secret_kind,
            secret_ref=secret_ref,
            status=status,
            last_opened_at=last_opened_at,
            last_verified_at=last_verified_at,
            metadata_json=metadata_json,
            user_id=user_id,
        )

    def get_lms_browser_session(
        self,
        *,
        chat_id: str,
        school_slug: str,
        status: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        return db_connections.get_lms_browser_session(
            self,
            chat_id=chat_id,
            school_slug=school_slug,
            status=status,
            user_id=user_id,
        )

    def list_lms_browser_sessions(
        self,
        *,
        chat_id: str | None = None,
        user_id: int | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return db_connections.list_lms_browser_sessions(
            self,
            chat_id=chat_id,
            user_id=user_id,
            status=status,
            limit=limit,
        )

    def mark_lms_browser_session_inactive(
        self,
        *,
        chat_id: str,
        school_slug: str,
        metadata_json: dict[str, Any] | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        return db_connections.mark_lms_browser_session_inactive(
            self,
            chat_id=chat_id,
            school_slug=school_slug,
            metadata_json=metadata_json,
            user_id=user_id,
        )

    def list_chat_ids_with_active_school_connections(self, *, limit: int = 1000) -> list[str]:
        query = """
            SELECT chat_id
            FROM (
                SELECT chat_id, updated_at
                FROM moodle_connections
                WHERE status = 'active' AND chat_id IS NOT NULL AND TRIM(chat_id) <> ''
                UNION ALL
                SELECT chat_id, updated_at
                FROM lms_browser_sessions
                WHERE status = 'active' AND chat_id IS NOT NULL AND TRIM(chat_id) <> ''
            )
            GROUP BY chat_id
            ORDER BY MAX(updated_at) DESC, chat_id ASC
            LIMIT ?
        """
        with self.connection() as conn:
            rows = conn.execute(query, (max(int(limit), 1),)).fetchall()
        return [str(row["chat_id"] or "").strip() for row in rows if str(row["chat_id"] or "").strip()]

    def upsert_moodle_school_directory(
        self,
        *,
        school_slug: str,
        display_name: str,
        ws_base_url: str,
        login_url: str | None = None,
        homepage_url: str | None = None,
        source_url: str | None = None,
        aliases: list[str] | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        slug = str(school_slug or "").strip().lower()
        if not slug:
            raise ValueError("school_slug is required")
        display = str(display_name or "").strip() or slug
        ws_base = str(ws_base_url or "").strip()
        if not ws_base:
            raise ValueError("ws_base_url is required")
        login = str(login_url or "").strip() or None
        homepage = str(homepage_url or "").strip() or None
        source = str(source_url or "").strip() or None
        alias_items = _string_list(aliases)
        metadata = _json_dump(metadata_json)
        aliases_json = _json_dump_list(alias_items)
        ts = now_utc_iso()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO moodle_school_directory(
                    school_slug, display_name, ws_base_url, login_url, homepage_url,
                    source_url, aliases_json, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(school_slug)
                DO UPDATE SET
                    display_name = excluded.display_name,
                    ws_base_url = excluded.ws_base_url,
                    login_url = excluded.login_url,
                    homepage_url = excluded.homepage_url,
                    source_url = excluded.source_url,
                    aliases_json = excluded.aliases_json,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    slug,
                    display,
                    ws_base,
                    login,
                    homepage,
                    source,
                    aliases_json,
                    metadata,
                    ts,
                    ts,
                ),
            )
            row = conn.execute(
                """
                SELECT school_slug, display_name, ws_base_url, login_url, homepage_url,
                       source_url, aliases_json, metadata_json, created_at, updated_at
                FROM moodle_school_directory
                WHERE school_slug = ?
                """,
                (slug,),
            ).fetchone()
        if not row:
            raise RuntimeError("failed to persist moodle school directory entry")
        return {
            "school_slug": str(row["school_slug"] or ""),
            "display_name": str(row["display_name"] or ""),
            "ws_base_url": str(row["ws_base_url"] or ""),
            "login_url": str(row["login_url"] or ""),
            "homepage_url": str(row["homepage_url"] or ""),
            "source_url": str(row["source_url"] or ""),
            "aliases": _string_list(_json_load_list(row["aliases_json"])),
            "metadata_json": _json_load(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_moodle_school_directory(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT school_slug, display_name, ws_base_url, login_url, homepage_url,
                       source_url, aliases_json, metadata_json, created_at, updated_at
                FROM moodle_school_directory
                ORDER BY display_name COLLATE NOCASE ASC, school_slug ASC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "school_slug": str(row["school_slug"] or ""),
                    "display_name": str(row["display_name"] or ""),
                    "ws_base_url": str(row["ws_base_url"] or ""),
                    "login_url": str(row["login_url"] or ""),
                    "homepage_url": str(row["homepage_url"] or ""),
                    "source_url": str(row["source_url"] or ""),
                    "aliases": _string_list(_json_load_list(row["aliases_json"])),
                    "metadata_json": _json_load(row["metadata_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return output

    def find_moodle_school_directory(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized_query = normalize_course_alias(query)
        entries = self.list_moodle_school_directory(limit=1000)
        if not normalized_query:
            return entries[: max(int(limit), 1)]
        scored: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
        for entry in entries:
            tokens = [
                str(entry.get("display_name") or ""),
                str(entry.get("school_slug") or ""),
                *[str(item) for item in list(entry.get("aliases") or [])],
            ]
            normalized_tokens = [normalize_course_alias(token) for token in tokens]
            if normalized_query not in normalized_tokens and not any(
                normalized_query in token for token in normalized_tokens if token
            ):
                continue
            exact = any(token == normalized_query for token in normalized_tokens)
            prefix = any(token.startswith(normalized_query) for token in normalized_tokens)
            display_name = str(entry.get("display_name") or "")
            score = (
                0 if exact else 1,
                0 if prefix else 1,
                display_name.lower(),
            )
            scored.append((score, entry))
        scored.sort(key=lambda item: item[0])
        return [entry for _, entry in scored[: max(int(limit), 1)]]

    def _resolve_task_row(
        self,
        conn: sqlite3.Connection,
        selector: str,
        *,
        user_id: int | None = 0,
    ) -> sqlite3.Row | None:
        selected = str(selector).strip()
        if not selected:
            return None
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        if selected.isdigit():
            if owner_id is None:
                row = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE id = ?
                    """,
                    (int(selected),),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE id = ?
                      AND user_id = ?
                    """,
                    (int(selected), owner_id),
                ).fetchone()
            if row:
                return row
        if owner_id is None:
            return conn.execute(
                """
                SELECT id, user_id, external_id, source, due_at, title, status, metadata_json
                FROM tasks
                WHERE external_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (selected,),
            ).fetchone()
        return conn.execute(
            """
            SELECT id, user_id, external_id, source, due_at, title, status, metadata_json
            FROM tasks
            WHERE external_id = ?
              AND user_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (selected, owner_id),
        ).fetchone()

    def _resolve_review_event_row(
        self,
        conn: sqlite3.Connection,
        selector: str,
        *,
        user_id: int | None = 0,
    ) -> sqlite3.Row | None:
        selected = str(selector).strip()
        if not selected:
            return None
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        if selected.isdigit():
            if owner_id is None:
                row = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, start_at, end_at, title, location, rrule, metadata_json
                    FROM events
                    WHERE id = ? AND (source = 'review' OR external_id LIKE 'review:%')
                    """,
                    (int(selected),),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, user_id, external_id, source, start_at, end_at, title, location, rrule, metadata_json
                    FROM events
                    WHERE id = ?
                      AND user_id = ?
                      AND (source = 'review' OR external_id LIKE 'review:%')
                    """,
                    (int(selected), owner_id),
                ).fetchone()
            if row:
                return row
        if owner_id is None:
            return conn.execute(
                """
                SELECT id, user_id, external_id, source, start_at, end_at, title, location, rrule, metadata_json
                FROM events
                WHERE external_id = ? AND (source = 'review' OR external_id LIKE 'review:%')
                ORDER BY id ASC
                LIMIT 1
                """,
                (selected,),
            ).fetchone()
        return conn.execute(
            """
            SELECT id, user_id, external_id, source, start_at, end_at, title, location, rrule, metadata_json
            FROM events
            WHERE external_id = ?
              AND user_id = ?
              AND (source = 'review' OR external_id LIKE 'review:%')
            ORDER BY id ASC
            LIMIT 1
            """,
            (selected, owner_id),
        ).fetchone()

    def list_tasks(
        self,
        open_only: bool = False,
        limit: int = 500,
        *,
        user_id: int | None = 0,
    ) -> list[Task]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if open_only:
                if owner_id is None:
                    rows = conn.execute(
                        """
                        SELECT user_id, external_id, source, due_at, title, status, metadata_json
                        FROM tasks
                        WHERE status = 'open'
                        ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT user_id, external_id, source, due_at, title, status, metadata_json
                        FROM tasks
                        WHERE status = 'open'
                          AND user_id = ?
                        ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                        LIMIT ?
                        """,
                        (owner_id, limit),
                    ).fetchall()
            else:
                if owner_id is None:
                    rows = conn.execute(
                        """
                        SELECT user_id, external_id, source, due_at, title, status, metadata_json
                        FROM tasks
                        ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT user_id, external_id, source, due_at, title, status, metadata_json
                        FROM tasks
                        WHERE user_id = ?
                        ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                        LIMIT ?
                        """,
                        (owner_id, limit),
                    ).fetchall()
        return [
            Task(
                external_id=row["external_id"],
                source=row["source"],
                due_at=row["due_at"],
                title=row["title"],
                status=row["status"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def get_task_for_selector(
        self,
        selector: str,
        *,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = self._resolve_task_row(conn, selector, user_id=user_id)
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "external_id": row["external_id"],
            "source": row["source"],
            "due_at": row["due_at"],
            "title": row["title"],
            "status": row["status"],
            "metadata_json": row["metadata_json"],
        }

    def update_task_status(
        self,
        selector: str,
        status: str,
        *,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        canonical_status = canonical_task_status(status)
        ts = now_utc_iso()
        with self.connection() as conn:
            row = self._resolve_task_row(conn, selector, user_id=user_id)
            if not row:
                return None
            metadata = _json_load(row["metadata_json"])
            metadata["completed_status"] = canonical_status
            metadata["completed_at"] = ts
            if canonical_status == "open":
                metadata.pop("completed_at", None)
                metadata.pop("completed_status", None)
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (canonical_status, _json_dump(metadata), ts, int(row["id"])),
            )
            updated = conn.execute(
                """
                SELECT id, user_id, external_id, source, due_at, title, status, metadata_json
                FROM tasks
                WHERE id = ?
                """,
                (int(row["id"]),),
            ).fetchone()
        if not updated:
            return None
        return {
            "id": int(updated["id"]),
            "user_id": int(updated["user_id"]),
            "external_id": updated["external_id"],
            "source": updated["source"],
            "due_at": updated["due_at"],
            "title": updated["title"],
            "status": updated["status"],
            "metadata_json": updated["metadata_json"],
        }

    def get_review_event_for_selector(
        self,
        selector: str,
        *,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = self._resolve_review_event_row(conn, selector, user_id=user_id)
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "external_id": row["external_id"],
            "source": row["source"],
            "start_at": row["start_at"],
            "end_at": row["end_at"],
            "title": row["title"],
            "metadata_json": row["metadata_json"],
        }

    def update_review_status(
        self,
        selector: str,
        review_status: str,
        *,
        user_id: int | None = 0,
    ) -> dict[str, Any] | None:
        status_value = str(review_status or "").strip().lower()
        if status_value not in {"scheduled", "done", "skipped"}:
            raise ValueError("review_status must be one of scheduled, done, skipped")
        ts = now_utc_iso()
        with self.connection() as conn:
            row = self._resolve_review_event_row(conn, selector, user_id=user_id)
            if not row:
                return None
            metadata = _json_load(row["metadata_json"])
            metadata["review_status"] = status_value
            metadata["review_status_updated_at"] = ts
            conn.execute(
                """
                UPDATE events
                SET metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dump(metadata), ts, int(row["id"])),
            )
            updated = conn.execute(
                """
                SELECT id, user_id, external_id, source, start_at, end_at, title, metadata_json
                FROM events
                WHERE id = ?
                """,
                (int(row["id"]),),
            ).fetchone()
        if not updated:
            return None
        return {
            "id": int(updated["id"]),
            "user_id": int(updated["user_id"]),
            "external_id": updated["external_id"],
            "source": updated["source"],
            "start_at": updated["start_at"],
            "end_at": updated["end_at"],
            "title": updated["title"],
            "metadata_json": updated["metadata_json"],
            "review_status": _event_review_status(updated["metadata_json"]),
        }

    def list_events(
        self,
        limit: int = 500,
        include_inactive_reviews: bool = False,
        *,
        user_id: int | None = None,
    ) -> list[Event]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, start_at, end_at, title, location, rrule, metadata_json
                    FROM events
                    ORDER BY start_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, start_at, end_at, title, location, rrule, metadata_json
                    FROM events
                    WHERE user_id = ?
                    ORDER BY start_at ASC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        events = [
            Event(
                external_id=row["external_id"],
                source=row["source"],
                start_at=row["start_at"],
                end_at=row["end_at"],
                title=row["title"],
                location=row["location"],
                rrule=row["rrule"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]
        if include_inactive_reviews:
            return events
        return [
            item
            for item in events
            if _is_event_active_for_lists(
                source=item.source,
                external_id=item.external_id,
                metadata_json=item.metadata_json,
            )
        ]

    def list_open_tasks(
        self,
        limit: int = 500,
        *,
        user_id: int | None = None,
    ) -> list[Task]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                    ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                      AND user_id = ?
                    ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                    LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
        return [
            Task(
                external_id=row["external_id"],
                source=row["source"],
                due_at=row["due_at"],
                title=row["title"],
                status=row["status"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def list_open_tasks_due_from(
        self,
        since_iso: str,
        limit: int = 500,
        *,
        until_iso: str | None = None,
        user_id: int | None = None,
    ) -> list[Task]:
        since_value = normalize_datetime(since_iso) if since_iso else None
        if not since_value:
            since_value = now_utc_iso()
        until_value = normalize_datetime(until_iso) if until_iso else None
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            params: list[Any] = [since_value]
            if owner_id is None:
                query = """
                    SELECT user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                      AND due_at IS NOT NULL
                      AND due_at >= ?
                """
            else:
                query = """
                    SELECT user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                      AND due_at IS NOT NULL
                      AND due_at >= ?
                      AND user_id = ?
                """
                params.append(owner_id)
            if until_value:
                query += "\n  AND due_at <= ?"
                params.append(until_value)
            query += "\nORDER BY due_at ASC\nLIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            Task(
                external_id=row["external_id"],
                source=row["source"],
                due_at=row["due_at"],
                title=row["title"],
                status=row["status"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def list_tasks_due_within(
        self,
        days: int,
        now_iso: str | None = None,
        limit: int = 200,
        *,
        user_id: int | None = None,
    ) -> list[Task]:
        now_value = normalize_datetime(now_iso) if now_iso else now_utc_iso()
        if not now_value:
            now_value = now_utc_iso()
        now_dt = dt_parser.isoparse(now_value)
        until_iso = (now_dt.replace(microsecond=0) + timedelta(days=max(days, 0))).isoformat()
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                      AND due_at IS NOT NULL
                      AND due_at >= ?
                      AND due_at <= ?
                    ORDER BY due_at ASC
                    LIMIT ?
                    """,
                    (now_value, until_iso, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, external_id, source, due_at, title, status, metadata_json
                    FROM tasks
                    WHERE status = 'open'
                      AND due_at IS NOT NULL
                      AND due_at >= ?
                      AND due_at <= ?
                      AND user_id = ?
                    ORDER BY due_at ASC
                    LIMIT ?
                    """,
                    (now_value, until_iso, owner_id, limit),
                ).fetchall()
        return [
            Task(
                external_id=row["external_id"],
                source=row["source"],
                due_at=row["due_at"],
                title=row["title"],
                status=row["status"],
                metadata_json=row["metadata_json"],
                user_id=int(row["user_id"]),
            )
            for row in rows
        ]

    def _dashboard_parse_metadata_json(self, value: str | dict[str, Any] | None) -> dict[str, Any]:
        return parse_metadata_json(value)

    def _dashboard_normalize_provenance(
        self,
        value: str | dict[str, Any] | None,
        *,
        fallback_source: str | None = None,
    ) -> dict[str, Any]:
        return normalize_provenance(value, fallback_source=fallback_source)

    def _dashboard_event_is_active_for_lists(
        self,
        *,
        source: str,
        external_id: str,
        metadata_json: str | None,
    ) -> bool:
        return _is_event_active_for_lists(
            source=source,
            external_id=external_id,
            metadata_json=metadata_json,
        )

    def sync_dashboard_snapshot(self, *, user_id: int | None = None) -> dict[str, Any]:
        return db_sync.sync_dashboard_snapshot(self, user_id=user_id)

    def latest_weather_snapshot(
        self,
        *,
        user_id: int | None = 0,
        allow_global_fallback: bool = True,
    ) -> dict[str, Any] | None:
        return db_sync.latest_weather_snapshot(
            self,
            user_id=user_id,
            allow_global_fallback=allow_global_fallback,
        )

    def dashboard_snapshot(self, now_iso: str | None = None, *, user_id: int | None = None) -> dict[str, Any]:
        return db_dashboard_queries.dashboard_snapshot(self, now_iso=now_iso, user_id=user_id)

    def upsert_school_building(
        self,
        *,
        school_slug: str,
        building_code: str,
        building_name: str,
        aliases: list[str] | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        school = self.get_school_by_slug(school_slug)
        if school is None:
            raise ValueError("school_slug is required")
        code = str(building_code or "").strip()
        name = str(building_name or "").strip()
        if not code:
            raise ValueError("building_code is required")
        if not name:
            raise ValueError("building_name is required")
        ts = now_utc_iso()
        alias_json = _json_dump_list(_string_list(aliases))
        metadata = _json_dump(metadata_json)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO school_buildings(
                    school_id, building_code, building_name, aliases_json, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(school_id, building_code)
                DO UPDATE SET
                    building_name = excluded.building_name,
                    aliases_json = excluded.aliases_json,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    int(school["school_id"]),
                    code,
                    name,
                    alias_json,
                    metadata,
                    ts,
                ),
            )
        return {
            "school_id": int(school["school_id"]),
            "school_slug": str(school["school_slug"] or ""),
            "building_code": code,
            "building_name": name,
            "aliases": _string_list(aliases),
            "metadata_json": metadata_json or {},
            "updated_at": ts,
        }

    def upsert_building(
        self,
        building_no: str,
        building_name: str,
        metadata_json: dict[str, Any] | None = None,
        *,
        school_slug: str = "uos_online_class",
    ) -> dict[str, Any]:
        ts = now_utc_iso()
        key = str(building_no or "").strip()
        name = str(building_name or "").strip()
        if not key:
            raise ValueError("building_no is required")
        if not name:
            raise ValueError("building_name is required")
        metadata = _json_dump(metadata_json)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO building_map(building_no, building_name, metadata_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(building_no)
                DO UPDATE SET
                    building_name = excluded.building_name,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (key, name, metadata, ts),
            )
        if self.get_school_by_slug(school_slug) is not None:
            self.upsert_school_building(
                school_slug=school_slug,
                building_code=key,
                building_name=name,
                metadata_json=metadata_json,
            )
        return {
            "building_no": key,
            "building_name": name,
            "metadata_json": metadata,
            "updated_at": ts,
        }

    def get_building_name(self, building_no: str, *, school_slug: str = "uos_online_class") -> str | None:
        key = str(building_no or "").strip()
        if not key:
            return None
        school = self.get_school_by_slug(school_slug)
        if school is not None:
            with self.connection() as conn:
                row = conn.execute(
                    """
                    SELECT building_name
                    FROM school_buildings
                    WHERE school_id = ? AND building_code = ?
                    LIMIT 1
                    """,
                    (int(school["school_id"]), key),
                ).fetchone()
            if row:
                value = str(row["building_name"] or "").strip()
                if value:
                    return value
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT building_name
                FROM building_map
                WHERE building_no = ?
                """,
                (key,),
            ).fetchone()
        if not row:
            return None
        value = str(row["building_name"] or "").strip()
        return value or None

    def list_buildings(
        self,
        limit: int = 500,
        *,
        school_slug: str = "uos_online_class",
    ) -> list[dict[str, Any]]:
        school = self.get_school_by_slug(school_slug)
        with self.connection() as conn:
            if school is not None:
                rows = conn.execute(
                    """
                    SELECT building_code AS building_no, building_name, metadata_json, updated_at
                    FROM school_buildings
                    WHERE school_id = ?
                    ORDER BY CAST(building_code AS INTEGER) ASC, building_code ASC
                    LIMIT ?
                    """,
                    (int(school["school_id"]), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT building_no, building_name, metadata_json, updated_at
                    FROM building_map
                    ORDER BY CAST(building_no AS INTEGER) ASC, building_no ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "building_no": str(row["building_no"] or ""),
                    "building_name": str(row["building_name"] or ""),
                    "metadata_json": str(row["metadata_json"] or "{}"),
                    "updated_at": row["updated_at"],
                }
            )
        return output

    def upsert_telegram_reminder(
        self,
        external_id: str,
        chat_id: str,
        run_at: str | datetime,
        message: str,
        metadata_json: dict[str, Any] | None = None,
        status: str = "pending",
        user_id: int | None = 0,
    ) -> dict[str, Any]:
        ts = now_utc_iso()
        external = str(external_id or "").strip()
        if not external:
            raise ValueError("external_id is required")
        chat = str(chat_id or "").strip()
        if not chat:
            raise ValueError("chat_id is required")
        run_at_norm = normalize_datetime(run_at)
        if not run_at_norm:
            raise ValueError("run_at is required")
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")
        status_value = str(status or "pending").strip().lower()
        if status_value not in {"pending", "sent", "failed", "cancelled"}:
            status_value = "pending"
        metadata = _json_dump(metadata_json)
        sent_at = ts if status_value == "sent" else None
        owner_id = _normalize_user_id(user_id, default=0) or 0
        if owner_id == 0 and chat:
            user = self.ensure_user_for_chat(
                chat_id=chat,
                metadata_json={"source": "telegram_reminder"},
            )
            owner_id = int(user["id"])
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO telegram_reminders(
                    user_id, external_id, chat_id, run_at, message, status, sent_at,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, external_id)
                DO UPDATE SET
                    chat_id = excluded.chat_id,
                    run_at = excluded.run_at,
                    message = excluded.message,
                    status = excluded.status,
                    sent_at = excluded.sent_at,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    external,
                    chat,
                    run_at_norm,
                    text,
                    status_value,
                    sent_at,
                    metadata,
                    ts,
                    ts,
                ),
            )
            row = conn.execute(
                """
                SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json, updated_at
                FROM telegram_reminders
                WHERE external_id = ?
                  AND user_id = ?
                LIMIT 1
                """,
                (external, owner_id),
            ).fetchone()
        if not row:
            raise RuntimeError("failed to persist telegram reminder")
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "external_id": str(row["external_id"] or ""),
            "chat_id": str(row["chat_id"] or ""),
            "run_at": row["run_at"],
            "message": str(row["message"] or ""),
            "status": str(row["status"] or "pending"),
            "sent_at": row["sent_at"],
            "metadata_json": str(row["metadata_json"] or "{}"),
            "updated_at": row["updated_at"],
        }

    def list_due_telegram_reminders(
        self,
        now_iso: str | None = None,
        limit: int = 100,
        *,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        now_value = normalize_datetime(now_iso) if now_iso else now_utc_iso()
        if not now_value:
            now_value = now_utc_iso()
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                rows = conn.execute(
                    """
                    SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json
                    FROM telegram_reminders
                    WHERE status = 'pending'
                      AND run_at <= ?
                    ORDER BY run_at ASC, id ASC
                    LIMIT ?
                    """,
                    (now_value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json
                    FROM telegram_reminders
                    WHERE status = 'pending'
                      AND run_at <= ?
                      AND user_id = ?
                    ORDER BY run_at ASC, id ASC
                    LIMIT ?
                    """,
                    (now_value, owner_id, limit),
                ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "id": int(row["id"]),
                    "user_id": int(row["user_id"]),
                    "external_id": str(row["external_id"] or ""),
                    "chat_id": str(row["chat_id"] or ""),
                    "run_at": row["run_at"],
                    "message": str(row["message"] or ""),
                    "status": str(row["status"] or "pending"),
                    "sent_at": row["sent_at"],
                    "metadata_json": str(row["metadata_json"] or "{}"),
                }
            )
        return output

    def mark_telegram_reminder_status(
        self,
        reminder_id: int,
        status: str,
        sent_at: str | datetime | None = None,
        *,
        user_id: int | None = None,
    ) -> bool:
        status_value = str(status or "").strip().lower()
        if status_value not in {"pending", "sent", "failed", "cancelled"}:
            raise ValueError("unsupported reminder status")
        ts = now_utc_iso()
        if status_value == "sent":
            resolved_sent_at = normalize_datetime(sent_at) or ts
        else:
            resolved_sent_at = normalize_datetime(sent_at) if sent_at else None
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if owner_id is None:
                result = conn.execute(
                    """
                    UPDATE telegram_reminders
                    SET status = ?, sent_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status_value, resolved_sent_at, ts, int(reminder_id)),
                )
            else:
                result = conn.execute(
                    """
                    UPDATE telegram_reminders
                    SET status = ?, sent_at = ?, updated_at = ?
                    WHERE id = ?
                      AND user_id = ?
                    """,
                    (status_value, resolved_sent_at, ts, int(reminder_id), owner_id),
                )
        return result.rowcount > 0

    def list_telegram_reminders(
        self,
        status: str | None = None,
        limit: int = 200,
        *,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        status_value = str(status or "").strip().lower()
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            if status_value:
                if owner_id is None:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json, updated_at
                        FROM telegram_reminders
                        WHERE status = ?
                        ORDER BY run_at ASC, id ASC
                        LIMIT ?
                        """,
                        (status_value, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json, updated_at
                        FROM telegram_reminders
                        WHERE status = ?
                          AND user_id = ?
                        ORDER BY run_at ASC, id ASC
                        LIMIT ?
                        """,
                        (status_value, owner_id, limit),
                    ).fetchall()
            else:
                if owner_id is None:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json, updated_at
                        FROM telegram_reminders
                        ORDER BY run_at ASC, id ASC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, external_id, chat_id, run_at, message, status, sent_at, metadata_json, updated_at
                        FROM telegram_reminders
                        WHERE user_id = ?
                        ORDER BY run_at ASC, id ASC
                        LIMIT ?
                        """,
                        (owner_id, limit),
                    ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "id": int(row["id"]),
                    "user_id": int(row["user_id"]),
                    "external_id": str(row["external_id"] or ""),
                    "chat_id": str(row["chat_id"] or ""),
                    "run_at": row["run_at"],
                    "message": str(row["message"] or ""),
                    "status": str(row["status"] or "pending"),
                    "sent_at": row["sent_at"],
                    "metadata_json": str(row["metadata_json"] or "{}"),
                    "updated_at": row["updated_at"],
                }
            )
        return output

    def counts(self, *, user_id: int | None = None) -> dict[str, int]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            output: dict[str, int] = {}
            if owner_id is None:
                for table in (
                    "events",
                    "tasks",
                    "artifacts",
                    "notifications",
                    "sync_state",
                    "inbox",
                    "summaries",
                    "building_map",
                    "telegram_reminders",
                ):
                    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                    output[table] = int(row["c"])
                return output
            for table in (
                "events",
                "tasks",
                "artifacts",
                "notifications",
                "inbox",
                "summaries",
                "telegram_reminders",
            ):
                row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?",
                    (owner_id,),
                ).fetchone()
                output[table] = int(row["c"])
            for table in ("sync_state", "building_map"):
                row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                output[table] = int(row["c"])
            return output

    def day_brief_cache_snapshot(self, *, user_id: int | None = None) -> dict[str, Any]:
        owner_id = _normalize_user_id(user_id, default=0) if user_id is not None else None
        with self.connection() as conn:
            def _aggregate(
                table: str,
                *,
                count_expr: str = "COUNT(*)",
                max_expr: str = "MAX(updated_at)",
                where_clause: str = "",
                params: tuple[Any, ...] = (),
            ) -> dict[str, Any]:
                row = conn.execute(
                    f"""
                    SELECT
                        {count_expr} AS count,
                        {max_expr} AS max_value
                    FROM {table}
                    {where_clause}
                    """,
                    params,
                ).fetchone()
                return {
                    "count": int(row["count"] or 0) if row else 0,
                    "max_value": str(row["max_value"] or "").strip() or None,
                }

            scope_clause = "" if owner_id is None else "WHERE user_id = ?"
            scope_params: tuple[Any, ...] = () if owner_id is None else (owner_id,)
            event_state = _aggregate("events", where_clause=scope_clause, params=scope_params)
            artifact_state = _aggregate("artifacts", where_clause=scope_clause, params=scope_params)
            notification_state = _aggregate(
                "notifications",
                max_expr="MAX(COALESCE(updated_at, created_at))",
                where_clause=scope_clause,
                params=scope_params,
            )
            course_state = _aggregate("courses", where_clause=scope_clause, params=scope_params)
            alias_state = _aggregate("course_aliases", where_clause=scope_clause, params=scope_params)
            if owner_id is None:
                open_task_state = _aggregate(
                    "tasks",
                    max_expr="MAX(COALESCE(updated_at, due_at))",
                    where_clause="WHERE status = 'open'",
                )
            else:
                open_task_state = _aggregate(
                    "tasks",
                    max_expr="MAX(COALESCE(updated_at, due_at))",
                    where_clause="WHERE status = 'open' AND user_id = ?",
                    params=(owner_id,),
                )
        task_merge_state = self.get_sync_state("task_merge_cache", user_id=owner_id if owner_id is not None else 0)
        task_merge_cursor = _json_load(task_merge_state.last_cursor_json if task_merge_state else None)
        return {
            "user_id": owner_id,
            "events": event_state,
            "artifacts": artifact_state,
            "notifications": notification_state,
            "open_tasks": open_task_state,
            "courses": course_state,
            "course_aliases": alias_state,
            "task_merge_cache": {
                "last_run_at": str(task_merge_state.last_run_at or "").strip() or None,
                "payload_hash": str(task_merge_cursor.get("payload_hash") or "").strip() or None,
                "fingerprint": str(task_merge_cursor.get("fingerprint") or "").strip() or None,
                "updated_at": str(task_merge_cursor.get("updated_at") or "").strip() or None,
                "group_count": len(list(task_merge_cursor.get("groups") or [])),
            },
        }


def dataclass_dict(
    item: Event | Task | Artifact | Notification | SyncState | InboxItem | Summary,
) -> dict[str, Any]:
    return asdict(item)
