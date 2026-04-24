from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def test_task_done_status_persists_across_upserts(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2026-03-10T10:00:00+09:00",
        title="HW1",
        status="open",
        metadata_json={},
    )

    updated = db.update_task_status("uclass:task:1", "done")
    assert updated is not None
    assert updated["status"] == "done"

    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2026-03-10T10:00:00+09:00",
        title="HW1 (renamed upstream)",
        status="open",
        metadata_json={"raw": {"status": "open"}},
    )

    row = db.get_task_for_selector("uclass:task:1")
    assert row is not None
    assert row["status"] == "done"
    assert db.list_open_tasks(limit=20) == []


def test_review_done_is_hidden_from_dashboard_upcoming(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_event(
        external_id="review:portal:event:1:D+1",
        source="review",
        start="2099-03-05T09:00:00+09:00",
        end="2099-03-05T09:25:00+09:00",
        title="Review: Algorithms",
        location=None,
        rrule=None,
        metadata_json={"review_status": "scheduled"},
    )
    before = db.dashboard_snapshot(now_iso="2099-03-01T00:00:00+00:00")
    assert len(before["upcoming_events"]) == 1

    done = db.update_review_status("review:portal:event:1:D+1", "done")
    assert done is not None
    assert done["review_status"] == "done"

    after = db.dashboard_snapshot(now_iso="2099-03-01T00:00:00+00:00")
    assert after["upcoming_events"] == []


def test_apply_inbox_all_skips_command_items(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_inbox_item(
        external_id="telegram:update:1",
        source="telegram",
        received_at="2026-03-04T09:00:00+09:00",
        title="/status",
        body="/status",
        item_type="command",
        draft_json={"command": "status", "ok": True},
        processed=False,
        metadata_json={"chat_id": "123"},
    )
    db.upsert_inbox_item(
        external_id="telegram:update:2",
        source="telegram",
        received_at="2026-03-04T10:00:00+09:00",
        title="Task",
        body="Do assignment",
        item_type="task_draft",
        draft_json={"title": "Do assignment", "status": "open"},
        processed=False,
        metadata_json={},
    )

    result = pipeline.apply_inbox_items(
        settings=SimpleNamespace(),
        db=db,
        apply_all=True,
    )

    assert result["processed"] == 1
    assert result["created_tasks"] == 1
    commands = db.list_unprocessed_inbox_commands(limit=20)
    assert len(commands) == 1
