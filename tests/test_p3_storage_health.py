from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli


class _DoctorSettings(SimpleNamespace):
    def required_missing(self) -> list[str]:
        return []

    def as_doctor_dict(self) -> dict[str, str]:
        return {
            "STORAGE_ROOT_DIR": str(self.storage_root_dir) if self.storage_root_dir else "",
            "DATABASE_PATH": str(self.database_path),
        }


def _settings(tmp_path: Path, storage_root_dir: Path) -> _DoctorSettings:
    return _DoctorSettings(
        database_path=tmp_path / "sidae.db",
        storage_root_dir=storage_root_dir,
        onboarding_browser_profiles_dir=storage_root_dir / "browser_profiles",
    )


def test_doctor_fix_repairs_missing_storage_directories(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    storage_root_dir = tmp_path / "storage"
    settings = _settings(tmp_path, storage_root_dir)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    before = runner.invoke(cli.app, ["status"])
    assert before.exit_code == 0
    before_payload = json.loads(before.stdout)
    assert before_payload["storage_health"]["exists"] is False
    assert before_payload["storage_health"]["ok"] is False

    monkeypatch.setattr(
        cli,
        "_runtime_environment_report",
        lambda: {
            "python": {
                "ok": True,
                "required_min": "3.11",
                "current": "3.11.9",
                "error": None,
            },
            "ssl": {
                "ok": True,
                "backend": "OpenSSL",
                "version": "OpenSSL 3.2.0",
                "warning": None,
            },
        },
    )
    fixed = runner.invoke(cli.app, ["doctor", "--fix"])
    assert fixed.exit_code == 0

    assert (storage_root_dir / "publish" / "dashboard").exists()
    assert (storage_root_dir / "materials").exists()
    assert (storage_root_dir / "backups").exists()
    assert (storage_root_dir / "browser_profiles").exists()

    after = runner.invoke(cli.app, ["status"])
    assert after.exit_code == 0
    after_payload = json.loads(after.stdout)
    assert after_payload["storage_health"]["exists"] is True
    assert after_payload["storage_health"]["writable"] is True
    assert after_payload["storage_health"]["ok"] is True


def test_status_reports_non_writable_storage_root(tmp_path: Path, monkeypatch) -> None:
    if os.name == "nt":
        return

    runner = CliRunner()
    storage_root_dir = tmp_path / "storage"
    (storage_root_dir / "publish" / "dashboard").mkdir(parents=True)
    (storage_root_dir / "materials").mkdir(parents=True)
    (storage_root_dir / "backups").mkdir(parents=True)
    (storage_root_dir / "browser_profiles").mkdir(parents=True)

    settings = _settings(tmp_path, storage_root_dir)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    storage_root_dir.chmod(0o555)
    try:
        result = runner.invoke(cli.app, ["status"])
    finally:
        storage_root_dir.chmod(0o755)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["storage_health"]["exists"] is True
    assert payload["storage_health"]["writable"] is False
    assert payload["storage_health"]["ok"] is False
