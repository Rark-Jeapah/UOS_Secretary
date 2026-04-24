from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dateutil import parser as dt_parser

from sidae_secretary.config import Settings
from sidae_secretary.connectors.uos_openapi import (
    uos_openapi_timetable_configured,
    uos_openapi_uses_official_catalog_mode,
)
from sidae_secretary.connectors.uos_portal import (
    UOS_PORTAL_PROVIDER,
    UOS_PORTAL_SCHOOL_SLUG,
)
from sidae_secretary.db import Database
from sidae_secretary.secret_store import StoredSecretRef, default_secret_store


TELEGRAM_SETUP_PORTAL_STALE_DAYS = 14
TELEGRAM_SETUP_UCLASS_STALE_DAYS = 30


@dataclass(frozen=True)
class ChatLmsConnectionSnapshot:
    owner_id: int | None
    all_labels: tuple[str, ...]
    uclass_labels: tuple[str, ...]
    portal_labels: tuple[str, ...]
    moodle_connections: tuple[dict[str, Any], ...]
    browser_sessions: tuple[dict[str, Any], ...]
    portal_sessions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class TelegramSetupState:
    allowed: bool
    official_catalog_mode: bool
    uclass_labels: tuple[str, ...]
    portal_labels: tuple[str, ...]
    online_connection_level: str
    uclass_account_level: str
    portal_level: str
    local_llm_ready: bool
    core_ready: bool
    legacy_uclass_account: bool
    has_moodle_connection: bool
    show_official_api_connection: bool
    uclass_notes: tuple[str, ...]
    portal_notes: tuple[str, ...]


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


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


def _format_status_time(value: str | None, timezone_name: str) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return "기록 없음"
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M %Z")


def is_uos_portal_browser_session(item: dict[str, Any] | None) -> bool:
    payload = item or {}
    school_slug = str(payload.get("school_slug") or "").strip().lower()
    provider = str(payload.get("provider") or "").strip().lower()
    return school_slug == UOS_PORTAL_SCHOOL_SLUG or provider == UOS_PORTAL_PROVIDER


def chat_lms_connection_snapshot(db: Database, chat_id: str | None) -> ChatLmsConnectionSnapshot:
    chat = str(chat_id or "").strip()
    if not chat:
        return ChatLmsConnectionSnapshot(
            owner_id=None,
            all_labels=(),
            uclass_labels=(),
            portal_labels=(),
            moodle_connections=(),
            browser_sessions=(),
            portal_sessions=(),
        )
    user = db.get_user_by_chat_id(chat)
    owner_id = int(user["id"]) if user else None
    output: list[str] = []
    uclass_labels: list[str] = []
    portal_labels: list[str] = []
    moodle_connections = tuple(
        dict(item)
        for item in db.list_moodle_connections(chat_id=chat, user_id=owner_id, status="active", limit=10)
    )
    browser_sessions = tuple(
        dict(item)
        for item in db.list_lms_browser_sessions(chat_id=chat, user_id=owner_id, status="active", limit=10)
    )
    portal_sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in moodle_connections:
        display = str(item.get("display_name") or item.get("school_slug") or "").strip()
        username = str(item.get("username") or "").strip()
        if display and username:
            label = f"{display} ({username})"
        elif display:
            label = display
        else:
            continue
        if label not in seen:
            output.append(label)
            uclass_labels.append(label)
            seen.add(label)
    for item in browser_sessions:
        display = str(item.get("display_name") or item.get("school_slug") or "").strip()
        if not display:
            continue
        provider = str(item.get("provider") or "").strip().lower()
        label_suffix = "포털 세션" if provider == UOS_PORTAL_PROVIDER else "연결 세션"
        label = f"{display} ({label_suffix})"
        if label not in seen:
            output.append(label)
            seen.add(label)
        if provider == UOS_PORTAL_PROVIDER or is_uos_portal_browser_session(item):
            portal_labels.append(label)
            portal_sessions.append(item)
        else:
            uclass_labels.append(label)
    return ChatLmsConnectionSnapshot(
        owner_id=owner_id,
        all_labels=tuple(output),
        uclass_labels=tuple(uclass_labels),
        portal_labels=tuple(portal_labels),
        moodle_connections=moodle_connections,
        browser_sessions=browser_sessions,
        portal_sessions=tuple(portal_sessions),
    )


def sync_dashboard_source_card(
    db: Database,
    source_key: str,
    *,
    user_id: int | None = None,
    allow_global_fallback: bool = True,
) -> dict[str, Any]:
    snapshot = db.sync_dashboard_snapshot(user_id=user_id)
    card_map = {
        str(item.get("key") or "").strip(): item
        for item in list(snapshot.get("sources") or [])
        if isinstance(item, dict)
    }
    card = dict(card_map.get(source_key) or {})
    owner_id = _safe_int(user_id)
    if owner_id and allow_global_fallback:
        fallback_snapshot = db.sync_dashboard_snapshot()
        fallback_map = {
            str(item.get("key") or "").strip(): item
            for item in list(fallback_snapshot.get("sources") or [])
            if isinstance(item, dict)
        }
        fallback = dict(fallback_map.get(source_key) or {})
        if (
            str(card.get("status") or "").strip().lower() == "never"
            and fallback
            and (fallback.get("last_run_at") or fallback.get("last_success_at") or fallback.get("last_error"))
        ):
            card = fallback
    return card or {
        "key": source_key,
        "status": "never",
        "last_run_at": None,
        "last_success_at": None,
        "last_error": None,
        "action_required": 0,
    }


def looks_like_auth_or_session_issue(message: str | None) -> bool:
    lowered = str(message or "").strip().lower()
    if not lowered:
        return False
    keywords = (
        "auth",
        "login",
        "token",
        "password",
        "session",
        "invalid",
        "expired",
        "로그인",
        "세션",
        "비밀번호",
        "인증",
        "재연결",
    )
    return any(keyword in lowered for keyword in keywords)


def looks_like_secure_storage_missing(message: str | None) -> bool:
    lowered = str(message or "").strip().lower()
    if not lowered:
        return False
    hints = (
        "seckeychainsearchcopynext",
        "could not be found in the keychain",
        "stored secret file is missing",
        "missing from secure storage",
    )
    return any(hint in lowered for hint in hints)


def safe_read_secret_ref(
    settings: Settings,
    *,
    secret_kind: str | None,
    secret_ref: str | None,
) -> tuple[str, str]:
    kind = str(secret_kind or "").strip()
    ref = str(secret_ref or "").strip()
    if not kind or not ref:
        return "", ""
    try:
        secret = default_secret_store(settings).read_secret(
            ref=StoredSecretRef(kind=kind, ref=ref)
        )
    except Exception as exc:
        return "", str(exc).strip()
    return str(secret or ""), ""


def _is_setup_stale(value: str | None, *, max_days: int) -> bool:
    parsed = _parse_dt(value)
    if parsed is None:
        return False
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)) > timedelta(days=max(int(max_days), 1))


def _sync_card_error_is_stale_after_reconnect(card: dict[str, Any], *, last_verified_at: str) -> bool:
    verified_at = _parse_dt(last_verified_at)
    if verified_at is None:
        return False
    last_run_at = _parse_dt(str(card.get("last_run_at") or "").strip())
    if last_run_at is None:
        return True
    return verified_at > last_run_at


def evaluate_uclass_setup_health(
    *,
    uclass_labels: tuple[str, ...],
    moodle_connections: tuple[dict[str, Any], ...],
    browser_sessions: tuple[dict[str, Any], ...],
    legacy_uclass_account: bool,
    uclass_card: dict[str, Any],
    timezone_name: str,
    secure_storage_missing: bool = False,
) -> tuple[str, str, tuple[str, ...]]:
    has_online_connection = bool(uclass_labels)
    connection_level = "OK" if has_online_connection else "TODO"
    account_level = "OK" if has_online_connection or legacy_uclass_account else "TODO"
    notes: list[str] = []
    last_verified_at = ""
    for item in [*moodle_connections, *browser_sessions]:
        verified_at = str(item.get("last_verified_at") or "").strip()
        if verified_at and (not last_verified_at or verified_at > last_verified_at):
            last_verified_at = verified_at

    status = str(uclass_card.get("status") or "").strip().lower()
    last_error = str(uclass_card.get("last_error") or "").strip()
    if secure_storage_missing:
        connection_level = "WARN" if has_online_connection else connection_level
        account_level = "WARN"
        notes.append("- 저장된 온라인강의실 연결을 다시 확인해야 합니다. `/connect`로 다시 연결해 주세요.")
    elif (
        status == "error"
        and (has_online_connection or legacy_uclass_account)
        and not _sync_card_error_is_stale_after_reconnect(uclass_card, last_verified_at=last_verified_at)
    ):
        connection_level = "WARN" if has_online_connection else connection_level
        account_level = "WARN"
        notes.append("- 온라인강의실 동기화에 문제가 있었습니다.")
        if looks_like_auth_or_session_issue(last_error):
            notes.append("- 로그인 정보가 바뀌었다면 `/connect`로 학교 계정을 다시 연결해 주세요.")
    elif has_online_connection and not uclass_card.get("last_success_at") and not last_verified_at:
        connection_level = "WARN"
        account_level = "WARN"
        notes.append("- 학교 계정은 연결됐지만 아직 첫 동기화 확인 기록이 없습니다.")
    elif has_online_connection and _is_setup_stale(last_verified_at, max_days=TELEGRAM_SETUP_UCLASS_STALE_DAYS):
        connection_level = "WARN"
        account_level = "WARN"
        notes.append(
            "- 온라인강의실 연결 확인이 오래됐습니다: "
            + _format_status_time(last_verified_at, timezone_name)
        )
    return connection_level, account_level, tuple(notes)


def evaluate_portal_setup_health(
    *,
    portal_sessions: tuple[dict[str, Any], ...],
    portal_card: dict[str, Any],
    timezone_name: str,
    secure_storage_missing: bool = False,
    official_catalog_mode: bool = False,
    has_uclass_connection: bool = False,
) -> tuple[str, tuple[str, ...]]:
    if official_catalog_mode:
        status = str(portal_card.get("status") or "").strip().lower()
        last_success_at = str(portal_card.get("last_success_at") or "").strip()
        if not has_uclass_connection:
            return (
                "TODO",
                ("- 학교 계정을 연결하면 시간표를 자동으로 불러옵니다.",),
            )
        if status == "error":
            return (
                "WARN",
                ("- 학교 시간표 동기화에 문제가 있었습니다.",),
            )
        if status == "skipped":
            return (
                "WARN",
                ("- 학교 시간표 동기화가 아직 완료되지 않았습니다.",),
            )
        if status == "success" or last_success_at:
            return "OK", ()
        return (
            "WARN",
            ("- 학교 공식 시간표 동기화 기록이 아직 없습니다. 다음 sync에서 자동 확인합니다.",),
        )
    if not portal_sessions:
        return (
            "TODO",
            ("- `/connect`로 학교 계정을 연결하면 시간표 연결도 같이 준비합니다.",),
        )
    latest = portal_sessions[0]
    metadata = dict(latest.get("metadata_json") or {}) if isinstance(latest.get("metadata_json"), dict) else {}
    portal_sync = (
        dict(metadata.get("portal_timetable_sync"))
        if isinstance(metadata.get("portal_timetable_sync"), dict)
        else {}
    )
    last_verified_at = str(latest.get("last_verified_at") or "").strip()
    status = str(portal_card.get("status") or "").strip().lower()
    last_error = str(portal_card.get("last_error") or "").strip()
    auth_required = bool(portal_sync.get("auth_required"))
    notes: list[str] = []
    level = "OK"
    if secure_storage_missing:
        level = "WARN"
        notes.append("- 저장된 시간표 연결을 다시 확인해야 합니다. `/connect`로 다시 연결해 주세요.")
    elif auth_required:
        level = "WARN"
        notes.append("- 시간표 연결이 만료된 것 같습니다. `/connect`로 다시 연결해 주세요.")
    elif status == "error":
        level = "WARN"
        notes.append("- 시간표 확인에 문제가 있었습니다.")
        if looks_like_auth_or_session_issue(last_error):
            notes.append("- `/connect`로 포털 세션을 다시 연결해 주세요.")
    elif str(portal_sync.get("status") or "").strip().lower() == "skipped":
        level = "WARN"
        notes.append("- 최근 시간표 확인이 완료되지 않았습니다. `/connect`로 다시 연결해 주세요.")
    elif not str(portal_sync.get("last_synced_at") or "").strip() and not portal_card.get("last_success_at"):
        level = "WARN"
        notes.append("- 시간표 연결은 저장됐지만 아직 첫 확인 기록이 없습니다.")
    elif not portal_card.get("last_success_at") and not last_verified_at:
        level = "WARN"
        notes.append("- 시간표 연결은 저장됐지만 아직 첫 확인 기록이 없습니다.")
    elif _is_setup_stale(last_verified_at, max_days=TELEGRAM_SETUP_PORTAL_STALE_DAYS):
        level = "WARN"
        notes.append(
            "- 시간표 연결 확인이 오래됐습니다: "
            + _format_status_time(last_verified_at, timezone_name)
        )
    return level, tuple(notes)


def build_telegram_setup_state(
    settings: Settings,
    *,
    db: Database,
    allowed: bool,
    chat_id: str | None = None,
    user_id: int | None = None,
    read_secret_ref: Callable[[str | None, str | None], tuple[str, str]] | None = None,
) -> TelegramSetupState:
    connection_snapshot = chat_lms_connection_snapshot(db, chat_id)
    official_catalog_mode = uos_openapi_timetable_configured(
        str(getattr(settings, "uos_openapi_timetable_url", "") or "").strip(),
        str(getattr(settings, "uos_openapi_timetable_api_key", "") or "").strip() or None,
    ) and uos_openapi_uses_official_catalog_mode(
        str(getattr(settings, "uos_openapi_timetable_url", "") or "").strip()
    )
    uclass_labels = connection_snapshot.uclass_labels
    portal_labels = () if official_catalog_mode else connection_snapshot.portal_labels
    moodle_connections = connection_snapshot.moodle_connections
    browser_sessions = connection_snapshot.browser_sessions
    portal_sessions = () if official_catalog_mode else connection_snapshot.portal_sessions
    owner_id = _safe_int(user_id)
    secret_ref_reader = read_secret_ref or (
        lambda secret_kind, secret_ref: safe_read_secret_ref(
            settings,
            secret_kind=secret_kind,
            secret_ref=secret_ref,
        )
    )
    legacy_uclass_account = bool(str(getattr(settings, "uclass_wstoken", "") or "").strip())
    uclass_card = sync_dashboard_source_card(
        db,
        "uclass",
        user_id=owner_id,
        allow_global_fallback=False,
    )
    portal_card = sync_dashboard_source_card(
        db,
        "portal",
        user_id=owner_id,
        allow_global_fallback=False,
    )
    uclass_secret_missing = any(
        bool(
            secret_ref_reader(
                str(item.get("secret_kind") or ""),
                str(item.get("secret_ref") or ""),
            )[1]
        )
        for item in moodle_connections
    )
    portal_secret_missing = False if official_catalog_mode else any(
        bool(
            secret_ref_reader(
                str(item.get("secret_kind") or ""),
                str(item.get("secret_ref") or ""),
            )[1]
        )
        for item in portal_sessions
        if not (
            str(item.get("profile_dir") or "").strip()
            and Path(str(item.get("profile_dir") or "")).expanduser().exists()
        )
    )
    online_connection_level, uclass_account_level, uclass_notes = evaluate_uclass_setup_health(
        uclass_labels=uclass_labels,
        moodle_connections=moodle_connections,
        browser_sessions=tuple(
            item for item in browser_sessions if not is_uos_portal_browser_session(item)
        ),
        legacy_uclass_account=legacy_uclass_account,
        uclass_card=uclass_card,
        timezone_name=str(getattr(settings, "timezone", "Asia/Seoul") or "Asia/Seoul"),
        secure_storage_missing=uclass_secret_missing,
    )
    portal_level, portal_notes = evaluate_portal_setup_health(
        portal_sessions=portal_sessions,
        portal_card=portal_card,
        timezone_name=str(getattr(settings, "timezone", "Asia/Seoul") or "Asia/Seoul"),
        secure_storage_missing=portal_secret_missing,
        official_catalog_mode=official_catalog_mode,
        has_uclass_connection=bool(uclass_labels or moodle_connections or legacy_uclass_account),
    )
    llm_provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
    local_endpoint = str(getattr(settings, "llm_local_endpoint", "") or "").strip()
    local_llm_ready = llm_provider == "local" and bool(local_endpoint)
    core_ready = all(
        [
            allowed,
            online_connection_level == "OK",
            uclass_account_level == "OK",
            portal_level == "OK",
        ]
    )
    return TelegramSetupState(
        allowed=allowed,
        official_catalog_mode=official_catalog_mode,
        uclass_labels=uclass_labels,
        portal_labels=portal_labels,
        online_connection_level=online_connection_level,
        uclass_account_level=uclass_account_level,
        portal_level=portal_level,
        local_llm_ready=local_llm_ready,
        core_ready=core_ready,
        legacy_uclass_account=legacy_uclass_account,
        has_moodle_connection=bool(moodle_connections),
        show_official_api_connection=official_catalog_mode and bool(uclass_labels or moodle_connections),
        uclass_notes=uclass_notes,
        portal_notes=portal_notes,
    )
