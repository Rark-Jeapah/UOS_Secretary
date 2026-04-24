from __future__ import annotations

import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli


def _install_result(monkeypatch, plist_path: Path):
    monkeypatch.setattr(
        cli,
        "_launchd_plist_path",
        lambda label, scope="agent": plist_path,
    )
    monkeypatch.setattr(
        cli,
        "_launchctl_bootout",
        lambda plist_path, scope="agent": None,
    )
    monkeypatch.setattr(
        cli,
        "_launchctl_bootstrap",
        lambda plist_path, scope="agent": SimpleNamespace(
            returncode=0, stderr="", stdout=""
        ),
    )


def test_launchd_install_program_arguments_include_wait_and_timeout_default(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(cli.app, ["launchd", "install", "--time", "06:00"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["sync_timeout_seconds"] == 600
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    args = plist_payload["ProgramArguments"]
    assert args[:5] == [cli.sys.executable, "-m", "sidae_secretary.cli", "sync", "--all"]
    assert "--wait" in args
    assert "--timeout" in args
    timeout_idx = args.index("--timeout")
    assert args[timeout_idx + 1] == "600"
    assert "--config-file" in args
    config_idx = args.index("--config-file")
    expected_config = (tmp_path / "config.toml").resolve()
    assert Path(args[config_idx + 1]) == expected_config
    assert plist_payload["WorkingDirectory"] == str(expected_config.parent)


def test_launchd_install_allows_sync_timeout_override(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.plist"
    _install_result(monkeypatch, plist_path)
    config_file = tmp_path / "deploy" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[sidae]\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "launchd",
            "install",
            "--time",
            "06:00",
            "--sync-timeout-seconds",
            "123",
            "--config-file",
            str(config_file.relative_to(tmp_path)),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sync_timeout_seconds"] == 123
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    args = plist_payload["ProgramArguments"]
    timeout_idx = args.index("--timeout")
    assert args[timeout_idx + 1] == "123"
    assert "--config-file" in args
    config_idx = args.index("--config-file")
    assert Path(args[config_idx + 1]) == config_file.resolve()
    assert plist_payload["WorkingDirectory"] == str(config_file.resolve().parent)


def test_launchd_install_supports_multiple_daily_times(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install", "--time", "07:30,21:00"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["times"] == ["07:30", "21:00"]
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    start = plist_payload["StartCalendarInterval"]
    assert isinstance(start, list)
    assert start == [
        {"Hour": 7, "Minute": 30},
        {"Hour": 21, "Minute": 0},
    ]


def test_launchd_install_telegram_poller_uses_start_interval(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.telegram-poller.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install-telegram-poller", "--interval-minutes", "5"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["interval_minutes"] == 5
    assert payload["start_interval_seconds"] == 300
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["StartInterval"] == 300
    args = plist_payload["ProgramArguments"]
    assert args[:4] == [cli.sys.executable, "-m", "sidae_secretary.cli", "sync-telegram"]
    assert "--wait" in args
    assert "--timeout" in args


def test_launchd_install_uclass_poller_uses_keepalive_and_daemon_args(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.uclass-poller.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        [
            "launchd",
            "install-uclass-poller",
            "--interval-minutes",
            "60",
            "--connectivity-check-seconds",
            "25",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["interval_minutes"] == 60
    assert payload["connectivity_check_seconds"] == 25
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["KeepAlive"] is True
    assert plist_payload["RunAtLoad"] is True
    args = plist_payload["ProgramArguments"]
    assert args[:4] == [cli.sys.executable, "-m", "sidae_secretary.cli", "uclass-poller"]
    assert "--interval-minutes" in args
    assert "--connectivity-check-seconds" in args
    assert "--sync-timeout-seconds" in args


def test_launchd_install_telegram_listener_uses_keepalive_and_long_poll_args(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.telegram-listener.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        [
            "launchd",
            "install-telegram-listener",
            "--poll-timeout-seconds",
            "30",
            "--error-backoff-seconds",
            "7",
            "--max-consecutive-errors",
            "9",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["poll_timeout_seconds"] == 30
    assert payload["error_backoff_seconds"] == 7
    assert payload["max_consecutive_errors"] == 9
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["KeepAlive"] is True
    assert plist_payload["RunAtLoad"] is True
    args = plist_payload["ProgramArguments"]
    assert args[:4] == [cli.sys.executable, "-m", "sidae_secretary.cli", "telegram-listener"]
    assert "--poll-timeout-seconds" in args
    assert "--error-backoff-seconds" in args
    assert "--max-consecutive-errors" in args


def test_launchd_install_weather_sync_uses_hourly_calendar_interval(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.weather-sync.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install-weather-sync"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["interval_minutes"] == 60
    assert payload["minute_offset"] == 20
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    start = plist_payload["StartCalendarInterval"]
    assert isinstance(start, list)
    assert len(start) == 24
    assert start[0] == {"Hour": 0, "Minute": 20}
    assert start[-1] == {"Hour": 23, "Minute": 20}
    args = plist_payload["ProgramArguments"]
    assert args[:4] == [cli.sys.executable, "-m", "sidae_secretary.cli", "sync-weather"]
    assert "--wait" in args
    assert "--timeout" in args


def test_launchd_install_onboarding_uses_keepalive_and_bind_args(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.onboarding.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install-onboarding", "--host", "0.0.0.0", "--port", "8791"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["host"] == "0.0.0.0"
    assert payload["port"] == 8791
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["KeepAlive"] is True
    assert plist_payload["RunAtLoad"] is True
    args = plist_payload["ProgramArguments"]
    assert args[:5] == [cli.sys.executable, "-m", "sidae_secretary.cli", "onboarding", "serve"]
    assert "--host" in args
    assert "--port" in args
    assert "--config-file" in args


def test_launchd_install_ops_dashboard_uses_keepalive_and_bind_args(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.ops-dashboard.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        [
            "launchd",
            "install-ops-dashboard",
            "--host",
            "127.0.0.1",
            "--port",
            "8793",
            "--refresh-interval-sec",
            "20",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 8793
    assert payload["refresh_interval_sec"] == 20
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["KeepAlive"] is True
    assert plist_payload["RunAtLoad"] is True
    args = plist_payload["ProgramArguments"]
    assert args[:5] == [cli.sys.executable, "-m", "sidae_secretary.cli", "ops", "serve"]
    assert "--host" in args
    assert "--port" in args
    assert "--refresh-interval-sec" in args
    assert "--config-file" in args


def test_launchd_install_publish_uses_start_interval(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.publish.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install-publish", "--interval-minutes", "60"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["interval_minutes"] == 60
    assert payload["start_interval_seconds"] == 3600
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["StartInterval"] == 3600
    args = plist_payload["ProgramArguments"]
    assert args[:4] == [cli.sys.executable, "-m", "sidae_secretary.cli", "publish"]
    assert "--config-file" in args


def test_launchd_install_briefings_defaults_to_0900_and_2100(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.briefings.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install-briefings"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["times"] == ["09:00", "21:00"]
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    start = plist_payload["StartCalendarInterval"]
    assert isinstance(start, list)
    assert start == [
        {"Hour": 9, "Minute": 0},
        {"Hour": 21, "Minute": 0},
    ]
    args = plist_payload["ProgramArguments"]
    assert args[:4] == [cli.sys.executable, "-m", "sidae_secretary.cli", "send-briefings"]
    assert "--wait" in args
    assert "--timeout" in args


def test_launchd_install_relay_uses_keepalive_and_bind_args(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    plist_path = tmp_path / "com.sidae.secretary.briefing-relay.plist"
    _install_result(monkeypatch, plist_path)

    result = runner.invoke(
        cli.app,
        ["launchd", "install-relay", "--host", "0.0.0.0", "--port", "8787"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["host"] == "0.0.0.0"
    assert payload["port"] == 8787
    with plist_path.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["KeepAlive"] is True
    args = plist_payload["ProgramArguments"]
    assert args[:5] == [cli.sys.executable, "-m", "sidae_secretary.cli", "relay", "serve"]
    assert "--host" in args
    assert "--port" in args
    assert "--config-file" in args


def test_launchd_install_telegram_listener_uses_instance_name_from_config(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "beta" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text('[sidae]\nINSTANCE_NAME = "Beta App"\n', encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "_launchd_plist_path",
        lambda label, scope="agent": tmp_path / f"{label}.plist",
    )
    monkeypatch.setattr(
        cli,
        "_launchctl_bootout",
        lambda plist_path, scope="agent": None,
    )
    monkeypatch.setattr(
        cli,
        "_launchctl_bootstrap",
        lambda plist_path, scope="agent": SimpleNamespace(
            returncode=0, stderr="", stdout=""
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "launchd",
            "install-telegram-listener",
            "--config-file",
            str(config_file),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["instance_name"] == "beta-app"
    expected_plist = tmp_path / "com.sidae.secretary.telegram-listener.beta-app.plist"
    assert payload["plist_path"] == str(expected_plist)
    with expected_plist.open("rb") as fp:
        plist_payload = plistlib.load(fp)
    assert plist_payload["Label"] == "com.sidae.secretary.telegram-listener.beta-app"
    assert plist_payload["StandardOutPath"] == "/tmp/com.sidae.secretary.telegram-listener.beta-app.out.log"
    assert plist_payload["StandardErrorPath"] == "/tmp/com.sidae.secretary.telegram-listener.beta-app.err.log"


def test_launchd_uninstall_telegram_listener_supports_instance_name(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "_launchd_plist_path",
        lambda label, scope="agent": tmp_path / f"{label}.plist",
    )
    monkeypatch.setattr(
        cli,
        "_launchctl_bootout",
        lambda plist_path, scope="agent": None,
    )
    plist_path = tmp_path / "com.sidae.secretary.telegram-listener.beta.plist"
    plist_path.write_text("plist", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["launchd", "uninstall-telegram-listener", "--instance-name", "beta"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["plist_path"] == str(plist_path)
    assert payload["removed"] is True
    assert not plist_path.exists()
