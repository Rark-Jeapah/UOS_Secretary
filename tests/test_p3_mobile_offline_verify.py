from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.db import Database, now_utc_iso


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        database_path=tmp_path / "sidae.db",
        storage_root_dir=tmp_path / "storage",
    )


def _write_dashboard_state(
    db: Database,
    tmp_path: Path,
    last_run_at: str,
    *,
    include_materials: bool = True,
) -> Path:
    storage_root = tmp_path / "storage"
    dashboard_dir = storage_root / "publish" / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    (storage_root / "backups").mkdir(parents=True, exist_ok=True)
    (storage_root / "browser_profiles").mkdir(parents=True, exist_ok=True)
    if include_materials:
        (storage_root / "materials").mkdir(parents=True, exist_ok=True)
    (dashboard_dir / "index.html").write_text("ok", encoding="utf-8")
    db.update_sync_state(
        "publish_dashboard",
        last_run_at=last_run_at,
        last_cursor_json={"output": str(dashboard_dir)},
    )
    return dashboard_dir


def _record_material_artifact(
    db: Database,
    path: Path,
    *,
    exists: bool = True,
    external_id: str = "uclass:artifact:1",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        path.write_text("artifact", encoding="utf-8")
    db.record_artifact(
        external_id=external_id,
        source="uclass",
        filename=path.name,
        icloud_path=str(path),
        content_hash="hash-1",
        metadata_json={},
    )


def test_verify_mobile_offline_flags_stale_snapshot(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db = Database(tmp_path / "sidae.db")
    db.init()
    _write_dashboard_state(db=db, tmp_path=tmp_path, last_run_at="2026-01-01T00:00:00+00:00")
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["verify", "mobile-offline", "--max-age-hours", "2"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["publish_dashboard"]["fresh"] is False
    assert payload["storage_health"]["ok"] is True
    assert payload["manual_checklist"][0].startswith("Open the published dashboard")


def test_verify_mobile_offline_ok_when_snapshot_and_materials_are_ready(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    db = Database(tmp_path / "sidae.db")
    db.init()
    _write_dashboard_state(db=db, tmp_path=tmp_path, last_run_at=now_utc_iso())
    _record_material_artifact(
        db=db,
        path=tmp_path / "storage" / "materials" / "week1.pdf",
        exists=True,
    )
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["verify", "mobile-offline", "--max-age-hours", "24"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["storage_health"]["ok"] is True
    assert payload["materials"]["ok"] is True
    assert payload["materials"]["artifact_count_with_path"] == 1


def test_verify_mobile_offline_fails_when_materials_are_missing(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db = Database(tmp_path / "sidae.db")
    db.init()
    _write_dashboard_state(
        db=db,
        tmp_path=tmp_path,
        last_run_at=now_utc_iso(),
        include_materials=False,
    )
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["verify", "mobile-offline", "--max-age-hours", "24"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["publish_dashboard"]["fresh"] is True
    assert payload["materials"]["ok"] is False
    assert payload["materials"]["materials_dir_exists"] is False

