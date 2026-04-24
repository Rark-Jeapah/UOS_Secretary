from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import importlib.util
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import zipfile

from dateutil import parser as dt_parser
import typer

from sidae_secretary.browser_session import (
    BROWSER_SESSION_AUTH_MODE,
    browser_session_profile_dir,
    launch_browser_session_login,
    sanitize_browser_session_result,
    school_directory_auth_mode,
    school_directory_login_url,
    school_directory_provider,
)
from sidae_secretary.briefing_relay import (
    BriefingRelayStateStore,
    deliver_signed_briefing_request,
)
from sidae_secretary.buildings import UOS_BUILDING_MAP
from sidae_secretary.cli_admin import register_cli_admin_commands
from sidae_secretary.cli_launchd import register_cli_launchd_commands
from sidae_secretary.cli_onboarding import register_cli_onboarding_commands
from sidae_secretary.cli_ops import register_cli_ops_commands
from sidae_secretary.config import (
    load_instance_name,
    load_settings,
    normalize_instance_name,
    select_config_path,
)
from sidae_secretary.connectors.telegram import TelegramBotClient
from sidae_secretary import docs_artifacts as docs_artifacts_module
from sidae_secretary.db import Database, normalize_course_alias, now_utc_iso
from sidae_secretary.jobs.pipeline import (
    apply_inbox_items,
    build_beta_ops_health_report,
    ignore_inbox_item,
    inspect_last_failed_stage,
    import_portal_events,
    mark_task_status,
    publish_dashboard,
    refresh_beta_user,
    run_all_jobs,
    send_scheduled_briefings as send_scheduled_briefings_job,
    sync_weather as sync_weather_job,
    sync_uclass as sync_uclass_job,
    sync_telegram as sync_telegram_job,
    run_uclass_probe,
)
from sidae_secretary.logging_utils import configure_logging
from sidae_secretary.onboarding import (
    build_onboarding_http_server,
    onboarding_allowed_school_slugs,
    normalize_public_moodle_connect_base_url,
    school_entry_allowed_for_onboarding,
)
from sidae_secretary.ops_dashboard import (
    build_ops_dashboard_http_server,
    build_ops_dashboard_snapshot,
)
from sidae_secretary.secret_store import secret_store_report
from sidae_secretary.storage import (
    backups_dir as storage_backups_dir,
    browser_profiles_dir as storage_browser_profiles_dir,
    dashboard_dir as storage_dashboard_dir,
    expected_storage_subdirs,
    materials_dir as storage_materials_dir,
    resolve_storage_root,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


logger = logging.getLogger(__name__)


app = typer.Typer(
    name="sidae",
    no_args_is_help=True,
    help="Sidae Secretary local-first sync agent.",
    add_completion=False,
)
portal_app = typer.Typer(name="portal", help="Portal academic calendar tools.")
launchd_app = typer.Typer(name="launchd", help="Install or remove launchd schedule.")
uclass_app = typer.Typer(name="uclass", help="UClass webservice tools.")
inbox_app = typer.Typer(name="inbox", help="Inbox processing tools.")
tasks_app = typer.Typer(name="tasks", help="Task completion tools.")
verify_app = typer.Typer(name="verify", help="Operational verification tools.")
ack_app = typer.Typer(name="ack", help="Acknowledgement tools.")
buildings_app = typer.Typer(name="buildings", help="Building metadata tools.")
courses_app = typer.Typer(name="courses", help="Course entity and alias tools.")
reminders_app = typer.Typer(name="reminders", help="Reminder schedule tools.")
relay_app = typer.Typer(name="relay", help="Signed briefing relay server.")
onboarding_app = typer.Typer(name="onboarding", help="Telegram onboarding web server.")
admin_app = typer.Typer(name="admin", help="Operational admin tools.")
ops_app = typer.Typer(name="ops", help="Local operations dashboard tools.")
app.add_typer(portal_app, name="portal")
app.add_typer(launchd_app, name="launchd")
app.add_typer(uclass_app, name="uclass")
app.add_typer(inbox_app, name="inbox")
app.add_typer(tasks_app, name="tasks")
app.add_typer(verify_app, name="verify")
app.add_typer(ack_app, name="ack")
app.add_typer(buildings_app, name="buildings")
app.add_typer(courses_app, name="courses")
app.add_typer(reminders_app, name="reminders")
app.add_typer(relay_app, name="relay")
app.add_typer(onboarding_app, name="onboarding")
app.add_typer(admin_app, name="admin")
app.add_typer(ops_app, name="ops")
register_cli_launchd_commands(launchd_app)
register_cli_onboarding_commands(onboarding_app)
register_cli_admin_commands(admin_app=admin_app, verify_app=verify_app)
register_cli_ops_commands(ops_app)


def _dependency_checks(settings: Any | None = None) -> dict[str, bool]:
    def _module_ok(module: str) -> bool:
        return importlib.util.find_spec(module) is not None

    typer_ok = _module_ok("typer")
    requests_ok = _module_ok("requests")
    dateutil_ok = _module_ok("dateutil")
    icalendar_ok = _module_ok("icalendar")
    playwright_ok = _module_ok("playwright")
    llm_provider = str(
        getattr(settings, "llm_provider", "") if settings is not None else ""
    ).strip().lower()
    llm_enabled = bool(getattr(settings, "llm_enabled", False)) if settings is not None else False
    if not llm_provider:
        llm_provider = "local"
    llm_provider_supported = (not llm_enabled) or llm_provider == "local"
    llm_provider_import_ok = requests_ok and llm_provider_supported
    return {
        "typer_import_ok": typer_ok,
        "requests_import_ok": requests_ok,
        "dateutil_import_ok": dateutil_ok,
        "icalendar_import_ok": icalendar_ok,
        "playwright_import_ok": playwright_ok,
        "telegram_requests_import_ok": requests_ok,
        "telegram_dateutil_import_ok": dateutil_ok,
        "telegram_import_ok": requests_ok and dateutil_ok,
        "llm_provider_supported": llm_provider_supported,
        "llm_provider_import_ok": llm_provider_import_ok,
        "llm_import_ok": llm_provider_import_ok,
    }


def _python_version_tuple(version_info: Any) -> tuple[int, int, int]:
    major = getattr(version_info, "major", None)
    minor = getattr(version_info, "minor", None)
    micro = getattr(version_info, "micro", None)
    if all(isinstance(item, int) for item in (major, minor, micro)):
        return int(major), int(minor), int(micro)
    try:
        values = list(version_info)
    except Exception:
        return (0, 0, 0)
    if len(values) < 2:
        return (0, 0, 0)
    raw_major = values[0]
    raw_minor = values[1]
    raw_micro = values[2] if len(values) >= 3 else 0
    try:
        return int(raw_major), int(raw_minor), int(raw_micro)
    except Exception:
        return (0, 0, 0)


def _runtime_environment_report() -> dict[str, Any]:
    major, minor, micro = _python_version_tuple(sys.version_info)
    current_python = f"{major}.{minor}.{micro}"
    python_ok = (major, minor) >= (3, 11)
    python_error = (
        None
        if python_ok
        else f"Python 3.11+ is required (current: {current_python})."
    )

    openssl_version = str(getattr(ssl, "OPENSSL_VERSION", "") or "").strip()
    lowered = openssl_version.lower()
    if "libressl" in lowered:
        ssl_backend = "LibreSSL"
    elif "openssl" in lowered:
        ssl_backend = "OpenSSL"
    elif openssl_version:
        ssl_backend = openssl_version.split(" ", 1)[0]
    else:
        ssl_backend = "unknown"
    ssl_ok = ssl_backend == "OpenSSL"
    ssl_warning = None
    if ssl_backend == "LibreSSL":
        ssl_warning = (
            "SSL backend is LibreSSL. Use a Homebrew/python.org OpenSSL-backed "
            "Python build for automation reliability."
        )
    return {
        "python": {
            "ok": python_ok,
            "required_min": "3.11",
            "current": current_python,
            "error": python_error,
        },
        "ssl": {
            "ok": ssl_ok,
            "backend": ssl_backend,
            "version": openssl_version,
            "warning": ssl_warning,
        },
    }


def _print_key_values(title: str, values: dict[str, str]) -> None:
    typer.echo(title)
    for key, value in values.items():
        typer.echo(f"  {key}={value}")


def _feature_flag_report(settings: Any) -> dict[str, bool]:
    return {
        "TELEGRAM_COMMANDS_ENABLED": bool(
            getattr(settings, "telegram_commands_enabled", False)
        ),
        "TELEGRAM_SMART_COMMANDS_ENABLED": bool(
            getattr(settings, "telegram_smart_commands_enabled", False)
        ),
        "TELEGRAM_ASSISTANT_ENABLED": bool(
            getattr(settings, "telegram_assistant_enabled", False)
        ),
        "TELEGRAM_ASSISTANT_WRITE_ENABLED": bool(
            getattr(settings, "telegram_assistant_write_enabled", False)
        ),
    }


def _normalize_ops_dashboard_url_path(value: str) -> str:
    text = str(value or "/").strip()
    if not text:
        text = "/"
    if not text.startswith("/"):
        text = "/" + text
    return text


def _open_browser_url(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(
            ["open", url],
            check=False,
            capture_output=True,
            text=True,
        )
        return
    try:  # pragma: no cover - non-macOS fallback
        import webbrowser

        webbrowser.open(url, new=1, autoraise=True)
    except Exception:
        return


def _wait_for_local_port(host: str, port: int, *, timeout_sec: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    while time.monotonic() <= deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _ops_dashboard_ssh_target(*, ssh_host: str, ssh_user: str | None = None) -> str:
    host = str(ssh_host or "").strip()
    user = str(ssh_user or "").strip()
    if not host:
        raise ValueError("ssh host is required")
    return f"{user}@{host}" if user else host


def _ops_dashboard_tunnel_command(
    *,
    ssh_target: str,
    ssh_port: int,
    local_port: int,
    remote_host: str,
    remote_port: int,
) -> list[str]:
    command = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"{int(local_port)}:{str(remote_host).strip() or '127.0.0.1'}:{int(remote_port)}",
    ]
    if int(ssh_port) > 0 and int(ssh_port) != 22:
        command.extend(["-p", str(int(ssh_port))])
    command.append(ssh_target)
    return command


def _parse_hhmm(value: str) -> tuple[int, int]:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        raise ValueError("expected HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time is out of range")
    return hour, minute


def _hourly_start_calendar_interval(minute_offset: int) -> list[dict[str, int]]:
    minute = int(minute_offset)
    if minute < 0 or minute > 59:
        raise ValueError("minute offset must be between 0 and 59")
    return [{"Hour": hour, "Minute": minute} for hour in range(24)]


def _normalize_launchd_scope(scope: str) -> str:
    normalized = str(scope or "agent").strip().lower()
    if normalized not in {"agent", "daemon"}:
        raise ValueError("scope must be one of: agent, daemon")
    return normalized


def _launchd_plist_path(label: str, scope: str = "agent") -> Path:
    normalized_scope = _normalize_launchd_scope(scope)
    if normalized_scope == "daemon":
        return Path("/Library/LaunchDaemons") / f"{label}.plist"
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_domain(scope: str = "agent") -> str:
    normalized_scope = _normalize_launchd_scope(scope)
    if normalized_scope == "daemon":
        return "system"
    return f"gui/{os.getuid()}"


def _resolve_instance_name(
    *,
    config_file: Path | None = None,
    instance_name: str | None = None,
) -> str:
    normalized = normalize_instance_name(instance_name)
    if normalized:
        return normalized
    return load_instance_name(config_file=config_file)


def _launchd_label(base_label: str, instance_name: str = "") -> str:
    normalized = normalize_instance_name(instance_name)
    if not normalized:
        return base_label
    return f"{base_label}.{normalized}"


def _launchd_log_paths(label: str) -> tuple[str, str]:
    return (f"/tmp/{label}.out.log", f"/tmp/{label}.err.log")


def _launchd_run_as_user(run_as_user: Optional[str] = None) -> str:
    candidate = str(run_as_user or "").strip()
    if candidate:
        return candidate
    sudo_user = str(os.environ.get("SUDO_USER") or "").strip()
    if sudo_user:
        return sudo_user
    current_user = str(getpass.getuser() or "").strip()
    if current_user:
        return current_user
    raise ValueError("unable to determine daemon user; pass --run-as-user explicitly")


def _launchctl_bootout(plist_path: Path, scope: str = "agent") -> None:
    domain = _launchd_domain(scope)
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def _launchctl_bootstrap(plist_path: Path, scope: str = "agent"):
    domain = _launchd_domain(scope)
    return subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def _launchd_job_payload(
    *,
    label: str,
    resolved_config: Path,
    args: list[str],
    stdout_path: str,
    stderr_path: str,
    scope: str = "agent",
    run_as_user: Optional[str] = None,
    run_at_load: bool = True,
    keepalive: bool = False,
    start_interval: Optional[int] = None,
    start_calendar_interval: Optional[dict[str, int] | list[dict[str, int]]] = None,
) -> tuple[dict[str, Any], Optional[str]]:
    normalized_scope = _normalize_launchd_scope(scope)
    daemon_user: Optional[str] = None
    payload: dict[str, Any] = {
        "Label": label,
        "WorkingDirectory": str(resolved_config.parent),
        "ProgramArguments": args,
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stderr_path,
    }
    if run_at_load:
        payload["RunAtLoad"] = True
    if keepalive:
        payload["KeepAlive"] = True
    if start_interval is not None:
        payload["StartInterval"] = int(start_interval)
    if start_calendar_interval is not None:
        payload["StartCalendarInterval"] = start_calendar_interval
    if normalized_scope == "daemon":
        daemon_user = _launchd_run_as_user(run_as_user)
        payload["UserName"] = daemon_user
    return payload, daemon_user


def _install_launchd_job(
    *,
    label: str,
    resolved_config: Path,
    args: list[str],
    stdout_path: str,
    stderr_path: str,
    scope: str = "agent",
    run_as_user: Optional[str] = None,
    run_at_load: bool = True,
    keepalive: bool = False,
    start_interval: Optional[int] = None,
    start_calendar_interval: Optional[dict[str, int] | list[dict[str, int]]] = None,
) -> tuple[Path, str, Optional[str]]:
    try:
        normalized_scope = _normalize_launchd_scope(scope)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)
    plist_path = _launchd_plist_path(label, normalized_scope)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        plist_payload, daemon_user = _launchd_job_payload(
            label=label,
            resolved_config=resolved_config,
            args=args,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            scope=normalized_scope,
            run_as_user=run_as_user,
            run_at_load=run_at_load,
            keepalive=keepalive,
            start_interval=start_interval,
            start_calendar_interval=start_calendar_interval,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)
    with plist_path.open("wb") as fp:
        plistlib.dump(plist_payload, fp, sort_keys=False)
    _launchctl_bootout(plist_path, normalized_scope)
    loaded = _launchctl_bootstrap(plist_path, normalized_scope)
    if loaded.returncode != 0:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "plist_path": str(plist_path),
                    "error": loaded.stderr.strip(),
                },
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    return plist_path, normalized_scope, daemon_user


def _echo_launchd_install_result(
    *,
    plist_path: Path,
    resolved_config: Path,
    scope: str,
    run_as_user: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    result: dict[str, Any] = {
        "ok": True,
        "plist_path": str(plist_path),
        "scope": scope,
        "launchd_domain": _launchd_domain(scope),
        "config_file": str(resolved_config),
        "working_directory": str(resolved_config.parent),
    }
    if run_as_user is not None:
        result["run_as_user"] = run_as_user
    if extra:
        result.update({key: value for key, value in extra.items() if value is not None})
    typer.echo(json.dumps(result, indent=2))


def _uninstall_launchd_job(*, label: str, scope: str = "agent") -> None:
    try:
        normalized_scope = _normalize_launchd_scope(scope)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)
    plist_path = _launchd_plist_path(label, normalized_scope)
    _launchctl_bootout(plist_path, normalized_scope)
    removed = False
    if plist_path.exists():
        plist_path.unlink()
        removed = True
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "plist_path": str(plist_path),
                "removed": removed,
                "scope": normalized_scope,
                "launchd_domain": _launchd_domain(normalized_scope),
            },
            indent=2,
        )
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join([_toml_value(item) for item in value]) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _upsert_env_value(env_file: Path, key: str, value: str) -> None:
    normalized_key = str(key or "").strip()
    line = f"{normalized_key}={value}"
    existing_lines: list[str] = []
    if env_file.exists():
        existing_lines = env_file.read_text(encoding="utf-8").splitlines()
    updated = False
    output_lines: list[str] = []
    for existing in existing_lines:
        if existing.startswith(f"{normalized_key}="):
            output_lines.append(line)
            updated = True
        else:
            output_lines.append(existing)
    if not updated:
        output_lines.append(line)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")


def _redact_url_tokens(value: str) -> str:
    def _redact_single_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        query = parse_qsl(parsed.query, keep_blank_values=True)
        redacted: list[tuple[str, str]] = []
        for key, item in query:
            key_lower = key.strip().lower()
            if key_lower in {"token", "apikey", "api_key", "access_token", "wstoken", "authkey"}:
                redacted.append((key, "***"))
            else:
                redacted.append((key, item))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(redacted, doseq=True),
                parsed.fragment,
            )
        )

    return re.sub(r"https?://[^\s'\"<>]+", lambda match: _redact_single_url(match.group(0)), value)


def _scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if any(token in key_text for token in ("token", "secret", "password", "api_key", "apikey", "wstoken")):
                output[str(key)] = "***"
            else:
                output[str(key)] = _scrub_secrets(item)
        return output
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_url_tokens(value)
    return value


def _parse_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _load_settings_and_init_db(config_file: Optional[Path]) -> tuple[Any, Database]:
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    return settings, db


def _sync_job_lock_path(db_path: Path, lock_name: str) -> Path:
    suffix = db_path.suffix
    if suffix:
        return db_path.with_suffix(f"{suffix}.{lock_name}.lock")
    return Path(f"{db_path}.{lock_name}.lock")


def _sync_all_lock_path(db_path: Path) -> Path:
    return _sync_job_lock_path(db_path, "sync-all")


def _sync_telegram_lock_path(db_path: Path) -> Path:
    return _sync_job_lock_path(db_path, "sync-telegram")


def _sync_uclass_lock_path(db_path: Path) -> Path:
    return _sync_job_lock_path(db_path, "sync-uclass")

def _sync_weather_lock_path(db_path: Path) -> Path:
    return _sync_job_lock_path(db_path, "sync-weather")


def _send_briefings_lock_path(db_path: Path) -> Path:
    return _sync_job_lock_path(db_path, "send-briefings")


class _SyncLockBusyError(RuntimeError):
    pass


class _SyncLockTimeoutError(RuntimeError):
    def __init__(self, lock_path: Path, waited_seconds: float):
        self.lock_path = str(lock_path)
        self.waited_seconds = float(waited_seconds)
        super().__init__(f"sync lock wait timed out: {self.lock_path}")


def _sync_lock_busy_payload(lock_path: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "sync_lock_held",
        "message": "Another `sidae sync --all` process is running.",
        "lock_path": str(lock_path),
    }


def _sync_lock_timeout_payload(exc: _SyncLockTimeoutError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "sync_lock_timeout",
        "message": "Timed out while waiting for sync lock.",
        "lock_path": exc.lock_path,
        "waited_seconds": round(exc.waited_seconds, 3),
    }


def _run_sync_all_once(
    settings: Any,
    db: Database,
    *,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    lock_path = _sync_all_lock_path(settings.database_path)
    with _sync_all_execution_lock(
        lock_path=lock_path,
        wait=wait,
        timeout_seconds=timeout_seconds,
    ):
        run_all_jobs_params = inspect.signature(run_all_jobs).parameters
        if "job_runner" in run_all_jobs_params:
            summary = run_all_jobs(
                settings,
                db,
                job_runner=lambda job_name, job_fn: _run_sync_all_job_with_shared_lock(
                    settings=settings,
                    db=db,
                    job_name=job_name,
                    job_fn=job_fn,
                    wait=wait,
                    timeout_seconds=timeout_seconds,
                ),
            )
        else:
            summary = run_all_jobs(settings, db)
    return {
        "ok": bool(summary.ok),
        "stats": summary.stats,
        "errors": summary.errors,
        "lock_path": str(lock_path),
    }


def _run_sync_telegram_once(
    settings: Any,
    db: Database,
    *,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    lock_path = _sync_telegram_lock_path(settings.database_path)
    with _sync_all_execution_lock(
        lock_path=lock_path,
        wait=wait,
        timeout_seconds=timeout_seconds,
    ):
        result = sync_telegram_job(settings, db)
    return {
        "ok": True,
        "stats": result,
        "errors": [],
        "lock_path": str(lock_path),
    }


def _run_sync_uclass_once(
    settings: Any,
    db: Database,
    *,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    lock_path = _sync_uclass_lock_path(settings.database_path)
    stats: dict[str, Any] = {}
    errors: list[str] = []
    with _sync_all_execution_lock(
        lock_path=lock_path,
        wait=wait,
        timeout_seconds=timeout_seconds,
    ):
        for name, fn in (("sync_uclass", sync_uclass_job),):
            try:
                stats[name] = fn(settings, db)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    return {
        "ok": not errors,
        "stats": stats,
        "errors": errors,
        "lock_path": str(lock_path),
    }


def _run_send_briefings_once(
    settings: Any,
    db: Database,
    *,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    lock_path = _send_briefings_lock_path(settings.database_path)
    with _sync_all_execution_lock(
        lock_path=lock_path,
        wait=wait,
        timeout_seconds=timeout_seconds,
    ):
        result = send_scheduled_briefings_job(settings, db)
    error = str(result.get("error") or "").strip() if isinstance(result, dict) else ""
    errors = [error] if error else []
    return {
        "ok": not errors,
        "stats": result,
        "errors": errors,
        "lock_path": str(lock_path),
    }


def _run_sync_weather_once(
    settings: Any,
    db: Database,
    *,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    lock_path = _sync_weather_lock_path(settings.database_path)
    with _sync_all_execution_lock(
        lock_path=lock_path,
        wait=wait,
        timeout_seconds=timeout_seconds,
    ):
        result = sync_weather_job(settings, db)
    error = str(result.get("error") or "").strip() if isinstance(result, dict) else ""
    errors = [error] if error else []
    return {
        "ok": not errors,
        "stats": result,
        "errors": errors,
        "lock_path": str(lock_path),
    }


def _sync_all_shared_job_lock_path(job_name: str, db_path: Path) -> Path | None:
    lock_builders: dict[str, Callable[[Path], Path]] = {
        "sync_uclass": _sync_uclass_lock_path,
        "sync_weather": _sync_weather_lock_path,
        "sync_telegram": _sync_telegram_lock_path,
        "scheduled_briefings": _send_briefings_lock_path,
    }
    builder = lock_builders.get(job_name)
    if builder is None:
        return None
    return builder(db_path)


def _sync_all_job_should_wait(job_name: str, wait: bool) -> bool:
    if not wait:
        return False
    # telegram-listener holds this lock for its full lifetime, so sync --all should skip instead
    # of blocking behind a long-poll daemon.
    return job_name in {"sync_uclass", "sync_weather"}


def _run_sync_all_job_with_shared_lock(
    *,
    settings: Any,
    db: Database,
    job_name: str,
    job_fn: Callable[[Any, Database], Any],
    wait: bool,
    timeout_seconds: float | None,
) -> Any:
    lock_path = _sync_all_shared_job_lock_path(job_name, settings.database_path)
    if lock_path is None:
        return job_fn(settings, db)

    job_wait = _sync_all_job_should_wait(job_name, wait)
    job_timeout = timeout_seconds if job_wait else None
    try:
        with _sync_all_execution_lock(
            lock_path=lock_path,
            wait=job_wait,
            timeout_seconds=job_timeout,
        ):
            return job_fn(settings, db)
    except _SyncLockBusyError:
        return {
            "skipped": True,
            "reason": f"{job_name}_lock_held",
            "lock_path": str(lock_path),
        }
    except _SyncLockTimeoutError as exc:
        return {
            "skipped": True,
            "reason": f"{job_name}_lock_timeout",
            "lock_path": exc.lock_path,
            "waited_seconds": round(exc.waited_seconds, 3),
        }


@contextmanager
def _sync_all_execution_lock(
    lock_path: Path,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fp:
        if fcntl is None:
            yield
            return
        acquired = False
        try:
            if wait and timeout_seconds is not None:
                timeout = max(float(timeout_seconds), 0.0)
                started = time.monotonic()
                while True:
                    try:
                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError as exc:
                        waited = time.monotonic() - started
                        if waited >= timeout:
                            raise _SyncLockTimeoutError(
                                lock_path=lock_path,
                                waited_seconds=waited,
                            ) from exc
                        remaining = timeout - waited
                        time.sleep(min(0.05, max(remaining, 0.01)))
            else:
                lock_mode = fcntl.LOCK_EX
                if not wait:
                    lock_mode |= fcntl.LOCK_NB
                try:
                    fcntl.flock(lock_fp.fileno(), lock_mode)
                    acquired = True
                except BlockingIOError as exc:
                    raise _SyncLockBusyError(str(lock_path)) from exc
            yield
        finally:
            if acquired:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def _is_directory_writable(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    marker = path / f".sidae_write_probe_{os.getpid()}"
    try:
        marker.write_text("probe", encoding="utf-8")
        marker.unlink(missing_ok=True)
        return True
    except Exception:
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _storage_health_report(settings: Any) -> dict[str, Any]:
    root = resolve_storage_root(settings)
    if root is None:
        return {
            "configured": False,
            "path": "",
            "exists": False,
            "writable": False,
            "subdirs": [],
            "ok": False,
            "reason": "STORAGE_ROOT_DIR not set",
        }
    exists = root.exists()
    writable = _is_directory_writable(root)
    subdirs: list[dict[str, Any]] = []
    for target in expected_storage_subdirs(root):
        subdirs.append(
            {
                "path": str(target),
                "exists": target.exists(),
            }
        )
    ok = exists and writable and all(bool(item["exists"]) for item in subdirs)
    return {
        "configured": True,
        "path": str(root),
        "exists": exists,
        "writable": writable,
        "subdirs": subdirs,
        "ok": ok,
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = dt_parser.isoparse(str(value))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _doctor_operational_report(settings: Any) -> dict[str, Any]:
    feature_flags = _feature_flag_report(settings)
    allowed_chat_ids = [
        str(item).strip()
        for item in list(getattr(settings, "telegram_allowed_chat_ids", []) or [])
        if str(item).strip()
    ]
    onboarding_public_base_url = str(
        getattr(settings, "onboarding_public_base_url", "") or ""
    ).strip()
    onboarding_https_ready = False
    onboarding_error = ""
    if onboarding_public_base_url:
        try:
            normalize_public_moodle_connect_base_url(onboarding_public_base_url)
            onboarding_https_ready = True
        except Exception as exc:
            onboarding_error = str(exc)
    allowed_school_slugs = sorted(onboarding_allowed_school_slugs(settings))
    instance_name = str(getattr(settings, "instance_name", "") or "").strip()
    is_beta_instance = bool(instance_name) and (
        instance_name == "beta" or instance_name.startswith("beta-")
    )
    uos_beta_scope = bool(allowed_school_slugs) and set(allowed_school_slugs).issubset(
        {"uos_online_class", "uos_portal"}
    )
    warnings: list[str] = []
    if bool(getattr(settings, "telegram_enabled", False)) and not allowed_chat_ids:
        warnings.append("TELEGRAM_ALLOWED_CHAT_IDS is empty")
    if bool(getattr(settings, "telegram_commands_enabled", False)) and not onboarding_https_ready:
        warnings.append("ONBOARDING_PUBLIC_BASE_URL is missing or not https")
    if str(getattr(settings, "uclass_username", "") or "").strip() or str(
        getattr(settings, "uclass_password", "") or ""
    ):
        warnings.append("UCLASS_USERNAME/UCLASS_PASSWORD are deprecated; use UCLASS_WSTOKEN or /connect")
    if (
        feature_flags["TELEGRAM_ASSISTANT_WRITE_ENABLED"]
        and not feature_flags["TELEGRAM_ASSISTANT_ENABLED"]
    ):
        warnings.append(
            "TELEGRAM_ASSISTANT_WRITE_ENABLED requires TELEGRAM_ASSISTANT_ENABLED"
        )
    if is_beta_instance and not feature_flags["TELEGRAM_ASSISTANT_ENABLED"]:
        warnings.append("Beta instance should enable TELEGRAM_ASSISTANT_ENABLED for /bot validation")
    if (
        is_beta_instance
        and feature_flags["TELEGRAM_ASSISTANT_ENABLED"]
        and not feature_flags["TELEGRAM_ASSISTANT_WRITE_ENABLED"]
    ):
        warnings.append(
            "Beta instance keeps /bot in read-only mode; enable TELEGRAM_ASSISTANT_WRITE_ENABLED to validate write flows"
        )
    if (
        is_beta_instance
        and feature_flags["TELEGRAM_ASSISTANT_ENABLED"]
        and not bool(getattr(settings, "llm_enabled", False))
    ):
        warnings.append("Beta instance should enable LLM_ENABLED when /bot assistant is on")
    if is_beta_instance and not allowed_school_slugs:
        warnings.append("Beta instance should set ONBOARDING_ALLOWED_SCHOOL_SLUGS")
    if is_beta_instance and allowed_school_slugs and not uos_beta_scope:
        warnings.append("Beta instance school scope is not UOS-only")
    return {
        "instance_name": instance_name,
        "is_beta_instance": is_beta_instance,
        "telegram_allowlist_configured": bool(allowed_chat_ids),
        "allowed_chat_count": len(allowed_chat_ids),
        "onboarding_https_ready": onboarding_https_ready,
        "onboarding_error": onboarding_error or None,
        "onboarding_scope_limited": bool(allowed_school_slugs),
        "onboarding_allowed_school_slugs": allowed_school_slugs,
        "uos_beta_scope": uos_beta_scope,
        "warnings": warnings,
    }


def _doctor_readiness_report(settings: Any, db: Database) -> dict[str, Any]:
    missing = settings.required_missing() if hasattr(settings, "required_missing") else []
    counts = db.counts()
    dep_report = _dependency_checks(settings=settings)
    feature_flags = _feature_flag_report(settings)
    storage_health = _storage_health_report(settings)
    secret_store = secret_store_report(settings)
    runtime = _runtime_environment_report()
    operational = _doctor_operational_report(settings)
    health = build_beta_ops_health_report(settings, db)
    return {
        "ok": (not bool(missing)) and bool(runtime["python"]["ok"]),
        "missing_required_config": missing,
        "counts": counts,
        "deps": dep_report,
        "feature_flags": feature_flags,
        "storage_health": storage_health,
        "secret_store": secret_store,
        "runtime": runtime,
        "operational": operational,
        "health": health,
    }


def _print_health_surface_summary(health: dict[str, Any]) -> None:
    typer.echo("Health Surfaces")
    surfaces = health.get("surfaces") if isinstance(health.get("surfaces"), dict) else {}
    for key in (
        "uos_official_api",
        "uclass_sync",
        "telegram_listener",
        "telegram_send",
        "weather_sync",
        "notice_fetch",
    ):
        item = surfaces.get(key) if isinstance(surfaces, dict) else {}
        if not isinstance(item, dict):
            continue
        typer.echo(
            f"  {key}: status={item.get('status')} ready={item.get('ready')} last_run_at={item.get('last_run_at')}"
        )
        if item.get("reason"):
            typer.echo(f"    reason={item['reason']}")
        if item.get("last_error"):
            typer.echo(f"    last_error={item['last_error']}")


def _material_row_sort_key(row: Any) -> tuple[int, str, str, str]:
    icloud_path = str(row["icloud_path"] or "")
    resolved = Path(icloud_path).expanduser() if icloud_path else None
    exists = bool(resolved and resolved.exists())
    if exists and resolved is not None:
        try:
            mtime_ns = int(resolved.stat().st_mtime_ns)
        except Exception:
            mtime_ns = -1
    else:
        mtime_ns = -1
    filename_key = resolved.name.lower() if resolved else ""
    return (
        -mtime_ns,
        filename_key,
        icloud_path.lower(),
        str(row["external_id"] or ""),
    )


def _mobile_materials_report(
    settings: Any,
    db: Database,
    check_limit: int = 5,
    check_all: bool = False,
) -> dict[str, Any]:
    sample_limit = max(int(check_limit), 1)
    storage_root = resolve_storage_root(settings)
    materials_dir = storage_materials_dir(storage_root) if storage_root else None
    expected_subdirs: list[dict[str, Any]] = []
    if storage_root:
        for path in expected_storage_subdirs(storage_root):
            expected_subdirs.append({"path": str(path), "exists": path.exists()})
    expected_subdirs_ok = bool(expected_subdirs) and all(
        bool(item["exists"]) for item in expected_subdirs
    )

    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM artifacts
            WHERE icloud_path IS NOT NULL AND TRIM(icloud_path) <> ''
            """
        ).fetchone()
        artifact_count = int(row["count"]) if row else 0
        rows = conn.execute(
            """
            SELECT external_id, source, icloud_path
            FROM artifacts
            WHERE icloud_path IS NOT NULL AND TRIM(icloud_path) <> ''
            """
        ).fetchall()
    sorted_rows = sorted(rows, key=_material_row_sort_key)
    if check_all:
        sample_rows = sorted_rows
        check_mode = "all"
    else:
        sample_rows = sorted_rows[:sample_limit]
        check_mode = "limit"

    artifact_checks: list[dict[str, Any]] = []
    for row in sample_rows:
        icloud_path = str(row["icloud_path"] or "")
        artifact_checks.append(
            {
                "external_id": str(row["external_id"] or ""),
                "source": str(row["source"] or ""),
                "icloud_path": icloud_path,
                "exists": bool(icloud_path) and Path(icloud_path).expanduser().exists(),
            }
        )
    artifacts_ok = all(bool(item["exists"]) for item in artifact_checks)
    materials_dir_exists = bool(materials_dir and materials_dir.exists())

    reasons: list[str] = []
    if not storage_root:
        reasons.append("STORAGE_ROOT_DIR not set")
    if not expected_subdirs_ok:
        reasons.append("expected storage subdirectories are missing")
    if storage_root and not materials_dir_exists:
        reasons.append("materials directory is missing")
    if not artifacts_ok:
        reasons.append("one or more artifact files are missing")

    return {
        "ok": expected_subdirs_ok and materials_dir_exists and artifacts_ok,
        "storage_root": str(storage_root) if storage_root else "",
        "materials_dir": str(materials_dir) if materials_dir else "",
        "materials_dir_exists": materials_dir_exists,
        "expected_subdirs": expected_subdirs,
        "expected_subdirs_ok": expected_subdirs_ok,
        "artifact_count_with_path": artifact_count,
        "artifact_count_with_icloud_path": artifact_count,
        "artifact_check_mode": check_mode,
        "artifact_check_limit": sample_limit,
        "checked_artifact_count": len(artifact_checks),
        "checked_paths": [str(item["icloud_path"]) for item in artifact_checks],
        "checked_artifact_paths": [str(item["icloud_path"]) for item in artifact_checks],
        "checked_artifacts": artifact_checks,
        "checked_artifacts_ok": artifacts_ok,
        "reasons": reasons,
    }


def _directory_inventory(path: Path, *, sample_limit: int = 20) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return {
            "path": str(resolved),
            "exists": False,
            "file_count": 0,
            "total_size_bytes": 0,
            "sample_files": [],
        }
    file_items: list[Path] = []
    total_size_bytes = 0
    for item in resolved.rglob("*"):
        if not item.is_file():
            continue
        file_items.append(item)
        try:
            total_size_bytes += int(item.stat().st_size)
        except OSError:
            continue
    file_items.sort(key=lambda item: str(item.relative_to(resolved)))
    sample_files = []
    for item in file_items[: max(int(sample_limit), 1)]:
        try:
            size_bytes = int(item.stat().st_size)
        except OSError:
            size_bytes = 0
        sample_files.append(
            {
                "relative_path": str(item.relative_to(resolved)),
                "size_bytes": size_bytes,
            }
        )
    return {
        "path": str(resolved),
        "exists": True,
        "file_count": len(file_items),
        "total_size_bytes": total_size_bytes,
        "sample_files": sample_files,
    }


def _summarize_json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"kind": "list", "count": len(value)}
    if isinstance(value, dict):
        return {"kind": "object", "count": len(value)}
    if value is None:
        return {"kind": "null", "count": 0}
    return {"kind": type(value).__name__, "count": 1}


def _dashboard_storage_report(root: Path) -> dict[str, Any]:
    dashboard_root = storage_dashboard_dir(root)
    report = _directory_inventory(dashboard_root)
    data_json = dashboard_root / "data.json"
    report["data_json_exists"] = data_json.exists()
    report["sections"] = {}
    if not data_json.exists():
        return report
    try:
        payload = json.loads(data_json.read_text(encoding="utf-8"))
    except Exception as exc:
        report["data_json_error"] = str(exc)
        return report
    if isinstance(payload, dict):
        report["sections"] = {
            str(key): _summarize_json_value(value)
            for key, value in payload.items()
        }
    return report


def _backup_storage_report(root: Path, *, sample_limit: int = 10) -> dict[str, Any]:
    backup_root = storage_backups_dir(root)
    report = _directory_inventory(backup_root, sample_limit=sample_limit)
    archives: list[dict[str, Any]] = []
    for item in sorted(backup_root.glob("*.zip"))[: max(int(sample_limit), 1)]:
        try:
            with zipfile.ZipFile(item, "r") as archive:
                entries = [
                    {"name": info.filename, "size_bytes": int(info.file_size)}
                    for info in archive.infolist()
                ]
        except Exception as exc:
            entries = [{"name": "__error__", "size_bytes": 0, "error": str(exc)}]
        archives.append(
            {
                "filename": item.name,
                "size_bytes": int(item.stat().st_size) if item.exists() else 0,
                "entries": entries,
            }
        )
    report["archives"] = archives
    return report


def _storage_report(settings: Any, db: Database, *, sample_limit: int = 20) -> dict[str, Any]:
    root = resolve_storage_root(settings)
    legacy_icloud_root = (
        Path.home()
        / "Library"
        / "Mobile Documents"
        / "com~apple~CloudDocs"
        / "SidaeSecretary"
    )
    legacy_report = _directory_inventory(legacy_icloud_root, sample_limit=sample_limit)
    if root is None:
        return {
            "ok": False,
            "configured": False,
            "storage_root": None,
            "cloud_synced": False,
            "error": "STORAGE_ROOT_DIR missing",
            "legacy_icloud_root": legacy_report,
        }
    root_resolved = Path(root).expanduser().resolve()
    artifact_rows = db.list_artifacts(limit=500)
    by_source: dict[str, int] = {}
    stored_paths: list[str] = []
    for item in artifact_rows:
        source = str(getattr(item, "source", "") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        stored_path = str(getattr(item, "icloud_path", "") or "").strip()
        if stored_path:
            stored_paths.append(stored_path)
    return {
        "ok": True,
        "configured": True,
        "storage_root": str(root_resolved),
        "cloud_synced": root_resolved == legacy_icloud_root.resolve(),
        "dashboard": _dashboard_storage_report(root_resolved),
        "materials": _directory_inventory(storage_materials_dir(root_resolved), sample_limit=sample_limit),
        "backups": _backup_storage_report(root_resolved, sample_limit=sample_limit),
        "browser_profiles": _directory_inventory(storage_browser_profiles_dir(root_resolved), sample_limit=sample_limit),
        "artifacts": {
            "count": len(artifact_rows),
            "by_source": by_source,
            "stored_paths": stored_paths[: max(int(sample_limit), 1)],
        },
        "legacy_icloud_root": legacy_report,
    }


def _mobile_offline_report(
    settings: Any,
    db: Database,
    max_age_hours: int,
    materials_check_limit: int = 5,
    materials_check_all: bool = False,
) -> dict[str, Any]:
    publish_state = db.get_sync_state("publish_dashboard")
    publish_cursor = _parse_json_field(publish_state.last_cursor_json)
    published_at = _parse_iso(publish_state.last_run_at)
    now_utc = datetime.now(timezone.utc)
    freshness_max = max(max_age_hours, 1)
    age_hours: float | None = None
    if published_at is not None:
        age_hours = round((now_utc - published_at.astimezone(timezone.utc)).total_seconds() / 3600, 2)
    dashboard_dir = str(publish_cursor.get("output") or "")
    dashboard_index_exists = False
    if dashboard_dir:
        dashboard_index_exists = (Path(dashboard_dir) / "index.html").exists()
    snapshot_fresh = (
        published_at is not None
        and age_hours is not None
        and age_hours <= freshness_max
        and dashboard_index_exists
    )

    storage_health = _storage_health_report(settings)
    materials_report = _mobile_materials_report(
        settings=settings,
        db=db,
        check_limit=materials_check_limit,
        check_all=materials_check_all,
    )

    checklist = [
        "Open the published dashboard index.html from the local storage root.",
        "Confirm upcoming classes, tasks, materials, and summaries are visible.",
        "Stop the Mac that runs Sidae Secretary sync.",
        "Re-open the published dashboard from local storage to confirm continuity.",
    ]
    return {
        "ok": snapshot_fresh and bool(storage_health.get("ok")) and bool(materials_report.get("ok")),
        "publish_dashboard": {
            "last_run_at": publish_state.last_run_at,
            "dashboard_dir": dashboard_dir,
            "dashboard_index_exists": dashboard_index_exists,
            "age_hours": age_hours,
            "max_age_hours": freshness_max,
            "fresh": snapshot_fresh,
        },
        "storage_health": storage_health,
        "materials": materials_report,
        "manual_checklist": checklist,
    }


def _export_db_payload(db: Database) -> dict[str, Any]:
    with db.connection() as conn:
        def rows(query: str) -> list[dict[str, Any]]:
            return [dict(item) for item in conn.execute(query).fetchall()]

        payload = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "events": rows(
                "SELECT external_id, source, start_at, end_at, title, location, rrule, metadata_json FROM events ORDER BY id ASC"
            ),
            "tasks": rows(
                "SELECT external_id, source, due_at, title, status, metadata_json FROM tasks ORDER BY id ASC"
            ),
            "artifacts": rows(
                "SELECT external_id, source, filename, icloud_path, content_hash, metadata_json FROM artifacts ORDER BY id ASC"
            ),
            "notifications": rows(
                "SELECT external_id, source, created_at, title, body, url, metadata_json FROM notifications ORDER BY id ASC"
            ),
            "inbox": rows(
                "SELECT external_id, source, received_at, title, body, item_type, draft_json, processed, metadata_json FROM inbox ORDER BY id ASC"
            ),
            "summaries": rows(
                "SELECT external_id, source, created_at, title, body, action_item, metadata_json FROM summaries ORDER BY id ASC"
            ),
            "sync_state": rows(
                "SELECT job_name, last_run_at, last_cursor_json FROM sync_state ORDER BY job_name ASC"
            ),
            "building_map": rows(
                "SELECT building_no, building_name, metadata_json, updated_at FROM building_map ORDER BY building_no ASC"
            ),
            "telegram_reminders": rows(
                "SELECT external_id, chat_id, run_at, message, status, sent_at, metadata_json FROM telegram_reminders ORDER BY id ASC"
            ),
        }
    return _scrub_secrets(payload)


def _import_db_payload(db: Database, payload: dict[str, Any]) -> dict[str, int]:
    counts = {
        "events": 0,
        "tasks": 0,
        "artifacts": 0,
        "notifications": 0,
        "inbox": 0,
        "summaries": 0,
        "sync_state": 0,
        "building_map": 0,
        "telegram_reminders": 0,
    }
    for item in payload.get("events", []):
        if not isinstance(item, dict):
            continue
        db.upsert_event(
            external_id=str(item.get("external_id") or ""),
            source=str(item.get("source") or "import"),
            start=str(item.get("start_at") or ""),
            end=str(item.get("end_at") or ""),
            title=str(item.get("title") or "Imported Event"),
            location=str(item.get("location") or "").strip() or None,
            rrule=str(item.get("rrule") or "").strip() or None,
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["events"] += 1
    for item in payload.get("tasks", []):
        if not isinstance(item, dict):
            continue
        db.upsert_task(
            external_id=str(item.get("external_id") or ""),
            source=str(item.get("source") or "import"),
            due_at=str(item.get("due_at") or "").strip() or None,
            title=str(item.get("title") or "Imported Task"),
            status=str(item.get("status") or "open"),
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["tasks"] += 1
    for item in payload.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        db.record_artifact(
            external_id=str(item.get("external_id") or ""),
            source=str(item.get("source") or "import"),
            filename=str(item.get("filename") or "artifact.bin"),
            icloud_path=str(item.get("icloud_path") or "").strip() or None,
            content_hash=str(item.get("content_hash") or "").strip() or None,
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["artifacts"] += 1
    for item in payload.get("notifications", []):
        if not isinstance(item, dict):
            continue
        db.upsert_notification(
            external_id=str(item.get("external_id") or ""),
            source=str(item.get("source") or "import"),
            created_at=str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
            title=str(item.get("title") or "Notification"),
            body=str(item.get("body") or "").strip() or None,
            url=str(item.get("url") or "").strip() or None,
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["notifications"] += 1
    for item in payload.get("inbox", []):
        if not isinstance(item, dict):
            continue
        db.upsert_inbox_item(
            external_id=str(item.get("external_id") or ""),
            source=str(item.get("source") or "import"),
            received_at=str(item.get("received_at") or datetime.now(timezone.utc).isoformat()),
            title=str(item.get("title") or "Inbox Item"),
            body=str(item.get("body") or "").strip() or None,
            item_type=str(item.get("item_type") or "note"),
            draft_json=_parse_json_field(item.get("draft_json")),
            processed=bool(item.get("processed")),
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["inbox"] += 1
    for item in payload.get("summaries", []):
        if not isinstance(item, dict):
            continue
        db.record_summary(
            external_id=str(item.get("external_id") or ""),
            source=str(item.get("source") or "import"),
            created_at=str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
            title=str(item.get("title") or "Summary"),
            body=str(item.get("body") or ""),
            action_item=str(item.get("action_item") or "").strip() or None,
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["summaries"] += 1
    for item in payload.get("sync_state", []):
        if not isinstance(item, dict):
            continue
        cursor = item.get("last_cursor_json")
        parsed_cursor = None
        if isinstance(cursor, str) and cursor.strip():
            try:
                parsed_cursor = json.loads(cursor)
            except Exception:
                parsed_cursor = {"raw": cursor}
        elif isinstance(cursor, dict):
            parsed_cursor = cursor
        db.update_sync_state(
            job_name=str(item.get("job_name") or "import"),
            last_run_at=str(item.get("last_run_at") or datetime.now(timezone.utc).isoformat()),
            last_cursor_json=parsed_cursor,
        )
        counts["sync_state"] += 1
    for item in payload.get("building_map", []):
        if not isinstance(item, dict):
            continue
        db.upsert_building(
            building_no=str(item.get("building_no") or "").strip(),
            building_name=str(item.get("building_name") or "").strip(),
            metadata_json=_parse_json_field(item.get("metadata_json")),
        )
        counts["building_map"] += 1
    for item in payload.get("telegram_reminders", []):
        if not isinstance(item, dict):
            continue
        external_id = str(item.get("external_id") or "").strip()
        chat_id = str(item.get("chat_id") or "").strip()
        run_at = str(item.get("run_at") or "").strip()
        message = str(item.get("message") or "").strip()
        if not external_id or not chat_id or not run_at or not message:
            continue
        db.upsert_telegram_reminder(
            external_id=external_id,
            chat_id=chat_id,
            run_at=run_at,
            message=message,
            metadata_json=_parse_json_field(item.get("metadata_json")),
            status=str(item.get("status") or "pending"),
        )
        counts["telegram_reminders"] += 1
    return counts


@app.command("doctor")
def doctor(
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    fix: bool = typer.Option(False, "--fix", help="Create missing local folders."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print config checks and initialize DB."""
    configure_logging()
    settings, db = _load_settings_and_init_db(config_file=config_file)
    created_dirs: list[str] = []
    failed_dirs: list[str] = []
    if fix:
        targets = [
            settings.database_path.parent,
            Path("data"),
        ]
        storage_root = resolve_storage_root(settings)
        if storage_root is not None:
            targets.append(storage_root)
            targets.extend(expected_storage_subdirs(storage_root))
        if settings.onboarding_browser_profiles_dir:
            targets.append(Path(settings.onboarding_browser_profiles_dir))
        for path in targets:
            if path.exists():
                continue
            try:
                path.mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(path))
            except Exception as exc:
                failed_dirs.append(f"{path} ({exc})")
    readiness = _doctor_readiness_report(settings=settings, db=db)
    missing = list(readiness.get("missing_required_config") or [])
    dep_report = readiness["deps"]
    counts = readiness["counts"]
    storage_health = readiness["storage_health"]
    secret_store = readiness["secret_store"]
    runtime = readiness["runtime"]
    operational = readiness["operational"]
    health = readiness["health"]

    if json_output:
        typer.echo(json.dumps(readiness, indent=2))
        if missing:
            raise typer.Exit(code=2)
        if runtime["python"].get("error"):
            raise typer.Exit(code=2)
        return

    _print_key_values("Config", settings.as_doctor_dict())
    typer.echo("Feature Flags")
    for name, enabled in readiness["feature_flags"].items():
        typer.echo(f"  {name}: {'ON' if enabled else 'OFF'}")
    typer.echo("Dependency Checks")
    for name, ok in dep_report.items():
        typer.echo(f"  {name}: {'OK' if ok else 'MISSING'}")
    typer.echo("Storage Health")
    typer.echo(f"  configured: {storage_health['configured']}")
    typer.echo(f"  path: {storage_health['path']}")
    typer.echo(f"  exists: {storage_health['exists']}")
    typer.echo(f"  writable: {storage_health['writable']}")
    for item in storage_health["subdirs"]:
        typer.echo(f"  subdir: {item['path']} exists={item['exists']}")
    typer.echo("Secret Store")
    typer.echo(f"  configured_backend: {secret_store['configured_backend']}")
    typer.echo(f"  preferred_backend: {secret_store['preferred_backend']}")
    typer.echo(f"  active_backend: {secret_store['active_backend']}")
    typer.echo(f"  keychain_available: {secret_store['keychain_available']}")
    typer.echo(f"  file_fallback_enabled: {secret_store['file_fallback_enabled']}")
    typer.echo(f"  legacy_file_read_compat: {secret_store['legacy_file_read_compat']}")
    typer.echo(f"  write_ready: {secret_store['write_ready']}")
    typer.echo("Runtime")
    typer.echo(f"  python_ok: {runtime['python']['ok']}")
    typer.echo(f"  python_current: {runtime['python']['current']}")
    typer.echo(f"  python_required_min: {runtime['python']['required_min']}")
    typer.echo(f"  ssl_ok: {runtime['ssl']['ok']}")
    typer.echo(f"  ssl_backend: {runtime['ssl']['backend']}")
    typer.echo(f"  ssl_version: {runtime['ssl']['version']}")
    if runtime["ssl"].get("warning"):
        typer.echo(f"  ssl_warning: {runtime['ssl']['warning']}")
    typer.echo("Operational Checks")
    typer.echo(f"  instance_name: {operational['instance_name']}")
    typer.echo(f"  is_beta_instance: {operational['is_beta_instance']}")
    typer.echo(
        f"  telegram_allowlist_configured: {operational['telegram_allowlist_configured']}"
    )
    typer.echo(f"  allowed_chat_count: {operational['allowed_chat_count']}")
    typer.echo(f"  onboarding_https_ready: {operational['onboarding_https_ready']}")
    typer.echo(f"  onboarding_scope_limited: {operational['onboarding_scope_limited']}")
    typer.echo(f"  uos_beta_scope: {operational['uos_beta_scope']}")
    typer.echo(
        "  onboarding_allowed_school_slugs: "
        + ",".join(list(operational["onboarding_allowed_school_slugs"]) or [])
    )
    if operational.get("onboarding_error"):
        typer.echo(f"  onboarding_error: {operational['onboarding_error']}")
    for warning in list(operational.get("warnings") or []):
        typer.echo(f"  warning: {warning}")
    _print_health_surface_summary(health)
    typer.echo("DB Counts")
    for table, count in counts.items():
        typer.echo(f"  {table}: {count}")
    if fix:
        typer.echo("Fixes")
        if created_dirs:
            for item in created_dirs:
                typer.echo(f"  created: {item}")
        else:
            typer.echo("  no new folders needed")
        for item in failed_dirs:
            typer.echo(f"  failed: {item}")
        typer.echo("Next Steps")
        typer.echo("  1. Run `sidae init` if config is incomplete.")
        typer.echo("  2. Run `sidae sync --all`.")

    if missing:
        typer.echo("")
        typer.echo("Missing required config: " + ", ".join(missing))
        raise typer.Exit(code=2)
    if runtime["python"].get("error"):
        typer.echo("")
        typer.echo(str(runtime["python"]["error"]))
        raise typer.Exit(code=2)
    typer.echo("")
    typer.echo("Doctor OK")


@app.command("init")
def init_config(
    config_file: Path = typer.Option(Path("config.toml"), help="Config TOML output path."),
    env_file: Path = typer.Option(Path(".env"), help="Env file output path."),
    force: bool = typer.Option(False, "--force", help="Overwrite files without prompt."),
) -> None:
    """Interactive setup wizard for baseline local configuration."""
    configure_logging()
    existing = load_settings(config_file=config_file if config_file.exists() else None)
    if config_file.exists() and not force:
        if not typer.confirm(f"{config_file} exists. Overwrite?", default=False):
            raise typer.Exit(code=1)
    if env_file.exists() and not force:
        if not typer.confirm(f"{env_file} exists. Overwrite?", default=False):
            raise typer.Exit(code=1)

    storage_root_default = str(resolve_storage_root(existing) or "")
    storage_root_dir = typer.prompt("STORAGE_ROOT_DIR", default=storage_root_default).strip()
    timezone_name = typer.prompt("TIMEZONE", default=existing.timezone or "Asia/Seoul").strip()

    uclass_ws_base = typer.prompt(
        "UCLASS_WS_BASE (optional for now)",
        default=existing.uclass_ws_base or "",
    ).strip()
    uclass_wstoken = typer.prompt(
        "UCLASS_WSTOKEN (optional for now)",
        default=existing.uclass_wstoken or "",
        hide_input=True,
    ).strip()

    enable_telegram = typer.confirm("Enable Telegram inbox/digest?", default=existing.telegram_enabled)
    telegram_allowed_ids = ",".join(existing.telegram_allowed_chat_ids)
    telegram_bot_token = ""
    if enable_telegram:
        telegram_bot_token = typer.prompt(
            "TELEGRAM_BOT_TOKEN",
            default=existing.telegram_bot_token or "",
            hide_input=True,
        ).strip()
        telegram_allowed_ids = typer.prompt(
            "TELEGRAM_ALLOWED_CHAT_IDS (comma-separated)",
            default=telegram_allowed_ids,
        ).strip()

    enable_llm = typer.confirm("Enable LLM summaries/briefing?", default=existing.llm_enabled)
    llm_provider = "local"
    llm_model = existing.llm_model or "gemma4"
    if enable_llm:
        typer.echo("LLM_PROVIDER is fixed to `local`.")
        llm_model = typer.prompt("LLM_MODEL", default=llm_model).strip()

    config_values: dict[str, Any] = {
        "INSTANCE_NAME": getattr(existing, "instance_name", ""),
        "STORAGE_ROOT_DIR": storage_root_dir,
        "TIMEZONE": timezone_name,
        "UCLASS_WS_BASE": uclass_ws_base,
        "TELEGRAM_ENABLED": enable_telegram,
        "TELEGRAM_ALLOWED_CHAT_IDS": telegram_allowed_ids,
        "ONBOARDING_ALLOWED_SCHOOL_SLUGS": list(
            getattr(existing, "onboarding_allowed_school_slugs", []) or []
        ),
        "LLM_ENABLED": enable_llm,
        "LLM_PROVIDER": llm_provider,
        "LLM_MODEL": llm_model,
        "INCLUDE_IDENTITY": bool(getattr(existing, "include_identity", False)),
    }
    config_lines = ["[sidae]"]
    for key, value in config_values.items():
        config_lines.append(f"{key} = {_toml_value(value)}")
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("\n".join(config_lines) + "\n", encoding="utf-8")

    env_lines = [
        f"UCLASS_WSTOKEN={uclass_wstoken}",
        f"UCLASS_TOKEN_SERVICE={getattr(existing, 'uclass_token_service', 'moodle_mobile_app') or 'moodle_mobile_app'}",
        f"UCLASS_TOKEN_ENDPOINT={getattr(existing, 'uclass_token_endpoint', '') or ''}",
        f"TELEGRAM_BOT_TOKEN={telegram_bot_token if enable_telegram else ''}",
        f"LLM_LOCAL_ENDPOINT={getattr(existing, 'llm_local_endpoint', 'http://127.0.0.1:11434/api/chat') or 'http://127.0.0.1:11434/api/chat'}",
        f"LLM_TIMEOUT_SEC={getattr(existing, 'llm_timeout_sec', 120) or 120}",
    ]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "config_file": str(config_file),
                "env_file": str(env_file),
                "next": ["sidae doctor --fix", "sidae sync --all"],
            },
            indent=2,
        )
    )


@app.command("sync")
def sync(
    all: bool = typer.Option(
        False, "--all", help="Run full sync pipeline once."
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for sync lock if another `sync --all` process is already running.",
    ),
    timeout_seconds: Optional[float] = typer.Option(
        None,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="With --wait, stop waiting after this many seconds.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Run sync jobs."""
    configure_logging()
    if not all:
        typer.echo("Only --all mode is supported.")
        raise typer.Exit(code=2)
    if timeout_seconds is not None and not wait:
        typer.echo("--timeout-seconds requires --wait.")
        raise typer.Exit(code=2)
    settings, db = _load_settings_and_init_db(config_file=config_file)
    try:
        report = _run_sync_all_once(
            settings=settings,
            db=db,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
    except _SyncLockBusyError:
        payload = _sync_lock_busy_payload(_sync_all_lock_path(settings.database_path))
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except _SyncLockTimeoutError as exc:
        payload = _sync_lock_timeout_payload(exc)
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    typer.echo(json.dumps({"ok": report["ok"], "stats": report["stats"], "errors": report["errors"]}, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)


@app.command("sync-telegram")
def sync_telegram_command(
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for sync lock if another `sidae sync-telegram` process is already running.",
    ),
    timeout_seconds: Optional[float] = typer.Option(
        None,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="With --wait, stop waiting after this many seconds.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Run Telegram polling + reminder dispatch only."""
    configure_logging()
    if timeout_seconds is not None and not wait:
        typer.echo("--timeout-seconds requires --wait.")
        raise typer.Exit(code=2)
    settings, db = _load_settings_and_init_db(config_file=config_file)
    try:
        report = _run_sync_telegram_once(
            settings=settings,
            db=db,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
    except _SyncLockBusyError:
        payload = {
            "ok": False,
            "error": "sync_telegram_lock_held",
            "message": "Another `sidae sync-telegram` process is running.",
            "lock_path": str(_sync_telegram_lock_path(settings.database_path)),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except _SyncLockTimeoutError as exc:
        payload = {
            "ok": False,
            "error": "sync_telegram_lock_timeout",
            "message": "Timed out while waiting for Telegram sync lock.",
            "lock_path": exc.lock_path,
            "waited_seconds": round(exc.waited_seconds, 3),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    typer.echo(json.dumps(report, indent=2))


@app.command("sync-uclass")
def sync_uclass_command(
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for sync lock if another `sidae sync-uclass` process is already running.",
    ),
    timeout_seconds: Optional[float] = typer.Option(
        None,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="With --wait, stop waiting after this many seconds.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Run UClass ingestion + local review scheduling only."""
    configure_logging()
    if timeout_seconds is not None and not wait:
        typer.echo("--timeout-seconds requires --wait.")
        raise typer.Exit(code=2)
    settings, db = _load_settings_and_init_db(config_file=config_file)
    try:
        report = _run_sync_uclass_once(
            settings=settings,
            db=db,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
    except _SyncLockBusyError:
        payload = {
            "ok": False,
            "error": "sync_uclass_lock_held",
            "message": "Another `sidae sync-uclass` process is running.",
            "lock_path": str(_sync_uclass_lock_path(settings.database_path)),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except _SyncLockTimeoutError as exc:
        payload = {
            "ok": False,
            "error": "sync_uclass_lock_timeout",
            "message": "Timed out while waiting for UClass sync lock.",
            "lock_path": exc.lock_path,
            "waited_seconds": round(exc.waited_seconds, 3),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    typer.echo(json.dumps(report, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)

@app.command("sync-weather")
def sync_weather_command(
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for sync lock if another `sidae sync-weather` process is already running.",
    ),
    timeout_seconds: Optional[float] = typer.Option(
        None,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="With --wait, stop waiting after this many seconds.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Run weather + Seoul air-quality snapshot sync only."""
    configure_logging()
    if timeout_seconds is not None and not wait:
        typer.echo("--timeout-seconds requires --wait.")
        raise typer.Exit(code=2)
    settings, db = _load_settings_and_init_db(config_file=config_file)
    try:
        report = _run_sync_weather_once(
            settings=settings,
            db=db,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
    except _SyncLockBusyError:
        payload = {
            "ok": False,
            "error": "sync_weather_lock_held",
            "message": "Another `sidae sync-weather` process is running.",
            "lock_path": str(_sync_weather_lock_path(settings.database_path)),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except _SyncLockTimeoutError as exc:
        payload = {
            "ok": False,
            "error": "sync_weather_lock_timeout",
            "message": "Timed out while waiting for weather sync lock.",
            "lock_path": exc.lock_path,
            "waited_seconds": round(exc.waited_seconds, 3),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    typer.echo(json.dumps(report, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)


@app.command("send-briefings")
def send_briefings_command(
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for sync lock if another `sidae send-briefings` process is already running.",
    ),
    timeout_seconds: Optional[float] = typer.Option(
        None,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="With --wait, stop waiting after this many seconds.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Send morning/evening Telegram briefings if a scheduled slot is due."""
    configure_logging()
    if timeout_seconds is not None and not wait:
        typer.echo("--timeout-seconds requires --wait.")
        raise typer.Exit(code=2)
    settings, db = _load_settings_and_init_db(config_file=config_file)
    try:
        report = _run_send_briefings_once(
            settings=settings,
            db=db,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
    except _SyncLockBusyError:
        payload = {
            "ok": False,
            "error": "send_briefings_lock_held",
            "message": "Another `sidae send-briefings` process is running.",
            "lock_path": str(_send_briefings_lock_path(settings.database_path)),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except _SyncLockTimeoutError as exc:
        payload = {
            "ok": False,
            "error": "send_briefings_lock_timeout",
            "message": "Timed out while waiting for briefing send lock.",
            "lock_path": exc.lock_path,
            "waited_seconds": round(exc.waited_seconds, 3),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    typer.echo(json.dumps(report, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)


def _uclass_host_reachable(ws_base: str, timeout_sec: float = 5.0) -> bool:
    parsed = urlparse(str(ws_base or "").strip())
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        with socket.create_connection((host, int(port)), timeout=max(float(timeout_sec), 0.1)):
            return True
    except OSError:
        return False


def _briefings_direct_enabled(settings: Any) -> bool:
    if not bool(getattr(settings, "briefing_enabled", False)):
        return False
    channel = str(getattr(settings, "briefing_channel", "telegram") or "telegram").strip().lower()
    if channel != "telegram":
        return False
    delivery_mode = str(getattr(settings, "briefing_delivery_mode", "direct") or "direct").strip().lower()
    return delivery_mode == "direct"


def _telegram_listener_sync(
    settings: Any,
    db: Database,
    *,
    client: Any,
    poll_timeout_seconds: int,
) -> dict[str, Any]:
    return sync_telegram_job(
        settings,
        db,
        client=client,
        poll_timeout=max(int(poll_timeout_seconds), 1),
    )


def _run_telegram_listener_cycle(
    settings: Any,
    db: Database,
    *,
    client: Any,
    poll_timeout_seconds: int,
) -> dict[str, Any]:
    telegram_result = _telegram_listener_sync(
        settings=settings,
        db=db,
        client=client,
        poll_timeout_seconds=poll_timeout_seconds,
    )
    briefings_result: dict[str, Any] | None = None
    if _briefings_direct_enabled(settings):
        try:
            briefings_result = _run_send_briefings_once(
                settings=settings,
                db=db,
                wait=False,
                timeout_seconds=None,
            )
        except _SyncLockBusyError:
            briefings_result = {
                "ok": False,
                "stats": {"skipped": True, "reason": "send_briefings_lock_held"},
                "errors": [],
            }
    return {
        "telegram": telegram_result,
        "briefings": briefings_result,
    }


def _telegram_listener_cycle_has_activity(report: dict[str, Any]) -> bool:
    telegram = report.get("telegram") if isinstance(report.get("telegram"), dict) else {}
    telegram = telegram if isinstance(telegram, dict) else {}
    if telegram.get("error"):
        return True
    fetched = int(telegram.get("fetched_updates") or 0)
    stored = int(telegram.get("stored_messages") or 0)
    commands = telegram.get("commands") if isinstance(telegram.get("commands"), dict) else {}
    commands = commands if isinstance(commands, dict) else {}
    reminders = telegram.get("reminders") if isinstance(telegram.get("reminders"), dict) else {}
    reminders = reminders if isinstance(reminders, dict) else {}
    if fetched or stored or int(commands.get("processed") or 0) or int(reminders.get("sent") or 0):
        return True
    briefings = report.get("briefings") if isinstance(report.get("briefings"), dict) else {}
    briefings = briefings if isinstance(briefings, dict) else {}
    briefing_stats = briefings.get("stats") if isinstance(briefings.get("stats"), dict) else {}
    briefing_stats = briefing_stats if isinstance(briefing_stats, dict) else {}
    return bool(briefing_stats.get("sent_slots"))


def _telegram_listener_cycle_error(report: dict[str, Any]) -> str | None:
    telegram = report.get("telegram") if isinstance(report.get("telegram"), dict) else {}
    telegram = telegram if isinstance(telegram, dict) else {}
    error = str(telegram.get("error") or "").strip()
    return error or None


@app.command("telegram-listener")
def telegram_listener_command(
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
    once: bool = typer.Option(
        False,
        "--once",
        help="Run one long-poll cycle and exit.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for the Telegram listener/sync lock if another process already holds it.",
    ),
    timeout_seconds: Optional[float] = typer.Option(
        None,
        "--timeout-seconds",
        "--timeout",
        min=0.01,
        help="With --wait, stop waiting after this many seconds.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Keep a long-running Telegram long-poll listener alive."""
    configure_logging()
    if timeout_seconds is not None and not wait:
        typer.echo("--timeout-seconds requires --wait.")
        raise typer.Exit(code=2)
    settings, db = _load_settings_and_init_db(config_file=config_file)
    if not bool(getattr(settings, "telegram_enabled", False)):
        payload = {"ok": True, "skipped": True, "reason": "TELEGRAM_ENABLED is false"}
        typer.echo(json.dumps(payload, indent=2))
        return
    bot_token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    if not bot_token:
        payload = {"ok": True, "skipped": True, "reason": "TELEGRAM_BOT_TOKEN missing"}
        typer.echo(json.dumps(payload, indent=2))
        return
    lock_path = _sync_telegram_lock_path(settings.database_path)
    try:
        with _sync_all_execution_lock(
            lock_path=lock_path,
            wait=wait,
            timeout_seconds=timeout_seconds,
        ):
            client = TelegramBotClient(
                bot_token,
                timeout_sec=max(int(poll_timeout_seconds), 5),
            )
            cycle_count = 0
            consecutive_error_cycles = 0
            while True:
                cycle_count += 1
                cycle_report = _run_telegram_listener_cycle(
                    settings=settings,
                    db=db,
                    client=client,
                    poll_timeout_seconds=poll_timeout_seconds,
                )
                if once:
                    typer.echo(
                        json.dumps(
                            {
                                "ok": True,
                                "cycles": cycle_count,
                                "lock_path": str(lock_path),
                                "report": cycle_report,
                            },
                            indent=2,
                        )
                    )
                    return
                if _telegram_listener_cycle_has_activity(cycle_report):
                    logger.info(
                        "telegram listener cycle completed",
                        extra={"cycle": cycle_count, "report": cycle_report},
                    )
                error_message = _telegram_listener_cycle_error(cycle_report)
                if error_message:
                    consecutive_error_cycles += 1
                    logger.warning(
                        "telegram listener consecutive error",
                        extra={
                            "cycle": cycle_count,
                            "consecutive_errors": consecutive_error_cycles,
                            "max_consecutive_errors": max_consecutive_errors,
                            "error": error_message,
                        },
                    )
                    if consecutive_error_cycles >= int(max_consecutive_errors):
                        logger.error(
                            "telegram listener exiting for launchd restart after consecutive errors",
                            extra={
                                "cycle": cycle_count,
                                "consecutive_errors": consecutive_error_cycles,
                                "max_consecutive_errors": max_consecutive_errors,
                                "error": error_message,
                            },
                        )
                        raise typer.Exit(code=1)
                    time.sleep(float(error_backoff_seconds))
                    continue
                consecutive_error_cycles = 0
    except _SyncLockBusyError:
        payload = {
            "ok": False,
            "error": "telegram_listener_lock_held",
            "message": "Another `sidae sync-telegram` or `sidae telegram-listener` process is running.",
            "lock_path": str(lock_path),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except _SyncLockTimeoutError as exc:
        payload = {
            "ok": False,
            "error": "telegram_listener_lock_timeout",
            "message": "Timed out while waiting for Telegram listener lock.",
            "lock_path": exc.lock_path,
            "waited_seconds": round(exc.waited_seconds, 3),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=4)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


@app.command("uclass-poller")
def uclass_poller_command(
    interval_minutes: int = typer.Option(
        60,
        "--interval-minutes",
        min=1,
        help="While network stays available, run `sync-uclass` at this interval.",
    ),
    connectivity_check_seconds: int = typer.Option(
        30,
        "--connectivity-check-seconds",
        min=5,
        help="How often to re-check UClass host reachability while waiting.",
    ),
    sync_timeout_seconds: int = typer.Option(
        600,
        "--sync-timeout-seconds",
        min=1,
        help="Pass --timeout to the internal `sync-uclass --wait` run.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Keep a UClass-first collector alive: run once on network availability, then hourly."""
    configure_logging()
    settings, db = _load_settings_and_init_db(config_file=config_file)
    interval_seconds = max(int(interval_minutes), 1) * 60
    probe_seconds = max(int(connectivity_check_seconds), 5)
    last_network_up = False
    last_sync_started_at: float | None = None
    ws_base = str(getattr(settings, "uclass_ws_base", "") or "").strip()

    while True:
        try:
            network_up = _uclass_host_reachable(ws_base)
            if network_up and not last_network_up:
                logger.info("uclass poller network became reachable", extra={"ws_base": ws_base})
            if (not network_up) and last_network_up:
                logger.info("uclass poller network became unreachable", extra={"ws_base": ws_base})

            should_run = False
            if network_up:
                elapsed = (
                    None
                    if last_sync_started_at is None
                    else max(time.monotonic() - last_sync_started_at, 0.0)
                )
                should_run = (
                    not last_network_up
                    or last_sync_started_at is None
                    or (elapsed is not None and elapsed >= interval_seconds)
                )

            if should_run:
                uclass_report = _run_sync_uclass_once(
                    settings=settings,
                    db=db,
                    wait=True,
                    timeout_seconds=float(sync_timeout_seconds),
                )
                last_sync_started_at = time.monotonic()
                logger.info(
                    "uclass poller cycle completed",
                    extra={"uclass_report": uclass_report},
                )

            last_network_up = network_up
            if network_up and last_sync_started_at is not None:
                elapsed = max(time.monotonic() - last_sync_started_at, 0.0)
                remaining = max(interval_seconds - elapsed, 1.0)
                sleep_for = min(float(probe_seconds), remaining)
            else:
                sleep_for = float(probe_seconds)
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            raise typer.Exit(code=130)
        except Exception as exc:
            logger.exception("uclass poller loop failed", extra={"error": str(exc)})
            last_network_up = False
            time.sleep(float(probe_seconds))


@app.command("gui")
def gui(config_file: Optional[Path] = typer.Option(None, help="Path to config TOML.")) -> None:
    """Open a small desktop control panel for manual sync actions."""
    from sidae_secretary.gui import launch_gui

    launch_gui(config_file=select_config_path(config_file=config_file), python_executable=sys.executable)


@app.command("publish")
def publish(config_file: Optional[Path] = typer.Option(None, help="Path to config TOML.")) -> None:
    """Publish static dashboard snapshot to local storage."""
    configure_logging()
    settings, db = _load_settings_and_init_db(config_file=config_file)
    result = publish_dashboard(settings, db)
    typer.echo(json.dumps(result, indent=2))


@app.command("status")
def status(config_file: Optional[Path] = typer.Option(None, help="Path to config TOML.")) -> None:
    """Show latest DB counts and last run state."""
    configure_logging()
    settings, db = _load_settings_and_init_db(config_file=config_file)
    counts = db.counts()
    sync_dashboard = db.sync_dashboard_snapshot()
    auth_monitor = db.auth_attempt_dashboard_snapshot()
    health = build_beta_ops_health_report(settings, db)
    sync_states = [
        {
            "job_name": row.job_name,
            "last_run_at": row.last_run_at,
            "last_cursor_json": row.last_cursor_json,
        }
        for row in db.list_sync_states()
    ]
    typer.echo(
        json.dumps(
            {
                "counts": counts,
                "sync_dashboard": sync_dashboard,
                "auth_monitor": auth_monitor,
                "health": health,
                "sync_state": sync_states,
                "deps": _dependency_checks(settings=settings),
                "feature_flags": _feature_flag_report(settings),
                "storage_health": _storage_health_report(settings),
                "secret_store": secret_store_report(settings),
                "runtime": _runtime_environment_report(),
            },
            indent=2,
        )
    )


@app.command("storage-report")
def storage_report_command(
    sample_limit: int = typer.Option(
        20,
        "--sample-limit",
        min=1,
        max=200,
        help="Maximum sample files or stored paths per section.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Show exactly what Sidae Secretary stores under local storage and any legacy iCloud root."""
    configure_logging()
    settings, db = _load_settings_and_init_db(config_file=config_file)
    typer.echo(json.dumps(_storage_report(settings=settings, db=db, sample_limit=sample_limit), indent=2))


@ack_app.command("identity")
def ack_identity(
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Optional human ACK token to store with this acknowledgement.",
    ),
    expires_hours: float = typer.Option(
        24.0,
        "--expires-hours",
        min=0.01,
        help="Validity window for ACK before identity-gated sends are blocked again.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Acknowledge identity exposure risk for gated outbound sends."""
    configure_logging()
    settings, db = _load_settings_and_init_db(config_file=config_file)
    ack_token = str(token or "").strip() or secrets.token_urlsafe(12)
    acked_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at_dt = acked_at_dt + timedelta(hours=max(float(expires_hours), 0.01))
    ack_row = db.record_identity_ack(
        token=ack_token,
        expires_at=expires_at_dt.isoformat(),
        metadata_json={
            "source": "cli",
            "include_identity": bool(getattr(settings, "include_identity", False)),
        },
    )
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "ack": {
                    "token": ack_row["token"],
                    "acknowledged_at": ack_row["acknowledged_at"],
                    "expires_at": ack_row["expires_at"],
                },
            },
            indent=2,
        )
    )


@app.command("docs-artifacts")
def docs_artifacts(
    operation: str = typer.Argument(
        "sync",
        help="Operation mode: sync or check.",
    ),
    check: bool = typer.Option(False, "--check", help="Validate docs artifacts without writing."),
    docs_dir: Path = typer.Option(Path("docs"), "--docs-dir", help="Docs directory path."),
    repo_root: Path = typer.Option(Path("."), "--repo-root", help="Repository root path."),
    generated_at: Optional[str] = typer.Option(
        None,
        "--generated-at",
        help="Override generated_at timestamp when syncing.",
    ),
    require_clean_git: bool = typer.Option(
        False,
        "--require-clean-git",
        help="Fail if working tree is dirty.",
    ),
) -> None:
    """Sync or validate docs snapshot/audit artifacts against current git metadata."""
    configure_logging()
    mode = str(operation or "sync").strip().lower()
    if mode not in {"sync", "check"}:
        typer.echo(json.dumps({"ok": False, "error": "operation must be 'sync' or 'check'"}, indent=2))
        raise typer.Exit(code=2)
    check_mode = check or mode == "check"
    docs_path = docs_dir.expanduser().resolve()
    repo_path = repo_root.expanduser().resolve()
    try:
        if check_mode:
            result = docs_artifacts_module.check_docs_artifacts_consistency(
                docs_dir=docs_path,
                repo_root=repo_path,
                require_clean_git=require_clean_git,
            )
        else:
            result = docs_artifacts_module.sync_docs_artifacts(
                docs_dir=docs_path,
                repo_root=repo_path,
                generated_at=generated_at,
                require_clean_git=require_clean_git,
            )
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        raise typer.Exit(code=1)
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("export")
def export_json(
    json_out: Path = typer.Option(Path("data/export.json"), "--json-out", help="Output JSON path."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Export DB state to JSON (secrets redacted)."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    payload = _export_db_payload(db)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    typer.echo(json.dumps({"ok": True, "json_out": str(json_out)}, indent=2))


@app.command("backup")
def backup(
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Create backup zip with DB + JSON snapshot in local storage."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    storage_root = resolve_storage_root(settings)
    if storage_root is None:
        typer.echo("STORAGE_ROOT_DIR is required for backup")
        raise typer.Exit(code=2)
    db = Database(settings.database_path)
    db.init()
    backup_dir = storage_backups_dir(storage_root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_path = backup_dir / f"export-{timestamp}.json"
    export_path.write_text(
        json.dumps(_export_db_payload(db), ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    zip_path = backup_dir / f"sidae-backup-{timestamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if settings.database_path.exists():
            zf.write(settings.database_path, arcname="sidae.db")
        zf.write(export_path, arcname="export.json")
        probe_path = Path("data/uclass_probe.json")
        if probe_path.exists():
            zf.write(probe_path, arcname="uclass_probe.json")
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "zip": str(zip_path),
                "export_json": str(export_path),
            },
            indent=2,
        )
    )


@app.command("import")
def import_json(
    json_file: Path = typer.Option(..., "--json", help="Export JSON file path."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Import minimal state from export JSON."""
    configure_logging()
    if not json_file.exists():
        typer.echo(f"missing file: {json_file}")
        raise typer.Exit(code=2)
    try:
        payload = json.loads(json_file.read_text(encoding="utf-8"))
    except Exception as exc:
        typer.echo(f"invalid json: {exc}")
        raise typer.Exit(code=2)
    if not isinstance(payload, dict):
        typer.echo("invalid export payload")
        raise typer.Exit(code=2)
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    counts = _import_db_payload(db, payload)
    typer.echo(json.dumps({"ok": True, "imported": counts}, indent=2))


@uclass_app.command("probe")
def uclass_probe(
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    write_json: bool = typer.Option(False, "--write-json", help="Write report to data/uclass_probe.json."),
    json_out: Optional[Path] = typer.Option(None, "--json-out", help="Custom report JSON output path."),
) -> None:
    """Probe configured UClass wsfunctions and print availability matrix."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    output_path = json_out
    if write_json and output_path is None:
        output_path = Path("data/uclass_probe.json")
    try:
        report = run_uclass_probe(settings=settings, db=db, output_json_path=output_path)
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        raise typer.Exit(code=2)

    site_info = report.get("site_info", {}) if isinstance(report, dict) else {}
    typer.echo("Site Info")
    for key in ("userid", "username", "fullname", "sitename", "siteurl", "release", "version"):
        typer.echo(f"  {key}: {site_info.get(key) or '-'}")
    if report.get("site_info_error"):
        typer.echo(f"  error: {report['site_info_error']}")
    typer.echo("")
    typer.echo("WSFunction Probe Matrix")
    typer.echo("  status | key | wsfunction | error")
    for row in report.get("rows", []):
        status_name = str(row.get("status") or "UNKNOWN")
        key = str(row.get("key") or "-")
        wsfunction = str(row.get("wsfunction") or "-")
        error = str(row.get("error") or "")
        typer.echo(f"  {status_name:<6} | {key:<20} | {wsfunction:<55} | {error}")
        fingerprint = str(row.get("shape_fingerprint") or "").strip()
        if fingerprint:
            typer.echo(f"    shape: {fingerprint}")
    if output_path:
        typer.echo("")
        typer.echo(f"JSON report: {output_path}")


@inbox_app.command("list")
def inbox_list(
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    """List unprocessed inbox items."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    items = db.list_unprocessed_inbox(limit=limit)
    payload = []
    for item in items:
        payload.append(
            {
                "id": item.id,
                "external_id": item.external_id,
                "received_at": item.received_at,
                "title": item.title,
                "item_type": item.item_type,
                "draft": json.loads(item.draft_json or "{}"),
            }
        )
    typer.echo(json.dumps({"count": len(payload), "items": payload}, indent=2))


@inbox_app.command("apply")
def inbox_apply(
    id: Optional[int] = typer.Option(None, "--id", help="Inbox row id to apply."),
    all: bool = typer.Option(False, "--all", help="Apply all unprocessed inbox items."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Apply inbox drafts into actionable events/tasks."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    try:
        result = apply_inbox_items(settings=settings, db=db, item_id=id, apply_all=all)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)
    typer.echo(json.dumps(result, indent=2))


@inbox_app.command("ignore")
def inbox_ignore(
    id: int = typer.Option(..., "--id", help="Inbox row id to ignore."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Ignore inbox item and mark as processed."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    result = ignore_inbox_item(db=db, item_id=id)
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@tasks_app.command("list")
def tasks_list(
    open: bool = typer.Option(False, "--open", help="Only show open tasks."),
    limit: int = typer.Option(100, "--limit", min=1, max=1000),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """List tasks."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    with db.connection() as conn:
        if open:
            rows = conn.execute(
                """
                SELECT id, external_id, source, due_at, title, status
                FROM tasks
                WHERE status = 'open'
                ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, external_id, source, due_at, title, status
                FROM tasks
                ORDER BY COALESCE(due_at, '9999-01-01T00:00:00+00:00') ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    payload = [dict(row) for row in rows]
    typer.echo(json.dumps({"count": len(payload), "items": payload}, indent=2))


@tasks_app.command("done")
def tasks_done(
    id: str = typer.Option(..., "--id", help="Task row id or external_id."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Mark task as done."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    result = mark_task_status(settings=settings, db=db, selector=id, status="done")
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@tasks_app.command("ignore")
def tasks_ignore(
    id: str = typer.Option(..., "--id", help="Task row id or external_id."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Mark task as ignored."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    result = mark_task_status(settings=settings, db=db, selector=id, status="ignored")
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise typer.Exit(code=1)


def _course_alias_payload(item: Any) -> dict[str, Any]:
    metadata = {}
    try:
        metadata = json.loads(str(getattr(item, "metadata_json", "") or "{}"))
    except Exception:
        metadata = {}
    return {
        "id": int(getattr(item, "id", 0) or 0),
        "canonical_course_id": str(getattr(item, "canonical_course_id", "") or ""),
        "alias": str(getattr(item, "alias", "") or ""),
        "normalized_alias": str(getattr(item, "normalized_alias", "") or ""),
        "alias_type": str(getattr(item, "alias_type", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "metadata_json": metadata,
    }


def _course_payload(
    item: Any,
    aliases: list[Any] | None = None,
    *,
    alias_count: int | None = None,
) -> dict[str, Any]:
    metadata = {}
    try:
        metadata = json.loads(str(getattr(item, "metadata_json", "") or "{}"))
    except Exception:
        metadata = {}
    payload = {
        "canonical_course_id": str(getattr(item, "canonical_course_id", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "external_course_id": str(getattr(item, "external_course_id", "") or "") or None,
        "display_name": str(getattr(item, "display_name", "") or ""),
        "metadata_json": metadata,
    }
    alias_items = list(aliases or [])
    payload["alias_count"] = int(alias_count if alias_count is not None else len(alias_items))
    if aliases is not None:
        payload["aliases"] = [_course_alias_payload(alias) for alias in alias_items]
    return payload


def _resolve_course_selector(
    db: Database,
    selector: str,
) -> tuple[Any | None, str | None, list[dict[str, Any]]]:
    selected = str(selector or "").strip()
    if not selected:
        return None, "course selector is required", []
    courses = db.list_courses(limit=5000)
    selected_casefold = selected.casefold()
    normalized_selected = normalize_course_alias(selected)

    def _unique(items: list[Any]) -> list[Any]:
        seen: set[str] = set()
        output: list[Any] = []
        for item in items:
            canonical_course_id = str(getattr(item, "canonical_course_id", "") or "").strip()
            if not canonical_course_id or canonical_course_id in seen:
                continue
            seen.add(canonical_course_id)
            output.append(item)
        return output

    exact_id = [item for item in courses if str(item.canonical_course_id) == selected]
    if len(exact_id) == 1:
        return exact_id[0], None, []

    exact_external = [
        item
        for item in courses
        if str(getattr(item, "external_course_id", "") or "").strip() == selected
    ]
    exact_external = _unique(exact_external)
    if len(exact_external) == 1:
        return exact_external[0], None, []
    if len(exact_external) > 1:
        return None, "ambiguous course selector", [_course_payload(item) for item in exact_external]

    exact_display = [
        item for item in courses if str(getattr(item, "display_name", "") or "").strip().casefold() == selected_casefold
    ]
    exact_display = _unique(exact_display)
    if len(exact_display) == 1:
        return exact_display[0], None, []
    if len(exact_display) > 1:
        return None, "ambiguous course selector", [_course_payload(item) for item in exact_display]

    normalized_display = [
        item
        for item in courses
        if normalize_course_alias(str(getattr(item, "display_name", "") or "")) == normalized_selected
    ]
    alias_rows = db.list_course_aliases(normalized_alias=normalized_selected, limit=100)
    by_course_id = {str(item.canonical_course_id): item for item in courses}
    alias_courses = [
        by_course_id[str(row.canonical_course_id)]
        for row in alias_rows
        if str(row.canonical_course_id) in by_course_id
    ]
    candidates = _unique(normalized_display + alias_courses)
    if len(candidates) == 1:
        return candidates[0], None, []
    if len(candidates) > 1:
        return None, "ambiguous course selector", [_course_payload(item) for item in candidates]
    return None, f"course not found: {selected}", []


@courses_app.command("list")
def courses_list(
    limit: int = typer.Option(500, "--limit", min=1, max=5000),
    aliases: bool = typer.Option(False, "--aliases/--no-aliases", help="Include alias rows per course."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """List canonical courses and optional alias mappings."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    course_items = db.list_courses(limit=limit)
    alias_rows = db.list_course_aliases(limit=max(limit * 20, 1000))
    aliases_by_course: dict[str, list[Any]] = {}
    for row in alias_rows:
        aliases_by_course.setdefault(str(row.canonical_course_id), []).append(row)
    payload = [
        _course_payload(
            item,
            aliases=aliases_by_course.get(str(item.canonical_course_id), []) if aliases else None,
            alias_count=len(aliases_by_course.get(str(item.canonical_course_id), [])),
        )
        for item in course_items
    ]
    typer.echo(json.dumps({"count": len(payload), "items": payload}, indent=2))


@courses_app.command("resolve")
def courses_resolve(
    alias: str = typer.Option(..., "--alias", help="Course alias/title to resolve."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Resolve an alias/title to canonical course ids."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    normalized_alias = normalize_course_alias(alias)
    alias_rows = db.list_course_aliases(normalized_alias=normalized_alias, limit=100)
    courses_by_id = {
        str(item.canonical_course_id): item
        for item in db.list_courses(limit=5000)
    }
    grouped_rows: dict[str, list[Any]] = {}
    for row in alias_rows:
        grouped_rows.setdefault(str(row.canonical_course_id), []).append(row)
    payload = [
        _course_payload(courses_by_id[course_id], aliases=rows)
        for course_id, rows in grouped_rows.items()
        if course_id in courses_by_id
    ]
    result = {
        "ok": bool(payload),
        "alias": alias,
        "normalized_alias": normalized_alias,
        "count": len(payload),
        "items": payload,
    }
    typer.echo(json.dumps(result, indent=2))
    if not payload:
        raise typer.Exit(code=1)


@courses_app.command("alias-add")
def courses_alias_add(
    course: str = typer.Option(..., "--course", help="Course selector: canonical id, external id, display name, or existing alias."),
    alias: str = typer.Option(..., "--alias", help="Alias/title to bind to the course."),
    alias_type: str = typer.Option("manual", "--type", help="Alias type label."),
    source: str = typer.Option("cli", "--source", help="Alias source label."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Add a manual course alias."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    selected_course, error, candidates = _resolve_course_selector(db, course)
    if selected_course is None:
        typer.echo(json.dumps({"ok": False, "error": error, "candidates": candidates}, indent=2))
        raise typer.Exit(code=1)
    normalized_alias = normalize_course_alias(alias)
    if not normalized_alias:
        typer.echo(json.dumps({"ok": False, "error": "alias is empty after normalization"}, indent=2))
        raise typer.Exit(code=1)
    conflicts = [
        row
        for row in db.list_course_aliases(normalized_alias=normalized_alias, limit=100)
        if str(row.canonical_course_id) != str(selected_course.canonical_course_id)
    ]
    if conflicts:
        conflict_ids = sorted({str(row.canonical_course_id) for row in conflicts})
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "alias_conflict",
                    "alias": alias,
                    "normalized_alias": normalized_alias,
                    "conflict_course_ids": conflict_ids,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    created = db.upsert_course_alias(
        canonical_course_id=str(selected_course.canonical_course_id),
        alias=alias,
        alias_type=alias_type,
        source=source,
        metadata_json={"source": "cli", "selector": course},
    )
    aliases_for_course = db.list_course_aliases(
        canonical_course_id=str(selected_course.canonical_course_id),
        limit=200,
    )
    typer.echo(
        json.dumps(
            {
                "ok": created is not None,
                "course": _course_payload(selected_course, aliases=aliases_for_course),
                "alias": _course_alias_payload(created) if created is not None else None,
            },
            indent=2,
        )
    )
    if created is None:
        raise typer.Exit(code=1)


@courses_app.command("alias-remove")
def courses_alias_remove(
    course: str = typer.Option(..., "--course", help="Course selector: canonical id, external id, display name, or existing alias."),
    alias: str = typer.Option(..., "--alias", help="Alias/title to remove."),
    alias_type: str = typer.Option("manual", "--type", help="Alias type filter. Use '*' to ignore."),
    source: str = typer.Option("cli", "--source", help="Alias source filter. Use '*' to ignore."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Remove a course alias."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    selected_course, error, candidates = _resolve_course_selector(db, course)
    if selected_course is None:
        typer.echo(json.dumps({"ok": False, "error": error, "candidates": candidates}, indent=2))
        raise typer.Exit(code=1)
    deleted = db.delete_course_alias(
        canonical_course_id=str(selected_course.canonical_course_id),
        alias=alias,
        alias_type=None if alias_type.strip() in {"*", "any"} else alias_type,
        source=None if source.strip() in {"*", "any"} else source,
    )
    aliases_for_course = db.list_course_aliases(
        canonical_course_id=str(selected_course.canonical_course_id),
        limit=200,
    )
    typer.echo(
        json.dumps(
            {
                "ok": deleted > 0,
                "deleted": deleted,
                "course": _course_payload(selected_course, aliases=aliases_for_course),
                "alias": alias,
            },
            indent=2,
        )
    )
    if deleted <= 0:
        raise typer.Exit(code=1)


@buildings_app.command("set")
def buildings_set(
    number: str = typer.Option(..., "--number", help="Building number token, e.g. 20"),
    name: str = typer.Option(..., "--name", help="Human-readable building name."),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Upsert one building number->name mapping."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    row = db.upsert_building(
        building_no=number,
        building_name=name,
        metadata_json={"source": "cli"},
    )
    typer.echo(json.dumps({"ok": True, "building": row}, indent=2))


@buildings_app.command("import")
def buildings_import(
    csv_file: Path = typer.Option(..., "--csv", help="CSV file with columns: number,name"),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Bulk import building mappings from CSV."""
    configure_logging()
    if not csv_file.exists():
        typer.echo(json.dumps({"ok": False, "error": f"missing file: {csv_file}"}, indent=2))
        raise typer.Exit(code=2)
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    imported = 0
    skipped = 0
    with csv_file.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            number = str(
                row.get("number")
                or row.get("building_no")
                or row.get("building")
                or ""
            ).strip()
            name = str(
                row.get("name")
                or row.get("building_name")
                or ""
            ).strip()
            if not number or not name:
                skipped += 1
                continue
            db.upsert_building(
                building_no=number,
                building_name=name,
                metadata_json={"source": "csv", "csv_file": str(csv_file)},
            )
            imported += 1
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "csv_file": str(csv_file),
                "imported": imported,
                "skipped": skipped,
            },
            indent=2,
        )
    )


@buildings_app.command("list")
def buildings_list(
    limit: int = typer.Option(500, "--limit", min=1, max=5000),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """List building mappings."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    items = db.list_buildings(limit=limit)
    payload = []
    for item in items:
        payload.append(
            {
                "building_no": item["building_no"],
                "building_name": item["building_name"],
                "updated_at": item["updated_at"],
            }
        )
    typer.echo(json.dumps({"count": len(payload), "items": payload}, indent=2))


@buildings_app.command("seed-uos")
def buildings_seed_uos(
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing mappings for matching building numbers.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Seed University of Seoul building map into DB."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    existing = {
        str(item["building_no"]): str(item["building_name"])
        for item in db.list_buildings(limit=5000)
    }
    inserted = 0
    updated = 0
    skipped = 0
    for building_no, building_name in UOS_BUILDING_MAP.items():
        has_existing = building_no in existing
        if has_existing and not overwrite:
            skipped += 1
            continue
        db.upsert_building(
            building_no=building_no,
            building_name=building_name,
            metadata_json={
                "source": "seed:uos",
                "overwrite": bool(overwrite),
            },
        )
        if has_existing:
            updated += 1
        else:
            inserted += 1
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "seed": "uos",
                "overwrite": bool(overwrite),
                "inserted": inserted,
                "updated": updated,
                "skipped": skipped,
                "total_seed_rows": len(UOS_BUILDING_MAP),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@reminders_app.command("list")
def reminders_list(
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Filter by status (pending, sent, failed, cancelled).",
    ),
    limit: int = typer.Option(200, "--limit", min=1, max=1000),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """List scheduled Telegram reminders."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    items = db.list_telegram_reminders(status=status, limit=limit)
    typer.echo(json.dumps({"count": len(items), "items": items}, indent=2))


@relay_app.command("serve")
def relay_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8787, "--port", min=1, max=65535, help="Bind port."),
    state_file: Optional[Path] = typer.Option(
        None,
        "--state-file",
        help="Override relay state file for item_key dedupe tracking.",
    ),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Serve signed Telegram briefing relay endpoint."""
    configure_logging()
    settings = load_settings(config_file=config_file)
    relay_secret = str(getattr(settings, "briefing_relay_shared_secret", "") or "").strip()
    relay_state_file = state_file or getattr(
        settings,
        "briefing_relay_state_file",
        Path("data/briefing_relay_state.json"),
    )
    bot_token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    if not relay_secret:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "BRIEFING_RELAY_SHARED_SECRET is missing",
                },
                indent=2,
            )
        )
        raise typer.Exit(code=2)
    if not bot_token:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": "TELEGRAM_BOT_TOKEN is missing",
                },
                indent=2,
            )
        )
        raise typer.Exit(code=2)

    relay_store = BriefingRelayStateStore(Path(relay_state_file))
    allowed_chat_ids = [
        str(chat).strip()
        for chat in list(getattr(settings, "telegram_allowed_chat_ids", []) or [])
        if str(chat).strip()
    ]

    class RelayHandler(BaseHTTPRequestHandler):
        def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in {"/", "/healthz"}:
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            self._write_json(
                200,
                {
                    "ok": True,
                    "service": "sidae-briefing-relay",
                    "listening_on": f"http://{host}:{port}",
                    "state_file": str(Path(relay_state_file)),
                },
            )

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/briefing":
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._write_json(400, {"ok": False, "error": "invalid_content_length"})
                return
            raw = self.rfile.read(content_length) if content_length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._write_json(400, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._write_json(400, {"ok": False, "error": "invalid_payload"})
                return
            result = deliver_signed_briefing_request(
                payload=payload,
                shared_secret=relay_secret,
                bot_token=bot_token,
                state_store=relay_store,
                allowed_chat_ids=allowed_chat_ids,
            )
            status_code = int(result.pop("_http_status", 200))
            self._write_json(status_code, result)

    server = ThreadingHTTPServer((host, port), RelayHandler)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "service": "sidae-briefing-relay",
                "host": host,
                "port": port,
                "state_file": str(Path(relay_state_file)),
                "healthz": f"http://{host}:{port}/healthz",
                "briefing_endpoint": f"http://{host}:{port}/briefing",
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


@portal_app.command("import")
def portal_import(
    ics_url: Optional[str] = typer.Option(None, "--ics-url", help="Remote ICS URL"),
    ics_file: Optional[Path] = typer.Option(None, "--ics-file", help="Local ICS path"),
    csv: Optional[Path] = typer.Option(None, "--csv", help="CSV path (date,title)"),
    config_file: Optional[Path] = typer.Option(None, help="Path to config TOML."),
) -> None:
    """Import portal academic dates from ICS URL/file or CSV and upsert to the local DB."""
    configure_logging()
    selected = [bool(ics_url), bool(ics_file), bool(csv)]
    if sum(selected) != 1:
        typer.echo("Provide exactly one of --ics-url, --ics-file, --csv.")
        raise typer.Exit(code=2)
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    result = import_portal_events(
        settings=settings,
        db=db,
        ics_url=ics_url,
        ics_file=ics_file,
        csv_file=csv,
    )
    typer.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
