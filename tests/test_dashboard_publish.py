from __future__ import annotations

import json
import re
from pathlib import Path

from sidae_secretary.db import Database, attach_provenance
from sidae_secretary.publish.dashboard import render_dashboard_snapshot


def test_dashboard_html_embeds_json_without_fetch(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_event(
        external_id="event:1",
        source="test",
        start="2099-03-05T09:00:00+09:00",
        end="2099-03-05T10:00:00+09:00",
        title="Class",
        location=None,
        rrule=None,
        metadata_json={"x": 1},
    )

    output = render_dashboard_snapshot(db=db, storage_root_dir=tmp_path / "storage")

    html = Path(output["html_path"]).read_text(encoding="utf-8")
    assert '<script id="sidae-data" type="application/json">' in html
    assert 'fetch("data.json")' not in html

    match = re.search(
        r'<script id="sidae-data" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    embedded = json.loads(match.group(1))
    assert embedded["upcoming_events"][0]["external_id"] == "event:1"

    data_json = Path(output["data_path"]).read_text(encoding="utf-8")
    parsed = json.loads(data_json)
    assert parsed["upcoming_events"][0]["external_id"] == "event:1"


def test_dashboard_writes_precomputed_telegram_briefing_files(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    precomputed = {
        "ok": True,
        "generated_at": "2026-03-08T00:00:00+00:00",
        "items": {
            "2026-03-09-morning": {
                "item_key": "2026-03-09-morning",
                "slot": "morning",
                "send_at_local": "2026-03-09T09:00:00+09:00",
                "message": "[Sidae] 아침 브리핑",
                "chat_ids": ["12345"],
            },
            "2026-03-08-evening": {
                "item_key": "2026-03-08-evening",
                "slot": "evening",
                "send_at_local": "2026-03-08T21:00:00+09:00",
                "message": "[Sidae] 저녁 브리핑",
                "chat_ids": ["12345"],
            },
        },
    }

    output = render_dashboard_snapshot(
        db=db,
        storage_root_dir=tmp_path / "storage",
        extra_data={"precomputed_telegram_briefings": precomputed},
    )

    manifest_path = Path(output["telegram_briefing_files"]["manifest_path"])
    morning_json_path = Path(
        output["telegram_briefing_files"]["items"]["2026-03-09-morning"]["json_path"]
    )
    morning_text_path = Path(
        output["telegram_briefing_files"]["items"]["2026-03-09-morning"]["text_path"]
    )
    parsed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parsed_morning = json.loads(morning_json_path.read_text(encoding="utf-8"))

    assert parsed_manifest["items"]["2026-03-09-morning"]["send_at_local"] == "2026-03-09T09:00:00+09:00"
    assert parsed_morning["message"] == "[Sidae] 아침 브리핑"
    assert morning_text_path.read_text(encoding="utf-8") == "[Sidae] 아침 브리핑\n"

    data_json = Path(output["data_path"]).read_text(encoding="utf-8")
    parsed = json.loads(data_json)
    assert parsed["precomputed_telegram_briefings"]["items"]["2026-03-08-evening"]["message"] == "[Sidae] 저녁 브리핑"


def test_dashboard_snapshot_includes_sync_dashboard_and_provenance(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_event(
        external_id="portal:1",
        source="portal",
        start="2099-03-05T09:00:00+09:00",
        end="2099-03-05T10:00:00+09:00",
        title="Class",
        location=None,
        rrule=None,
        metadata_json=attach_provenance(
            {"timetable_source": "uos_portal"},
            source="portal_uos_timetable",
            confidence="high",
        ),
    )
    db.upsert_task(
        external_id="inbox:1",
        source="inbox",
        due_at="2099-03-06T23:59:00+09:00",
        title="Draft task",
        status="open",
        metadata_json=attach_provenance({}, source="telegram_draft", confidence="low"),
    )
    db.record_summary(
        external_id="llm:summary:1",
        source="llm",
        created_at="2099-03-05T00:00:00+09:00",
        title="Summary",
        body="- item",
        action_item=None,
        metadata_json=attach_provenance({}, source="llm_inferred", confidence="medium"),
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2099-03-05T00:00:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "new_items": 3,
                "action_required": 0,
                "last_success_at": "2099-03-05T00:00:00+09:00",
            }
        },
    )

    output = render_dashboard_snapshot(db=db, storage_root_dir=tmp_path / "storage")
    parsed = json.loads(Path(output["data_path"]).read_text(encoding="utf-8"))

    assert parsed["sync_dashboard"]["action_required_count"] >= 1
    assert parsed["upcoming_events"][0]["provenance"]["source"] == "portal_uos_timetable"
    assert parsed["due_tasks"][0]["provenance"]["source"] == "telegram_draft"
    assert parsed["summaries"][0]["provenance"]["source"] == "llm_inferred"
