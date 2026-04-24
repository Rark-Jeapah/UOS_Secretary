from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.db import Database


class _DoctorSettings(SimpleNamespace):
    def required_missing(self) -> list[str]:
        return []

    def as_doctor_dict(self) -> dict[str, str]:
        return {
            "DATABASE_PATH": str(self.database_path),
            "ICLOUD_DIR": str(self.icloud_dir) if self.icloud_dir else "",
        }


def _settings(tmp_path: Path) -> _DoctorSettings:
    return _DoctorSettings(
        database_path=tmp_path / "sidae.db",
        icloud_dir=None,
    )


def test_doctor_runtime_report_includes_python_and_ssl_keys(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(cli.ssl, "OPENSSL_VERSION", "OpenSSL 3.2.0")

    report = cli._doctor_readiness_report(settings=settings, db=db)

    assert report["runtime"]["python"]["ok"] is True
    assert report["runtime"]["python"]["required_min"] == "3.11"
    assert report["runtime"]["ssl"]["backend"] == "OpenSSL"
    assert report["runtime"]["ssl"]["ok"] is True


def test_doctor_fails_with_clear_message_when_python_is_too_old(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli.sys, "version_info", (3, 10, 12))
    monkeypatch.setattr(cli.ssl, "OPENSSL_VERSION", "LibreSSL 2.8.3")

    report = cli._doctor_readiness_report(settings=settings, db=db)

    assert report["ok"] is False
    assert report["runtime"]["python"]["ok"] is False
    assert "Python 3.11+ is required" in str(report["runtime"]["python"]["error"])
    assert report["runtime"]["ssl"]["backend"] == "LibreSSL"


def test_doctor_readiness_report_includes_assistant_feature_flags_and_warning(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    settings.telegram_assistant_enabled = False
    settings.telegram_assistant_write_enabled = True
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(cli.ssl, "OPENSSL_VERSION", "OpenSSL 3.2.0")

    report = cli._doctor_readiness_report(settings=settings, db=db)

    assert report["feature_flags"]["TELEGRAM_ASSISTANT_ENABLED"] is False
    assert report["feature_flags"]["TELEGRAM_ASSISTANT_WRITE_ENABLED"] is True
    assert (
        "TELEGRAM_ASSISTANT_WRITE_ENABLED requires TELEGRAM_ASSISTANT_ENABLED"
        in report["operational"]["warnings"]
    )


def test_doctor_operational_report_flags_beta_scope_gaps(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    settings.instance_name = "beta"
    settings.telegram_enabled = True
    settings.telegram_allowed_chat_ids = []
    settings.telegram_commands_enabled = True
    settings.onboarding_public_base_url = "http://connect.example.invalid"
    settings.onboarding_allowed_school_slugs = []
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(cli.ssl, "OPENSSL_VERSION", "OpenSSL 3.2.0")

    report = cli._doctor_readiness_report(settings=settings, db=db)

    assert report["operational"]["is_beta_instance"] is True
    assert report["operational"]["telegram_allowlist_configured"] is False
    assert report["operational"]["onboarding_https_ready"] is False
    assert report["operational"]["uos_beta_scope"] is False
    assert "TELEGRAM_ALLOWED_CHAT_IDS is empty" in report["operational"]["warnings"]
    assert (
        "Beta instance should enable TELEGRAM_ASSISTANT_ENABLED for /bot validation"
        in report["operational"]["warnings"]
    )
    assert "Beta instance should set ONBOARDING_ALLOWED_SCHOOL_SLUGS" in report["operational"]["warnings"]


def test_doctor_operational_report_flags_beta_bot_readiness_gaps(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    settings.instance_name = "beta"
    settings.telegram_enabled = True
    settings.telegram_allowed_chat_ids = ["12345"]
    settings.telegram_commands_enabled = True
    settings.onboarding_public_base_url = "https://connect.example.invalid"
    settings.onboarding_allowed_school_slugs = ["uos_online_class", "uos_portal"]
    settings.telegram_assistant_enabled = True
    settings.telegram_assistant_write_enabled = False
    settings.llm_enabled = False
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(cli.ssl, "OPENSSL_VERSION", "OpenSSL 3.2.0")

    report = cli._doctor_readiness_report(settings=settings, db=db)

    assert (
        "Beta instance keeps /bot in read-only mode; enable TELEGRAM_ASSISTANT_WRITE_ENABLED to validate write flows"
        in report["operational"]["warnings"]
    )
    assert (
        "Beta instance should enable LLM_ENABLED when /bot assistant is on"
        in report["operational"]["warnings"]
    )


def test_doctor_does_not_require_legacy_uclass_config_when_onboarding_is_ready(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    settings.storage_root_dir = tmp_path / "storage"
    settings.timezone = "Asia/Seoul"
    settings.uclass_ws_base = ""
    settings.uclass_wstoken = ""
    settings.uclass_username = ""
    settings.uclass_password = ""
    settings.onboarding_public_base_url = "https://connect.example.invalid"
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(cli.ssl, "OPENSSL_VERSION", "OpenSSL 3.2.0")

    report = cli._doctor_readiness_report(settings=settings, db=db)

    assert report["missing_required_config"] == []
    assert report["ok"] is True


def test_doctor_output_includes_secret_store_summary(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
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
    monkeypatch.setattr(
        cli,
        "secret_store_report",
        lambda settings=None: {
            "configured_backend": "default",
            "preferred_backend": "keychain",
            "active_backend": "keychain",
            "keychain_available": True,
            "file_fallback_enabled": False,
            "legacy_file_read_compat": True,
            "write_ready": True,
        },
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Secret Store" in result.stdout
    assert "active_backend: keychain" in result.stdout


def test_doctor_json_includes_beta_ops_health(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    monkeypatch.setattr(
        cli,
        "build_beta_ops_health_report",
        lambda settings, db: {
            "overall_ready": False,
            "ready_count": 2,
            "not_ready_count": 4,
            "surfaces": {
                "uos_official_api": {"status": "not_configured", "ready": False},
                "uclass_sync": {"status": "ready", "ready": True},
            },
        },
    )

    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "health" in payload
    assert payload["health"]["overall_ready"] is False
    assert payload["health"]["surfaces"]["uclass_sync"]["ready"] is True
