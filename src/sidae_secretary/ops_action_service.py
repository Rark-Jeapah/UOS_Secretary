from __future__ import annotations

from collections import deque
from pathlib import Path
import threading
from typing import Any, Callable
from uuid import uuid4

from sidae_secretary.config import load_settings
from sidae_secretary.db import Database, now_utc_iso
from sidae_secretary.jobs.pipeline import (
    build_beta_ops_health_report,
    inspect_last_failed_stage,
    refresh_beta_user,
    repair_missing_material_briefs_for_user,
)
from sidae_secretary.ops_snapshot_service import (
    build_user_card,
    discover_instance_roots,
    instance_label,
    scrub_secrets,
    trim_sync_dashboard,
)


def _load_ops_instance_context(root: Path) -> dict[str, Any]:
    config_file = (root / "config.toml").resolve()
    settings = load_settings(config_file=config_file)
    db = Database(settings.database_path)
    db.init()
    label = instance_label(root=root, instance_name=str(getattr(settings, "instance_name", "") or ""))
    return {
        "root": root.resolve(),
        "config_file": config_file,
        "settings": settings,
        "db": db,
        "label": label,
    }


def _find_ops_instance_context(
    roots: list[Path],
    *,
    instance_label_value: str | None = None,
) -> dict[str, Any]:
    requested = str(instance_label_value or "").strip().lower()
    contexts: list[dict[str, Any]] = []
    for root in roots:
        try:
            context = _load_ops_instance_context(root)
        except Exception:
            continue
        contexts.append(context)
        if not requested or str(context.get("label") or "").strip().lower() == requested:
            if requested:
                return context
    if requested:
        raise ValueError(f"unknown instance label: {requested}")
    if contexts:
        return contexts[0]
    raise ValueError("no instance context available")


def _resolve_ops_user(
    db: Database,
    *,
    chat_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    owner_id = int(user_id or 0)
    if owner_id > 0:
        user = db.get_user(owner_id)
        if user is not None:
            return user
    chat = str(chat_id or "").strip()
    if chat:
        return db.get_user_by_chat_id(chat)
    return None


def _build_user_audit_findings(user_card: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    sync_dashboard = user_card.get("sync_dashboard") if isinstance(user_card, dict) else {}
    health_summary = user_card.get("health_summary") if isinstance(user_card, dict) else {}
    brief_summary = user_card.get("material_brief_summary") if isinstance(user_card, dict) else {}
    action_required = int(sync_dashboard.get("action_required_count") or 0)
    if action_required > 0:
        findings.append(f"직접 확인 {action_required}건")
    if not bool(health_summary.get("overall_ready")):
        findings.append(f"health surface 점검 {int(health_summary.get('not_ready_count') or 0)}개")
    failed_stage = user_card.get("last_failed_stage") if isinstance(user_card, dict) else None
    if isinstance(failed_stage, dict) and str(failed_stage.get("stage") or "").strip():
        findings.append(f"실패 단계 {str(failed_stage.get('stage') or '').strip()}")
    missing_briefs = int(brief_summary.get("missing_count") or 0)
    if missing_briefs > 0:
        findings.append(f"강의자료 요약 누락 {missing_briefs}건")
    return findings


def _build_ops_user_audit(
    *,
    settings: Any,
    db: Database,
    instance_label_value: str,
    chat_id: str | None = None,
    user_id: int | None = None,
    build_beta_ops_health_report_fn: Callable[..., dict[str, Any]] = build_beta_ops_health_report,
    inspect_last_failed_stage_fn: Callable[..., dict[str, Any] | None] = inspect_last_failed_stage,
) -> dict[str, Any]:
    user = _resolve_ops_user(db, chat_id=chat_id, user_id=user_id)
    if user is None:
        return {
            "ok": False,
            "error": "user not found",
            "instance_label": instance_label_value,
            "scope": {
                "chat_id": str(chat_id or "").strip() or None,
                "user_id": int(user_id or 0) or None,
            },
        }
    card = build_user_card(
        settings,
        db,
        user=user,
        build_beta_ops_health_report_fn=build_beta_ops_health_report_fn,
        inspect_last_failed_stage_fn=inspect_last_failed_stage_fn,
    )
    findings = _build_user_audit_findings(card)
    brief_summary = card.get("material_brief_summary") if isinstance(card, dict) else {}
    return {
        "ok": True,
        "ready": not findings,
        "instance_label": instance_label_value,
        "scope": {
            "chat_id": card.get("chat_id"),
            "user_id": int(card.get("user_id") or 0) or None,
        },
        "findings": findings,
        "sync_dashboard": card.get("sync_dashboard"),
        "health_summary": card.get("health_summary"),
        "material_brief_summary": brief_summary,
        "last_failed_stage": card.get("last_failed_stage"),
        "next_event": card.get("next_event"),
        "next_task": card.get("next_task"),
        "recent_material": card.get("recent_material"),
        "recommendation": (
            "누락 요약 채우기 실행"
            if int(brief_summary.get("missing_count") or 0) > 0
            else "사용자 새로고침 실행"
            if findings
            else "즉시 조치 없음"
        ),
    }


def _build_ops_global_audit(
    *,
    roots: list[Path],
    build_beta_ops_health_report_fn: Callable[..., dict[str, Any]] = build_beta_ops_health_report,
    inspect_last_failed_stage_fn: Callable[..., dict[str, Any] | None] = inspect_last_failed_stage,
) -> dict[str, Any]:
    environments: list[dict[str, Any]] = []
    totals = {
        "environments": 0,
        "active_users": 0,
        "users_with_findings": 0,
        "users_missing_briefs": 0,
        "missing_briefs": 0,
        "load_errors": 0,
    }
    for root in roots:
        try:
            context = _load_ops_instance_context(root)
        except Exception as exc:
            totals["environments"] += 1
            totals["load_errors"] += 1
            environments.append(
                {
                    "label": instance_label(root=root, instance_name=""),
                    "load_error": str(exc),
                    "active_users": 0,
                    "latest_sync": None,
                    "users_with_findings": 0,
                    "users_missing_briefs": 0,
                    "missing_briefs": 0,
                    "top_users": [],
                }
            )
            continue
        settings = context["settings"]
        db = context["db"]
        users = [
            build_user_card(
                settings,
                db,
                user=item,
                build_beta_ops_health_report_fn=build_beta_ops_health_report_fn,
                inspect_last_failed_stage_fn=inspect_last_failed_stage_fn,
            )
            for item in db.list_users(status="active", limit=2000)
        ]
        audits: list[dict[str, Any]] = []
        missing_briefs = 0
        users_missing_briefs = 0
        for card in users:
            findings = _build_user_audit_findings(card)
            brief_summary = card.get("material_brief_summary") if isinstance(card, dict) else {}
            missing_count = int(brief_summary.get("missing_count") or 0)
            if missing_count > 0:
                missing_briefs += missing_count
                users_missing_briefs += 1
            if findings:
                audits.append(
                    {
                        "chat_id": card.get("chat_id"),
                        "user_id": int(card.get("user_id") or 0) or None,
                        "findings": findings,
                        "missing_briefs": missing_count,
                        "last_failed_stage": card.get("last_failed_stage"),
                    }
                )
        audits.sort(
            key=lambda item: (
                -len(list(item.get("findings") or [])),
                -int(item.get("missing_briefs") or 0),
                str(item.get("chat_id") or ""),
            )
        )
        sync_dashboard = trim_sync_dashboard(db.sync_dashboard_snapshot())
        environment = {
            "label": str(context.get("label") or ""),
            "load_error": None,
            "active_users": len(users),
            "latest_sync": sync_dashboard.get("last_successful_sync_at"),
            "users_with_findings": len(audits),
            "users_missing_briefs": users_missing_briefs,
            "missing_briefs": missing_briefs,
            "top_users": audits[:8],
        }
        environments.append(environment)
        totals["environments"] += 1
        totals["active_users"] += len(users)
        totals["users_with_findings"] += len(audits)
        totals["users_missing_briefs"] += users_missing_briefs
        totals["missing_briefs"] += missing_briefs
    environments.sort(
        key=lambda item: (
            0 if str(item.get("label") or "") == "prod" else 1,
            str(item.get("label") or ""),
        )
    )
    return {
        "ok": True,
        "ready": totals["users_with_findings"] == 0 and totals["load_errors"] == 0,
        "generated_at": now_utc_iso(),
        "totals": totals,
        "environments": environments,
    }


def execute_ops_dashboard_action(
    *,
    action: str,
    roots: list[Path],
    instance_label_value: str | None = None,
    chat_id: str | None = None,
    user_id: int | None = None,
    build_beta_ops_health_report_fn: Callable[..., dict[str, Any]] = build_beta_ops_health_report,
    inspect_last_failed_stage_fn: Callable[..., dict[str, Any] | None] = inspect_last_failed_stage,
    refresh_beta_user_fn: Callable[..., dict[str, Any]] = refresh_beta_user,
    repair_missing_material_briefs_for_user_fn: Callable[..., dict[str, Any]] = repair_missing_material_briefs_for_user,
) -> dict[str, Any]:
    name = str(action or "").strip().lower()
    if name == "global_audit":
        return _build_ops_global_audit(
            roots=roots,
            build_beta_ops_health_report_fn=build_beta_ops_health_report_fn,
            inspect_last_failed_stage_fn=inspect_last_failed_stage_fn,
        )
    context = _find_ops_instance_context(roots, instance_label_value=instance_label_value)
    settings = context["settings"]
    db = context["db"]
    label = str(context.get("label") or instance_label_value or "")
    if name == "user_audit":
        return _build_ops_user_audit(
            settings=settings,
            db=db,
            instance_label_value=label,
            chat_id=chat_id,
            user_id=user_id,
            build_beta_ops_health_report_fn=build_beta_ops_health_report_fn,
            inspect_last_failed_stage_fn=inspect_last_failed_stage_fn,
        )
    if name == "user_refresh":
        return refresh_beta_user_fn(
            settings,
            db,
            chat_id=chat_id,
            user_id=user_id,
        ) | {"instance_label": label}
    if name == "repair_missing_material_briefs":
        return repair_missing_material_briefs_for_user_fn(
            settings,
            db,
            chat_id=chat_id,
            user_id=user_id,
        ) | {"instance_label": label}
    raise ValueError(f"unsupported action: {name}")


class OpsDashboardActionQueue:
    def __init__(
        self,
        *,
        roots: list[Path],
        execute_action: Callable[..., dict[str, Any]],
        scrub_value: Callable[[Any], Any] = scrub_secrets,
        max_records: int = 32,
    ) -> None:
        self._roots = [path.resolve() for path in list(roots) if isinstance(path, Path)]
        self._execute_action = execute_action
        self._scrub_value = scrub_value
        self._lock = threading.Lock()
        self._records: deque[dict[str, Any]] = deque(maxlen=max(int(max_records), 1))
        self._index: dict[str, dict[str, Any]] = {}
        self._running: dict[str, str] = {}

    def normalize_action_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        user_id = payload.get("user_id")
        try:
            normalized_user_id = int(user_id) if user_id is not None and str(user_id).strip() else None
        except Exception:
            normalized_user_id = None
        request = {
            "action": str(payload.get("action") or "").strip().lower(),
            "instance_label": str(payload.get("instance_label") or "").strip().lower() or None,
            "chat_id": str(payload.get("chat_id") or "").strip() or None,
            "user_id": normalized_user_id,
        }
        if request["action"] not in {
            "global_audit",
            "user_audit",
            "user_refresh",
            "repair_missing_material_briefs",
        }:
            raise ValueError("unsupported action")
        if request["action"] != "global_audit":
            if not request["instance_label"]:
                raise ValueError("instance_label is required")
            if not request["chat_id"] and not request["user_id"]:
                raise ValueError("chat_id or user_id is required")
        return request

    def _action_scope_text(self, request: dict[str, Any]) -> str:
        parts = [str(request.get("action") or "action")]
        if request.get("instance_label"):
            parts.append(str(request["instance_label"]))
        if request.get("chat_id"):
            parts.append(f"chat:{request['chat_id']}")
        elif request.get("user_id"):
            parts.append(f"user:{request['user_id']}")
        return "|".join(parts)

    def _action_record_view(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._scrub_value(
            {
                "id": record.get("id"),
                "status": record.get("status"),
                "created_at": record.get("created_at"),
                "started_at": record.get("started_at"),
                "finished_at": record.get("finished_at"),
                "request": record.get("request"),
                "result": record.get("result"),
                "error": record.get("error"),
                "duplicate": bool(record.get("duplicate")),
            }
        )

    def list_action_records(self) -> dict[str, Any]:
        with self._lock:
            actions = [self._action_record_view(item) for item in self._records]
            running = sum(1 for item in self._records if str(item.get("status") or "") == "running")
        return {
            "ok": True,
            "running_count": running,
            "actions": actions,
        }

    def get_action_record(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._index.get(str(action_id or ""))
        if record is None:
            return None
        return self._action_record_view(record)

    def launch_action(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        dedupe_key = self._action_scope_text(request)
        with self._lock:
            existing_id = self._running.get(dedupe_key)
            if existing_id:
                existing = self._index.get(existing_id)
                if existing is not None:
                    existing["duplicate"] = True
                    return self._action_record_view(existing), True
            action_id = f"ops-{uuid4().hex[:10]}"
            record = {
                "id": action_id,
                "status": "queued",
                "created_at": now_utc_iso(),
                "started_at": None,
                "finished_at": None,
                "request": request,
                "result": None,
                "error": None,
                "duplicate": False,
                "dedupe_key": dedupe_key,
            }
            if self._records.maxlen and len(self._records) >= self._records.maxlen:
                stale = self._records.pop()
                stale_id = str(stale.get("id") or "")
                if stale_id:
                    self._index.pop(stale_id, None)
            self._records.appendleft(record)
            self._index[action_id] = record
            self._running[dedupe_key] = action_id

        def _runner() -> None:
            record["status"] = "running"
            record["started_at"] = now_utc_iso()
            try:
                record["result"] = self._execute_action(
                    action=str(request.get("action") or ""),
                    roots=self._roots,
                    instance_label_value=request.get("instance_label"),
                    chat_id=request.get("chat_id"),
                    user_id=request.get("user_id"),
                )
                record["status"] = "completed"
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
            finally:
                record["finished_at"] = now_utc_iso()
                with self._lock:
                    if self._running.get(dedupe_key) == action_id:
                        self._running.pop(dedupe_key, None)

        thread = threading.Thread(
            target=_runner,
            daemon=True,
            name=f"ops-dashboard-{request.get('action') or 'action'}",
        )
        thread.start()
        return self._action_record_view(record), False


def build_default_action_roots(
    *,
    config_file: Path | None = None,
    instance_roots: list[Path] | None = None,
) -> list[Path]:
    roots = [path.resolve() for path in list(instance_roots or []) if isinstance(path, Path)]
    if roots:
        return roots
    return discover_instance_roots(config_file=config_file)
