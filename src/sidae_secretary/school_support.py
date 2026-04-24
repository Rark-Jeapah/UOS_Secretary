from __future__ import annotations

from typing import Any


TRULY_SUPPORTED = "truly_supported"
PARTIALLY_SUPPORTED = "partially_supported"
UNSUPPORTED = "unsupported"

PASSWORD_TOKEN_AUTH_MODE = "password_token"
BROWSER_SESSION_AUTH_MODE = "browser_session"
DEFAULT_LMS_PROVIDER = "moodle"
OFFICIALLY_SUPPORTED_SCHOOL_SLUGS = {"uos_online_class", "uos_portal"}


def _entry_metadata(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    metadata = entry.get("metadata_json")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _portal_metadata(entry: dict[str, Any] | None) -> dict[str, Any]:
    metadata = _entry_metadata(entry)
    portal = metadata.get("portal")
    return dict(portal) if isinstance(portal, dict) else {}


def is_officially_supported_school_slug(school_slug: str | None) -> bool:
    slug = str(school_slug or "").strip().lower()
    return bool(slug) and slug in OFFICIALLY_SUPPORTED_SCHOOL_SLUGS


def school_support_summary(entry: dict[str, Any] | None) -> dict[str, Any]:
    item = entry if isinstance(entry, dict) else {}
    metadata = _entry_metadata(item)
    portal = _portal_metadata(item)

    school_slug = str(item.get("school_slug") or "").strip().lower()
    display_name = str(item.get("display_name") or school_slug or "").strip()
    provider = str(metadata.get("provider") or DEFAULT_LMS_PROVIDER).strip().lower()
    auth_mode = str(metadata.get("auth_mode") or PASSWORD_TOKEN_AUTH_MODE).strip().lower()
    ws_base_url = str(item.get("ws_base_url") or "").strip()
    portal_login_url = str(portal.get("login_url") or "").strip()
    portal_timetable_support = str(portal.get("timetable_support") or "").strip().lower()

    capabilities = {
        "directory_listing": bool(school_slug or display_name),
        "lms_credential_onboarding": bool(ws_base_url),
        "lms_sync": bool(ws_base_url) and provider == DEFAULT_LMS_PROVIDER,
        "portal_shared_account_hint": bool(portal_login_url) and bool(
            portal.get("shared_school_account", True)
        ),
        "portal_browser_session_onboarding": auth_mode == BROWSER_SESSION_AUTH_MODE,
        "portal_timetable_sync": portal_timetable_support == "supported",
        "school_notice_sync": is_officially_supported_school_slug(school_slug),
    }
    official_support = is_officially_supported_school_slug(school_slug)
    if official_support and (
        capabilities["lms_sync"] or capabilities["portal_timetable_sync"]
    ):
        support_level = TRULY_SUPPORTED
    elif any(capabilities.values()):
        support_level = PARTIALLY_SUPPORTED
    else:
        support_level = UNSUPPORTED

    return {
        "school_slug": school_slug or None,
        "display_name": display_name or None,
        "support_level": support_level,
        "official_user_support": official_support,
        "provider": provider or DEFAULT_LMS_PROVIDER,
        "auth_mode": auth_mode or PASSWORD_TOKEN_AUTH_MODE,
        "capabilities": capabilities,
    }
