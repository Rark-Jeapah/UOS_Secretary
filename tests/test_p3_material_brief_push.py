from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
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
        "uclass_enable_assignments": False,
        "uclass_enable_forums": False,
        "uclass_download_materials": True,
        "uclass_download_retries": 1,
        "uclass_download_backoff_sec": 0.01,
        "icloud_dir": tmp_path / "icloud",
        "material_extraction_enabled": True,
        "material_briefing_enabled": True,
        "material_brief_push_enabled": True,
        "material_brief_push_max_items": 3,
        "material_extract_max_chars": 12000,
        "llm_enabled": False,
        "llm_provider": "local",
        "llm_model": "gemma4",
        "llm_timeout_sec": 30,
        "llm_local_endpoint": "http://127.0.0.1:11434/api/chat",
        "telegram_enabled": True,
        "telegram_bot_token": "token",
        "telegram_allowed_chat_ids": ["12345"],
        "timezone": "Asia/Seoul",
        "include_identity": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeClient:
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
                                "filename": "week1.pptx",
                                "fileurl": "https://example.com/week1.pptx",
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


def _fake_download_material(db, settings, external_id, url, target, owner_id):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"pptx-placeholder")
    return str(target), "hash-1", True


def test_sync_uclass_pushes_material_brief_to_telegram(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "MoodleWSClient", _FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", _fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: (
            "INTRODUCTION\nMain theorem and proof sketch.\nPractice examples.",
            None,
            "pptx",
        ),
    )
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)

    result = pipeline.sync_uclass(settings=_settings(tmp_path), db=db)

    assert result["generated_material_briefs"] == 1
    assert result["material_brief_push"]["ok"] is True
    assert result["material_brief_push"]["brief_count"] == 1
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "12345"
    assert "[Sidae] 새 강의자료 요약" in sent_messages[0][1]
    assert "자료: 1" in sent_messages[0][1]
    assert "[Algorithms] week1.pptx" in sent_messages[0][1]
    assert "week1.pptx" in sent_messages[0][1]


def test_material_brief_push_message_uses_action_label_for_valid_question() -> None:
    message = pipeline._material_brief_push_message(
        [
            {
                "filename": "chapter01.pdf",
                "course_name": "계량경제학",
                "bullets": [
                    "확률변수 기초: 이산·연속형, pmf/pdf 의미와 확률 계산 방식 정리.",
                    "시험 핵심: 기댓값 선형성, 분산 공식, 공분산·상관계수 관계를 구분해야 함.",
                ],
                "question": "베르누이·균등·정규분포 예제로 기댓값, 분산, 조건부확률 계산 연습해.",
            }
        ]
    )

    assert "[Sidae] 새 강의자료 요약" in message
    assert "자료: 1" in message
    assert "1. [계량경제학] chapter01.pdf" in message
    assert "- 할 일: 베르누이·균등·정규분포 예제로 기댓값, 분산, 조건부확률 계산 연습해." in message


def test_material_brief_push_skips_low_signal_llm_output(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)

    result = pipeline.send_material_brief_push(
        settings=_settings(tmp_path),
        db=db,
        generated_brief_items=[
            {
                "external_id": "uclass:artifact:bad-1",
                "filename": "chapter01_1.pdf",
                "course_name": "Algorithms",
                "bullets": [
                    "token",
                    "No additional material details detected.",
                    "No additional material details detected.",
                ],
                "question": "What is the core argument in chapter01.pdf and how can I explain it from memory?",
            },
            {
                "external_id": "uclass:artifact:bad-2",
                "filename": "Expectation-보충자료.pdf",
                "course_name": "Algorithms",
                "bullets": ["<answer>", "No additional update.", "No additional update."],
                "question": "Review updates and schedule next step.",
            },
        ],
    )

    assert result["skipped"] is True
    assert result["reason"] == "No high-quality material briefs"
    assert sent_messages == []


def test_material_brief_push_skips_login_page_summaries(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)

    result = pipeline.send_material_brief_push(
        settings=_settings(tmp_path),
        db=db,
        generated_brief_items=[
            {
                "external_id": "uclass:artifact:bad-login-1",
                "filename": "index_7.php",
                "course_name": "인간과생명의본질",
                "extract_type": "html",
                "text_excerpt": "서울시립대학교 온라인강의실 로그인 아이디 비밀번호",
                "mode": "llm",
                "bullets": [
                    "제공된 텍스트는 강의실 로그인 페이지 정보일 뿐 실제 수업 내용이나 강의 주제가 포함되어 있지 않습니다.",
                    "파일이 첨부되지 않았으므로 구체적인 시험 중요 개념이나 학습 요약을 생성할 수 없습니다.",
                    "다음 단계: 관련 강의 자료(PDF, PPT) 또는 녹음 파일을 찾아 주요 학습 목표를 정리하세요.",
                ],
                "question": "실제 수업 자료 또는 강의 개요를 제공해 주세요.",
            },
            {
                "external_id": "uclass:artifact:good-1",
                "filename": "week3.pdf",
                "course_name": "알고리즘",
                "extract_type": "pdf",
                "text_excerpt": "최단 경로 문제와 다익스트라 알고리즘의 핵심 개념",
                "mode": "llm",
                "bullets": [
                    "최단 경로 문제 정의와 가중치 그래프 모델링 방식을 정리했다.",
                    "다익스트라 알고리즘의 탐욕 선택 조건과 우선순위 큐 사용 이유를 설명했다.",
                    "시험 대비로 시간복잡도 비교와 예제 추적 연습이 필요하다.",
                ],
                "question": "다익스트라와 BFS의 차이를 예제로 설명해 봐.",
            },
        ],
    )

    assert result["ok"] is True
    assert result["brief_count"] == 1
    assert len(sent_messages) == 1
    assert "index_7.php" not in sent_messages[0][1]
    assert "[알고리즘] week3.pdf" in sent_messages[0][1]


def test_material_brief_push_respects_notification_policy_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(chat_id="12345", material_brief_push_enabled=True)
    db.upsert_user_preferences(chat_id="67890", material_brief_push_enabled=False)
    db.upsert_notification_policy(
        chat_id="12345",
        policy_kind="material_brief_push",
        enabled=False,
    )
    db.upsert_notification_policy(
        chat_id="67890",
        policy_kind="material_brief_push",
        enabled=True,
        days_of_week_json=["mon"],
        time_local="09:00",
        timezone="Asia/Seoul",
    )
    sent_messages: list[tuple[str, str]] = []
    fixed_now = datetime(2026, 4, 6, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))

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

    result = pipeline.send_material_brief_push(
        settings=_settings(tmp_path, telegram_allowed_chat_ids=[]),
        db=db,
        generated_brief_items=[
            {
                "external_id": "uclass:artifact:1",
                "filename": "week1.pdf",
                "course_name": "Algorithms",
                "bullets": [
                    "핵심 개념 정리.",
                    "시험 포인트 요약.",
                    "다음 수업 준비 사항.",
                ],
                "question": "예제 문제를 다시 풀어봐.",
            }
        ],
    )

    assert result["ok"] is True
    assert result["sent_to"] == ["67890"]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "67890"


def test_sync_uclass_material_brief_push_respects_identity_gate(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "MoodleWSClient", _FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", _fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: ("content", None, "pptx"),
    )
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)

    settings = _settings(tmp_path, include_identity=True)
    result = pipeline.sync_uclass(settings=settings, db=db)

    assert result["material_brief_push"]["error"] == "identity_ack_required"
    assert result["material_brief_push"]["blocked"] is True
    assert sent_messages == []


def test_sync_uclass_detects_deadline_tasks_from_material_text(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    monkeypatch.setattr(pipeline, "MoodleWSClient", _FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", _fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: (
            "1주차 안내\n과제 1\n제출기한: 2026.03.10 23:59\n형식: PDF 업로드",
            None,
            "pptx",
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "send_material_brief_push",
        lambda settings, db, generated_brief_items: {"skipped": True},
    )

    result = pipeline.sync_uclass(settings=_settings(tmp_path), db=db)

    tasks = db.list_open_tasks(limit=20)
    assert result["upserted_tasks"] == 0
    assert result["detected_material_tasks"] == 1
    detected = next(task for task in tasks if task.title == "과제 1")
    due_local = pipeline._parse_dt(detected.due_at).astimezone(ZoneInfo("Asia/Seoul"))
    assert due_local.year == 2026
    assert due_local.month == 3
    assert due_local.day == 10
    assert due_local.hour == 23
    assert due_local.minute == 59
    metadata = pipeline._json_load(detected.metadata_json)
    assert metadata["detected_via"] == "material_deadline"
    assert metadata["course_name"] == "Algorithms"


def test_sync_uclass_detects_deadline_tasks_via_llm_when_text_has_no_date(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    llm_calls: list[dict[str, object]] = []

    class FakeLLM:
        def generate_text(self, system_prompt: str, prompt: str, attachment_paths=None) -> str:
            llm_calls.append(
                {
                    "system_prompt": system_prompt,
                    "prompt": prompt,
                    "attachment_paths": list(attachment_paths or []),
                }
            )
            return (
                '{"tasks":[{"title":"과제 2","due_at":"2026-03-12T23:59:00+09:00",'
                '"evidence":"과제 2 제출 마감은 3월 12일 23:59입니다."}]}'
            )

    monkeypatch.setattr(pipeline, "MoodleWSClient", _FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", _fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: (
            "과제 안내는 첨부파일을 확인하세요.\n세부 요구사항은 파일 본문 참고.",
            None,
            "pptx",
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_llm_client",
        lambda settings: FakeLLM(),
    )
    monkeypatch.setattr(
        pipeline,
        "send_material_brief_push",
        lambda settings, db, generated_brief_items: {"skipped": True},
    )

    settings = _settings(
        tmp_path,
        llm_enabled=True,
        llm_provider="local",
    )
    result = pipeline.sync_uclass(settings=settings, db=db)

    tasks = db.list_open_tasks(limit=20)
    assert result["detected_material_tasks"] == 1
    assert any(task.title == "과제 2" for task in tasks)
    assert llm_calls
    assert llm_calls[0]["attachment_paths"] == []


def test_sync_uclass_marks_deadline_scan_incomplete_when_llm_deadline_probe_fails(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    class FailingLLM:
        def generate_text(self, system_prompt: str, prompt: str, attachment_paths=None) -> str:
            raise RuntimeError("playwright missing")

    monkeypatch.setattr(pipeline, "MoodleWSClient", _FakeClient)
    monkeypatch.setattr(pipeline, "_download_material", _fake_download_material)
    monkeypatch.setattr(
        pipeline,
        "extract_material_text",
        lambda path, max_chars: (
            "과제 안내는 첨부파일을 확인하세요.\n세부 요구사항은 파일 본문 참고.",
            None,
            "pptx",
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_llm_client",
        lambda settings: FailingLLM(),
    )
    monkeypatch.setattr(
        pipeline,
        "send_material_brief_push",
        lambda settings, db, generated_brief_items: {"skipped": True},
    )

    settings = _settings(
        tmp_path,
        llm_enabled=True,
        llm_provider="local",
    )
    result = pipeline.sync_uclass(settings=settings, db=db)

    artifact = db.list_artifacts(limit=10)[0]
    metadata = pipeline._json_load(artifact.metadata_json)
    assert result["detected_material_tasks"] == 0
    assert metadata["deadline_scan"]["ok"] is False
    assert metadata["deadline_scan"]["error"] == "playwright missing"


def test_material_deadline_scan_skips_llm_when_heuristic_already_finds_due_date(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    called = {"count": 0}

    class UnexpectedLLM:
        def generate_text(self, system_prompt: str, prompt: str, attachment_paths=None) -> str:
            called["count"] += 1
            raise AssertionError("LLM should be skipped when heuristic task extraction already succeeded")

    monkeypatch.setattr(
        pipeline,
        "_llm_client",
        lambda settings, timeout_sec=None: UnexpectedLLM(),
    )
    settings = _settings(
        tmp_path,
        llm_enabled=True,
        llm_provider="local",
        llm_timeout_sec=120,
    )

    scan = pipeline._build_material_deadline_scan(
        settings=settings,
        db=db,
        artifact_external_id="uclass:artifact:week1",
        title="week1.pptx",
        course_name="Algorithms",
        canonical_course_id="course-101",
        extracted_text="과제 1 제출\n마감 2026-03-25 18:00",
        local_path=str(tmp_path / "week1.pptx"),
        reference_local=datetime(2026, 3, 19, 21, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        artifact_provenance_source="uclass_html",
        artifact_evidence_links=None,
    )

    assert called["count"] == 0
    assert scan["method"] == "heuristic"
    assert scan["count"] == 1
