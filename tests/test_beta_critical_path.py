from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sidae_secretary.connectors import portal as portal_connector
from sidae_secretary.connectors import uos_openapi
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


pytestmark = pytest.mark.beta_critical


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "sidae.db")
    database.init()
    return database


@pytest.fixture
def seoul_tz() -> ZoneInfo:
    return ZoneInfo("Asia/Seoul")


@pytest.fixture
def beta_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Seoul",
        weather_enabled=True,
        weather_location_label="서울특별시",
        weather_lat=37.5665,
        weather_lon=126.9780,
        weather_kma_auth_key=None,
        air_quality_enabled=False,
        air_quality_district_codes=["111152"],
        air_quality_seoul_api_key=None,
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        telegram_smart_commands_enabled=True,
        include_identity=False,
        llm_enabled=False,
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
    )


@pytest.fixture
def fixed_now_local(seoul_tz: ZoneInfo) -> datetime:
    return datetime(2026, 3, 9, 9, 0, tzinfo=seoul_tz)


@pytest.fixture
def openapi_absolute_payload() -> dict[str, object]:
    return {
        "data": {
            "title": "2026-1 학생별강의시간표",
            "academic_year": "2026",
            "semester": "1",
            "events": [
                {
                    "name": "운영체제",
                    "startAt": "2026-03-16T09:00:00",
                    "endAt": "2026-03-16T10:15:00",
                    "buildingNo": "21",
                    "buildingNm": "자연과학관",
                    "lectureRoom": "21-201",
                    "courseCode": "CSC201",
                    "planId": "2026-1-CSC201",
                    "teacher": "김교수",
                    "source_row": "3",
                    "metadata": {"section": "A"},
                }
            ],
        }
    }


@pytest.fixture
def weather_scope_data(
    db: Database,
    beta_settings: SimpleNamespace,
    fixed_now_local: datetime,
) -> dict[str, object]:
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])
    db.upsert_user_preferences(
        user_id=user_id,
        weather_location_label="동대문구",
        weather_lat=37.5744,
        weather_lon=127.0396,
        weather_air_quality_district_code="111152",
    )

    global_target = pipeline._default_weather_target(beta_settings)
    user_target = pipeline._user_weather_target(beta_settings, db, user_id=user_id)
    observed_at = fixed_now_local.isoformat()

    db.update_sync_state(
        "sync_weather",
        last_run_at=observed_at,
        last_cursor_json=_weather_snapshot(
            target=global_target,
            fixed_now_local=fixed_now_local,
            label="서울특별시",
            observed_at=observed_at,
            temperature_c=7.4,
        ),
    )
    db.update_sync_state(
        "sync_weather",
        last_run_at=observed_at,
        last_cursor_json=_weather_snapshot(
            target=user_target,
            fixed_now_local=fixed_now_local,
            label="동대문구",
            observed_at=observed_at,
            temperature_c=6.1,
        ),
        user_id=user_id,
    )
    return {"user_id": user_id}


@pytest.fixture
def scoped_day_brief_data(
    db: Database,
    seoul_tz: ZoneInfo,
) -> dict[str, object]:
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])
    target_day = datetime(2026, 3, 9, 0, 0, tzinfo=seoul_tz)
    class_start = target_day.replace(hour=10)

    db.upsert_course(
        canonical_course_id="uclass:uclass-example:201",
        source="uclass",
        external_course_id="201",
        display_name="Intro to Economics",
        metadata_json={},
        user_id=user_id,
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:201",
        alias="경제학원론",
        alias_type="manual",
        source="test",
        metadata_json={},
        user_id=user_id,
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:201",
        alias="Intro to Economics",
        alias_type="uclass",
        source="uclass",
        metadata_json={},
        user_id=user_id,
    )
    db.upsert_event(
        external_id="portal:econ-1",
        source="portal",
        start=class_start.isoformat(),
        end=(class_start + timedelta(hours=1, minutes=15)).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json={"timetable_source": "uos_portal"},
        user_id=user_id,
    )
    db.record_artifact(
        external_id="uclass:artifact:econ-1",
        source="uclass",
        filename="week4.pdf",
        icloud_path=None,
        content_hash="econ-1",
        metadata_json={
            "course_name": "Intro to Economics",
            "brief": {
                "bullets": [
                    "수요와 공급 곡선 이동 요인을 다시 정리해.",
                ],
            },
            "source_kind": "attachment",
        },
        user_id=user_id,
    )
    db.upsert_notification(
        external_id="uclass:notif:econ-1",
        source="uclass",
        created_at=target_day.replace(hour=8).isoformat(),
        title="Reading memo 안내",
        body="오늘 공지",
        url=None,
        metadata_json={"course_name": "Intro to Economics"},
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-1",
        source="uclass",
        due_at=target_day.replace(hour=23, minute=59).isoformat(),
        title="Reading memo 제출",
        status="open",
        metadata_json={
            "course_name": "Intro to Economics",
            "summary": "수요와 공급 예시를 1개 추가해 제출",
        },
        user_id=user_id,
    )
    return {
        "user_id": user_id,
        "target_day_local": target_day,
        "lookahead_now_iso": target_day.replace(hour=8).isoformat(),
    }


def _weather_snapshot(
    *,
    target: dict[str, object],
    fixed_now_local: datetime,
    label: str,
    observed_at: str,
    temperature_c: float,
) -> dict[str, object]:
    return {
        "generated_at": observed_at,
        "observed_at": observed_at,
        "location_label": label,
        "target": pipeline._weather_target_payload(target),
        "current": {
            "temperature_c": temperature_c,
            "condition_text": "맑음",
        },
        "today": {
            "date": fixed_now_local.date().isoformat(),
            "morning": {
                "temperature_min_c": temperature_c - 2,
                "temperature_max_c": temperature_c,
                "condition_text": "맑음",
                "precip_probability_max": 10,
            },
            "afternoon": {
                "temperature_min_c": temperature_c,
                "temperature_max_c": temperature_c + 4,
                "condition_text": "구름많음",
                "precip_probability_max": 20,
            },
            "temperature_min_c": temperature_c - 2,
            "temperature_max_c": temperature_c + 4,
        },
        "tomorrow": {
            "date": (fixed_now_local + timedelta(days=1)).date().isoformat(),
            "temperature_min_c": temperature_c - 1,
            "temperature_max_c": temperature_c + 5,
            "precip_probability_max": 30,
        },
        "air_quality": {
            "ok": False,
            "skipped": True,
            "reason": "AIR_QUALITY_ENABLED is false",
            "districts": [],
        },
    }


def test_normalize_uos_openapi_timetable_payload_handles_nested_absolute_events(
    openapi_absolute_payload: dict[str, object],
    seoul_tz: ZoneInfo,
) -> None:
    normalized = uos_openapi.normalize_uos_openapi_timetable_payload(
        openapi_absolute_payload,
        timezone_name="Asia/Seoul",
        source_url="https://api.uos.example/timetable/student-demo-001",
        current_dt=datetime(2026, 3, 9, 9, 0, tzinfo=seoul_tz),
    )

    assert normalized["payload_source"] == uos_openapi.UOS_OPENAPI_TIMETABLE_SOURCE
    assert normalized["title"] == "2026-1 학생별강의시간표"

    event = normalized["events"][0]
    metadata = event["metadata"]

    assert event["source"] == "portal"
    assert event["start_at"] == "2026-03-16T09:00:00+09:00"
    assert event["end_at"] == "2026-03-16T10:15:00+09:00"
    assert event["rrule"] == "FREQ=WEEKLY;BYDAY=MO"
    assert metadata["academic_year"] == 2026
    assert metadata["semester"] == 1
    assert metadata["weekday_code"] == "MO"
    assert metadata["instructor"] == "김교수"
    assert metadata["official_building_no"] == "21"
    assert metadata["official_building_name"] == "자연과학관"
    assert metadata["official_room"] == "21-201"
    assert metadata["official_course_name"] == "운영체제"
    assert metadata["official_course_code"] == "CSC201"
    assert metadata["official_syllabus_id"] == "2026-1-CSC201"
    assert metadata["section"] == "A"
    assert metadata["source_row"] == "3"


def test_normalize_uos_openapi_timetable_payload_handles_official_info_rows(
    seoul_tz: ZoneInfo,
) -> None:
    payload = {
        "INFO": [
            {
                "YEAR": "2026",
                "TERM": "1학기",
                "SUBJECT_NO": "01092",
                "SUBJECT_NM": "논리와사고",
                "DVCL_NO": "02",
                "CLASS_NM": "금[2,3,4]/5-303",
                "PROF_KOR_NM": "서진리",
            }
        ]
    }

    normalized = uos_openapi.normalize_uos_openapi_timetable_payload(
        payload,
        timezone_name="Asia/Seoul",
        source_url=uos_openapi.UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
        requested_year=2026,
        requested_term=10,
        api_key="token",
        current_dt=datetime(2026, 3, 9, 9, 0, tzinfo=seoul_tz),
    )

    assert normalized["payload_source"] == uos_openapi.UOS_OPENAPI_TIMETABLE_SOURCE
    assert len(normalized["events"]) == 1

    event = normalized["events"][0]
    metadata = event["metadata"]

    assert event["title"] == "논리와사고"
    assert event["location"] == "5-303"
    assert event["rrule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert event["start_at"] == "2026-03-13T10:00:00+09:00"
    assert event["end_at"] == "2026-03-13T12:50:00+09:00"
    assert metadata["official_subject_no"] == "01092"
    assert metadata["official_dvcl_no"] == "02"
    assert metadata["official_term_code"] == 10
    assert metadata["official_syllabus_url"].endswith(
        "apiKey=token&year=2026&term=10&subjectNo=01092&dvclNo=02"
    )
    assert metadata["official_syllabus_id"] == "2026:10:01092:02"


@pytest.mark.parametrize(
    ("selected_user_id", "expected_label", "expected_temperature"),
    [
        (None, "서울특별시", "현재 7.4C / 맑음"),
        ("custom", "동대문구", "현재 6.1C / 맑음"),
    ],
)
def test_build_briefing_weather_lines_selects_user_specific_snapshot(
    db: Database,
    beta_settings: SimpleNamespace,
    fixed_now_local: datetime,
    weather_scope_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    selected_user_id: str | None,
    expected_label: str,
    expected_temperature: str,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_fetch_weather_snapshot_for_target",
        lambda *args, **kwargs: pytest.fail("weather refresh should not run for cached beta fixtures"),
    )

    user_id = int(weather_scope_data["user_id"]) if selected_user_id == "custom" else None
    lines = pipeline._build_briefing_weather_lines(
        beta_settings,
        db,
        now_local=fixed_now_local,
        user_id=user_id,
    )
    message = "\n".join(lines)

    assert f"- 지역 {expected_label}" in message
    assert expected_temperature in message


def test_day_brief_service_builds_shared_course_view_from_scoped_fixtures(
    db: Database,
    beta_settings: SimpleNamespace,
    scoped_day_brief_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_collect_primary_meetings_scoped",
        lambda settings, db, *, target_day_local, user_id=None: {
            "ok": True,
            "events": [
                {
                    "title": "지도교수 상담",
                    "start_at": target_day_local.replace(hour=15).isoformat(),
                }
            ],
        },
    )

    day_brief = pipeline.DayBriefService(
        beta_settings,
        db,
        user_id=int(scoped_day_brief_data["user_id"]),
    ).build_day_brief(
        target_day_local=scoped_day_brief_data["target_day_local"],
        reference_day_local=scoped_day_brief_data["target_day_local"],
        max_classes=6,
        lookahead_days=2,
        lookahead_limit=10,
        lookahead_now_iso=str(scoped_day_brief_data["lookahead_now_iso"]),
    )

    assert day_brief.meetings_result["ok"] is True
    assert len(day_brief.meeting_items) == 1
    assert len(day_brief.course_briefs) == 1
    assert len(day_brief.tasks_due_on_day) == 1
    assert len(day_brief.tasks_due_within_window) == 1

    course_brief = day_brief.course_briefs[0]
    assert course_brief.class_item["title"] == "경제학원론"
    assert course_brief.best_brief is not None
    assert course_brief.best_brief["bullets"] == ["수요와 공급 곡선 이동 요인을 다시 정리해."]
    assert course_brief.preparation == "수요와 공급 곡선 이동 요인을 다시 정리해."
    assert course_brief.notice_titles == ("Reading memo 안내",)
    assert any("Reading memo 제출" in line for line in course_brief.task_lines)
    assert day_brief.tasks_due_on_day[0].title == "Reading memo 제출"
    assert day_brief.tasks_due_within_window[0].title == "Reading memo 제출"


@pytest.mark.parametrize(
    ("command_text", "kind", "expected_label", "expected_title"),
    [
        ("/notice_general", "general", "[Sidae] 학교 일반공지", "일반 공지 제목"),
        ("/notice_academic", "academic", "[Sidae] 학교 학사공지", "학사 공지 제목"),
    ],
)
def test_sync_telegram_notice_commands_render_hermetic_portal_snapshots(
    db: Database,
    beta_settings: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    command_text: str,
    kind: str,
    expected_label: str,
    expected_title: str,
) -> None:
    sent_messages: list[tuple[str, str]] = []
    fetched_at = "2026-03-09T00:00:00Z"
    config = pipeline.UOS_NOTICE_FEEDS[kind]

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 1,
                    "message": {
                        "date": 1770000000,
                        "text": command_text,
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    def _fake_fetch(list_id, menuid, limit=10):
        assert str(list_id) == str(config["list_id"])
        assert str(menuid) == str(config["menuid"])
        return portal_connector.PortalNoticeFetchResult(
            notices=[
                portal_connector.PortalNotice(
                    seq="101",
                    title=expected_title,
                    posted_on="2026-03-09",
                    department="학생과",
                    list_id=str(list_id),
                    menuid=str(menuid),
                )
            ],
            metadata=portal_connector.PortalNoticeFetchMetadata(
                list_id=str(list_id),
                menuid=str(menuid),
                requested_limit=limit,
                requested_at=fetched_at,
                fetched_at=fetched_at,
                source_url=f"https://www.uos.ac.kr/korNotice/list.do?list_id={list_id}",
                resolved_url=f"https://www.uos.ac.kr/korNotice/list.do?list_id={list_id}",
                http_status=200,
                page_title=f"{config['label']} < UOS공지",
                parsed_count=1,
            ),
        )

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(pipeline, "fetch_uos_notice_feed", _fake_fetch)

    result = pipeline.sync_telegram(settings=beta_settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    assert expected_label in sent_messages[0][1]
    assert expected_title in sent_messages[0][1]
    snapshot_state = db.get_sync_state(f"uos_notice_snapshot_{kind}")
    assert snapshot_state is not None
