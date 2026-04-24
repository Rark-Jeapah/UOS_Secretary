from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidae_secretary.connectors.uclass import (
    MaterialCandidate,
    extract_material_candidates_from_course_contents,
)
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline

pytestmark = pytest.mark.beta_critical


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "uclass_ws_base": "https://uclass.example/webservice/rest/server.php",
        "uclass_wstoken": "token",
        "uclass_username": "student",
        "uclass_password": "secret",
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
        "uclass_enable_contents": True,
        "uclass_enable_assignments": True,
        "uclass_enable_forums": True,
        "uclass_download_materials": True,
        "uclass_download_retries": 1,
        "uclass_download_backoff_sec": 0.01,
        "storage_root_dir": tmp_path / "storage",
        "material_extraction_enabled": True,
        "material_briefing_enabled": True,
        "material_extract_max_chars": 12000,
        "timezone": "Asia/Seoul",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_prepare_uclass_target_auth_requires_token_when_ws_token_unavailable(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, uclass_wstoken="")
    with pytest.raises(RuntimeError, match="UClass token expired or unavailable; reconnect required"):
        pipeline._prepare_uclass_target_auth(
            settings,
            target={
                "user_id": 7,
                "ws_base_url": settings.uclass_ws_base,
                "token": "",
                "token_error": "",
            },
            owner_id=7,
        )

    assert getattr(settings, "_uclass_resolved_token") == ""


def test_resolve_uclass_sync_targets_returns_token_only_targets(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = _settings(tmp_path, uclass_username="student", uclass_password="secret")
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url=settings.uclass_ws_base,
        username="student",
        secret_kind="file",
        secret_ref="student.secret",
    )
    db.upsert_moodle_connection(
        chat_id="67890",
        school_slug="uos_online_class_2",
        display_name="서울시립대학교 온라인강의실 2",
        ws_base_url=settings.uclass_ws_base,
        username="other-student",
        secret_kind="file",
        secret_ref="other.secret",
        login_secret_kind="file",
        login_secret_ref="other-login.secret",
    )

    class FakeSecretStore:
        def read_secret(self, ref):
            return f"token-for:{ref.ref}"

    monkeypatch.setattr(pipeline, "default_secret_store", lambda settings: FakeSecretStore())

    targets = pipeline._resolve_uclass_sync_targets(settings, db)

    assert len(targets) == 2
    by_chat = {str(item["chat_id"]): item for item in targets}
    assert by_chat["12345"]["token"] == "token-for:student.secret"
    assert by_chat["12345"]["token_error"] == ""
    assert "allow_html_fallback" not in by_chat["12345"]
    assert "html_fallback_username" not in by_chat["12345"]
    assert by_chat["67890"]["token"] == "token-for:other.secret"
    assert by_chat["67890"]["token_error"] == ""
    assert "allow_html_fallback" not in by_chat["67890"]
    assert "html_fallback_secret_ref" not in by_chat["67890"]


def test_resolve_uclass_sync_targets_scopes_static_token_fallback_to_single_user(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_user_preferences(
        user_id=int(user["id"]),
        telegram_chat_allowed=True,
        scheduled_briefings_enabled=True,
    )
    settings = _settings(
        tmp_path,
        uclass_wstoken="legacy-token",
        telegram_allowed_chat_ids=["12345"],
    )

    targets = pipeline._resolve_uclass_sync_targets(settings, db)

    assert len(targets) == 1
    assert targets[0]["user_id"] == int(user["id"])
    assert targets[0]["chat_id"] == "12345"
    assert targets[0]["token"] == "legacy-token"


def test_fetch_uclass_ws_stage_normalizes_payloads_and_registers_courses(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = _settings(tmp_path)

    class FakeClient:
        def get_site_info(self, wsfunction: str):
            return {"sitename": "Demo", "userid": 1}

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {
                "notifications": [
                    {
                        "id": 10,
                        "subject": "Forum notice",
                        "fullmessage": "Week 1 posted",
                        "timecreated": 1770000000,
                        "courseid": 101,
                    }
                ]
            }

        def get_action_events(self, wsfunction: str, limitnum: int = 50):
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
            return [{"id": 101, "fullname": "Algorithms", "shortname": "알고리즘"}]

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
                    }
                ]
            }

    auth = pipeline._UClassTargetAuthContext(
        owner_id=0,
        ws_base_url=settings.uclass_ws_base,
        token="token",
        token_error="",
        html_material_candidates=[],
        html_material_error="",
        client=FakeClient(),
        ws_available=True,
        required_ws=set(),
        ws_status={},
    )

    ws_data = pipeline._fetch_uclass_ws_stage(
        settings,
        db,
        owner_id=0,
        auth=auth,
    )

    assert ws_data.site_info["sitename"] == "Demo"
    assert len(ws_data.notifications) == 2
    assert len(ws_data.tasks) == 2
    assert len(ws_data.events) == 1
    assert ws_data.contents_payload_by_course[101]
    assert ws_data.canonical_courses[101] == "uclass:uclass-example:101"
    assert auth.ws_status[settings.uclass_func_site_info]["ok"] == 1
    courses = db.list_courses(limit=10)
    assert len(courses) == 1
    assert courses[0].canonical_course_id == "uclass:uclass-example:101"


def test_register_uclass_courses_extracts_official_section_from_shortname(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    settings = _settings(tmp_path)

    pipeline._register_uclass_courses(
        settings,
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

    course = db.get_course("uclass:uclass-uos-ac-kr:3821", user_id=int(user["id"]))
    metadata = json.loads(course.metadata_json or "{}")
    assert metadata["official_subject_no"] == "02115"
    assert metadata["official_dvcl_no"] == "35"
    aliases = db.list_course_aliases(
        user_id=int(user["id"]),
        normalized_alias="0211535",
        limit=10,
    )
    assert len(aliases) == 1
    assert aliases[0].canonical_course_id == "uclass:uclass-uos-ac-kr:3821"


def test_discover_uclass_material_candidates_dedupes_html_and_ws_sources() -> None:
    course_contents = {
        101: [
            {
                "name": "Week 1",
                "modules": [
                    {
                        "id": 500,
                        "name": "Lecture 1",
                        "modname": "resource",
                        "url": "https://uclass.example/mod/resource/view.php?id=500",
                        "contents": [
                            {
                                "filename": "slides.pdf",
                                "fileurl": "https://uclass.example/pluginfile.php/500/slides.pdf",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    ws_data = pipeline._UClassTargetWSResult(
        site_info={},
        course_index={101: {"fullname": "Algorithms"}},
        canonical_courses={},
        alias_map={},
        contents_payload_by_course=course_contents,
        notifications=[],
        tasks=[],
        events=[],
        semantic_warnings=0,
    )
    ws_candidates = extract_material_candidates_from_course_contents(
        course_contents=course_contents,
        course_index=ws_data.course_index,
    )
    html_duplicate = MaterialCandidate(
        external_id=ws_candidates[0].external_id,
        filename="slides.pdf",
        url="https://uclass.example/pluginfile.php/500/slides.pdf",
        course="Algorithms",
        date_folder="2026-03-04",
        metadata={"source_kind": "uboard_attachment"},
    )

    candidates = pipeline._discover_uclass_material_candidates(
        ws_data,
        timezone_name="Asia/Seoul",
        html_material_candidates=[html_duplicate],
    )

    assert len(candidates) == len(ws_candidates)
    assert sorted(item.external_id for item in candidates) == sorted(
        item.external_id for item in ws_candidates
    )


def test_discover_uclass_material_candidates_passes_timezone_name(monkeypatch) -> None:
    seen: list[str] = []
    ws_data = pipeline._UClassTargetWSResult(
        site_info={},
        course_index={},
        canonical_courses={},
        alias_map={},
        contents_payload_by_course={},
        notifications=[],
        tasks=[],
        events=[],
        semantic_warnings=0,
    )

    monkeypatch.setattr(
        pipeline,
        "extract_material_candidates",
        lambda notifications, tasks, events, **kwargs: seen.append(kwargs["timezone_name"]) or [],
    )
    monkeypatch.setattr(
        pipeline,
        "extract_material_candidates_from_course_contents",
        lambda course_contents, course_index, **kwargs: seen.append(kwargs["timezone_name"]) or [],
    )

    pipeline._discover_uclass_material_candidates(
        ws_data,
        timezone_name="Asia/Seoul",
        html_material_candidates=[],
    )

    assert seen == ["Asia/Seoul", "Asia/Seoul"]


def test_sync_uclass_materials_runs_download_summary_and_task_stages(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = _settings(tmp_path)
    candidate = MaterialCandidate(
        external_id="uclass:artifact:stage-1",
        filename="week1.pdf",
        url="https://uclass.example/week1.pdf",
        course="Algorithms",
        date_folder="2026-03-04",
        metadata={"course_name": "Algorithms"},
    )

    def _fake_download_material(db, settings, external_id, url, target, owner_id):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4 fake")
        return str(target), "hash-1", True

    monkeypatch.setattr(pipeline, "_download_material", _fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: ("week1 text with assignment note", None, "pdf"),
    )
    monkeypatch.setattr(
        pipeline,
        "_build_material_brief",
        lambda **kwargs: {
            "mode": "heuristic",
            "bullets": ["A", "B", "C", "D", "E"],
            "question": "Review week1.pdf",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "_build_material_deadline_scan",
        lambda **kwargs: {
            "ok": True,
            "version": pipeline.MATERIAL_DEADLINE_SCAN_VERSION,
            "count": 2,
        },
    )

    result = pipeline._sync_uclass_materials(
        settings,
        db,
        owner_id=0,
        candidates=[candidate],
        alias_map={},
        canonical_courses={},
    )

    assert result.artifact_count == 1
    assert result.downloaded_count == 1
    assert result.extracted_count == 1
    assert result.brief_count == 1
    assert result.material_task_count == 2
    assert result.generated_brief_items[0]["filename"] == "week1.pdf"

    artifacts = db.list_artifacts(limit=10)
    metadata = json.loads(artifacts[0].metadata_json)
    assert metadata["text_extract"]["ok"] is True
    assert metadata["brief"]["question"] == "Review week1.pdf"
    assert metadata["deadline_scan"]["count"] == 2
