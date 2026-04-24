from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading
import time
import socketserver
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from sidae_secretary.browser_session import (
    InteractiveBrowserSession,
    browser_session_profile_dir,
    sanitize_browser_session_result,
)
from sidae_secretary.connectors.telegram import TelegramBotClient
from sidae_secretary.connectors.uos_portal import (
    UOS_PORTAL_LOGIN_URL,
    UOS_PORTAL_PROVIDER,
    UOS_PORTAL_SCHOOL_SLUG,
    login_uos_portal_browser_session,
)
from sidae_secretary.connectors.uclass import (
    MoodleWSClient,
    request_moodle_mobile_launch_token,
    request_moodle_ws_token,
)
from sidae_secretary.db import Database, now_utc_iso
from sidae_secretary.onboarding_render import (
    render_browser_connect_invalid,
    render_browser_connect_page,
    render_browser_frame_placeholder_svg,
    render_moodle_connect_form,
    render_moodle_connect_invalid,
    render_moodle_connect_success,
    render_portal_connect_form,
    render_portal_connect_invalid,
    render_portal_connect_success,
)
from sidae_secretary.onboarding_school_connect import (
    connect_plan_entries,
    entry_metadata as _entry_metadata,
    finalize_school_account_connection,
    is_uos_school_entry as _is_uos_school_entry,
    parse_moodle_connect_form,
    portal_info_from_entry as _portal_info_from_entry,
    resolve_school_account_connect_plan,
)
from sidae_secretary.portal_sync_service import prime_post_connect_portal_sync
from sidae_secretary.onboarding_service import OnboardingApplicationService
from sidae_secretary.school_support import school_support_summary
from sidae_secretary.secret_store import SecretStore, default_secret_store


logger = logging.getLogger(__name__)

MOODLE_CONNECT_PATH = "/moodle-connect"
MOODLE_ONBOARDING_SESSION_KIND = "moodle_connect"
PORTAL_CONNECT_PATH = "/portal-connect"
PORTAL_ONBOARDING_SESSION_KIND = "portal_connect"
BROWSER_CONNECT_PATH = "/browser-connect"
BROWSER_ONBOARDING_SESSION_KIND = "browser_connect"
UOS_ONLINE_CLASS_SCHOOL_SLUG = "uos_online_class"
UOS_SCHOOL_ACCOUNT_SLUGS = {UOS_PORTAL_SCHOOL_SLUG, UOS_ONLINE_CLASS_SCHOOL_SLUG}
REMOTE_BROWSER_IDLE_TIMEOUT_SECONDS = 20 * 60
AUTH_RATE_WINDOW_SECONDS = 15 * 60
AUTH_MAX_FAILED_PER_SESSION = 5
AUTH_MAX_FAILED_PER_REMOTE = 10
AUTH_MAX_TOTAL_PER_REMOTE = 20


class FastBindHTTPServer(ThreadingHTTPServer):
    # Avoid reverse DNS lookups during server startup. On some macOS/Tailscale setups
    # socket.getfqdn(host) can stall long enough to make the onboarding server appear dead.
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host or "localhost")
        self.server_port = int(port)


def normalize_public_moodle_connect_base_url(public_base_url: str) -> str:
    parsed = urlparse(str(public_base_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("ONBOARDING_PUBLIC_BASE_URL is invalid")
    if str(parsed.scheme or "").lower() != "https":
        raise ValueError("ONBOARDING_PUBLIC_BASE_URL must use https")
    return urlunparse((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def build_public_moodle_connect_url(public_base_url: str, token: str) -> str:
    parsed = urlparse(normalize_public_moodle_connect_base_url(public_base_url))
    base_path = parsed.path.rstrip("/")
    final_path = f"{base_path}{MOODLE_CONNECT_PATH}" if base_path else MOODLE_CONNECT_PATH
    query = urlencode({"token": str(token or "").strip()})
    return urlunparse((parsed.scheme, parsed.netloc, final_path, "", query, ""))


def normalize_moodle_ws_base(candidate_url: str) -> str:
    raw = str(candidate_url or "").strip()
    if not raw:
        raise ValueError("온라인강의실 주소를 입력하세요.")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("주소 형식이 올바르지 않습니다. 예: https://ys.learnus.org")
    path = parsed.path.rstrip("/")
    if path.endswith("/webservice/rest/server.php"):
        final_path = path
    else:
        for suffix in ("/login/index.php", "/my/courses.php", "/my", "/index.php"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        path = path.rstrip("/")
        final_path = f"{path}/webservice/rest/server.php" if path else "/webservice/rest/server.php"
    return urlunparse((parsed.scheme, parsed.netloc, final_path, "", "", ""))


def derive_moodle_school_slug(ws_base_url: str) -> str:
    parsed = urlparse(str(ws_base_url or "").strip())
    path = str(parsed.path or "").replace("/webservice/rest/server.php", "").strip("/")
    seed = parsed.netloc
    if path:
        seed = f"{seed}_{path.replace('/', '_')}"
    cleaned = []
    for ch in seed.lower():
        cleaned.append(ch if ("a" <= ch <= "z") or ("0" <= ch <= "9") else "_")
    slug = "".join(cleaned).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "moodle"


def onboarding_allowed_school_slugs(settings: Any | None) -> set[str]:
    raw = getattr(settings, "onboarding_allowed_school_slugs", []) if settings is not None else []
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    else:
        items = [str(item).strip() for item in list(raw or [])]
    allowed = {item.lower() for item in items if item}
    # UOS onboarding is a bundled school-account flow, so either slug should enable the pair.
    if allowed & UOS_SCHOOL_ACCOUNT_SLUGS:
        allowed.update(UOS_SCHOOL_ACCOUNT_SLUGS)
    return allowed


def school_entry_allowed_for_onboarding(
    entry: dict[str, Any] | None,
    *,
    settings: Any | None = None,
) -> bool:
    if not isinstance(entry, dict):
        return False
    allowed = onboarding_allowed_school_slugs(settings)
    if not allowed:
        return True
    school_slug = str(entry.get("school_slug") or "").strip().lower()
    return bool(school_slug) and school_slug in allowed


def filter_onboarding_school_entries(
    entries: list[dict[str, Any]] | None,
    *,
    settings: Any | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for entry in list(entries or []):
        if school_entry_allowed_for_onboarding(entry, settings=settings):
            output.append(entry)
    return output


def exchange_moodle_credentials(
    *,
    lms_url: str,
    username: str,
    password: str,
    request_method: str = "GET",
    site_info_wsfunction: str = "core_webservice_get_site_info",
    token_service: str = "moodle_mobile_app",
) -> dict[str, Any]:
    ws_base_url = normalize_moodle_ws_base(lms_url)
    try:
        token = request_moodle_ws_token(
            ws_base_url=ws_base_url,
            username=username,
            password=password,
            service=token_service,
        )
    except Exception:
        token = request_moodle_mobile_launch_token(
            ws_base_url=ws_base_url,
            username=username,
            password=password,
            service=token_service,
            request_method=request_method,
        )
    client = MoodleWSClient(
        base_url=ws_base_url,
        token=token,
        request_method=request_method,
    )
    site_info = client.get_site_info(site_info_wsfunction)
    school_slug = derive_moodle_school_slug(ws_base_url)
    display_name = (
        str(site_info.get("sitename") or "").strip()
        or str(site_info.get("siteurl") or "").strip()
        or school_slug
    )
    return {
        "school_slug": school_slug,
        "display_name": display_name,
        "ws_base_url": ws_base_url,
        "username": str(username or "").strip(),
        "token": token,
        "site_info": site_info if isinstance(site_info, dict) else {"raw": site_info},
        "verified_at": now_utc_iso(),
    }

def _should_hide_from_school_options(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    slug = str(entry.get("school_slug") or "").strip().lower()
    if slug == UOS_PORTAL_SCHOOL_SLUG:
        return True
    metadata = _entry_metadata(entry)
    auth_mode = str(metadata.get("auth_mode") or "").strip().lower()
    display_name = str(entry.get("display_name") or "").strip()
    return auth_mode == "browser_session" and "포털" in display_name


def visible_onboarding_school_entries(
    entries: list[dict[str, Any]] | None,
    *,
    settings: Any | None = None,
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in filter_onboarding_school_entries(entries, settings=settings)
        if not _should_hide_from_school_options(entry)
    ]


def _render_school_account_form(
    *,
    token: str,
    school_name: str = "",
    username: str = "",
    error: str | None = None,
    school_options: list[dict[str, Any]] | None = None,
    resolved_school: dict[str, Any] | None = None,
) -> bytes:
    return render_moodle_connect_form(
        token=token,
        moodle_connect_path=MOODLE_CONNECT_PATH,
        school_name=school_name,
        username=username,
        error=error,
        school_options=school_options,
        resolved_school=resolved_school,
        resolved_portal_info=_portal_info_from_entry(resolved_school),
        resolved_support=school_support_summary(resolved_school) if resolved_school else {},
        resolved_is_uos_school=_is_uos_school_entry(resolved_school),
    )


def _render_moodle_connect_form(
    *,
    token: str,
    school_name: str = "",
    username: str = "",
    error: str | None = None,
    school_options: list[dict[str, Any]] | None = None,
    resolved_school: dict[str, Any] | None = None,
) -> bytes:
    return _render_school_account_form(
        token=token,
        school_name=school_name,
        username=username,
        error=error,
        school_options=school_options,
        resolved_school=resolved_school,
    )


def _render_moodle_connect_success(*, display_name: str) -> bytes:
    return render_moodle_connect_success(display_name=display_name)


def _render_moodle_connect_invalid(reason: str) -> bytes:
    return render_moodle_connect_invalid(reason)


def _render_portal_connect_form(
    *,
    token: str,
    username: str = "",
    error: str | None = None,
) -> bytes:
    return render_portal_connect_form(
        token=token,
        portal_connect_path=PORTAL_CONNECT_PATH,
        username=username,
        error=error,
    )


def _render_portal_connect_success(*, display_name: str) -> bytes:
    return render_portal_connect_success(display_name=display_name)


def _render_portal_connect_invalid(reason: str) -> bytes:
    return render_portal_connect_invalid(reason)


def _render_browser_connect_page(*, token: str, display_name: str) -> bytes:
    return render_browser_connect_page(
        token=token,
        display_name=display_name,
        browser_connect_path=BROWSER_CONNECT_PATH,
    )


def _render_browser_connect_invalid(reason: str) -> bytes:
    return render_browser_connect_invalid(reason)


def _render_browser_frame_placeholder_svg(message: str) -> bytes:
    return render_browser_frame_placeholder_svg(message)


def _requests_session_from_browser_cookies(cookies: list[dict[str, Any]]) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").strip() or None
        path = str(item.get("path") or "").strip() or "/"
        if not name:
            continue
        session.cookies.set(name, value, domain=domain, path=path)
    return session


def _prime_post_connect_portal_sync(
    *,
    settings: Any,
    db: Database,
    chat_id: str,
    user_id: int | None,
    fetched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return prime_post_connect_portal_sync(
        settings=settings,
        db=db,
        chat_id=chat_id,
        user_id=user_id,
        fetched=fetched,
    )
def build_onboarding_http_server(
    *,
    host: str,
    port: int,
    settings: Any,
    db: Database,
    secret_store: SecretStore | None = None,
    telegram_client_factory: type[TelegramBotClient] = TelegramBotClient,
) -> HTTPServer:
    store = secret_store or default_secret_store(settings)
    token_service = str(getattr(settings, "uclass_token_service", "moodle_mobile_app") or "moodle_mobile_app").strip()
    request_method = str(getattr(settings, "uclass_request_method", "GET") or "GET").strip().upper() or "GET"
    site_info_wsfunction = str(
        getattr(settings, "uclass_func_site_info", "core_webservice_get_site_info")
        or "core_webservice_get_site_info"
    ).strip()
    bot_token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    browser_profiles_dir = Path(
        str(getattr(settings, "onboarding_browser_profiles_dir", "data/browser-profiles"))
    ).expanduser()
    browser_channel = str(getattr(settings, "onboarding_browser_channel", "") or "")
    browser_executable_path = getattr(settings, "onboarding_browser_executable_path", None)
    browser_headless = bool(getattr(settings, "onboarding_browser_headless", True))
    active_browser_sessions: dict[str, InteractiveBrowserSession] = {}
    pending_browser_sessions: dict[str, threading.Thread] = {}
    failed_browser_sessions: dict[str, str] = {}
    browser_session_identity_by_token: dict[str, tuple[str, str, str]] = {}
    browser_session_token_by_identity: dict[tuple[str, str, str], str] = {}
    active_browser_sessions_lock = threading.Lock()

    def _browser_session_identity(session_row: dict[str, Any]) -> tuple[str, str, str]:
        metadata = session_row.get("metadata_json") if isinstance(session_row, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        chat_id = str(session_row.get("chat_id") or "").strip()
        provider = str(metadata.get("provider") or "moodle").strip() or "moodle"
        school_slug = str(metadata.get("school_slug") or "").strip() or "school"
        return (chat_id, provider, school_slug)

    def _close_remote_browser_session(token: str) -> None:
        token = str(token or "").strip()
        identity_to_clear: tuple[str, str, str] | None = None
        with active_browser_sessions_lock:
            session = active_browser_sessions.pop(token, None)
            pending_browser_sessions.pop(token, None)
            failed_browser_sessions.pop(token, None)
            identity_to_clear = browser_session_identity_by_token.pop(token, None)
            if (
                identity_to_clear is not None
                and browser_session_token_by_identity.get(identity_to_clear) == token
            ):
                browser_session_token_by_identity.pop(identity_to_clear, None)
        if session is not None:
            session.close()

    def _cleanup_remote_browser_sessions() -> None:
        now_monotonic = time.monotonic()
        stale_tokens: list[str] = []
        with active_browser_sessions_lock:
            for token, session in list(active_browser_sessions.items()):
                if now_monotonic - float(session.last_activity_at) > REMOTE_BROWSER_IDLE_TIMEOUT_SECONDS:
                    stale_tokens.append(token)
        for token in stale_tokens:
            _close_remote_browser_session(token)

    def _build_browser_profile_dir(
        *,
        chat_id: str,
        provider: str,
        school_slug: str,
    ) -> Path:
        return browser_session_profile_dir(
            browser_profiles_dir,
            provider=provider,
            school_slug=school_slug,
            chat_id=chat_id,
        )

    def _create_remote_browser_session(session_row: dict[str, Any]) -> InteractiveBrowserSession:
        metadata = session_row.get("metadata_json") if isinstance(session_row, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        chat_id = str(session_row.get("chat_id") or "").strip()
        provider = str(metadata.get("provider") or "moodle").strip() or "moodle"
        school_slug = str(metadata.get("school_slug") or "").strip() or "school"
        login_url = str(metadata.get("login_url") or "").strip()
        if not chat_id or not login_url:
            raise ValueError("browser onboarding metadata is incomplete")
        profile_dir = _build_browser_profile_dir(
            chat_id=chat_id,
            provider=provider,
            school_slug=school_slug,
        )
        return InteractiveBrowserSession(
            login_url=login_url,
            profile_dir=profile_dir,
            browser_channel=browser_channel,
            browser_executable_path=browser_executable_path,
            headless=True,
        )

    def _start_remote_browser_session(session_row: dict[str, Any], *, token: str) -> None:
        token = str(token or "").strip()
        if not token:
            return
        identity = _browser_session_identity(session_row)

        def _worker() -> None:
            created: InteractiveBrowserSession | None = None
            error_text = ""
            try:
                created = _create_remote_browser_session(session_row)
            except Exception as exc:
                error_text = str(exc)
            with active_browser_sessions_lock:
                pending_browser_sessions.pop(token, None)
                if error_text:
                    failed_browser_sessions[token] = error_text
                    return
                if browser_session_token_by_identity.get(identity) != token:
                    if created is not None:
                        created.close()
                    return
                current = active_browser_sessions.get(token)
                if current is not None:
                    if created is not None:
                        created.close()
                    return
                if created is not None:
                    active_browser_sessions[token] = created
                failed_browser_sessions.pop(token, None)

        stale_session: InteractiveBrowserSession | None = None
        with active_browser_sessions_lock:
            if token in active_browser_sessions or token in pending_browser_sessions:
                return
            previous_token = browser_session_token_by_identity.get(identity)
            if previous_token and previous_token != token:
                pending_browser_sessions.pop(previous_token, None)
                failed_browser_sessions.pop(previous_token, None)
                stale_session = active_browser_sessions.pop(previous_token, None)
                browser_session_identity_by_token.pop(previous_token, None)
            failed_browser_sessions.pop(token, None)
            browser_session_identity_by_token[token] = identity
            browser_session_token_by_identity[identity] = token
            worker = threading.Thread(
                target=_worker,
                name=f"onboarding-browser-warmup-{token[:8]}",
                daemon=True,
            )
            pending_browser_sessions[token] = worker
        if stale_session is not None:
            stale_session.close()
        worker.start()

    def _get_remote_browser_session_status(
        session_row: dict[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            return {"status": "error", "error": "missing browser onboarding token"}
        _cleanup_remote_browser_sessions()
        with active_browser_sessions_lock:
            existing = active_browser_sessions.get(token)
            if existing is not None:
                return {"status": "ready", "session": existing}
            pending = pending_browser_sessions.get(token)
            if pending is not None:
                return {"status": "pending"}
            error_text = failed_browser_sessions.get(token)
            if error_text:
                return {"status": "error", "error": error_text}
        _start_remote_browser_session(session_row, token=token)
        return {"status": "pending"}

    def _get_or_create_remote_browser_session(
        session_row: dict[str, Any],
        *,
        token: str,
    ) -> InteractiveBrowserSession:
        token = str(token or "").strip()
        if not token:
            raise ValueError("missing browser onboarding token")
        _cleanup_remote_browser_sessions()
        with active_browser_sessions_lock:
            existing = active_browser_sessions.get(token)
        if existing is not None:
            return existing
        created = _create_remote_browser_session(session_row)
        with active_browser_sessions_lock:
            current = active_browser_sessions.get(token)
            if current is not None:
                created.close()
                return current
            active_browser_sessions[token] = created
        return created

    def _finalize_browser_connect_session(session_row: dict[str, Any], *, token: str) -> bytes:
        metadata = session_row.get("metadata_json") if isinstance(session_row, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        token = str(token or "").strip()
        remote_session = _get_or_create_remote_browser_session(session_row, token=token)
        state = remote_session.state()
        chat_id = str(session_row.get("chat_id") or "").strip()
        school_slug = str(metadata.get("school_slug") or "").strip()
        provider = str(metadata.get("provider") or "moodle").strip() or "moodle"
        display_name = str(metadata.get("display_name") or school_slug or "학교 로그인").strip()
        profile_dir = _build_browser_profile_dir(
            chat_id=chat_id,
            provider=provider,
            school_slug=school_slug,
        )
        try:
            if str(metadata.get("auth_mode") or "").strip().lower() == "password_token":
                ws_base_url = str(metadata.get("ws_base_url") or "").strip()
                if not ws_base_url:
                    raise ValueError("ws_base_url is missing")
                cookies = remote_session.cookies()
                requests_session = _requests_session_from_browser_cookies(cookies)
                token_service = str(getattr(settings, "uclass_token_service", "moodle_mobile_app") or "moodle_mobile_app").strip()
                validation_wsfunction = str(
                    getattr(settings, "uclass_func_site_info", "core_webservice_get_site_info")
                    or "core_webservice_get_site_info"
                ).strip()
                issued_token = request_moodle_mobile_launch_token(
                    ws_base_url=ws_base_url,
                    username="",
                    password="",
                    service=token_service,
                    timeout_sec=30,
                    session=requests_session,
                    request_method=request_method,
                    validation_wsfunction=validation_wsfunction,
                )
                site_info_client = MoodleWSClient(
                    base_url=ws_base_url,
                    token=issued_token,
                    request_method=request_method,
                )
                site_info = site_info_client.get_site_info(site_info_wsfunction)
                username = str(site_info.get("username") or "").strip() or None
                secret_key = f"telegram:{chat_id}:moodle:{school_slug}"
                stored = store.store_secret(key=secret_key, secret=issued_token)
                connection = db.upsert_moodle_connection(
                    chat_id=chat_id,
                    school_slug=school_slug,
                    display_name=display_name,
                    ws_base_url=ws_base_url,
                    username=username,
                    secret_kind=stored.kind,
                    secret_ref=stored.ref,
                    last_verified_at=now_utc_iso(),
                    metadata_json={
                        "site_info": site_info if isinstance(site_info, dict) else {"raw": site_info},
                        "onboarding_session_id": session_row["id"],
                        "source": "remote_browser_onboarding",
                        "browser_current_url": state.get("current_url"),
                    },
                )
                browser_session = db.upsert_lms_browser_session(
                    chat_id=chat_id,
                    school_slug=school_slug,
                    provider=provider,
                    display_name=display_name,
                    login_url=str(metadata.get("login_url") or "").strip(),
                    profile_dir=profile_dir,
                    status="active",
                    last_opened_at=now_utc_iso(),
                    last_verified_at=now_utc_iso(),
                        metadata_json={
                            "auth_mode": "password_token",
                            "source": "remote_browser_onboarding",
                            "browser_result": sanitize_browser_session_result(state),
                            "manual_confirmation": False,
                        },
                )
                db.mark_onboarding_session_used(
                    session_id=int(session_row["id"]),
                    metadata_json={
                        "connection_id": connection["id"],
                        "browser_session_id": browser_session["id"],
                        "school_slug": school_slug,
                    },
                )
                if bot_token:
                    try:
                        stored_login_secret = bool(
                            str(connection.get("login_secret_kind") or "").strip()
                            and str(connection.get("login_secret_ref") or "").strip()
                        )
                        telegram_client_factory(bot_token).send_message(
                            chat_id=chat_id,
                            text=(
                                "[Sidae] 온라인강의실 연결 완료\n\n"
                                f"- 학교: {display_name}\n"
                                f"- 계정: {username or '확인됨'}\n"
                                + (
                                    "- 비밀번호와 접근 token, 브라우저 세션을 이 사용자용 보안 저장소에 저장했습니다."
                                    if stored_login_secret
                                    else "- 비밀번호는 저장하지 않았고, 접근 token과 브라우저 세션만 저장했습니다."
                                )
                            ),
                        )
                    except Exception as exc:
                        logger.warning("failed to send browser onboarding telegram", extra={"error": str(exc)})
                _close_remote_browser_session(token)
                return _render_moodle_connect_success(display_name=display_name)

            browser_session = db.upsert_lms_browser_session(
                chat_id=chat_id,
                school_slug=school_slug,
                provider=provider,
                display_name=display_name,
                login_url=str(metadata.get("login_url") or "").strip(),
                profile_dir=profile_dir,
                status="active",
                last_opened_at=now_utc_iso(),
                last_verified_at=now_utc_iso(),
                metadata_json={
                    "auth_mode": str(metadata.get("auth_mode") or "browser_session").strip() or "browser_session",
                    "source": "remote_browser_onboarding",
                    "browser_result": sanitize_browser_session_result(state),
                    "manual_confirmation": False,
                },
            )
            db.mark_onboarding_session_used(
                session_id=int(session_row["id"]),
                metadata_json={
                    "browser_session_id": browser_session["id"],
                    "school_slug": school_slug,
                },
            )
            if bot_token:
                try:
                    completion_title = (
                        "[Sidae] 포털 연결 완료"
                        if school_slug == UOS_PORTAL_SCHOOL_SLUG
                        else "[Sidae] 학교 로그인 연결 완료"
                    )
                    telegram_client_factory(bot_token).send_message(
                        chat_id=chat_id,
                        text=(
                            f"{completion_title}\n\n"
                            f"- 학교: {display_name}\n"
                            "- 비밀번호는 저장하지 않았고, 브라우저 세션만 저장했습니다."
                        ),
                    )
                except Exception as exc:
                    logger.warning("failed to send browser onboarding telegram", extra={"error": str(exc)})
            _close_remote_browser_session(token)
            return _render_portal_connect_success(display_name=display_name)
        except Exception:
            raise

    def _load_school_options() -> list[dict[str, Any]]:
        return visible_onboarding_school_entries(
            db.list_moodle_school_directory(limit=500),
            settings=settings,
        )

    def _resolve_prefilled_school(
        school_name: str,
        *,
        allowed_school_slugs: set[str],
    ) -> dict[str, Any] | None:
        if not school_name:
            return None
        try:
            connect_plan = resolve_school_account_connect_plan(
                db,
                school_name=school_name,
                allowed_school_slugs=allowed_school_slugs,
            )
        except ValueError:
            return None
        resolved_school, _, _ = connect_plan_entries(connect_plan)
        return resolved_school

    school_account_service = OnboardingApplicationService(
        db=db,
        store=store,
        settings=settings,
        telegram_client_factory=telegram_client_factory,
        exchange_moodle_credentials_fn=exchange_moodle_credentials,
        finalize_school_account_connection_fn=finalize_school_account_connection,
        school_support_summary_fn=school_support_summary,
        request_method=request_method,
        site_info_wsfunction=site_info_wsfunction,
        token_service=token_service,
        browser_channel=browser_channel,
        browser_executable_path=browser_executable_path,
        browser_headless=browser_headless,
        build_browser_profile_dir=_build_browser_profile_dir,
        portal_login_browser_session=login_uos_portal_browser_session,
        prime_post_connect_portal_sync=_prime_post_connect_portal_sync,
        sanitize_browser_result=sanitize_browser_session_result,
        now_utc_iso_fn=now_utc_iso,
        uos_portal_provider=UOS_PORTAL_PROVIDER,
        uos_portal_school_slug=UOS_PORTAL_SCHOOL_SLUG,
        uos_portal_login_url=UOS_PORTAL_LOGIN_URL,
    )

    class OnboardingHandler(BaseHTTPRequestHandler):
        def _write_bytes(
            self,
            status_code: int,
            body: bytes,
            content_type: str,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(str(key), str(value))
            self.end_headers()
            self.wfile.write(body)

        def _write_html(self, status_code: int, body: bytes) -> None:
            self._write_bytes(status_code, body, "text/html; charset=utf-8")

        def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
            self._write_bytes(status_code, data, "application/json; charset=utf-8")

        def _write_png(self, status_code: int, body: bytes) -> None:
            self._write_bytes(status_code, body, "image/png")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _remote_addr(self) -> str:
            forwarded = str(self.headers.get("X-Forwarded-For", "") or "").strip()
            if forwarded:
                return forwarded.split(",", 1)[0].strip()
            return str(self.client_address[0] if self.client_address else "").strip()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"", "/", "/healthz"}:
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "service": "sidae-onboarding",
                        "listening_on": f"http://{host}:{port}",
                        "moodle_connect_path": MOODLE_CONNECT_PATH,
                    },
                )
                return
            if parsed.path == BROWSER_CONNECT_PATH:
                self._write_html(404, _render_moodle_connect_invalid("이 연결 경로는 더 이상 사용하지 않습니다."))
                return
            if parsed.path == f"{BROWSER_CONNECT_PATH}/state":
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            if parsed.path == f"{BROWSER_CONNECT_PATH}/frame":
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            if parsed.path == PORTAL_CONNECT_PATH:
                self._write_html(404, _render_moodle_connect_invalid("이 연결 경로는 더 이상 사용하지 않습니다."))
                return
            if parsed.path != MOODLE_CONNECT_PATH:
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            session = db.get_active_onboarding_session(
                token=str(params.get("token") or "").strip(),
                session_kind=MOODLE_ONBOARDING_SESSION_KIND,
            )
            if not session:
                self._write_html(404, _render_moodle_connect_invalid("링크가 만료되었거나 이미 사용되었습니다."))
                return
            session_metadata = session.get("metadata_json") if isinstance(session, dict) else {}
            prefilled_school_name = str(
                (session_metadata or {}).get("school_query") or ""
            ).strip()
            allowed_school_slugs = onboarding_allowed_school_slugs(settings)
            school_options = _load_school_options()
            resolved_school = _resolve_prefilled_school(
                prefilled_school_name,
                allowed_school_slugs=allowed_school_slugs,
            )
            db.record_auth_attempt(
                chat_id=str(session.get("chat_id") or "").strip() or None,
                onboarding_session_id=int(session["id"]),
                session_kind=MOODLE_ONBOARDING_SESSION_KIND,
                school_slug=(
                    str((resolved_school or {}).get("school_slug") or "").strip().lower()
                    or None
                ),
                remote_addr=self._remote_addr() or None,
                status="viewed",
                metadata_json={"path": MOODLE_CONNECT_PATH, "method": "GET"},
            )
            self._write_html(
                200,
                _render_moodle_connect_form(
                    token=str(params.get("token") or "").strip(),
                    school_name=prefilled_school_name,
                    school_options=school_options,
                    resolved_school=resolved_school,
                ),
            )

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == f"{BROWSER_CONNECT_PATH}/action":
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            if parsed.path == f"{BROWSER_CONNECT_PATH}/complete":
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            if parsed.path == PORTAL_CONNECT_PATH:
                self._write_html(404, _render_moodle_connect_invalid("이 연결 경로는 더 이상 사용하지 않습니다."))
                return
            if parsed.path != MOODLE_CONNECT_PATH:
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._write_json(400, {"ok": False, "error": "invalid_content_length"})
                return
            raw = self.rfile.read(content_length) if content_length > 0 else b""
            form = parse_moodle_connect_form(raw)
            token = form.token
            school_name = form.school_name
            username = form.username
            remote_addr = self._remote_addr() or None
            allowed_school_slugs = onboarding_allowed_school_slugs(settings)
            school_options = _load_school_options()
            result = school_account_service.complete_school_account_connect(
                form=form,
                remote_addr=remote_addr,
                session_kind=MOODLE_ONBOARDING_SESSION_KIND,
                allowed_school_slugs=allowed_school_slugs,
            )
            if result.status == "session_not_found":
                self._write_html(404, _render_moodle_connect_invalid("링크가 만료되었거나 이미 사용되었습니다."))
                return
            if result.status != "success":
                self._write_html(
                    result.http_status,
                    _render_moodle_connect_form(
                        token=token,
                        school_name=school_name,
                        username=username,
                        error=result.public_error,
                        school_options=school_options,
                        resolved_school=result.directory_entry,
                    ),
                )
                return
            self._write_html(
                200,
                _render_moodle_connect_success(
                    display_name=str(result.success_display_name or "학교 계정")
                ),
            )

    return FastBindHTTPServer((host, port), OnboardingHandler)
