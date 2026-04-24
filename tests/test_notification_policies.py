from __future__ import annotations

from pathlib import Path
import sqlite3

from sidae_secretary.db import Database, MIGRATIONS


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
                (version, "2026-03-31T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def test_notification_policies_migration_upgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "sidae.db"
    _apply_migrations_through(db_path, 20)

    db = Database(db_path)
    db.init()

    with db.connection() as conn:
        version_row = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        columns = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA table_info(notification_policies)").fetchall()
        }

    assert int(version_row["version"]) == max(version for version, _ in MIGRATIONS)
    assert set(
        {
            "id",
            "user_id",
            "policy_kind",
            "enabled",
            "days_of_week_json",
            "time_local",
            "timezone",
            "metadata_json",
            "created_at",
            "updated_at",
        }
    ).issubset(columns)


def test_notification_policies_crud_is_separate_from_user_preferences(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    created = db.upsert_notification_policy(
        chat_id="12345",
        policy_kind="morning_briefing",
        enabled=True,
        days_of_week_json=[1, 2, 3, 4, 5],
        time_local="08:30",
        timezone="Asia/Seoul",
        metadata_json={"source": "test"},
    )

    assert created["user_id"] > 0
    assert created["chat_id"] == "12345"
    assert created["policy_kind"] == "morning_briefing"
    assert created["enabled"] is True
    assert created["days_of_week_json"] == [1, 2, 3, 4, 5]
    assert created["time_local"] == "08:30"
    assert created["timezone"] == "Asia/Seoul"
    assert created["metadata_json"] == {"source": "test"}
    assert db.get_user_preferences(chat_id="12345") is None

    by_chat = db.get_notification_policy(
        "morning_briefing",
        chat_id="12345",
    )
    by_user = db.get_notification_policy(
        "morning_briefing",
        user_id=created["user_id"],
    )
    assert by_chat == by_user

    updated = db.upsert_notification_policy(
        user_id=created["user_id"],
        policy_kind="morning_briefing",
        enabled=False,
        days_of_week_json=[0, 6],
        time_local="09:00",
        metadata_json={"updated_by": "test"},
    )

    assert updated["id"] == created["id"]
    assert updated["enabled"] is False
    assert updated["days_of_week_json"] == [0, 6]
    assert updated["time_local"] == "09:00"
    assert updated["timezone"] == "Asia/Seoul"
    assert updated["metadata_json"]["source"] == "test"
    assert updated["metadata_json"]["updated_by"] == "test"

    second = db.upsert_notification_policy(
        user_id=created["user_id"],
        policy_kind="daily_digest",
        enabled=True,
        days_of_week_json=["mon", "wed"],
        time_local="19:00",
        timezone="Asia/Seoul",
    )

    listed = db.list_notification_policies(user_id=created["user_id"])
    enabled_only = db.list_notification_policies(
        user_id=created["user_id"],
        enabled=True,
    )
    disabled_only = db.list_notification_policies(chat_id="12345", enabled=False)

    assert {item["policy_kind"] for item in listed} == {
        "morning_briefing",
        "daily_digest",
    }
    assert [item["policy_kind"] for item in enabled_only] == ["daily_digest"]
    assert [item["policy_kind"] for item in disabled_only] == ["morning_briefing"]
    assert second["policy_kind"] == "daily_digest"

    assert db.delete_notification_policy("morning_briefing", chat_id="12345") is True
    assert (
        db.get_notification_policy("morning_briefing", user_id=created["user_id"])
        is None
    )
    assert db.delete_notification_policy("morning_briefing", user_id=created["user_id"]) is False
