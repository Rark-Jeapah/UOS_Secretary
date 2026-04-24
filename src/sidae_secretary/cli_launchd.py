from __future__ import annotations

import importlib
import json
from pathlib import Path
import plistlib
from typing import Optional

import typer


def _cli_module():
    return importlib.import_module("sidae_secretary.cli")


def _resolved_launchd_context(
    *,
    config_file: Optional[Path],
    instance_name: str,
    base_label: str,
) -> tuple[object, Path, str, str, str, str]:
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    resolved_instance = cli._resolve_instance_name(
        config_file=resolved_config,
        instance_name=instance_name,
    )
    label = cli._launchd_label(base_label, resolved_instance)
    stdout_path, stderr_path = cli._launchd_log_paths(label)
    return cli, resolved_config, resolved_instance, label, stdout_path, stderr_path


def _install_launchd_job_and_echo(
    *,
    config_file: Optional[Path],
    instance_name: str,
    base_label: str,
    args: list[str],
    scope: str,
    run_as_user: Optional[str],
    extra: dict[str, object],
    keepalive: bool = False,
    start_interval: int | None = None,
    start_calendar_interval: dict[str, int] | list[dict[str, int]] | None = None,
) -> None:
    cli, resolved_config, _, label, stdout_path, stderr_path = _resolved_launchd_context(
        config_file=config_file,
        instance_name=instance_name,
        base_label=base_label,
    )
    plist_path, normalized_scope, daemon_user = cli._install_launchd_job(
        label=label,
        resolved_config=resolved_config,
        args=args,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        scope=scope,
        run_as_user=run_as_user,
        keepalive=keepalive,
        start_interval=start_interval,
        start_calendar_interval=start_calendar_interval,
    )
    cli._echo_launchd_install_result(
        plist_path=plist_path,
        resolved_config=resolved_config,
        scope=normalized_scope,
        run_as_user=daemon_user,
        extra=extra,
    )


def launchd_install(
    time_hhmm: str = typer.Option(
        ...,
        "--time",
        help="Daily run time in HH:MM (24h). Use comma-separated values like 09:00,21:00 for multiple runs.",
    ),
    sync_timeout_seconds: int = typer.Option(
        600,
        "--sync-timeout-seconds",
        min=1,
        help="Pass --timeout to `sidae sync --all --wait` in launchd job.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Write launch agent plist and load it with launchctl."""
    cli = _cli_module()
    raw_times = [part.strip() for part in str(time_hhmm or "").split(",") if part.strip()]
    if not raw_times:
        typer.echo("at least one --time value is required")
        raise typer.Exit(code=2)
    parsed_times: list[tuple[int, int]] = []
    normalized_times: list[str] = []
    try:
        for raw_time in raw_times:
            hour, minute = cli._parse_hhmm(raw_time)
            parsed_times.append((hour, minute))
            normalized_times.append(f"{hour:02d}:{minute:02d}")
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)

    try:
        normalized_scope = cli._normalize_launchd_scope(scope)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)

    resolved_config = cli.select_config_path(config_file=config_file)
    resolved_instance = cli._resolve_instance_name(
        config_file=resolved_config,
        instance_name=instance_name,
    )
    label = cli._launchd_label("com.sidae.secretary", resolved_instance)
    plist_path = cli._launchd_plist_path(label, normalized_scope)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "sync",
        "--all",
        "--wait",
        "--timeout",
        str(sync_timeout_seconds),
        "--config-file",
        str(resolved_config),
    ]

    start_calendar_interval: dict[str, int] | list[dict[str, int]]
    if len(parsed_times) == 1:
        hour, minute = parsed_times[0]
        start_calendar_interval = {"Hour": hour, "Minute": minute}
    else:
        start_calendar_interval = [
            {"Hour": hour, "Minute": minute} for hour, minute in parsed_times
        ]

    stdout_path, stderr_path = cli._launchd_log_paths(label)
    try:
        plist_payload, daemon_user = cli._launchd_job_payload(
            label=label,
            resolved_config=resolved_config,
            args=args,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            scope=normalized_scope,
            run_as_user=run_as_user,
            start_calendar_interval=start_calendar_interval,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)
    with plist_path.open("wb") as fp:
        plistlib.dump(plist_payload, fp, sort_keys=False)

    cli._launchctl_bootout(plist_path, normalized_scope)
    loaded = cli._launchctl_bootstrap(plist_path, normalized_scope)
    if loaded.returncode != 0:
        typer.echo(
            json.dumps(
                {"ok": False, "plist_path": str(plist_path), "error": loaded.stderr.strip()},
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    result = {
        "ok": True,
        "plist_path": str(plist_path),
        "time": normalized_times[0],
        "times": normalized_times,
        "sync_timeout_seconds": int(sync_timeout_seconds),
        "scope": normalized_scope,
        "launchd_domain": cli._launchd_domain(normalized_scope),
        "config_file": str(resolved_config),
        "working_directory": str(resolved_config.parent),
    }
    if daemon_user is not None:
        result["run_as_user"] = daemon_user
    if resolved_instance:
        result["instance_name"] = resolved_instance
    typer.echo(json.dumps(result, indent=2))


def launchd_install_uclass_poller(
    interval_minutes: int = typer.Option(
        60,
        "--interval-minutes",
        min=1,
        help="While connected, run UClass ingestion at this interval.",
    ),
    connectivity_check_seconds: int = typer.Option(
        30,
        "--connectivity-check-seconds",
        min=5,
        help="How often the long-running poller checks network reachability.",
    ),
    sync_timeout_seconds: int = typer.Option(
        600,
        "--sync-timeout-seconds",
        min=1,
        help="Pass --timeout to internal `sidae sync-uclass --wait` runs.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Install a long-running UClass-only poller launch agent."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "uclass-poller",
        "--interval-minutes",
        str(interval_minutes),
        "--connectivity-check-seconds",
        str(connectivity_check_seconds),
        "--sync-timeout-seconds",
        str(sync_timeout_seconds),
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.uclass-poller",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        keepalive=True,
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "interval_minutes": interval_minutes,
            "connectivity_check_seconds": connectivity_check_seconds,
            "sync_timeout_seconds": sync_timeout_seconds,
        },
    )


def launchd_install_telegram_poller(
    interval_minutes: int = typer.Option(
        5,
        "--interval-minutes",
        min=1,
        max=60,
        help="How often to run Telegram-only polling.",
    ),
    sync_timeout_seconds: int = typer.Option(
        120,
        "--sync-timeout-seconds",
        min=1,
        help="Pass --timeout to `sidae sync-telegram --wait` in launchd job.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Write launch agent plist for Telegram polling and load it with launchctl."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "sync-telegram",
        "--wait",
        "--timeout",
        str(sync_timeout_seconds),
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.telegram-poller",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        start_interval=int(interval_minutes * 60),
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "interval_minutes": interval_minutes,
            "start_interval_seconds": int(interval_minutes * 60),
            "sync_timeout_seconds": sync_timeout_seconds,
        },
    )


def launchd_install_telegram_listener(
    poll_timeout_seconds: int = typer.Option(
        10,
        "--poll-timeout-seconds",
        min=1,
        max=60,
        help="Telegram long-poll timeout per request.",
    ),
    error_backoff_seconds: int = typer.Option(
        2,
        "--error-backoff-seconds",
        min=1,
        help="Seconds to wait before retrying after a listener error.",
    ),
    max_consecutive_errors: int = typer.Option(
        6,
        "--max-consecutive-errors",
        min=1,
        help="Exit after this many Telegram error cycles in a row so launchd can restart the listener.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Install a KeepAlive launch agent for the long-running Telegram listener."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "telegram-listener",
        "--poll-timeout-seconds",
        str(poll_timeout_seconds),
        "--error-backoff-seconds",
        str(error_backoff_seconds),
        "--max-consecutive-errors",
        str(max_consecutive_errors),
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.telegram-listener",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        keepalive=True,
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "poll_timeout_seconds": poll_timeout_seconds,
            "error_backoff_seconds": error_backoff_seconds,
            "max_consecutive_errors": max_consecutive_errors,
        },
    )


def launchd_install_weather_sync(
    minute_offset: int = typer.Option(
        20,
        "--minute-offset",
        min=0,
        max=59,
        help=(
            "Minute past each hour to run weather + Seoul air-quality sync. "
            "Default 20 because the Seoul cleanair site can still show the previous hour during 00-15."
        ),
    ),
    sync_timeout_seconds: int = typer.Option(
        600,
        "--sync-timeout-seconds",
        min=1,
        help="Pass --timeout to `sidae sync-weather --wait` in launchd job.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Install a fixed hourly launchd job for weather + Seoul air-quality sync."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "sync-weather",
        "--wait",
        "--timeout",
        str(sync_timeout_seconds),
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.weather-sync",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        start_calendar_interval=cli._hourly_start_calendar_interval(minute_offset),
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "interval_minutes": 60,
            "minute_offset": int(minute_offset),
            "sync_timeout_seconds": int(sync_timeout_seconds),
        },
    )


def launchd_install_onboarding(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host passed to `sidae onboarding serve`.",
    ),
    port: int = typer.Option(
        8791,
        "--port",
        min=1,
        max=65535,
        help="Bind port passed to `sidae onboarding serve`.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Install a long-running launchd job for the Moodle onboarding web server."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "onboarding",
        "serve",
        "--host",
        str(host),
        "--port",
        str(port),
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.onboarding",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        keepalive=True,
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "host": str(host),
            "port": int(port),
        },
    )


def launchd_install_ops_dashboard(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host passed to `sidae ops serve`.",
    ),
    port: int = typer.Option(
        8793,
        "--port",
        min=1,
        max=65535,
        help="Bind port passed to `sidae ops serve`.",
    ),
    refresh_interval_sec: int = typer.Option(
        15,
        "--refresh-interval-sec",
        min=1,
        max=3600,
        help="Browser auto-refresh interval for the served ops dashboard.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Install a KeepAlive launchd job for the local-only ops dashboard."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "ops",
        "serve",
        "--host",
        str(host),
        "--port",
        str(port),
        "--refresh-interval-sec",
        str(refresh_interval_sec),
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.ops-dashboard",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        keepalive=True,
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "host": str(host),
            "port": int(port),
            "refresh_interval_sec": int(refresh_interval_sec),
        },
    )


def launchd_install_publish(
    interval_minutes: int = typer.Option(
        60,
        "--interval-minutes",
        min=1,
        help="How often to render the local dashboard snapshot.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Write launch agent plist for local dashboard publish and load it with launchctl."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "publish",
        "--config-file",
        str(resolved_config),
    ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.publish",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        start_interval=int(interval_minutes * 60),
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "interval_minutes": interval_minutes,
            "start_interval_seconds": int(interval_minutes * 60),
        },
    )


def launchd_install_briefings(
    time_hhmm: str = typer.Option(
        "09:00,21:00",
        "--time",
        help="Briefing send time(s) in HH:MM (24h). Defaults to 09:00,21:00.",
    ),
    sync_timeout_seconds: int = typer.Option(
        120,
        "--sync-timeout-seconds",
        min=1,
        help="Pass --timeout to `sidae send-briefings --wait` in launchd job.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Install a fixed-time launch agent for Telegram briefings."""
    cli = _cli_module()
    raw_times = [part.strip() for part in str(time_hhmm or "").split(",") if part.strip()]
    if not raw_times:
        typer.echo("at least one --time value is required")
        raise typer.Exit(code=2)
    parsed_times: list[tuple[int, int]] = []
    normalized_times: list[str] = []
    try:
        for raw_time in raw_times:
            hour, minute = cli._parse_hhmm(raw_time)
            parsed_times.append((hour, minute))
            normalized_times.append(f"{hour:02d}:{minute:02d}")
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)

    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "send-briefings",
        "--wait",
        "--timeout",
        str(sync_timeout_seconds),
        "--config-file",
        str(resolved_config),
    ]
    if len(parsed_times) == 1:
        hour, minute = parsed_times[0]
        start_calendar_interval: dict[str, int] | list[dict[str, int]] = {
            "Hour": hour,
            "Minute": minute,
        }
    else:
        start_calendar_interval = [
            {"Hour": hour, "Minute": minute} for hour, minute in parsed_times
        ]
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.briefings",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        start_calendar_interval=start_calendar_interval,
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "time": normalized_times[0],
            "times": normalized_times,
            "sync_timeout_seconds": int(sync_timeout_seconds),
        },
    )


def launchd_install_relay(
    host: str = typer.Option(
        "0.0.0.0",
        "--host",
        help="Bind host passed to `sidae relay serve`.",
    ),
    port: int = typer.Option(
        8787,
        "--port",
        min=1,
        max=65535,
        help="Bind port passed to `sidae relay serve`.",
    ),
    state_file: Optional[Path] = typer.Option(
        None,
        "--state-file",
        help="Optional relay dedupe state file override.",
    ),
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd install scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    run_as_user: Optional[str] = typer.Option(
        None,
        "--run-as-user",
        help="UserName written into the plist when `--scope daemon` is used.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Write launch agent plist for the briefing relay and load it with launchctl."""
    cli = _cli_module()
    resolved_config = cli.select_config_path(config_file=config_file)
    args = [
        cli.sys.executable,
        "-m",
        "sidae_secretary.cli",
        "relay",
        "serve",
        "--host",
        str(host),
        "--port",
        str(port),
        "--config-file",
        str(resolved_config),
    ]
    resolved_state_file: Path | None = None
    if state_file is not None:
        resolved_state_file = state_file.expanduser()
        if not resolved_state_file.is_absolute():
            resolved_state_file = (resolved_config.parent / resolved_state_file).resolve()
        args.extend(["--state-file", str(resolved_state_file)])
    _install_launchd_job_and_echo(
        config_file=config_file,
        instance_name=instance_name,
        base_label="com.sidae.secretary.briefing-relay",
        args=args,
        scope=scope,
        run_as_user=run_as_user,
        keepalive=True,
        extra={
            "instance_name": cli._resolve_instance_name(
                config_file=resolved_config,
                instance_name=instance_name,
            )
            or None,
            "host": str(host),
            "port": int(port),
            "state_file": str(resolved_state_file) if resolved_state_file else None,
        },
    )


def launchd_uninstall(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary", instance_name),
        scope=scope,
    )


def launchd_uninstall_telegram_poller(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove Telegram poller launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.telegram-poller", instance_name),
        scope=scope,
    )


def launchd_uninstall_telegram_listener(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove Telegram listener launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.telegram-listener", instance_name),
        scope=scope,
    )


def launchd_uninstall_uclass_poller(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove UClass poller launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.uclass-poller", instance_name),
        scope=scope,
    )


def launchd_uninstall_briefings(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove briefing sender launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.briefings", instance_name),
        scope=scope,
    )


def launchd_uninstall_weather_sync(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove hourly weather sync launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.weather-sync", instance_name),
        scope=scope,
    )


def launchd_uninstall_onboarding(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove onboarding web-server launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.onboarding", instance_name),
        scope=scope,
    )


def launchd_uninstall_ops_dashboard(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove ops dashboard launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.ops-dashboard", instance_name),
        scope=scope,
    )


def launchd_uninstall_publish(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove publish launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.publish", instance_name),
        scope=scope,
    )


def launchd_uninstall_relay(
    scope: str = typer.Option(
        "agent",
        "--scope",
        help="launchd uninstall scope: `agent` for the logged-in user session or `daemon` for the system domain.",
    ),
    instance_name: str = typer.Option(
        "",
        "--instance-name",
        help="Optional instance suffix for parallel runtimes, for example `beta`.",
    ),
) -> None:
    """Unload and remove briefing relay launch agent plist."""
    cli = _cli_module()
    cli._uninstall_launchd_job(
        label=cli._launchd_label("com.sidae.secretary.briefing-relay", instance_name),
        scope=scope,
    )


def register_cli_launchd_commands(launchd_app: typer.Typer) -> None:
    launchd_app.command("install")(launchd_install)
    launchd_app.command("install-uclass-poller")(launchd_install_uclass_poller)
    launchd_app.command("install-telegram-poller")(launchd_install_telegram_poller)
    launchd_app.command("install-telegram-listener")(launchd_install_telegram_listener)
    launchd_app.command("install-weather-sync")(launchd_install_weather_sync)
    launchd_app.command("install-onboarding")(launchd_install_onboarding)
    launchd_app.command("install-ops-dashboard")(launchd_install_ops_dashboard)
    launchd_app.command("install-publish")(launchd_install_publish)
    launchd_app.command("install-briefings")(launchd_install_briefings)
    launchd_app.command("install-relay")(launchd_install_relay)
    launchd_app.command("uninstall")(launchd_uninstall)
    launchd_app.command("uninstall-telegram-poller")(launchd_uninstall_telegram_poller)
    launchd_app.command("uninstall-telegram-listener")(launchd_uninstall_telegram_listener)
    launchd_app.command("uninstall-uclass-poller")(launchd_uninstall_uclass_poller)
    launchd_app.command("uninstall-briefings")(launchd_uninstall_briefings)
    launchd_app.command("uninstall-weather-sync")(launchd_uninstall_weather_sync)
    launchd_app.command("uninstall-onboarding")(launchd_uninstall_onboarding)
    launchd_app.command("uninstall-ops-dashboard")(launchd_uninstall_ops_dashboard)
    launchd_app.command("uninstall-publish")(launchd_uninstall_publish)
    launchd_app.command("uninstall-relay")(launchd_uninstall_relay)
