from __future__ import annotations

from hashlib import sha1
import json
from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


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
        "uclass_enable_contents": True,
        "uclass_enable_assignments": True,
        "uclass_enable_forums": True,
        "uclass_download_materials": True,
        "uclass_download_retries": 1,
        "uclass_download_backoff_sec": 0.01,
        "icloud_dir": tmp_path / "icloud",
        "material_extraction_enabled": True,
        "material_briefing_enabled": True,
        "material_extract_max_chars": 12000,
        "llm_enabled": False,
        "llm_provider": "local",
        "llm_model": "gemma4",
        "llm_timeout_sec": 10,
        "llm_local_endpoint": "http://127.0.0.1:11434/api/chat",
        "timezone": "Asia/Seoul",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_uclass_probe_report_has_ok_fail_skip_rows(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"userid": 7, "sitename": "Demo"}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms"}]

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            raise RuntimeError("ws disabled on this server")

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return {"events": []}

        def get_course_contents(self, wsfunction: str, course_id: int):
            return []

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {"courses": []}

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return {"forums": [{"id": 1, "name": "General"}]}

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": []}

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    settings = _uclass_settings(tmp_path, uclass_enable_assignments=False)

    report = pipeline.build_uclass_probe_report(settings=settings)
    rows = {row["key"]: row for row in report["rows"]}

    assert report["site_info"]["userid"] == 7
    assert rows["site_info"]["status"] == "OK"
    assert rows["popup_notifications"]["status"] == "FAIL"
    assert rows["assignments"]["status"] == "SKIP"
    assert rows["forum_discussions"]["status"] == "OK"


def test_sync_uclass_enriches_material_with_extract_and_brief(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"sitename": "Demo", "userid": 1}

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {"notifications": []}

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return {"events": []}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms"}]

        def get_course_contents(self, wsfunction: str, course_id: int):
            return [
                {
                    "name": "Week 1",
                    "modules": [
                        {
                            "id": 11,
                            "name": "Lecture Slides",
                            "modname": "resource",
                            "contents": [
                                {
                                    "filename": "week1.pdf",
                                    "fileurl": "https://example.com/week1.pdf",
                                }
                            ],
                        }
                    ],
                }
            ]

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {"courses": []}

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return {"forums": []}

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": []}

    def fake_download_material(db, settings, external_id, url, target, owner_id):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4 fake")
        return str(target), "hash-1", True

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: (
            "INTRODUCTION\nMain theorem and proof sketch.\nPractice examples.",
            None,
            "pdf",
        ),
    )

    settings = _uclass_settings(
        tmp_path,
        uclass_enable_forums=False,
        uclass_enable_assignments=False,
        llm_enabled=False,
    )

    result = pipeline.sync_uclass(settings=settings, db=db)

    artifacts = db.list_artifacts(limit=20)
    assert result["recorded_artifacts"] == 1
    assert result["extracted_artifacts"] == 1
    assert result["generated_material_briefs"] == 1
    assert len(artifacts) == 1
    metadata = json.loads(artifacts[0].metadata_json)
    assert metadata["text_extract"]["ok"] is True
    assert len(metadata["brief"]["bullets"]) == 5
    assert len(metadata["brief"]["key_terms"]) == 3


def test_sync_uclass_backfills_missing_brief_from_existing_extract(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    material_path = tmp_path / "week1.pdf"
    material_path.write_bytes(b"%PDF-1.4 fake")

    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"sitename": "Demo", "userid": 1}

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {"notifications": []}

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return {"events": []}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms"}]

        def get_course_contents(self, wsfunction: str, course_id: int):
            return [
                {
                    "name": "Week 1",
                    "modules": [
                        {
                            "id": 11,
                            "name": "Lecture Slides",
                            "modname": "resource",
                            "contents": [
                                {
                                    "filename": "week1.pdf",
                                    "fileurl": "https://example.com/week1.pdf",
                                }
                            ],
                        }
                    ],
                }
            ]

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {"courses": []}

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return {"forums": []}

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": []}

    def fake_download_material(db, settings, external_id, url, target, owner_id):
        return str(material_path), "hash-1", False

    def fail_extract(path, max_chars):
        raise AssertionError("existing excerpt should be reused for brief backfill")

    db.record_artifact(
        external_id="uclass:artifact:existing-week1",
        source="uclass",
        filename="week1.pdf",
        icloud_path=str(material_path),
        content_hash="hash-1",
        metadata_json={
            "course_name": "Algorithms",
            "url": "https://example.com/week1.pdf",
            "text_extract": {
                "ok": True,
                "type": "pdf",
                "hash": "text-hash",
                "chars": 48,
                "excerpt": "Shortest path introduction\nDijkstra overview\nPractice examples",
            },
            "deadline_scan": {
                "ok": True,
                "version": pipeline.MATERIAL_DEADLINE_SCAN_VERSION,
                "count": 0,
            },
        },
    )

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_candidates_from_course_contents",
        lambda course_contents, course_index, **kwargs: [
            SimpleNamespace(
                external_id="uclass:artifact:existing-week1",
                filename="week1.pdf",
                url="https://example.com/week1.pdf",
                course="Algorithms",
                date_folder="2026-03-04",
                metadata={"course_name": "Algorithms"},
            )
        ],
    )
    monkeypatch.setattr(pipeline, "extract_material_text", fail_extract)
    settings = _uclass_settings(
        tmp_path,
        uclass_enable_forums=False,
        uclass_enable_assignments=False,
        llm_enabled=False,
    )

    result = pipeline.sync_uclass(settings=settings, db=db)

    artifacts = db.list_artifacts(limit=20)
    metadata = json.loads(artifacts[0].metadata_json)
    assert result["recorded_artifacts"] == 1
    assert result["generated_material_briefs"] == 1
    assert result["extracted_artifacts"] == 0
    assert metadata["text_extract"]["ok"] is True
    assert isinstance(metadata.get("brief"), dict)
    assert len(metadata["brief"]["bullets"]) == 5


def test_sync_uclass_regenerates_brief_when_material_content_changes(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    old_material_path = tmp_path / "week1-old.pdf"
    new_material_path = tmp_path / "week1-new.pdf"
    old_material_path.write_bytes(b"%PDF-1.4 old")
    new_material_path.write_bytes(b"%PDF-1.4 new")

    class FakeClient:
        def __init__(self, base_url: str, token: str, request_method: str = "GET", timeout_sec: int = 30):
            pass

        def get_site_info(self, wsfunction: str):
            return {"sitename": "Demo", "userid": 1}

        def get_popup_notifications(self, wsfunction: str, limit: int = 50):
            return {"notifications": []}

        def get_action_events(self, wsfunction: str, timesortfrom: int | None = None, limitnum: int = 50):
            return {"events": []}

        def get_users_courses(self, wsfunction: str):
            return [{"id": 101, "fullname": "Algorithms"}]

        def get_course_contents(self, wsfunction: str, course_id: int):
            return [
                {
                    "name": "Week 1",
                    "modules": [
                        {
                            "id": 11,
                            "name": "Lecture Slides",
                            "modname": "resource",
                            "contents": [
                                {
                                    "filename": "week1.pdf",
                                    "fileurl": "https://example.com/week1.pdf",
                                }
                            ],
                        }
                    ],
                }
            ]

        def get_assignments(self, wsfunction: str, course_ids: list[int]):
            return {"courses": []}

        def get_forums(self, wsfunction: str, course_ids: list[int]):
            return {"forums": []}

        def get_forum_discussions(self, wsfunction: str, forum_id: int, page: int = 0, per_page: int = 20):
            return {"discussions": []}

    def fake_download_material(db, settings, external_id, url, target, owner_id):
        return str(new_material_path), "hash-2", True

    db.record_artifact(
        external_id="uclass:artifact:existing-week1",
        source="uclass",
        filename="week1.pdf",
        icloud_path=str(old_material_path),
        content_hash="hash-1",
        metadata_json={
            "course_name": "Algorithms",
            "url": "https://example.com/week1.pdf",
            "text_extract": {
                "ok": True,
                "type": "pdf",
                "hash": "text-hash-1",
                "chars": 22,
                "excerpt": "Old theorem summary",
            },
            "brief": {
                "mode": "heuristic",
                "version": pipeline.MATERIAL_BRIEF_VERSION,
                "source_text_hash": "text-hash-1",
                "bullets": ["Old theorem summary"],
                "key_terms": ["old"],
                "question": "Old question",
            },
        },
    )

    monkeypatch.setattr(pipeline, "MoodleWSClient", FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_candidates_from_course_contents",
        lambda course_contents, course_index, **kwargs: [
            SimpleNamespace(
                external_id="uclass:artifact:existing-week1",
                filename="week1.pdf",
                url="https://example.com/week1.pdf",
                course="Algorithms",
                date_folder="2026-03-04",
                metadata={"course_name": "Algorithms"},
            )
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: (
            "Updated theorem statement\nNew proof sketch\nPractice set 2",
            None,
            "pdf",
        ),
    )

    settings = _uclass_settings(
        tmp_path,
        uclass_enable_forums=False,
        uclass_enable_assignments=False,
        llm_enabled=False,
    )
    result = pipeline.sync_uclass(settings=settings, db=db)

    artifacts = db.list_artifacts(limit=20)
    metadata = json.loads(artifacts[0].metadata_json)
    assert result["generated_material_briefs"] == 1
    assert result["extracted_artifacts"] == 1
    assert metadata["text_extract"]["hash"] == sha1(
        "Updated theorem statement\nNew proof sketch\nPractice set 2".encode("utf-8")
    ).hexdigest()
    assert metadata["brief"]["source_text_hash"] == metadata["text_extract"]["hash"]
    assert metadata["brief"]["version"] == pipeline.MATERIAL_BRIEF_VERSION
    assert metadata["brief"]["bullets"][0] != "Old theorem summary"


def test_build_material_brief_uses_local_llm_without_attachment_paths(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    called: dict[str, object] = {}
    material_path = tmp_path / "week1.pdf"
    material_path.write_bytes(b"%PDF-1.4 fake")

    class FakeClient:
        def summarize(
            self,
            payload,
            *,
            system_prompt: str,
            attachment_paths=None,
        ):
            called["payload"] = payload
            called["system_prompt"] = system_prompt
            called["attachment_paths"] = attachment_paths
            return SimpleNamespace(
                bullets=["핵심 1", "핵심 2", "핵심 3"],
                action_item="복습하기",
            )

    def _fake_llm_client(settings, timeout_sec=None):
        called["timeout_sec"] = timeout_sec
        return FakeClient()

    monkeypatch.setattr(pipeline, "_llm_client", _fake_llm_client)
    settings = _uclass_settings(
        tmp_path,
        llm_enabled=True,
        llm_provider="local",
        llm_timeout_sec=120,
    )
    extracted_text = "복지국가와 빈곤 개념을 설명한다. " * 400

    brief = pipeline._build_material_brief(
        settings=settings,
        db=db,
        title="week1.pdf",
        extracted_text=extracted_text,
        local_path=str(material_path),
    )

    assert brief["mode"] == "llm"
    assert called["attachment_paths"] is None
    assert called["payload"]["file_attached"] is False
    assert called["timeout_sec"] == pipeline.MATERIAL_LLM_TIMEOUT_SEC_CAP
    assert called["payload"]["text_excerpt"] == extracted_text[: pipeline.MATERIAL_BRIEF_LLM_TEXT_EXCERPT_CHAR_CAP]
