from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Optional

import typer


def _cli_module():
    return importlib.import_module("sidae_secretary.cli")


def onboarding_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8791, "--port", min=1, max=65535, help="Bind port."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Serve the Telegram-driven Moodle onboarding web flow."""
    cli = _cli_module()
    cli.configure_logging()
    settings = cli.load_settings(config_file=config_file)
    db = cli.Database(settings.database_path)
    db.init()
    server = cli.build_onboarding_http_server(
        host=host,
        port=port,
        settings=settings,
        db=db,
    )
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "service": "sidae-onboarding",
                "host": host,
                "port": port,
                "healthz": f"http://{host}:{port}/healthz",
                "public_base_url": str(getattr(settings, "onboarding_public_base_url", "") or ""),
            },
            indent=2,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def onboarding_browser_login(
    school: str = typer.Option(..., "--school", help="School name to resolve from the LMS directory."),
    chat_id: str = typer.Option(..., "--chat-id", help="Telegram chat_id that owns this browser profile."),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help="Open the login page and return immediately without waiting for Enter.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Open a persistent browser profile for schools that require browser-session onboarding."""
    cli = _cli_module()
    cli.configure_logging()
    settings = cli.load_settings(config_file=config_file)
    db = cli.Database(settings.database_path)
    db.init()
    raw_matches = db.find_moodle_school_directory(school, limit=5)
    matches = [
        entry
        for entry in raw_matches
        if cli.school_entry_allowed_for_onboarding(entry, settings=settings)
    ]
    if not matches:
        error = "school_not_found"
        if raw_matches and cli.onboarding_allowed_school_slugs(settings):
            error = "school_not_allowed"
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": error,
                    "school_query": str(school),
                    "allowed_school_slugs": sorted(cli.onboarding_allowed_school_slugs(settings)),
                },
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    school_entry = matches[0]
    auth_mode = cli.school_directory_auth_mode(school_entry)
    if auth_mode != cli.BROWSER_SESSION_AUTH_MODE:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "school_does_not_require_browser_session",
                    "school_slug": str(school_entry.get("school_slug") or ""),
                    "display_name": str(school_entry.get("display_name") or ""),
                    "auth_mode": auth_mode,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=2)

    provider = cli.school_directory_provider(school_entry)
    login_url = cli.school_directory_login_url(school_entry)
    if not login_url:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "school_login_url_missing",
                    "school_slug": str(school_entry.get("school_slug") or ""),
                    "display_name": str(school_entry.get("display_name") or ""),
                },
                indent=2,
            )
        )
        raise typer.Exit(code=1)

    profile_dir = cli.browser_session_profile_dir(
        settings.onboarding_browser_profiles_dir,
        provider=provider,
        school_slug=str(school_entry.get("school_slug") or ""),
        chat_id=str(chat_id),
    )

    def _wait_for_confirmation() -> None:
        if no_prompt:
            return
        input("브라우저에서 학교 로그인을 마친 뒤 Enter를 누르세요...")

    result = cli.launch_browser_session_login(
        login_url=login_url,
        profile_dir=profile_dir,
        browser_channel=str(getattr(settings, "onboarding_browser_channel", "") or ""),
        browser_executable_path=getattr(settings, "onboarding_browser_executable_path", None),
        headless=bool(getattr(settings, "onboarding_browser_headless", False)),
        wait_callback=None if no_prompt else _wait_for_confirmation,
    )
    confirmed_at = cli.now_utc_iso()
    session = db.upsert_lms_browser_session(
        chat_id=str(chat_id),
        school_slug=str(school_entry.get("school_slug") or ""),
        provider=provider,
        display_name=str(school_entry.get("display_name") or ""),
        login_url=login_url,
        profile_dir=profile_dir,
        status="active",
        last_opened_at=confirmed_at,
        last_verified_at=(confirmed_at if not no_prompt else None),
        metadata_json={
            "auth_mode": auth_mode,
            "school_query": str(school),
            "browser_result": cli.sanitize_browser_session_result(result),
            "manual_confirmation": (not no_prompt),
        },
    )
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "auth_mode": auth_mode,
                "provider": provider,
                "school_slug": session["school_slug"],
                "display_name": session["display_name"],
                "chat_id": session["chat_id"],
                "profile_dir": session["profile_dir"],
                "login_url": session["login_url"],
                "last_opened_at": session["last_opened_at"],
                "last_verified_at": session["last_verified_at"],
            },
            indent=2,
        )
    )


def register_cli_onboarding_commands(onboarding_app: typer.Typer) -> None:
    onboarding_app.command("serve")(onboarding_serve)
    onboarding_app.command("browser-login")(onboarding_browser_login)
