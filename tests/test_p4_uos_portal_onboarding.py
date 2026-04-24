from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import requests

from sidae_secretary import onboarding
from sidae_secretary.connectors.uos_portal import UOS_PORTAL_SCHOOL_SLUG
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline
from sidae_secretary.secret_store import StoredSecretRef


class FakeSecretStore:
    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        self._items[str(key)] = str(secret)
        return StoredSecretRef(kind="inline", ref=str(key))

    def read_secret(self, *, ref: StoredSecretRef) -> str:
        return self._items[str(ref.ref)]


def _build_server(
    *,
    tmp_path: Path,
    db: Database,
    secret_store: FakeSecretStore | None = None,
    telegram_client_factory=None,
):
    settings = SimpleNamespace(
        telegram_bot_token="token" if telegram_client_factory else "",
        uclass_token_service="moodle_mobile_app",
        uclass_request_method="GET",
        uclass_func_site_info="core_webservice_get_site_info",
        onboarding_browser_profiles_dir=tmp_path / "profiles",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        onboarding_browser_headless=True,
    )
    server = onboarding.build_onboarding_http_server(
        host="127.0.0.1",
        port=0,
        settings=settings,
        db=db,
        secret_store=secret_store or FakeSecretStore(),
        telegram_client_factory=telegram_client_factory,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, int(server.server_address[1])


def test_legacy_portal_and_browser_connect_routes_are_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    server, thread, port = _build_server(tmp_path=tmp_path, db=db)
    try:
        portal_response = requests.get(f"http://127.0.0.1:{port}/portal-connect", timeout=5)
        browser_response = requests.get(f"http://127.0.0.1:{port}/browser-connect", timeout=5)
        assert portal_response.status_code == 404
        assert browser_response.status_code == 404
        assert "더 이상 사용하지 않습니다" in portal_response.text
        assert "더 이상 사용하지 않습니다" in browser_response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_uos_school_account_onboarding_stores_moodle_connection_without_portal_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test", "school_query": "시립대 포털", "school_slug": "uos_portal"},
    )
    sent_messages: list[tuple[str, str]] = []
    secret_store = FakeSecretStore()
    primed: list[tuple[str, int | None]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(
        onboarding,
        "exchange_moodle_credentials",
        lambda **kwargs: {
            "school_slug": "uos_online_class",
            "display_name": "서울시립대학교 온라인강의실",
            "ws_base_url": "https://uclass.uos.ac.kr/webservice/rest/server.php",
            "username": "uos-demo-student",
            "token": "uos-issued-token",
            "site_info": {"sitename": "서울시립대학교 온라인강의실"},
            "verified_at": "2026-03-11T05:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_prime_post_connect_portal_sync",
        lambda *, settings, db, chat_id, user_id, fetched=None: (
            primed.append((chat_id, user_id)) or {"ok": True, "status": "success"}
        ),
    )

    server, thread, port = _build_server(
        tmp_path=tmp_path,
        db=db,
        secret_store=secret_store,
        telegram_client_factory=FakeTelegram,
    )
    try:
        response = requests.post(
            f"http://127.0.0.1:{port}/moodle-connect",
            data={
                "token": session["token"],
                "school_name": "시립대 포털",
                "username": "uos-demo-student",
                "password": "secret-pass",
            },
            timeout=5,
        )
        assert response.status_code == 200
        assert "연결 완료" in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    moodle_connections = db.list_moodle_connections(chat_id="77777")
    assert len(moodle_connections) == 1
    assert moodle_connections[0]["school_slug"] == "uos_online_class"
    assert not moodle_connections[0]["login_secret_kind"]
    assert not moodle_connections[0]["login_secret_ref"]

    portal_session = db.get_lms_browser_session(chat_id="77777", school_slug=UOS_PORTAL_SCHOOL_SLUG)
    assert portal_session is None
    assert primed == [("77777", int(moodle_connections[0]["user_id"]))]

    auth_attempts = db.list_auth_attempts(session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND, limit=5)
    assert auth_attempts[0]["status"] == "success"
    assert sent_messages == [
        (
            "77777",
            "[Sidae] 학교 계정 연결 완료\n\n- 학교: 서울시립대학교\n- 온라인강의실: 서울시립대학교 온라인강의실\n- 시간표: 학교 공식 API 자동 동기화\n- ID: uos-demo-student\n- 온라인강의실 접근 token만 이 사용자용으로 저장했습니다.",
        )
    ]


def test_uos_school_account_onboarding_warns_when_portal_prime_requires_reconnect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test", "school_query": "시립대 포털", "school_slug": "uos_portal"},
    )
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(
        onboarding,
        "exchange_moodle_credentials",
        lambda **kwargs: {
            "school_slug": "uos_online_class",
            "display_name": "서울시립대학교 온라인강의실",
            "ws_base_url": "https://uclass.uos.ac.kr/webservice/rest/server.php",
            "username": "uos-demo-student",
            "token": "uos-issued-token",
            "site_info": {"sitename": "서울시립대학교 온라인강의실"},
            "verified_at": "2026-03-11T05:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_prime_post_connect_portal_sync",
        lambda **kwargs: {
            "ok": False,
            "status": "error",
            "reason": "UOS portal session expired; reconnect required",
        },
    )

    server, thread, port = _build_server(
        tmp_path=tmp_path,
        db=db,
        secret_store=FakeSecretStore(),
        telegram_client_factory=FakeTelegram,
    )
    try:
        response = requests.post(
            f"http://127.0.0.1:{port}/moodle-connect",
            data={
                "token": session["token"],
                "school_name": "시립대 포털",
                "username": "uos-demo-student",
                "password": "secret-pass",
            },
            timeout=5,
        )
        assert response.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "시간표: 학교 공식 API 자동 동기화" in message
    assert "시간표 동기화 확인: UOS portal session expired; reconnect required" in message
    assert "온라인강의실 접근 token만 이 사용자용으로 저장했습니다." in message


def test_prime_post_connect_portal_sync_uses_prefetched_fetch(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        pipeline,
        "record_uos_portal_timetable_fetch_for_user",
        lambda **kwargs: captured.update(kwargs) or {"ok": True, "status": "success", "upserted_events": 2},
    )
    monkeypatch.setattr(
        pipeline,
        "prime_uos_portal_timetable_for_user",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("fallback prime should not run")),
    )

    result = onboarding._prime_post_connect_portal_sync(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        chat_id="77777",
        user_id=1,
        fetched={"ok": True, "events": [{"external_id": "portal:1"}]},
    )

    assert result["ok"] is True
    assert captured["chat_id"] == "77777"
    assert captured["user_id"] == 1
    assert captured["fetched"] == {"ok": True, "events": [{"external_id": "portal:1"}]}


def test_uos_school_account_onboarding_keeps_moodle_and_disables_broken_portal_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test", "school_query": "시립대 포털", "school_slug": "uos_portal"},
    )
    user = db.ensure_user_for_chat(chat_id="77777", timezone_name="Asia/Seoul")
    legacy_profile_dir = tmp_path / "profiles" / "legacy"
    legacy_profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="77777",
        school_slug=UOS_PORTAL_SCHOOL_SLUG,
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=legacy_profile_dir,
        metadata_json={"source": "legacy"},
        user_id=int(user["id"]),
    )
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(
        onboarding,
        "exchange_moodle_credentials",
        lambda **kwargs: {
            "school_slug": "uos_online_class",
            "display_name": "서울시립대학교 온라인강의실",
            "ws_base_url": "https://uclass.uos.ac.kr/webservice/rest/server.php",
            "username": "uos-demo-student",
            "token": "uos-issued-token",
            "site_info": {"sitename": "서울시립대학교 온라인강의실"},
            "verified_at": "2026-03-11T05:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_prime_post_connect_portal_sync",
        lambda **kwargs: {"ok": True, "status": "success"},
    )

    server, thread, port = _build_server(
        tmp_path=tmp_path,
        db=db,
        secret_store=FakeSecretStore(),
        telegram_client_factory=FakeTelegram,
    )
    try:
        response = requests.post(
            f"http://127.0.0.1:{port}/moodle-connect",
            data={
                "token": session["token"],
                "school_name": "시립대 포털",
                "username": "uos-demo-student",
                "password": "secret-pass",
            },
            timeout=5,
        )
        assert response.status_code == 200
        assert "연결 완료" in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    moodle_connections = db.list_moodle_connections(chat_id="77777")
    assert len(moodle_connections) == 1

    active_portal_sessions = db.list_lms_browser_sessions(chat_id="77777", status="active")
    assert active_portal_sessions == []

    all_portal_sessions = db.list_lms_browser_sessions(chat_id="77777", status=None)
    assert len(all_portal_sessions) == 1
    assert all_portal_sessions[0]["status"] == "inactive"
    assert all_portal_sessions[0]["metadata_json"]["disabled_reason"] == "official_api_only"
    assert not legacy_profile_dir.exists()

    assert len(sent_messages) == 1
    message = sent_messages[0][1]
    assert "시간표: 학교 공식 API 자동 동기화" in message
    assert "온라인강의실 접근 token만 이 사용자용으로 저장했습니다." in message


def test_uos_school_account_connect_form_mentions_official_api_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test", "school_query": "서울시립대학교 온라인강의실"},
    )

    server, thread, port = _build_server(tmp_path=tmp_path, db=db)
    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/moodle-connect",
            params={"token": session["token"]},
            timeout=5,
        )
        assert response.status_code == 200
        assert "시간표는 학교 공식 API로 자동 동기화합니다." in response.text
        assert "포털 세션 없이 학교 공식 API를 사용합니다." in response.text
        assert "온라인강의실과 포털/대학행정을 함께 연결합니다." not in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_moodle_connect_form_uses_registered_school_select_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test"},
    )
    server, thread, port = _build_server(tmp_path=tmp_path, db=db)
    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/moodle-connect",
            params={"token": session["token"]},
            timeout=5,
        )
        assert response.status_code == 200
        assert '<select id="school_name" name="school_name" required>' in response.text
        assert "온라인강의실 주소" not in response.text
        assert 'name="lms_url"' not in response.text
        assert "학교는 목록에서만 선택할 수 있습니다." in response.text
        attempts = db.list_auth_attempts(status=None, session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND, limit=5)
        assert attempts[0]["status"] == "viewed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_moodle_connect_rate_limits_repeated_failures(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test"},
    )
    monkeypatch.setattr(
        onboarding,
        "exchange_moodle_credentials",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("UOS portal login failed")),
    )

    server, thread, port = _build_server(tmp_path=tmp_path, db=db)
    try:
        for _ in range(onboarding.AUTH_MAX_FAILED_PER_SESSION):
            response = requests.post(
                f"http://127.0.0.1:{port}/moodle-connect",
                data={
                    "token": session["token"],
                    "school_name": "서울시립대학교 온라인강의실",
                    "username": "uos-demo-student",
                    "password": "wrong-pass",
                },
                timeout=5,
            )
            assert response.status_code == 400
            assert "로그인에 실패했습니다." in response.text

        blocked = requests.post(
            f"http://127.0.0.1:{port}/moodle-connect",
            data={
                "token": session["token"],
                "school_name": "서울시립대학교 온라인강의실",
                "username": "uos-demo-student",
                "password": "wrong-pass",
            },
            timeout=5,
        )
        assert blocked.status_code == 429
        assert "로그인 시도가 너무 많습니다." in blocked.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert db.count_auth_attempts(
        onboarding_session_id=int(session["id"]),
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        status="failed",
    ) == onboarding.AUTH_MAX_FAILED_PER_SESSION
    assert db.count_auth_attempts(
        onboarding_session_id=int(session["id"]),
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        status="blocked",
    ) == 1
    snapshot = db.auth_attempt_dashboard_snapshot(session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND)
    assert snapshot["window_last_15m"]["blocked"] == 1
