from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.db import Database


def test_verify_closed_loop_runs_doctor_then_sync_then_mobile_offline(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    monkeypatch.setattr(cli, "_load_settings_and_init_db", lambda config_file=None: (settings, db))

    order: list[str] = []

    def _doctor(settings, db):
        order.append("doctor")
        return {"ok": True}

    def _sync(settings, db, wait=False, timeout_seconds=None):
        order.append("sync")
        assert wait is True
        assert timeout_seconds == 321
        return {"ok": True, "stats": {}, "errors": []}

    def _mobile(settings, db, max_age_hours, materials_check_limit=5, materials_check_all=False):
        order.append("mobile_offline")
        return {"ok": True}

    monkeypatch.setattr(cli, "_doctor_readiness_report", _doctor)
    monkeypatch.setattr(cli, "_run_sync_all_once", _sync)
    monkeypatch.setattr(cli, "_mobile_offline_report", _mobile)

    result = runner.invoke(
        cli.app,
        ["verify", "closed-loop", "--timeout-seconds", "321"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert order == ["doctor", "sync", "mobile_offline"]
    assert set(payload["subreports"].keys()) == {"doctor", "sync", "mobile_offline"}


def test_verify_closed_loop_keeps_exit_code_4_when_sync_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    monkeypatch.setattr(cli, "_load_settings_and_init_db", lambda config_file=None: (settings, db))
    monkeypatch.setattr(cli, "_doctor_readiness_report", lambda settings, db: {"ok": True})

    def _sync_raises(settings, db, wait=False, timeout_seconds=None):
        raise cli._SyncLockBusyError("held")

    monkeypatch.setattr(cli, "_run_sync_all_once", _sync_raises)

    result = runner.invoke(cli.app, ["verify", "closed-loop"])

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["subreports"]["sync"]["error"] == "sync_lock_held"
    assert payload["subreports"]["sync"]["lock_path"] == str(
        cli._sync_all_lock_path(settings.database_path)
    )


def test_verify_closed_loop_keeps_exit_code_4_on_sync_lock_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(database_path=tmp_path / "sidae.db")
    monkeypatch.setattr(cli, "_load_settings_and_init_db", lambda config_file=None: (settings, db))
    monkeypatch.setattr(cli, "_doctor_readiness_report", lambda settings, db: {"ok": True})

    def _sync_raises(settings, db, wait=False, timeout_seconds=None):
        raise cli._SyncLockTimeoutError(Path("/tmp/sidae.lock"), 0.25)

    monkeypatch.setattr(cli, "_run_sync_all_once", _sync_raises)

    result = runner.invoke(cli.app, ["verify", "closed-loop"])

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["subreports"]["sync"]["error"] == "sync_lock_timeout"
    assert payload["subreports"]["sync"]["lock_path"] == "/tmp/sidae.lock"
    assert float(payload["subreports"]["sync"]["waited_seconds"]) >= 0.25
