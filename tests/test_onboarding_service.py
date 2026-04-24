from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database, now_utc_iso
from sidae_secretary.onboarding import MOODLE_ONBOARDING_SESSION_KIND
from sidae_secretary.onboarding_school_connect import (
    MoodleConnectFormData,
    finalize_school_account_connection,
)
from sidae_secretary.onboarding_service import (
    AUTH_MAX_FAILED_PER_SESSION,
    OnboardingApplicationService,
)
from sidae_secretary.school_support import school_support_summary
from sidae_secretary.secret_store import StoredSecretRef


class FakeSecretStore:
    def store_secret(self, *, key: str, secret: str) -> StoredSecretRef:
        return StoredSecretRef(kind="inline", ref=f"{key}::{secret}")


def _build_service(
    *,
    db: Database,
    settings: object,
    store: FakeSecretStore,
    telegram_client_factory,
    exchange_moodle_credentials_fn,
) -> OnboardingApplicationService:
    return OnboardingApplicationService(
        db=db,
        store=store,
        settings=settings,
        telegram_client_factory=telegram_client_factory,
        exchange_moodle_credentials_fn=exchange_moodle_credentials_fn,
        finalize_school_account_connection_fn=finalize_school_account_connection,
        school_support_summary_fn=school_support_summary,
        request_method="GET",
        site_info_wsfunction="core_webservice_get_site_info",
        token_service="moodle_mobile_app",
        browser_channel="",
        browser_executable_path=None,
        browser_headless=True,
        build_browser_profile_dir=lambda **kwargs: Path("/tmp/unused-profile"),
        portal_login_browser_session=lambda **kwargs: {"ok": True},
        prime_post_connect_portal_sync=lambda **kwargs: {"ok": True, "status": "success"},
        sanitize_browser_result=lambda payload: payload,
        now_utc_iso_fn=now_utc_iso,
        uos_portal_provider="uos_portal",
        uos_portal_school_slug="uos_portal",
        uos_portal_login_url="https://portal.uos.ac.kr/p/STUD/",
    )


def test_onboarding_service_completes_school_account_connect_and_sends_message(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=MOODLE_ONBOARDING_SESSION_KIND,
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

    settings = SimpleNamespace(
        telegram_bot_token="token",
        timezone="Asia/Seoul",
    )
    service = _build_service(
        db=db,
        settings=settings,
        store=FakeSecretStore(),
        telegram_client_factory=FakeTelegram,
        exchange_moodle_credentials_fn=lambda **kwargs: {
            "school_slug": "ys_learnus_org",
            "display_name": "LearnUs YONSEI",
            "ws_base_url": "https://ys.learnus.org/webservice/rest/server.php",
            "username": "student-demo-001",
            "token": "issued-token",
            "site_info": {"sitename": "LearnUs YONSEI", "userid": 1},
            "verified_at": "2026-03-10T03:00:00+00:00",
        },
    )

    result = service.complete_school_account_connect(
        form=MoodleConnectFormData(
            token=str(session["token"]),
            school_name="연세대학교",
            username="student-demo-001",
            password="secret",
        ),
        remote_addr="127.0.0.1",
        session_kind=MOODLE_ONBOARDING_SESSION_KIND,
        allowed_school_slugs=None,
    )

    assert result.status == "success"
    assert result.success_display_name == "연세대학교 LearnUs"
    connections = db.list_moodle_connections(chat_id="77777")
    assert len(connections) == 1
    assert connections[0]["school_slug"] == "yonsei_learnus"
    assert connections[0]["secret_kind"] == "inline"
    assert connections[0]["secret_ref"].endswith("::issued-token")
    attempts = db.list_auth_attempts(session_kind=MOODLE_ONBOARDING_SESSION_KIND, limit=5)
    assert attempts[0]["status"] == "success"
    assert sent_messages == [
        (
            "77777",
            "[Sidae] 학교 계정 연결 완료\n\n- 학교: 연세대학교 LearnUs\n- 포털: 연세포털서비스\n- 포털 로그인: https://portal.yonsei.ac.kr/main/index.jsp\n- ID: student-demo-001\n- 온라인강의실 접근 token을 이 사용자용 보안 저장소에 저장했습니다.\n- 이 학교는 같은 계정으로 포털을 사용합니다.\n- 제약: 연세포털 시간표 페이지와 수강 데이터 엔드포인트는 아직 학교별 자동화가 필요합니다.\n- 현재 사용자-facing 공식 지원 학교는 서울시립대학교입니다.",
        )
    ]


def test_onboarding_service_blocks_rate_limited_school_account_connect(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    session = db.create_onboarding_session(
        session_kind=MOODLE_ONBOARDING_SESSION_KIND,
        chat_id="77777",
        expires_at="2099-03-10T12:30:00+09:00",
        metadata_json={"source": "test"},
    )
    for _ in range(AUTH_MAX_FAILED_PER_SESSION):
        db.record_auth_attempt(
            chat_id="77777",
            onboarding_session_id=int(session["id"]),
            session_kind=MOODLE_ONBOARDING_SESSION_KIND,
            remote_addr="127.0.0.1",
            username="student-demo-001",
            status="failed",
        )

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            raise AssertionError("telegram should not be called when blocked")

    service = _build_service(
        db=db,
        settings=SimpleNamespace(telegram_bot_token="token", timezone="Asia/Seoul"),
        store=FakeSecretStore(),
        telegram_client_factory=FakeTelegram,
        exchange_moodle_credentials_fn=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("credential exchange should not run when blocked")
        ),
    )

    result = service.complete_school_account_connect(
        form=MoodleConnectFormData(
            token=str(session["token"]),
            school_name="서울시립대학교 온라인강의실",
            username="student-demo-001",
            password="secret",
        ),
        remote_addr="127.0.0.1",
        session_kind=MOODLE_ONBOARDING_SESSION_KIND,
        allowed_school_slugs=None,
    )

    assert result.status == "blocked"
    assert result.http_status == 429
    assert db.count_auth_attempts(
        onboarding_session_id=int(session["id"]),
        session_kind=MOODLE_ONBOARDING_SESSION_KIND,
        status="blocked",
    ) == 1
