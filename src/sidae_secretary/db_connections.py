from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from dateutil import parser as dt_parser


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_datetime(value: str | datetime | None) -> str | None:
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


def _normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _moodle_connection_row_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "chat_id": str(row["chat_id"] or ""),
        "school_slug": str(row["school_slug"] or ""),
        "display_name": str(row["display_name"] or ""),
        "ws_base_url": str(row["ws_base_url"] or ""),
        "username": str(row["username"] or ""),
        "secret_kind": str(row["secret_kind"] or ""),
        "secret_ref": str(row["secret_ref"] or ""),
        "login_secret_kind": str(row["login_secret_kind"] or ""),
        "login_secret_ref": str(row["login_secret_ref"] or ""),
        "status": str(row["status"] or ""),
        "last_verified_at": row["last_verified_at"],
        "metadata_json": _json_load(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _browser_session_row_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "chat_id": str(row["chat_id"] or ""),
        "school_slug": str(row["school_slug"] or ""),
        "provider": str(row["provider"] or ""),
        "display_name": str(row["display_name"] or ""),
        "login_url": str(row["login_url"] or ""),
        "profile_dir": str(row["profile_dir"] or ""),
        "secret_kind": str(row["secret_kind"] or ""),
        "secret_ref": str(row["secret_ref"] or ""),
        "status": str(row["status"] or ""),
        "last_opened_at": row["last_opened_at"],
        "last_verified_at": row["last_verified_at"],
        "metadata_json": _json_load(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def upsert_moodle_connection(
    db: Any,
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
    chat = str(chat_id or "").strip()
    slug = str(school_slug or "").strip().lower()
    display = str(display_name or "").strip() or slug
    ws_base = str(ws_base_url or "").strip()
    username_value = str(username or "").strip()
    kind = str(secret_kind or "").strip()
    ref = str(secret_ref or "").strip()
    login_kind = _normalize_optional_text(login_secret_kind)
    login_ref = _normalize_optional_text(login_secret_ref)
    state = str(status or "active").strip().lower() or "active"
    if not chat:
        raise ValueError("chat_id is required")
    if not slug:
        raise ValueError("school_slug is required")
    if not ws_base:
        raise ValueError("ws_base_url is required")
    if not username_value:
        raise ValueError("username is required")
    if not kind or not ref:
        raise ValueError("secret reference is required")
    verified_at = _normalize_datetime(last_verified_at)
    metadata = _json_dump(metadata_json)
    ts = _now_utc_iso()
    resolved_user_id = _normalize_user_id(user_id)
    if resolved_user_id is None:
        resolved_user = db.ensure_user_for_chat(
            chat_id=chat,
            metadata_json={"source": "moodle_connection"},
        )
        resolved_user_id = int(resolved_user["id"])
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO moodle_connections(
                user_id, chat_id, school_slug, display_name, ws_base_url, username,
                secret_kind, secret_ref, login_secret_kind, login_secret_ref,
                status, last_verified_at,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, school_slug)
            DO UPDATE SET
                user_id = excluded.user_id,
                display_name = excluded.display_name,
                ws_base_url = excluded.ws_base_url,
                username = excluded.username,
                secret_kind = excluded.secret_kind,
                secret_ref = excluded.secret_ref,
                login_secret_kind = COALESCE(excluded.login_secret_kind, moodle_connections.login_secret_kind),
                login_secret_ref = COALESCE(excluded.login_secret_ref, moodle_connections.login_secret_ref),
                status = excluded.status,
                last_verified_at = excluded.last_verified_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                resolved_user_id,
                chat,
                slug,
                display,
                ws_base,
                username_value,
                kind,
                ref,
                login_kind,
                login_ref,
                state,
                verified_at,
                metadata,
                ts,
                ts,
            ),
        )
        row = conn.execute(
            """
            SELECT id, user_id, chat_id, school_slug, display_name, ws_base_url, username,
                   secret_kind, secret_ref, login_secret_kind, login_secret_ref,
                   status, last_verified_at, metadata_json, created_at, updated_at
            FROM moodle_connections
            WHERE chat_id = ? AND school_slug = ?
            """,
            (chat, slug),
        ).fetchone()
    if not row:
        raise RuntimeError("failed to persist moodle connection")
    return _moodle_connection_row_payload(row)


def list_moodle_connections(
    db: Any,
    *,
    chat_id: str | None = None,
    user_id: int | None = None,
    school_slug: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, user_id, chat_id, school_slug, display_name, ws_base_url, username,
               secret_kind, secret_ref, login_secret_kind, login_secret_ref,
               status, last_verified_at, metadata_json, created_at, updated_at
        FROM moodle_connections
        WHERE 1 = 1
    """
    params: list[Any] = []
    owner_id = _normalize_user_id(user_id)
    if owner_id is not None:
        query += " AND user_id = ?"
        params.append(owner_id)
    chat = str(chat_id or "").strip()
    if chat:
        query += " AND chat_id = ?"
        params.append(chat)
    slug = str(school_slug or "").strip().lower()
    if slug:
        query += " AND school_slug = ?"
        params.append(slug)
    state = str(status or "").strip().lower()
    if state:
        query += " AND status = ?"
        params.append(state)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with db.connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_moodle_connection_row_payload(row) for row in rows]


def get_moodle_connection(
    db: Any,
    *,
    chat_id: str | None = None,
    user_id: int | None = None,
    school_slug: str,
    status: str | None = None,
) -> dict[str, Any] | None:
    items = list_moodle_connections(
        db,
        chat_id=chat_id,
        user_id=user_id,
        school_slug=school_slug,
        status=status,
        limit=1,
    )
    return items[0] if items else None


def upsert_lms_browser_session(
    db: Any,
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
    chat = str(chat_id or "").strip()
    slug = str(school_slug or "").strip().lower()
    provider_name = str(provider or "").strip().lower()
    display = str(display_name or "").strip() or slug
    login = str(login_url or "").strip()
    profile = str(profile_dir or "").strip()
    kind = str(secret_kind or "").strip()
    ref = str(secret_ref or "").strip()
    state = str(status or "active").strip().lower() or "active"
    opened_at = _normalize_datetime(last_opened_at)
    verified_at = _normalize_datetime(last_verified_at)
    metadata = _json_dump(metadata_json)
    if not chat:
        raise ValueError("chat_id is required")
    if not slug:
        raise ValueError("school_slug is required")
    if not provider_name:
        raise ValueError("provider is required")
    if not login:
        raise ValueError("login_url is required")
    if not profile and (not kind or not ref):
        raise ValueError("profile_dir or secret reference is required")
    ts = _now_utc_iso()
    resolved_user_id = _normalize_user_id(user_id)
    if resolved_user_id is None:
        user = db.ensure_user_for_chat(
            chat_id=chat,
            metadata_json={"source": "lms_browser_session"},
        )
        resolved_user_id = int(user["id"])
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO lms_browser_sessions(
                user_id, chat_id, school_slug, provider, display_name, login_url,
                profile_dir, secret_kind, secret_ref, status, last_opened_at, last_verified_at,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, school_slug)
            DO UPDATE SET
                user_id = excluded.user_id,
                provider = excluded.provider,
                display_name = excluded.display_name,
                login_url = excluded.login_url,
                profile_dir = excluded.profile_dir,
                secret_kind = excluded.secret_kind,
                secret_ref = excluded.secret_ref,
                status = excluded.status,
                last_opened_at = excluded.last_opened_at,
                last_verified_at = excluded.last_verified_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                resolved_user_id,
                chat,
                slug,
                provider_name,
                display,
                login,
                profile,
                kind or None,
                ref or None,
                state,
                opened_at,
                verified_at,
                metadata,
                ts,
                ts,
            ),
        )
        row = conn.execute(
            """
            SELECT id, user_id, chat_id, school_slug, provider, display_name, login_url, profile_dir,
                   secret_kind, secret_ref, status, last_opened_at, last_verified_at, metadata_json,
                   created_at, updated_at
            FROM lms_browser_sessions
            WHERE chat_id = ? AND school_slug = ?
            LIMIT 1
            """,
            (chat, slug),
        ).fetchone()
    if not row:
        raise RuntimeError("failed to persist browser session")
    return _browser_session_row_payload(row)


def get_lms_browser_session(
    db: Any,
    *,
    chat_id: str,
    school_slug: str,
    status: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    chat = str(chat_id or "").strip()
    slug = str(school_slug or "").strip().lower()
    if not chat or not slug:
        return None
    query = """
        SELECT id, user_id, chat_id, school_slug, provider, display_name, login_url, profile_dir,
               secret_kind, secret_ref, status, last_opened_at, last_verified_at, metadata_json,
               created_at, updated_at
        FROM lms_browser_sessions
        WHERE chat_id = ? AND school_slug = ?
    """
    params: list[Any] = [chat, slug]
    owner_id = _normalize_user_id(user_id)
    if owner_id is not None:
        query += " AND user_id = ?"
        params.append(owner_id)
    normalized_status = str(status or "").strip().lower()
    if normalized_status:
        query += " AND status = ?"
        params.append(normalized_status)
    query += " ORDER BY updated_at DESC LIMIT 1"
    with db.connection() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    if not row:
        return None
    return _browser_session_row_payload(row)


def list_lms_browser_sessions(
    db: Any,
    *,
    chat_id: str | None = None,
    user_id: int | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, user_id, chat_id, school_slug, provider, display_name, login_url, profile_dir,
               secret_kind, secret_ref, status, last_opened_at, last_verified_at, metadata_json,
               created_at, updated_at
        FROM lms_browser_sessions
        WHERE 1 = 1
    """
    params: list[Any] = []
    owner_id = _normalize_user_id(user_id)
    if owner_id is not None:
        query += " AND user_id = ?"
        params.append(owner_id)
    chat = str(chat_id or "").strip()
    if chat:
        query += " AND chat_id = ?"
        params.append(chat)
    normalized_status = str(status or "").strip().lower()
    if normalized_status:
        query += " AND status = ?"
        params.append(normalized_status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with db.connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_browser_session_row_payload(row) for row in rows]


def mark_lms_browser_session_inactive(
    db: Any,
    *,
    chat_id: str,
    school_slug: str,
    metadata_json: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    existing = get_lms_browser_session(
        db,
        chat_id=chat_id,
        school_slug=school_slug,
        user_id=user_id,
    )
    if not existing:
        return None
    metadata = dict(existing.get("metadata_json") or {})
    if metadata_json:
        metadata.update(metadata_json)
    ts = _now_utc_iso()
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE lms_browser_sessions
            SET status = 'inactive',
                metadata_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (_json_dump(metadata), ts, int(existing["id"])),
        )
    return get_lms_browser_session(
        db,
        chat_id=chat_id,
        school_slug=school_slug,
        user_id=user_id,
    )
