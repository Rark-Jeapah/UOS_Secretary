from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
from typing import Optional

import typer


def _cli_module():
    return importlib.import_module("sidae_secretary.cli")


def ops_snapshot(
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    max_users: int = typer.Option(
        40,
        "--max-users",
        min=1,
        max=500,
        help="Maximum active users to render per instance.",
    ),
    refresh_interval_sec: int = typer.Option(
        15,
        "--refresh-interval-sec",
        min=1,
        max=3600,
        help="Suggested client refresh interval in seconds.",
    ),
    log_file_limit: int = typer.Option(
        8,
        "--log-file-limit",
        min=1,
        max=32,
        help="Maximum number of log files to include.",
    ),
    log_tail_lines: int = typer.Option(
        30,
        "--log-tail-lines",
        min=1,
        max=200,
        help="Tail lines to keep per log file.",
    ),
) -> None:
    """Emit the local ops dashboard snapshot as JSON."""
    cli = _cli_module()
    cli.configure_logging()
    payload = cli.build_ops_dashboard_snapshot(
        config_file=config_file,
        max_users=max_users,
        log_file_limit=log_file_limit,
        log_tail_lines=log_tail_lines,
        refresh_interval_sec=refresh_interval_sec,
    )
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def ops_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8793, "--port", min=1, max=65535, help="Bind port."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    max_users: int = typer.Option(
        40,
        "--max-users",
        min=1,
        max=500,
        help="Maximum active users to render per instance.",
    ),
    refresh_interval_sec: int = typer.Option(
        15,
        "--refresh-interval-sec",
        min=1,
        max=3600,
        help="Browser auto-refresh interval in seconds.",
    ),
    log_file_limit: int = typer.Option(
        8,
        "--log-file-limit",
        min=1,
        max=32,
        help="Maximum number of log files to include.",
    ),
    log_tail_lines: int = typer.Option(
        30,
        "--log-tail-lines",
        min=1,
        max=200,
        help="Tail lines to keep per log file.",
    ),
) -> None:
    """Serve a local-only ops dashboard for live monitoring."""
    cli = _cli_module()
    cli.configure_logging()
    server = cli.build_ops_dashboard_http_server(
        host=host,
        port=port,
        config_file=config_file,
        max_users=max_users,
        log_file_limit=log_file_limit,
        log_tail_lines=log_tail_lines,
        refresh_interval_sec=refresh_interval_sec,
    )
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "service": "sidae-ops-dashboard",
                "host": host,
                "port": port,
                "url": f"http://{host}:{port}",
                "healthz": f"http://{host}:{port}/healthz",
                "snapshot_api": f"http://{host}:{port}/api/snapshot",
                "refresh_interval_sec": refresh_interval_sec,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def ops_open_remote(
    ssh_host: str = typer.Option(
        "",
        "--ssh-host",
        help="SSH host, local DNS name, or Tailscale name for the runtime Mac.",
    ),
    ssh_user: str = typer.Option(
        "",
        "--ssh-user",
        help="Optional SSH user. Leave empty to use the SSH client default.",
    ),
    ssh_port: int = typer.Option(
        0,
        "--ssh-port",
        min=0,
        max=65535,
        help="SSH port. Uses config/default 22 when omitted.",
    ),
    remote_host: str = typer.Option(
        "",
        "--remote-host",
        help="Dashboard bind host on the remote Mac. Defaults to config or 127.0.0.1.",
    ),
    remote_port: int = typer.Option(
        0,
        "--remote-port",
        min=0,
        max=65535,
        help="Dashboard port on the remote Mac. Uses config/default 8793 when omitted.",
    ),
    local_port: int = typer.Option(
        0,
        "--local-port",
        min=0,
        max=65535,
        help="Local forwarded port on this Mac. Uses config/default 8793 when omitted.",
    ),
    url_path: str = typer.Option(
        "",
        "--url-path",
        help="Path to open after the local tunnel is ready. Defaults to config or '/'.",
    ),
    ready_timeout_sec: int = typer.Option(
        10,
        "--ready-timeout-sec",
        min=1,
        max=120,
        help="Seconds to wait for the local forwarded port to become ready.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open-browser/--no-browser",
        help="Open the forwarded dashboard URL in the local browser.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Open the remote local-only ops dashboard through an SSH tunnel."""
    cli = _cli_module()
    settings = cli.load_settings(config_file=config_file)
    resolved_ssh_host = str(ssh_host or settings.ops_dashboard_ssh_host or "").strip()
    if not resolved_ssh_host:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "OPS_DASHBOARD_SSH_HOST missing",
                    "hint": "Set OPS_DASHBOARD_SSH_HOST in config/.env or pass --ssh-host.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=2)
    resolved_ssh_user = str(ssh_user or settings.ops_dashboard_ssh_user or "").strip() or None
    resolved_ssh_port = int(ssh_port or settings.ops_dashboard_ssh_port or 22)
    resolved_remote_host = (
        str(remote_host or settings.ops_dashboard_remote_host or "127.0.0.1").strip()
        or "127.0.0.1"
    )
    resolved_remote_port = int(remote_port or settings.ops_dashboard_remote_port or 8793)
    resolved_local_port = int(local_port or settings.ops_dashboard_local_port or 8793)
    resolved_url_path = cli._normalize_ops_dashboard_url_path(
        url_path or settings.ops_dashboard_url_path or "/"
    )
    ssh_target = cli._ops_dashboard_ssh_target(
        ssh_host=resolved_ssh_host,
        ssh_user=resolved_ssh_user,
    )
    tunnel_command = cli._ops_dashboard_tunnel_command(
        ssh_target=ssh_target,
        ssh_port=resolved_ssh_port,
        local_port=resolved_local_port,
        remote_host=resolved_remote_host,
        remote_port=resolved_remote_port,
    )
    local_url = f"http://127.0.0.1:{resolved_local_port}{resolved_url_path}"
    process = subprocess.Popen(
        tunnel_command,
        text=True,
    )
    if not cli._wait_for_local_port(
        "127.0.0.1",
        resolved_local_port,
        timeout_sec=float(ready_timeout_sec),
    ):
        if process.poll() is not None:
            return_code = process.returncode
        else:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            return_code = process.returncode
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "ssh tunnel did not become ready",
                    "ssh_target": ssh_target,
                    "local_port": resolved_local_port,
                    "remote_dashboard": f"{resolved_remote_host}:{resolved_remote_port}",
                    "returncode": return_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "ssh_target": ssh_target,
                "ssh_port": resolved_ssh_port,
                "local_url": local_url,
                "local_port": resolved_local_port,
                "remote_dashboard": f"{resolved_remote_host}:{resolved_remote_port}",
                "browser_opened": bool(open_browser),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if open_browser:
        cli._open_browser_url(local_url)
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        return
    if return_code != 0:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "ssh tunnel exited unexpectedly",
                    "ssh_target": ssh_target,
                    "returncode": return_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1)


def register_cli_ops_commands(ops_app: typer.Typer) -> None:
    ops_app.command("snapshot")(ops_snapshot)
    ops_app.command("serve")(ops_serve)
    ops_app.command("open-remote")(ops_open_remote)
