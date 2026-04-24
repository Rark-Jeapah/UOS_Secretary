from __future__ import annotations

import json
import re
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline
from sidae_secretary.moodle_school_directory import BUILTIN_MOODLE_SCHOOL_DIRECTORY
from sidae_secretary import onboarding
from sidae_secretary.secret_store import StoredSecretRef


def test_sync_telegram_connect_command_issues_onboarding_link(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def get_updates(self, offset=None, limit: int = 100, timeout: int = 10):
            return [
                {
                    "update_id": 10,
                    "message": {
                        "date": 1770000000,
                        "text": "/connect 연세대학교",
                        "chat": {"id": 77777, "type": "private"},
                        "from": {"id": 999},
                    },
                }
            ]

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

        def set_my_commands(self, commands):
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    settings = SimpleNamespace(
        telegram_enabled=True,
        telegram_bot_token="token",
        timezone="Asia/Seoul",
        telegram_allowed_chat_ids=["12345"],
        telegram_poll_limit=100,
        telegram_commands_enabled=True,
        include_identity=False,
        onboarding_public_base_url="https://connect.example.invalid",
        onboarding_session_ttl_minutes=15,
    )

    result = pipeline.sync_telegram(settings=settings, db=db)

    assert result["commands"]["processed"] == 1
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "77777"
    assert "[Sidae] 학교 계정 연결" in sent_messages[0][1]
    assert "- 대상: 연세대학교 LearnUs" in sent_messages[0][1]
    match = re.search(r"https://connect\.example\.invalid/moodle-connect\?token=([A-Za-z0-9_\-]+)", sent_messages[0][1])
    assert match is not None
    session = db.get_active_onboarding_session(
        token=match.group(1),
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
    )
    assert session is not None
    assert session["chat_id"] == "77777"
    assert session["metadata_json"]["school_query"] == "연세대학교 LearnUs"
    assert session["metadata_json"]["directory_school_slug"] == "yonsei_learnus"


def test_build_public_moodle_connect_url_rejects_http_scheme() -> None:
    with pytest.raises(ValueError, match="must use https"):
        onboarding.build_public_moodle_connect_url("http://connect.example.invalid", "token")


def test_issue_connect_command_rejects_insecure_public_base_url(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        onboarding_public_base_url="http://connect.example.invalid:8792",
        onboarding_session_ttl_minutes=15,
        timezone="Asia/Seoul",
    )

    result = pipeline._issue_moodle_connect_link(
        settings=settings,
        db=db,
        chat_id="77777",
        school_query="연세대학교",
    )

    assert result["ok"] is False
    assert result["error"] == "ONBOARDING_PUBLIC_BASE_URL must use https"
    assert "반드시 HTTPS 주소여야 합니다" in result["message"]
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM onboarding_sessions").fetchone()[0] == 0


def test_issue_connect_without_school_query_issues_single_school_account_link(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        onboarding_public_base_url="https://connect.example.invalid",
        onboarding_session_ttl_minutes=15,
        timezone="Asia/Seoul",
    )

    result = pipeline._issue_moodle_connect_link(
        settings=settings,
        db=db,
        chat_id="77777",
        school_query=None,
    )

    assert result["ok"] is True
    assert "[Sidae] 학교 계정 연결" in result["message"]
    assert "- 링크를 열어 학교를 선택한 뒤 학교 계정으로 로그인하세요." in result["message"]
    assert "https://connect.example.invalid/moodle-connect?token=" in result["link"]
    token = result["link"].split("token=", 1)[1]
    session = db.get_active_onboarding_session(
        token=token,
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
    )
    assert session is not None
    assert session["metadata_json"]["school_query"] is None


def test_issue_connect_without_school_query_defaults_to_single_allowed_school(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        onboarding_public_base_url="https://connect.example.invalid",
        onboarding_session_ttl_minutes=15,
        onboarding_allowed_school_slugs=["uos_online_class"],
        timezone="Asia/Seoul",
    )

    result = pipeline._issue_moodle_connect_link(
        settings=settings,
        db=db,
        chat_id="77777",
        school_query=None,
    )

    assert result["ok"] is True
    assert "- 대상: 서울시립대학교 온라인강의실" in result["message"]
    token = result["link"].split("token=", 1)[1]
    session = db.get_active_onboarding_session(
        token=token,
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
    )
    assert session is not None
    assert session["metadata_json"]["school_query"] == "서울시립대학교 온라인강의실"
    assert session["metadata_json"]["directory_school_slug"] == "uos_online_class"


def test_issue_connect_command_rejects_disallowed_school_for_beta_scope(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        onboarding_public_base_url="https://connect.example.invalid",
        onboarding_session_ttl_minutes=15,
        onboarding_allowed_school_slugs=["uos_online_class"],
        timezone="Asia/Seoul",
    )

    result = pipeline._issue_moodle_connect_link(
        settings=settings,
        db=db,
        chat_id="77777",
        school_query="연세대학교",
    )

    assert result["ok"] is False
    assert result["error"] == "school_not_allowed"
    assert "서울시립대학교 온라인강의실만 지원합니다." in result["message"]


def test_db_init_seeds_builtin_moodle_school_directory(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    entries = db.list_moodle_school_directory(limit=100)
    assert len(entries) >= len(BUILTIN_MOODLE_SCHOOL_DIRECTORY)

    matches = db.find_moodle_school_directory("연세대학교", limit=3)
    assert matches
    assert matches[0]["display_name"] == "연세대학교 LearnUs"
    assert matches[0]["ws_base_url"] == "https://ys.learnus.org/webservice/rest/server.php"

    uos_matches = db.find_moodle_school_directory("서울시립대", limit=3)
    assert uos_matches
    assert uos_matches[0]["display_name"] == "서울시립대학교 온라인강의실"
    assert uos_matches[0]["ws_base_url"] == "https://uclass.uos.ac.kr/webservice/rest/server.php"

    uos_portal_matches = db.find_moodle_school_directory("시립대 포털", limit=3)
    assert uos_portal_matches
    assert uos_portal_matches[0]["display_name"] == "서울시립대학교 포털/대학행정"
    assert uos_portal_matches[0]["login_url"] == "https://portal.uos.ac.kr/p/STUD/"

    yonsei_portal_matches = db.find_moodle_school_directory("연세포털", limit=3)
    assert yonsei_portal_matches
    assert yonsei_portal_matches[0]["display_name"] == "연세대학교 LearnUs"
    assert (
        yonsei_portal_matches[0]["metadata_json"]["portal"]["login_url"]
        == "https://portal.yonsei.ac.kr/main/index.jsp"
    )


def test_resolve_school_account_connect_plan_exposes_portal_info_for_shared_account_school(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    plan = onboarding.resolve_school_account_connect_plan(
        db,
        school_name="부산대 포털",
    )

    assert plan["bundle_kind"] == "shared_school_account"
    assert plan["school_entry"]["school_slug"] == "pusan_plato"
    assert plan["portal_entry"] is None
    assert plan["portal_info"]["display_name"] == "부산대학교 학생지원시스템"
    assert plan["portal_info"]["login_url"] == "https://onestop.pusan.ac.kr/"


def test_onboarding_server_exchanges_credentials_and_stores_connection(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test"},
    )
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    class FakeSecretStore:
        def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
            return StoredSecretRef(kind="inline", ref=f"stored::{secret}")

        def read_secret(self, *, ref: StoredSecretRef) -> str:
            return ref.ref

    monkeypatch.setattr(
        onboarding,
        "exchange_moodle_credentials",
        lambda **kwargs: {
            "school_slug": "ys_learnus_org",
            "display_name": "LearnUs YONSEI",
            "ws_base_url": "https://ys.learnus.org/webservice/rest/server.php",
            "username": "student-demo-001",
            "token": "issued-token",
            "site_info": {"sitename": "LearnUs YONSEI", "userid": 1},
            "verified_at": "2026-03-10T03:00:00+00:00",
        },
    )
    settings = SimpleNamespace(
        telegram_bot_token="token",
        uclass_token_service="moodle_mobile_app",
        uclass_request_method="GET",
        uclass_func_site_info="core_webservice_get_site_info",
    )
    server = onboarding.build_onboarding_http_server(
        host="127.0.0.1",
        port=0,
        settings=settings,
        db=db,
        secret_store=FakeSecretStore(),
        telegram_client_factory=FakeTelegram,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/moodle-connect",
            params={"token": session["token"]},
            timeout=5,
        )
        assert response.status_code == 200
        assert "학교 계정 연결" in response.text
        assert 'name="school_name"' in response.text
        assert "학교 로그인 계정 (학번 또는 ID)" in response.text
        assert "학교 로그인 화면에서 쓰는 계정 식별자를 그대로 입력하세요." in response.text
        assert "서울시립대학교 포털/대학행정" not in response.text

        submit = requests.post(
            f"http://127.0.0.1:{port}/moodle-connect",
            data={
                "token": session["token"],
                "school_name": "연세대학교",
                "username": "student-demo-001",
                "password": "secret",
            },
            timeout=5,
        )
        assert submit.status_code == 200
        assert "연결 완료" in submit.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    connections = db.list_moodle_connections(chat_id="77777")
    assert len(connections) == 1
    assert connections[0]["display_name"] == "연세대학교 LearnUs"
    assert connections[0]["school_slug"] == "yonsei_learnus"
    assert connections[0]["secret_kind"] == "inline"
    assert connections[0]["secret_ref"] == "stored::issued-token"
    assert not connections[0]["login_secret_kind"]
    assert not connections[0]["login_secret_ref"]
    assert connections[0]["metadata_json"]["directory_school_slug"] == "yonsei_learnus"
    assert (
        connections[0]["metadata_json"]["portal_info"]["login_url"]
        == "https://portal.yonsei.ac.kr/main/index.jsp"
    )
    assert sent_messages == [
        (
            "77777",
            "[Sidae] 학교 계정 연결 완료\n\n- 학교: 연세대학교 LearnUs\n- 포털: 연세포털서비스\n- 포털 로그인: https://portal.yonsei.ac.kr/main/index.jsp\n- ID: student-demo-001\n- 온라인강의실 접근 token을 이 사용자용 보안 저장소에 저장했습니다.\n- 이 학교는 같은 계정으로 포털을 사용합니다.\n- 제약: 연세포털 시간표 페이지와 수강 데이터 엔드포인트는 아직 학교별 자동화가 필요합니다.\n- 현재 사용자-facing 공식 지원 학교는 서울시립대학교입니다.",
        )
    ]


def test_onboarding_server_limits_school_options_for_beta_scope(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test"},
    )
    settings = SimpleNamespace(
        telegram_bot_token="",
        uclass_token_service="moodle_mobile_app",
        uclass_request_method="GET",
        uclass_func_site_info="core_webservice_get_site_info",
        onboarding_allowed_school_slugs=["uos_online_class"],
    )
    server = onboarding.build_onboarding_http_server(
        host="127.0.0.1",
        port=0,
        settings=settings,
        db=db,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/moodle-connect",
            params={"token": session["token"]},
            timeout=5,
        )
        assert response.status_code == 200
        assert "서울시립대학교 온라인강의실" in response.text
        assert "연세대학교 LearnUs" not in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_onboarding_server_rejects_disallowed_school_for_beta_scope(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=onboarding.MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test"},
    )
    settings = SimpleNamespace(
        telegram_bot_token="",
        uclass_token_service="moodle_mobile_app",
        uclass_request_method="GET",
        uclass_func_site_info="core_webservice_get_site_info",
        onboarding_allowed_school_slugs=["uos_online_class"],
    )
    server = onboarding.build_onboarding_http_server(
        host="127.0.0.1",
        port=0,
        settings=settings,
        db=db,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        submit = requests.post(
            f"http://127.0.0.1:{port}/moodle-connect",
            data={
                "token": session["token"],
                "school_name": "연세대학교",
                "username": "student-demo-001",
                "password": "secret",
            },
            timeout=5,
        )
        assert submit.status_code == 400
        assert "등록된 학교를 찾지 못했습니다." in submit.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_onboarding_server_build_avoids_reverse_dns_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = SimpleNamespace(
        telegram_bot_token="",
        uclass_token_service="moodle_mobile_app",
        uclass_request_method="GET",
        uclass_func_site_info="core_webservice_get_site_info",
    )

    def _boom(_host: str) -> str:
        raise AssertionError("socket.getfqdn should not be called")

    monkeypatch.setattr(socket, "getfqdn", _boom)

    server = onboarding.build_onboarding_http_server(
        host="127.0.0.1",
        port=0,
        settings=settings,
        db=db,
    )
    try:
        assert int(server.server_address[1]) > 0
    finally:
        server.server_close()


def test_issue_connect_command_for_browser_session_school_without_public_base_url(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_moodle_school_directory(
        school_slug="browser_session_univ",
        display_name="브라우저세션대학교 LearningX",
        ws_base_url="https://learningx.example.edu/webservice/rest/server.php",
        login_url="https://learningx.example.edu/login",
        homepage_url="https://learningx.example.edu/",
        source_url="https://learningx.example.edu/login",
        aliases=["브라우저세션대학교", "브세대"],
        metadata_json={
            "provider": "learningx",
            "auth_mode": "browser_session",
        },
    )
    settings = SimpleNamespace(
        onboarding_public_base_url=None,
        onboarding_session_ttl_minutes=15,
        timezone="Asia/Seoul",
    )

    result = pipeline._issue_moodle_connect_link(
        settings=settings,
        db=db,
        chat_id="77777",
        school_query="브라우저세션대학교",
    )

    assert result["ok"] is False
    assert result["error"] == "ONBOARDING_PUBLIC_BASE_URL is missing"
    assert "지금은 연결 링크를 만들 수 없습니다." in result["message"]


def test_db_upsert_lms_browser_session_round_trip(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    stored = db.upsert_lms_browser_session(
        chat_id="77777",
        school_slug="browser_session_univ",
        provider="learningx",
        display_name="브라우저세션대학교 LearningX",
        login_url="https://learningx.example.edu/login",
        profile_dir=tmp_path / "profiles" / "learningx" / "browser_session_univ" / "77777",
        status="active",
        last_opened_at="2026-03-10T09:00:00+09:00",
        last_verified_at="2026-03-10T09:03:00+09:00",
        metadata_json={"manual_confirmation": True},
    )

    assert stored["provider"] == "learningx"
    fetched = db.get_lms_browser_session(
        chat_id="77777",
        school_slug="browser_session_univ",
        status="active",
    )
    assert fetched is not None
    assert fetched["display_name"] == "브라우저세션대학교 LearningX"
    assert fetched["metadata_json"]["manual_confirmation"] is True
    items = db.list_lms_browser_sessions(chat_id="77777", status="active")
    assert len(items) == 1
    assert items[0]["school_slug"] == "browser_session_univ"


def test_onboarding_browser_login_cli_stores_browser_session(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    db = Database(db_path)
    db.init()
    db.upsert_moodle_school_directory(
        school_slug="browser_session_univ",
        display_name="브라우저세션대학교 LearningX",
        ws_base_url="https://learningx.example.edu/webservice/rest/server.php",
        login_url="https://learningx.example.edu/login",
        homepage_url="https://learningx.example.edu/",
        source_url="https://learningx.example.edu/login",
        aliases=["브라우저세션대학교", "브세대"],
        metadata_json={
            "provider": "learningx",
            "auth_mode": "browser_session",
        },
    )
    settings = SimpleNamespace(
        database_path=db_path,
        onboarding_browser_profiles_dir=tmp_path / "profiles",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        onboarding_browser_headless=False,
    )
    captured: dict[str, str] = {}

    def _fake_launch_browser_session_login(**kwargs):
        captured["login_url"] = str(kwargs["login_url"])
        captured["profile_dir"] = str(kwargs["profile_dir"])
        return {
            "profile_dir": str(kwargs["profile_dir"]),
            "current_url": "https://learningx.example.edu/home",
            "title": "LearningX",
        }

    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    monkeypatch.setattr(cli, "launch_browser_session_login", _fake_launch_browser_session_login)

    result = runner.invoke(
        cli.app,
        [
            "onboarding",
            "browser-login",
            "--school",
            "브라우저세션대학교",
            "--chat-id",
            "77777",
            "--no-prompt",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "learningx"
    assert payload["school_slug"] == "browser_session_univ"
    assert captured["login_url"] == "https://learningx.example.edu/login"
    assert captured["profile_dir"].endswith("/learningx/browser_session_univ/77777")
    stored = db.get_lms_browser_session(chat_id="77777", school_slug="browser_session_univ")
    assert stored is not None
    assert stored["profile_dir"].endswith("/learningx/browser_session_univ/77777")
    assert stored["metadata_json"]["browser_result"]["title"] == "LearningX"
    assert stored["metadata_json"]["manual_confirmation"] is False


def test_onboarding_browser_login_cli_rejects_school_outside_allowed_scope(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    db = Database(db_path)
    db.init()
    db.upsert_moodle_school_directory(
        school_slug="browser_session_univ",
        display_name="브라우저세션대학교 LearningX",
        ws_base_url="https://learningx.example.edu/webservice/rest/server.php",
        login_url="https://learningx.example.edu/login",
        homepage_url="https://learningx.example.edu/",
        source_url="https://learningx.example.edu/login",
        aliases=["브라우저세션대학교", "브세대"],
        metadata_json={
            "provider": "learningx",
            "auth_mode": "browser_session",
        },
    )
    settings = SimpleNamespace(
        database_path=db_path,
        onboarding_browser_profiles_dir=tmp_path / "profiles",
        onboarding_browser_channel="",
        onboarding_browser_executable_path=None,
        onboarding_browser_headless=False,
        onboarding_allowed_school_slugs=["uos_online_class"],
    )

    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    result = runner.invoke(
        cli.app,
        [
            "onboarding",
            "browser-login",
            "--school",
            "브라우저세션대학교",
            "--chat-id",
            "77777",
            "--no-prompt",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "school_not_allowed"
