from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.day_agenda_state import build_day_agenda_state
from sidae_secretary.db import Database


def _empty_day_brief() -> SimpleNamespace:
    return SimpleNamespace(
        meetings_result={},
        meeting_items=(),
        course_briefs=(),
        tasks_due_on_day=(),
        tasks_due_within_window=(),
    )


def test_build_day_agenda_state_marks_no_connection_empty_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    target_day_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)

    state = build_day_agenda_state(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        day_brief=_empty_day_brief(),
        target_day_local=target_day_local,
        day_label="오늘",
        summary_hint_command="/todaysummary",
        include_upcoming_tasks=True,
        format_time_range_local=lambda start, end: "unused",
        format_task_line=lambda task, reference_day_local: "unused",
        is_task_due_on_target_day=lambda task: False,
    )

    assert state.is_empty is True
    assert state.empty_reason == "no_connection"


def test_build_day_agenda_state_marks_first_sync_pending_empty_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    target_day_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)

    state = build_day_agenda_state(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        day_brief=_empty_day_brief(),
        target_day_local=target_day_local,
        day_label="오늘",
        summary_hint_command="/todaysummary",
        include_upcoming_tasks=True,
        user_id=int(user["id"]),
        format_time_range_local=lambda start, end: "unused",
        format_task_line=lambda task, reference_day_local: "unused",
        is_task_due_on_target_day=lambda task: False,
    )

    assert state.is_empty is True
    assert state.empty_reason == "first_sync_pending"


def test_build_day_agenda_state_treats_stale_tomorrow_only_timetable_as_no_items(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={
            "browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"},
            "portal_timetable_sync": {
                "status": "success",
                "auth_required": False,
                "event_count": 1,
                "last_synced_at": "2026-03-11T09:00:00+09:00",
            },
        },
        user_id=int(user["id"]),
    )
    target_day_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    tomorrow_local = target_day_local + timedelta(days=1)
    db.upsert_event(
        external_id="portal:uos:timetable:tomorrow-only",
        source="portal",
        start=tomorrow_local.replace(hour=9, minute=0).isoformat(),
        end=tomorrow_local.replace(hour=10, minute=15).isoformat(),
        title="오래된시간표",
        location="21-101",
        rrule=None,
        metadata_json={
            "school_slug": "uos_portal",
            "timetable_source": "uos_portal",
        },
        user_id=int(user["id"]),
    )

    state = build_day_agenda_state(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        day_brief=_empty_day_brief(),
        target_day_local=target_day_local,
        day_label="오늘",
        summary_hint_command="/todaysummary",
        include_upcoming_tasks=True,
        user_id=int(user["id"]),
        format_time_range_local=lambda start, end: "unused",
        format_task_line=lambda task, reference_day_local: "unused",
        is_task_due_on_target_day=lambda task: False,
    )

    assert state.is_empty is True
    assert state.empty_reason == "no_items"
