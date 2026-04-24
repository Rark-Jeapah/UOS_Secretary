from __future__ import annotations

from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from sidae_secretary.jobs.pipeline import (
    build_beta_ops_health_report,
    inspect_last_failed_stage,
    refresh_beta_user,
    repair_missing_material_briefs_for_user,
)
from sidae_secretary.ops_action_service import (
    OpsDashboardActionQueue,
    build_default_action_roots,
    execute_ops_dashboard_action as _execute_ops_dashboard_action_impl,
)
from sidae_secretary.ops_snapshot_service import (
    build_ops_dashboard_snapshot as _build_ops_dashboard_snapshot_impl,
    collect_log_snapshot as _collect_log_snapshot_impl,
    collect_service_processes as _collect_service_processes_impl,
    probe_ollama_endpoint as _probe_ollama_endpoint_impl,
    scrub_secrets as _scrub_secrets_impl,
)


_collect_service_processes = _collect_service_processes_impl
_collect_log_snapshot = _collect_log_snapshot_impl
_probe_ollama_endpoint = _probe_ollama_endpoint_impl
_scrub_secrets = _scrub_secrets_impl


@lru_cache(maxsize=1)
def _ops_dashboard_html_template() -> str:
    asset_path = Path(__file__).with_name("ops_dashboard_assets") / "dashboard.html"
    return asset_path.read_text(encoding="utf-8")


def build_ops_dashboard_snapshot(
    *,
    config_file: Path | None = None,
    instance_roots: list[Path] | None = None,
    max_users: int = 40,
    log_file_limit: int = 8,
    log_tail_lines: int = 30,
    refresh_interval_sec: int = 15,
) -> dict[str, Any]:
    return _build_ops_dashboard_snapshot_impl(
        config_file=config_file,
        instance_roots=instance_roots,
        max_users=max_users,
        log_file_limit=log_file_limit,
        log_tail_lines=log_tail_lines,
        refresh_interval_sec=refresh_interval_sec,
        build_beta_ops_health_report_fn=build_beta_ops_health_report,
        inspect_last_failed_stage_fn=inspect_last_failed_stage,
        collect_log_snapshot_fn=_collect_log_snapshot,
        collect_service_processes_fn=_collect_service_processes,
        probe_ollama_endpoint_fn=_probe_ollama_endpoint,
    )


def _execute_ops_dashboard_action(
    *,
    action: str,
    roots: list[Path],
    instance_label: str | None = None,
    chat_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    return _execute_ops_dashboard_action_impl(
        action=action,
        roots=roots,
        instance_label_value=instance_label,
        chat_id=chat_id,
        user_id=user_id,
        build_beta_ops_health_report_fn=build_beta_ops_health_report,
        inspect_last_failed_stage_fn=inspect_last_failed_stage,
        refresh_beta_user_fn=refresh_beta_user,
        repair_missing_material_briefs_for_user_fn=repair_missing_material_briefs_for_user,
    )


def render_ops_dashboard_html(*, refresh_interval_sec: int = 15) -> str:
    refresh_ms = max(int(refresh_interval_sec), 1) * 1000
    return _ops_dashboard_html_template().replace("__REFRESH_MS__", str(refresh_ms))


def build_ops_dashboard_http_server(
    *,
    host: str,
    port: int,
    config_file: Path | None = None,
    instance_roots: list[Path] | None = None,
    max_users: int = 40,
    log_file_limit: int = 8,
    log_tail_lines: int = 30,
    refresh_interval_sec: int = 15,
    snapshot_factory: Callable[[], dict[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    resolved_roots = build_default_action_roots(
        config_file=config_file,
        instance_roots=instance_roots,
    )
    action_queue = OpsDashboardActionQueue(
        roots=resolved_roots,
        execute_action=_execute_ops_dashboard_action,
        scrub_value=_scrub_secrets,
    )

    def _snapshot() -> dict[str, Any]:
        if callable(snapshot_factory):
            return snapshot_factory()
        return build_ops_dashboard_snapshot(
            config_file=config_file,
            instance_roots=resolved_roots or None,
            max_users=max_users,
            log_file_limit=log_file_limit,
            log_tail_lines=log_tail_lines,
            refresh_interval_sec=refresh_interval_sec,
        )

    html = render_ops_dashboard_html(refresh_interval_sec=refresh_interval_sec).encode("utf-8")

    class OpsHandler(BaseHTTPRequestHandler):
        def _write_bytes(self, status_code: int, body: bytes, content_type: str) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            self._write_bytes(
                status_code,
                json.dumps(_scrub_secrets(payload), ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("json object required")
            return payload

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._write_bytes(200, html, "text/html; charset=utf-8")
                return
            if parsed.path == "/healthz":
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "service": "sidae-ops-dashboard",
                        "host": host,
                        "port": port,
                        "instance_roots": [str(path) for path in resolved_roots],
                    },
                )
                return
            if parsed.path == "/api/snapshot":
                self._write_json(200, _snapshot())
                return
            if parsed.path == "/api/actions":
                self._write_json(200, action_queue.list_action_records())
                return
            if parsed.path.startswith("/api/actions/"):
                action_id = parsed.path.rsplit("/", 1)[-1].strip()
                record = action_queue.get_action_record(action_id)
                if record is None:
                    self._write_json(404, {"ok": False, "error": "not_found"})
                    return
                self._write_json(200, {"ok": True, "action": record})
                return
            self._write_json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/actions/run":
                self._write_json(404, {"ok": False, "error": "not_found"})
                return
            try:
                payload = self._read_json_body()
                request = action_queue.normalize_action_request(payload)
            except ValueError as exc:
                self._write_json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._write_json(400, {"ok": False, "error": f"invalid json: {exc}"})
                return
            record, duplicate = action_queue.launch_action(request)
            self._write_json(
                202,
                {
                    "ok": True,
                    "duplicate": duplicate,
                    "action": record,
                },
            )

    return ThreadingHTTPServer((host, port), OpsHandler)
