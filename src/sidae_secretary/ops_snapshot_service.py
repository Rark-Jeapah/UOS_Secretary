from __future__ import annotations

from datetime import datetime, timezone
import getpass
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from sidae_secretary.config import load_settings, select_config_path
from sidae_secretary.db import Database, now_utc_iso, parse_metadata_json
from sidae_secretary.jobs.pipeline import (
    build_beta_ops_health_report,
    inspect_last_failed_stage,
)


def redact_url_tokens(value: str) -> str:
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


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if any(
                token in key_text
                for token in ("token", "secret", "password", "api_key", "apikey", "wstoken", "authkey")
            ):
                output[str(key)] = "***"
            else:
                output[str(key)] = scrub_secrets(item)
        return output
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, str):
        return redact_url_tokens(value)
    return value


def _status_tone(value: Any) -> str:
    lower = str(value or "").strip().lower()
    if lower in {"ready", "success", "ok", "active"}:
        return "ok"
    if lower in {"error", "failed"}:
        return "err"
    if lower in {"degraded", "warning", "warn", "partial", "skipped", "unknown", "check", "stale"}:
        return "warn"
    return "info"


def _headline_sync_issue_counts(instances: list[dict[str, Any]]) -> tuple[int, int]:
    error_count = 0
    warning_count = 0
    for instance in instances:
        for item in list(instance.get("sync_dashboard", {}).get("sources") or []):
            tone = _status_tone(item.get("status"))
            action_required = int(item.get("action_required") or 0)
            has_issue = tone in {"warn", "err"} or action_required > 0 or bool(
                str(item.get("last_error") or "").strip()
            )
            if not has_issue:
                continue
            if tone == "err":
                error_count += 1
            else:
                warning_count += 1
    return error_count, warning_count


def _headline_health_issue_counts(instances: list[dict[str, Any]]) -> tuple[int, int]:
    error_count = 0
    warning_count = 0
    for instance in instances:
        surfaces = instance.get("health", {}).get("surfaces", {})
        if not isinstance(surfaces, dict):
            continue
        for item in surfaces.values():
            tone = _status_tone(item.get("status") if isinstance(item, dict) else None)
            if tone == "err":
                error_count += 1
            elif tone == "warn":
                warning_count += 1
    return error_count, warning_count


def build_dashboard_headline(snapshot: dict[str, Any]) -> dict[str, Any]:
    instances = list(snapshot.get("instances") or [])
    load_errors = sum(1 for instance in instances if instance.get("load_error"))
    action_required = sum(
        int(instance.get("sync_dashboard", {}).get("action_required_count") or 0)
        for instance in instances
    )
    health_errors, health_warnings = _headline_health_issue_counts(instances)
    sync_errors, sync_warnings = _headline_sync_issue_counts(instances)

    llm = snapshot.get("llm") if isinstance(snapshot.get("llm"), dict) else {}
    llm_tone = _status_tone(llm.get("status"))
    failing_endpoints = [
        item
        for item in list(llm.get("endpoints") or [])
        if isinstance(item, dict) and (not item.get("http_ok") or item.get("error"))
    ]
    llm_errors = len(failing_endpoints) or (1 if llm_tone == "err" else 0)
    llm_warnings = len(list(llm.get("recent_warnings") or [])) + (1 if llm_tone == "warn" else 0)

    log_files = list(snapshot.get("logs", {}).get("files") or [])
    log_error_files = sum(1 for item in log_files if int(item.get("error_count") or 0) > 0)
    log_warning_files = sum(
        1
        for item in log_files
        if int(item.get("error_count") or 0) <= 0 and int(item.get("warning_count") or 0) > 0
    )

    critical_count = load_errors + health_errors + llm_errors + log_error_files
    warning_count = (
        action_required
        + health_warnings
        + sync_errors
        + sync_warnings
        + llm_warnings
        + log_warning_files
    )
    if critical_count > 0:
        tone = "err"
        reason = "실제 장애 신호 있음"
    elif warning_count > 0:
        tone = "warn"
        reason = "수동 확인 항목 있음"
    else:
        tone = "ok"
        reason = "즉시 조치 항목 없음"
    return {
        "tone": tone,
        "reason": reason,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "load_errors": load_errors,
        "action_required": action_required,
        "health_errors": health_errors,
        "health_warnings": health_warnings,
        "sync_errors": sync_errors,
        "sync_warnings": sync_warnings,
        "llm_errors": llm_errors,
        "llm_warnings": llm_warnings,
        "log_error_files": log_error_files,
        "log_warning_files": log_warning_files,
    }


def trim_sync_dashboard(snapshot: dict[str, Any]) -> dict[str, Any]:
    sources = []
    for item in list(snapshot.get("sources") or []):
        sources.append(
            {
                "key": str(item.get("key") or ""),
                "label": str(item.get("label") or ""),
                "status": str(item.get("status") or ""),
                "last_run_at": str(item.get("last_run_at") or "").strip() or None,
                "last_success_at": str(item.get("last_success_at") or "").strip() or None,
                "last_error": str(item.get("last_error") or "").strip() or None,
                "new_items": int(item.get("new_items") or 0),
                "action_required": int(item.get("action_required") or 0),
            }
        )
    return {
        "last_successful_sync_at": str(snapshot.get("last_successful_sync_at") or "").strip() or None,
        "last_error": snapshot.get("last_error") if isinstance(snapshot.get("last_error"), dict) else None,
        "pending_inbox_count": int(snapshot.get("pending_inbox_count") or 0),
        "low_confidence_task_count": int(snapshot.get("low_confidence_task_count") or 0),
        "conflict_warning_count": int(snapshot.get("conflict_warning_count") or 0),
        "sync_error_count": int(snapshot.get("sync_error_count") or 0),
        "action_required_count": int(snapshot.get("action_required_count") or 0),
        "sources": sources,
    }


def trim_health_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    surfaces: dict[str, Any] = {}
    for key, item in dict(snapshot.get("surfaces") or {}).items():
        surfaces[str(key)] = {
            "status": str(item.get("status") or ""),
            "reason": str(item.get("reason") or "").strip() or None,
            "last_run_at": str(item.get("last_run_at") or "").strip() or None,
            "last_success_at": str(item.get("last_success_at") or "").strip() or None,
            "last_error": str(item.get("last_error") or "").strip() or None,
        }
    return {
        "overall_ready": bool(snapshot.get("overall_ready")),
        "ready_count": int(snapshot.get("ready_count") or 0),
        "not_ready_count": int(snapshot.get("not_ready_count") or 0),
        "surfaces": surfaces,
    }


def trim_event(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        "title": str(item.get("title") or ""),
        "start_at": str(item.get("start_at") or "").strip() or None,
        "end_at": str(item.get("end_at") or "").strip() or None,
        "location": str(item.get("location") or "").strip() or None,
        "source": str(item.get("source") or "").strip() or None,
    }


def trim_task(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        "title": str(item.get("title") or ""),
        "due_at": str(item.get("due_at") or "").strip() or None,
        "source": str(item.get("source") or "").strip() or None,
        "status": str(item.get("status") or "").strip() or None,
    }


def trim_material(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        "filename": str(item.get("filename") or ""),
        "updated_at": str(item.get("updated_at") or "").strip() or None,
        "source": str(item.get("source") or "").strip() or None,
    }


def _tail_lines(path: Path, *, limit: int) -> list[str]:
    if limit <= 0 or not path.exists() or not path.is_file():
        return []
    data = b""
    try:
        with path.open("rb") as fp:
            fp.seek(0, os.SEEK_END)
            remaining = fp.tell()
            while remaining > 0 and data.count(b"\n") <= limit:
                read_size = min(4096, remaining)
                remaining -= read_size
                fp.seek(remaining)
                data = fp.read(read_size) + data
    except Exception:
        return []
    lines = data.splitlines()[-limit:]
    return [line.decode("utf-8", errors="replace") for line in lines]


def _default_log_files() -> list[Path]:
    root = Path("/tmp")
    files = list(root.glob("com.sidae.secretary*.log")) + list(root.glob("sidae_secretary*.log"))
    existing = [path for path in files if path.exists() and path.is_file()]
    existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return existing


def collect_log_snapshot(*, file_limit: int = 8, tail_lines: int = 30) -> dict[str, Any]:
    files_payload: list[dict[str, Any]] = []
    for path in _default_log_files()[: max(int(file_limit), 1)]:
        tail = _tail_lines(path, limit=max(int(tail_lines), 1))
        warning_count = sum(1 for line in tail if "warning" in line.lower())
        error_count = sum(1 for line in tail if "error" in line.lower())
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except Exception:
            mtime = None
        files_payload.append(
            {
                "path": str(path),
                "mtime": mtime,
                "warning_count": warning_count,
                "error_count": error_count,
                "tail": tail,
            }
        )
    llm_lines: list[str] = []
    for item in files_payload:
        for line in list(item.get("tail") or []):
            lowered = line.lower()
            if "llm" in lowered or "ollama" in lowered or "gemma" in lowered:
                llm_lines.append(f"{Path(str(item['path'])).name}: {line}")
    return {
        "files": files_payload,
        "llm_highlights": llm_lines[-8:],
    }


def discover_instance_roots(config_file: Path | None = None) -> list[Path]:
    selected = select_config_path(config_file=config_file)
    base_root = selected.parent.resolve()
    candidates = [base_root]
    name = base_root.name
    if name.endswith("_beta"):
        sibling_name = name[: -len("_beta")]
        if sibling_name:
            candidates.append(base_root.with_name(sibling_name))
    else:
        candidates.append(base_root.with_name(f"{name}_beta"))
    seen: set[str] = set()
    roots: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() or candidate == base_root:
            roots.append(candidate.resolve())
    return roots


def instance_label(*, root: Path, instance_name: str) -> str:
    normalized = str(instance_name or "").strip().lower()
    if normalized:
        return normalized
    if root.name.endswith("_beta"):
        return "beta"
    return "prod"


def trim_failed_stage(match: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(match, dict):
        return None
    return {
        "component": str(match.get("component") or ""),
        "stage": str(match.get("stage") or ""),
        "status": str(match.get("status") or ""),
        "message": str(match.get("message") or "").strip() or None,
        "last_run_at": str(match.get("last_run_at") or "").strip() or None,
    }


def _artifact_field(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def build_material_brief_summary(
    artifacts: list[Any] | None,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    total = 0
    ready = 0
    missing = 0
    llm = 0
    heuristic = 0
    missing_local_file = 0
    missing_extract = 0
    extract_failed = 0
    samples: list[dict[str, Any]] = []
    for item in list(artifacts or [])[: max(int(limit), 1)]:
        filename = str(_artifact_field(item, "filename") or "").strip() or "material"
        updated_at = str(_artifact_field(item, "updated_at") or "").strip() or None
        local_path = str(_artifact_field(item, "icloud_path") or "").strip() or None
        file_exists = bool(local_path and Path(local_path).exists())
        metadata = parse_metadata_json(_artifact_field(item, "metadata_json"))
        brief = metadata.get("brief") if isinstance(metadata.get("brief"), dict) else None
        text_extract = (
            metadata.get("text_extract") if isinstance(metadata.get("text_extract"), dict) else {}
        )
        total += 1
        if brief is not None:
            ready += 1
            if str(brief.get("mode") or "").strip().lower() == "llm":
                llm += 1
            else:
                heuristic += 1
            continue
        missing += 1
        if not file_exists and not str(text_extract.get("excerpt") or "").strip():
            missing_local_file += 1
            reason = "로컬 파일 없음"
        elif text_extract and text_extract.get("ok") is False:
            extract_failed += 1
            reason = str(text_extract.get("error") or "텍스트 추출 실패").strip() or "텍스트 추출 실패"
        elif not str(text_extract.get("excerpt") or "").strip():
            missing_extract += 1
            reason = "추출 텍스트 없음"
        else:
            reason = "요약 미생성"
        if len(samples) < 5:
            samples.append(
                {
                    "filename": filename,
                    "updated_at": updated_at,
                    "reason": reason,
                    "local_file": file_exists,
                }
            )
    return {
        "scanned_count": total,
        "ready_count": ready,
        "missing_count": missing,
        "llm_count": llm,
        "heuristic_count": heuristic,
        "missing_local_file_count": missing_local_file,
        "missing_extract_count": missing_extract,
        "extract_failed_count": extract_failed,
        "samples": samples,
    }


def build_user_card(
    settings: Any,
    db: Database,
    *,
    user: dict[str, Any],
    build_beta_ops_health_report_fn: Callable[..., dict[str, Any]] = build_beta_ops_health_report,
    inspect_last_failed_stage_fn: Callable[..., dict[str, Any] | None] = inspect_last_failed_stage,
) -> dict[str, Any]:
    user_id = int(user.get("id") or user.get("user_id") or 0)
    dashboard = db.dashboard_snapshot(user_id=user_id)
    health = build_beta_ops_health_report_fn(settings, db, user_id=user_id)
    sync_dashboard = trim_sync_dashboard(db.sync_dashboard_snapshot(user_id=user_id))
    preferences = db.get_user_preferences(user_id=user_id) or {}
    failed_stage = inspect_last_failed_stage_fn(settings, db, user_id=user_id)
    connections: list[dict[str, Any]] = []
    for item in db.list_moodle_connections(user_id=user_id, status="active", limit=20):
        connections.append(
            {
                "kind": "moodle_connection",
                "school_slug": str(item.get("school_slug") or ""),
                "display_name": str(item.get("display_name") or ""),
                "last_verified_at": str(item.get("last_verified_at") or "").strip() or None,
                "updated_at": str(item.get("updated_at") or "").strip() or None,
            }
        )
    for item in db.list_lms_browser_sessions(user_id=user_id, status="active", limit=20):
        connections.append(
            {
                "kind": "browser_session",
                "school_slug": str(item.get("school_slug") or ""),
                "display_name": str(item.get("display_name") or ""),
                "last_verified_at": str(item.get("last_verified_at") or "").strip() or None,
                "updated_at": str(item.get("updated_at") or "").strip() or None,
            }
        )
    connections.sort(key=lambda item: (item.get("display_name") or "", item.get("kind") or ""))
    return {
        "user_id": user_id,
        "chat_id": str(user.get("telegram_chat_id") or user.get("chat_id") or "").strip() or None,
        "status": str(user.get("status") or ""),
        "timezone": str(user.get("timezone") or "").strip() or None,
        "counts": db.counts(user_id=user_id),
        "preferences": {
            "telegram_chat_allowed": preferences.get("telegram_chat_allowed"),
            "material_brief_push_enabled": preferences.get("material_brief_push_enabled"),
            "scheduled_briefings_enabled": preferences.get("scheduled_briefings_enabled"),
            "daily_digest_enabled": preferences.get("daily_digest_enabled"),
        },
        "sync_dashboard": sync_dashboard,
        "health": trim_health_snapshot(health),
        "health_summary": {
            "overall_ready": bool(health.get("overall_ready")),
            "ready_count": int(health.get("ready_count") or 0),
            "not_ready_count": int(health.get("not_ready_count") or 0),
        },
        "connections": connections,
        "next_event": trim_event((dashboard.get("upcoming_events") or [None])[0]),
        "next_task": trim_task((dashboard.get("due_tasks") or [None])[0]),
        "recent_material": trim_material((dashboard.get("recent_materials") or [None])[0]),
        "material_brief_summary": build_material_brief_summary(
            list(dashboard.get("recent_materials") or [])
        ),
        "last_failed_stage": trim_failed_stage(
            failed_stage.get("match") if isinstance(failed_stage, dict) else None
        ),
    }


def collect_service_processes(*, instance_configs: dict[str, Path]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid,pcpu,pmem,etime,command"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return {
            "counts": {
                "total": 0,
                "sidae": 0,
                "ollama": 0,
            },
            "error": str(exc),
            "processes": [],
        }
    processes: list[dict[str, Any]] = []
    for raw_line in str(result.stdout or "").splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^(\d+)\s+([\d\.]+)\s+([\d\.]+)\s+(\S+)\s+(.+)$", line)
        if not match:
            continue
        command = match.group(5).strip()
        lowered = command.lower()
        if "ollama" not in lowered and "sidae_secretary.cli" not in lowered:
            continue
        kind = "sidae"
        kind_label = "Sidae"
        if "ollama runner" in lowered:
            kind = "ollama_runner"
            kind_label = "Ollama 러너"
        elif "ollama serve" in lowered:
            kind = "ollama"
            kind_label = "Ollama 서버"
        elif "telegram-listener" in lowered:
            kind = "telegram_listener"
            kind_label = "텔레그램 리스너"
        elif "uclass-poller" in lowered:
            kind = "uclass_poller"
            kind_label = "UClass 폴러"
        elif "onboarding" in lowered and " serve" in lowered:
            kind = "onboarding"
            kind_label = "온보딩"
        elif re.search(r"\bsync\b", lowered):
            kind = "sync"
            kind_label = "동기화"
        elif re.search(r"\bpublish\b", lowered):
            kind = "publish"
            kind_label = "발행"
        elif re.search(r"\bops\b", lowered):
            kind = "ops_dashboard"
            kind_label = "운영 대시보드"
        instance_label_value = None
        for label, config_path in instance_configs.items():
            if str(config_path) in command or str(config_path.parent) in command:
                instance_label_value = label
                break
        processes.append(
            {
                "pid": int(match.group(1)),
                "cpu_percent": float(match.group(2)),
                "memory_percent": float(match.group(3)),
                "elapsed": match.group(4),
                "command": command,
                "kind": kind,
                "kind_label": kind_label,
                "instance_label": instance_label_value,
            }
        )
    processes.sort(
        key=lambda item: (
            0 if str(item.get("kind") or "").startswith("ollama") else 1,
            str(item.get("instance_label") or ""),
            str(item.get("kind") or ""),
            int(item.get("pid") or 0),
        )
    )
    return {
        "counts": {
            "total": len(processes),
            "sidae": sum(1 for item in processes if "ollama" not in str(item.get("kind") or "")),
            "ollama": sum(1 for item in processes if "ollama" in str(item.get("kind") or "")),
        },
        "processes": processes,
    }


def ollama_base_url(endpoint: str) -> str:
    text = str(endpoint or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path = parsed.path or ""
    for suffix in ("/api/chat", "/api/generate", "/v1/chat/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] or ""
            break
    normalized = parsed._replace(path=path.rstrip("/"), params="", query="", fragment="")
    return normalized.geturl().rstrip("/")


def probe_ollama_endpoint(base_url: str, *, timeout_sec: float) -> dict[str, Any]:
    ps_url = f"{base_url}/api/ps"
    tags_url = f"{base_url}/api/tags"
    started = time.perf_counter()
    response = requests.get(ps_url, timeout=timeout_sec)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.raise_for_status()
    ps_payload = response.json() if hasattr(response, "json") else {}
    started_tags = time.perf_counter()
    tags_response = requests.get(tags_url, timeout=timeout_sec)
    tags_elapsed_ms = int((time.perf_counter() - started_tags) * 1000)
    tags_response.raise_for_status()
    tags_payload = tags_response.json() if hasattr(tags_response, "json") else {}
    loaded_models = []
    for item in list(ps_payload.get("models") or []):
        if not isinstance(item, dict):
            continue
        loaded_models.append(
            {
                "name": str(item.get("name") or item.get("model") or "").strip(),
                "processor": str(item.get("processor") or "").strip() or None,
                "context": item.get("context"),
                "until": str(item.get("until") or item.get("expires_at") or "").strip() or None,
                "size": item.get("size"),
            }
        )
    available_models = []
    for item in list(tags_payload.get("models") or []):
        if not isinstance(item, dict):
            continue
        available_models.append(str(item.get("name") or "").strip())
    return {
        "base_url": base_url,
        "http_ok": True,
        "response_ms": elapsed_ms,
        "tags_response_ms": tags_elapsed_ms,
        "loaded_models": loaded_models,
        "loaded_model_names": [item["name"] for item in loaded_models if item.get("name")],
        "available_models": [name for name in available_models if name],
        "error": None,
    }


def build_llm_snapshot(
    *,
    instances: list[dict[str, Any]],
    log_snapshot: dict[str, Any],
    timeout_sec: float = 2.5,
    probe_ollama_endpoint_fn: Callable[..., dict[str, Any]] = probe_ollama_endpoint,
) -> dict[str, Any]:
    endpoints: list[str] = []
    configured_models: list[str] = []
    for instance in instances:
        llm_config = instance.get("llm") if isinstance(instance, dict) else None
        if not isinstance(llm_config, dict):
            continue
        if not bool(llm_config.get("enabled")):
            continue
        if str(llm_config.get("provider") or "").strip().lower() != "local":
            continue
        endpoint = ollama_base_url(str(llm_config.get("endpoint") or ""))
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
        model = str(llm_config.get("model") or "").strip()
        if model and model not in configured_models:
            configured_models.append(model)
    endpoint_payloads: list[dict[str, Any]] = []
    loaded_models: list[dict[str, Any]] = []
    errors: list[str] = []
    for endpoint in endpoints:
        try:
            payload = probe_ollama_endpoint_fn(endpoint, timeout_sec=max(float(timeout_sec), 0.2))
        except Exception as exc:
            payload = {
                "base_url": endpoint,
                "http_ok": False,
                "response_ms": None,
                "tags_response_ms": None,
                "loaded_models": [],
                "loaded_model_names": [],
                "available_models": [],
                "error": str(exc),
            }
            errors.append(str(exc))
        endpoint_payloads.append(payload)
        loaded_models.extend(list(payload.get("loaded_models") or []))
    if not configured_models:
        return {
            "status": "disabled",
            "error": None,
            "configured_models": [],
            "loaded_models": [],
            "endpoints": [],
            "recent_warnings": [],
        }
    warning_lines = list(log_snapshot.get("llm_highlights") or [])
    if errors:
        status = "error"
        error = errors[0]
    elif warning_lines:
        status = "degraded"
        error = None
    else:
        status = "ready"
        error = None
    return {
        "status": status,
        "error": error,
        "configured_models": configured_models,
        "loaded_models": loaded_models,
        "endpoints": endpoint_payloads,
        "recent_warnings": warning_lines[-8:],
    }


def load_instance_snapshot(
    root: Path,
    *,
    max_users: int,
    process_payload: dict[str, Any],
    build_beta_ops_health_report_fn: Callable[..., dict[str, Any]] = build_beta_ops_health_report,
    inspect_last_failed_stage_fn: Callable[..., dict[str, Any] | None] = inspect_last_failed_stage,
) -> dict[str, Any]:
    config_file = (root / "config.toml").resolve()
    payload = {
        "label": root.name,
        "instance_name": "",
        "app_root": str(root),
        "config_file": str(config_file),
        "database_path": None,
        "load_error": None,
        "llm": {},
        "counts": {},
        "sync_dashboard": {},
        "health": {},
        "health_summary": {
            "overall_ready": False,
            "ready_count": 0,
            "not_ready_count": 0,
        },
        "processes": [],
        "users": [],
    }
    try:
        settings = load_settings(config_file=config_file)
        db = Database(settings.database_path)
        db.init()
        label = instance_label(root=root, instance_name=str(getattr(settings, "instance_name", "") or ""))
        payload["label"] = label
        payload["instance_name"] = str(getattr(settings, "instance_name", "") or "")
        payload["database_path"] = str(settings.database_path)
        payload["llm"] = {
            "enabled": bool(getattr(settings, "llm_enabled", False)),
            "provider": str(getattr(settings, "llm_provider", "") or "").strip(),
            "model": str(getattr(settings, "llm_model", "") or "").strip(),
            "endpoint": str(getattr(settings, "llm_local_endpoint", "") or "").strip(),
        }
        payload["counts"] = db.counts()
        sync_dashboard = db.sync_dashboard_snapshot()
        health = build_beta_ops_health_report_fn(settings, db)
        payload["sync_dashboard"] = trim_sync_dashboard(sync_dashboard)
        payload["health"] = trim_health_snapshot(health)
        payload["health_summary"] = {
            "overall_ready": bool(health.get("overall_ready")),
            "ready_count": int(health.get("ready_count") or 0),
            "not_ready_count": int(health.get("not_ready_count") or 0),
        }
        users = [
            build_user_card(
                settings,
                db,
                user=item,
                build_beta_ops_health_report_fn=build_beta_ops_health_report_fn,
                inspect_last_failed_stage_fn=inspect_last_failed_stage_fn,
            )
            for item in db.list_users(status="active", limit=max(int(max_users), 1))
        ]
        users.sort(
            key=lambda item: (
                -int(item.get("sync_dashboard", {}).get("action_required_count") or 0),
                str(item.get("chat_id") or ""),
            )
        )
        payload["users"] = users
        payload["processes"] = [
            item
            for item in list(process_payload.get("processes") or [])
            if str(item.get("instance_label") or "") == label
        ]
    except Exception as exc:
        payload["load_error"] = str(exc)
    return payload


def build_ops_dashboard_snapshot(
    *,
    config_file: Path | None = None,
    instance_roots: list[Path] | None = None,
    max_users: int = 40,
    log_file_limit: int = 8,
    log_tail_lines: int = 30,
    refresh_interval_sec: int = 15,
    build_beta_ops_health_report_fn: Callable[..., dict[str, Any]] = build_beta_ops_health_report,
    inspect_last_failed_stage_fn: Callable[..., dict[str, Any] | None] = inspect_last_failed_stage,
    collect_log_snapshot_fn: Callable[..., dict[str, Any]] = collect_log_snapshot,
    collect_service_processes_fn: Callable[..., dict[str, Any]] = collect_service_processes,
    probe_ollama_endpoint_fn: Callable[..., dict[str, Any]] = probe_ollama_endpoint,
) -> dict[str, Any]:
    roots = [path.resolve() for path in list(instance_roots or []) if isinstance(path, Path)]
    if not roots:
        roots = discover_instance_roots(config_file=config_file)
    instance_config_map: dict[str, Path] = {}
    for root in roots:
        config_path = (root / "config.toml").resolve()
        label = instance_label(root=root, instance_name="")
        instance_config_map[label] = config_path
    log_snapshot = collect_log_snapshot_fn(
        file_limit=max(int(log_file_limit), 1),
        tail_lines=max(int(log_tail_lines), 1),
    )
    process_payload = collect_service_processes_fn(instance_configs=instance_config_map)
    instances = [
        load_instance_snapshot(
            root,
            max_users=max_users,
            process_payload=process_payload,
            build_beta_ops_health_report_fn=build_beta_ops_health_report_fn,
            inspect_last_failed_stage_fn=inspect_last_failed_stage_fn,
        )
        for root in roots
    ]
    llm_snapshot = build_llm_snapshot(
        instances=instances,
        log_snapshot=log_snapshot,
        probe_ollama_endpoint_fn=probe_ollama_endpoint_fn,
    )
    snapshot = {
        "generated_at": now_utc_iso(),
        "host": socket.gethostname(),
        "user": getpass.getuser(),
        "cwd": str(Path.cwd()),
        "python": sys.version.split(" ", 1)[0],
        "refresh_interval_sec": max(int(refresh_interval_sec), 1),
        "instances": instances,
        "services": process_payload,
        "logs": {
            "files": list(log_snapshot.get("files") or []),
        },
        "llm": llm_snapshot,
    }
    snapshot["headline"] = build_dashboard_headline(snapshot)
    return scrub_secrets(snapshot)
