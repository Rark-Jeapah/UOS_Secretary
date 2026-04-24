from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sidae_secretary import onboarding
from sidae_secretary.connectors import uos_openapi
from sidae_secretary.connectors import uos_portal
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline
from sidae_secretary.connectors.uos_portal import (
    UOS_PORTAL_SCHOOL_SLUG,
    parse_uos_timetable_table,
)

pytestmark = pytest.mark.beta_critical


def test_parse_uos_timetable_table_extracts_weekly_classes() -> None:
    table = [
        [
            {"key": "0:0", "text": "교시"},
            {"key": "0:1", "text": "요일"},
            {"key": "0:2", "text": "월"},
            {"key": "0:3", "text": "화"},
            {"key": "0:4", "text": "수"},
            {"key": "0:5", "text": "목"},
            {"key": "0:6", "text": "금"},
            {"key": "0:7", "text": "토"},
        ],
        [
            {"key": "1:0", "text": "1교시"},
            {"key": "1:1", "text": "09:00-09:50"},
            {"key": "1:2", "text": ""},
            {"key": "1:3", "text": "자료구조\n21-101\n김교수", "rowspan": 2},
            {"key": "1:4", "text": ""},
            {"key": "1:5", "text": ""},
            {"key": "1:6", "text": ""},
            {"key": "1:7", "text": ""},
        ],
        [
            {"key": "2:0", "text": "2교시"},
            {"key": "2:1", "text": "10:00-10:50"},
            {"key": "2:2", "text": ""},
            {"key": "2:4", "text": ""},
            {"key": "2:5", "text": ""},
            {"key": "2:6", "text": ""},
            {"key": "2:7", "text": ""},
        ],
        [
            {"key": "3:0", "text": "3교시"},
            {"key": "3:1", "text": "11:00-11:50"},
            {"key": "3:2", "text": ""},
            {"key": "3:3", "text": ""},
            {"key": "3:4", "text": ""},
            {"key": "3:5", "text": "대학영어\n20-1034\nMENDERING NATHAN ROBERT", "rowspan": 2},
            {"key": "3:6", "text": ""},
            {"key": "3:7", "text": ""},
        ],
        [
            {"key": "4:0", "text": "4교시"},
            {"key": "4:1", "text": "12:00-12:50"},
            {"key": "4:2", "text": ""},
            {"key": "4:3", "text": ""},
            {"key": "4:4", "text": ""},
            {"key": "4:6", "text": ""},
            {"key": "4:7", "text": ""},
        ],
    ]

    events = parse_uos_timetable_table(
        table,
        timezone_name="Asia/Seoul",
        current_dt=datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        year=2026,
        semester=1,
    )

    assert len(events) == 2
    by_title = {item["title"]: item for item in events}
    assert by_title["자료구조"]["location"] == "21-101"
    assert by_title["자료구조"]["rrule"] == "FREQ=WEEKLY;BYDAY=TU"
    assert by_title["자료구조"]["metadata"]["instructor"] == "김교수"
    assert by_title["자료구조"]["metadata"]["academic_year"] == 2026
    assert by_title["대학영어"]["location"] == "20-1034"
    assert by_title["대학영어"]["rrule"] == "FREQ=WEEKLY;BYDAY=TH"


def test_parse_uos_timetable_table_accepts_tilde_time_ranges() -> None:
    table = [
        [
            {"key": "0:0", "text": "요일"},
            {"key": "0:1", "text": "월"},
            {"key": "0:2", "text": "화"},
            {"key": "0:3", "text": "수"},
            {"key": "0:4", "text": "목"},
            {"key": "0:5", "text": "금"},
        ],
        [
            {"key": "1:0", "text": "09:00~10:15"},
            {"key": "1:1", "text": ""},
            {"key": "1:2", "text": "중국어1\n5-224"},
            {"key": "1:3", "text": ""},
            {"key": "1:4", "text": ""},
            {"key": "1:5", "text": ""},
        ],
    ]

    events = parse_uos_timetable_table(
        table,
        timezone_name="Asia/Seoul",
        current_dt=datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        year=2026,
        semester=1,
    )

    assert len(events) == 1
    assert events[0]["title"] == "중국어1"
    assert events[0]["rrule"] == "FREQ=WEEKLY;BYDAY=TU"


def test_parse_uos_timetable_table_accepts_period_only_rows() -> None:
    table = [
        [
            {"key": "0:0", "text": "교시"},
            {"key": "0:1", "text": "월"},
            {"key": "0:2", "text": "화"},
            {"key": "0:3", "text": "수"},
            {"key": "0:4", "text": "목"},
            {"key": "0:5", "text": "금"},
        ],
        [
            {"key": "1:0", "text": "2교시"},
            {"key": "1:1", "text": ""},
            {"key": "1:2", "text": ""},
            {"key": "1:3", "text": "자료구조\n21-101\n김교수", "rowspan": 2},
            {"key": "1:4", "text": ""},
            {"key": "1:5", "text": ""},
        ],
        [
            {"key": "2:0", "text": "3교시"},
            {"key": "2:1", "text": ""},
            {"key": "2:2", "text": ""},
            {"key": "2:4", "text": ""},
            {"key": "2:5", "text": ""},
        ],
    ]

    events = parse_uos_timetable_table(
        table,
        timezone_name="Asia/Seoul",
        current_dt=datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        year=2026,
        semester=1,
    )

    assert len(events) == 1
    assert events[0]["title"] == "자료구조"
    assert events[0]["start_at"] == "2026-03-11T10:00:00+09:00"
    assert events[0]["end_at"] == "2026-03-11T11:50:00+09:00"
    assert events[0]["rrule"] == "FREQ=WEEKLY;BYDAY=WE"


def test_navigate_to_timetable_surface_clicks_menu_label() -> None:
    class FakeLocator:
        def __init__(self, page, selector: str):
            self._page = page
            self._selector = selector
            self.first = self

        def count(self) -> int:
            return 1 if "학생별강의시간표" in self._selector else 0

        def click(self, timeout: int = 3000) -> None:
            self._page._title = "학생별강의시간표"
            self._page._text = "학생별강의시간표\n자료구조"

    class FakeFrame:
        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(page, selector)

        def evaluate(self, script: str, args: object) -> str:
            return ""

    class FakePage(FakeFrame):
        def __init__(self) -> None:
            self._title = "대학행정"
            self._text = "WISE 메인"
            self.frames: list[FakeFrame] = [FakeFrame()]

        def title(self) -> str:
            return self._title

        def wait_for_load_state(self, state: str, timeout: int = 1000) -> None:
            return None

        def wait_for_timeout(self, timeout: int) -> None:
            return None

    page = FakePage()

    ok = uos_portal._navigate_to_timetable_surface(page, timeout_sec=5)

    assert ok is True
    assert uos_portal.UOS_TIMETABLE_TITLE in page.title()


def test_page_has_timetable_surface_ignores_menu_label_on_wise_home() -> None:
    class FakeLocator:
        def __init__(self, text: str) -> None:
            self._text = text

        def inner_text(self, timeout: int = 5000) -> str:
            return self._text

    class FakePage:
        def __init__(self) -> None:
            self.frames: list[object] = []

        def title(self) -> str:
            return "대학행정_이루넷_UOS"

        def locator(self, selector: str) -> FakeLocator:
            assert selector == "body"
            return FakeLocator("WISE 메인\nLecture schedule\nCheck major/elective/consilience schedule")

    assert uos_portal._page_has_timetable_surface(FakePage()) is False


def test_navigate_to_timetable_surface_uses_exact_korean_label_without_wrong_menu_id() -> None:
    class FakeBodyLocator:
        def __init__(self, page) -> None:
            self._page = page

        def inner_text(self, timeout: int = 5000) -> str:
            return self._page._text

    class FakeMenuLocator:
        def __init__(self, page, selector: str) -> None:
            self._page = page
            self._selector = selector
            self.first = self

        def count(self) -> int:
            return 1 if 'text="강의시간표"' in self._selector else 0

        def click(self, timeout: int = 3000) -> None:
            self._page._title = "학생별강의시간표"
            self._page._text = "학생별강의시간표\n자료구조"

    class FakeFrame:
        def __init__(self, page) -> None:
            self._page = page

        def locator(self, selector: str) -> FakeMenuLocator:
            return FakeMenuLocator(self._page, selector)

        def evaluate(self, script: str, args: object) -> str:
            payload = dict(args or {})
            assert list(payload.get("target_item_ids") or []) == []
            assert "강의시간표" in list(payload.get("labels") or [])
            assert "전공/교양/통섭 시간표조회" in list(payload.get("blocked_labels") or [])
            return ""

    class FakePage:
        def __init__(self) -> None:
            self._title = "대학행정_이루넷_UOS"
            self._text = "WISE 메인\nLecture schedule"
            self.frames: list[FakeFrame] = [FakeFrame(self)]

        def title(self) -> str:
            return self._title

        def locator(self, selector: str):
            if selector == "body":
                return FakeBodyLocator(self)
            return FakeMenuLocator(self, selector)

        def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 1000) -> None:
            raise AssertionError("direct timetable URL navigation should not run")

        def wait_for_load_state(self, state: str, timeout: int = 1000) -> None:
            return None

        def wait_for_timeout(self, timeout: int) -> None:
            return None

    page = FakePage()

    ok = uos_portal._navigate_to_timetable_surface(page, timeout_sec=5)

    assert ok is True
    assert uos_portal.UOS_TIMETABLE_TITLE in page.title()


def test_auth_eps_page_is_detected_as_login_failure() -> None:
    class FakeLocator:
        def __init__(self) -> None:
            self.first = self

        def count(self) -> int:
            return 0

    class FakePage:
        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator()

    assert (
        uos_portal._looks_like_login_failure(
            current_url="https://sso.uos.ac.kr/svc/tk/Auth.eps",
            title="University of Seoul portal system",
            page_text="",
            page=FakePage(),
        )
        is True
    )


def test_issue_connect_link_for_uos_portal_issues_single_school_account_link(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        onboarding_public_base_url="https://connect.example.invalid",
        onboarding_session_ttl_minutes=15,
        timezone="Asia/Seoul",
    )

    result = pipeline._issue_moodle_connect_link(
        settings=settings,
        db=db,
        chat_id="77777",
        school_query="시립대 포털",
    )

    assert result["ok"] is True
    assert "[Sidae] 학교 계정 연결" in result["message"]
    assert "- 대상: 서울시립대학교 포털/대학행정" in result["message"]
    assert "https://connect.example.invalid/moodle-connect?token=" in result["link"]
    token = result["link"].split("token=", 1)[1]
    session = db.get_active_onboarding_session(
        token=token,
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
    )
    assert session is not None
    assert session["metadata_json"]["school_query"] == "서울시립대학교 포털/대학행정"
    assert session["metadata_json"]["directory_school_slug"] == UOS_PORTAL_SCHOOL_SLUG


def test_sync_uos_portal_timetable_uses_browser_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:test-1",
                    "start_at": now_local.replace(hour=15, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=16, minute=15).isoformat(),
                    "title": "계량경제학",
                    "location": "33-B103",
                    "rrule": "FREQ=WEEKLY;BYDAY=WE",
                    "metadata": {
                        "school_slug": UOS_PORTAL_SCHOOL_SLUG,
                        "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
                    },
                }
            ],
            "current_url": "https://wise.uos.ac.kr/index.do",
            "title": "학생별강의시간표",
            "page_text": "학생별강의시간표",
            "table_count": 1,
            "network_samples": [],
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 1
    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    assert events[0].source == "portal"
    assert events[0].title == "계량경제학"


def test_sync_uos_portal_timetable_prefers_official_api_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-openapi"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:official-api-1",
                    "start_at": now_local.replace(hour=9, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=10, minute=15).isoformat(),
                    "title": "자료구조",
                    "location": "21-101",
                    "rrule": "FREQ=WEEKLY;BYDAY=WE",
                    "metadata": {
                        "school_slug": UOS_PORTAL_SCHOOL_SLUG,
                        "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
                    },
                }
            ],
            "title": "학생별강의시간표",
            "table_count": 1,
            "auth_required": False,
            "source_url": "https://api.uos.example/timetable/1",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("portal fallback should not run")),
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url="https://api.uos.example/timetable/{user_id}",
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 1
    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    metadata = json.loads(events[0].metadata_json)
    assert metadata["timetable_source"] == UOS_PORTAL_SCHOOL_SLUG
    assert metadata["timetable_payload_source"] == "uos_openapi"

    portal_session = db.get_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        status="active",
        user_id=int(user["id"]),
    )
    assert portal_session is not None
    assert portal_session["metadata_json"]["portal_timetable_sync"]["payload_source"] == "uos_openapi"

    state = db.get_sync_state("sync_uos_portal_timetable", user_id=int(user["id"]))
    cursor = json.loads(state.last_cursor_json or "{}")
    assert cursor["payload_source"] == "uos_openapi"
    assert cursor["source_attempts"] == [
        {
            "source": "uos_openapi",
            "status": "selected",
            "reason": None,
            "source_url": "https://api.uos.example/timetable/1",
        }
    ]


def test_sync_uos_portal_timetable_falls_back_when_official_api_request_is_unsupported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-openapi-unsupported"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:portal-fallback-1",
                    "start_at": now_local.replace(hour=15, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=16, minute=15).isoformat(),
                    "title": "계량경제학",
                    "location": "33-B103",
                    "rrule": "FREQ=WEEKLY;BYDAY=WE",
                    "metadata": {
                        "school_slug": UOS_PORTAL_SCHOOL_SLUG,
                        "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
                    },
                }
            ],
            "current_url": "https://wise.uos.ac.kr/index.do",
            "title": "학생별강의시간표",
            "page_text": "학생별강의시간표",
            "table_count": 1,
            "network_samples": [],
            "auth_required": False,
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url="https://api.uos.example/timetable/{student_no}",
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 1
    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    metadata = json.loads(events[0].metadata_json)
    assert metadata["timetable_payload_source"] == "uos_portal_browser"

    state = db.get_sync_state("sync_uos_portal_timetable", user_id=int(user["id"]))
    cursor = json.loads(state.last_cursor_json or "{}")
    assert cursor["payload_source"] == "uos_portal_browser"
    assert cursor["fallback_used"] is True
    assert cursor["source_attempts"][0]["source"] == "uos_openapi"
    assert cursor["source_attempts"][0]["status"] == "unsupported"
    assert "missing student_no" in cursor["source_attempts"][0]["reason"]


def test_sync_uos_portal_timetable_records_source_provenance_metadata_after_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-openapi-fallback"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: (_ for _ in ()).throw(
            pipeline.requests.RequestException("temporary outage")
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:portal-fallback-2",
                    "start_at": now_local.replace(hour=11, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=12, minute=15).isoformat(),
                    "title": "대학영어",
                    "location": "20-1034",
                    "rrule": "FREQ=WEEKLY;BYDAY=WE",
                    "metadata": {
                        "school_slug": UOS_PORTAL_SCHOOL_SLUG,
                        "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
                    },
                }
            ],
            "current_url": "https://portal.uos.ac.kr/uos/LinkUrl.eps?menuid=SucrMjTimeInq",
            "title": "학생별강의시간표",
            "table_count": 1,
            "auth_required": False,
            "network_samples": [],
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url="https://api.uos.example/timetable/{user_id}",
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
    )

    pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    portal_session = db.get_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        status="active",
        user_id=int(user["id"]),
    )
    assert portal_session is not None
    sync_meta = portal_session["metadata_json"]["portal_timetable_sync"]
    assert sync_meta["payload_source"] == "uos_portal_browser"
    assert sync_meta["fallback_used"] is True
    assert sync_meta["source_attempts"] == [
        {
            "source": "uos_openapi",
            "status": "fallback",
            "reason": "temporary outage",
            "source_url": "https://api.uos.example/timetable/{user_id}",
        },
        {
            "source": "uos_portal_browser",
            "status": "selected",
            "reason": None,
            "source_url": "https://portal.uos.ac.kr/uos/LinkUrl.eps?menuid=SucrMjTimeInq",
        },
    ]


def test_sync_uos_portal_timetable_matches_official_catalog_to_uclass_courses_without_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_course(
        canonical_course_id="uclass:uclass-uos:101",
        source="uclass",
        external_course_id="101",
        display_name="논리와사고",
        metadata_json={"fullname": "논리와사고", "shortname": "논리와사고"},
        user_id=int(user["id"]),
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-uos:101",
        alias="논리와사고",
        alias_type="manual",
        source="test",
        metadata_json={},
        user_id=int(user["id"]),
    )

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": int(user["id"]),
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.example/webservice/rest/server.php",
                "token": "uclass-token",
                "token_error": "",
                "allow_html_fallback": False,
                "connection_id": 77,
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:official-catalog-1",
                    "start_at": now_local.replace(day=13, hour=10, minute=0).isoformat(),
                    "end_at": now_local.replace(day=13, hour=12, minute=50).isoformat(),
                    "title": "논리와사고",
                    "location": "5-303",
                    "rrule": "FREQ=WEEKLY;BYDAY=FR",
                    "metadata": {
                        "official_subject_no": "01092",
                        "official_dvcl_no": "02",
                        "official_syllabus_url": "https://wise.uos.ac.kr/COM/ApiCoursePlan/list.do?apiKey=token&year=2026&term=10&subjectNo=01092&dvclNo=02",
                        "official_syllabus_id": "2026:10:01092:02",
                        "official_term_code": 10,
                        "official_course_name": "논리와사고",
                    },
                }
            ],
            "source_url": "https://wise.uos.ac.kr/COM/ApiTimeTable/list.do?apiKey=token&year=2026&term=10",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("browser fallback should not run in official catalog mode")),
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url=uos_openapi.UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
        uos_openapi_year=2026,
        uos_openapi_term="10",
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    assert result["upserted_events"] == 1
    assert result["synced_targets"] == 1
    metadata = json.loads(events[0].metadata_json)
    assert metadata["canonical_course_id"] == "uclass:uclass-uos:101"
    assert metadata["official_subject_no"] == "01092"
    assert metadata["official_dvcl_no"] == "02"
    assert metadata["official_syllabus_id"] == "2026:10:01092:02"
    assert metadata["official_syllabus_url"].endswith(
        "apiKey=token&year=2026&term=10&subjectNo=01092&dvclNo=02"
    )
    assert metadata["official_api_target"]["connection_id"] == 77
    assert db.list_lms_browser_sessions(user_id=int(user["id"]), limit=10) == []

    state = db.get_sync_state("sync_uos_portal_timetable", user_id=int(user["id"]))
    cursor = json.loads(state.last_cursor_json or "{}")
    assert cursor["payload_source"] == "uos_openapi"
    assert cursor["course_match_summary"]["selected_sections"] == 1
    assert cursor["catalog_section_count"] == 1


def test_sync_uos_portal_timetable_uses_uclass_shortname_section_code_to_break_ties(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    pipeline._register_uclass_courses(
        SimpleNamespace(uclass_ws_base="https://uclass.uos.ac.kr/webservice/rest/server.php"),
        db,
        {
            3821: {
                "id": 3821,
                "fullname": "대학글쓰기",
                "shortname": "대학글쓰기 (2026-10, 02115_35_U)",
            }
        },
        user_id=int(user["id"]),
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
    )

    now_local = datetime(2026, 3, 17, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": int(user["id"]),
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.uos.ac.kr/webservice/rest/server.php",
                "token": "uclass-token",
                "token_error": "",
                "allow_html_fallback": False,
                "connection_id": 77,
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:official-catalog-02115-08",
                    "start_at": now_local.replace(hour=18, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=20, minute=20).isoformat(),
                    "title": "대학글쓰기",
                    "location": "20-201,2",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                    "metadata": {
                        "official_subject_no": "02115",
                        "official_dvcl_no": "08",
                        "official_course_code": "02115",
                        "official_course_name": "대학글쓰기",
                    },
                },
                {
                    "external_id": "portal:uos:timetable:official-catalog-02115-35",
                    "start_at": now_local.replace(hour=18, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=20, minute=20).isoformat(),
                    "title": "대학글쓰기",
                    "location": "20-202",
                    "rrule": "FREQ=WEEKLY;BYDAY=TU",
                    "metadata": {
                        "official_subject_no": "02115",
                        "official_dvcl_no": "35",
                        "official_course_code": "02115",
                        "official_course_name": "대학글쓰기",
                    },
                },
            ],
            "source_url": "https://wise.uos.ac.kr/COM/ApiTimeTable/list.do?apiKey=token&year=2026&term=10",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("browser fallback should not run in official catalog mode")),
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url=uos_openapi.UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
        uos_openapi_year=2026,
        uos_openapi_term="10",
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 1
    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata["official_dvcl_no"] == "35"
    assert events[0].location == "20-202"


def test_sync_uos_portal_timetable_skips_tokenless_target_without_uclass_aliases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": int(user["id"]),
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.example/webservice/rest/server.php",
                "token": "",
                "token_error": "",
                "connection_id": 77,
            }
        ],
    )

    class FakeMoodleClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            assert base_url == "https://uclass.example/webservice/rest/server.php"
            assert token == "resolved-token"
            self.site_userid = None

        def get_site_info(self, wsfunction: str):
            return {"userid": 321}

        def get_users_courses(self, wsfunction: str):
            return [
                {
                    "id": 101,
                    "fullname": "논리와사고",
                    "shortname": "논리와사고",
                }
            ]

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeMoodleClient)
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:official-catalog-tokenless-1",
                    "start_at": now_local.replace(day=13, hour=10, minute=0).isoformat(),
                    "end_at": now_local.replace(day=13, hour=12, minute=50).isoformat(),
                    "title": "논리와사고",
                    "location": "5-303",
                    "rrule": "FREQ=WEEKLY;BYDAY=FR",
                    "metadata": {
                        "official_subject_no": "01092",
                        "official_dvcl_no": "02",
                        "official_course_name": "논리와사고",
                    },
                }
            ],
            "source_url": "https://wise.uos.ac.kr/COM/ApiTimeTable/list.do?apiKey=token&year=2026&term=10",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("browser fallback should not run in official catalog mode")),
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uclass_ws_base="https://uclass.example/webservice/rest/server.php",
        uclass_request_method="GET",
        uclass_func_site_info="core_webservice_get_site_info",
        uclass_func_courses="core_enrol_get_users_courses",
        uos_openapi_timetable_url=uos_openapi.UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
        uos_openapi_year=2026,
        uos_openapi_term="10",
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    events = db.list_events(user_id=int(user["id"]))
    assert events == []
    assert result["upserted_events"] == 0
    assert result["synced_targets"] == 0
    assert len(result["skipped_targets"]) == 1
    assert result["skipped_targets"][0]["reason"] == "No active UClass course aliases available for official timetable matching"


def test_sync_uos_portal_timetable_records_match_failures_as_skipped_not_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": int(user["id"]),
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.example/webservice/rest/server.php",
                "token": "",
                "token_error": "",
                "allow_html_fallback": False,
                "connection_id": 77,
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:official-catalog-unmatched-1",
                    "start_at": now_local.replace(day=13, hour=10, minute=0).isoformat(),
                    "end_at": now_local.replace(day=13, hour=12, minute=50).isoformat(),
                    "title": "논리와사고",
                    "location": "5-303",
                    "rrule": "FREQ=WEEKLY;BYDAY=FR",
                    "metadata": {
                        "official_subject_no": "01092",
                        "official_dvcl_no": "02",
                        "official_course_name": "논리와사고",
                    },
                }
            ],
            "source_url": "https://wise.uos.ac.kr/COM/ApiTimeTable/list.do?apiKey=token&year=2026&term=10",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("browser fallback should not run in official catalog mode")),
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url=uos_openapi.UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
        uos_openapi_year=2026,
        uos_openapi_term="10",
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 0
    assert result["synced_targets"] == 0
    assert len(result["skipped_targets"]) == 1
    assert (
        result["skipped_targets"][0]["reason"]
        == "No active UClass course aliases available for official timetable matching"
    )

    state = db.get_sync_state("sync_uos_portal_timetable", user_id=int(user["id"]))
    cursor = json.loads(state.last_cursor_json or "{}")
    assert cursor["reason"] == "No active UClass course aliases available for official timetable matching"


def test_register_uclass_courses_preserves_official_metadata(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")

    db.upsert_course(
        canonical_course_id="uclass:uclass-uos-ac-kr:101",
        source="uclass",
        external_course_id="101",
        display_name="논리와사고",
        metadata_json={
            "official_subject_no": "01092",
            "official_dvcl_no": "02",
            "official_course_code": "01092",
        },
        user_id=int(user["id"]),
    )

    pipeline._register_uclass_courses(
        SimpleNamespace(uclass_ws_base="https://uclass.uos.ac.kr/webservice/rest/server.php"),
        db,
        {
            101: {
                "id": 101,
                "fullname": "논리와사고",
                "shortname": "논리와사고",
            }
        },
        user_id=int(user["id"]),
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
    )

    course = db.get_course("uclass:uclass-uos-ac-kr:101", user_id=int(user["id"]))
    metadata = json.loads(course.metadata_json or "{}")
    assert metadata["fullname"] == "논리와사고"
    assert metadata["official_subject_no"] == "01092"
    assert metadata["official_dvcl_no"] == "02"
    assert metadata["official_course_code"] == "01092"


def test_sync_uos_portal_timetable_enriches_with_building_api_and_persists_course_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_course(
        canonical_course_id="uclass:uclass-uos:101",
        source="uclass",
        external_course_id="101",
        display_name="논리와사고",
        metadata_json={"fullname": "논리와사고", "shortname": "논리와사고"},
        user_id=int(user["id"]),
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-uos:101",
        alias="논리와사고",
        alias_type="manual",
        source="test",
        metadata_json={},
        user_id=int(user["id"]),
    )

    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": int(user["id"]),
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.uos.ac.kr/webservice/rest/server.php",
                "token": "uclass-token",
                "token_error": "",
                "allow_html_fallback": False,
                "connection_id": 77,
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_timetable",
        lambda **kwargs: {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:official-catalog-building-1",
                    "start_at": now_local.replace(day=13, hour=10, minute=0).isoformat(),
                    "end_at": now_local.replace(day=13, hour=12, minute=50).isoformat(),
                    "title": "논리와사고",
                    "location": "5-303",
                    "rrule": "FREQ=WEEKLY;BYDAY=FR",
                    "metadata": {
                        "official_subject_no": "01092",
                        "official_dvcl_no": "02",
                        "official_course_name": "논리와사고",
                        "official_course_code": "01092",
                        "official_syllabus_url": "https://wise.uos.ac.kr/COM/ApiCoursePlan/list.do?apiKey=token&year=2026&term=10&subjectNo=01092&dvclNo=02",
                        "official_syllabus_id": "2026:10:01092:02",
                    },
                }
            ],
            "source_url": "https://wise.uos.ac.kr/COM/ApiTimeTable/list.do?apiKey=token&year=2026&term=10",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_openapi_building_catalog",
        lambda **kwargs: {
            "ok": True,
            "items": [
                {
                    "building_code": "5",
                    "building_name": "인문학관",
                    "room_code": "303",
                    "room_name": "303호",
                    "space_name": "인문학관 303호",
                }
            ],
            "source_url": "https://wise.uos.ac.kr/COM/ApiBldg/list.do?apiKey=token",
            "payload_source": "uos_openapi_buildings",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("browser fallback should not run in official catalog mode")),
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        uos_openapi_timetable_url=uos_openapi.UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
        uos_openapi_timetable_api_key="token",
        uos_openapi_timetable_timeout_sec=5,
        uos_openapi_year=2026,
        uos_openapi_term="10",
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    metadata = json.loads(events[0].metadata_json or "{}")
    assert result["upserted_events"] == 1
    assert metadata["official_building_no"] == "5"
    assert metadata["official_building_name"] == "인문학관"
    assert metadata["official_room"] == "303"
    assert metadata["official_room_name"] == "303호"
    assert metadata["official_space_name"] == "인문학관 303호"
    assert db.get_building_name("5", school_slug=UOS_PORTAL_SCHOOL_SLUG) == "인문학관"

    course = db.get_course("uclass:uclass-uos:101", user_id=int(user["id"]))
    course_metadata = json.loads(course.metadata_json or "{}")
    assert course_metadata["official_subject_no"] == "01092"
    assert course_metadata["official_dvcl_no"] == "02"
    assert course_metadata["official_syllabus_id"] == "2026:10:01092:02"
    assert db.resolve_course_alias("01092", user_id=int(user["id"])) == "uclass:uclass-uos:101"

    state = db.get_sync_state("sync_uos_portal_timetable", user_id=int(user["id"]))
    cursor = json.loads(state.last_cursor_json or "{}")
    assert cursor["building_catalog_summary"]["payload_source"] == "uos_openapi_buildings"
    assert cursor["building_catalog_summary"]["resolved_locations"] == 1


def test_sync_uos_portal_timetable_prefers_profile_dir_over_secret_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-profile-first"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        secret_kind="keychain",
        secret_ref="telegram:12345:portal:uos_portal",
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    captured: dict[str, object] = {}
    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    class MissingSecretStore:
        def read_secret(self, *, ref):
            raise AssertionError("secret store should not be used when profile_dir is available")

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "events": [
                {
                    "external_id": "portal:uos:timetable:profile-first",
                    "start_at": now_local.replace(hour=15, minute=0).isoformat(),
                    "end_at": now_local.replace(hour=16, minute=15).isoformat(),
                    "title": "계량경제학",
                    "location": "33-B103",
                    "rrule": "FREQ=WEEKLY;BYDAY=WE",
                    "metadata": {
                        "school_slug": UOS_PORTAL_SCHOOL_SLUG,
                        "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
                    },
                }
            ],
            "current_url": "https://wise.uos.ac.kr/index.do",
            "title": "학생별강의시간표",
            "page_text": "학생별강의시간표",
            "table_count": 1,
            "network_samples": [],
            "auth_required": False,
        }

    monkeypatch.setattr(pipeline, "default_secret_store", lambda settings=None: MissingSecretStore())
    monkeypatch.setattr(pipeline, "fetch_uos_portal_timetable", fake_fetch)

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 1
    assert captured["storage_state"] is None
    assert captured["profile_dir"] == profile_dir


def test_record_uos_portal_timetable_fetch_for_user_uses_prefetched_result(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-prefetched"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    now_local = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    fetched = {
        "ok": True,
        "events": [
            {
                "external_id": "portal:uos:timetable:prefetched-1",
                "start_at": now_local.replace(hour=15, minute=0).isoformat(),
                "end_at": now_local.replace(hour=16, minute=15).isoformat(),
                "title": "계량경제학",
                "location": "33-B103",
                "rrule": "FREQ=WEEKLY;BYDAY=WE",
                "metadata": {
                    "school_slug": UOS_PORTAL_SCHOOL_SLUG,
                    "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
                },
            }
        ],
        "current_url": "https://portal.uos.ac.kr/uos/LinkUrl.eps?menuid=SucrMjTimeInq",
        "title": "학생별강의시간표",
        "table_count": 1,
        "auth_required": False,
        "network_samples": [],
    }
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.record_uos_portal_timetable_fetch_for_user(
        settings=settings,
        db=db,
        fetched=fetched,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert result["ok"] is True
    assert result["upserted_events"] == 1
    events = db.list_events(user_id=int(user["id"]))
    assert len(events) == 1
    assert events[0].source == "portal"
    assert events[0].title == "계량경제학"
    portal_session = db.get_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        status="active",
        user_id=int(user["id"]),
    )
    assert portal_session is not None
    assert portal_session["metadata_json"]["portal_timetable_sync"]["status"] == "success"
    assert portal_session["metadata_json"]["browser_result"]["title"] == "학생별강의시간표"


def test_sync_uos_portal_timetable_marks_expired_session_as_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-expired"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: {
            "ok": False,
            "events": [],
            "current_url": "https://portal.uos.ac.kr/login",
            "title": "서울시립대학교 포털",
            "table_count": 0,
            "auth_required": True,
            "network_samples": [],
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 0
    assert len(result["error_targets"]) == 1
    assert result["error_targets"][0]["reason"] == "UOS portal session expired; reconnect required"

    state = db.get_sync_state("sync_uos_portal_timetable", user_id=int(user["id"]))
    assert state.last_cursor_json is not None
    assert "UOS portal session expired; reconnect required" in state.last_cursor_json

    portal_session = db.get_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        status="active",
        user_id=int(user["id"]),
    )
    assert portal_session is not None
    assert portal_session["metadata_json"]["portal_timetable_sync"]["auth_required"] is True
    assert portal_session["metadata_json"]["portal_timetable_sync"]["status"] == "error"


def test_sync_uos_portal_timetable_does_not_treat_generic_portal_page_as_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos-generic"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: {
            "ok": False,
            "events": [],
            "current_url": "https://wise.uos.ac.kr/index.do",
            "title": "대학행정_이루넷_UOS",
            "has_timetable_surface": False,
            "table_count": 6,
            "auth_required": False,
            "network_samples": [],
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_uos_portal_timetable(settings=settings, db=db)

    assert result["upserted_events"] == 0
    assert result["synced_targets"] == 0
    assert len(result["skipped_targets"]) == 1
    assert result["skipped_targets"][0]["reason"] == "portal timetable not available"

    portal_session = db.get_lms_browser_session(
        chat_id="12345",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        status="active",
        user_id=int(user["id"]),
    )
    assert portal_session is not None
    assert portal_session["metadata_json"]["portal_timetable_sync"]["status"] == "skipped"
    assert portal_session["metadata_json"]["portal_timetable_sync"]["event_count"] == 0


def test_normalize_uos_openapi_timetable_payload_preserves_official_class_and_course_metadata() -> None:
    payload = {
        "events": [
            {
                "course_name": "자료구조",
                "weekday": "WE",
                "start_hm": "09:00",
                "end_hm": "10:15",
                "building_code": "21",
                "building_name": "자연과학관",
                "classroom": "21-101",
                "course_code": "CSC101",
                "syllabus_url": "https://uos.example/syllabus/csc101",
                "syllabus_id": "2026-1-CSC101",
            }
        ]
    }

    normalized = uos_openapi.normalize_uos_openapi_timetable_payload(
        payload,
        timezone_name="Asia/Seoul",
        current_dt=datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    event = normalized["events"][0]
    metadata = event["metadata"]

    assert metadata["official_building_no"] == "21"
    assert metadata["official_building_name"] == "자연과학관"
    assert metadata["official_room"] == "21-101"
    assert metadata["official_course_name"] == "자료구조"
    assert metadata["official_course_code"] == "CSC101"
    assert metadata["official_syllabus_url"] == "https://uos.example/syllabus/csc101"
    assert metadata["official_syllabus_id"] == "2026-1-CSC101"


def test_collect_class_occurrences_includes_portal_timetable_events(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    tz = ZoneInfo("Asia/Seoul")
    start_local = datetime(2026, 3, 11, 15, 0, tzinfo=tz)
    db.upsert_event(
        external_id="portal:uos:timetable:test-2",
        source="portal",
        start=start_local.isoformat(),
        end=start_local.replace(hour=16, minute=15).isoformat(),
        title="중국어1",
        location="5-224,5",
        rrule="FREQ=WEEKLY;BYDAY=WE",
        metadata_json={
            "school_slug": UOS_PORTAL_SCHOOL_SLUG,
            "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
        },
    )

    items = pipeline._collect_class_occurrences(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        target_day_local=start_local,
        max_items=5,
    )

    assert len(items) == 1
    assert items[0]["title"] == "중국어1"
    assert items[0]["building_no"] == "5"
    assert items[0]["room"] == "224"
    assert items[0]["location_source"] == "parsed"
    assert items[0]["location_confidence"] == "low"
    assert items[0]["location_text"] == "인문학관 224호"


def test_collect_class_occurrences_prefers_official_location_and_course_metadata(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_course(
        canonical_course_id="uclass:uclass-example:101",
        source="uclass",
        external_course_id="101",
        display_name="Data Structures",
        metadata_json={"syllabus_url": "https://uclass.example/syllabus/101"},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="자료구조",
        alias_type="manual",
        source="test",
        metadata_json={},
    )

    tz = ZoneInfo("Asia/Seoul")
    start_local = datetime(2026, 3, 11, 9, 0, tzinfo=tz)
    db.upsert_event(
        external_id="portal:uos:timetable:official-meta-1",
        source="portal",
        start=start_local.isoformat(),
        end=start_local.replace(hour=10, minute=15).isoformat(),
        title="자료구조",
        location="99-999",
        rrule="FREQ=WEEKLY;BYDAY=WE",
        metadata_json={
            "school_slug": UOS_PORTAL_SCHOOL_SLUG,
            "timetable_source": UOS_PORTAL_SCHOOL_SLUG,
            "official_building_no": "21",
            "official_building_name": "자연과학관",
            "official_room": "21-101",
            "official_course_name": "자료구조",
            "official_course_code": "CSC101",
        },
    )

    items = pipeline._collect_class_occurrences(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        target_day_local=start_local,
        max_items=5,
    )

    assert len(items) == 1
    assert items[0]["canonical_course_id"] == "uclass:uclass-example:101"
    assert items[0]["course_display_name"] == "Data Structures"
    assert items[0]["official_course_name"] == "자료구조"
    assert items[0]["official_course_code"] == "CSC101"
    assert items[0]["syllabus_url"] == "https://uclass.example/syllabus/101"
    assert items[0]["building_no"] == "21"
    assert items[0]["building_name"] == "자연과학관"
    assert items[0]["room"] == "101"
    assert items[0]["location_source"] == "official"
    assert items[0]["location_confidence"] == "high"
    assert items[0]["location_text"] == "자연과학관 101호"
