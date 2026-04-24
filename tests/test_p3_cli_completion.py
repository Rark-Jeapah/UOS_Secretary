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
    )


def test_tasks_done_and_ignore_commands(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2026-03-07T09:00:00+09:00",
        title="Essay",
        status="open",
        metadata_json={},
    )
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    runner = CliRunner()

    done = runner.invoke(cli.app, ["tasks", "done", "--id", "uclass:task:1"])
    assert done.exit_code == 0
    done_payload = json.loads(done.stdout)
    assert done_payload["task"]["status"] == "done"

    ignored = runner.invoke(cli.app, ["tasks", "ignore", "--id", "uclass:task:1"])
    assert ignored.exit_code == 0
    ignored_payload = json.loads(ignored.stdout)
    assert ignored_payload["task"]["status"] == "ignored"


def test_reviews_command_removed(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["reviews", "done", "--id", "review:portal:abc:D+1"])
    assert result.exit_code != 0
    error_output = result.stdout
    if hasattr(result, "stderr") and result.stderr:
        error_output += result.stderr
    assert "No such command" in error_output
