from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


class FakeMoodleWSClient:
    def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
        self.base_url = base_url

    def get_site_info(self, wsfunction: str):
        return {"sitename": "Demo", "userid": 7}

    def get_popup_notifications(self, wsfunction: str, limit: int = 50):
        return {
            "notifications": [
                {
                    "id": 10,
                    "subject": "Forum notice",
                    "fullmessage": "Week 1 posted",
                    "timecreated": 1770000000,
                }
            ]
        }

    def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
        return {
            "events": [
                {
                    "id": 21,
                    "name": "Quiz 1",
                    "timesort": 1770100000,
                    "timestart": 1770100000,
                    "timeend": 1770103600,
                }
            ]
        }

    def get_users_courses(self, wsfunction: str):
        return [{"id": 101, "fullname": "Algorithms"}]

    def get_course_contents(self, wsfunction: str, course_id: int):
        return [
            {
                "name": "Week 1",
                "modules": [
                    {
                        "id": 501,
                        "name": "Lecture Slides",
                        "modname": "resource",
                        "contents": [
                            {
                                "filename": "week1.pdf",
                                "fileurl": "https://uclass.example/week1.pdf",
                            }
                        ],
                    }
                ],
            }
        ]

    def get_assignments(self, wsfunction: str, course_ids: list[int]):
        return {
            "courses": [
                {
                    "id": 101,
                    "assignments": [
                        {
                            "id": 301,
                            "name": "HW 1",
                            "duedate": 1770200000,
                        }
                    ],
                }
            ]
        }

    def get_forums(self, wsfunction: str, course_ids: list[int]):
        return {"forums": [{"id": 901, "name": "General"}]}

    def get_forum_discussions(
        self,
        wsfunction: str,
        forum_id: int,
        page: int = 0,
        per_page: int = 20,
    ):
        return {
            "discussions": [
                {
                    "discussion": 44,
                    "name": "Read chapter 1",
                    "timemodified": 1770001000,
                    "message": "Reminder",
                }
            ]
        }


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        timezone="Asia/Seoul",
        sync_window_days=120,
        uclass_ws_base="https://uclass.example/webservice/rest/server.php",
        uclass_wstoken="token",
        uclass_request_method="GET",
        uclass_required_wsfunctions=[],
        uclass_func_site_info="core_webservice_get_site_info",
        uclass_func_popup_notifications="message_popup_get_popup_notifications",
        uclass_func_action_events="core_calendar_get_action_events_by_timesort",
        uclass_func_courses="core_enrol_get_users_courses",
        uclass_func_course_contents="core_course_get_contents",
        uclass_func_assignments="mod_assign_get_assignments",
        uclass_func_forums="mod_forum_get_forums_by_courses",
        uclass_func_forum_discussions="mod_forum_get_forum_discussions_paginated",
        uclass_page_limit=50,
        uclass_enable_popup_notifications=True,
        uclass_enable_action_events=True,
        uclass_enable_courses=True,
        uclass_enable_contents=True,
        uclass_enable_assignments=True,
        uclass_enable_forums=True,
        uclass_download_materials=False,
        uclass_download_retries=1,
        uclass_download_backoff_sec=0.01,
        storage_root_dir=tmp_path / "storage",
        material_extraction_enabled=False,
        material_briefing_enabled=False,
        material_extract_max_chars=1000,
        llm_enabled=False,
        llm_provider="local",
        llm_model="gemma4",
        llm_timeout_sec=10,
        llm_local_endpoint="http://127.0.0.1:11434/api/chat",
        review_enabled=False,
        review_intervals_days=[1, 3],
        review_duration_min=25,
        review_morning_hour=9,
        telegram_enabled=False,
        telegram_bot_token=None,
        telegram_allowed_chat_ids=[],
        telegram_poll_limit=100,
        telegram_commands_enabled=False,
        digest_enabled=False,
        digest_channel="telegram",
        digest_time_local="08:30",
        digest_task_lookahead_days=3,
    )


def test_sync_all_is_idempotent_with_deterministic_adapters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeMoodleWSClient)

    settings = _settings(tmp_path)
    db = Database(tmp_path / "sidae.db")
    db.init()

    first = pipeline.run_all_jobs(settings=settings, db=db)
    assert first.ok is True
    counts_after_first = db.counts()

    second = pipeline.run_all_jobs(settings=settings, db=db)
    assert second.ok is True
    counts_after_second = db.counts()

    for table in ("events", "tasks", "artifacts", "notifications", "inbox", "summaries"):
        assert counts_after_second[table] == counts_after_first[table]
