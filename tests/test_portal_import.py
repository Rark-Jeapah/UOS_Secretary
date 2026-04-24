from __future__ import annotations

from pathlib import Path

from sidae_secretary.connectors.portal import parse_csv_file, parse_ics_text


ICS_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:test-1
DTSTART;VALUE=DATE:20260310
SUMMARY:Midterm Week
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_text_builds_portal_external_id() -> None:
    events = parse_ics_text(ICS_SAMPLE, timezone_name="Asia/Seoul")
    assert len(events) == 1
    assert events[0].external_id == "portal:midterm-week:2026-03-10"


def test_parse_csv_file_builds_deduped_events(tmp_path: Path) -> None:
    csv_path = tmp_path / "academic.csv"
    csv_path.write_text(
        "date,title\n2026-03-01,Semester Start\n2026-03-01,Semester Start\n",
        encoding="utf-8",
    )

    events = parse_csv_file(csv_path, timezone_name="Asia/Seoul")

    assert len(events) == 1
    assert events[0].external_id == "portal:semester-start:2026-03-01"
