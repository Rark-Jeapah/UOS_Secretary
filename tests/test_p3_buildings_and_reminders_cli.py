from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.buildings import UOS_BUILDING_MAP
from sidae_secretary.db import Database, now_utc_iso


def test_buildings_and_reminders_cli_flow(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    set_result = runner.invoke(
        cli.app,
        ["buildings", "set", "--number", "20", "--name", "Science Hall"],
    )
    assert set_result.exit_code == 0
    set_payload = json.loads(set_result.stdout)
    assert set_payload["ok"] is True
    assert set_payload["building"]["building_no"] == "20"

    csv_path = tmp_path / "buildings.csv"
    csv_path.write_text("number,name\n21,Main Hall\n", encoding="utf-8")
    import_result = runner.invoke(
        cli.app,
        ["buildings", "import", "--csv", str(csv_path)],
    )
    assert import_result.exit_code == 0
    import_payload = json.loads(import_result.stdout)
    assert import_payload["imported"] == 1

    list_result = runner.invoke(cli.app, ["buildings", "list"])
    assert list_result.exit_code == 0
    list_payload = json.loads(list_result.stdout)
    mapped = {row["building_no"]: row["building_name"] for row in list_payload["items"]}
    assert list_payload["count"] >= len(UOS_BUILDING_MAP)
    assert mapped["20"] == "Science Hall"
    assert mapped["21"] == "Main Hall"

    db.upsert_telegram_reminder(
        external_id="tg-reminder:test-cli",
        chat_id="12345",
        run_at=now_utc_iso(),
        message="test reminder",
        metadata_json={"source": "test"},
    )
    reminders_result = runner.invoke(
        cli.app,
        ["reminders", "list", "--status", "pending"],
    )
    assert reminders_result.exit_code == 0
    reminders_payload = json.loads(reminders_result.stdout)
    assert reminders_payload["count"] >= 1


def test_buildings_seed_uos_imports_default_map(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["buildings", "seed-uos"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["inserted"] == 0
    assert payload["updated"] == 0
    assert payload["skipped"] == len(UOS_BUILDING_MAP)

    all_buildings = db.list_buildings(limit=5000)
    mapped = {row["building_no"]: row["building_name"] for row in all_buildings}
    assert mapped.get("37") == "100주년 기념관"
