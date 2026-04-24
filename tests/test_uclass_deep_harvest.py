from __future__ import annotations

from datetime import datetime, timezone

from sidae_secretary.connectors.uclass import (
    NormalizedNotification,
    extract_material_candidates,
    extract_material_candidates_from_course_contents,
    normalize_assignments,
)


def test_normalize_assignments_sets_due_at() -> None:
    payload = {
        "courses": [
            {
                "id": 101,
                "assignments": [
                    {"id": 11, "name": "HW 1", "duedate": 1770000000}
                ],
            }
        ]
    }
    course_index = {101: {"fullname": "Algorithms"}}

    tasks = normalize_assignments(payload, course_index=course_index)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.external_id == "uclass:assign:11"
    assert task.due_at is not None
    assert task.metadata["course_name"] == "Algorithms"


def test_extract_materials_from_course_contents_stable_ids() -> None:
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
    course_index = {101: {"fullname": "Algorithms"}}

    items1 = extract_material_candidates_from_course_contents(course_contents, course_index)
    items2 = extract_material_candidates_from_course_contents(course_contents, course_index)

    assert len(items1) == 1
    assert sorted([x.external_id for x in items1]) == sorted([x.external_id for x in items2])
    assert all(x.url for x in items1)


def test_extract_materials_from_course_contents_skips_ubboard_view_container() -> None:
    course_contents = {
        101: [
            {
                "name": "Week 2",
                "modules": [
                    {
                        "id": 601,
                        "name": "강의 참고 자료",
                        "modname": "ubboard",
                        "url": "https://uclass.example/mod/ubboard/view.php?id=601",
                        "contents": [
                            {
                                "filename": "week2.pdf",
                                "fileurl": "https://uclass.example/pluginfile.php/601/week2.pdf",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    course_index = {101: {"fullname": "Algorithms"}}

    items = extract_material_candidates_from_course_contents(course_contents, course_index)

    assert [item.filename for item in items] == ["week2.pdf"]


def test_extract_materials_from_course_contents_keeps_non_view_direct_urls() -> None:
    course_contents = {
        101: [
            {
                "name": "Week 3",
                "modules": [
                    {
                        "id": 701,
                        "name": "External reference",
                        "modname": "url",
                        "url": "https://example.com/materials/week3.pdf",
                        "contents": [],
                    }
                ],
            }
        ]
    }
    course_index = {101: {"fullname": "Algorithms"}}

    items = extract_material_candidates_from_course_contents(course_contents, course_index)

    assert [item.url for item in items] == ["https://example.com/materials/week3.pdf"]


def test_extract_materials_from_course_contents_uses_local_timezone_for_date_folder() -> None:
    course_contents = {
        101: [
            {
                "name": "Week 1",
                "modules": [
                    {
                        "id": 500,
                        "name": "Lecture 1",
                        "modname": "resource",
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
    course_index = {101: {"fullname": "Algorithms"}}

    items = extract_material_candidates_from_course_contents(
        course_contents,
        course_index,
        timezone_name="Asia/Seoul",
        current_dt=datetime(2026, 3, 19, 0, 35, tzinfo=timezone.utc),
    )

    assert [item.date_folder for item in items] == ["2026-03-19"]


def test_extract_material_candidates_uses_local_timezone_for_date_folder() -> None:
    notifications = [
        NormalizedNotification(
            external_id="uclass:notif:1",
            created_at="2026-03-19T00:35:00+00:00",
            title="Week 1 slides",
            body=None,
            url="https://uclass.example/pluginfile.php/500/slides.pdf",
            metadata={
                "raw": {
                    "courseid": 101,
                    "coursename": "Algorithms",
                    "url": "https://uclass.example/pluginfile.php/500/slides.pdf",
                }
            },
        )
    ]

    items = extract_material_candidates(
        notifications,
        tasks=[],
        events=[],
        timezone_name="Asia/Seoul",
        current_dt=datetime(2026, 3, 19, 0, 35, tzinfo=timezone.utc),
    )

    assert [item.date_folder for item in items] == ["2026-03-19"]
