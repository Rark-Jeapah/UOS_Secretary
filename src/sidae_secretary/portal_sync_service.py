from __future__ import annotations

from importlib import import_module
import logging
from typing import Any, Protocol, cast

from sidae_secretary.db import Database


logger = logging.getLogger(__name__)


class PortalSyncAPI(Protocol):
    def prime_uos_portal_timetable_for_user(
        self,
        settings: Any,
        db: Database,
        *,
        chat_id: str | None = None,
        user_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        ...

    def record_uos_portal_timetable_fetch_for_user(
        self,
        settings: Any,
        db: Database,
        *,
        fetched: dict[str, Any],
        chat_id: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        ...


def _default_portal_sync_api() -> PortalSyncAPI:
    return cast(PortalSyncAPI, import_module("sidae_secretary.jobs.pipeline"))


def prime_post_connect_portal_sync(
    *,
    settings: Any,
    db: Database,
    chat_id: str,
    user_id: int | None,
    fetched: dict[str, Any] | None = None,
    portal_sync_api: PortalSyncAPI | None = None,
) -> dict[str, Any]:
    api = portal_sync_api or _default_portal_sync_api()
    try:
        if isinstance(fetched, dict) and fetched:
            return dict(
                api.record_uos_portal_timetable_fetch_for_user(
                    settings=settings,
                    db=db,
                    fetched=fetched,
                    chat_id=chat_id,
                    user_id=user_id,
                )
                or {}
            )
        return dict(
            api.prime_uos_portal_timetable_for_user(
                settings=settings,
                db=db,
                chat_id=chat_id,
                user_id=user_id,
                force=True,
            )
            or {}
        )
    except Exception as exc:
        logger.info("post-connect portal sync prime failed", exc_info=True)
        return {"ok": False, "error": str(exc).strip() or "post-connect portal sync failed"}
