from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidae_secretary.connectors.uclass import (
    normalize_assignments,
    normalize_forum_notifications,
    normalize_notifications,
)
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline

pytestmark = pytest.mark.beta_critical


def _uclass_settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "uclass_ws_base": "https://uclass.example/webservice/rest/server.php",
        "uclass_wstoken": "token",
        "uclass_request_method": "GET",
        "uclass_required_wsfunctions": [],
        "uclass_func_site_info": "core_webservice_get_site_info",
        "uclass_func_popup_notifications": "message_popup_get_popup_notifications",
        "uclass_func_action_events": "core_calendar_get_action_events_by_timesort",
        "uclass_func_courses": "core_enrol_get_users_courses",
        "uclass_func_course_contents": "core_course_get_contents",
        "uclass_func_assignments": "mod_assign_get_assignments",
        "uclass_func_forums": "mod_forum_get_forums_by_courses",
        "uclass_func_forum_discussions": "mod_forum_get_forum_discussions_paginated",
        "uclass_page_limit": 50,
        "uclass_enable_popup_notifications": True,
        "uclass_enable_action_events": True,
        "uclass_enable_courses": True,
        "uclass_enable_contents": False,
        "uclass_enable_assignments": True,
        "uclass_enable_forums": True,
        "uclass_download_materials": False,
        "uclass_download_retries": 1,
        "uclass_download_backoff_sec": 0.01,
        "icloud_dir": tmp_path / "icloud",
        "material_extraction_enabled": False,
        "material_briefing_enabled": False,
        "material_extract_max_chars": 1000,
        "llm_enabled": False,
        "llm_provider": "local",
        "llm_model": "gemma4",
        "llm_timeout_sec": 10,
        "llm_local_endpoint": "http://127.0.0.1:11434/api/chat",
        "timezone": "Asia/Seoul",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "payload",
    [
        {"notifications": [{"id": 1, "subject": "A", "timecreated": 1770000000}]},
        {"items": [{"notificationid": "n-2", "title": "B", "createdat": 1770000100}]},
        [{"id": 3, "text": "C", "timemodified": 1770000200}],
    ],
)
def test_normalize_notifications_supports_multiple_payload_shapes(payload) -> None:
    items = normalize_notifications(payload)
    assert len(items) == 1
    assert items[0].external_id.startswith("uclass:notif:")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "courses": [
                {"id": 101, "assignments": [{"id": 11, "name": "HW1", "duedate": 1770000300}]}
            ]
        },
        {
            "courses": [
                {
                    "id": "101",
                    "assignments": [{"id": "12", "name": "HW2", "cutoffdate": "2026-03-10T10:00:00+09:00"}],
                }
            ]
        },
        {
            "courses": [
                {
                    "id": 101,
                    "assignments": [{"name": "HW3", "allowsubmissionsfromdate": 1770000400}],
                }
            ]
        },
    ],
)
def test_normalize_assignments_supports_multiple_payload_shapes(payload) -> None:
    tasks = normalize_assignments(payload, course_index={101: {"fullname": "Algorithms"}})
    assert len(tasks) == 1
    assert tasks[0].external_id.startswith("uclass:assign:")


@pytest.mark.parametrize(
    "payload",
    [
        {"discussions": [{"discussion": 1, "name": "Topic A", "timemodified": 1770000500}]},
        {"discussions": [{"discussion": "2", "subject": "Topic B", "created": 1770000600}]},
        {"discussions": [{"discussion": 3, "message": "Topic C", "timemodified": 1770000700}]},
    ],
)
def test_normalize_forum_notifications_supports_multiple_shapes(payload) -> None:
    notices = normalize_forum_notifications(payload, forum={"id": 9, "name": "General"})
    assert len(notices) == 1
    assert notices[0].external_id.startswith("uclass:forum:")


def test_uclass_probe_rows_include_shape_fingerprint(tmp_path: Path, monkeypatch) -> None:
    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"userid": 7, "sitename": "Demo"}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms"}]

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {"items": [{"notificationid": "x", "title": "A"}]}

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return [{"id": 10, "name": "Quiz"}]

        def get_course_contents(self, wsfunction: str, course_id: int):
            return []

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {"courses": []}

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return [{"id": 1, "name": "General"}]

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": []}

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    report = pipeline.build_uclass_probe_report(settings=_uclass_settings(tmp_path))

    assert report["rows"]
    assert all("shape_fingerprint" in row for row in report["rows"])
    assert any(row.get("shape_fingerprint") for row in report["rows"] if row.get("status") == "OK")


def test_sync_uclass_emits_structured_semantic_warnings(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"sitename": "Demo", "userid": 1}

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {"items": [{"text": "missing semantic ids/time"}]}

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return {"events": []}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms"}]

        def get_course_contents(self, wsfunction: str, course_id: int):
            return []

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {"courses": [{"id": 101, "assignments": [{"name": "HW missing id+due"}]}]}

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return [{"id": 77, "name": "General"}]

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": [{"subject": "Missing discussion/time"}]}

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    settings = _uclass_settings(tmp_path)

    with caplog.at_level(logging.WARNING):
        result = pipeline.sync_uclass(settings=settings, db=db)

    assert result["semantic_warnings"] >= 3
    records = [
        row for row in caplog.records if row.getMessage() == "uclass semantic fields missing"
    ]
    assert records
    categories = {getattr(row, "category", "") for row in records}
    assert "popup_notifications" in categories
    assert "assignments" in categories
    assert "forum_discussions" in categories
    assert all(isinstance(getattr(row, "missing_fields", None), list) for row in records)


def test_sync_uclass_registers_courses_and_canonical_links(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"sitename": "Demo", "userid": 1}

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {
                "items": [
                    {
                        "id": 1,
                        "title": "중간고사 공지",
                        "timecreated": 1770000000,
                        "courseid": 101,
                    }
                ]
            }

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return {"events": []}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms", "shortname": "알고리즘"}]

        def get_course_contents(self, wsfunction: str, course_id: int):
            return []

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {
                "courses": [
                    {
                        "id": 101,
                        "assignments": [{"id": 11, "name": "HW1", "duedate": 1770000300}],
                    }
                ]
            }

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return []

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": []}

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    settings = _uclass_settings(tmp_path)

    result = pipeline.sync_uclass(settings=settings, db=db)

    assert result["upserted_tasks"] == 1
    canonical_course_id = "uclass:uclass-example:101"
    course = db.get_course(canonical_course_id)
    assert course is not None
    assert course.display_name == "Algorithms"
    aliases = {(item.alias, item.canonical_course_id) for item in db.list_course_aliases()}
    assert ("Algorithms", canonical_course_id) in aliases
    assert ("알고리즘", canonical_course_id) in aliases

    task = db.list_open_tasks(limit=10)[0]
    task_meta = json.loads(task.metadata_json)
    assert task_meta["canonical_course_id"] == canonical_course_id

    notification = db.list_notifications(limit=10)[0]
    notice_meta = json.loads(notification.metadata_json)
    assert notice_meta["canonical_course_id"] == canonical_course_id
