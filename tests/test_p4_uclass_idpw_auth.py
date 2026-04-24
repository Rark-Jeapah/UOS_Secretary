from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.connectors import uclass as uclass_connector
from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


class _FakeTokenResponse:
    def __init__(self, payload: dict[str, str], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return dict(self._payload)


def _uclass_settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "uclass_ws_base": "https://uclass.example/webservice/rest/server.php",
        "uclass_wstoken": "",
        "uclass_username": "student",
        "uclass_password": "secret",
        "uclass_token_service": "moodle_mobile_app",
        "uclass_token_endpoint": "",
        "uclass_request_method": "GET",
        "uclass_required_wsfunctions": [],
        "uclass_func_site_info": "core_webservice_get_site_info",
        "uclass_func_popup_notifications": "message_popup_get_popup_notifications",
        "uclass_func_action_events": "core_calendar_get_action_events_by_timesort",
        "uclass_func_courses": "core_enrol_get_users_courses",
        "uclass_func_course_contents": "core_course_get_contents",
        "uclass_func_assignments": "mod_assign_get_assignments",
        "uclass_func_forums": "mod_forum_get_forums_by_courses",
        "uclass_func_forum_discussions": "mod_forum_get_forum_discussions_paginated",
        "uclass_page_limit": 50,
        "uclass_enable_popup_notifications": True,
        "uclass_enable_action_events": True,
        "uclass_enable_courses": True,
        "uclass_enable_contents": False,
        "uclass_enable_assignments": True,
        "uclass_enable_forums": False,
        "uclass_download_materials": False,
        "uclass_download_retries": 1,
        "uclass_download_backoff_sec": 0.01,
        "icloud_dir": tmp_path / "icloud",
        "material_extraction_enabled": False,
        "material_briefing_enabled": False,
        "material_brief_push_enabled": False,
        "material_brief_push_max_items": 3,
        "material_extract_max_chars": 1000,
        "llm_enabled": False,
        "llm_provider": "local",
        "llm_model": "gemma4",
        "llm_timeout_sec": 10,
        "llm_local_endpoint": "http://127.0.0.1:11434/api/chat",
        "timezone": "Asia/Seoul",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_infer_moodle_token_endpoint_from_ws_base() -> None:
    endpoint = uclass_connector.infer_moodle_token_endpoint(
        "https://uclass.example/webservice/rest/server.php"
    )
    assert endpoint == "https://uclass.example/login/token.php"


def test_request_moodle_ws_token_success(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _fake_post(url: str, data=None, timeout: int = 30):
        called["url"] = url
        called["data"] = dict(data or {})
        called["timeout"] = timeout
        return _FakeTokenResponse({"token": "issued-token"})

    monkeypatch.setattr(uclass_connector.requests, "post", _fake_post)
    token = uclass_connector.request_moodle_ws_token(
        ws_base_url="https://uclass.example/webservice/rest/server.php",
        username="student",
        password="secret",
        service="moodle_mobile_app",
    )

    assert token == "issued-token"
    assert called["url"] == "https://uclass.example/login/token.php"
    assert called["data"] == {
        "username": "student",
        "password": "secret",
        "service": "moodle_mobile_app",
    }


def test_request_moodle_mobile_launch_token_uses_valid_candidate(monkeypatch) -> None:
    encoded = base64.b64encode(
        "bad-token:::good-token:::passport".encode("utf-8")
    ).decode("ascii")

    class FakeLaunchResponse:
        status_code = 302
        headers = {"location": f"moodlemobile://token={encoded}"}

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def get(self, url: str, params=None, timeout: int = 30, allow_redirects: bool = False):
            assert url == "https://uclass.example/admin/tool/mobile/launch.php"
            assert params == {
                "service": "moodle_mobile_app",
                "passport": "sidae-secretary",
                "urlscheme": "moodlemobile",
            }
            return FakeLaunchResponse()

    def _fake_get(url: str, params=None, timeout: int = 30):
        token = str((params or {}).get("wstoken") or "")
        if token == "good-token":
            return _FakeTokenResponse({"userid": 1, "fullname": "Demo"})
        return _FakeTokenResponse(
            {
                "exception": "core\\exception\\moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token",
            }
        )

    monkeypatch.setattr(
        uclass_connector,
        "login_uclass_session",
        lambda **kwargs: FakeSession(),
    )
    monkeypatch.setattr(uclass_connector.requests, "get", _fake_get)

    token = uclass_connector.request_moodle_mobile_launch_token(
        ws_base_url="https://uclass.example/webservice/rest/server.php",
        username="student",
        password="secret",
        service="moodle_mobile_app",
    )

    assert token == "good-token"


def test_resolve_uclass_token_falls_back_to_mobile_launch(tmp_path: Path, monkeypatch) -> None:
    settings = _uclass_settings(tmp_path, uclass_wstoken="")

    monkeypatch.setattr(
        pipeline,
        "request_moodle_ws_token",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("token endpoint blocked")),
    )
    monkeypatch.setattr(
        pipeline,
        "request_moodle_mobile_launch_token",
        lambda **kwargs: "mobile-launch-token",
    )

    token = pipeline._resolve_uclass_token(
        settings,
        prefer_static=False,
        username="student",
        password="secret",
        ws_base_url=settings.uclass_ws_base,
    )

    assert token == "mobile-launch-token"


def test_moodle_ws_client_reuses_site_userid_for_user_scoped_calls(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _fake_get(url: str, params=None, timeout: int = 30):
        payload = dict(params or {})
        calls.append(payload)
        wsfunction = str(payload.get("wsfunction") or "")
        if wsfunction == "core_webservice_get_site_info":
            return _FakeTokenResponse({"userid": 7, "fullname": "Demo"})
        if wsfunction == "core_enrol_get_users_courses":
            return _FakeTokenResponse([{"id": 101, "fullname": "Algorithms"}])
        if wsfunction == "message_popup_get_popup_notifications":
            return _FakeTokenResponse({"notifications": [], "unreadcount": 0})
        raise AssertionError(f"unexpected wsfunction {wsfunction}")

    monkeypatch.setattr(uclass_connector.requests, "get", _fake_get)

    client = uclass_connector.MoodleWSClient(
        "https://uclass.example/webservice/rest/server.php",
        "token",
        request_method="GET",
    )
    client.get_site_info("core_webservice_get_site_info")
    client.get_users_courses("core_enrol_get_users_courses")
    client.get_popup_notifications("message_popup_get_popup_notifications", limit=3)

    assert calls[1]["userid"] == 7
    assert calls[2]["useridto"] == 7
    assert calls[2]["newestfirst"] == 1
    assert calls[2]["limit"] == 3
    assert calls[2]["offset"] == 0


def test_sync_uclass_skips_without_static_token_when_wstoken_missing(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    settings = _uclass_settings(tmp_path, uclass_wstoken="")
    result = pipeline.sync_uclass(settings=settings, db=db)

    assert result["skipped"] is True
    assert result["reason"] == "No active moodle_connections and UCLASS_WSTOKEN missing"


def test_scrape_material_candidates_via_session_parses_course_and_article_files(
    monkeypatch,
) -> None:
    class FakeResponse:
        def __init__(self, text: str, url: str):
            self.text = text
            self.url = url

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def __init__(self):
            self.responses = {
                "https://uclass.example/my/courses.php": FakeResponse(
                    '<a href="https://uclass.example/course/view.php?id=101">Algorithms</a>',
                    "https://uclass.example/my/courses.php",
                ),
                "https://uclass.example/course/view.php?id=101": FakeResponse(
                    """
                    <a href="https://uclass.example/course/view.php?id=101&mode=sections&expandsection=1#section-1">Week 1</a>
                    <a href="https://uclass.example/mod/ubfile/view.php?id=501">Week 1 Slides</a>
                    <a href="https://uclass.example/mod/ubboard/view.php?id=601">Notices</a>
                    """,
                    "https://uclass.example/course/view.php?id=101",
                ),
                "https://uclass.example/course/view.php?id=101&mode=sections&expandsection=1#section-1": FakeResponse(
                    '<a href="https://uclass.example/mod/ubboard/article.php?id=601&amp;bwid=77">Lecture note</a>',
                    "https://uclass.example/course/view.php?id=101&mode=sections&expandsection=1#section-1",
                ),
                "https://uclass.example/mod/ubboard/view.php?id=601": FakeResponse(
                    '<a href="https://uclass.example/mod/ubboard/article.php?id=601&amp;bwid=77">Lecture note</a>',
                    "https://uclass.example/mod/ubboard/view.php?id=601",
                ),
                "https://uclass.example/mod/ubboard/article.php?id=601&bwid=77": FakeResponse(
                    """
                    <h3 class="article-title">Lecture note</h3>
                    <div class="subject-description-date">2026-03-07 09:00:00</div>
                    <div class="article-content"><div class="text_to_html"><p>Read before class.</p></div>
                    <li class="file-item">
                      <a href="https://uclass.example/pluginfile.php/10/mod_ubboard/attachment/77/week1.pdf?forcedownload=1">
                        <div class="file-name">week1.pdf</div>
                      </a>
                    </li>
                    </div><div class="article-buttons"></div>
                    """,
                    "https://uclass.example/mod/ubboard/article.php?id=601&bwid=77",
                ),
            }

        def get(self, url: str, timeout: int = 30, allow_redirects: bool = True):
            if url not in self.responses:
                raise AssertionError(f"unexpected GET {url}")
            return self.responses[url]

    fake_session = FakeSession()
    monkeypatch.setattr(
        uclass_connector,
        "login_uclass_session",
        lambda **kwargs: fake_session,
    )

    items = uclass_connector.scrape_material_candidates_via_session(
        ws_base_url="https://uclass.example/webservice/rest/server.php",
        username="student",
        password="secret",
    )

    names = {item.filename for item in items}
    assert "Week 1 Slides" in names
    assert "week1.pdf" in names
    attachment = [item for item in items if item.filename == "week1.pdf"][0]
    assert attachment.metadata["article_title"] == "Lecture note"
    assert attachment.metadata["article_body"] == "Read before class."
    assert attachment.date_folder == "2026-03-07"


def test_scrape_material_candidates_via_session_ignores_invalid_board_ids(
    monkeypatch,
) -> None:
    class FakeResponse:
        def __init__(self, text: str, url: str, status_code: int = 200):
            self.text = text
            self.url = url
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

    class FakeSession:
        def __init__(self):
            self.responses = {
                "https://uclass.example/my/courses.php": FakeResponse(
                    '<a href="https://uclass.example/course/view.php?id=101">Algorithms</a>',
                    "https://uclass.example/my/courses.php",
                ),
                "https://uclass.example/course/view.php?id=101": FakeResponse(
                    """
                    <a href="https://uclass.example/mod/ubboard/view.php?id=0">Broken board</a>
                    <a href="https://uclass.example/mod/ubboard/view.php?id=601">Week 1 board</a>
                    """,
                    "https://uclass.example/course/view.php?id=101",
                ),
                "https://uclass.example/mod/ubboard/view.php?id=601": FakeResponse(
                    '<a href="https://uclass.example/mod/ubboard/article.php?id=601&amp;bwid=77">Lecture note</a>',
                    "https://uclass.example/mod/ubboard/view.php?id=601",
                ),
                "https://uclass.example/mod/ubboard/article.php?id=601&bwid=77": FakeResponse(
                    """
                    <h3 class="article-title">Lecture note</h3>
                    <div class="subject-description-date">2026-03-07 09:00:00</div>
                    <div class="article-content"><div class="text_to_html"><p>Read before class.</p></div>
                    <li class="file-item">
                      <a href="https://uclass.example/pluginfile.php/10/mod_ubboard/attachment/77/week1.pdf?forcedownload=1">
                        <div class="file-name">week1.pdf</div>
                      </a>
                    </li>
                    </div><div class="article-buttons"></div>
                    """,
                    "https://uclass.example/mod/ubboard/article.php?id=601&bwid=77",
                ),
            }

        def get(self, url: str, timeout: int = 30, allow_redirects: bool = True):
            if url == "https://uclass.example/mod/ubboard/view.php?id=0":
                return FakeResponse("", url, status_code=404)
            if url not in self.responses:
                raise AssertionError(f"unexpected GET {url}")
            return self.responses[url]

    fake_session = FakeSession()
    monkeypatch.setattr(
        uclass_connector,
        "login_uclass_session",
        lambda **kwargs: fake_session,
    )

    items = uclass_connector.scrape_material_candidates_via_session(
        ws_base_url="https://uclass.example/webservice/rest/server.php",
        username="student",
        password="secret",
    )

    assert any(item.filename == "week1.pdf" for item in items)


def test_download_material_uses_session_download_when_available(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = _uclass_settings(tmp_path, uclass_wstoken="")
    settings._uclass_html_session = object()

    monkeypatch.setattr(
        pipeline,
        "_download_material_response",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("token download should not run")),
    )
    monkeypatch.setattr(
        pipeline,
        "_download_material_response_via_session",
        lambda **kwargs: (
            b"%PDF-1.4 fake",
            {"content-type": "application/pdf"},
            "https://uclass.example/pluginfile.php/10/week1.pdf?forcedownload=1",
        ),
    )

    target = tmp_path / "icloud" / "SidaeSecretary" / "materials" / "algo" / "2026-03-07" / "week1"
    local_path, digest, downloaded, metadata = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:test-session",
        url="https://uclass.example/mod/ubfile/view.php?id=501",
        target=target,
        owner_id=0,
    )

    assert downloaded is True
    assert Path(local_path).name == "week1.pdf"
    assert digest
    assert metadata["content_type"] == "application/pdf"


def test_download_material_rewrites_webservice_pluginfile_for_session_download(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    settings = _uclass_settings(tmp_path, uclass_wstoken="")
    settings._uclass_html_session = object()
    seen: dict[str, str] = {}

    monkeypatch.setattr(
        pipeline,
        "_download_material_response",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("token download should not run")),
    )

    def _fake_session_download(**kwargs):
        seen["url"] = kwargs["url"]
        return (
            b"%PDF-1.4 fake",
            {"content-type": "application/pdf"},
            kwargs["url"],
        )

    monkeypatch.setattr(
        pipeline,
        "_download_material_response_via_session",
        _fake_session_download,
    )

    target = tmp_path / "icloud" / "SidaeSecretary" / "materials" / "algo" / "2026-03-07" / "week1"
    local_path, digest, downloaded, metadata = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:test-session-rewrite",
        url="https://uclass.example/webservice/pluginfile.php/10/week1.pdf?forcedownload=1",
        target=target,
        owner_id=0,
    )

    assert seen["url"] == "https://uclass.example/pluginfile.php/10/week1.pdf?forcedownload=1"
    assert downloaded is True
    assert digest
    assert Path(local_path).name == "week1.pdf"
    assert metadata["content_type"] == "application/pdf"


def test_sync_uclass_fails_closed_when_target_token_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_sync_targets",
        lambda settings, db: [
            {
                "user_id": 7,
                "chat_id": "12345",
                "school_slug": "uos_online_class",
                "display_name": "서울시립대학교 온라인강의실",
                "ws_base_url": "https://uclass.example/webservice/rest/server.php",
                "token": "",
                "token_error": "UClass token expired or unavailable; reconnect required",
                "connection_id": 1,
            }
        ],
    )

    settings = _uclass_settings(tmp_path, uclass_wstoken="")
    result = pipeline.sync_uclass(settings=settings, db=db)
    state = db.get_sync_state("sync_uclass", user_id=7)

    assert result["ok"] is False
    assert result["error"] == "UClass token expired or unavailable; reconnect required"
    assert result["failed_targets"][0]["user_id"] == 7
    assert state is not None
    assert "reconnect required" in (state.last_cursor_json or "")
