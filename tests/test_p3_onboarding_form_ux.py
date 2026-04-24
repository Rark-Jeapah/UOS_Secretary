from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import requests

from sidae_secretary import onboarding
from sidae_secretary.db import Database


def test_onboarding_form_explains_login_identifier_field(tmp_path: Path) -> None:
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
        assert "학교 로그인 계정 (학번 또는 ID)" in response.text
        assert "학교 로그인 화면에서 쓰는 계정 식별자를 그대로 입력하세요." in response.text
        assert "계정 칸에는 학교 로그인 화면에서 쓰는 학번 또는 ID를 그대로 입력하세요." in response.text
        assert '<select id="school_name" name="school_name" required>' in response.text
        assert 'name="lms_url"' not in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
