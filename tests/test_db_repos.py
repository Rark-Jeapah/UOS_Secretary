from __future__ import annotations

from pathlib import Path

from sidae_secretary import db_auth_attempts, db_connections, db_dashboard_queries, db_sync
from sidae_secretary.db import Database


def test_database_auth_attempt_facade_delegates_to_repo(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    captured: list[tuple[str, dict[str, object]]] = []

    def _make_stub(name: str, result):
        def _stub(database, **kwargs):
            captured.append((name, {"database": database, **kwargs}))
            return result

        return _stub

    monkeypatch.setattr(db_auth_attempts, "record_auth_attempt", _make_stub("record", {"id": 1}))
    monkeypatch.setattr(db_auth_attempts, "count_auth_attempts", _make_stub("count", 7))
    monkeypatch.setattr(db_auth_attempts, "list_auth_attempts", _make_stub("list", [{"id": 2}]))
    monkeypatch.setattr(
        db_auth_attempts,
        "auth_attempt_dashboard_snapshot",
        _make_stub("snapshot", {"window_last_15m": {"total": 1}}),
    )

    assert db.record_auth_attempt(chat_id="77777", status="success") == {"id": 1}
    assert db.count_auth_attempts(status="failed") == 7
    assert db.list_auth_attempts(session_kind="moodle_connect") == [{"id": 2}]
    assert db.auth_attempt_dashboard_snapshot(session_kind="moodle_connect") == {
        "window_last_15m": {"total": 1}
    }

    assert [name for name, _ in captured] == ["record", "count", "list", "snapshot"]
    assert all(item["database"] is db for _, item in captured)


def test_database_connection_facade_delegates_to_repo(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    captured: list[tuple[str, dict[str, object]]] = []

    def _make_stub(name: str, result):
        def _stub(database, **kwargs):
            captured.append((name, {"database": database, **kwargs}))
            return result

        return _stub

    monkeypatch.setattr(db_connections, "upsert_moodle_connection", _make_stub("upsert_moodle", {"id": 1}))
    monkeypatch.setattr(db_connections, "list_moodle_connections", _make_stub("list_moodle", [{"id": 2}]))
    monkeypatch.setattr(db_connections, "get_moodle_connection", _make_stub("get_moodle", {"id": 3}))
    monkeypatch.setattr(db_connections, "upsert_lms_browser_session", _make_stub("upsert_browser", {"id": 4}))
    monkeypatch.setattr(db_connections, "get_lms_browser_session", _make_stub("get_browser", {"id": 5}))
    monkeypatch.setattr(db_connections, "list_lms_browser_sessions", _make_stub("list_browser", [{"id": 6}]))
    monkeypatch.setattr(
        db_connections,
        "mark_lms_browser_session_inactive",
        _make_stub("mark_browser_inactive", {"id": 7}),
    )

    assert (
        db.upsert_moodle_connection(
            chat_id="77777",
            school_slug="uos_online_class",
            display_name="UClass",
            ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
            username="student-demo-001",
            secret_kind="inline",
            secret_ref="stored::token",
        )
        == {"id": 1}
    )
    assert db.list_moodle_connections(chat_id="77777") == [{"id": 2}]
    assert db.get_moodle_connection(chat_id="77777", school_slug="uos_online_class") == {"id": 3}
    assert (
        db.upsert_lms_browser_session(
            chat_id="77777",
            school_slug="uos_portal",
            provider="uos_portal",
            display_name="서울시립대학교 포털/대학행정",
            login_url="https://portal.uos.ac.kr/p/STUD/",
            profile_dir="/tmp/profile",
        )
        == {"id": 4}
    )
    assert db.get_lms_browser_session(chat_id="77777", school_slug="uos_portal") == {"id": 5}
    assert db.list_lms_browser_sessions(chat_id="77777") == [{"id": 6}]
    assert db.mark_lms_browser_session_inactive(chat_id="77777", school_slug="uos_portal") == {"id": 7}

    assert [name for name, _ in captured] == [
        "upsert_moodle",
        "list_moodle",
        "get_moodle",
        "upsert_browser",
        "get_browser",
        "list_browser",
        "mark_browser_inactive",
    ]
    assert all(item["database"] is db for _, item in captured)


def test_database_sync_facade_delegates_to_repo(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    captured: list[tuple[str, dict[str, object]]] = []

    def _make_stub(name: str, result):
        def _stub(database, **kwargs):
            captured.append((name, {"database": database, **kwargs}))
            return result

        return _stub

    monkeypatch.setattr(db_sync, "sync_dashboard_snapshot", _make_stub("sync_dashboard", {"counts": {}}))
    monkeypatch.setattr(db_sync, "latest_weather_snapshot", _make_stub("latest_weather", {"current": {}}))
    monkeypatch.setattr(
        db_dashboard_queries,
        "dashboard_snapshot",
        _make_stub("dashboard_snapshot", {"upcoming_events": []}),
    )

    assert db.sync_dashboard_snapshot(user_id=7) == {"counts": {}}
    assert db.latest_weather_snapshot(user_id=7, allow_global_fallback=False) == {"current": {}}
    assert db.dashboard_snapshot(now_iso="2099-03-01T00:00:00+00:00", user_id=7) == {"upcoming_events": []}

    assert [name for name, _ in captured] == [
        "sync_dashboard",
        "latest_weather",
        "dashboard_snapshot",
    ]
    assert all(item["database"] is db for _, item in captured)
