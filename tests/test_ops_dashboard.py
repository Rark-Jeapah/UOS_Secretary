from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import requests

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline
from sidae_secretary.ops_dashboard import (
    build_ops_dashboard_http_server,
    build_ops_dashboard_snapshot,
    render_ops_dashboard_html,
)


def _write_instance_env(root: Path, *, instance_name: str = "", llm_enabled: bool = True) -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    env_lines = [
        f"INSTANCE_NAME={instance_name}",
        "TIMEZONE=Asia/Seoul",
        "DATABASE_PATH=data/sidae.db",
        "UCLASS_WS_BASE=https://uclass.example.com/webservice/rest/server.php",
        "UCLASS_WSTOKEN=test-token",
        f"LLM_ENABLED={'true' if llm_enabled else 'false'}",
        "LLM_PROVIDER=local",
        "LLM_MODEL=gemma4",
        "LLM_LOCAL_ENDPOINT=http://127.0.0.1:11434/api/chat",
        "TELEGRAM_ENABLED=true",
        "TELEGRAM_ALLOWED_CHAT_IDS=12345",
    ]
    (root / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return root / "data" / "sidae.db"


def test_build_beta_ops_health_report_summarizes_surface_statuses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="inline",
        secret_ref="dummy-secret",
        status="active",
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2099-03-17T09:05:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2099-03-17T09:05:00+09:00",
            },
            "payload_source": pipeline.UOS_OPENAPI_TIMETABLE_SOURCE,
            "payload_sources": [
                {"payload_source": pipeline.UOS_OPENAPI_TIMETABLE_SOURCE}
            ],
        },
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2099-03-17T09:00:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2099-03-17T09:00:00+09:00",
            },
            "site": "UClass",
            "wsfunctions": {
                "core_course_get_contents": {
                    "ok": 1,
                    "failed": 0,
                }
            },
        },
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_telegram",
        last_run_at="2099-03-17T09:10:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2099-03-17T09:10:00+09:00",
            },
            "fetched": 1,
            "stored": 1,
            "menu": {"ok": True},
            "commands": {"failed": 0, "blocked_sends": 0},
            "reminders": {"failed": 0, "sent": 1},
        },
    )
    db.update_sync_state(
        "sync_weather",
        last_run_at="2099-03-17T09:12:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2099-03-17T09:12:00+09:00",
            },
            "observed_at": "2099-03-17T09:12:00+09:00",
            "current": {"temperature_c": 13.5},
            "air_quality": {"ok": True},
        },
        user_id=int(user["id"]),
    )

    monkeypatch.setattr(
        pipeline,
        "_resolve_uos_portal_timetable_targets",
        lambda settings, db: [{"user_id": int(user["id"]), "chat_id": "12345"}],
    )

    health = pipeline.build_beta_ops_health_report(
        SimpleNamespace(
            timezone="Asia/Seoul",
            uos_openapi_timetable_url="https://wise.uos.ac.kr/COM/ApiTimeTable/list.do",
            uos_openapi_timetable_api_key="test-key",
            uclass_ws_base="https://uclass.uos.ac.kr/webservice/rest/server.php",
            telegram_enabled=True,
            telegram_bot_token="token",
            telegram_allowed_chat_ids=["12345"],
            telegram_commands_enabled=True,
            weather_enabled=True,
        ),
        db,
        user_id=int(user["id"]),
    )

    assert health["ready_count"] == 5
    assert health["not_ready_count"] == 1
    assert health["overall_ready"] is False
    assert health["surfaces"]["uos_official_api"]["status"] == "ready"
    assert health["surfaces"]["uclass_sync"]["status"] == "ready"
    assert health["surfaces"]["telegram_listener"]["status"] == "ready"
    assert health["surfaces"]["telegram_send"]["status"] == "ready"
    assert health["surfaces"]["weather_sync"]["status"] == "ready"
    assert health["surfaces"]["notice_fetch"]["status"] == "never_checked"


def test_build_beta_ops_health_report_aggregates_notice_feed_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.update_sync_state(
        "uos_notice_snapshot_general",
        last_run_at="2099-03-17T09:20:00+09:00",
        last_cursor_json={
            "last_attempt": {
                "ok": False,
                "attempted_at": "2099-03-17T09:20:00+09:00",
                "error": "timeout",
                "http_status": 504,
            },
            "snapshot": {
                "fetched_at": "2099-03-17T09:18:00+09:00",
                "empty": False,
                "notices": [{"title": "공지 1"}],
            },
        },
    )
    db.update_sync_state(
        "uos_notice_snapshot_academic",
        last_run_at="2099-03-17T09:21:00+09:00",
        last_cursor_json={
            "last_attempt": {
                "ok": True,
                "attempted_at": "2099-03-17T09:21:00+09:00",
                "http_status": 200,
            },
            "snapshot": {
                "fetched_at": "2099-03-17T09:21:00+09:00",
                "empty": False,
                "notices": [{"title": "학사 공지"}],
            },
        },
    )

    monkeypatch.setattr(
        pipeline,
        "_resolve_uos_portal_timetable_targets",
        lambda settings, db: [],
    )

    health = pipeline.build_beta_ops_health_report(
        SimpleNamespace(
            timezone="Asia/Seoul",
            uos_openapi_timetable_url="",
            uos_openapi_timetable_api_key="",
            uclass_ws_base="",
            telegram_enabled=False,
            telegram_bot_token="",
            telegram_allowed_chat_ids=[],
            telegram_commands_enabled=False,
            weather_enabled=False,
        ),
        db,
    )

    notice = health["surfaces"]["notice_fetch"]
    assert notice["status"] == "degraded"
    assert notice["reason"] == "timeout"
    assert notice["details"]["feeds"]["general"]["status"] == "degraded"
    assert notice["details"]["feeds"]["academic"]["status"] == "ready"


def test_build_ops_dashboard_snapshot_collects_instance_and_user_cards(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "test-weather-key"
    prod_root = tmp_path / "UOS_secretary"
    db_path = _write_instance_env(prod_root)
    db = Database(db_path)
    db.init()

    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_user_preferences(
        user_id=int(user["id"]),
        telegram_chat_allowed=True,
        scheduled_briefings_enabled=True,
        daily_digest_enabled=False,
        material_brief_push_enabled=True,
    )
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="file",
        secret_ref="telegram:12345:moodle:uos_online_class",
        user_id=int(user["id"]),
    )
    db.upsert_event(
        external_id="portal:event:1",
        source="portal",
        start="2099-03-17T18:00:00+09:00",
        end="2099-03-17T20:00:00+09:00",
        title="대학글쓰기",
        location="법학관 116호",
        rrule=None,
        metadata_json={"source": "portal"},
        user_id=int(user["id"]),
    )
    db.upsert_task(
        external_id="uclass:task:1",
        source="uclass",
        due_at="2099-03-18T23:59:00+09:00",
        title="학문 윤리 서약서 제출",
        status="open",
        metadata_json={"course_name": "대학글쓰기"},
        user_id=int(user["id"]),
    )
    db.record_artifact(
        external_id="uclass:artifact:1",
        source="uclass",
        filename="3주차_자기소개서.hwp",
        icloud_path=None,
        content_hash="artifact-1",
        metadata_json={"course_name": "대학글쓰기"},
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uclass",
        last_run_at="2099-03-17T09:00:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "new_items": 3,
                "action_required": 1,
                "last_success_at": "2099-03-17T09:00:00+09:00",
            }
        },
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2099-03-17T09:05:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "new_items": 1,
                "action_required": 0,
                "last_success_at": "2099-03-17T09:05:00+09:00",
            }
        },
        user_id=int(user["id"]),
    )

    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._collect_service_processes",
        lambda instance_configs: {
            "counts": {"total": 2, "sidae": 1, "ollama": 1},
            "processes": [
                {
                    "pid": 100,
                    "cpu_percent": 0.1,
                    "memory_percent": 1.5,
                    "elapsed": "00:10",
                    "command": "python -m sidae_secretary.cli sync --config-file /tmp/UOS_secretary/config.toml",
                    "kind": "sync",
                    "kind_label": "Sync",
                    "instance_label": "prod",
                },
                {
                    "pid": 200,
                    "cpu_percent": 4.0,
                    "memory_percent": 10.0,
                    "elapsed": "00:12",
                    "command": "ollama serve",
                    "kind": "ollama",
                    "kind_label": "Ollama serve",
                    "instance_label": None,
                },
            ],
        },
    )
    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._collect_log_snapshot",
        lambda file_limit=8, tail_lines=30: {
            "files": [
                {
                    "path": "/tmp/com.sidae.secretary.err.log",
                    "mtime": "2099-03-17T09:10:00+09:00",
                    "warning_count": 1,
                    "error_count": 0,
                    "tail": [
                        '{"level":"WARNING","msg":"weather failed https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-dfs_xy_lonlat?lat=37.58&lon=127.05&authKey='
                        + secret
                        + '"}'
                    ],
                }
            ],
            "llm_highlights": [
                "com.sidae.secretary.err.log: weather failed https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-dfs_xy_lonlat?lat=37.58&lon=127.05&authKey="
                + secret
            ],
        },
    )
    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._probe_ollama_endpoint",
        lambda base_url, timeout_sec: {
            "base_url": base_url,
            "http_ok": True,
            "response_ms": 48,
            "tags_response_ms": 12,
            "loaded_models": [
                {
                    "name": "gemma4",
                    "processor": "100% GPU",
                    "context": 16384,
                    "until": "4 minutes from now",
                    "size": 9100000000,
                }
            ],
            "loaded_model_names": ["gemma4"],
            "available_models": ["gemma4"],
            "error": None,
        },
    )
    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard.inspect_last_failed_stage",
        lambda settings, db, user_id=None, chat_id=None, component=None: {
            "ok": False,
            "match": {
                "component": "uclass_sync",
                "stage": "material_download",
                "status": "error",
                "message": "403 Client Error: Forbidden for url: https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-dfs_xy_lonlat?lat=37.58&lon=127.05&authKey="
                + secret,
                "last_run_at": "2099-03-17T09:00:00+09:00",
            },
        },
    )

    snapshot = build_ops_dashboard_snapshot(
        instance_roots=[prod_root],
        max_users=10,
        refresh_interval_sec=9,
    )

    assert snapshot["refresh_interval_sec"] == 9
    assert snapshot["services"]["counts"]["ollama"] == 1
    assert snapshot["headline"]["tone"] == "warn"
    assert snapshot["headline"]["critical_count"] == 0
    assert snapshot["headline"]["warning_count"] > 0
    assert snapshot["llm"]["status"] == "degraded"
    assert snapshot["llm"]["configured_models"] == ["gemma4"]
    assert snapshot["llm"]["loaded_models"][0]["name"] == "gemma4"
    assert len(snapshot["instances"]) == 1
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert secret not in serialized
    assert "authKey=%2A%2A%2A" in serialized

    instance = snapshot["instances"][0]
    assert instance["label"] == "prod"
    assert instance["database_path"] == str(db_path.resolve())
    assert len(instance["users"]) == 1
    assert instance["processes"][0]["kind"] == "sync"

    user_card = instance["users"][0]
    assert user_card["chat_id"] == "12345"
    assert user_card["preferences"]["scheduled_briefings_enabled"] is True
    assert user_card["connections"][0]["school_slug"] == "uos_online_class"
    assert user_card["next_event"]["title"] == "대학글쓰기"
    assert user_card["next_task"]["title"] == "학문 윤리 서약서 제출"
    assert user_card["recent_material"]["filename"] == "3주차_자기소개서.hwp"
    assert user_card["material_brief_summary"]["missing_count"] == 1
    assert user_card["material_brief_summary"]["ready_count"] == 0
    assert "authKey=%2A%2A%2A" in user_card["last_failed_stage"]["message"]


def test_build_ops_dashboard_snapshot_marks_runtime_failures_as_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prod_root = tmp_path / "UOS_secretary"
    db_path = _write_instance_env(prod_root)
    db = Database(db_path)
    db.init()

    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._collect_service_processes",
        lambda instance_configs: {
            "counts": {"total": 0, "sidae": 0, "ollama": 0},
            "processes": [],
        },
    )
    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._collect_log_snapshot",
        lambda file_limit=8, tail_lines=30: {
            "files": [],
            "llm_highlights": [],
        },
    )
    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._probe_ollama_endpoint",
        lambda base_url, timeout_sec: (_ for _ in ()).throw(
            requests.RequestException("connection refused")
        ),
    )

    snapshot = build_ops_dashboard_snapshot(instance_roots=[prod_root], max_users=5)

    assert snapshot["llm"]["status"] == "error"
    assert snapshot["headline"]["tone"] == "err"
    assert snapshot["headline"]["critical_count"] >= 1


def test_ops_dashboard_http_server_serves_html_and_snapshot() -> None:
    snapshot = {
        "generated_at": "2099-03-17T09:00:00+09:00",
        "host": "mini",
        "user": "runtime_user",
        "cwd": "/srv/sidae/apps/UOS_secretary",
        "python": "3.11.0",
        "refresh_interval_sec": 7,
        "instances": [],
        "services": {"counts": {"total": 0, "sidae": 0, "ollama": 0}, "processes": []},
        "logs": {"files": []},
        "llm": {
            "status": "disabled",
            "error": None,
            "configured_models": [],
            "loaded_models": [],
            "endpoints": [],
            "recent_warnings": [],
        },
    }
    server = build_ops_dashboard_http_server(
        host="127.0.0.1",
        port=0,
        refresh_interval_sec=7,
        snapshot_factory=lambda: snapshot,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        health = requests.get(f"{base_url}/healthz", timeout=5)
        page = requests.get(base_url, timeout=5)
        api = requests.get(f"{base_url}/api/snapshot", timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert health.status_code == 200
    assert health.json()["service"] == "sidae-ops-dashboard"
    assert page.status_code == 200
    assert "UOS Secretary 운영 대시보드" in page.text
    assert api.status_code == 200
    assert api.json()["host"] == "mini"
    assert api.json()["refresh_interval_sec"] == 7


def test_ops_dashboard_http_server_runs_and_lists_actions(monkeypatch) -> None:
    snapshot = {
        "generated_at": "2099-03-17T09:00:00+09:00",
        "host": "mini",
        "user": "runtime_user",
        "cwd": "/srv/sidae/apps/UOS_secretary",
        "python": "3.11.0",
        "refresh_interval_sec": 7,
        "instances": [],
        "services": {"counts": {"total": 0, "sidae": 0, "ollama": 0}, "processes": []},
        "logs": {"files": []},
        "llm": {
            "status": "disabled",
            "error": None,
            "configured_models": [],
            "loaded_models": [],
            "endpoints": [],
            "recent_warnings": [],
        },
    }

    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._execute_ops_dashboard_action",
        lambda **kwargs: {
            "ok": True,
            "ready": False,
            "totals": {"environments": 2, "active_users": 11, "users_with_findings": 3, "missing_briefs": 4},
            "environments": [],
        },
    )

    server = build_ops_dashboard_http_server(
        host="127.0.0.1",
        port=0,
        refresh_interval_sec=7,
        snapshot_factory=lambda: snapshot,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        launched = requests.post(
            f"{base_url}/api/actions/run",
            json={"action": "global_audit"},
            timeout=5,
        )
        assert launched.status_code == 202
        action_id = launched.json()["action"]["id"]

        detail = {}
        deadline = time.time() + 5
        while time.time() < deadline:
            fetched = requests.get(f"{base_url}/api/actions/{action_id}", timeout=5)
            assert fetched.status_code == 200
            detail = fetched.json()["action"]
            if detail["status"] == "completed":
                break
            time.sleep(0.05)
        listed = requests.get(f"{base_url}/api/actions", timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert detail["status"] == "completed"
    assert detail["result"]["totals"]["users_with_findings"] == 3
    assert listed.status_code == 200
    assert listed.json()["actions"][0]["request"]["action"] == "global_audit"


def test_build_ops_dashboard_snapshot_hides_llm_alerts_when_llm_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prod_root = tmp_path / "UOS_secretary"
    db_path = _write_instance_env(prod_root, llm_enabled=False)
    db = Database(db_path)
    db.init()

    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._collect_service_processes",
        lambda instance_configs: {
            "counts": {"total": 0, "sidae": 0, "ollama": 0},
            "processes": [],
        },
    )
    monkeypatch.setattr(
        "sidae_secretary.ops_dashboard._collect_log_snapshot",
        lambda file_limit=8, tail_lines=30: {
            "files": [
                {
                    "path": "/tmp/com.sidae.secretary.err.log",
                    "mtime": "2099-03-17T09:10:00+09:00",
                    "warning_count": 2,
                    "error_count": 0,
                    "tail": [
                        '{"level":"WARNING","msg":"material brief llm fallback"}',
                    ],
                }
            ],
            "llm_highlights": [
                "com.sidae.secretary.err.log: material brief llm fallback",
            ],
        },
    )

    snapshot = build_ops_dashboard_snapshot(instance_roots=[prod_root], max_users=5)

    assert snapshot["llm"]["status"] == "disabled"
    assert snapshot["llm"]["configured_models"] == []
    assert snapshot["llm"]["endpoints"] == []
    assert snapshot["llm"]["recent_warnings"] == []


def test_render_ops_dashboard_html_contains_operator_layout_landmarks() -> None:
    html = render_ops_dashboard_html(refresh_interval_sec=11)

    assert "알림 요약 (Alert Digest)" in html
    assert "운영 실행 (Admin Actions)" in html
    assert "환경 비교 (Environment Comparison)" in html
    assert 'id="alertDigest"' in html
    assert 'id="actionPanel"' in html
    assert 'id="environmentPanels"' in html
    assert 'id="servicesPanel"' in html
    assert 'id="llmPanel"' in html
    assert 'id="userPanel"' in html
    assert 'id="logsPanel"' in html
    assert "overflow-wrap: anywhere" in html
    assert "검색 슬롯 (Search)" in html
    assert "Recent Warning / Error Lines" in html
    assert "영향 범위" in html
    assert "관련 위치로 이동" in html
    assert "function renderHeaderMeta(payload)" in html
    assert "function renderKpis(payload)" in html
    assert "function renderAlertDigest(payload)" in html
    assert "function renderAdminActions(payload, actionPayload)" in html
    assert "function renderEnvironmentPanels(payload)" in html
    assert "function renderServices(payload)" in html
    assert "function renderLlm(payload)" in html
    assert "function renderUsers(payload, actionPayload)" in html
    assert "function renderLogs(payload)" in html
    assert "data-action-trigger" in html
    assert "const REFRESH_MS = 11000;" in html
    assert "__REFRESH_MS__" not in html
