from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sidae_secretary.db import Database, MIGRATIONS
from sidae_secretary.jobs import pipeline


def _apply_migrations_through(db_path: Path, version_limit: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        for version, sql in MIGRATIONS:
            if version > version_limit:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (version, "2026-03-14T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def test_user_preferences_migration_upgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "sidae.db"
    _apply_migrations_through(db_path, 16)

    db = Database(db_path)
    db.init()

    with db.connection() as conn:
        version_row = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        columns = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA table_info(user_preferences)").fetchall()
        }

    assert int(version_row["version"]) == max(version for version, _ in MIGRATIONS)
    assert set(
        {
            "user_id",
            "telegram_chat_allowed",
            "material_brief_push_enabled",
            "scheduled_briefings_enabled",
            "daily_digest_enabled",
            "weather_location_label",
            "weather_lat",
            "weather_lon",
            "weather_air_quality_district_code",
            "metadata_json",
            "created_at",
            "updated_at",
        }
    ).issubset(columns)


def test_user_preferences_crud_by_chat_id_and_user_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    created = db.upsert_user_preferences(
        chat_id="12345",
        telegram_chat_allowed=True,
        material_brief_push_enabled=True,
        weather_location_label="동대문구",
        weather_lat=37.5744,
        weather_lon=127.0396,
        weather_air_quality_district_code="111152",
        metadata_json={"source": "test"},
    )

    assert created["user_id"] > 0
    assert created["chat_id"] == "12345"
    assert created["telegram_chat_allowed"] is True
    assert created["material_brief_push_enabled"] is True
    assert created["scheduled_briefings_enabled"] is None
    assert created["daily_digest_enabled"] is None
    assert created["weather_location_label"] == "동대문구"
    assert created["weather_lat"] == 37.5744
    assert created["weather_lon"] == 127.0396
    assert created["weather_air_quality_district_code"] == "111152"

    by_chat = db.get_user_preferences(chat_id="12345")
    by_user = db.get_user_preferences(user_id=created["user_id"])
    assert by_chat == by_user

    updated = db.upsert_user_preferences(
        user_id=created["user_id"],
        material_brief_push_enabled=False,
        scheduled_briefings_enabled=True,
        weather_location_label=None,
        weather_lat=None,
        weather_lon=None,
        weather_air_quality_district_code=None,
        metadata_json={"updated_by": "test"},
    )

    assert updated["telegram_chat_allowed"] is True
    assert updated["material_brief_push_enabled"] is False
    assert updated["scheduled_briefings_enabled"] is True
    assert updated["weather_location_label"] is None
    assert updated["weather_lat"] is None
    assert updated["weather_lon"] is None
    assert updated["weather_air_quality_district_code"] is None
    assert updated["metadata_json"]["source"] == "test"
    assert updated["metadata_json"]["updated_by"] == "test"
    assert db.has_user_preference_value("telegram_chat_allowed") is True
    assert db.has_user_preference_value("daily_digest_enabled") is False
    assert db.list_chat_ids_by_preference("telegram_chat_allowed") == ["12345"]
    assert db.list_chat_ids_by_preference("scheduled_briefings_enabled") == ["12345"]
    assert db.list_chat_ids_by_preference("material_brief_push_enabled") == []
    assert db.list_user_weather_locations() == []


def test_sync_telegram_uses_db_preferences_without_legacy_allowlist(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(chat_id="12345", telegram_chat_allowed=True)
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
        telegram_allowed_chat_ids=[],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "12345"


def test_material_brief_push_uses_db_preferences_without_legacy_allowlist(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(chat_id="12345", material_brief_push_enabled=True)
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        material_brief_push_enabled=True,
        material_brief_push_max_items=3,
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=[],
        include_identity=False,
    )

    result = pipeline.send_material_brief_push(
        settings=settings,
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
    assert result["sent_to"] == ["12345"]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "12345"
    assert "[Sidae] 새 강의자료 요약" in sent_messages[0][1]


def test_chat_ids_for_preference_merge_connected_defaults_with_db_overrides(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(chat_id="111", material_brief_push_enabled=False)
    db.upsert_user_preferences(chat_id="333", material_brief_push_enabled=True)
    connected_user = db.ensure_user_for_chat(chat_id="444", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="444",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="connected-user",
        secret_kind="inline",
        secret_ref="dummy-secret",
        status="active",
        user_id=int(connected_user["id"]),
    )
    excluded_connected_user = db.ensure_user_for_chat(chat_id="555", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="555",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="excluded-user",
        secret_kind="inline",
        secret_ref="dummy-secret",
        status="active",
        user_id=int(excluded_connected_user["id"]),
    )
    db.upsert_user_preferences(chat_id="555", material_brief_push_enabled=False)
    settings = SimpleNamespace(
        telegram_allowed_chat_ids=["111", "222"],
    )

    resolved = pipeline._chat_ids_for_user_preference(
        settings,
        db,
        pipeline.USER_PREFERENCE_MATERIAL_BRIEF_PUSH_ENABLED,
    )

    assert resolved == ["222", "333", "444"]


def test_notification_policies_override_legacy_preference_targets(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_user_preferences(chat_id="111", scheduled_briefings_enabled=True)
    db.upsert_user_preferences(chat_id="222", scheduled_briefings_enabled=False)
    db.upsert_notification_policy(
        chat_id="111",
        policy_kind="briefing_morning",
        enabled=False,
    )
    db.upsert_notification_policy(
        chat_id="222",
        policy_kind="morning_briefing",
        enabled=True,
        days_of_week_json=["mon"],
        time_local="09:00",
        timezone="Asia/Seoul",
    )
    settings = SimpleNamespace(
        telegram_allowed_chat_ids=["333"],
        timezone="Asia/Seoul",
    )

    resolved = pipeline._chat_ids_for_notification_dispatch(
        settings,
        db,
        preference=pipeline.USER_PREFERENCE_SCHEDULED_BRIEFINGS_ENABLED,
        policy_kinds=(pipeline.NOTIFICATION_POLICY_KIND_BRIEFING_MORNING,),
        reference_local=datetime(2026, 4, 6, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert resolved == ["222", "333"]
