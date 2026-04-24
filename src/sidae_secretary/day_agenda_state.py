from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sidae_secretary.config import Settings
from sidae_secretary.db import Database
from sidae_secretary.telegram_setup_state import sync_dashboard_source_card


@dataclass(frozen=True)
class DayAgendaMeetingState:
    when: str
    title: str
    location: str | None


@dataclass(frozen=True)
class DayAgendaCourseState:
    when: str
    title: str
    location_text: str | None
    preparation: str | None
    notice_titles: tuple[str, ...]
    task_lines: tuple[str, ...]
    file_task_lines: tuple[str, ...]


@dataclass(frozen=True)
class DayAgendaState:
    target_day_local: datetime
    day_label: str
    summary_hint_command: str
    show_meeting_section: bool
    meetings_failed: bool
    skipped_reason: str
    meeting_items: tuple[DayAgendaMeetingState, ...]
    course_items: tuple[DayAgendaCourseState, ...]
    task_lines: tuple[str, ...]
    upcoming_task_lines: tuple[str, ...]
    empty_reason: str | None

    @property
    def is_empty(self) -> bool:
        return bool(self.empty_reason)


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _user_has_active_school_connection(db: Database, *, user_id: int | None = None) -> bool:
    owner_id = _safe_int(user_id)
    return bool(
        db.list_moodle_connections(user_id=owner_id, status="active", limit=1)
        or db.list_lms_browser_sessions(user_id=owner_id, status="active", limit=1)
    )


def _has_portal_snapshot(portal_sessions: list[dict[str, Any]]) -> bool:
    for item in portal_sessions:
        metadata = dict(item.get("metadata_json") or {}) if isinstance(item.get("metadata_json"), dict) else {}
        portal_sync = (
            dict(metadata.get("portal_timetable_sync"))
            if isinstance(metadata.get("portal_timetable_sync"), dict)
            else {}
        )
        if str(portal_sync.get("last_synced_at") or "").strip():
            return True
        if str(portal_sync.get("status") or "").strip().lower() == "success":
            return True
    return False


def _has_completed_sync(db: Database, *, user_id: int | None = None) -> bool:
    uclass_card = sync_dashboard_source_card(db, "uclass", user_id=user_id)
    portal_card = sync_dashboard_source_card(db, "portal", user_id=user_id)
    portal_sessions = [
        dict(item)
        for item in db.list_lms_browser_sessions(
            user_id=_safe_int(user_id),
            status="active",
            limit=10,
        )
    ]
    return bool(
        str(uclass_card.get("last_success_at") or "").strip()
        or str(portal_card.get("last_success_at") or "").strip()
        or _has_portal_snapshot(portal_sessions)
    )


def build_day_agenda_state(
    settings: Settings,
    db: Database,
    *,
    day_brief: Any,
    target_day_local: datetime,
    day_label: str,
    summary_hint_command: str,
    include_upcoming_tasks: bool,
    user_id: int | None = None,
    format_time_range_local: Callable[[datetime, datetime], str],
    format_task_line: Callable[[Any, datetime], str],
    is_task_due_on_target_day: Callable[[Any], bool],
) -> DayAgendaState:
    meetings_result = day_brief.meetings_result
    meeting_items = list(day_brief.meeting_items)
    course_briefs = list(day_brief.course_briefs)
    tasks_due_today = list(day_brief.tasks_due_on_day)
    upcoming_tasks = [
        task
        for task in day_brief.tasks_due_within_window
        if include_upcoming_tasks and not is_task_due_on_target_day(task)
    ]

    meetings_failed = isinstance(meetings_result, dict) and meetings_result.get("ok") is False
    skipped_reason = (
        str(meetings_result.get("skipped_reason") or "").strip()
        if isinstance(meetings_result, dict)
        else ""
    )
    show_meeting_section = bool(meeting_items) or meetings_failed or bool(skipped_reason)

    empty_reason: str | None = None
    if not meetings_failed and not meeting_items and not course_briefs and not tasks_due_today and not upcoming_tasks:
        if not _user_has_active_school_connection(db, user_id=user_id):
            empty_reason = "no_connection"
        elif not _has_completed_sync(db, user_id=user_id):
            empty_reason = "first_sync_pending"
        else:
            empty_reason = "no_items"

    rendered_meetings = tuple(
        DayAgendaMeetingState(
            when="하루 종일"
            if bool(item.get("all_day"))
            else format_time_range_local(item["start_local"], item["end_local"]),
            title=str(item.get("title") or "일정").strip() or "일정",
            location=str(item.get("location") or "").strip() or None,
        )
        for item in meeting_items[:6]
    )
    rendered_courses = tuple(
        DayAgendaCourseState(
            when=format_time_range_local(
                course_brief.class_item["start_local"],
                course_brief.class_item["end_local"],
            ),
            title=str(course_brief.class_item.get("title") or "수업").strip() or "수업",
            location_text=(
                str(course_brief.class_item.get("location_text") or "").strip() or None
            ),
            preparation=str(course_brief.preparation or "").strip() or None,
            notice_titles=tuple(str(item or "").strip() for item in course_brief.notice_titles if str(item or "").strip()),
            task_lines=tuple(str(item or "").strip() for item in course_brief.task_lines if str(item or "").strip()),
            file_task_lines=tuple(str(item or "").strip() for item in course_brief.file_task_lines if str(item or "").strip()),
        )
        for course_brief in course_briefs
    )
    rendered_task_lines = tuple(
        format_task_line(task, target_day_local)
        for task in tasks_due_today[:5]
    )
    rendered_upcoming_task_lines = tuple(
        format_task_line(task, target_day_local)
        for task in upcoming_tasks[:3]
    )

    return DayAgendaState(
        target_day_local=target_day_local,
        day_label=day_label,
        summary_hint_command=summary_hint_command,
        show_meeting_section=show_meeting_section,
        meetings_failed=meetings_failed,
        skipped_reason=skipped_reason,
        meeting_items=rendered_meetings,
        course_items=rendered_courses,
        task_lines=rendered_task_lines,
        upcoming_task_lines=rendered_upcoming_task_lines,
        empty_reason=empty_reason,
    )
