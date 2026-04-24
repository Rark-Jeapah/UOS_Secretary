from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
from typing import Any, Callable
from urllib.parse import parse_qsl
from urllib.parse import urlparse

from sidae_secretary.db import Database, normalize_course_alias
from sidae_secretary.secret_store import SecretStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MoodleConnectFormData:
    token: str
    school_name: str
    username: str
    password: str


@dataclass(frozen=True)
class PortalLoginState:
    existing_portal_session: dict[str, Any] | None
    profile_dir: Path | None
    result: dict[str, Any] | None
    error: str


@dataclass(frozen=True)
class SchoolAccountFinalizeResult:
    connection: dict[str, Any]
    portal_browser_session: dict[str, Any] | None
    portal_prime_result: dict[str, Any] | None
    portal_login_error: str


def parse_moodle_connect_form(raw: bytes) -> MoodleConnectFormData:
    form = dict(parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
    return MoodleConnectFormData(
        token=str(form.get("token") or "").strip(),
        school_name=str(form.get("school_name") or "").strip(),
        username=str(form.get("username") or "").strip(),
        password=str(form.get("password") or ""),
    )


def validate_moodle_connect_form(form: MoodleConnectFormData) -> str | None:
    if not form.school_name or not form.username or not form.password:
        return "missing_fields"
    return None


def connect_plan_entries(
    connect_plan: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    payload = connect_plan if isinstance(connect_plan, dict) else {}
    directory_entry = payload.get("school_entry") if isinstance(payload.get("school_entry"), dict) else None
    portal_entry = payload.get("portal_entry") if isinstance(payload.get("portal_entry"), dict) else None
    portal_info = payload.get("portal_info") if isinstance(payload.get("portal_info"), dict) else None
    return directory_entry, portal_entry, portal_info


def connect_plan_school_slug(connect_plan: dict[str, Any] | None) -> str | None:
    directory_entry, portal_entry, _ = connect_plan_entries(connect_plan)
    school_slug = str((directory_entry or portal_entry or {}).get("school_slug") or "").strip().lower()
    return school_slug or None


def apply_directory_entry_overrides(
    resolved: dict[str, Any],
    directory_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    output = dict(resolved or {})
    if not directory_entry:
        return output
    output["school_slug"] = str(directory_entry.get("school_slug") or output.get("school_slug") or "")
    output["display_name"] = str(directory_entry.get("display_name") or output.get("display_name") or "")
    output["ws_base_url"] = str(directory_entry.get("ws_base_url") or output.get("ws_base_url") or "")
    return output


def _directory_entry_tokens(entry: dict[str, Any]) -> list[str]:
    return [
        str(entry.get("display_name") or ""),
        str(entry.get("school_slug") or ""),
        *[str(item) for item in list(entry.get("aliases") or [])],
    ]


def resolve_moodle_connect_target(
    db: Database,
    *,
    school_name: str,
    allowed_school_slugs: set[str] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    query = str(school_name or "").strip()
    normalized_query = normalize_course_alias(query)
    if not normalized_query:
        raise ValueError("학교를 선택하세요.")
    matches = db.find_moodle_school_directory(query, limit=5)
    allowed = {str(item).strip().lower() for item in list(allowed_school_slugs or set()) if str(item).strip()}
    if allowed:
        matches = [
            entry
            for entry in matches
            if str(entry.get("school_slug") or "").strip().lower() in allowed
        ]
    if not matches:
        raise ValueError("등록된 학교를 찾지 못했습니다. 목록에서 학교를 다시 선택하세요.")
    exact_matches = [
        entry
        for entry in matches
        if any(
            normalize_course_alias(token) == normalized_query
            for token in _directory_entry_tokens(entry)
        )
    ]
    entry = exact_matches[0] if exact_matches else matches[0]
    return str(entry.get("ws_base_url") or ""), entry


def entry_metadata(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    metadata = entry.get("metadata_json")
    return metadata if isinstance(metadata, dict) else {}


def portal_info_from_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    metadata = entry_metadata(entry)
    portal = metadata.get("portal")
    if not isinstance(portal, dict):
        return None
    login_url = str(portal.get("login_url") or "").strip()
    if not login_url:
        return None
    info: dict[str, Any] = {
        "display_name": str(portal.get("display_name") or "학교 포털").strip() or "학교 포털",
        "login_url": login_url,
        "homepage_url": str(portal.get("homepage_url") or login_url).strip() or login_url,
        "source_url": str(portal.get("source_url") or login_url).strip() or login_url,
        "source_note": str(portal.get("source_note") or "").strip() or None,
        "constraints": str(portal.get("constraints") or "").strip() or None,
        "timetable_support": str(portal.get("timetable_support") or "").strip() or "planned",
        "shared_school_account": bool(portal.get("shared_school_account", True)),
    }
    return info


def is_uos_school_entry(
    entry: dict[str, Any] | None,
    *,
    uos_school_account_slugs: set[str] | None = None,
) -> bool:
    if not isinstance(entry, dict):
        return False
    allowed_slugs = {str(item).strip().lower() for item in list(uos_school_account_slugs or {"uos_portal", "uos_online_class"})}
    return str(entry.get("school_slug") or "").strip().lower() in allowed_slugs


def _is_uos_lms_url(ws_base_url: str) -> bool:
    parsed = urlparse(str(ws_base_url or "").strip())
    return str(parsed.netloc or "").strip().lower() == "uclass.uos.ac.kr"


def _get_directory_school_entry_by_slug(
    db: Database,
    school_slug: str,
) -> dict[str, Any] | None:
    slug = str(school_slug or "").strip().lower()
    if not slug:
        return None
    for entry in db.list_moodle_school_directory(limit=2000):
        if str(entry.get("school_slug") or "").strip().lower() == slug:
            return entry
    return None


def resolve_school_account_connect_plan(
    db: Database,
    *,
    school_name: str,
    allowed_school_slugs: set[str] | None = None,
    uos_online_class_school_slug: str = "uos_online_class",
    uos_portal_school_slug: str = "uos_portal",
) -> dict[str, Any]:
    resolved_ws_base_url, directory_entry = resolve_moodle_connect_target(
        db,
        school_name=school_name,
        allowed_school_slugs=allowed_school_slugs,
    )
    uos_school_account_slugs = {
        str(uos_online_class_school_slug or "").strip().lower(),
        str(uos_portal_school_slug or "").strip().lower(),
    }
    plan: dict[str, Any] = {
        "moodle_ws_base_url": resolved_ws_base_url,
        "school_entry": directory_entry,
        "portal_entry": None,
        "portal_info": None,
        "bundle_kind": None,
    }
    if is_uos_school_entry(directory_entry, uos_school_account_slugs=uos_school_account_slugs) or _is_uos_lms_url(
        resolved_ws_base_url
    ):
        moodle_entry = _get_directory_school_entry_by_slug(db, uos_online_class_school_slug)
        portal_entry = _get_directory_school_entry_by_slug(db, uos_portal_school_slug)
        if moodle_entry and str(moodle_entry.get("ws_base_url") or "").strip():
            plan["moodle_ws_base_url"] = str(moodle_entry.get("ws_base_url") or "").strip()
            plan["school_entry"] = moodle_entry
        plan["portal_entry"] = portal_entry
        plan["portal_info"] = (
            portal_info_from_entry(moodle_entry)
            or portal_info_from_entry(portal_entry)
        )
        plan["bundle_kind"] = "uos_school_account"
    elif directory_entry:
        portal_info = portal_info_from_entry(directory_entry)
        if portal_info:
            plan["portal_info"] = portal_info
            plan["bundle_kind"] = "shared_school_account"
    return plan


def safe_failure_reason(exc: Exception) -> str:
    message = str(exc or "").strip()
    lowered = message.lower()
    if any(token in lowered for token in ("login failed", "invalidlogin", "invalid token", "access denied")):
        return "login_failed"
    return "connect_failed"


def public_error_message(reason: str) -> str:
    if reason == "rate_limited":
        return "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요."
    if reason == "school_not_found":
        return "등록된 학교를 찾지 못했습니다. 목록에서 학교를 다시 선택하세요."
    if reason == "missing_fields":
        return "학교, ID, 비밀번호를 모두 입력하세요."
    if reason == "login_failed":
        return "로그인에 실패했습니다. 학교 계정과 비밀번호를 다시 확인하세요."
    return "연결에 실패했습니다. 잠시 후 다시 시도하세요."


def portal_prime_requires_reconnect(result: dict[str, Any] | None) -> bool:
    payload = result if isinstance(result, dict) else {}
    reason = str(payload.get("reason") or payload.get("error") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    if reason == "UOS portal session expired; reconnect required":
        return True
    if status == "error" and bool(reason):
        return True
    return False


def portal_prime_warning_lines(result: dict[str, Any] | None) -> list[str]:
    payload = result if isinstance(result, dict) else {}
    reason = str(payload.get("reason") or payload.get("error") or "").strip()
    if not reason:
        return []
    if reason == "UOS portal session expired; reconnect required":
        return [
            "- 포털 시간표는 바로 확인되지 않았습니다.",
            "- `/connect`로 다시 연결해 주세요.",
        ]
    return [f"- 포털 시간표 확인: {reason}"]


def portal_connect_retry_lines() -> list[str]:
    return [
        "- 비밀번호와 온라인강의실 접근 token을 이 사용자용 보안 저장소에 저장했습니다.",
        "- 포털 시간표 연결은 바로 확인하지 못했습니다.",
        "- `/connect`로 다시 연결해 주세요.",
    ]


def school_account_success_display_name(
    *,
    connection: dict[str, Any],
    portal_browser_session: dict[str, Any] | None,
) -> str:
    if str(connection.get("school_slug") or "").strip().lower() == "uos_online_class":
        return "서울시립대학교 학교 계정"
    if portal_browser_session:
        return "서울시립대학교 학교 계정"
    return str(connection["display_name"])


def _has_connection_login_secret(connection: dict[str, Any]) -> bool:
    return bool(
        str(connection.get("login_secret_kind") or "").strip()
        and str(connection.get("login_secret_ref") or "").strip()
    )


def build_school_account_completion_message(
    *,
    connection: dict[str, Any],
    directory_entry: dict[str, Any] | None,
    portal_info: dict[str, Any] | None,
    portal_browser_session: dict[str, Any] | None,
    portal_login_error: str,
    portal_prime_result: dict[str, Any] | None,
    school_support_summary_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    password_storage_line = (
        "- 비밀번호와 온라인강의실 접근 token을 이 사용자용 보안 저장소에 저장했습니다."
        if _has_connection_login_secret(connection)
        else "- 온라인강의실 접근 token만 이 사용자용으로 저장했습니다."
    )
    if str(connection.get("school_slug") or "").strip().lower() == "uos_online_class":
        message_lines = [
            "[Sidae] 학교 계정 연결 완료",
            "",
            "- 학교: 서울시립대학교",
            f"- 온라인강의실: {connection['display_name']}",
            "- 시간표: 학교 공식 API 자동 동기화",
            f"- ID: {connection['username']}",
            password_storage_line,
        ]
        payload = portal_prime_result if isinstance(portal_prime_result, dict) else {}
        status = str(payload.get("status") or "").strip().lower()
        skipped = bool(payload.get("skipped"))
        reason = str(payload.get("reason") or payload.get("error") or "").strip()
        if reason and status == "error" and not skipped:
            message_lines.append(f"- 시간표 동기화 확인: {reason}")
        return "\n".join(message_lines)

    support = school_support_summary_fn(
        directory_entry
        or {
            "school_slug": connection["school_slug"],
            "display_name": connection["display_name"],
        }
    )
    if portal_browser_session:
        message_lines = [
            "[Sidae] 학교 계정 연결 완료",
            "",
            "- 학교: 서울시립대학교",
            f"- 온라인강의실: {connection['display_name']}",
            f"- 포털/대학행정: {portal_browser_session['display_name']}",
            f"- ID: {connection['username']}",
        ]
        if portal_prime_requires_reconnect(portal_prime_result):
            message_lines.append(
                "- 온라인강의실 접근 token과 포털 브라우저 세션을 이 사용자용으로 저장했습니다."
            )
            message_lines.extend(portal_prime_warning_lines(portal_prime_result))
        else:
            message_lines.append(
                "- 온라인강의실 접근 token과 포털 세션을 이 사용자용으로 저장했습니다."
            )
        return "\n".join(message_lines)
    if portal_info and portal_login_error:
        return "\n".join(
            [
                "[Sidae] 학교 계정 연결 완료",
                "",
                "- 학교: 서울시립대학교",
                f"- 온라인강의실: {connection['display_name']}",
                f"- 포털/대학행정: {portal_info['display_name']}",
                f"- ID: {connection['username']}",
                *portal_connect_retry_lines(),
            ]
        )
    if portal_info:
        message_lines = [
            "[Sidae] 학교 계정 연결 완료",
            "",
            f"- 학교: {connection['display_name']}",
            f"- 포털: {portal_info['display_name']}",
            f"- 포털 로그인: {portal_info['login_url']}",
            f"- ID: {connection['username']}",
            "- 온라인강의실 접근 token을 이 사용자용 보안 저장소에 저장했습니다.",
        ]
        timetable_support = str(portal_info.get("timetable_support") or "").strip().lower()
        constraints = str(portal_info.get("constraints") or "").strip()
        if timetable_support == "supported":
            message_lines.append("- 이 학교는 포털 세션과 시간표 자동 연동까지 구현되어 있습니다.")
        else:
            message_lines.append("- 이 학교는 같은 계정으로 포털을 사용합니다.")
            if constraints:
                message_lines.append(f"- 제약: {constraints}")
            if not bool(support.get("official_user_support")):
                message_lines.append("- 현재 사용자-facing 공식 지원 학교는 서울시립대학교입니다.")
        return "\n".join(message_lines)
    message_lines = [
        "[Sidae] 학교 계정 연결 완료",
        "",
        f"- 학교: {connection['display_name']}",
        f"- ID: {connection['username']}",
        "- 접근 token을 이 사용자용 보안 저장소에 저장했습니다.",
    ]
    if not bool(support.get("official_user_support")):
        message_lines.append("- 현재 사용자-facing 공식 지원 학교는 서울시립대학교입니다.")
    return "\n".join(message_lines)


def login_uos_portal_for_school_account(
    *,
    db: Database,
    session: dict[str, Any],
    portal_entry: dict[str, Any] | None,
    username: str,
    password: str,
    settings: Any,
    browser_channel: str,
    browser_executable_path: Any,
    browser_headless: bool,
    build_browser_profile_dir: Callable[..., Path],
    portal_login_browser_session: Callable[..., dict[str, Any]],
    uos_portal_provider: str,
    uos_portal_school_slug: str,
) -> PortalLoginState:
    if not portal_entry:
        return PortalLoginState(
            existing_portal_session=None,
            profile_dir=None,
            result=None,
            error="",
        )
    existing_portal_session = db.get_lms_browser_session(
        chat_id=str(session["chat_id"] or ""),
        school_slug=uos_portal_school_slug,
    )
    profile_dir = build_browser_profile_dir(
        chat_id=str(session["chat_id"] or ""),
        provider=uos_portal_provider,
        school_slug=uos_portal_school_slug,
    )
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    try:
        return PortalLoginState(
            existing_portal_session=existing_portal_session,
            profile_dir=profile_dir,
            result=portal_login_browser_session(
                username=username,
                password=password,
                profile_dir=profile_dir,
                prefetch_timetable=True,
                timezone_name=str(getattr(settings, "timezone", "Asia/Seoul") or "Asia/Seoul"),
                browser_channel=browser_channel,
                browser_executable_path=browser_executable_path,
                headless=browser_headless,
            ),
            error="",
        )
    except Exception as exc:
        error_text = str(exc).strip() or "UOS portal login failed"
        logger.info("uos portal login failed during school account onboarding", exc_info=True)
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            logger.info("failed to remove broken UOS portal profile dir", exc_info=True)
        return PortalLoginState(
            existing_portal_session=existing_portal_session,
            profile_dir=profile_dir,
            result=None,
            error=error_text,
        )


def persist_uos_portal_session_for_school_account(
    *,
    settings: Any,
    db: Database,
    session: dict[str, Any],
    connection: dict[str, Any],
    portal_entry: dict[str, Any] | None,
    portal_state: PortalLoginState,
    username: str,
    sanitize_browser_result: Callable[[dict[str, Any]], dict[str, Any]],
    prime_post_connect_portal_sync: Callable[..., dict[str, Any]],
    now_utc_iso_fn: Callable[[], str],
    uos_portal_provider: str,
    uos_portal_school_slug: str,
    uos_portal_login_url: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not portal_entry or not portal_state.result:
        return None, None
    stored_profile_dir = str(
        portal_state.result.get("profile_dir")
        or portal_state.profile_dir
        or ""
    ).strip()
    portal_browser_session = db.upsert_lms_browser_session(
        chat_id=str(session["chat_id"] or ""),
        school_slug=uos_portal_school_slug,
        provider=uos_portal_provider,
        display_name=str(portal_entry.get("display_name") or "서울시립대학교 포털/대학행정"),
        login_url=uos_portal_login_url,
        profile_dir=stored_profile_dir,
        secret_kind=None,
        secret_ref=None,
        status="active",
        last_opened_at=now_utc_iso_fn(),
        last_verified_at=now_utc_iso_fn(),
        metadata_json={
            "auth_mode": "browser_session",
            "source": "school_account_onboarding",
            "username": username,
            "onboarding_session_id": session["id"],
            "browser_result": sanitize_browser_result(portal_state.result),
            "session_storage": "profile_dir",
            "manual_confirmation": False,
        },
    )
    legacy_profile_dir = str((portal_state.existing_portal_session or {}).get("profile_dir") or "").strip()
    if legacy_profile_dir and legacy_profile_dir != stored_profile_dir:
        try:
            shutil.rmtree(legacy_profile_dir, ignore_errors=True)
        except Exception:
            logger.info("failed to remove legacy UOS portal profile dir", exc_info=True)
    portal_prime_result = prime_post_connect_portal_sync(
        settings=settings,
        db=db,
        chat_id=str(session["chat_id"] or ""),
        user_id=int(connection.get("user_id") or 0) or None,
        fetched=(
            dict(portal_state.result.get("timetable_fetch"))
            if isinstance(portal_state.result.get("timetable_fetch"), dict)
            else None
        ),
    )
    return portal_browser_session, portal_prime_result


def finalize_school_account_connection(
    *,
    db: Database,
    store: SecretStore,
    settings: Any,
    session: dict[str, Any],
    connect_plan: dict[str, Any],
    school_name: str,
    username: str,
    password: str,
    resolved: dict[str, Any],
    browser_channel: str,
    browser_executable_path: Any,
    browser_headless: bool,
    build_browser_profile_dir: Callable[..., Path],
    portal_login_browser_session: Callable[..., dict[str, Any]],
    prime_post_connect_portal_sync: Callable[..., dict[str, Any]],
    sanitize_browser_result: Callable[[dict[str, Any]], dict[str, Any]],
    now_utc_iso_fn: Callable[[], str],
    uos_portal_provider: str,
    uos_portal_school_slug: str,
    uos_portal_login_url: str,
) -> SchoolAccountFinalizeResult:
    directory_entry, portal_entry, portal_info = connect_plan_entries(connect_plan)
    secret_key = f"telegram:{session['chat_id']}:moodle:{resolved['school_slug']}"
    stored = store.store_secret(
        key=secret_key,
        secret=str(resolved["token"] or ""),
    )
    connection = db.upsert_moodle_connection(
        chat_id=str(session["chat_id"] or ""),
        school_slug=str(resolved["school_slug"] or ""),
        display_name=str(resolved["display_name"] or ""),
        ws_base_url=str(resolved["ws_base_url"] or ""),
        username=str(resolved["username"] or ""),
        secret_kind=stored.kind,
        secret_ref=stored.ref,
        login_secret_kind=None,
        login_secret_ref=None,
        last_verified_at=str(resolved["verified_at"] or ""),
        metadata_json={
            "site_info": resolved.get("site_info"),
            "onboarding_session_id": session["id"],
            "bundle_kind": connect_plan.get("bundle_kind"),
            "directory_school_slug": (
                str(directory_entry.get("school_slug") or "")
                if directory_entry
                else None
            ),
            "directory_source_url": (
                str(directory_entry.get("source_url") or "")
                if directory_entry
                else None
            ),
            "portal_info": portal_info,
        },
    )
    portal_browser_session = None
    portal_prime_result = None
    portal_login_error = ""
    if str(connect_plan.get("bundle_kind") or "").strip().lower() == "uos_school_account":
        existing_portal_session = db.get_lms_browser_session(
            chat_id=str(session["chat_id"] or ""),
            school_slug=uos_portal_school_slug,
            user_id=int(connection.get("user_id") or 0) or None,
        )
        legacy_profile_dir = str((existing_portal_session or {}).get("profile_dir") or "").strip()
        db.mark_lms_browser_session_inactive(
            chat_id=str(session["chat_id"] or ""),
            school_slug=uos_portal_school_slug,
            user_id=int(connection.get("user_id") or 0) or None,
            metadata_json={
                "disabled_reason": "official_api_only",
                "disabled_at": now_utc_iso_fn(),
            },
        )
        if legacy_profile_dir:
            try:
                shutil.rmtree(legacy_profile_dir, ignore_errors=True)
            except Exception:
                logger.info("failed to remove legacy UOS portal profile dir", exc_info=True)
        portal_prime_result = prime_post_connect_portal_sync(
            settings=settings,
            db=db,
            chat_id=str(session["chat_id"] or ""),
            user_id=int(connection.get("user_id") or 0) or None,
            fetched=None,
        )
    db.mark_onboarding_session_used(
        session_id=int(session["id"]),
        metadata_json={
            "connection_id": connection["id"],
            "school_slug": connection["school_slug"],
            "school_query": school_name,
            "bundle_kind": connect_plan.get("bundle_kind"),
            "portal_browser_session_id": (
                portal_browser_session["id"]
                if isinstance(portal_browser_session, dict)
                else None
            ),
            "portal_login_error": portal_login_error or None,
        },
    )
    return SchoolAccountFinalizeResult(
        connection=connection,
        portal_browser_session=portal_browser_session,
        portal_prime_result=portal_prime_result,
        portal_login_error=portal_login_error,
    )
