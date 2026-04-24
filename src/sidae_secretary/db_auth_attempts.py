from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
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


def _auth_attempt_row_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"] or 0),
        "chat_id": str(row["chat_id"] or ""),
        "onboarding_session_id": int(row["onboarding_session_id"] or 0)
        if row["onboarding_session_id"] is not None
        else None,
        "session_kind": str(row["session_kind"] or ""),
        "school_slug": str(row["school_slug"] or ""),
        "remote_addr": str(row["remote_addr"] or ""),
        "username": str(row["username"] or ""),
        "status": str(row["status"] or ""),
        "failure_reason": str(row["failure_reason"] or ""),
        "metadata_json": _json_load(row["metadata_json"]),
        "created_at": row["created_at"],
    }


def record_auth_attempt(
    db: Any,
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
    state = str(status or "").strip().lower()
    if not state:
        raise ValueError("status is required")
    chat = str(chat_id or "").strip() or None
    owner_id = _normalize_user_id(user_id, default=0) or 0
    if owner_id <= 0 and chat:
        user = db.ensure_user_for_chat(chat_id=chat, metadata_json={"source": "auth_attempt"})
        owner_id = int(user["id"])
    session_id = _normalize_user_id(onboarding_session_id)
    kind = str(session_kind or "").strip().lower() or None
    slug = str(school_slug or "").strip().lower() or None
    remote = str(remote_addr or "").strip() or None
    account = str(username or "").strip() or None
    reason = str(failure_reason or "").strip() or None
    metadata = _json_dump(metadata_json)
    created_at = _now_utc_iso()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO auth_attempts(
                user_id, chat_id, onboarding_session_id, session_kind, school_slug,
                remote_addr, username, status, failure_reason, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                chat,
                session_id,
                kind,
                slug,
                remote,
                account,
                state,
                reason,
                metadata,
                created_at,
            ),
        )
        row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return {
        "id": row_id,
        "user_id": owner_id,
        "chat_id": chat,
        "onboarding_session_id": session_id,
        "session_kind": kind,
        "school_slug": slug,
        "remote_addr": remote,
        "username": account,
        "status": state,
        "failure_reason": reason,
        "metadata_json": _json_load(metadata),
        "created_at": created_at,
    }


def count_auth_attempts(
    db: Any,
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
    query = "SELECT COUNT(*) AS count FROM auth_attempts WHERE 1 = 1"
    params: list[Any] = []
    owner_id = _normalize_user_id(user_id)
    if owner_id is not None:
        query += " AND user_id = ?"
        params.append(owner_id)
    chat = str(chat_id or "").strip()
    if chat:
        query += " AND chat_id = ?"
        params.append(chat)
    session_id = _normalize_user_id(onboarding_session_id)
    if session_id is not None:
        query += " AND onboarding_session_id = ?"
        params.append(session_id)
    kind = str(session_kind or "").strip().lower()
    if kind:
        query += " AND session_kind = ?"
        params.append(kind)
    slug = str(school_slug or "").strip().lower()
    if slug:
        query += " AND school_slug = ?"
        params.append(slug)
    remote = str(remote_addr or "").strip()
    if remote:
        query += " AND remote_addr = ?"
        params.append(remote)
    state = str(status or "").strip().lower()
    if state:
        query += " AND status = ?"
        params.append(state)
    since_value = _normalize_datetime(since_iso)
    if since_value:
        query += " AND created_at >= ?"
        params.append(since_value)
    with db.connection() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return int(row["count"]) if row else 0


def list_auth_attempts(
    db: Any,
    *,
    status: str | None = None,
    session_kind: str | None = None,
    since_iso: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, user_id, chat_id, onboarding_session_id, session_kind, school_slug,
               remote_addr, username, status, failure_reason, metadata_json, created_at
        FROM auth_attempts
        WHERE 1 = 1
    """
    params: list[Any] = []
    state = str(status or "").strip().lower()
    if state:
        query += " AND status = ?"
        params.append(state)
    kind = str(session_kind or "").strip().lower()
    if kind:
        query += " AND session_kind = ?"
        params.append(kind)
    since_value = _normalize_datetime(since_iso)
    if since_value:
        query += " AND created_at >= ?"
        params.append(since_value)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(int(limit), 1))
    with db.connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_auth_attempt_row_payload(row) for row in rows]


def auth_attempt_dashboard_snapshot(
    db: Any,
    now_iso: str | None = None,
    *,
    session_kind: str | None = None,
) -> dict[str, Any]:
    now_value = _normalize_datetime(now_iso) if now_iso else _now_utc_iso()
    try:
        now_dt = dt_parser.isoparse(str(now_value or _now_utc_iso()))
    except Exception:
        now_dt = datetime.now(timezone.utc)
    last_15m = (now_dt - timedelta(minutes=15)).isoformat()
    last_1h = (now_dt - timedelta(hours=1)).isoformat()
    last_24h = (now_dt - timedelta(hours=24)).isoformat()
    kind = str(session_kind or "").strip().lower()

    def _count(*, status: str | None = None, since_iso: str) -> int:
        return count_auth_attempts(
            db,
            session_kind=kind or None,
            status=status,
            since_iso=since_iso,
        )

    query = """
        SELECT
            COALESCE(NULLIF(remote_addr, ''), 'unknown') AS remote_addr,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
            COUNT(*) AS total_count,
            MAX(created_at) AS last_seen_at
        FROM auth_attempts
        WHERE created_at >= ?
    """
    params: list[Any] = [last_24h]
    if kind:
        query += " AND session_kind = ?"
        params.append(kind)
    query += """
        GROUP BY COALESCE(NULLIF(remote_addr, ''), 'unknown')
        ORDER BY blocked_count DESC, failed_count DESC, total_count DESC, last_seen_at DESC
        LIMIT 20
    """
    with db.connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    recent = list_auth_attempts(
        db,
        session_kind=kind or None,
        since_iso=last_24h,
        limit=25,
    )
    suspicious: list[dict[str, Any]] = []
    top_remotes: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "remote_addr": str(row["remote_addr"] or "unknown"),
            "failed_count": int(row["failed_count"] or 0),
            "blocked_count": int(row["blocked_count"] or 0),
            "total_count": int(row["total_count"] or 0),
            "last_seen_at": row["last_seen_at"],
        }
        top_remotes.append(item)
        if item["blocked_count"] > 0 or item["failed_count"] >= 5:
            suspicious.append(item)
    return {
        "window_last_15m": {
            "total": _count(since_iso=last_15m),
            "failed": _count(status="failed", since_iso=last_15m),
            "blocked": _count(status="blocked", since_iso=last_15m),
            "success": _count(status="success", since_iso=last_15m),
        },
        "window_last_1h": {
            "total": _count(since_iso=last_1h),
            "failed": _count(status="failed", since_iso=last_1h),
            "blocked": _count(status="blocked", since_iso=last_1h),
            "success": _count(status="success", since_iso=last_1h),
        },
        "top_remotes": top_remotes,
        "suspicious_remotes": suspicious,
        "recent_attempts": recent,
    }
