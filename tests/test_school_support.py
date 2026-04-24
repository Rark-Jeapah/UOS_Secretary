from __future__ import annotations

from pathlib import Path

from sidae_secretary.db import Database
from sidae_secretary.school_support import school_support_summary


def test_school_support_summary_marks_uos_and_yonsei_differently(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    entries = {item["school_slug"]: item for item in db.list_moodle_school_directory(limit=2000)}
    uos = entries["uos_online_class"]
    yonsei = entries["yonsei_learnus"]

    uos_support = school_support_summary(uos)
    yonsei_support = school_support_summary(yonsei)

    assert uos_support["support_level"] == "truly_supported"
    assert uos_support["official_user_support"] is True
    assert uos_support["capabilities"]["portal_timetable_sync"] is True

    assert yonsei_support["support_level"] == "partially_supported"
    assert yonsei_support["official_user_support"] is False
    assert yonsei_support["capabilities"]["lms_sync"] is True
    assert yonsei_support["capabilities"]["portal_shared_account_hint"] is True
    assert yonsei_support["capabilities"]["portal_timetable_sync"] is False


def test_db_init_persists_school_support_metadata(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    uos = db.get_school_by_slug("uos_online_class")
    yonsei = db.get_school_by_slug("yonsei_learnus")

    assert uos is not None
    assert yonsei is not None
    assert uos["metadata_json"]["support_level"] == "truly_supported"
    assert uos["metadata_json"]["official_user_support"] is True
    assert uos["metadata_json"]["capabilities"]["portal_timetable_sync"] is True

    assert yonsei["metadata_json"]["support_level"] == "partially_supported"
    assert yonsei["metadata_json"]["official_user_support"] is False
    assert yonsei["metadata_json"]["capabilities"]["lms_sync"] is True
    assert yonsei["metadata_json"]["capabilities"]["portal_timetable_sync"] is False
