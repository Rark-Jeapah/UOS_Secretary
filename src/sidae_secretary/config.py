from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
from typing import Any

from dotenv import dotenv_values

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for Python <3.11 runtime
    import tomli as tomllib


def _to_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except ValueError:
        return default


def _to_optional_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_float(value: str | float | int | None, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return float(value)
    except ValueError:
        return default


def _to_secret_store_backend(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"file", "keychain"}:
        return text
    return ""


def _to_csv_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                output.append(text)
        return output
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _to_int_list(value: Any, default: list[int]) -> list[int]:
    if value is None:
        return list(default)
    items: list[Any]
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip()
        if not text:
            return list(default)
        items = [part.strip() for part in text.split(",")]
    output: list[int] = []
    for item in items:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            output.append(parsed)
    return output or list(default)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fp:
        raw = tomllib.load(fp)
    if "sidae" in raw and isinstance(raw["sidae"], dict):
        raw = raw["sidae"]
    output: dict[str, Any] = {}
    for key, value in raw.items():
        output[str(key).upper()] = value
    return output


def select_config_path(config_file: Path | None = None) -> Path:
    selected = config_file or Path(os.getenv("SIDAE_CONFIG_FILE", "config.toml"))
    return selected.expanduser().resolve()


_INSTANCE_NAME_RE = re.compile(r"[^a-z0-9]+")


def normalize_instance_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return _INSTANCE_NAME_RE.sub("-", text).strip("-")


def _resolve_path_from_config_dir(value: Any, default: str, config_dir: Path) -> Path:
    path = Path(str(value or default)).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


def _resolve_optional_path_from_config_dir(value: Any, config_dir: Path) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


def _normalize_url_path(value: Any, default: str = "/") -> str:
    text = str(value or default).strip()
    if not text:
        text = default
    if not text.startswith("/"):
        text = "/" + text
    return text


@dataclass
class Settings:
    instance_name: str
    storage_root_dir: Path | None
    uclass_ws_base: str | None
    uclass_wstoken: str | None
    uclass_username: str | None
    uclass_password: str | None
    uclass_token_service: str
    uclass_token_endpoint: str | None
    timezone: str
    uos_openapi_timetable_url: str | None
    uos_openapi_timetable_api_key: str | None
    uos_openapi_timetable_timeout_sec: int
    uos_openapi_year: int | None
    uos_openapi_term: str | None
    database_path: Path
    secret_store_backend: str
    secret_store_allow_file_fallback: bool
    uclass_func_site_info: str
    uclass_func_popup_notifications: str
    uclass_func_action_events: str
    uclass_func_courses: str
    uclass_func_course_contents: str
    uclass_func_assignments: str
    uclass_func_forums: str
    uclass_func_forum_discussions: str
    uclass_request_method: str
    uclass_page_limit: int
    uclass_enable_popup_notifications: bool
    uclass_enable_action_events: bool
    uclass_enable_courses: bool
    uclass_enable_contents: bool
    uclass_enable_assignments: bool
    uclass_enable_forums: bool
    uclass_required_wsfunctions: list[str]
    uclass_download_materials: bool
    uclass_download_retries: int
    uclass_download_backoff_sec: float
    portal_slug: str
    telegram_enabled: bool
    telegram_bot_token: str | None
    telegram_allowed_chat_ids: list[str]
    telegram_poll_limit: int
    telegram_smart_commands_enabled: bool
    telegram_assistant_enabled: bool
    telegram_assistant_write_enabled: bool
    onboarding_public_base_url: str | None
    onboarding_session_ttl_minutes: int
    onboarding_allowed_school_slugs: list[str]
    onboarding_browser_profiles_dir: Path
    onboarding_browser_channel: str
    onboarding_browser_executable_path: Path | None
    onboarding_browser_headless: bool
    llm_enabled: bool
    llm_provider: str
    llm_model: str
    llm_local_endpoint: str
    llm_timeout_sec: int
    include_identity: bool
    sync_window_days: int
    material_extraction_enabled: bool
    material_briefing_enabled: bool
    material_brief_push_enabled: bool
    material_brief_push_max_items: int
    material_extract_max_chars: int
    review_enabled: bool
    review_intervals_days: list[int]
    review_duration_min: int
    review_morning_hour: int
    digest_enabled: bool
    digest_time_local: str
    digest_channel: str
    digest_task_lookahead_days: int
    briefing_enabled: bool
    briefing_morning_time_local: str
    briefing_evening_time_local: str
    briefing_channel: str
    briefing_delivery_mode: str
    briefing_relay_endpoint: str | None
    briefing_relay_shared_secret: str | None
    briefing_relay_state_file: Path
    briefing_task_lookahead_days: int
    briefing_max_classes: int
    telegram_commands_enabled: bool
    weather_enabled: bool
    weather_location_label: str
    weather_lat: float
    weather_lon: float
    weather_kma_auth_key: str | None
    air_quality_enabled: bool
    air_quality_seoul_api_key: str | None
    air_quality_district_codes: list[str]
    ops_dashboard_ssh_host: str | None
    ops_dashboard_ssh_user: str | None
    ops_dashboard_ssh_port: int
    ops_dashboard_remote_host: str
    ops_dashboard_remote_port: int
    ops_dashboard_local_port: int
    ops_dashboard_url_path: str

    @property
    def icloud_dir(self) -> Path | None:
        # Legacy alias kept so older helpers/tests can still read the storage root.
        return self.storage_root_dir

    def required_missing(self) -> list[str]:
        missing: list[str] = []
        if not self.storage_root_dir:
            missing.append("STORAGE_ROOT_DIR")
        onboarding_ready = bool(str(self.onboarding_public_base_url or "").strip())
        if not self.uclass_ws_base and not onboarding_ready:
            missing.append("UCLASS_WS_BASE")
        has_token = bool(str(self.uclass_wstoken or "").strip())
        if not has_token and not onboarding_ready:
            missing.append("UCLASS_WSTOKEN")
        if not self.timezone:
            missing.append("TIMEZONE")
        return missing

    def as_doctor_dict(self) -> dict[str, str]:
        token = self.uclass_wstoken or ""
        redacted = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else ("***" if token else "")
        username = str(self.uclass_username or "").strip()
        auth_mode = "wstoken" if token else ("deprecated_idpw" if (username or self.uclass_password) else "none")
        return {
            "INSTANCE_NAME": self.instance_name,
            "STORAGE_ROOT_DIR": str(self.storage_root_dir) if self.storage_root_dir else "",
            "UCLASS_WS_BASE": self.uclass_ws_base or "",
            "UCLASS_WSTOKEN": redacted,
            "UCLASS_USERNAME": username,
            "UCLASS_PASSWORD": "***" if self.uclass_password else "",
            "UCLASS_TOKEN_SERVICE": self.uclass_token_service,
            "UCLASS_TOKEN_ENDPOINT": self.uclass_token_endpoint or "",
            "UCLASS_AUTH_MODE": auth_mode,
            "TIMEZONE": self.timezone,
            "DATABASE_PATH": str(self.database_path),
            "UOS_OPENAPI_TIMETABLE_URL": self.uos_openapi_timetable_url or "",
            "UOS_OPENAPI_TIMETABLE_API_KEY": (
                "***" if self.uos_openapi_timetable_api_key else ""
            ),
            "UOS_OPENAPI_TIMETABLE_TIMEOUT_SEC": str(
                self.uos_openapi_timetable_timeout_sec
            ),
            "UOS_OPENAPI_YEAR": str(self.uos_openapi_year or ""),
            "UOS_OPENAPI_TERM": str(self.uos_openapi_term or ""),
            "SECRET_STORE_BACKEND": self.secret_store_backend,
            "SECRET_STORE_ALLOW_FILE_FALLBACK": str(
                self.secret_store_allow_file_fallback
            ),
            "UCLASS_FUNC_SITE_INFO": self.uclass_func_site_info,
            "UCLASS_FUNC_POPUP_NOTIFICATIONS": self.uclass_func_popup_notifications,
            "UCLASS_FUNC_ACTION_EVENTS": self.uclass_func_action_events,
            "UCLASS_FUNC_COURSES": self.uclass_func_courses,
            "UCLASS_FUNC_COURSE_CONTENTS": self.uclass_func_course_contents,
            "UCLASS_FUNC_ASSIGNMENTS": self.uclass_func_assignments,
            "UCLASS_FUNC_FORUMS": self.uclass_func_forums,
            "UCLASS_FUNC_FORUM_DISCUSSIONS": self.uclass_func_forum_discussions,
            "UCLASS_REQUEST_METHOD": self.uclass_request_method,
            "UCLASS_ENABLE_POPUP_NOTIFICATIONS": str(
                self.uclass_enable_popup_notifications
            ),
            "UCLASS_ENABLE_ACTION_EVENTS": str(self.uclass_enable_action_events),
            "UCLASS_ENABLE_COURSES": str(self.uclass_enable_courses),
            "UCLASS_ENABLE_CONTENTS": str(self.uclass_enable_contents),
            "UCLASS_ENABLE_ASSIGNMENTS": str(self.uclass_enable_assignments),
            "UCLASS_ENABLE_FORUMS": str(self.uclass_enable_forums),
            "UCLASS_REQUIRED_WSFUNCTIONS": ",".join(self.uclass_required_wsfunctions),
            "UCLASS_DOWNLOAD_MATERIALS": str(self.uclass_download_materials),
            "UCLASS_DOWNLOAD_RETRIES": str(self.uclass_download_retries),
            "UCLASS_DOWNLOAD_BACKOFF_SEC": str(self.uclass_download_backoff_sec),
            "PORTAL_SLUG": self.portal_slug,
            "TELEGRAM_ENABLED": str(self.telegram_enabled),
            "TELEGRAM_BOT_TOKEN": "***" if self.telegram_bot_token else "",
            "TELEGRAM_ALLOWED_CHAT_IDS": ",".join(self.telegram_allowed_chat_ids),
            "TELEGRAM_POLL_LIMIT": str(self.telegram_poll_limit),
            "TELEGRAM_SMART_COMMANDS_ENABLED": str(self.telegram_smart_commands_enabled),
            "TELEGRAM_ASSISTANT_ENABLED": str(self.telegram_assistant_enabled),
            "TELEGRAM_ASSISTANT_WRITE_ENABLED": str(
                self.telegram_assistant_write_enabled
            ),
            "ONBOARDING_PUBLIC_BASE_URL": self.onboarding_public_base_url or "",
            "ONBOARDING_SESSION_TTL_MINUTES": str(self.onboarding_session_ttl_minutes),
            "ONBOARDING_ALLOWED_SCHOOL_SLUGS": ",".join(self.onboarding_allowed_school_slugs),
            "ONBOARDING_BROWSER_PROFILES_DIR": str(self.onboarding_browser_profiles_dir),
            "ONBOARDING_BROWSER_CHANNEL": self.onboarding_browser_channel,
            "ONBOARDING_BROWSER_EXECUTABLE_PATH": (
                str(self.onboarding_browser_executable_path)
                if self.onboarding_browser_executable_path
                else ""
            ),
            "ONBOARDING_BROWSER_HEADLESS": str(self.onboarding_browser_headless),
            "LLM_ENABLED": str(self.llm_enabled),
            "LLM_PROVIDER": self.llm_provider,
            "LLM_MODEL": self.llm_model,
            "LLM_LOCAL_ENDPOINT": self.llm_local_endpoint,
            "LLM_TIMEOUT_SEC": str(self.llm_timeout_sec),
            "INCLUDE_IDENTITY": str(self.include_identity),
            "MATERIAL_EXTRACTION_ENABLED": str(self.material_extraction_enabled),
            "MATERIAL_BRIEFING_ENABLED": str(self.material_briefing_enabled),
            "MATERIAL_BRIEF_PUSH_ENABLED": str(self.material_brief_push_enabled),
            "MATERIAL_BRIEF_PUSH_MAX_ITEMS": str(self.material_brief_push_max_items),
            "MATERIAL_EXTRACT_MAX_CHARS": str(self.material_extract_max_chars),
            "REVIEW_ENABLED": str(self.review_enabled),
            "REVIEW_INTERVALS_DAYS": ",".join(
                str(item) for item in self.review_intervals_days
            ),
            "REVIEW_DURATION_MIN": str(self.review_duration_min),
            "REVIEW_MORNING_HOUR": str(self.review_morning_hour),
            "DIGEST_ENABLED": str(self.digest_enabled),
            "DIGEST_TIME_LOCAL": self.digest_time_local,
            "DIGEST_CHANNEL": self.digest_channel,
            "DIGEST_TASK_LOOKAHEAD_DAYS": str(self.digest_task_lookahead_days),
            "BRIEFING_ENABLED": str(self.briefing_enabled),
            "BRIEFING_MORNING_TIME_LOCAL": self.briefing_morning_time_local,
            "BRIEFING_EVENING_TIME_LOCAL": self.briefing_evening_time_local,
            "BRIEFING_CHANNEL": self.briefing_channel,
            "BRIEFING_DELIVERY_MODE": self.briefing_delivery_mode,
            "BRIEFING_RELAY_ENDPOINT": self.briefing_relay_endpoint or "",
            "BRIEFING_RELAY_SHARED_SECRET": (
                "***" if self.briefing_relay_shared_secret else ""
            ),
            "BRIEFING_RELAY_STATE_FILE": str(self.briefing_relay_state_file),
            "BRIEFING_TASK_LOOKAHEAD_DAYS": str(self.briefing_task_lookahead_days),
            "BRIEFING_MAX_CLASSES": str(self.briefing_max_classes),
            "TELEGRAM_COMMANDS_ENABLED": str(self.telegram_commands_enabled),
            "WEATHER_ENABLED": str(self.weather_enabled),
            "WEATHER_LOCATION_LABEL": self.weather_location_label,
            "WEATHER_LAT": str(self.weather_lat),
            "WEATHER_LON": str(self.weather_lon),
            "WEATHER_KMA_AUTH_KEY": "***" if self.weather_kma_auth_key else "",
            "AIR_QUALITY_ENABLED": str(self.air_quality_enabled),
            "AIR_QUALITY_SEOUL_API_KEY": "***" if self.air_quality_seoul_api_key else "",
            "AIR_QUALITY_DISTRICT_CODES": ",".join(self.air_quality_district_codes),
            "OPS_DASHBOARD_SSH_HOST": self.ops_dashboard_ssh_host or "",
            "OPS_DASHBOARD_SSH_USER": self.ops_dashboard_ssh_user or "",
            "OPS_DASHBOARD_SSH_PORT": str(self.ops_dashboard_ssh_port),
            "OPS_DASHBOARD_REMOTE_HOST": self.ops_dashboard_remote_host,
            "OPS_DASHBOARD_REMOTE_PORT": str(self.ops_dashboard_remote_port),
            "OPS_DASHBOARD_LOCAL_PORT": str(self.ops_dashboard_local_port),
            "OPS_DASHBOARD_URL_PATH": self.ops_dashboard_url_path,
        }


def load_settings(config_file: Path | None = None) -> Settings:
    selected_config = select_config_path(config_file=config_file)
    config_dir = selected_config.parent.resolve()
    config_values = _read_toml(selected_config)
    dotenv = {
        k.upper(): v
        for k, v in dotenv_values(config_dir / ".env").items()
        if v is not None
    }
    env_values = {k.upper(): v for k, v in os.environ.items()}
    merged: dict[str, Any] = {}
    merged.update(config_values)
    merged.update(dotenv)
    merged.update(env_values)
    storage_root_dir = _resolve_optional_path_from_config_dir(
        merged.get("STORAGE_ROOT_DIR") or merged.get("ICLOUD_DIR"),
        config_dir=config_dir,
    )
    onboarding_profiles_value = merged.get("ONBOARDING_BROWSER_PROFILES_DIR")
    onboarding_profiles_default = (
        str(storage_root_dir / "browser_profiles")
        if storage_root_dir is not None
        else "data/onboarding_browser_profiles"
    )

    settings = Settings(
        instance_name=normalize_instance_name(merged.get("INSTANCE_NAME")),
        storage_root_dir=storage_root_dir,
        uclass_ws_base=str(merged.get("UCLASS_WS_BASE") or "").strip() or None,
        uclass_wstoken=str(merged.get("UCLASS_WSTOKEN") or "").strip() or None,
        uclass_username=str(merged.get("UCLASS_USERNAME") or "").strip() or None,
        uclass_password=str(merged.get("UCLASS_PASSWORD") or "").strip() or None,
        uclass_token_service=str(
            merged.get("UCLASS_TOKEN_SERVICE") or "moodle_mobile_app"
        ).strip(),
        uclass_token_endpoint=str(merged.get("UCLASS_TOKEN_ENDPOINT") or "").strip() or None,
        timezone=str(merged.get("TIMEZONE") or "Asia/Seoul").strip(),
        uos_openapi_timetable_url=(
            str(merged.get("UOS_OPENAPI_TIMETABLE_URL") or "").strip() or None
        ),
        uos_openapi_timetable_api_key=(
            str(merged.get("UOS_OPENAPI_TIMETABLE_API_KEY") or "").strip() or None
        ),
        uos_openapi_timetable_timeout_sec=_to_int(
            merged.get("UOS_OPENAPI_TIMETABLE_TIMEOUT_SEC"),
            default=15,
        ),
        uos_openapi_year=_to_optional_int(merged.get("UOS_OPENAPI_YEAR")),
        uos_openapi_term=str(merged.get("UOS_OPENAPI_TERM") or "").strip() or None,
        database_path=_resolve_path_from_config_dir(
            merged.get("DATABASE_PATH"),
            default="data/sidae.db",
            config_dir=config_dir,
        ),
        secret_store_backend=_to_secret_store_backend(
            merged.get("SECRET_STORE_BACKEND")
        ),
        secret_store_allow_file_fallback=_to_bool(
            merged.get("SECRET_STORE_ALLOW_FILE_FALLBACK"),
            default=False,
        ),
        uclass_func_site_info=str(
            merged.get("UCLASS_FUNC_SITE_INFO") or "core_webservice_get_site_info"
        ).strip(),
        uclass_func_popup_notifications=str(
            merged.get("UCLASS_FUNC_POPUP_NOTIFICATIONS")
            or "message_popup_get_popup_notifications"
        ).strip(),
        uclass_func_action_events=str(
            merged.get("UCLASS_FUNC_ACTION_EVENTS")
            or "core_calendar_get_action_events_by_timesort"
        ).strip(),
        uclass_func_courses=str(
            merged.get("UCLASS_FUNC_COURSES") or "core_enrol_get_users_courses"
        ).strip(),
        uclass_func_course_contents=str(
            merged.get("UCLASS_FUNC_COURSE_CONTENTS") or "core_course_get_contents"
        ).strip(),
        uclass_func_assignments=str(
            merged.get("UCLASS_FUNC_ASSIGNMENTS") or "mod_assign_get_assignments"
        ).strip(),
        uclass_func_forums=str(
            merged.get("UCLASS_FUNC_FORUMS") or "mod_forum_get_forums_by_courses"
        ).strip(),
        uclass_func_forum_discussions=str(
            merged.get("UCLASS_FUNC_FORUM_DISCUSSIONS")
            or "mod_forum_get_forum_discussions_paginated"
        ).strip(),
        uclass_request_method=str(merged.get("UCLASS_REQUEST_METHOD") or "GET")
        .strip()
        .upper(),
        uclass_page_limit=_to_int(merged.get("UCLASS_PAGE_LIMIT"), default=50),
        uclass_enable_popup_notifications=_to_bool(
            merged.get("UCLASS_ENABLE_POPUP_NOTIFICATIONS"), default=True
        ),
        uclass_enable_action_events=_to_bool(
            merged.get("UCLASS_ENABLE_ACTION_EVENTS"), default=True
        ),
        uclass_enable_courses=_to_bool(merged.get("UCLASS_ENABLE_COURSES"), default=True),
        uclass_enable_contents=_to_bool(merged.get("UCLASS_ENABLE_CONTENTS"), default=True),
        uclass_enable_assignments=_to_bool(
            merged.get("UCLASS_ENABLE_ASSIGNMENTS"), default=True
        ),
        uclass_enable_forums=_to_bool(merged.get("UCLASS_ENABLE_FORUMS"), default=False),
        uclass_required_wsfunctions=_to_csv_list(
            merged.get("UCLASS_REQUIRED_WSFUNCTIONS")
        ),
        uclass_download_materials=_to_bool(
            merged.get("UCLASS_DOWNLOAD_MATERIALS"), default=True
        ),
        uclass_download_retries=_to_int(
            merged.get("UCLASS_DOWNLOAD_RETRIES"), default=3
        ),
        uclass_download_backoff_sec=_to_float(
            merged.get("UCLASS_DOWNLOAD_BACKOFF_SEC"), default=1.5
        ),
        portal_slug=str(merged.get("PORTAL_SLUG") or "academic").strip(),
        telegram_enabled=_to_bool(merged.get("TELEGRAM_ENABLED"), default=False),
        telegram_bot_token=str(merged.get("TELEGRAM_BOT_TOKEN") or "").strip() or None,
        telegram_allowed_chat_ids=_to_csv_list(
            merged.get("TELEGRAM_ALLOWED_CHAT_IDS")
        ),
        telegram_poll_limit=_to_int(merged.get("TELEGRAM_POLL_LIMIT"), default=100),
        telegram_smart_commands_enabled=_to_bool(
            merged.get("TELEGRAM_SMART_COMMANDS_ENABLED"), default=False
        ),
        telegram_assistant_enabled=_to_bool(
            merged.get("TELEGRAM_ASSISTANT_ENABLED"), default=False
        ),
        telegram_assistant_write_enabled=_to_bool(
            merged.get("TELEGRAM_ASSISTANT_WRITE_ENABLED"), default=False
        ),
        onboarding_public_base_url=str(
            merged.get("ONBOARDING_PUBLIC_BASE_URL") or ""
        ).strip()
        or None,
        onboarding_session_ttl_minutes=_to_int(
            merged.get("ONBOARDING_SESSION_TTL_MINUTES"),
            default=15,
        ),
        onboarding_allowed_school_slugs=_to_csv_list(
            merged.get("ONBOARDING_ALLOWED_SCHOOL_SLUGS")
        ),
        onboarding_browser_profiles_dir=_resolve_path_from_config_dir(
            onboarding_profiles_value,
            default=onboarding_profiles_default,
            config_dir=config_dir,
        ),
        onboarding_browser_channel=str(
            merged.get("ONBOARDING_BROWSER_CHANNEL") or ""
        ).strip(),
        onboarding_browser_executable_path=_resolve_optional_path_from_config_dir(
            merged.get("ONBOARDING_BROWSER_EXECUTABLE_PATH"),
            config_dir=config_dir,
        ),
        onboarding_browser_headless=_to_bool(
            merged.get("ONBOARDING_BROWSER_HEADLESS"),
            default=False,
        ),
        llm_enabled=_to_bool(merged.get("LLM_ENABLED"), default=False),
        llm_provider=str(merged.get("LLM_PROVIDER") or "local").strip().lower(),
        llm_model=str(merged.get("LLM_MODEL") or "gemma4").strip(),
        llm_local_endpoint=str(
            merged.get("LLM_LOCAL_ENDPOINT") or "http://127.0.0.1:11434/api/chat"
        ).strip(),
        llm_timeout_sec=_to_int(merged.get("LLM_TIMEOUT_SEC"), default=30),
        include_identity=_to_bool(merged.get("INCLUDE_IDENTITY"), default=False),
        sync_window_days=_to_int(merged.get("SYNC_WINDOW_DAYS"), default=120),
        material_extraction_enabled=_to_bool(
            merged.get("MATERIAL_EXTRACTION_ENABLED"), default=False
        ),
        material_briefing_enabled=_to_bool(
            merged.get("MATERIAL_BRIEFING_ENABLED"), default=False
        ),
        material_brief_push_enabled=_to_bool(
            merged.get("MATERIAL_BRIEF_PUSH_ENABLED"), default=False
        ),
        material_brief_push_max_items=_to_int(
            merged.get("MATERIAL_BRIEF_PUSH_MAX_ITEMS"), default=3
        ),
        material_extract_max_chars=_to_int(
            merged.get("MATERIAL_EXTRACT_MAX_CHARS"), default=12000
        ),
        review_enabled=_to_bool(merged.get("REVIEW_ENABLED"), default=False),
        review_intervals_days=_to_int_list(
            merged.get("REVIEW_INTERVALS_DAYS"), default=[1, 3, 7, 14]
        ),
        review_duration_min=_to_int(merged.get("REVIEW_DURATION_MIN"), default=25),
        review_morning_hour=_to_int(merged.get("REVIEW_MORNING_HOUR"), default=9),
        digest_enabled=_to_bool(merged.get("DIGEST_ENABLED"), default=False),
        digest_time_local=str(merged.get("DIGEST_TIME_LOCAL") or "08:30").strip(),
        digest_channel=str(merged.get("DIGEST_CHANNEL") or "telegram").strip().lower(),
        digest_task_lookahead_days=_to_int(
            merged.get("DIGEST_TASK_LOOKAHEAD_DAYS"), default=3
        ),
        briefing_enabled=_to_bool(merged.get("BRIEFING_ENABLED"), default=False),
        briefing_morning_time_local=str(
            merged.get("BRIEFING_MORNING_TIME_LOCAL") or "09:00"
        ).strip(),
        briefing_evening_time_local=str(
            merged.get("BRIEFING_EVENING_TIME_LOCAL") or "21:00"
        ).strip(),
        briefing_channel=str(merged.get("BRIEFING_CHANNEL") or "telegram").strip().lower(),
        briefing_delivery_mode=str(
            merged.get("BRIEFING_DELIVERY_MODE") or "direct"
        ).strip().lower(),
        briefing_relay_endpoint=(
            str(merged.get("BRIEFING_RELAY_ENDPOINT") or "").strip() or None
        ),
        briefing_relay_shared_secret=(
            str(merged.get("BRIEFING_RELAY_SHARED_SECRET") or "").strip() or None
        ),
        briefing_relay_state_file=_resolve_path_from_config_dir(
            merged.get("BRIEFING_RELAY_STATE_FILE"),
            default="data/briefing_relay_state.json",
            config_dir=config_dir,
        ),
        briefing_task_lookahead_days=_to_int(
            merged.get("BRIEFING_TASK_LOOKAHEAD_DAYS"), default=7
        ),
        briefing_max_classes=_to_int(merged.get("BRIEFING_MAX_CLASSES"), default=6),
        telegram_commands_enabled=_to_bool(
            merged.get("TELEGRAM_COMMANDS_ENABLED"), default=False
        ),
        weather_enabled=_to_bool(merged.get("WEATHER_ENABLED"), default=True),
        weather_location_label=str(
            merged.get("WEATHER_LOCATION_LABEL") or "서울특별시"
        ).strip(),
        weather_lat=_to_float(merged.get("WEATHER_LAT"), default=37.583801),
        weather_lon=_to_float(merged.get("WEATHER_LON"), default=127.058701),
        weather_kma_auth_key=str(merged.get("WEATHER_KMA_AUTH_KEY") or "").strip() or None,
        air_quality_enabled=_to_bool(merged.get("AIR_QUALITY_ENABLED"), default=True),
        air_quality_seoul_api_key=str(
            merged.get("AIR_QUALITY_SEOUL_API_KEY") or ""
        ).strip()
        or None,
        air_quality_district_codes=_to_csv_list(
            merged.get("AIR_QUALITY_DISTRICT_CODES") or "111152,111171"
        ),
        ops_dashboard_ssh_host=(
            str(merged.get("OPS_DASHBOARD_SSH_HOST") or "").strip() or None
        ),
        ops_dashboard_ssh_user=(
            str(merged.get("OPS_DASHBOARD_SSH_USER") or "").strip() or None
        ),
        ops_dashboard_ssh_port=_to_int(
            merged.get("OPS_DASHBOARD_SSH_PORT"), default=22
        ),
        ops_dashboard_remote_host=str(
            merged.get("OPS_DASHBOARD_REMOTE_HOST") or "127.0.0.1"
        ).strip()
        or "127.0.0.1",
        ops_dashboard_remote_port=_to_int(
            merged.get("OPS_DASHBOARD_REMOTE_PORT"), default=8793
        ),
        ops_dashboard_local_port=_to_int(
            merged.get("OPS_DASHBOARD_LOCAL_PORT"), default=8793
        ),
        ops_dashboard_url_path=_normalize_url_path(
            merged.get("OPS_DASHBOARD_URL_PATH"), default="/"
        ),
    )
    return settings


def load_instance_name(config_file: Path | None = None) -> str:
    selected_config = select_config_path(config_file=config_file)
    config_dir = selected_config.parent.resolve()
    config_values = _read_toml(selected_config)
    dotenv = {
        k.upper(): v
        for k, v in dotenv_values(config_dir / ".env").items()
        if v is not None
    }
    env_values = {k.upper(): v for k, v in os.environ.items()}
    merged: dict[str, Any] = {}
    merged.update(config_values)
    merged.update(dotenv)
    merged.update(env_values)
    return normalize_instance_name(merged.get("INSTANCE_NAME"))
