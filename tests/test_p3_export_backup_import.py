from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.db import Database


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        database_path=tmp_path / "sidae.db",
        storage_root_dir=tmp_path / "storage",
    )


def test_export_and_import_round_trip(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2026-03-05T12:00:00+09:00",
        title="HW",
        status="open",
        metadata_json={"token_like": "abc"},
    )
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    runner = CliRunner()
    export_path = tmp_path / "export.json"

    exported = runner.invoke(cli.app, ["export", "--json-out", str(export_path)])
    assert exported.exit_code == 0
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["tasks"][0]["external_id"] == "uclass:task:1"

    db2 = Database(tmp_path / "imported.db")
    db2.init()
    settings2 = SimpleNamespace(database_path=tmp_path / "imported.db", storage_root_dir=tmp_path / "storage")
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings2)
    imported = runner.invoke(cli.app, ["import", "--json", str(export_path)])
    assert imported.exit_code == 0
    assert len(db2.list_tasks(open_only=False, limit=10)) == 1


def test_backup_creates_zip_in_local_storage(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    (settings.storage_root_dir / "backups").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["backup"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    zip_path = Path(payload["zip"])
    assert zip_path.exists()
