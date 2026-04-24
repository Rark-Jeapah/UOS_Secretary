from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.db import Database, now_utc_iso
from sidae_secretary.jobs import pipeline


def test_sync_telegram_plan_command_schedules_reminder(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            if isinstance(offset, int) and offset > 10:
                return []
            return [
                {
                    "update_id": 10,
                    "message": {
                        "date": 1770000000,
                        "text": "/plan tomorrow 8am remind me to check uclass",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    monkeypatch.setattr(
        pipeline,
        "_plan_instruction_with_llm",
        lambda settings, db, text: {
            "ok": True,
            "mode": "test",
            "plan": {"action": "schedule", "run_at_iso": run_at, "message": "check uclass"},
        },
    )
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        telegram_smart_commands_enabled=True,
        llm_enabled=False,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    pending = db.list_telegram_reminders(status="pending", limit=10)
    assert result["commands"]["processed"] == 1
    assert result["reminders"]["due"] == 0
    assert len(pending) == 1
    assert pending[0]["chat_id"] == "12345"
    assert any("[Sidae] 리마인더 예약" in text for _, text in sent_messages)
    assert any("예약 결과" in text for _, text in sent_messages)
    assert any("check uclass" in text for _, text in sent_messages)


def test_sync_telegram_dispatches_due_reminders(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return []

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    db.upsert_telegram_reminder(
        external_id="tg-reminder:test-1",
        chat_id="12345",
        run_at=now_utc_iso(),
        message="submit assignment",
        metadata_json={},
    )
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=False,
        telegram_smart_commands_enabled=True,
        llm_enabled=False,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    sent_rows = db.list_telegram_reminders(status="sent", limit=10)
    assert result["reminders"]["due"] == 1
    assert result["reminders"]["sent"] == 1
    assert len(sent_rows) == 1
    assert any("[Reminder] submit assignment" in text for _, text in sent_messages)


def test_send_scheduled_briefings_sends_once_per_day(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz)
    today = now_local.date()
    tomorrow = (now_local + timedelta(days=1)).date()
    sent_messages: list[tuple[str, str]] = []

    def _iso(day, hour: int, minute: int) -> str:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).isoformat()

    db.upsert_building("21", "Main Hall", metadata_json={})
    db.upsert_event(
        external_id="portal:today-1",
        source="portal",
        start=_iso(today, 9, 0),
        end=_iso(today, 10, 15),
        title="Algorithms",
        location="21-101",
        rrule=None,
        metadata_json={"timetable_source": "uos_portal"},
    )
    db.upsert_event(
        external_id="portal:tomorrow-1",
        source="portal",
        start=_iso(tomorrow, 11, 0),
        end=_iso(tomorrow, 12, 15),
        title="Algorithms",
        location="21-101",
        rrule=None,
        metadata_json={"timetable_source": "uos_portal"},
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-1",
        source="uclass",
        filename="week3.pptx",
        icloud_path=str(tmp_path / "week3.pptx"),
        content_hash="hash-1",
        metadata_json={
            "course_name": "Algorithms",
            "brief": {"bullets": ["Graph shortest path intro", "Dijkstra examples"]},
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-html",
        source="uclass",
        filename="view.php",
        icloud_path=str(tmp_path / "view.php"),
        content_hash="hash-html",
        metadata_json={
            "course_name": "Algorithms",
            "content_type": "text/html; charset=utf-8",
            "module_name": "공지사항",
            "text_extract": {
                "ok": True,
                "type": "html",
                "excerpt": "서울시립대학교 온라인강의실 로그인 아이디 비밀번호",
            },
            "brief": {
                "bullets": [
                    "제공된 텍스트는 강의실 로그인 페이지 정보일 뿐 실제 수업 내용이나 강의 주제가 포함되어 있지 않습니다.",
                ],
                "question": "실제 수업 자료를 제공해 주세요.",
            },
        },
    )
    db.upsert_notification(
        external_id="uclass:notif:alg-1",
        source="uclass",
        created_at=now_utc_iso(),
        title="Algorithms quiz notice",
        body="Quiz scope updated",
        url=None,
        metadata_json={},
    )
    db.upsert_task(
        external_id="uclass:assign:alg-1",
        source="uclass",
        due_at=_iso(tomorrow, 23, 0),
        title="Algorithms HW3",
        status="open",
        metadata_json={
            "course_name": "Algorithms",
            "summary": "Implement shortest paths and compare runtime.",
        },
    )
    db.update_sync_state(
        "sync_weather",
        last_run_at="2026-03-08T01:00:00+00:00",
        last_cursor_json={
            "generated_at": "2026-03-08T10:00:00+09:00",
            "location_label": "서울시립대",
            "current": {
                "temperature_c": 7.0,
                "condition_text": "맑음",
            },
            "air_quality": {
                "ok": True,
                "measured_at": "2026-03-08T10:00:00+09:00",
                "districts": [
                    {
                        "district_code": "111152",
                        "district_name": "동대문구",
                        "cai_grade": "보통",
                        "dominant_pollutant": "PM-2.5",
                        "pm10": 56,
                        "pm25": 37,
                    },
                    {
                        "district_code": "111171",
                        "district_name": "도봉구",
                        "cai_grade": "보통",
                        "dominant_pollutant": "PM-2.5",
                        "pm10": 58,
                        "pm25": 36,
                    },
                ],
            },
            "today": {
                "date": "2026-03-08",
                "temperature_min_c": 2.0,
                "temperature_max_c": 12.0,
                "diurnal_range_c": 10.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 2.0,
                    "temperature_max_c": 7.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 10,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 8.0,
                    "temperature_max_c": 12.0,
                    "condition_text": "구름많음",
                    "precip_probability_max": 20,
                },
            },
            "tomorrow": {
                "date": "2026-03-09",
                "temperature_min_c": 3.0,
                "temperature_max_c": 14.0,
                "diurnal_range_c": 11.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 3.0,
                    "temperature_max_c": 8.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 0,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 10.0,
                    "temperature_max_c": 14.0,
                    "condition_text": "흐림",
                    "precip_probability_max": 60,
                },
            },
        },
    )
    db.update_sync_state(
        "sync_weather",
        last_run_at=datetime(2026, 3, 8, 9, 0, tzinfo=tz).astimezone(ZoneInfo("UTC")).isoformat(),
        last_cursor_json={
            "generated_at": "2026-03-08T10:00:00+09:00",
            "location_label": "서울시립대",
            "current": {
                "temperature_c": 7.0,
                "condition_text": "맑음",
            },
            "air_quality": {
                "ok": True,
                "measured_at": "2026-03-08T10:00:00+09:00",
                "districts": [
                    {
                        "district_code": "111152",
                        "district_name": "동대문구",
                        "cai_grade": "보통",
                        "dominant_pollutant": "PM-2.5",
                        "pm10": 56,
                        "pm25": 37,
                    },
                    {
                        "district_code": "111171",
                        "district_name": "도봉구",
                        "cai_grade": "보통",
                        "dominant_pollutant": "PM-2.5",
                        "pm10": 58,
                        "pm25": 36,
                    },
                ],
            },
            "today": {
                "date": "2026-03-08",
                "temperature_min_c": 2.0,
                "temperature_max_c": 12.0,
                "diurnal_range_c": 10.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 2.0,
                    "temperature_max_c": 7.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 10,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 8.0,
                    "temperature_max_c": 12.0,
                    "condition_text": "구름많음",
                    "precip_probability_max": 20,
                },
            },
            "tomorrow": {
                "date": "2026-03-09",
                "temperature_min_c": 3.0,
                "temperature_max_c": 14.0,
                "diurnal_range_c": 11.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 3.0,
                    "temperature_max_c": 8.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 0,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 10.0,
                    "temperature_max_c": 14.0,
                    "condition_text": "흐림",
                    "precip_probability_max": 60,
                },
            },
        },
    )
    db.upsert_task(
        external_id="uclass:material-task:alg-2",
        source="uclass",
        due_at=_iso(tomorrow, 21, 0),
        title="과제 1 수업시작 이전 교탁으로",
        status="open",
        metadata_json={
            "course_name": "Algorithms",
            "detected_via": "material_deadline",
            "detected_method": "heuristic",
            "evidence": "과제 1: 수업 시작 이전까지 교탁으로 제출",
        },
    )
    db.upsert_task(
        external_id="uclass:material-task:alg-2-dup",
        source="uclass",
        due_at=_iso(tomorrow, 21, 0),
        title="과제 1 수업시작 이전 교탁으로",
        status="open",
        metadata_json={
            "course_name": "형 Algorithms (01)",
            "detected_via": "material_deadline",
            "detected_method": "heuristic",
            "evidence": "과제 1: 수업 시작 이전까지 교탁으로 제출",
        },
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    def _fake_meetings(settings, db, *, target_day_local, user_id=None):
        start = target_day_local.replace(hour=13, minute=0, second=0, microsecond=0)
        end = target_day_local.replace(hour=14, minute=0, second=0, microsecond=0)
        return {
            "ok": True,
            "events": [
                {
                    "title": "Team meeting",
                    "all_day": False,
                    "start_local": start,
                    "end_local": end,
                    "location": "Cafe",
                }
            ],
        }

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(pipeline, "_collect_primary_meetings_scoped", _fake_meetings)
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_morning_time_local="00:00",
        briefing_evening_time_local="00:00",
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        timezone="Asia/Seoul",
        llm_enabled=False,
        include_identity=False,
    )

    first = pipeline.send_scheduled_briefings(settings=settings, db=db)
    second = pipeline.send_scheduled_briefings(settings=settings, db=db)

    assert set(first["sent_slots"]) == {"morning", "evening"}
    assert len(sent_messages) == 2
    assert any("아침 브리핑" in text for _, text in sent_messages)
    assert any("저녁 브리핑" in text for _, text in sent_messages)
    assert any("Graph shortest path intro" in text for _, text in sent_messages)
    assert any("101호에서 Algorithms 수업" in text for _, text in sent_messages)
    assert any("파일 감지 과제" in text for _, text in sent_messages)
    assert any("[파일] 과제 1 수업시작 이전 교탁으로" in text for _, text in sent_messages)
    assert any("[Algorithms]" in text for _, text in sent_messages)
    assert any("Implement shortest paths and compare runtime" in text for _, text in sent_messages)
    assert any("\n미세먼지 (10:00 기준)\n" in text for _, text in sent_messages)
    assert any("- 동대문구 나쁨" in text for _, text in sent_messages)
    assert any("도봉구" in text for _, text in sent_messages)
    assert any("\n내일 날씨\n" in text for _, text in sent_messages)
    assert any("수업 시작 이전까지 교탁으로 제출" in text for _, text in sent_messages)
    assert all("오늘 기준일:" not in text for _, text in sent_messages if "아침 브리핑" in text)
    assert all("주의:" not in text for _, text in sent_messages)
    assert all("view.php" not in text for _, text in sent_messages)
    assert all("로그인 페이지" not in text for _, text in sent_messages)
    assert all("오늘 복습 알림" not in text for _, text in sent_messages)
    assert second["skipped"] is True
    assert len(sent_messages) == 2


def test_send_scheduled_briefings_includes_connected_chat_outside_allowlist(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="77777", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="77777",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="tester",
        secret_kind="inline",
        secret_ref="dummy-secret",
        status="active",
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    def _fake_build_scheduled_briefing(*, slot, **kwargs):
        return (f"[Sidae] {slot} briefing", {"slot": slot})

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(pipeline, "_build_scheduled_briefing", _fake_build_scheduled_briefing)
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_morning_time_local="00:00",
        briefing_evening_time_local="00:00",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=[],
        timezone="Asia/Seoul",
        include_identity=False,
    )

    result = pipeline.send_scheduled_briefings(settings=settings, db=db)

    assert set(result["sent_slots"]) == {"morning", "evening"}
    assert [chat_id for chat_id, _ in sent_messages] == ["77777", "77777"]
    assert result["results"]["77777"]["user_id"] == int(user["id"])


def test_send_scheduled_briefings_respects_notification_policy_weekdays(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []
    fixed_now = datetime(2026, 4, 6, 21, 30, tzinfo=ZoneInfo("Asia/Seoul"))

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

    db.upsert_user_preferences(chat_id="111", scheduled_briefings_enabled=True)
    db.upsert_user_preferences(chat_id="222", scheduled_briefings_enabled=True)
    db.upsert_user_preferences(chat_id="333", scheduled_briefings_enabled=True)
    db.upsert_notification_policy(
        chat_id="111",
        policy_kind="briefing_morning",
        enabled=True,
        days_of_week_json=["mon"],
        time_local="08:00",
    )
    db.upsert_notification_policy(
        chat_id="111",
        policy_kind="briefing_evening",
        enabled=False,
    )
    db.upsert_notification_policy(
        chat_id="222",
        policy_kind="briefing_morning",
        enabled=True,
        days_of_week_json=["tue"],
    )
    db.upsert_notification_policy(
        chat_id="222",
        policy_kind="briefing_evening",
        enabled=True,
        days_of_week_json=["tue"],
    )

    monkeypatch.setattr(pipeline, "datetime", FakeDateTime)
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(
        pipeline,
        "_build_scheduled_briefing",
        lambda *, slot, **kwargs: (f"[Sidae] {slot} briefing", {"slot": slot}),
    )
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_delivery_mode="direct",
        briefing_morning_time_local="09:00",
        briefing_evening_time_local="21:00",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=[],
        timezone="Asia/Seoul",
        include_identity=False,
    )

    result = pipeline.send_scheduled_briefings(settings=settings, db=db)

    assert [chat_id for chat_id, _ in sent_messages] == ["111", "333", "333"]
    assert result["results"]["111"]["sent_slots"] == ["morning"]
    assert result["results"]["333"]["sent_slots"] == ["morning", "evening"]
    assert "222" not in result["results"]


def test_build_precomputed_telegram_briefings_targets_next_slot(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    tz = ZoneInfo("Asia/Seoul")
    today = datetime(2026, 3, 8, 12, 0, tzinfo=tz).date()
    tomorrow = today + timedelta(days=1)

    def _iso(day, hour: int, minute: int) -> str:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).isoformat()

    db.upsert_building("21", "Main Hall", metadata_json={})
    db.upsert_event(
        external_id="portal:tomorrow-1",
        source="portal",
        start=_iso(tomorrow, 11, 0),
        end=_iso(tomorrow, 12, 15),
        title="Algorithms",
        location="21-101",
        rrule=None,
        metadata_json={"timetable_source": "uos_portal"},
    )
    db.upsert_task(
        external_id="uclass:assign:alg-1",
        source="uclass",
        due_at=_iso(tomorrow, 23, 0),
        title="Algorithms HW3",
        status="open",
        metadata_json={
            "course_name": "Algorithms",
            "summary": "Implement shortest paths and compare runtime.",
        },
    )
    db.update_sync_state(
        "sync_weather",
        last_run_at="2026-03-08T01:00:00+00:00",
        last_cursor_json={
            "generated_at": "2026-03-08T10:00:00+09:00",
            "location_label": "서울시립대",
            "current": {
                "temperature_c": 7.0,
                "condition_text": "맑음",
            },
            "air_quality": {
                "ok": True,
                "measured_at": "2026-03-08T10:00:00+09:00",
                "districts": [
                    {
                        "district_code": "111152",
                        "district_name": "동대문구",
                        "cai_grade": "보통",
                        "dominant_pollutant": "PM-2.5",
                        "pm10": 56,
                        "pm25": 37,
                    },
                    {
                        "district_code": "111171",
                        "district_name": "도봉구",
                        "cai_grade": "보통",
                        "dominant_pollutant": "PM-2.5",
                        "pm10": 58,
                        "pm25": 36,
                    },
                ],
            },
            "today": {
                "date": "2026-03-08",
                "temperature_min_c": 2.0,
                "temperature_max_c": 12.0,
                "diurnal_range_c": 10.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 2.0,
                    "temperature_max_c": 7.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 10,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 8.0,
                    "temperature_max_c": 12.0,
                    "condition_text": "구름많음",
                    "precip_probability_max": 20,
                },
            },
            "tomorrow": {
                "date": "2026-03-09",
                "temperature_min_c": 3.0,
                "temperature_max_c": 14.0,
                "diurnal_range_c": 11.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 3.0,
                    "temperature_max_c": 8.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 0,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 10.0,
                    "temperature_max_c": 14.0,
                    "condition_text": "흐림",
                    "precip_probability_max": 60,
                },
            },
        },
    )

    def _fake_meetings(settings, db, *, target_day_local, user_id=None):
        start = target_day_local.replace(hour=13, minute=0, second=0, microsecond=0)
        end = target_day_local.replace(hour=14, minute=0, second=0, microsecond=0)
        return {
            "ok": True,
            "events": [
                {
                    "title": "Team meeting",
                    "all_day": False,
                    "start_local": start,
                    "end_local": end,
                    "location": "Cafe",
                }
            ],
        }

    monkeypatch.setattr(pipeline, "_collect_primary_meetings_scoped", _fake_meetings)
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_morning_time_local="09:00",
        briefing_evening_time_local="21:00",
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        timezone="Asia/Seoul",
        llm_enabled=False,
        include_identity=False,
    )

    result = pipeline.build_precomputed_telegram_briefings(
        settings=settings,
        db=db,
        now_local=datetime(2026, 3, 8, 10, 30, tzinfo=tz),
    )

    assert result["ok"] is True
    assert result["items"]["2026-03-09-morning"]["send_at_local"] == "2026-03-09T09:00:00+09:00"
    assert result["items"]["2026-03-08-evening"]["send_at_local"] == "2026-03-08T21:00:00+09:00"
    assert "아침 브리핑" in result["items"]["2026-03-09-morning"]["message"]
    assert "저녁 브리핑" in result["items"]["2026-03-08-evening"]["message"]
    assert "오늘 기준일" not in result["items"]["2026-03-09-morning"]["message"]
    assert "내일 기준일" in result["items"]["2026-03-08-evening"]["message"]
    assert "오늘 날씨" in result["items"]["2026-03-09-morning"]["message"]
    assert "오늘 날씨" in result["items"]["2026-03-08-evening"]["message"]
    assert "내일 날씨" in result["items"]["2026-03-08-evening"]["message"]
    assert "오전 :" in result["items"]["2026-03-08-evening"]["message"]
    assert "오후 :" in result["items"]["2026-03-08-evening"]["message"]
    assert "\n미세먼지 (10:00 기준)\n" in result["items"]["2026-03-08-evening"]["message"]
    assert "동대문구 나쁨" in result["items"]["2026-03-08-evening"]["message"]
    assert "도봉구" in result["items"]["2026-03-08-evening"]["message"]
    assert "주의:" not in result["items"]["2026-03-08-evening"]["message"]
    assert "[Algorithms]" in result["items"]["2026-03-08-evening"]["message"]
    assert "오늘 복습 알림" not in result["items"]["2026-03-08-evening"]["message"]


def test_build_precomputed_telegram_briefings_uses_user_specific_weather_and_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(
        chat_id="111",
        scheduled_briefings_enabled=True,
        weather_location_label="동대문구",
        weather_lat=37.5744,
        weather_lon=127.0396,
    )
    db.upsert_user_preferences(
        chat_id="222",
        scheduled_briefings_enabled=True,
    )
    fetch_calls: list[tuple[str, float, float]] = []
    tz = ZoneInfo("Asia/Seoul")

    class FakeWeatherClient:
        def __init__(self, auth_key=None):
            self.auth_key = auth_key

        def fetch_snapshot(self, *, lat, lon, location_label, timezone_name, now_local=None):
            fetch_calls.append((str(location_label), float(lat), float(lon)))
            today = datetime.now(tz).date()
            if str(location_label) == "동대문구":
                temperature = 7.0
                condition = "맑음"
            else:
                temperature = 4.0
                condition = "흐림"
            return {
                "generated_at": "2026-03-08T10:00:00+09:00",
                "location_label": location_label,
                "current": {
                    "temperature_c": temperature,
                    "condition_text": condition,
                },
                "today": {
                    "date": today.isoformat(),
                    "morning": {
                        "label": "오전",
                        "temperature_min_c": temperature - 2,
                        "temperature_max_c": temperature,
                        "condition_text": condition,
                        "precip_probability_max": 10,
                    },
                    "afternoon": {
                        "label": "오후",
                        "temperature_min_c": temperature + 1,
                        "temperature_max_c": temperature + 3,
                        "condition_text": condition,
                        "precip_probability_max": 20,
                    },
                },
                "tomorrow": {
                    "date": (today + timedelta(days=1)).isoformat(),
                    "temperature_min_c": temperature - 1,
                    "temperature_max_c": temperature + 2,
                },
            }

    monkeypatch.setattr(pipeline, "KMAWeatherClient", FakeWeatherClient)
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_morning_time_local="09:00",
        briefing_evening_time_local="21:00",
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=[],
        timezone="Asia/Seoul",
        llm_enabled=False,
        include_identity=False,
        weather_enabled=True,
        air_quality_enabled=False,
        weather_location_label="서울특별시",
        weather_lat=37.5665,
        weather_lon=126.9780,
    )

    result = pipeline.build_precomputed_telegram_briefings(
        settings=settings,
        db=db,
        now_local=datetime(2026, 3, 8, 10, 30, tzinfo=tz),
    )

    morning = result["items"]["2026-03-09-morning"]["messages_by_chat"]
    evening = result["items"]["2026-03-08-evening"]["messages_by_chat"]

    assert result["ok"] is True
    assert len(fetch_calls) == 2
    assert "- 지역 동대문구" in morning["111"]
    assert "현재 7C / 맑음" in morning["111"]
    assert "- 지역 서울특별시" in morning["222"]
    assert "현재 4C / 흐림" in morning["222"]
    assert "- 지역 동대문구" in evening["111"]
    assert "- 지역 서울특별시" in evening["222"]


def test_send_scheduled_briefings_skips_when_delivery_mode_is_precompute_only(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_delivery_mode="precompute_only",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        timezone="Asia/Seoul",
    )

    result = pipeline.send_scheduled_briefings(settings=settings, db=db)

    assert result["skipped"] is True
    assert result["reason"] == "BRIEFING_DELIVERY_MODE=precompute_only"


def test_build_precomputed_telegram_briefings_embeds_signed_relay_request(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    tz = ZoneInfo("Asia/Seoul")
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_morning_time_local="09:00",
        briefing_evening_time_local="21:00",
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        briefing_relay_endpoint="https://relay.example.com/briefing",
        briefing_relay_shared_secret="relay-secret",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        timezone="Asia/Seoul",
        llm_enabled=False,
        include_identity=False,
    )

    result = pipeline.build_precomputed_telegram_briefings(
        settings=settings,
        db=db,
        now_local=datetime(2026, 3, 8, 10, 30, tzinfo=tz),
    )

    evening = result["items"]["2026-03-08-evening"]
    relay_request = evening["relay_request"]
    assert result["relay"]["configured"] is True
    assert relay_request["url"] == "https://relay.example.com/briefing"
    assert relay_request["method"] == "POST"
    assert relay_request["body"]["item_key"] == "2026-03-08-evening"
    assert relay_request["body"]["signature"]


def test_publish_dashboard_disables_llm_guidance_for_precompute(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        pipeline,
        "build_precomputed_telegram_briefings",
        lambda settings, db, now_local=None, enable_llm_guidance=True: (
            captured.update({"enable_llm_guidance": enable_llm_guidance})
            or {"ok": True, "items": {}}
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "render_dashboard_snapshot",
        lambda **kwargs: {"dashboard_dir": str(storage_root / "publish" / "dashboard")},
    )
    monkeypatch.setattr(pipeline, "_record_sync_dashboard_state", lambda *args, **kwargs: None)
    settings = SimpleNamespace(storage_root_dir=storage_root)

    result = pipeline.publish_dashboard(settings=settings, db=db)

    assert captured["enable_llm_guidance"] is False
    assert result["precomputed_telegram_briefings"]["ok"] is True


def test_send_scheduled_briefings_reuses_llm_guidance_for_identical_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []
    llm_calls: list[dict[str, object]] = []

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 3, 8, 22, 0, 0)
            if tz is not None:
                return base.replace(tzinfo=tz)
            return base

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    class FakeLLMClient:
        def generate_text(self, system_prompt: str, prompt: str) -> str:
            llm_calls.append(
                {
                    "system_prompt": system_prompt,
                    "prompt": prompt,
                }
            )
            return "- Stay on top of tasks.\n- Keep tomorrow open.\n- Check UClass once."

    monkeypatch.setattr(pipeline, "datetime", FakeDateTime)
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(
        pipeline,
        "_chat_ids_for_user_preference",
        lambda settings, db, preference_key: ["111", "222"],
    )
    monkeypatch.setattr(pipeline, "_llm_client", lambda settings, timeout_sec=None: FakeLLMClient())
    settings = SimpleNamespace(
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_delivery_mode="direct",
        briefing_morning_time_local="09:00",
        briefing_evening_time_local="21:00",
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=[],
        timezone="Asia/Seoul",
        llm_enabled=True,
        llm_timeout_sec=120,
        include_identity=False,
        weather_enabled=False,
        air_quality_enabled=False,
    )

    result = pipeline.send_scheduled_briefings(settings=settings, db=db)

    assert set(result["sent_slots"]) == {"morning", "evening"}
    assert len(sent_messages) == 4
    assert len(llm_calls) == 2
