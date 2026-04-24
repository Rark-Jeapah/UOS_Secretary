from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable

from sidae_secretary.connectors.telegram import TelegramBotClient
from sidae_secretary.db import Database
from sidae_secretary.onboarding_school_connect import (
    MoodleConnectFormData,
    SchoolAccountFinalizeResult,
    apply_directory_entry_overrides,
    build_school_account_completion_message,
    connect_plan_entries,
    connect_plan_school_slug,
    public_error_message,
    resolve_school_account_connect_plan,
    safe_failure_reason,
    school_account_success_display_name,
    validate_moodle_connect_form,
)
from sidae_secretary.secret_store import SecretStore


logger = logging.getLogger(__name__)


AUTH_RATE_WINDOW_SECONDS = 15 * 60
AUTH_MAX_FAILED_PER_SESSION = 5
AUTH_MAX_FAILED_PER_REMOTE = 10
AUTH_MAX_TOTAL_PER_REMOTE = 20


@dataclass(frozen=True)
class SchoolAccountConnectResult:
    status: str
    http_status: int
    session: dict[str, Any] | None = None
    public_error: str | None = None
    failure_reason: str | None = None
    directory_entry: dict[str, Any] | None = None
    portal_info: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    portal_browser_session: dict[str, Any] | None = None
    portal_login_error: str = ""
    portal_prime_result: dict[str, Any] | None = None
    success_display_name: str | None = None


@dataclass
class OnboardingApplicationService:
    db: Database
    store: SecretStore
    settings: Any
    telegram_client_factory: type[TelegramBotClient]
    exchange_moodle_credentials_fn: Callable[..., dict[str, Any]]
    finalize_school_account_connection_fn: Callable[..., SchoolAccountFinalizeResult]
    school_support_summary_fn: Callable[[dict[str, Any]], dict[str, Any]]
    request_method: str
    site_info_wsfunction: str
    token_service: str
    browser_channel: str
    browser_executable_path: Any
    browser_headless: bool
    build_browser_profile_dir: Callable[..., Any]
    portal_login_browser_session: Callable[..., dict[str, Any]]
    prime_post_connect_portal_sync: Callable[..., dict[str, Any]]
    sanitize_browser_result: Callable[[dict[str, Any]], dict[str, Any]]
    now_utc_iso_fn: Callable[[], str]
    uos_portal_provider: str
    uos_portal_school_slug: str
    uos_portal_login_url: str

    def complete_school_account_connect(
        self,
        *,
        form: MoodleConnectFormData,
        remote_addr: str | None,
        session_kind: str,
        allowed_school_slugs: set[str] | None = None,
    ) -> SchoolAccountConnectResult:
        session = self.db.get_active_onboarding_session(
            token=form.token,
            session_kind=session_kind,
        )
        if not session:
            return SchoolAccountConnectResult(status="session_not_found", http_status=404)
        chat_id = str(session.get("chat_id") or "").strip() or None
        missing_reason = validate_moodle_connect_form(form)
        if missing_reason:
            self.db.record_auth_attempt(
                chat_id=chat_id,
                onboarding_session_id=int(session["id"]),
                session_kind=session_kind,
                remote_addr=remote_addr,
                username=form.username or None,
                status="invalid_request",
                failure_reason=missing_reason,
                metadata_json={"path": "/moodle-connect", "method": "POST"},
            )
            return SchoolAccountConnectResult(
                status="invalid_request",
                http_status=400,
                session=session,
                failure_reason=missing_reason,
                public_error=public_error_message(missing_reason),
            )
        try:
            connect_plan = resolve_school_account_connect_plan(
                self.db,
                school_name=form.school_name,
                allowed_school_slugs=allowed_school_slugs,
                uos_online_class_school_slug="uos_online_class",
                uos_portal_school_slug=self.uos_portal_school_slug,
            )
        except ValueError:
            self.db.record_auth_attempt(
                chat_id=chat_id,
                onboarding_session_id=int(session["id"]),
                session_kind=session_kind,
                remote_addr=remote_addr,
                username=form.username or None,
                status="invalid_request",
                failure_reason="school_not_found",
                metadata_json={
                    "path": "/moodle-connect",
                    "method": "POST",
                    "school_name": form.school_name,
                },
            )
            return SchoolAccountConnectResult(
                status="invalid_request",
                http_status=400,
                session=session,
                failure_reason="school_not_found",
                public_error=public_error_message("school_not_found"),
            )
        directory_entry, _, portal_info = connect_plan_entries(connect_plan)
        school_slug_for_attempt = connect_plan_school_slug(connect_plan)
        if self._is_rate_limited(
            session_id=int(session["id"]),
            session_kind=session_kind,
            remote_addr=remote_addr,
        ):
            self.db.record_auth_attempt(
                chat_id=chat_id,
                onboarding_session_id=int(session["id"]),
                session_kind=session_kind,
                school_slug=school_slug_for_attempt,
                remote_addr=remote_addr,
                username=form.username or None,
                status="blocked",
                failure_reason="rate_limited",
                metadata_json={"path": "/moodle-connect", "method": "POST"},
            )
            return SchoolAccountConnectResult(
                status="blocked",
                http_status=429,
                session=session,
                directory_entry=directory_entry,
                portal_info=portal_info,
                failure_reason="rate_limited",
                public_error=public_error_message("rate_limited"),
            )
        try:
            resolved = self.exchange_moodle_credentials_fn(
                lms_url=str(connect_plan.get("moodle_ws_base_url") or "").strip(),
                username=form.username,
                password=form.password,
                request_method=self.request_method,
                site_info_wsfunction=self.site_info_wsfunction,
                token_service=self.token_service,
            )
            resolved = apply_directory_entry_overrides(resolved, directory_entry)
            finalize_result = self.finalize_school_account_connection_fn(
                db=self.db,
                store=self.store,
                settings=self.settings,
                session=session,
                connect_plan=connect_plan,
                school_name=form.school_name,
                username=form.username,
                password=form.password,
                resolved=resolved,
                browser_channel=self.browser_channel,
                browser_executable_path=self.browser_executable_path,
                browser_headless=self.browser_headless,
                build_browser_profile_dir=self.build_browser_profile_dir,
                portal_login_browser_session=self.portal_login_browser_session,
                prime_post_connect_portal_sync=self.prime_post_connect_portal_sync,
                sanitize_browser_result=self.sanitize_browser_result,
                now_utc_iso_fn=self.now_utc_iso_fn,
                uos_portal_provider=self.uos_portal_provider,
                uos_portal_school_slug=self.uos_portal_school_slug,
                uos_portal_login_url=self.uos_portal_login_url,
            )
            self.db.record_auth_attempt(
                chat_id=chat_id,
                onboarding_session_id=int(session["id"]),
                session_kind=session_kind,
                school_slug=school_slug_for_attempt,
                remote_addr=remote_addr,
                username=form.username or None,
                status="success",
                metadata_json={
                    "path": "/moodle-connect",
                    "method": "POST",
                    "bundle_kind": connect_plan.get("bundle_kind"),
                },
            )
        except Exception as exc:
            logger.warning("moodle onboarding failed", extra={"error": str(exc)})
            failure_reason = safe_failure_reason(exc)
            self.db.record_auth_attempt(
                chat_id=chat_id,
                onboarding_session_id=int(session["id"]),
                session_kind=session_kind,
                school_slug=school_slug_for_attempt,
                remote_addr=remote_addr,
                username=form.username or None,
                status="failed",
                failure_reason=failure_reason,
                metadata_json={
                    "path": "/moodle-connect",
                    "method": "POST",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            return SchoolAccountConnectResult(
                status="failed",
                http_status=400,
                session=session,
                directory_entry=directory_entry,
                portal_info=portal_info,
                failure_reason=failure_reason,
                public_error=public_error_message(failure_reason),
            )
        self._send_school_account_completion_message(
            session_row=session,
            connection=finalize_result.connection,
            directory_entry=directory_entry,
            portal_info=portal_info,
            portal_browser_session=finalize_result.portal_browser_session,
            portal_login_error=finalize_result.portal_login_error,
            portal_prime_result=finalize_result.portal_prime_result,
        )
        return SchoolAccountConnectResult(
            status="success",
            http_status=200,
            session=session,
            directory_entry=directory_entry,
            portal_info=portal_info,
            connection=finalize_result.connection,
            portal_browser_session=finalize_result.portal_browser_session,
            portal_login_error=finalize_result.portal_login_error,
            portal_prime_result=finalize_result.portal_prime_result,
            success_display_name=school_account_success_display_name(
                connection=finalize_result.connection,
                portal_browser_session=finalize_result.portal_browser_session,
            ),
        )

    def _send_school_account_completion_message(
        self,
        *,
        session_row: dict[str, Any],
        connection: dict[str, Any],
        directory_entry: dict[str, Any] | None,
        portal_info: dict[str, Any] | None,
        portal_browser_session: dict[str, Any] | None,
        portal_login_error: str,
        portal_prime_result: dict[str, Any] | None,
    ) -> None:
        bot_token = str(getattr(self.settings, "telegram_bot_token", "") or "").strip()
        if not bot_token:
            return
        try:
            client = self.telegram_client_factory(bot_token)
            message = build_school_account_completion_message(
                connection=connection,
                directory_entry=directory_entry,
                portal_info=portal_info,
                portal_browser_session=portal_browser_session,
                portal_login_error=portal_login_error,
                portal_prime_result=portal_prime_result,
                school_support_summary_fn=self.school_support_summary_fn,
            )
            client.send_message(chat_id=str(session_row["chat_id"]), text=message)
        except Exception as exc:
            logger.warning("failed to send onboarding completion telegram", extra={"error": str(exc)})

    def _is_rate_limited(
        self,
        *,
        session_id: int,
        session_kind: str,
        remote_addr: str | None,
    ) -> bool:
        since_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00",
            time.gmtime(time.time() - AUTH_RATE_WINDOW_SECONDS),
        )
        if (
            self.db.count_auth_attempts(
                onboarding_session_id=session_id,
                session_kind=session_kind,
                status="failed",
                since_iso=since_iso,
            )
            >= AUTH_MAX_FAILED_PER_SESSION
        ):
            return True
        remote = str(remote_addr or "").strip()
        if not remote:
            return False
        if (
            self.db.count_auth_attempts(
                remote_addr=remote,
                session_kind=session_kind,
                status="failed",
                since_iso=since_iso,
            )
            >= AUTH_MAX_FAILED_PER_REMOTE
        ):
            return True
        return (
            self.db.count_auth_attempts(
                remote_addr=remote,
                session_kind=session_kind,
                since_iso=since_iso,
            )
            >= AUTH_MAX_TOTAL_PER_REMOTE
        )
