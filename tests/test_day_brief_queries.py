from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def test_list_open_tasks_due_from_filters_past_and_missing_due(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])

    seoul_tz = ZoneInfo("Asia/Seoul")
    reference_day = datetime(2026, 3, 9, 9, 0, tzinfo=seoul_tz)

    db.upsert_task(
        external_id="task:past",
        source="uclass",
        due_at=reference_day - timedelta(days=1),
        title="지난 과제",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )
    db.upsert_task(
        external_id="task:future",
        source="uclass",
        due_at=reference_day + timedelta(hours=3),
        title="오늘 과제",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )
    db.upsert_task(
        external_id="task:no-due",
        source="uclass",
        due_at=None,
        title="기한 없음",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )

    rows = db.list_open_tasks_due_from(reference_day.isoformat(), limit=10, user_id=user_id)

    assert [row.external_id for row in rows] == ["task:future"]


def test_day_brief_service_uses_scoped_recent_queries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])

    seoul_tz = ZoneInfo("Asia/Seoul")
    target_day = datetime(2026, 3, 9, 12, 0, tzinfo=seoul_tz)
    reference_day = datetime(2026, 3, 8, 22, 0, tzinfo=seoul_tz)

    calls: dict[str, tuple[object, ...]] = {}

    def _unexpected(*args, **kwargs):
        raise AssertionError("broad list helper should not be used")

    def _list_artifacts_since(since_iso: str, limit: int = 100, *, user_id: int | None = None):
        calls["artifacts"] = (since_iso, limit, user_id)
        return []

    def _list_notifications_since(since_iso: str, limit: int = 100, *, user_id: int | None = None):
        calls["notifications"] = (since_iso, limit, user_id)
        return []

    def _list_open_tasks_due_from(
        since_iso: str,
        limit: int = 500,
        *,
        until_iso: str | None = None,
        user_id: int | None = None,
    ):
        calls["open_tasks"] = (since_iso, limit, until_iso, user_id)
        return []

    monkeypatch.setattr(db, "list_artifacts", _unexpected)
    monkeypatch.setattr(db, "list_notifications", _unexpected)
    monkeypatch.setattr(db, "list_open_tasks", _unexpected)
    monkeypatch.setattr(db, "list_artifacts_since", _list_artifacts_since)
    monkeypatch.setattr(db, "list_notifications_since", _list_notifications_since)
    monkeypatch.setattr(db, "list_open_tasks_due_from", _list_open_tasks_due_from)

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        llm_enabled=False,
        llm_provider="local",
        llm_model="gemma4",
    )

    day_brief = pipeline.DayBriefService(settings, db, user_id=user_id).build_day_brief(
        target_day_local=target_day,
        reference_day_local=reference_day,
        max_classes=6,
        artifact_limit=321,
        notification_limit=54,
        open_task_limit=87,
        lookahead_days=7,
        lookahead_limit=10,
        lookahead_now_iso=target_day.astimezone(timezone.utc).isoformat(),
    )

    expected_artifact_since = (
        target_day.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=pipeline.DAY_BRIEF_ARTIFACT_LOOKBACK_DAYS)
    ).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    expected_notification_since = (
        reference_day.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=pipeline.DAY_BRIEF_NOTIFICATION_LOOKBACK_DAYS)
    ).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    expected_open_task_since = (
        reference_day.replace(hour=0, minute=0, second=0, microsecond=0)
    ).astimezone(timezone.utc).replace(microsecond=0).isoformat()

    assert day_brief.course_briefs == ()
    assert calls["artifacts"] == (expected_artifact_since, 321, user_id)
    assert calls["notifications"] == (expected_notification_since, 54, user_id)
    assert calls["open_tasks"] == (expected_open_task_since, 87, None, user_id)


def test_format_telegram_day_uses_shaped_limits(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    captured: dict[str, object] = {}

    class FakeDayBriefService:
        def __init__(self, settings, database, *, user_id=None):
            captured["init"] = (settings, database, user_id)

        def build_day_brief(self, **kwargs):
            captured["kwargs"] = kwargs
            target_day_local = kwargs["target_day_local"]
            return pipeline.DayBrief(
                target_day_local=target_day_local,
                meetings_result={"ok": True, "events": []},
                meeting_items=(),
                course_briefs=(),
                tasks_due_on_day=(),
                tasks_due_within_window=(),
            )

    monkeypatch.setattr(pipeline, "DayBriefService", FakeDayBriefService)

    settings = SimpleNamespace(timezone="Asia/Seoul")
    target_day = datetime(2026, 3, 9, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    pipeline._format_telegram_day(
        settings,
        db,
        target_day_local=target_day,
        day_label="오늘",
        summary_hint_command="/todaysummary",
        include_upcoming_tasks=True,
    )

    assert captured["kwargs"]["artifact_limit"] == pipeline.TELEGRAM_DAY_ARTIFACT_LIMIT
    assert captured["kwargs"]["notification_limit"] == pipeline.TELEGRAM_DAY_NOTIFICATION_LIMIT
    assert captured["kwargs"]["open_task_limit"] == pipeline.TELEGRAM_DAY_OPEN_TASK_LIMIT
