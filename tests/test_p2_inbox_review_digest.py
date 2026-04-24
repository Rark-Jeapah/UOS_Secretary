from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def test_apply_inbox_items_creates_records_and_marks_processed(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_inbox_item(
        external_id="telegram:update:101",
        source="telegram",
        received_at="2026-03-04T09:00:00+09:00",
        title="Project meeting",
        body="Project meeting 2026-03-05 14:00",
        item_type="event_draft",
        draft_json={
            "title": "Project meeting",
            "start_at": "2026-03-05T14:00:00+09:00",
            "end_at": "2026-03-05T15:00:00+09:00",
        },
        processed=False,
        metadata_json={},
    )
    db.upsert_inbox_item(
        external_id="telegram:update:102",
        source="telegram",
        received_at="2026-03-04T10:00:00+09:00",
        title="Essay draft",
        body="Essay due tomorrow",
        item_type="task_draft",
        draft_json={
            "title": "Essay draft",
            "due_at": "2026-03-06T23:00:00+09:00",
            "status": "pending",
        },
        processed=False,
        metadata_json={},
    )
    db.upsert_inbox_item(
        external_id="telegram:update:103",
        source="telegram",
        received_at="2026-03-04T11:00:00+09:00",
        title="Random note",
        body="Remember to ask TA",
        item_type="note",
        draft_json={"title": "Random note"},
        processed=False,
        metadata_json={},
    )

    result = pipeline.apply_inbox_items(
        settings=SimpleNamespace(),
        db=db,
        apply_all=True,
    )

    assert result["processed"] == 3
    assert result["created_events"] == 1
    assert result["created_tasks"] == 1
    assert result["notes"] == 1
    assert db.list_unprocessed_inbox() == []
    assert any(item.external_id == "inbox:101" for item in db.list_events(limit=20))
    assert any(item.external_id == "inbox:102" for item in db.list_open_tasks(limit=20))


def test_schedule_review_events_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_event(
        external_id="portal:event:1",
        source="portal",
        start="2026-03-04T09:00:00+09:00",
        end="2026-03-04T10:15:00+09:00",
        title="Algorithms",
        location="Room 201",
        rrule=None,
        metadata_json={"timetable_source": "uos_portal"},
    )
    db.record_artifact(
        external_id="uclass:artifact:abc",
        source="uclass",
        filename="week1.pdf",
        icloud_path=str(tmp_path / "week1.pdf"),
        content_hash="hash",
        metadata_json={"course_name": "Algorithms"},
    )
    settings = SimpleNamespace(
        review_enabled=True,
        review_intervals_days=[1, 3],
        review_duration_min=25,
        review_morning_hour=9,
        timezone="Asia/Seoul",
    )

    first = pipeline.schedule_review_events(settings=settings, db=db)
    second = pipeline.schedule_review_events(settings=settings, db=db)

    review_events = [
        item for item in db.list_events(limit=200) if item.source == "review"
    ]
    assert first["generated"] == 4
    assert second["generated"] == 0
    assert len(review_events) == 4


def test_send_daily_digest_only_once_per_day(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_notification(
        external_id="uclass:notif:1",
        source="uclass",
        created_at="2026-03-04T00:00:00+00:00",
        title="Forum notice",
        body="Read chapter 2",
        url=None,
        metadata_json={},
    )
    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2026-03-05T10:00:00+09:00",
        title="Lab report",
        status="pending",
        metadata_json={},
    )
    db.record_artifact(
        external_id="uclass:artifact:1",
        source="uclass",
        filename="slides.pdf",
        icloud_path=str(tmp_path / "slides.pdf"),
        content_hash="abc",
        metadata_json={},
    )

    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        digest_enabled=True,
        digest_channel="telegram",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        digest_time_local="00:00",
        timezone="Asia/Seoul",
        digest_task_lookahead_days=3,
    )

    first = pipeline.send_daily_digest(settings=settings, db=db)
    second = pipeline.send_daily_digest(settings=settings, db=db)

    assert first["sent_to"] == ["12345"]
    assert second["skipped"] is True
    assert "already sent today" in second["reason"]
    assert len(sent_messages) == 1


def test_send_daily_digest_respects_notification_policy_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_notification(
        external_id="uclass:notif:1",
        source="uclass",
        created_at="2026-04-05T12:00:00+00:00",
        title="Forum notice",
        body="Read chapter 2",
        url=None,
        metadata_json={},
    )
    db.upsert_user_preferences(chat_id="12345", daily_digest_enabled=True)
    db.upsert_user_preferences(chat_id="67890", daily_digest_enabled=False)
    db.upsert_notification_policy(
        chat_id="12345",
        policy_kind="daily_digest",
        enabled=False,
    )
    db.upsert_notification_policy(
        chat_id="67890",
        policy_kind="daily_digest",
        enabled=True,
        days_of_week_json=["mon"],
        time_local="08:00",
        timezone="Asia/Seoul",
    )
    sent_messages: list[tuple[str, str]] = []
    fixed_now = datetime(2026, 4, 6, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "datetime", FakeDateTime)
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        digest_enabled=True,
        digest_channel="telegram",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=[],
        digest_time_local="00:00",
        timezone="Asia/Seoul",
        digest_task_lookahead_days=3,
        include_identity=False,
    )

    result = pipeline.send_daily_digest(settings=settings, db=db)

    assert result["sent_to"] == ["67890"]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "67890"
