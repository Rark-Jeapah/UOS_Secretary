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


def test_assistant_runs_migration_upgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "sidae.db"
    _apply_migrations_through(db_path, 21)

    db = Database(db_path)
    db.init()

    with db.connection() as conn:
        version_row = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        columns = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA table_info(assistant_runs)").fetchall()
        }

    assert int(version_row["version"]) == max(version for version, _ in MIGRATIONS)
    assert set(
        {
            "id",
            "user_id",
            "chat_id",
            "request_raw",
            "context_json",
            "planner_output_json",
            "executor_result_json",
            "final_reply",
            "status",
            "created_at",
            "updated_at",
        }
    ).issubset(columns)


def test_assistant_run_success_log_storage(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    created = db.create_assistant_run(
        chat_id="12345",
        request_raw="/bot 오늘 일정 요약해줘",
        context_json={"surface": "telegram", "command": "/bot"},
    )

    assert created["user_id"] is not None
    assert created["chat_id"] == "12345"
    assert created["request_raw"] == "/bot 오늘 일정 요약해줘"
    assert created["context_json"] == {"surface": "telegram", "command": "/bot"}
    assert created["planner_output_json"] == {}
    assert created["executor_result_json"] == {}
    assert created["final_reply"] is None
    assert created["status"] == "pending"

    stored = db.update_assistant_run(
        created["id"],
        planner_output_json={"intent": "today_summary", "steps": ["read_schedule"]},
        executor_result_json={"ok": True, "events": 3},
        final_reply="오늘 일정 3건을 찾았습니다.",
        status="succeeded",
    )

    assert stored is not None
    assert stored["id"] == created["id"]
    assert stored["planner_output_json"]["intent"] == "today_summary"
    assert stored["executor_result_json"]["ok"] is True
    assert stored["final_reply"] == "오늘 일정 3건을 찾았습니다."
    assert stored["status"] == "succeeded"
    assert db.get_assistant_run(created["id"]) == stored
    assert [item["id"] for item in db.list_assistant_runs(chat_id="12345")] == [created["id"]]


def test_assistant_run_failure_log_storage(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="777", timezone_name="Asia/Seoul")

    created = db.create_assistant_run(
        user_id=int(user["id"]),
        request_raw="/bot 내일 수업 준비물 알려줘",
        context_json={"surface": "telegram", "chat_id": "777"},
        status="running",
    )

    failed = db.update_assistant_run(
        created["id"],
        planner_output_json={"intent": "tomorrow_prep"},
        executor_result_json={"ok": False, "error": "context timeout"},
        final_reply="지금은 응답을 완료하지 못했습니다.",
        status="failed",
    )

    assert failed is not None
    assert failed["user_id"] == int(user["id"])
    assert failed["status"] == "failed"
    assert failed["planner_output_json"]["intent"] == "tomorrow_prep"
    assert failed["executor_result_json"]["error"] == "context timeout"
    assert failed["final_reply"] == "지금은 응답을 완료하지 못했습니다."

    failed_only = db.list_assistant_runs(user_id=int(user["id"]), status="failed")

    assert [item["id"] for item in failed_only] == [created["id"]]
