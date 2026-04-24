from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def _portal_timetable_metadata(extra: dict[str, object] | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {"timetable_source": "uos_portal"}
    if extra:
        metadata.update(extra)
    return metadata


def _clear_day_brief_cache() -> None:
    with pipeline._DAY_BRIEF_CACHE_LOCK:
        pipeline._DAY_BRIEF_CACHE.clear()


def _base_settings() -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Seoul",
        llm_enabled=False,
        llm_provider="local",
        llm_model="gemma4",
        weather_enabled=False,
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
    )


def test_day_brief_cache_hits_identical_request(tmp_path: Path, monkeypatch) -> None:
    _clear_day_brief_cache()
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])
    tz = ZoneInfo("Asia/Seoul")
    target_day = datetime(2026, 3, 9, 12, 0, tzinfo=tz)

    db.upsert_event(
        external_id="portal:econ-cache-hit",
        source="portal",
        start=target_day.replace(hour=10).isoformat(),
        end=(target_day.replace(hour=10) + timedelta(hours=1, minutes=15)).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:101"}
        ),
        user_id=user_id,
    )

    collect_calls = 0
    original_collect = pipeline._collect_class_occurrences

    def _counted_collect(*args, **kwargs):
        nonlocal collect_calls
        collect_calls += 1
        return original_collect(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_collect_class_occurrences", _counted_collect)

    service = pipeline.DayBriefService(_base_settings(), db, user_id=user_id)
    first = service.build_day_brief(
        target_day_local=target_day,
        reference_day_local=target_day,
        max_classes=6,
    )
    second = service.build_day_brief(
        target_day_local=target_day,
        reference_day_local=target_day,
        max_classes=6,
    )

    assert collect_calls == 1
    assert len(first.course_briefs) == 1
    assert len(second.course_briefs) == 1
    assert first is not second
    assert first.course_briefs[0].class_item["title"] == second.course_briefs[0].class_item["title"]


def test_day_brief_cache_invalidates_when_open_tasks_change(tmp_path: Path, monkeypatch) -> None:
    _clear_day_brief_cache()
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])
    tz = ZoneInfo("Asia/Seoul")
    target_day = datetime(2026, 3, 9, 12, 0, tzinfo=tz)

    db.upsert_event(
        external_id="portal:econ-cache-miss",
        source="portal",
        start=target_day.replace(hour=10).isoformat(),
        end=(target_day.replace(hour=10) + timedelta(hours=1, minutes=15)).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:101"}
        ),
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-cache-1",
        source="uclass",
        due_at=target_day.replace(hour=23, minute=59).isoformat(),
        title="과제 1",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )

    open_task_calls = 0
    original_open_tasks = db.list_open_tasks_due_from

    def _counted_open_tasks(*args, **kwargs):
        nonlocal open_task_calls
        open_task_calls += 1
        return original_open_tasks(*args, **kwargs)

    monkeypatch.setattr(db, "list_open_tasks_due_from", _counted_open_tasks)

    service = pipeline.DayBriefService(_base_settings(), db, user_id=user_id)
    first = service.build_day_brief(
        target_day_local=target_day,
        reference_day_local=target_day,
        max_classes=6,
    )
    db.upsert_task(
        external_id="uclass:task:econ-cache-2",
        source="uclass",
        due_at=target_day.replace(hour=22, minute=0).isoformat(),
        title="과제 2",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )
    second = service.build_day_brief(
        target_day_local=target_day,
        reference_day_local=target_day,
        max_classes=6,
    )

    assert open_task_calls == 2
    assert len(first.tasks_due_on_day) == 1
    assert len(second.tasks_due_on_day) == 2


def test_day_brief_cache_reuses_scheduled_briefing_request(tmp_path: Path, monkeypatch) -> None:
    _clear_day_brief_cache()
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])
    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime(2026, 3, 9, 9, 0, tzinfo=tz)

    db.upsert_event(
        external_id="portal:econ-scheduled-cache",
        source="portal",
        start=now_local.replace(hour=10).isoformat(),
        end=(now_local.replace(hour=10) + timedelta(hours=1, minutes=15)).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:101"}
        ),
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-scheduled-cache",
        source="uclass",
        due_at=now_local.replace(hour=23, minute=59).isoformat(),
        title="리딩메모 제출",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )

    collect_calls = 0
    original_collect = pipeline._collect_class_occurrences

    def _counted_collect(*args, **kwargs):
        nonlocal collect_calls
        collect_calls += 1
        return original_collect(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_collect_class_occurrences", _counted_collect)

    first_message, _ = pipeline._build_scheduled_briefing(
        settings=_base_settings(),
        db=db,
        slot="morning",
        now_local=now_local,
        user_id=user_id,
        enable_llm_guidance=False,
    )
    second_message, _ = pipeline._build_scheduled_briefing(
        settings=_base_settings(),
        db=db,
        slot="morning",
        now_local=now_local,
        user_id=user_id,
        enable_llm_guidance=False,
    )

    assert collect_calls == 1
    assert first_message == second_message
