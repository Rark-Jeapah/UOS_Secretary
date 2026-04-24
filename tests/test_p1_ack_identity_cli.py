from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.config import load_settings
from sidae_secretary.db import Database


def test_ack_identity_command_persists_ack(tmp_path: Path) -> None:
    runner = CliRunner()
    config_file = tmp_path / "config.toml"
    config_file.write_text("[sidae]\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "ack",
            "identity",
            "--token",
            "manual-ack-token",
            "--expires-hours",
            "2",
            "--config-file",
            str(config_file),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["ack"]["token"] == "manual-ack-token"
    assert payload["ack"]["expires_at"]

    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    active = db.get_active_identity_ack()
    assert active is not None
    assert active["token"] == "manual-ack-token"
