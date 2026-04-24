from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.connectors import portal as portal_connector
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline
from sidae_secretary.secret_store import SecretStoreError


def _portal_timetable_metadata(extra: dict[str, object] | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {"timetable_source": "uos_portal"}
    if extra:
        metadata.update(extra)
    return metadata


def test_sync_telegram_registers_bot_menu_commands(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    registered_commands: list[list[dict[str, str]]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
            registered_commands.append(commands)
            return True

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return []

        def send_message(self, chat_id: str | int, text: str) -> bool:
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
    )

    first = pipeline.sync_telegram(settings=settings, db=db)
    second = pipeline.sync_telegram(settings=settings, db=db)

    assert len(registered_commands) == 1
    commands = registered_commands[0]
    assert {"command": "today", "description": "오늘 일정과 마감 보기"} in commands
    assert {"command": "tomorrow", "description": "내일 일정과 마감 보기"} in commands
    assert {"command": "weather", "description": "오늘/내일 날씨 보기"} in commands
    assert {"command": "region", "description": "날씨 지역 설정"} in commands
    assert {"command": "todaysummary", "description": "오늘 수업 자료 요약 보기"} in commands
    assert {"command": "tomorrowsummary", "description": "내일 수업 자료 요약 보기"} in commands
    assert {"command": "notice_uclass", "description": "온라인강의실 최근 알림 보기"} in commands
    assert {"command": "plan", "description": "자연어 리마인더 예약"} not in commands
    assert first["menu"]["updated"] is True
    assert second["menu"]["updated"] is False


def test_sync_telegram_registers_bot_only_when_assistant_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    registered_commands: list[list[dict[str, str]]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
            registered_commands.append(commands)
            return True

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return []

        def send_message(self, chat_id: str | int, text: str) -> bool:
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        telegram_assistant_enabled=True,
    )

    pipeline.sync_telegram(settings=settings, db=db)

    commands = registered_commands[0]
    assert {"command": "bot", "description": "자연어 비서"} in commands
    assert {"command": "assistant", "description": "자연어 비서"} not in commands
    assert {"command": "asis", "description": "자연어 비서"} not in commands


def test_sync_telegram_skips_menu_retry_during_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    register_calls: list[list[dict[str, str]]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
            register_calls.append(commands)
            raise RuntimeError("menu timeout")

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return []

        def send_message(self, chat_id: str | int, text: str) -> bool:
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
    )

    first = pipeline.sync_telegram(settings=settings, db=db)
    second = pipeline.sync_telegram(settings=settings, db=db)

    assert len(register_calls) == 1
    assert first["menu"]["ok"] is False
    assert second["menu"]["skipped"] is True
    assert second["menu"]["reason"] == "telegram menu retry cooldown"


def test_sync_telegram_processes_commands_idempotently(
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
                        "text": "/status",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
    )

    first = pipeline.sync_telegram(settings=settings, db=db)
    second = pipeline.sync_telegram(settings=settings, db=db)

    assert first["fetched_updates"] == 1
    assert first["commands"]["processed"] == 1
    assert second["fetched_updates"] == 0
    assert second["commands"]["processed"] == 0
    assert len(sent_messages) == 1
    assert "Telegram" in sent_messages[0][1]
    assert "UClass" in sent_messages[0][1]
    assert "[Sidae] 상태 요약" in sent_messages[0][1]
    assert "서비스별 상태" in sent_messages[0][1]


def test_sync_telegram_allows_start_for_unapproved_chat(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
            return True

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 10,
                    "message": {
                        "date": 1770000000,
                        "text": "/start",
                        "chat": {"id": 77777, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "77777"
    assert "[Sidae] 시작 안내" in sent_messages[0][1]
    assert "할 수 있는 일" in sent_messages[0][1]
    assert "- /connect" in sent_messages[0][1]
    assert "/connect 연세대학교" not in sent_messages[0][1]
    assert "자연어 리마인더 예약" not in sent_messages[0][1]
    assert "아직 사용할 수 있도록 활성화되지 않았습니다" in sent_messages[0][1]


def test_format_telegram_help_and_start_expose_only_bot_for_assistant(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_assistant_enabled=True,
        telegram_smart_commands_enabled=False,
    )

    help_message = pipeline._format_telegram_help(settings)
    start_message = pipeline._format_telegram_start(settings, db=db, chat_id="12345", user_id=None)

    assert "/bot <자연어>" in help_message
    assert "/assistant" not in help_message
    assert "/asis" not in help_message
    assert "/bot 오늘 일정이랑 날씨 알려줘" in start_message
    assert "/assistant" not in start_message
    assert "/asis" not in start_message


def test_sync_telegram_setup_command_returns_checklist(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 10,
                    "message": {
                        "date": 1770000000,
                        "text": "/setup",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        uclass_wstoken="legacy-token",
        llm_provider="local",
        llm_local_endpoint="http://127.0.0.1:11434/api/chat",
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 연결 상태" in message
    assert "- Telegram 채팅: 준비됨" in message
    assert "- UClass 계정: 준비됨" in message
    assert "- 로컬 LLM 요약(선택): 준비됨" in message
    assert "/plan 내일 밤 10시에 과제 제출하라고 알려줘" not in message


def test_sync_telegram_assistant_aliases_route_to_same_internal_command(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    seen_requests: list[str] = []
    sent_messages: list[tuple[str, str]] = []
    chat_actions: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
            return True

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 31,
                    "message": {
                        "date": 1770000000,
                        "text": "/bot 오늘 일정 알려줘",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                },
                {
                    "update_id": 32,
                    "message": {
                        "date": 1770000001,
                        "text": "/assistant 오늘 일정 알려줘",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                },
                {
                    "update_id": 33,
                    "message": {
                        "date": 1770000002,
                        "text": "/asis 오늘 일정 알려줘",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                },
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

        def send_chat_action(self, chat_id: str | int, action: str = "typing") -> bool:
            chat_actions.append((str(chat_id), action))
            return True

    def _fake_plan(settings, text: str):
        seen_requests.append(text)
        return {
            "intent": "needs_clarification",
            "confidence": 0.5,
            "actions": [],
            "reply": f"assistant:{text}",
            "needs_clarification": True,
        }

    def _fake_execute(settings, db, *, plan, user_id=None, chat_id=None):
        return {
            "ok": True,
            "reply": str(plan.get("reply") or ""),
            "needs_clarification": bool(plan.get("needs_clarification")),
        }

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(pipeline, "plan_assistant_request", _fake_plan)
    monkeypatch.setattr(pipeline, "execute_assistant_plan", _fake_execute)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        telegram_assistant_enabled=True,
        telegram_assistant_write_enabled=False,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 3
    assert seen_requests == ["오늘 일정 알려줘", "오늘 일정 알려줘", "오늘 일정 알려줘"]
    assert chat_actions == [
        ("12345", "typing"),
        ("12345", "typing"),
        ("12345", "typing"),
    ]
    assert [text for _, text in sent_messages] == [
        "assistant:오늘 일정 알려줘",
        "assistant:오늘 일정 알려줘",
        "assistant:오늘 일정 알려줘",
    ]


def test_sync_telegram_assistant_repeats_typing_for_long_running_request(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []
    chat_actions: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
            return True

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 34,
                    "message": {
                        "date": 1770000003,
                        "text": "/bot 오늘 일정 알려줘",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

        def send_chat_action(self, chat_id: str | int, action: str = "typing") -> bool:
            chat_actions.append((str(chat_id), action))
            return True

    def _fake_plan(settings, text: str):
        return {
            "intent": "needs_clarification",
            "confidence": 0.5,
            "actions": [],
            "reply": f"assistant:{text}",
            "needs_clarification": True,
        }

    def _slow_execute(settings, db, *, plan, user_id=None, chat_id=None):
        time.sleep(0.05)
        return {
            "ok": True,
            "reply": str(plan.get("reply") or ""),
            "needs_clarification": bool(plan.get("needs_clarification")),
        }

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(pipeline, "plan_assistant_request", _fake_plan)
    monkeypatch.setattr(pipeline, "execute_assistant_plan", _slow_execute)
    monkeypatch.setattr(pipeline, "TELEGRAM_ASSISTANT_CHAT_ACTION_INTERVAL_SEC", 0.01)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        telegram_assistant_enabled=True,
        telegram_assistant_write_enabled=False,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(chat_actions) >= 2
    assert all(item == ("12345", "typing") for item in chat_actions)
    assert [text for _, text in sent_messages] == ["assistant:오늘 일정 알려줘"]


def test_sync_telegram_setup_treats_connected_chat_as_allowed_even_if_not_in_allowlist(
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

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 14,
                    "message": {
                        "date": 1770000003,
                        "text": "/setup",
                        "chat": {"id": 77777, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        uclass_username="",
        uclass_password="",
        llm_provider="",
        llm_local_endpoint="",
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "- Telegram 채팅: 준비됨" in message
    assert "TELEGRAM_ALLOWED_CHAT_IDS에 등록되지 않았습니다" not in message


def test_sync_telegram_processes_commands_for_connected_chat_not_in_allowlist(
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

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 15,
                    "message": {
                        "date": 1770000004,
                        "text": "/today",
                        "chat": {"id": 77777, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert result["commands"]["failed"] == 0
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "77777"
    assert "[Sidae] 오늘 보기" in sent_messages[0][1]


def test_sync_telegram_connected_chat_stays_allowed_when_db_allow_preference_exists(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(chat_id="12345", telegram_chat_allowed=False)
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

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 16,
                    "message": {
                        "date": 1770000005,
                        "text": "/today",
                        "chat": {"id": 77777, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert result["commands"]["failed"] == 0
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "77777"
    assert "[Sidae] 오늘 보기" in sent_messages[0][1]


def test_sync_telegram_setup_command_warns_when_uos_portal_session_needs_reconnect(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={
            "portal_timetable_sync": {
                "status": "error",
                "auth_required": True,
                "reason": "UOS portal session expired; reconnect required",
            }
        },
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2026-03-11T09:10:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "error",
                "last_error": "UOS portal session expired; reconnect required",
                "action_required": 1,
            }
        },
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 13,
                    "message": {
                        "date": 1770000002,
                        "text": "/setup",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        uclass_username="",
        uclass_password="",
        llm_provider="",
        llm_local_endpoint="",
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    message = sent_messages[0][1]
    assert "- 시간표 소스: 확인 필요" in message
    assert "시간표 연결이 만료된 것 같습니다" in message
    assert "`/connect`로 다시 연결해 주세요." in message


def test_format_telegram_setup_hides_legacy_portal_session_in_official_api_mode(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="file",
        secret_ref="uclass.secret",
        status="active",
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2026-03-13T14:40:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2026-03-13T14:40:00+09:00",
            }
        },
        user_id=int(user["id"]),
    )

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
            uos_openapi_timetable_url="https://wise.uos.ac.kr/COM/ApiTimeTable/list.do",
            uos_openapi_timetable_api_key="test-key",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- 시간표 소스: 준비됨" in message
    assert "시간표 연결: 서울시립대 학교 공식 API 자동 동기화" in message
    assert "학교 공식 시간표 동기화 확인 시각이 오래됐습니다" not in message
    assert "연결된 시간표 세션:" not in message
    assert "서울시립대 포털 시간표 세션이 저장되어 있습니다." not in message


def test_format_telegram_setup_treats_official_api_success_as_ready_without_portal_session(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="inline",
        secret_ref="test-secret",
        status="active",
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2026-03-13T14:40:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
            }
        },
        user_id=int(user["id"]),
    )

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
            uos_openapi_timetable_url="https://wise.uos.ac.kr/COM/ApiTimeTable/list.do",
            uos_openapi_timetable_api_key="test-key",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- 시간표 소스: 준비됨" in message
    assert "시간표 연결: 서울시립대 학교 공식 API 자동 동기화" in message
    assert "학교 공식 시간표 동기화 기록이 아직 없습니다." not in message
    assert "학교 공식 시간표 동기화 확인 시각이 오래됐습니다" not in message


def test_format_telegram_setup_warns_when_portal_timetable_has_not_synced_yet(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        last_verified_at="2026-03-10T09:03:00+09:00",
        user_id=int(user["id"]),
    )

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- 시간표 소스: 확인 필요" in message
    assert "시간표 연결은 저장됐지만 아직 첫 확인 기록이 없습니다." in message


def test_format_telegram_setup_does_not_use_global_portal_success_for_unsynced_user(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        last_verified_at="2026-03-13T14:54:00+09:00",
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2026-03-13T14:40:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2026-03-13T14:40:00+09:00",
            }
        },
    )

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- 시간표 소스: 확인 필요" in message
    assert "시간표 연결은 저장됐지만 아직 첫 확인 기록이 없습니다." in message


def test_format_telegram_setup_warns_when_secure_storage_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="keychain",
        secret_ref="telegram:12345:moodle:uos_online_class",
        status="active",
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir="",
        secret_kind="keychain",
        secret_ref="telegram:12345:portal:uos_portal",
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    class MissingSecretStore:
        def read_secret(self, *, ref):
            raise SecretStoreError(
                "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
            )

    monkeypatch.setattr(pipeline, "default_secret_store", lambda settings=None: MissingSecretStore())

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- UClass 계정: 확인 필요" in message
    assert "- 시간표 소스: 확인 필요" in message
    assert "저장된 온라인강의실 연결을 다시 확인해야 합니다." in message
    assert "저장된 시간표 연결을 다시 확인해야 합니다." in message


def test_format_telegram_setup_ignores_stale_uclass_error_after_reconnect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="file",
        secret_ref="uclass.secret",
        status="active",
        last_verified_at="2099-03-13T06:31:47+00:00",
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"portal_timetable_sync": {"status": "success", "auth_required": False}},
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2026-03-13T06:28:28+00:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "error",
                "last_error": "UClass token missing from secure storage; reconnect required",
                "action_required": 1,
            }
        },
        user_id=int(user["id"]),
    )

    class FileSecretStore:
        def read_secret(self, *, ref):
            return "uos-issued-token"

    monkeypatch.setattr(pipeline, "default_secret_store", lambda settings=None: FileSecretStore())

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- 온라인강의실 연결: 준비됨" in message
    assert "- UClass 계정: 준비됨" in message
    assert "UClass 동기화가 최근 실패했습니다" not in message


def test_sync_telegram_today_uses_stored_portal_timetable_without_prime(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []
    today_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    db.upsert_event(
        external_id="portal:uos:timetable:stored-1",
        source="portal",
        start=today_local.replace(hour=13, minute=0).isoformat(),
        end=today_local.replace(hour=14, minute=15).isoformat(),
        title="자료구조",
        location="21-101",
        rrule=None,
        metadata_json={
            "school_slug": "uos_portal",
            "timetable_source": "uos_portal",
        },
        user_id=int(user["id"]),
    )

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("today should not prime portal")),
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 21,
                    "message": {
                        "date": 1770000000,
                        "text": "/today",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "수업: 1" in message
    assert "자료구조" in message


def test_sync_telegram_today_does_not_refresh_portal_timetable_when_today_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={
            "browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"},
            "portal_timetable_sync": {
                "status": "success",
                "auth_required": False,
                "event_count": 1,
                "last_synced_at": "2026-03-11T09:00:00+09:00",
            },
        },
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []
    today_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    tomorrow_local = today_local + timedelta(days=1)
    db.upsert_event(
        external_id="portal:uos:timetable:tomorrow-only",
        source="portal",
        start=tomorrow_local.replace(hour=9, minute=0).isoformat(),
        end=tomorrow_local.replace(hour=10, minute=15).isoformat(),
        title="오래된시간표",
        location="21-101",
        rrule=None,
        metadata_json={
            "school_slug": "uos_portal",
            "timetable_source": "uos_portal",
        },
        user_id=int(user["id"]),
    )

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("today should not refresh portal")),
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 22,
                    "message": {
                        "date": 1770000000,
                        "text": "/today",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "오늘은 등록된 일정, 수업, 마감 과제가 없습니다." in message


def test_format_telegram_today_cache_is_scoped_per_database(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    db_with_class = Database(tmp_path / "with_class.db")
    db_with_class.init()
    user_with_class = db_with_class.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    today_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    db_with_class.upsert_event(
        external_id="portal:uos:timetable:stored-1",
        source="portal",
        start=today_local.replace(hour=13, minute=0).isoformat(),
        end=today_local.replace(hour=14, minute=15).isoformat(),
        title="자료구조",
        location="21-101",
        rrule=None,
        metadata_json={
            "school_slug": "uos_portal",
            "timetable_source": "uos_portal",
        },
        user_id=int(user_with_class["id"]),
    )

    first_message = pipeline._format_telegram_today(
        settings,
        db_with_class,
        user_id=int(user_with_class["id"]),
    )

    db_without_class = Database(tmp_path / "without_class.db")
    db_without_class.init()
    user_without_class = db_without_class.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True, exist_ok=True)
    db_without_class.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={
            "browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"},
            "portal_timetable_sync": {
                "status": "success",
                "auth_required": False,
                "event_count": 1,
                "last_synced_at": "2026-03-11T09:00:00+09:00",
            },
        },
        user_id=int(user_without_class["id"]),
    )
    tomorrow_local = today_local + timedelta(days=1)
    db_without_class.upsert_event(
        external_id="portal:uos:timetable:tomorrow-only",
        source="portal",
        start=tomorrow_local.replace(hour=9, minute=0).isoformat(),
        end=tomorrow_local.replace(hour=10, minute=15).isoformat(),
        title="오래된시간표",
        location="21-101",
        rrule=None,
        metadata_json={
            "school_slug": "uos_portal",
            "timetable_source": "uos_portal",
        },
        user_id=int(user_without_class["id"]),
    )

    second_message = pipeline._format_telegram_today(
        settings,
        db_without_class,
        user_id=int(user_without_class["id"]),
    )

    assert "자료구조" in first_message
    assert "오늘은 등록된 일정, 수업, 마감 과제가 없습니다." in second_message
    assert "자료구조" not in second_message


def test_sync_telegram_today_does_not_append_portal_recheck_hint_on_read_path(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("today should not recheck portal")),
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 23,
                    "message": {
                        "date": 1770000000,
                        "text": "/today",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "학교 계정은 연결됐지만 아직 첫 동기화가 끝나지 않았습니다." in message
    assert "포털 시간표를 다시 확인했지만 오늘 수업을 찾지 못했습니다." not in message


def test_sync_telegram_today_does_not_touch_portal_secret_on_read_path(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir="",
        secret_kind="keychain",
        secret_ref="telegram:12345:portal:uos_portal",
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    class MissingSecretStore:
        def read_secret(self, *, ref):
            raise SecretStoreError(
                "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
            )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 24,
                    "message": {
                        "date": 1770000000,
                        "text": "/today",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "default_secret_store", lambda settings=None: MissingSecretStore())
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    message = sent_messages[0][1]
    assert "학교 계정은 연결됐지만 아직 첫 동기화가 끝나지 않았습니다." in message
    assert "저장된 포털 세션을 안전 저장소에서 읽지 못했습니다." not in message
    assert "`/connect`로 다시 연결해 주세요." not in message


def test_sync_telegram_today_does_not_append_portal_problem_even_when_tasks_exist(
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
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    today_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    db.upsert_task(
        external_id="uclass:task:test-1",
        source="uclass",
        due_at=today_local.replace(hour=23, minute=0).isoformat(),
        title="테스트 과제",
        status="open",
        metadata_json={"course_name": "자료구조"},
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("today should not inspect portal")),
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 25,
                    "message": {
                        "date": 1770000000,
                        "text": "/today",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    message = sent_messages[0][1]
    assert "마감 과제: 1" in message
    assert "테스트 과제" in message
    assert "포털 세션이 만료된 것으로 보입니다." not in message
    assert "`/connect`로 다시 연결해 주세요." not in message


def test_sync_telegram_today_omits_portal_reconnect_hint_when_non_portal_timetable_exists(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    today_local = datetime.now(ZoneInfo("Asia/Seoul")).replace(second=0, microsecond=0)
    legacy_start = (today_local - timedelta(days=1)).replace(hour=9, minute=0)
    db.upsert_event(
        external_id="portal:legacy-1",
        source="portal",
        start=legacy_start.isoformat(),
        end=(legacy_start + timedelta(hours=1, minutes=15)).isoformat(),
        title="중국어1",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
        user_id=int(user["id"]),
    )
    db.upsert_task(
        external_id="uclass:task:test-2",
        source="uclass",
        due_at=today_local.replace(hour=23, minute=0).isoformat(),
        title="테스트 과제",
        status="open",
        metadata_json={"course_name": "자료구조"},
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_portal_timetable",
        lambda **kwargs: {
            "ok": False,
            "events": [],
            "current_url": "https://sso.uos.ac.kr/svc/tk/Auth.eps",
            "title": "University of Seoul portal system",
            "table_count": 0,
            "auth_required": True,
            "network_samples": [],
        },
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 26,
                    "message": {
                        "date": 1770000001,
                        "text": "/today",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    message = sent_messages[0][1]
    assert "마감 과제: 1" in message
    assert "테스트 과제" in message
    assert "포털 세션이 만료된 것으로 보입니다." not in message
    assert "`/connect`로 다시 연결해 주세요." not in message


def test_sync_telegram_todaysummary_command_returns_class_briefs(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    db.upsert_event(
        external_id="portal:alg-1",
        source="portal",
        start=now_local.replace(hour=9, minute=0).isoformat(),
        end=(now_local.replace(hour=9, minute=0) + timedelta(hours=1, minutes=15)).isoformat(),
        title="계량경제학",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-brief",
        source="uclass",
        filename="chapter01.pdf",
        icloud_path=None,
        content_hash="hash-1",
        metadata_json={
            "course_name": "계량경제학",
            "brief": {
                "bullets": [
                    "확률변수 기초와 분포 개념을 정리해.",
                    "시험에 자주 나오는 기댓값과 분산 공식을 구분해.",
                ],
                "question": "기댓값 선형성과 분산 계산을 예제로 다시 풀어봐.",
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-login",
        source="uclass",
        filename="view.php",
        icloud_path=None,
        content_hash="hash-login",
        metadata_json={
            "course_name": "계량경제학",
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
    db.record_artifact(
        external_id="uclass:artifact:alg-empty-stub",
        source="uclass",
        filename="view.php",
        icloud_path=None,
        content_hash="hash-empty",
        metadata_json={
            "course_name": "계량경제학",
            "content_type": "text/html; charset=utf-8",
            "module_name": "강의 참고 자료",
            "section_name": "2주차",
        },
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 11,
                    "message": {
                        "date": 1770000000,
                        "text": "/todaysummary",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 오늘 수업 자료 요약" in message
    assert "계량경제학" in message
    assert "확률변수 기초와 분포 개념을 정리해." in message
    assert "기댓값 선형성과 분산 계산을 예제로 다시 풀어봐." in message
    assert "chapter01.pdf" in message
    assert "view.php" not in message
    assert "로그인 페이지" not in message


def test_format_telegram_today_summary_prefers_matching_week_material(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    first_week_start = (now_local - timedelta(days=7)).replace(hour=9, minute=0)
    db.upsert_event(
        external_id="portal:alg-weekly-1",
        source="portal",
        start=first_week_start.isoformat(),
        end=(first_week_start + timedelta(hours=1, minutes=15)).isoformat(),
        title="알고리즘",
        location="21-101",
        rrule="FREQ=WEEKLY",
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-week1",
        source="uclass",
        filename="alg-week1.pdf",
        icloud_path=None,
        content_hash="hash-week1",
        metadata_json={
            "course_name": "알고리즘",
            "module_name": "1주차 수업자료",
            "brief": {
                "bullets": ["1주차 내용 요약"],
                "question": "1주차 핵심 개념을 복습해.",
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-week2",
        source="uclass",
        filename="alg-week2.pdf",
        icloud_path=None,
        content_hash="hash-week2",
        metadata_json={
            "course_name": "알고리즘",
            "module_name": "2주차 수업자료",
            "brief": {
                "bullets": ["2주차 그래프 탐색 핵심을 정리해."],
                "question": "2주차 예제를 다시 손으로 풀어봐.",
            },
        },
    )

    message = pipeline._format_telegram_today_summary(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
    )

    assert "2주차 그래프 탐색 핵심을 정리해." in message
    assert "2주차 예제를 다시 손으로 풀어봐." in message
    assert "1주차 내용 요약" not in message


def test_collect_class_occurrences_uses_uclass_course_startdate_for_week_index(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    class_start = datetime.now(tz).replace(second=0, microsecond=0, hour=18, minute=0)
    course_start = class_start.replace(hour=0, minute=0) - timedelta(days=14)
    db.upsert_course(
        canonical_course_id="uclass:uclass-uos-ac-kr:3821",
        source="uclass",
        external_course_id="3821",
        display_name="대학글쓰기",
        metadata_json={
            "fullname": "대학글쓰기",
            "startdate": int(course_start.astimezone(timezone.utc).timestamp()),
        },
    )
    db.upsert_event(
        external_id="portal:writing-weekly-1",
        source="portal",
        start=class_start.isoformat(),
        end=(class_start + timedelta(hours=2, minutes=50)).isoformat(),
        title="대학글쓰기",
        location="20-116",
        rrule="FREQ=WEEKLY",
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-uos-ac-kr:3821"}
        ),
    )

    class_items = pipeline._collect_class_occurrences(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        target_day_local=class_start,
        max_items=10,
    )

    assert len(class_items) == 1
    assert class_items[0]["occurrence_week_index"] == 3


def test_format_telegram_today_summary_uses_uclass_course_startdate_for_week_matching(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    class_start = now_local.replace(hour=18, minute=0)
    course_start = class_start.replace(hour=0, minute=0) - timedelta(days=14)
    db.upsert_course(
        canonical_course_id="uclass:uclass-uos-ac-kr:3821",
        source="uclass",
        external_course_id="3821",
        display_name="대학글쓰기",
        metadata_json={
            "fullname": "대학글쓰기",
            "startdate": int(course_start.astimezone(timezone.utc).timestamp()),
        },
    )
    db.upsert_event(
        external_id="portal:writing-weekly-summary-1",
        source="portal",
        start=class_start.isoformat(),
        end=(class_start + timedelta(hours=2, minutes=50)).isoformat(),
        title="대학글쓰기",
        location="20-116",
        rrule="FREQ=WEEKLY",
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-uos-ac-kr:3821"}
        ),
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-week1",
        source="uclass",
        filename="writing-week1.pdf",
        icloud_path=None,
        content_hash="hash-writing-week1",
        metadata_json={
            "course_name": "대학글쓰기",
            "module_name": "1주차 강의계획 안내",
            "brief": {
                "bullets": ["1주차 강의계획서 요약"],
                "question": "1주차 공지사항을 다시 확인해.",
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-week3",
        source="uclass",
        filename="writing-week3.pdf",
        icloud_path=None,
        content_hash="hash-writing-week3",
        metadata_json={
            "course_name": "대학글쓰기",
            "module_name": "3주차 자기소개서 수업자료",
            "brief": {
                "bullets": ["3주차 자기소개서 작성 포인트를 정리해."],
                "question": "자기소개서 초안을 직접 써봐.",
            },
        },
    )

    message = pipeline._format_telegram_today_summary(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
    )

    assert "3주차 자기소개서 작성 포인트를 정리해." in message
    assert "자기소개서 초안을 직접 써봐." in message
    assert "1주차 강의계획서 요약" not in message


def test_format_telegram_today_aggregates_multiple_matching_materials(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    first_week_start = (now_local - timedelta(days=14)).replace(hour=18, minute=0)
    db.upsert_event(
        external_id="portal:writing-aggregate-1",
        source="portal",
        start=first_week_start.isoformat(),
        end=(first_week_start + timedelta(hours=2, minutes=50)).isoformat(),
        title="대학글쓰기",
        location="20-116",
        rrule="FREQ=WEEKLY",
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-spelling",
        source="uclass",
        filename="week3-spelling.pptx",
        icloud_path=None,
        content_hash="hash-writing-spelling",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "연음법칙 및 용언의 활용",
            "brief": {
                "bullets": ["음절 구조와 연음 규칙을 정리해."],
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-selfintro",
        source="uclass",
        filename="week3-selfintro.hwp",
        icloud_path=None,
        content_hash="hash-writing-selfintro",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "자기소개서 수업자료",
            "brief": {
                "bullets": ["자기소개서 4개 소제목 구성을 익혀."],
            },
        },
    )

    message = pipeline._format_telegram_today(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
    )

    assert "준비:" in message
    assert "음절 구조와 연음 규칙을 정리해." in message
    assert "자기소개서 4개 소제목 구성을 익혀." in message


def test_format_telegram_today_summary_aggregates_multiple_matching_materials(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    first_week_start = (now_local - timedelta(days=14)).replace(hour=18, minute=0)
    db.upsert_event(
        external_id="portal:writing-aggregate-summary-1",
        source="portal",
        start=first_week_start.isoformat(),
        end=(first_week_start + timedelta(hours=2, minutes=50)).isoformat(),
        title="대학글쓰기",
        location="20-116",
        rrule="FREQ=WEEKLY",
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-spelling-summary",
        source="uclass",
        filename="week3-spelling.pptx",
        icloud_path=None,
        content_hash="hash-writing-spelling-summary",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "연음법칙 및 용언의 활용",
            "brief": {
                "bullets": ["음절 구조와 연음 규칙을 정리해."],
                "question": "연음 규칙 예시를 직접 써봐.",
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-selfintro-summary",
        source="uclass",
        filename="week3-selfintro.hwp",
        icloud_path=None,
        content_hash="hash-writing-selfintro-summary",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "자기소개서 수업자료",
            "brief": {
                "bullets": ["자기소개서 4개 소제목 구성을 익혀."],
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-selfintro-summary-dup",
        source="uclass",
        filename="week3-selfintro.hwp",
        icloud_path=None,
        content_hash="hash-writing-selfintro-summary-dup",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "자기소개서 수업자료",
            "brief": {
                "bullets": ["자기소개서 4개 소제목 구성을 익혀."],
            },
        },
    )

    message = pipeline._format_telegram_today_summary(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
    )

    assert "음절 구조와 연음 규칙을 정리해." in message
    assert message.count("자기소개서 4개 소제목 구성을 익혀.") == 1
    assert "자료:" in message
    assert "week3-spelling.pptx" in message
    assert message.count("week3-selfintro.hwp") == 1
    assert "복습: 연음 규칙 예시를 직접 써봐." in message


def test_sync_telegram_tomorrow_command_returns_schedule_and_deadlines(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    tz = ZoneInfo("Asia/Seoul")
    tomorrow_local = (datetime.now(tz) + timedelta(days=1)).replace(second=0, microsecond=0)
    db.upsert_event(
        external_id="portal:tomorrow-class-1",
        source="portal",
        start=tomorrow_local.replace(hour=10, minute=0).isoformat(),
        end=(tomorrow_local.replace(hour=10, minute=0) + timedelta(hours=1, minutes=15)).isoformat(),
        title="대학글쓰기",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.upsert_task(
        external_id="uclass:task:tomorrow-1",
        source="uclass",
        due_at=tomorrow_local.replace(hour=23, minute=59).isoformat(),
        title="글쓰기 초안 제출",
        status="open",
        metadata_json={"course_name": "대학글쓰기"},
    )
    db.record_artifact(
        external_id="uclass:artifact:tomorrow-brief",
        source="uclass",
        filename="writing.pdf",
        icloud_path=None,
        content_hash="hash-tomorrow",
        metadata_json={
            "course_name": "대학글쓰기",
            "brief": {
                "bullets": [
                    "논지와 근거 문장을 분리해 구조를 먼저 세워.",
                ],
            },
        },
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 210,
                    "message": {
                        "date": 1770000000,
                        "text": "/tomorrow",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 내일 보기" in message
    assert "수업: 1" in message
    assert "대학글쓰기" in message
    assert "마감 과제: 1" in message
    assert "글쓰기 초안 제출 [대학글쓰기]" in message
    assert "자세한 수업 자료 요약: /tomorrowsummary" in message


def test_sync_telegram_tomorrowsummary_command_returns_class_briefs(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    tz = ZoneInfo("Asia/Seoul")
    tomorrow_local = (datetime.now(tz) + timedelta(days=1)).replace(second=0, microsecond=0)
    db.upsert_event(
        external_id="portal:tomorrow-summary-1",
        source="portal",
        start=tomorrow_local.replace(hour=13, minute=0).isoformat(),
        end=(tomorrow_local.replace(hour=13, minute=0) + timedelta(hours=1, minutes=15)).isoformat(),
        title="인문사회계를위한코딩기초",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:tomorrow-summary-brief",
        source="uclass",
        filename="coding.pdf",
        icloud_path=None,
        content_hash="hash-2",
        metadata_json={
            "course_name": "인문사회계를위한코딩기초",
            "brief": {
                "bullets": [
                    "조건문과 반복문 예제를 직접 손으로 다시 써봐.",
                ],
                "question": "반복문 종료 조건이 왜 중요한지 설명해봐.",
            },
        },
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 211,
                    "message": {
                        "date": 1770000000,
                        "text": "/tomorrowsummary",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 내일 수업 자료 요약" in message
    assert "인문사회계를위한코딩기초" in message
    assert "조건문과 반복문 예제를 직접 손으로 다시 써봐." in message
    assert "반복문 종료 조건이 왜 중요한지 설명해봐." in message
    assert "coding.pdf" in message


def test_sync_telegram_todayweather_command_returns_weather_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    db.update_sync_state(
        "sync_weather",
        last_run_at="2026-03-09T06:00:00+00:00",
        last_cursor_json={
            "generated_at": "2026-03-09T15:00:00+09:00",
            "location_label": "서울시립대",
            "current": {
                "temperature_c": 7.6,
                "condition_text": "맑음",
            },
            "air_quality": {
                "ok": True,
                "provider": "seoul_openapi",
                "measured_at": "2026-03-09T15:00:00+09:00",
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
                "date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
                "temperature_min_c": 2.0,
                "temperature_max_c": 13.0,
                "diurnal_range_c": 11.0,
                "diurnal_range_alert": True,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 2.0,
                    "temperature_max_c": 8.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 10,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 9.0,
                    "temperature_max_c": 13.0,
                    "condition_text": "구름많음",
                    "precip_probability_max": 20,
                },
            },
            "tomorrow": {
                "date": (datetime.now(ZoneInfo("Asia/Seoul")).date() + timedelta(days=1)).isoformat(),
                "temperature_min_c": 4.0,
                "temperature_max_c": 11.0,
                "diurnal_range_c": 7.0,
                "diurnal_range_alert": False,
            },
        },
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 111,
                    "message": {
                        "date": 1770000000,
                        "text": "/weather",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert message.startswith("오늘 날씨 (")
    assert "오늘 날씨 (15:00 기준)" in message
    assert "현재 7.6C / 맑음" in message
    assert "오전 : 2~8C, 맑음, 강수확률 10%" in message
    assert "오후 : 9~13C, 구름많음, 강수확률 20%" in message
    assert "\n미세먼지 (15:00 기준)\n" in message
    assert "동대문구 나쁨" in message
    assert "도봉구 나쁨" in message
    assert "\n내일 날씨\n" in message
    assert "최저 4C / 최고 11C" in message
    assert "갱신:" not in message


def test_format_telegram_tomorrowweather_focuses_on_forecast_snapshot(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.update_sync_state(
        "sync_weather",
        last_run_at="2026-03-09T06:00:00+00:00",
        last_cursor_json={
            "generated_at": "2026-03-09T15:00:00+09:00",
            "location_label": "서울시립대",
            "current": {
                "temperature_c": 7.6,
                "condition_text": "맑음",
            },
            "tomorrow": {
                "date": (datetime.now(ZoneInfo("Asia/Seoul")).date() + timedelta(days=1)).isoformat(),
                "temperature_min_c": 4.0,
                "temperature_max_c": 11.0,
                "diurnal_range_c": 7.0,
                "diurnal_range_alert": False,
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 4.0,
                    "temperature_max_c": 7.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 10,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 8.0,
                    "temperature_max_c": 11.0,
                    "condition_text": "구름많음",
                    "precip_probability_max": 20,
                },
            },
        },
    )
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        weather_location_label="서울특별시",
        weather_lat=37.5665,
        weather_lon=126.9780,
    )

    message = pipeline._format_telegram_tomorrowweather(settings, db)

    assert message.startswith("내일 날씨 (")
    assert "- 지역 서울시립대" in message
    assert "최저 4C / 최고 11C" in message
    assert "강수확률 20%" in message
    assert "오전 : 4~7C, 맑음, 강수확률 10%" in message
    assert "오후 : 8~11C, 구름많음, 강수확률 20%" in message
    assert "현재 7.6C / 맑음" not in message
    assert "미세먼지" not in message


def test_sync_telegram_region_command_stores_user_weather_location(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            if isinstance(offset, int) and offset > 501:
                return []
            return [
                {
                    "update_id": 501,
                    "message": {
                        "date": 1770000000,
                        "text": "/region 동대문구",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    monkeypatch.setattr(
        pipeline,
        "resolve_weather_location_query",
        lambda query: {
            "label": "동대문구",
            "lat": 37.5744,
            "lon": 127.0396,
            "air_quality_district_code": "111152",
            "source": "catalog",
        },
    )
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)
    preferences = db.get_user_preferences(chat_id="12345")

    assert result["commands"]["processed"] == 1
    assert preferences is not None
    assert preferences["weather_location_label"] == "동대문구"
    assert preferences["weather_lat"] == 37.5744
    assert preferences["weather_lon"] == 127.0396
    assert preferences["weather_air_quality_district_code"] == "111152"
    assert sent_messages[0][0] == "12345"
    assert "저장됨: 동대문구" in sent_messages[0][1]
    assert "/region reset" in sent_messages[0][1]


def test_sync_telegram_weather_command_uses_user_specific_regions_for_two_users(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(
        chat_id="111",
        weather_location_label="동대문구",
        weather_lat=37.5744,
        weather_lon=127.0396,
    )
    db.upsert_user_preferences(
        chat_id="222",
        weather_location_label="도봉구",
        weather_lat=37.6688,
        weather_lon=127.0471,
    )
    sent_messages: list[tuple[str, str]] = []
    weather_calls: list[tuple[str, float, float]] = []

    def _snapshot(label: str, temperature: float, condition: str) -> dict[str, object]:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        return {
            "generated_at": "2026-03-09T15:00:00+09:00",
            "location_label": label,
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

    class FakeWeatherClient:
        def __init__(self, auth_key=None):
            self.auth_key = auth_key

        def fetch_snapshot(self, *, lat, lon, location_label, timezone_name, now_local=None):
            weather_calls.append((str(location_label), float(lat), float(lon)))
            if str(location_label) == "동대문구":
                return _snapshot("동대문구", 7.0, "맑음")
            return _snapshot("도봉구", 3.0, "흐림")

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            if isinstance(offset, int) and offset > 602:
                return []
            return [
                {
                    "update_id": 601,
                    "message": {
                        "date": 1770000000,
                        "text": "/weather",
                        "chat": {"id": 111, "type": "private"},
                        "from": {"id": 999},
                    },
                },
                {
                    "update_id": 602,
                    "message": {
                        "date": 1770000001,
                        "text": "/weather",
                        "chat": {"id": 222, "type": "private"},
                        "from": {"id": 1000},
                    },
                },
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "KMAWeatherClient", FakeWeatherClient)
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["111", "222"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        weather_enabled=True,
        air_quality_enabled=False,
        weather_location_label="서울특별시",
        weather_lat=37.5665,
        weather_lon=126.9780,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)
    messages = {chat_id: text for chat_id, text in sent_messages}

    assert result["commands"]["processed"] == 2
    assert len(weather_calls) == 2
    assert "- 지역 동대문구" in messages["111"]
    assert "현재 7C / 맑음" in messages["111"]
    assert "- 지역 도봉구" in messages["222"]
    assert "현재 3C / 흐림" in messages["222"]


def test_sync_weather_reuses_shared_user_region_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    first = db.upsert_user_preferences(
        chat_id="111",
        weather_location_label="동대문구",
        weather_lat=37.5744,
        weather_lon=127.0396,
    )
    second = db.upsert_user_preferences(
        chat_id="222",
        weather_location_label="동대문구",
        weather_lat=37.5744,
        weather_lon=127.0396,
    )
    fetch_calls: list[tuple[str, float, float]] = []

    class FakeWeatherClient:
        def __init__(self, auth_key=None):
            self.auth_key = auth_key

        def fetch_snapshot(self, *, lat, lon, location_label, timezone_name, now_local=None):
            fetch_calls.append((str(location_label), float(lat), float(lon)))
            today = datetime.now(ZoneInfo("Asia/Seoul")).date()
            return {
                "generated_at": "2026-03-09T15:00:00+09:00",
                "location_label": location_label,
                "current": {"temperature_c": 6.0, "condition_text": "맑음"},
                "today": {
                    "date": today.isoformat(),
                    "morning": {
                        "label": "오전",
                        "temperature_min_c": 4.0,
                        "temperature_max_c": 6.0,
                        "condition_text": "맑음",
                        "precip_probability_max": 10,
                    },
                    "afternoon": {
                        "label": "오후",
                        "temperature_min_c": 7.0,
                        "temperature_max_c": 9.0,
                        "condition_text": "구름많음",
                        "precip_probability_max": 20,
                    },
                },
                "tomorrow": {
                    "date": (today + timedelta(days=1)).isoformat(),
                    "temperature_min_c": 5.0,
                    "temperature_max_c": 10.0,
                },
            }

    monkeypatch.setattr(pipeline, "KMAWeatherClient", FakeWeatherClient)
    settings = SimpleNamespace(
        weather_enabled=True,
        weather_location_label="서울특별시",
        weather_lat=37.5665,
        weather_lon=126.9780,
        weather_kma_auth_key=None,
        air_quality_enabled=False,
        timezone="Asia/Seoul",
    )

    result = pipeline.sync_weather(settings=settings, db=db)

    assert result["ok"] is True
    assert result["user_target_count"] == 1
    assert result["warmed_user_targets"] == 1
    assert len(fetch_calls) == 2
    assert db.latest_weather_snapshot(user_id=first["user_id"]) is not None
    assert db.latest_weather_snapshot(user_id=second["user_id"]) is not None


def test_sync_weather_failure_keeps_last_successful_snapshot_for_weather_command(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.update_sync_state(
        "sync_weather",
        last_run_at="2026-03-09T06:00:00+00:00",
        last_cursor_json={
            "generated_at": "2026-03-09T15:00:00+09:00",
            "location_label": "서울시립대",
            "current": {
                "temperature_c": 7.6,
                "condition_text": "맑음",
            },
            "today": {
                "date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
                "morning": {
                    "label": "오전",
                    "temperature_min_c": 2.0,
                    "temperature_max_c": 8.0,
                    "condition_text": "맑음",
                    "precip_probability_max": 10,
                },
                "afternoon": {
                    "label": "오후",
                    "temperature_min_c": 9.0,
                    "temperature_max_c": 13.0,
                    "condition_text": "구름많음",
                    "precip_probability_max": 20,
                },
            },
            "tomorrow": {
                "date": (datetime.now(ZoneInfo("Asia/Seoul")).date() + timedelta(days=1)).isoformat(),
                "temperature_min_c": 4.0,
                "temperature_max_c": 11.0,
            },
        },
    )

    class FailingWeatherClient:
        def __init__(self, auth_key=None):
            self.auth_key = auth_key

        def fetch_snapshot(self, **kwargs):
            raise RuntimeError("weather timeout")

    monkeypatch.setattr(pipeline, "KMAWeatherClient", FailingWeatherClient)
    settings = SimpleNamespace(
        weather_enabled=True,
        weather_location_label="서울특별시",
        weather_lat=37.583801,
        weather_lon=127.058701,
        weather_kma_auth_key=None,
        air_quality_enabled=False,
        timezone="Asia/Seoul",
    )

    result = pipeline.sync_weather(settings=settings, db=db)

    assert result["ok"] is False
    snapshot = db.latest_weather_snapshot()
    assert snapshot is not None
    assert snapshot["current"]["condition_text"] == "맑음"
    assert snapshot["error"] == "weather timeout"
    message = pipeline._format_telegram_todayweather(settings, db)
    assert "저장된 날씨 스냅샷이 없습니다." not in message
    assert "오늘 날씨 (15:00 기준)" in message
    assert "현재 7.6C / 맑음" in message


def test_sync_telegram_notice_general_command_returns_titles(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 12,
                    "message": {
                        "date": 1770000000,
                        "text": "/notice_general",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    fetched_at = "2026-03-09T00:00:00Z"
    monkeypatch.setattr(
        pipeline,
        "fetch_uos_notice_feed",
        lambda list_id, menuid, limit=10: portal_connector.PortalNoticeFetchResult(
            notices=[
                portal_connector.PortalNotice(
                    seq="30524",
                    title="2026년 대학생 청소년교육지원장학금 활동도우미(근로) 모집",
                    posted_on="2026-03-09",
                    department="대외협력과",
                    list_id=str(list_id),
                    menuid=str(menuid),
                ),
                portal_connector.PortalNotice(
                    seq="30525",
                    title="[인터넷증명발급] (신)인터넷 증명발급 시스템 오픈 및 원본대조 서비스 이용 안내",
                    posted_on="2026-03-08",
                    department="전산정보과",
                    list_id=str(list_id),
                    menuid=str(menuid),
                ),
            ],
            metadata=portal_connector.PortalNoticeFetchMetadata(
                list_id=str(list_id),
                menuid=str(menuid),
                requested_limit=limit,
                requested_at=fetched_at,
                fetched_at=fetched_at,
                source_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
                resolved_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
                http_status=200,
                page_title="일반공지 < UOS공지",
                parsed_count=2,
            ),
        ),
    )
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 학교 일반공지" in message
    assert "최근 공지 10개" in message
    assert "- 2026-03-09 | 2026년 대학생 청소년교육지원장학금 활동도우미(근로) 모집" in message
    assert "- 2026-03-08 | [인터넷증명발급] (신)인터넷 증명발급 시스템 오픈 및 원본대조 서비스 이용 안내" in message
    assert "- 출처: 학교 포털 (2026-03-09 09:00 KST)" in message
    snapshot_state = db.get_sync_state("uos_notice_snapshot_general")
    snapshot = json.loads(snapshot_state.last_cursor_json)
    assert snapshot["snapshot"]["notice_count"] == 2
    assert snapshot["last_attempt"]["ok"] is True
    assert snapshot["source"]["list_url"] == "https://www.uos.ac.kr/korNotice/list.do?list_id=FA1"


def test_format_telegram_uos_notice_marks_new_items_after_first_view(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    notice_batches = [
        [
            portal_connector.PortalNotice(
                title="기존 공지 A",
                posted_on="2026-03-09",
                seq="100",
                department=None,
                list_id="FA1",
                menuid="2000005009002000000",
            ),
            portal_connector.PortalNotice(
                title="기존 공지 B",
                posted_on="2026-03-08",
                seq="099",
                department=None,
                list_id="FA1",
                menuid="2000005009002000000",
            ),
        ],
        [
            portal_connector.PortalNotice(
                title="새 공지",
                posted_on="2026-03-10",
                seq="101",
                department=None,
                list_id="FA1",
                menuid="2000005009002000000",
            ),
            portal_connector.PortalNotice(
                title="기존 공지 A",
                posted_on="2026-03-09",
                seq="100",
                department=None,
                list_id="FA1",
                menuid="2000005009002000000",
            ),
            portal_connector.PortalNotice(
                title="기존 공지 B",
                posted_on="2026-03-08",
                seq="099",
                department=None,
                list_id="FA1",
                menuid="2000005009002000000",
            ),
        ],
    ]

    def _fake_fetch(list_id, menuid, limit=10):
        notices = notice_batches.pop(0)
        fetched_at = "2026-03-09T00:00:00Z" if notices[0].seq == "100" else "2026-03-10T00:00:00Z"
        return portal_connector.PortalNoticeFetchResult(
            notices=notices,
            metadata=portal_connector.PortalNoticeFetchMetadata(
                list_id=str(list_id),
                menuid=str(menuid),
                requested_limit=limit,
                requested_at=fetched_at,
                fetched_at=fetched_at,
                source_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
                resolved_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
                http_status=200,
                parsed_count=len(notices),
            ),
        )

    monkeypatch.setattr(pipeline, "fetch_uos_notice_feed", _fake_fetch)

    first = pipeline._format_telegram_uos_notice(
        db,
        "general",
        timezone_name="Asia/Seoul",
        user_id=int(user["id"]),
    )
    second = pipeline._format_telegram_uos_notice(
        db,
        "general",
        timezone_name="Asia/Seoul",
        user_id=int(user["id"]),
    )

    assert "[NEW]" not in first
    assert "- 새 공지: 1건" in second
    assert "- [NEW] 2026-03-10 | 새 공지" in second


def test_format_telegram_uos_notice_handles_empty_snapshot(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    monkeypatch.setattr(
        pipeline,
        "fetch_uos_notice_feed",
        lambda list_id, menuid, limit=10: portal_connector.PortalNoticeFetchResult(
            notices=[],
            metadata=portal_connector.PortalNoticeFetchMetadata(
                list_id=str(list_id),
                menuid=str(menuid),
                requested_limit=limit,
                requested_at="2026-03-09T00:00:00Z",
                fetched_at="2026-03-09T00:00:00Z",
                source_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA2",
                resolved_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA2",
                http_status=200,
                page_title="학사공지 < UOS공지",
                parsed_count=0,
                empty_detected=True,
            ),
        ),
    )

    message = pipeline._format_telegram_uos_notice(
        db,
        "academic",
        timezone_name="Asia/Seoul",
    )

    assert "[Sidae] 학교 학사공지" in message
    assert "최근 공지 10개" in message
    assert "- 표시할 공지가 없습니다." in message
    assert "- 출처: 학교 포털 (2026-03-09 09:00 KST)" in message
    snapshot_state = db.get_sync_state("uos_notice_snapshot_academic")
    snapshot = json.loads(snapshot_state.last_cursor_json)
    assert snapshot["snapshot"]["empty"] is True
    assert snapshot["snapshot"]["notice_count"] == 0
    assert snapshot["last_attempt"]["ok"] is True


def test_format_telegram_uos_notice_uses_cached_titles_on_upstream_failure(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    cached_notices = [
        portal_connector.PortalNotice(
            title="기존 공지 A",
            posted_on="2026-03-09",
            seq="100",
            department=None,
            list_id="FA1",
            menuid="2000005009002000000",
        ),
        portal_connector.PortalNotice(
            title="기존 공지 B",
            posted_on="2026-03-08",
            seq="099",
            department=None,
            list_id="FA1",
            menuid="2000005009002000000",
        ),
    ]
    responses: list[object] = [
        portal_connector.PortalNoticeFetchResult(
            notices=cached_notices,
            metadata=portal_connector.PortalNoticeFetchMetadata(
                list_id="FA1",
                menuid="2000005009002000000",
                requested_limit=10,
                requested_at="2026-03-09T00:00:00Z",
                fetched_at="2026-03-09T00:00:00Z",
                source_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
                resolved_url="https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
                http_status=200,
                parsed_count=2,
            ),
        ),
        portal_connector.PortalNoticeFetchError(
            "portal timeout",
            metadata={
                "list_id": "FA1",
                "menuid": "2000005009002000000",
                "requested_limit": 10,
                "requested_at": "2026-03-10T00:00:00Z",
                "source_url": "https://www.uos.ac.kr/korNotice/list.do?list_id=FA1",
            },
        ),
    ]

    def _fake_fetch(list_id, menuid, limit=10):
        current = responses.pop(0)
        if isinstance(current, Exception):
            raise current
        return current

    monkeypatch.setattr(pipeline, "fetch_uos_notice_feed", _fake_fetch)

    first = pipeline._format_telegram_uos_notice(
        db,
        "general",
        timezone_name="Asia/Seoul",
        user_id=int(user["id"]),
    )
    second = pipeline._format_telegram_uos_notice(
        db,
        "general",
        timezone_name="Asia/Seoul",
        user_id=int(user["id"]),
    )

    assert "- 2026-03-09 | 기존 공지 A" in first
    assert "- 2026-03-09 | 기존 공지 A" in second
    assert "상태" in second
    assert "저장된 최근 공지를 보여줍니다: portal timeout" in second
    assert "- 마지막 정상 반영: 2026-03-09 09:00 KST" in second
    assert "- 출처: 학교 포털 캐시 (2026-03-09 09:00 KST)" in second
    snapshot_state = db.get_sync_state("uos_notice_snapshot_general")
    snapshot = json.loads(snapshot_state.last_cursor_json)
    assert snapshot["snapshot"]["notice_count"] == 2
    assert snapshot["last_attempt"]["ok"] is False
    assert snapshot["last_attempt"]["error"] == "portal timeout"


def test_sync_telegram_notice_uclass_command_returns_recent_notifications(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []
    db.upsert_notification(
        external_id="uclass:notif:1",
        source="uclass",
        created_at="2026-03-09T16:20:00+09:00",
        title="계량경제학 과제 공지",
        body="과제 공지가 올라왔습니다.",
        url=None,
        metadata_json={},
    )
    db.upsert_notification(
        external_id="uclass:notif:2",
        source="uclass",
        created_at="2026-03-09T15:00:00+09:00",
        title="대학글쓰기 자료 업로드",
        body="자료가 업로드되었습니다.",
        url=None,
        metadata_json={},
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2026-03-09T16:25:00+09:00",
        last_cursor_json={"notifications": 2},
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 212,
                    "message": {
                        "date": 1770000000,
                        "text": "/notice_uclass",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 온라인강의실 알림" in message
    assert "알림: 2" in message
    assert "- 03-09 16:20 | 계량경제학 과제 공지" in message
    assert "- 03-09 15:00 | 대학글쓰기 자료 업로드" in message
    assert "- 출처: UClass (2026-03-09 16:25 KST)" in message


def test_format_telegram_uclass_notice_shows_reconnect_guidance_on_auth_error(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2026-03-09T16:25:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "error",
                "last_error": "uclass auth unavailable",
                "action_required": 1,
            }
        },
        user_id=int(user["id"]),
    )

    message = pipeline._format_telegram_uclass_notice(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        user_id=int(user["id"]),
    )

    assert "표시할 알림이 없습니다." in message
    assert "최근 UClass 동기화가 실패했습니다: uclass auth unavailable" in message
    assert "`/connect`로 학교 계정을 다시 연결해 주세요." in message


def test_format_telegram_today_uses_human_friendly_sections(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    class_start = now_local.replace(hour=9, minute=0)
    class_end = class_start + timedelta(hours=1, minutes=15)
    db.upsert_event(
        external_id="portal:alg-1",
        source="portal",
        start=class_start.isoformat(),
        end=class_end.isoformat(),
        title="계량경제학",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.upsert_task(
        external_id="uclass:task:alg-1",
        source="uclass",
        due_at=now_local.replace(hour=23, minute=59).isoformat(),
        title="과제 1 제출",
        status="open",
        metadata_json={
            "course_name": "계량경제학",
            "summary": "강의 노트 기준으로 풀이를 정리해 제출",
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:alg-brief",
        source="uclass",
        filename="chapter01.pdf",
        icloud_path=None,
        content_hash="hash-1",
        metadata_json={
            "course_name": "계량경제학",
            "brief": {
                "bullets": [
                    "확률변수 기초와 분포 개념을 정리해.",
                ],
            },
            "source_kind": "attachment",
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
    )

    message = pipeline._format_telegram_today(settings=settings, db=db)

    assert "[Sidae] 오늘 보기" in message
    assert "events=" not in message
    assert "tasks_due=" not in message
    assert "일정:" not in message
    assert "수업: 1" in message
    assert "계량경제학" in message
    assert "준비: 확률변수 기초와 분포 개념을 정리해." in message
    assert "마감 과제: 1" in message
    assert "과제 1 제출 [계량경제학]" in message
    assert "자세한 수업 자료 요약: /todaysummary" in message


def test_format_telegram_today_includes_class_specific_notices_and_tasks(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    class_start = now_local.replace(hour=11, minute=0)
    db.upsert_event(
        external_id="portal:stats-1",
        source="portal",
        start=class_start.isoformat(),
        end=(class_start + timedelta(hours=1, minutes=15)).isoformat(),
        title="통계학개론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.upsert_notification(
        external_id="uclass:notif:stats-1",
        source="uclass",
        created_at=now_local.isoformat(),
        title="통계학개론 중간고사 범위 공지",
        body="시험 범위를 확인하세요.",
        url=None,
        metadata_json={"course_name": "통계학개론"},
    )
    db.upsert_task(
        external_id="uclass:task:stats-1",
        source="uclass",
        due_at=now_local.replace(hour=23, minute=59).isoformat(),
        title="연습문제 2 제출",
        status="open",
        metadata_json={"course_name": "통계학개론"},
    )

    settings = SimpleNamespace(timezone="Asia/Seoul")

    message = pipeline._format_telegram_today(settings=settings, db=db)

    assert "통계학개론" in message
    assert "공지: 통계학개론 중간고사 범위 공지" in message
    assert "수업 과제:" in message
    assert "연습문제 2 제출" in message


def test_format_telegram_today_summary_includes_class_specific_notices_and_tasks(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    db.upsert_event(
        external_id="portal:algo-1",
        source="portal",
        start=now_local.replace(hour=14, minute=0).isoformat(),
        end=(now_local.replace(hour=14, minute=0) + timedelta(hours=1, minutes=15)).isoformat(),
        title="알고리즘",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:algo-1",
        source="uclass",
        filename="week03.pdf",
        icloud_path=None,
        content_hash="algo-hash",
        metadata_json={
            "course_name": "알고리즘",
            "brief": {
                "bullets": [
                    "분할 정복 풀이 흐름을 다시 정리해.",
                ],
            },
        },
    )
    db.upsert_notification(
        external_id="uclass:notif:algo-1",
        source="uclass",
        created_at=now_local.isoformat(),
        title="알고리즘 퀴즈 안내",
        body="다음 주 퀴즈 공지",
        url=None,
        metadata_json={"course_name": "알고리즘"},
    )
    db.upsert_task(
        external_id="uclass:task:algo-1",
        source="uclass",
        due_at=now_local.replace(hour=22, minute=0).isoformat(),
        title="정렬 과제 제출",
        status="open",
        metadata_json={"course_name": "알고리즘"},
    )

    settings = SimpleNamespace(timezone="Asia/Seoul")

    message = pipeline._format_telegram_today_summary(settings=settings, db=db)

    assert "분할 정복 풀이 흐름을 다시 정리해." in message
    assert "- 공지: 알고리즘 퀴즈 안내" in message
    assert "- 수업 과제:" in message
    assert "정렬 과제 제출" in message


def test_format_telegram_setup_treats_local_llm_as_optional_when_core_connections_are_ready(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="inline",
        secret_ref="dummy-secret",
        status="active",
        user_id=int(user["id"]),
    )
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={
            "portal_timetable_sync": {
                "status": "success",
                "auth_required": False,
            }
        },
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2026-03-10T09:00:00+09:00",
        last_cursor_json={"_sync_dashboard": {"status": "success"}},
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2026-03-10T09:00:00+09:00",
        last_cursor_json={"_sync_dashboard": {"status": "success"}},
        user_id=int(user["id"]),
    )

    message = pipeline._format_telegram_setup(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            telegram_allowed_chat_ids=["12345"],
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert "- 로컬 LLM 요약(선택): 선택" in message
    assert "로컬 LLM 요약은 선택 사항입니다." in message
    assert "핵심 연결은 준비됐습니다. 로컬 LLM은 선택 사항입니다." in message


def test_format_telegram_today_uses_course_alias_mapping_for_materials(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_course(
        canonical_course_id="uclass:uclass-example:101",
        source="uclass",
        external_course_id="101",
        display_name="Intro to Economics",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="경제학원론",
        alias_type="manual",
        source="test",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="Intro to Economics",
        alias_type="uclass",
        source="uclass",
        metadata_json={},
    )

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    class_start = now_local.replace(hour=10, minute=0)
    class_end = class_start + timedelta(hours=1, minutes=15)
    db.upsert_event(
        external_id="portal:econ-1",
        source="portal",
        start=class_start.isoformat(),
        end=class_end.isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:econ-brief",
        source="uclass",
        filename="week1.pdf",
        icloud_path=None,
        content_hash="hash-econ",
        metadata_json={
            "course_name": "Intro to Economics",
            "brief": {
                "bullets": [
                    "수요와 공급 곡선이 이동하는 원인을 다시 정리해.",
                ],
            },
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
    )

    message = pipeline._format_telegram_today(settings=settings, db=db)

    assert "경제학원론" in message
    assert "준비: 수요와 공급 곡선이 이동하는 원인을 다시 정리해." in message


def test_format_telegram_tomorrow_uses_human_friendly_sections(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    tomorrow_local = (datetime.now(tz) + timedelta(days=1)).replace(second=0, microsecond=0)
    class_start = tomorrow_local.replace(hour=9, minute=0)
    class_end = class_start + timedelta(hours=1, minutes=15)
    db.upsert_event(
        external_id="portal:tomorrow-class-format-1",
        source="portal",
        start=class_start.isoformat(),
        end=class_end.isoformat(),
        title="현대복지사회와법",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.upsert_task(
        external_id="uclass:task:tomorrow-format-1",
        source="uclass",
        due_at=tomorrow_local.replace(hour=23, minute=59).isoformat(),
        title="토론 준비 메모 제출",
        status="open",
        metadata_json={"course_name": "현대복지사회와법"},
    )
    db.record_artifact(
        external_id="uclass:artifact:tomorrow-format-brief",
        source="uclass",
        filename="welfare.pdf",
        icloud_path=None,
        content_hash="hash-welfare",
        metadata_json={
            "course_name": "현대복지사회와법",
            "brief": {
                "bullets": [
                    "복지국가 유형 비교표를 먼저 다시 훑어.",
                ],
            },
        },
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
    )

    message = pipeline._format_telegram_tomorrow(settings=settings, db=db)

    assert "[Sidae] 내일 보기" in message
    assert "수업: 1" in message
    assert "현대복지사회와법" in message
    assert "준비: 복지국가 유형 비교표를 먼저 다시 훑어." in message
    assert "마감 과제: 1" in message
    assert "토론 준비 메모 제출 [현대복지사회와법]" in message
    assert "자세한 수업 자료 요약: /tomorrowsummary" in message


def test_day_brief_matching_is_shared_across_today_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_course(
        canonical_course_id="uclass:uclass-example:201",
        source="uclass",
        external_course_id="201",
        display_name="Intro to Economics",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:201",
        alias="경제학원론",
        alias_type="manual",
        source="test",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:201",
        alias="Intro to Economics",
        alias_type="uclass",
        source="uclass",
        metadata_json={},
    )

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    class_start = now_local.replace(hour=10, minute=0)
    db.upsert_event(
        external_id="portal:econ-shared-1",
        source="portal",
        start=class_start.isoformat(),
        end=(class_start + timedelta(hours=1, minutes=15)).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:econ-shared-1",
        source="uclass",
        filename="week4.pdf",
        icloud_path=None,
        content_hash="econ-shared-1",
        metadata_json={
            "course_name": "Intro to Economics",
            "brief": {
                "bullets": [
                    "수요와 공급 곡선 이동 요인을 다시 정리해.",
                ],
            },
            "source_kind": "attachment",
        },
    )
    db.upsert_notification(
        external_id="uclass:notif:econ-shared-1",
        source="uclass",
        created_at=now_local.isoformat(),
        title="Reading memo 안내",
        body="오늘 공지",
        url=None,
        metadata_json={"course_name": "Intro to Economics"},
    )
    db.upsert_task(
        external_id="uclass:task:econ-shared-1",
        source="uclass",
        due_at=now_local.replace(hour=23, minute=59).isoformat(),
        title="Reading memo 제출",
        status="open",
        metadata_json={"course_name": "Intro to Economics"},
    )

    monkeypatch.setattr(
        pipeline,
        "_collect_primary_meetings_scoped",
        lambda settings, db, *, target_day_local, user_id=None: {"ok": True, "events": []},
    )
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        weather_enabled=False,
        air_quality_enabled=False,
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        llm_enabled=False,
    )

    today_message = pipeline._format_telegram_today(settings=settings, db=db)
    summary_message = pipeline._format_telegram_today_summary(settings=settings, db=db)
    morning_message, _ = pipeline._build_scheduled_briefing(
        settings=settings,
        db=db,
        slot="morning",
        now_local=now_local,
    )

    for message in (today_message, summary_message, morning_message):
        assert "경제학원론" in message
        assert "Reading memo 안내" in message
        assert "Reading memo 제출" in message
        assert "수요와 공급 곡선 이동 요인을 다시 정리해." in message


def test_day_brief_llm_course_summary_is_shared_across_today_and_briefing_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    first_week_start = (now_local - timedelta(days=14)).replace(hour=18, minute=0)
    db.upsert_event(
        external_id="portal:writing-llm-shared-1",
        source="portal",
        start=first_week_start.isoformat(),
        end=(first_week_start + timedelta(hours=2, minutes=50)).isoformat(),
        title="대학글쓰기",
        location="20-116",
        rrule="FREQ=WEEKLY",
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-llm-shared-1",
        source="uclass",
        filename="week3-spelling.pptx",
        icloud_path=None,
        content_hash="writing-llm-shared-1",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "연음법칙 및 용언의 활용",
            "brief": {
                "bullets": ["연음 규칙 핵심을 정리해."],
            },
        },
    )
    db.record_artifact(
        external_id="uclass:artifact:writing-llm-shared-2",
        source="uclass",
        filename="week3-selfintro.hwp",
        icloud_path=None,
        content_hash="writing-llm-shared-2",
        metadata_json={
            "course_name": "대학글쓰기",
            "section_name": "3주차",
            "module_name": "자기소개서 수업자료",
            "brief": {
                "bullets": ["자기소개서 4개 소제목 구조를 익혀."],
            },
        },
    )

    class FakeLLMClient:
        def generate_text(self, system_prompt: str, prompt: str, attachment_paths=None) -> str:
            payload = json.loads(prompt)
            if payload.get("mode") == "course_day_material_summary":
                return json.dumps(
                    {
                        "short_summary": "맞춤법 핵심과 자기소개서 구성을 함께 정리하세요.",
                        "long_bullets": [
                            "연음 규칙 핵심을 정리해.",
                            "자기소개서 4개 소제목 구조를 익혀.",
                        ],
                        "review": "예문 두 개를 직접 써봐.",
                    },
                    ensure_ascii=False,
                )
            return ""

    monkeypatch.setattr(
        pipeline,
        "_collect_primary_meetings_scoped",
        lambda settings, db, *, target_day_local, user_id=None: {"ok": True, "events": []},
    )
    monkeypatch.setattr(pipeline, "_llm_client", lambda settings: FakeLLMClient())
    monkeypatch.setattr(pipeline, "_build_briefing_llm_guidance", lambda **kwargs: [])

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        weather_enabled=False,
        air_quality_enabled=False,
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        llm_enabled=True,
        llm_provider="local",
        llm_model="gemma4",
        llm_timeout_sec=10,
        llm_local_endpoint="http://127.0.0.1:11434/api/chat",
    )

    today_message = pipeline._format_telegram_today(settings=settings, db=db)
    summary_message = pipeline._format_telegram_today_summary(settings=settings, db=db)
    morning_message, _ = pipeline._build_scheduled_briefing(
        settings=settings,
        db=db,
        slot="morning",
        now_local=now_local,
    )

    assert "준비: 맞춤법 핵심과 자기소개서 구성을 함께 정리하세요." in today_message
    assert "준비: 맞춤법 핵심과 자기소개서 구성을 함께 정리하세요." in morning_message
    assert "연음 규칙 핵심을 정리해." in summary_message
    assert "자기소개서 4개 소제목 구조를 익혀." in summary_message
    assert "복습: 예문 두 개를 직접 써봐." in summary_message
    assert "- 출처: AI" in summary_message
    assert "연음 규칙 핵심을 정리해." not in today_message


def test_day_brief_matching_is_shared_across_tomorrow_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_course(
        canonical_course_id="uclass:uclass-example:301",
        source="uclass",
        external_course_id="301",
        display_name="Statistics 101",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:301",
        alias="통계학개론",
        alias_type="manual",
        source="test",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:301",
        alias="Statistics 101",
        alias_type="uclass",
        source="uclass",
        metadata_json={},
    )

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    tomorrow_local = now_local + timedelta(days=1)
    class_start = tomorrow_local.replace(hour=13, minute=0)
    db.upsert_event(
        external_id="portal:stats-shared-1",
        source="portal",
        start=class_start.isoformat(),
        end=(class_start + timedelta(hours=1, minutes=15)).isoformat(),
        title="통계학개론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.record_artifact(
        external_id="uclass:artifact:stats-shared-1",
        source="uclass",
        filename="week5.pdf",
        icloud_path=None,
        content_hash="stats-shared-1",
        metadata_json={
            "course_name": "Statistics 101",
            "brief": {
                "bullets": [
                    "가설검정 절차와 p-value 해석을 다시 정리해.",
                ],
            },
            "source_kind": "attachment",
        },
    )
    db.upsert_notification(
        external_id="uclass:notif:stats-shared-1",
        source="uclass",
        created_at=now_local.isoformat(),
        title="퀴즈 범위 안내",
        body="내일 공지",
        url=None,
        metadata_json={"course_name": "Statistics 101"},
    )
    db.upsert_task(
        external_id="uclass:task:stats-shared-1",
        source="uclass",
        due_at=tomorrow_local.replace(hour=22, minute=0).isoformat(),
        title="문제집 3 제출",
        status="open",
        metadata_json={"course_name": "Statistics 101"},
    )

    monkeypatch.setattr(
        pipeline,
        "_collect_primary_meetings_scoped",
        lambda settings, db, *, target_day_local, user_id=None: {"ok": True, "events": []},
    )
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        weather_enabled=False,
        air_quality_enabled=False,
        briefing_task_lookahead_days=7,
        briefing_max_classes=6,
        llm_enabled=False,
    )

    tomorrow_message = pipeline._format_telegram_tomorrow(settings=settings, db=db)
    summary_message = pipeline._format_telegram_tomorrow_summary(settings=settings, db=db)
    evening_message, _ = pipeline._build_scheduled_briefing(
        settings=settings,
        db=db,
        slot="evening",
        now_local=now_local,
    )

    for message in (tomorrow_message, summary_message, evening_message):
        assert "통계학개론" in message
        assert "퀴즈 범위 안내" in message
        assert "문제집 3 제출" in message
        assert "가설검정 절차와 p-value 해석을 다시 정리해." in message


def test_matching_helpers_use_course_alias_mapping_for_tasks_and_notifications(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_course(
        canonical_course_id="uclass:uclass-example:101",
        source="uclass",
        external_course_id="101",
        display_name="Intro to Economics",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="경제학원론",
        alias_type="manual",
        source="test",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="Intro to Economics",
        alias_type="uclass",
        source="uclass",
        metadata_json={},
    )

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    db.upsert_task(
        external_id="uclass:task:econ-1",
        source="uclass",
        due_at=now_local.replace(hour=23, minute=59).isoformat(),
        title="Reading memo 제출",
        status="open",
        metadata_json={"course_name": "Intro to Economics"},
    )
    db.upsert_task(
        external_id="uclass:task:econ-overdue",
        source="uclass",
        due_at=(now_local - timedelta(days=7)).replace(hour=23, minute=59).isoformat(),
        title="지난주 과제 제출",
        status="open",
        metadata_json={"course_name": "Intro to Economics"},
    )
    db.upsert_notification(
        external_id="uclass:notif:econ-1",
        source="uclass",
        created_at=now_local.isoformat(),
        title="중간고사 공지",
        body="시험 범위 안내",
        url=None,
        metadata_json={"course_name": "Intro to Economics"},
    )

    class_item = {
        "title": "경제학원론",
        "canonical_course_id": "uclass:uclass-example:101",
        "start_local": now_local.replace(hour=10, minute=0),
        "end_local": now_local.replace(hour=11, minute=15),
        "location_text": "21-101",
    }

    task_lines, file_task_lines = pipeline._matched_tasks_for_class(
        db,
        class_item,
        db.list_open_tasks(limit=10),
        reference_day_local=now_local,
        limit=2,
    )
    notice_titles = pipeline._matched_notifications_for_class(
        db,
        class_item,
        db.list_notifications(limit=10),
        limit=2,
    )

    assert file_task_lines == []
    assert task_lines
    assert "Reading memo 제출" in task_lines[0]
    assert all("지난주 과제 제출" not in line for line in task_lines)
    assert notice_titles == ["중간고사 공지"]


def test_day_brief_uses_cached_task_merge_groups(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    target_day = now_local.replace(hour=12, minute=0)
    due_at = now_local.replace(hour=23, minute=59)

    db.upsert_event(
        external_id="portal:econ-brief-1",
        source="portal",
        start=now_local.replace(hour=10, minute=0).isoformat(),
        end=now_local.replace(hour=11, minute=15).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:101"}
        ),
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-merge-1",
        source="uclass",
        due_at=due_at.isoformat(),
        title="Reading memo 제출",
        status="open",
        metadata_json={
            "course_name": "경제학원론",
            "summary": "1장 reading memo를 정리해서 제출.",
        },
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-merge-2",
        source="uclass",
        due_at=(due_at + timedelta(minutes=5)).isoformat(),
        title="1주차 리딩메모",
        status="open",
        metadata_json={
            "course_name": "경제학원론",
            "summary": "chapter 1 reading memo 제출",
        },
        user_id=user_id,
    )

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        llm_enabled=True,
        llm_provider="local",
        llm_model="gemma4",
    )
    baseline_tasks = db.list_open_tasks(limit=10, user_id=user_id)
    db.update_sync_state(
        pipeline.TASK_MERGE_CACHE_JOB_NAME,
        last_run_at=datetime.now(tz).astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
        last_cursor_json={
            "version": pipeline.TASK_MERGE_CACHE_VERSION,
            "fingerprint": pipeline._task_merge_cache_fingerprint(settings),
            "payload_hash": pipeline._task_merge_payload_hash(
                pipeline._dedupe_tasks_for_briefing(baseline_tasks)
            ),
            "groups": [
                {
                    "ids": [
                        "uclass:task:econ-merge-1",
                        "uclass:task:econ-merge-2",
                    ],
                    "merged_title": "Reading memo 제출",
                    "confidence": "high",
                    "reason": "same reading memo assignment",
                }
            ],
        },
        user_id=user_id,
    )

    day_brief = pipeline.DayBriefService(settings, db, user_id=user_id).build_day_brief(
        target_day_local=target_day,
        reference_day_local=target_day,
        max_classes=4,
    )

    assert len(day_brief.tasks_due_on_day) == 1
    assert day_brief.tasks_due_on_day[0].title == "Reading memo 제출"
    assert len(day_brief.course_briefs[0].task_lines) == 1
    assert "Reading memo 제출" in day_brief.course_briefs[0].task_lines[0]
    assert "1주차 리딩메모" not in day_brief.course_briefs[0].task_lines[0]


def test_day_brief_reuses_shared_match_context(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])

    tz = ZoneInfo("Asia/Seoul")
    target_day = datetime.now(tz).replace(hour=9, minute=0, second=0, microsecond=0)

    db.upsert_event(
        external_id="portal:econ-shared-1",
        source="portal",
        start=target_day.replace(hour=10).isoformat(),
        end=(target_day.replace(hour=10) + timedelta(hours=1, minutes=15)).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:101"}
        ),
        user_id=user_id,
    )
    db.upsert_event(
        external_id="portal:stats-shared-1",
        source="portal",
        start=target_day.replace(hour=13).isoformat(),
        end=(target_day.replace(hour=13) + timedelta(hours=1, minutes=15)).isoformat(),
        title="통계학입문",
        location="21-201",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:202"}
        ),
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-shared-1",
        source="uclass",
        due_at=target_day.replace(hour=23, minute=59).isoformat(),
        title="Reading memo 제출",
        status="open",
        metadata_json={"course_name": "경제학원론"},
        user_id=user_id,
    )

    alias_map_calls = 0
    original_alias_map = db.course_alias_resolution_map

    def _counted_alias_map(*args, **kwargs):
        nonlocal alias_map_calls
        alias_map_calls += 1
        return original_alias_map(*args, **kwargs)

    dedupe_calls = 0
    original_dedupe = pipeline._dedupe_tasks_for_briefing

    def _counted_dedupe(tasks):
        nonlocal dedupe_calls
        dedupe_calls += 1
        return original_dedupe(tasks)

    monkeypatch.setattr(db, "course_alias_resolution_map", _counted_alias_map)
    monkeypatch.setattr(pipeline, "_dedupe_tasks_for_briefing", _counted_dedupe)

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        llm_enabled=False,
        llm_provider="local",
        llm_model="gemma4",
    )

    day_brief = pipeline.DayBriefService(settings, db, user_id=user_id).build_day_brief(
        target_day_local=target_day,
        reference_day_local=target_day,
        max_classes=6,
    )

    assert len(day_brief.course_briefs) == 2
    assert alias_map_calls == 1
    assert dedupe_calls == 1
    assert any("Reading memo 제출" in line for line in day_brief.course_briefs[0].task_lines)


def test_sync_telegram_todaysummary_precomputes_task_merge_cache(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    user_id = int(user["id"])
    sent_messages: list[tuple[str, str]] = []

    tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    due_at = now_local.replace(hour=23, minute=59)
    db.upsert_event(
        external_id="portal:econ-summary-1",
        source="portal",
        start=now_local.replace(hour=10, minute=0).isoformat(),
        end=now_local.replace(hour=11, minute=15).isoformat(),
        title="경제학원론",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(
            {"canonical_course_id": "uclass:uclass-example:101"}
        ),
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-sync-1",
        source="uclass",
        due_at=due_at.isoformat(),
        title="Reading memo 제출",
        status="open",
        metadata_json={
            "course_name": "경제학원론",
            "summary": "1장 reading memo를 정리해서 제출.",
        },
        user_id=user_id,
    )
    db.upsert_task(
        external_id="uclass:task:econ-sync-2",
        source="uclass",
        due_at=(due_at + timedelta(minutes=5)).isoformat(),
        title="1주차 리딩메모",
        status="open",
        metadata_json={
            "course_name": "경제학원론",
            "summary": "chapter 1 reading memo 제출",
        },
        user_id=user_id,
    )

    class FakeLLMClient:
        def generate_text(self, system_prompt: str, prompt: str, attachment_paths=None) -> str:
            payload = json.loads(prompt)
            ids = [item["id"] for item in payload["tasks"]]
            return json.dumps(
                {
                    "groups": [
                        {
                            "ids": ids,
                            "merged_title": "Reading memo 제출",
                            "confidence": "high",
                            "reason": "same assignment",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 22,
                    "message": {
                        "date": 1770000000,
                        "text": "/todaysummary",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "_llm_client", lambda settings: FakeLLMClient())
    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)

    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        llm_enabled=True,
        llm_provider="local",
        llm_model="gemma4",
        llm_timeout_sec=10,
        llm_local_endpoint="http://127.0.0.1:11434/api/chat",
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "Reading memo 제출" in message
    assert "1주차 리딩메모" not in message
    assert message.count("수업 과제:") == 1


def test_format_telegram_today_empty_state_is_human_friendly(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    settings = SimpleNamespace(
        timezone="Asia/Seoul",
    )

    message = pipeline._format_telegram_today(settings=settings, db=db)

    assert "[Sidae] 오늘 보기" in message
    assert "아직 학교 계정이 연결되지 않았습니다." in message
    assert "`/connect`로 학교 계정을 연결하세요." in message
    assert "events=" not in message
    assert "tasks_due=" not in message


def test_format_telegram_status_shows_separate_telegram_and_uclass_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_event(
        external_id="portal:class-1",
        source="portal",
        start="2026-03-09T09:00:00+09:00",
        end="2026-03-09T10:15:00+09:00",
        title="계량경제학",
        location="21-101",
        rrule=None,
        metadata_json=_portal_timetable_metadata(),
    )
    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2026-03-12T23:59:00+09:00",
        title="과제 1 제출",
        status="open",
        metadata_json={"course_name": "계량경제학"},
    )
    db.record_artifact(
        external_id="uclass:artifact:1",
        source="uclass",
        filename="chapter01.pdf",
        icloud_path=None,
        content_hash="hash-1",
        metadata_json={"course_name": "계량경제학"},
    )
    db.update_sync_state(
        "sync_telegram",
        last_run_at="2026-03-09T02:00:00+09:00",
        last_cursor_json={
            "fetched": 1,
            "commands": {"processed": 1, "failed": 0},
            "reminders": {"sent": 0, "failed": 0, "due": 0},
        },
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2026-03-09T01:51:26+09:00",
        last_cursor_json={
            "tasks": 1,
            "events": 0,
            "artifacts": 1,
            "wsfunctions": {
                "core_course_get_contents": {"ok": 6, "failed": 0, "skipped": 0, "last_error": None},
            },
        },
    )

    settings = SimpleNamespace(timezone="Asia/Seoul")
    message = pipeline._format_telegram_status(settings=settings, db=db)

    assert "[Sidae] 상태" in message
    assert "전체" in message
    assert "서비스별 상태" in message
    assert "마지막 성공 동기화" in message
    assert "Telegram" in message
    assert "Telegram: 준비됨" in message
    assert "UClass" in message
    assert "UClass: 준비됨" in message


def test_format_telegram_status_falls_back_to_global_sync_state_for_user_scope(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(
        chat_id="12345",
        timezone_name="Asia/Seoul",
        metadata_json={"source": "test"},
    )
    db.update_sync_state(
        "sync_telegram",
        last_run_at="2026-03-09T02:00:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2026-03-09T02:00:00+09:00",
                "new_items": 0,
                "action_required": 0,
                "last_error": None,
            },
            "fetched": 1,
            "commands": {"processed": 1, "failed": 0},
            "reminders": {"sent": 0, "failed": 0, "due": 0},
        },
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2026-03-09T01:51:26+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2026-03-09T01:51:26+09:00",
                "new_items": 1,
                "action_required": 0,
                "last_error": None,
            },
            "tasks": 1,
            "events": 0,
            "artifacts": 1,
            "wsfunctions": {
                "core_course_get_contents": {"ok": 6, "failed": 0, "skipped": 0, "last_error": None},
            },
        },
    )

    settings = SimpleNamespace(timezone="Asia/Seoul")
    message = pipeline._format_telegram_status(settings=settings, db=db, user_id=int(user["id"]))

    assert "마지막 성공 동기화: 2026-03-09 02:00 KST" in message
    assert "Telegram: 준비됨" in message
    assert "UClass: 준비됨" in message


def test_sync_telegram_apply_command_returns_human_friendly_summary(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []
    db.upsert_inbox_item(
        external_id="telegram:draft:1",
        source="telegram",
        received_at="2026-03-09T10:00:00+09:00",
        title="과제 초안",
        body="body",
        item_type="task_draft",
        draft_json={
            "title": "계량경제학 과제 제출",
            "due_at": "2026-03-12T23:59:00+09:00",
            "status": "open",
        },
        processed=False,
        metadata_json={"chat_id": "12345"},
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 300,
                    "message": {
                        "date": 1770000000,
                        "text": "/apply all",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] Inbox 반영" in message
    assert "- 처리 1건" in message
    assert "- 생성: 일정 0건 / 과제 1건 / 메모 0건" in message


def test_sync_telegram_done_command_returns_human_friendly_summary(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []
    db.upsert_task(
        external_id="uclass:task:done-1",
        source="uclass",
        due_at="2026-03-12T23:59:00+09:00",
        title="과제 1 제출",
        status="open",
        metadata_json={"course_name": "계량경제학"},
    )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 301,
                    "message": {
                        "date": 1770000000,
                        "text": "/done task uclass:task:done-1",
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "[Sidae] 과제 상태 변경" in message
    assert "- 과제: 과제 1 제출" in message
    assert "- 상태: done" in message
    assert "- ID: uclass:task:done-1" in message


def test_sync_uclass_records_reconnect_required_error_state(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": 1,
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.example.com/webservice/rest/server.php",
                "token": "",
                "token_error": "UClass token expired or unavailable; reconnect required",
            }
        ],
    )

    settings = SimpleNamespace(
        uclass_ws_base="https://uclass.example.com/webservice/rest/server.php",
    )

    result = pipeline.sync_uclass(settings=settings, db=db)
    state = db.get_sync_state("sync_uclass", user_id=1)

    assert result["ok"] is False
    assert result["error"] == "UClass token expired or unavailable; reconnect required"
    assert state.last_run_at is not None
    assert "reconnect required" in (state.last_cursor_json or "")
