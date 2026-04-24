from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sidae_secretary import cli


class _FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.returncode = 0

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def test_ops_open_remote_uses_config_backed_ssh_tunnel_and_browser(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                'OPS_DASHBOARD_SSH_HOST = "runtime.local"',
                'OPS_DASHBOARD_SSH_USER = "runtime_user"',
                "OPS_DASHBOARD_SSH_PORT = 2201",
                'OPS_DASHBOARD_REMOTE_HOST = "127.0.0.1"',
                "OPS_DASHBOARD_REMOTE_PORT = 8793",
                "OPS_DASHBOARD_LOCAL_PORT = 9901",
                'OPS_DASHBOARD_URL_PATH = "healthz"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    calls: dict[str, object] = {}

    def _fake_popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return _FakeProcess(command)

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(cli, "_wait_for_local_port", lambda host, port, timeout_sec: True)
    monkeypatch.setattr(cli, "_open_browser_url", lambda url: calls.setdefault("opened_url", url))

    result = runner.invoke(
        cli.app,
        ["ops", "open-remote", "--config-file", str(config_file)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["ssh_target"] == "runtime_user@runtime.local"
    assert payload["ssh_port"] == 2201
    assert payload["local_url"] == "http://127.0.0.1:9901/healthz"
    assert payload["remote_dashboard"] == "127.0.0.1:8793"
    assert calls["opened_url"] == "http://127.0.0.1:9901/healthz"
    assert calls["command"] == [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        "9901:127.0.0.1:8793",
        "-p",
        "2201",
        "runtime_user@runtime.local",
    ]


def test_ops_open_remote_requires_configured_ssh_host(tmp_path: Path) -> None:
    runner = CliRunner()
    config_file = tmp_path / "config.toml"
    config_file.write_text("[sidae]\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["ops", "open-remote", "--config-file", str(config_file), "--no-browser"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "OPS_DASHBOARD_SSH_HOST missing"
