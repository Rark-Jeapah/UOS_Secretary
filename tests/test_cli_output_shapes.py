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


def test_status_output_contains_expected_keys(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "counts" in payload
    assert "sync_state" in payload
    assert "health" in payload
    assert "deps" in payload
    assert "feature_flags" in payload
    assert "storage_health" in payload
    assert "secret_store" in payload
    assert "runtime" in payload
    assert "telegram_import_ok" in payload["deps"]
    assert "telegram_requests_import_ok" in payload["deps"]
    assert "telegram_dateutil_import_ok" in payload["deps"]
    assert "llm_import_ok" in payload["deps"]
    assert "llm_provider_supported" in payload["deps"]
    assert "llm_provider_import_ok" in payload["deps"]
    assert "icalendar_import_ok" in payload["deps"]
    assert "TELEGRAM_ASSISTANT_ENABLED" in payload["feature_flags"]
    assert "TELEGRAM_ASSISTANT_WRITE_ENABLED" in payload["feature_flags"]
    assert "active_backend" in payload["secret_store"]
    assert "events" in payload["counts"]
    assert "overall_ready" in payload["health"]
    assert "surfaces" in payload["health"]
    assert "uos_official_api" in payload["health"]["surfaces"]
    assert "uclass_sync" in payload["health"]["surfaces"]
    assert "telegram_listener" in payload["health"]["surfaces"]
    assert "telegram_send" in payload["health"]["surfaces"]
    assert "weather_sync" in payload["health"]["surfaces"]
    assert "notice_fetch" in payload["health"]["surfaces"]


def test_status_reports_telegram_assistant_feature_flags(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(
        database_path=tmp_path / "sidae.db",
        telegram_commands_enabled=True,
        telegram_smart_commands_enabled=False,
        telegram_assistant_enabled=True,
        telegram_assistant_write_enabled=False,
    )
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["feature_flags"]["TELEGRAM_COMMANDS_ENABLED"] is True
    assert payload["feature_flags"]["TELEGRAM_SMART_COMMANDS_ENABLED"] is False
    assert payload["feature_flags"]["TELEGRAM_ASSISTANT_ENABLED"] is True
    assert payload["feature_flags"]["TELEGRAM_ASSISTANT_WRITE_ENABLED"] is False


def test_storage_report_output_contains_expected_keys(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(
        database_path=tmp_path / "sidae.db",
        storage_root_dir=tmp_path / "storage",
    )
    db = Database(settings.database_path)
    db.init()
    (settings.storage_root_dir / "publish" / "dashboard").mkdir(parents=True, exist_ok=True)
    (settings.storage_root_dir / "materials").mkdir(parents=True, exist_ok=True)
    (settings.storage_root_dir / "backups").mkdir(parents=True, exist_ok=True)
    (settings.storage_root_dir / "browser_profiles").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["storage-report"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["storage_root"] == str(settings.storage_root_dir.resolve())
    assert "dashboard" in payload
    assert "materials" in payload
    assert "backups" in payload
    assert "browser_profiles" in payload
    assert "legacy_icloud_root" in payload


def test_uclass_probe_output_shape_no_crash(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    monkeypatch.setattr(
        cli,
        "run_uclass_probe",
        lambda settings, db, output_json_path=None: {
            "site_info": {"userid": 1, "sitename": "Demo"},
            "rows": [
                {
                    "status": "OK",
                    "key": "site_info",
                    "wsfunction": "core_webservice_get_site_info",
                    "error": None,
                }
            ],
        },
    )

    result = runner.invoke(cli.app, ["uclass", "probe"])

    assert result.exit_code == 0
    assert "WSFunction Probe Matrix" in result.stdout
    assert "status | key | wsfunction | error" in result.stdout


def test_status_marks_llm_dependency_not_ready_for_unsupported_provider(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    settings = SimpleNamespace(
        database_path=tmp_path / "sidae.db",
        llm_enabled=True,
        llm_provider="unsupported-provider",
    )
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    deps = payload["deps"]
    assert deps["llm_provider_supported"] is False
    assert deps["llm_provider_import_ok"] is False
    assert deps["llm_import_ok"] is False


def test_ops_snapshot_output_contains_expected_keys(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli,
        "build_ops_dashboard_snapshot",
        lambda **kwargs: {
            "generated_at": "2099-03-17T09:00:00+09:00",
            "host": "mini",
            "user": "runtime_user",
            "cwd": str(tmp_path),
            "python": "3.11.0",
            "refresh_interval_sec": 15,
            "instances": [
                {
                    "label": "prod",
                    "users": [{"chat_id": "12345"}],
                    "sync_dashboard": {"action_required_count": 1},
                }
            ],
            "services": {
                "counts": {"total": 2, "sidae": 1, "ollama": 1},
                "processes": [],
            },
            "logs": {"files": []},
            "llm": {"status": "ready", "configured_models": ["gemma4"]},
        },
    )

    result = runner.invoke(cli.app, ["ops", "snapshot"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["host"] == "mini"
    assert payload["instances"][0]["label"] == "prod"
    assert payload["services"]["counts"]["ollama"] == 1
    assert payload["llm"]["status"] == "ready"


def test_inbox_list_and_apply_flow_outputs_json_shape(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    row = db.upsert_inbox_item(
        external_id="telegram:update:1",
        source="telegram",
        received_at="2026-03-05T10:00:00+09:00",
        title="remember",
        body="remember this",
        item_type="note",
        draft_json={"title": "remember"},
        processed=False,
        metadata_json={},
    )
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    listed = runner.invoke(cli.app, ["inbox", "list"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.stdout)
    assert "count" in listed_payload
    assert "items" in listed_payload
    assert listed_payload["count"] >= 1

    item_id = db.list_unprocessed_inbox(limit=10)[0].id
    assert item_id is not None
    applied = runner.invoke(cli.app, ["inbox", "apply", "--id", str(item_id)])
    assert applied.exit_code == 0
    applied_payload = json.loads(applied.stdout)
    assert "processed" in applied_payload
    assert "notes" in applied_payload


def test_admin_refresh_user_outputs_json_shape(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    monkeypatch.setattr(
        cli,
        "refresh_beta_user",
        lambda settings, db, user_id=None, chat_id=None: {
            "ok": True,
            "scope": {"user_id": 7, "chat_id": "123"},
            "components": {"uclass_sync": {"ok": True}},
            "error_components": [],
            "skipped_components": [],
            "health": {"overall_ready": True, "surfaces": {}},
        },
    )

    result = runner.invoke(cli.app, ["admin", "refresh-user", "--user-id", "7"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "scope" in payload
    assert "components" in payload
    assert "health" in payload


def test_admin_last_failed_stage_outputs_json_shape(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init()
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    monkeypatch.setattr(
        cli,
        "inspect_last_failed_stage",
        lambda settings, db, component=None, user_id=None, chat_id=None: {
            "ok": False,
            "scope": {"user_id": 7, "chat_id": "123"},
            "match": {
                "component": "uclass_sync",
                "job_name": "sync_uclass",
                "stage": "uclass_ws:core_course_get_contents",
                "status": "error",
                "message": "token expired",
                "last_run_at": "2026-03-14T00:00:00+00:00",
                "scope": {"user_id": 7, "chat_id": "123"},
                "details": {},
            },
            "candidates": [],
        },
    )

    result = runner.invoke(cli.app, ["admin", "last-failed-stage", "--user-id", "7"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "scope" in payload
    assert "match" in payload
    assert payload["match"]["stage"] == "uclass_ws:core_course_get_contents"
