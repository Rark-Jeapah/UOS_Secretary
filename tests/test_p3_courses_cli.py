from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.db import Database


def test_courses_cli_list_resolve_and_alias_flow(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    db.upsert_course(
        canonical_course_id="uclass:uclass-example:101",
        source="uclass",
        external_course_id="101",
        display_name="Algorithms",
        metadata_json={"semester": "2026-1"},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="알고리즘",
        alias_type="uclass",
        source="uclass",
        metadata_json={},
    )

    listed = runner.invoke(cli.app, ["courses", "list", "--aliases"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.stdout)
    assert listed_payload["count"] == 1
    assert listed_payload["items"][0]["alias_count"] == 1
    assert listed_payload["items"][0]["aliases"][0]["alias"] == "알고리즘"

    added = runner.invoke(
        cli.app,
        ["courses", "alias-add", "--course", "Algorithms", "--alias", "알고리즘개론"],
    )
    assert added.exit_code == 0
    added_payload = json.loads(added.stdout)
    assert added_payload["ok"] is True
    assert added_payload["alias"]["canonical_course_id"] == "uclass:uclass-example:101"

    resolved = runner.invoke(
        cli.app,
        ["courses", "resolve", "--alias", "알고리즘개론"],
    )
    assert resolved.exit_code == 0
    resolved_payload = json.loads(resolved.stdout)
    assert resolved_payload["count"] == 1
    assert resolved_payload["items"][0]["canonical_course_id"] == "uclass:uclass-example:101"

    removed = runner.invoke(
        cli.app,
        ["courses", "alias-remove", "--course", "uclass:uclass-example:101", "--alias", "알고리즘개론"],
    )
    assert removed.exit_code == 0
    removed_payload = json.loads(removed.stdout)
    assert removed_payload["deleted"] == 1

    resolved_missing = runner.invoke(
        cli.app,
        ["courses", "resolve", "--alias", "알고리즘개론"],
    )
    assert resolved_missing.exit_code == 1
    resolved_missing_payload = json.loads(resolved_missing.stdout)
    assert resolved_missing_payload["ok"] is False
    assert resolved_missing_payload["count"] == 0


def test_courses_cli_alias_add_rejects_conflicts(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    db.upsert_course(
        canonical_course_id="uclass:uclass-example:101",
        source="uclass",
        external_course_id="101",
        display_name="Algorithms A",
        metadata_json={},
    )
    db.upsert_course(
        canonical_course_id="uclass:uclass-example:102",
        source="uclass",
        external_course_id="102",
        display_name="Algorithms B",
        metadata_json={},
    )
    db.upsert_course_alias(
        canonical_course_id="uclass:uclass-example:101",
        alias="알고리즘",
        alias_type="manual",
        source="cli",
        metadata_json={},
    )

    result = runner.invoke(
        cli.app,
        ["courses", "alias-add", "--course", "Algorithms B", "--alias", "알고리즘"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "alias_conflict"
    assert payload["conflict_course_ids"] == ["uclass:uclass-example:101"]
