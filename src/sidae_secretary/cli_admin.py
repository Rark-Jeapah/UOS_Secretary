from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Optional

import typer


def _cli_module():
    return importlib.import_module("sidae_secretary.cli")


def admin_refresh_user(
    user_id: Optional[int] = typer.Option(None, "--user-id", help="Internal user id to refresh."),
    chat_id: Optional[str] = typer.Option(None, "--chat-id", help="Telegram chat id to refresh."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Refresh beta-critical surfaces for one user without redesigning the full sync pipeline."""
    cli = _cli_module()
    cli.configure_logging()
    settings, db = cli._load_settings_and_init_db(config_file=config_file)
    result = cli.refresh_beta_user(settings=settings, db=db, user_id=user_id, chat_id=chat_id)
    typer.echo(json.dumps(result, indent=2))


def admin_last_failed_stage(
    component: Optional[str] = typer.Option(
        None,
        "--component",
        help="Optional component filter: uos_official_api, uclass_sync, telegram, weather_sync, notice_fetch.",
    ),
    user_id: Optional[int] = typer.Option(None, "--user-id", help="Internal user id scope."),
    chat_id: Optional[str] = typer.Option(None, "--chat-id", help="Telegram chat id scope."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Inspect the most recent failed or degraded stage for beta operations."""
    cli = _cli_module()
    cli.configure_logging()
    settings, db = cli._load_settings_and_init_db(config_file=config_file)
    result = cli.inspect_last_failed_stage(
        settings=settings,
        db=db,
        component=component,
        user_id=user_id,
        chat_id=chat_id,
    )
    typer.echo(json.dumps(result, indent=2))


def verify_auth_attempts(
    limit: int = typer.Option(25, "--limit", min=1, max=200, help="Maximum recent attempts to return."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Show recent onboarding auth attempts and suspicious sources."""
    cli = _cli_module()
    cli.configure_logging()
    _, db = cli._load_settings_and_init_db(config_file=config_file)
    snapshot = db.auth_attempt_dashboard_snapshot()
    recent = snapshot.get("recent_attempts") if isinstance(snapshot.get("recent_attempts"), list) else []
    typer.echo(
        json.dumps(
            {
                "window_last_15m": snapshot.get("window_last_15m"),
                "window_last_1h": snapshot.get("window_last_1h"),
                "suspicious_remotes": snapshot.get("suspicious_remotes"),
                "top_remotes": snapshot.get("top_remotes"),
                "recent_attempts": recent[: max(int(limit), 1)],
            },
            indent=2,
        )
    )


def verify_mobile_offline(
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        help="Maximum allowed age for publish_dashboard snapshot freshness.",
    ),
    materials_check_limit: int = typer.Option(
        5,
        "--materials-check-limit",
        min=1,
        help="Check up to N most recently updated artifacts.",
    ),
    materials_check_all: bool = typer.Option(
        False,
        "--materials-check-all",
        help="Check every artifact path (can be slower).",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Validate local published snapshot readiness using dashboard freshness + stored artifacts."""
    cli = _cli_module()
    cli.configure_logging()
    settings, db = cli._load_settings_and_init_db(config_file=config_file)
    report = cli._mobile_offline_report(
        settings=settings,
        db=db,
        max_age_hours=max_age_hours,
        materials_check_limit=materials_check_limit,
        materials_check_all=materials_check_all,
    )
    typer.echo(json.dumps(report, indent=2))
    if not report.get("ok"):
        raise typer.Exit(code=1)


def verify_closed_loop(
    timeout_seconds: float = typer.Option(
        600,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="Maximum seconds to wait for sync lock during closed-loop run.",
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        help="Maximum allowed age for publish_dashboard snapshot freshness.",
    ),
    materials_check_limit: int = typer.Option(
        5,
        "--materials-check-limit",
        min=1,
        help="Check up to N artifacts when not using --materials-check-all.",
    ),
    materials_check_all: bool = typer.Option(
        False,
        "--materials-check-all",
        help="Check every artifact path (can be slower).",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Run doctor readiness -> sync --all --wait -> verify mobile-offline as one report."""
    cli = _cli_module()
    cli.configure_logging()
    settings, db = cli._load_settings_and_init_db(config_file=config_file)

    subreports: dict[str, object] = {}
    doctor_report = cli._doctor_readiness_report(settings=settings, db=db)
    subreports["doctor"] = doctor_report

    if not doctor_report.get("ok"):
        report = {"ok": False, "subreports": subreports}
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(code=1)

    try:
        sync_report = cli._run_sync_all_once(
            settings=settings,
            db=db,
            wait=True,
            timeout_seconds=timeout_seconds,
        )
    except cli._SyncLockBusyError:
        lock_error = cli._sync_lock_busy_payload(cli._sync_all_lock_path(settings.database_path))
        subreports["sync"] = lock_error
        report = {"ok": False, "subreports": subreports}
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(code=4)
    except cli._SyncLockTimeoutError as exc:
        lock_error = cli._sync_lock_timeout_payload(exc)
        subreports["sync"] = lock_error
        report = {"ok": False, "subreports": subreports}
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(code=4)

    subreports["sync"] = sync_report
    mobile_report = cli._mobile_offline_report(
        settings=settings,
        db=db,
        max_age_hours=max_age_hours,
        materials_check_limit=materials_check_limit,
        materials_check_all=materials_check_all,
    )
    subreports["mobile_offline"] = mobile_report

    final_ok = (
        bool(doctor_report.get("ok"))
        and bool(sync_report.get("ok"))
        and bool(mobile_report.get("ok"))
    )
    report = {"ok": final_ok, "subreports": subreports}
    typer.echo(json.dumps(report, indent=2))
    if not final_ok:
        raise typer.Exit(code=1)


def register_cli_admin_commands(*, admin_app: typer.Typer, verify_app: typer.Typer) -> None:
    admin_app.command("refresh-user")(admin_refresh_user)
    admin_app.command("last-failed-stage")(admin_last_failed_stage)
    verify_app.command("auth-attempts")(verify_auth_attempts)
    verify_app.command("mobile-offline")(verify_mobile_offline)
    verify_app.command("closed-loop")(verify_closed_loop)
