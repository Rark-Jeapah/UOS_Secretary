from pathlib import Path
import sqlite3

from sidae_secretary.buildings import UOS_BUILDING_MAP
from sidae_secretary.db import Database


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_event_upsert_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sidae.db"
    db = Database(db_path)
    db.init()
    payload = {
        "external_id": "portal:test-1",
        "source": "portal",
        "start": "2026-03-05T00:00:00+00:00",
        "end": "2026-03-05T01:00:00+00:00",
        "title": "Math",
        "location": "Room A",
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
        "metadata_json": {"term": "2026-1", "timetable_source": "uos_portal"},
    }
    db.upsert_event(**payload)
    db.upsert_event(**payload)
    assert _count(db_path, "events") == 1


def test_task_upsert_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sidae.db"
    db = Database(db_path)
    db.init()
    payload = {
        "external_id": "uclass:task:99",
        "source": "uclass",
        "due_at": "2026-03-10T10:00:00+00:00",
        "title": "Assignment",
        "status": "pending",
        "metadata_json": {"course": "CS101"},
    }
    db.upsert_task(**payload)
    db.upsert_task(**payload)
    assert _count(db_path, "tasks") == 1


def test_init_seeds_builtin_uos_buildings(tmp_path: Path) -> None:
    db_path = tmp_path / "sidae.db"
    db = Database(db_path)
    db.init()

    buildings = db.list_buildings(limit=5000, school_slug="uos_online_class")
    mapped = {row["building_no"]: row["building_name"] for row in buildings}

    assert mapped.get("20") == "법학관"
    assert mapped.get("21") == "중앙도서관"
    assert len(mapped) >= len(UOS_BUILDING_MAP)
    assert db.get_building_name("20", school_slug="uos_portal") == "법학관"
