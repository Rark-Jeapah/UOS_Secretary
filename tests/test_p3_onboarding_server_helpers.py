from __future__ import annotations

from sidae_secretary.onboarding_school_connect import (
    apply_directory_entry_overrides,
    build_school_account_completion_message,
    parse_moodle_connect_form,
    validate_moodle_connect_form,
)


def test_parse_moodle_connect_form_and_validate_required_fields() -> None:
    form = parse_moodle_connect_form(
        "token=abc123&school_name=&username=student-demo-001&password=".encode("utf-8")
    )

    assert form.token == "abc123"
    assert form.school_name == ""
    assert form.username == "student-demo-001"
    assert form.password == ""
    assert validate_moodle_connect_form(form) == "missing_fields"


def test_apply_directory_entry_overrides_prefers_registered_school_metadata() -> None:
    resolved = {
        "school_slug": "learnus_yonsei",
        "display_name": "LearnUs YONSEI",
        "ws_base_url": "https://custom.example.com/webservice/rest/server.php",
    }
    directory_entry = {
        "school_slug": "yonsei_learnus",
        "display_name": "연세대학교 LearnUs",
        "ws_base_url": "https://ys.learnus.org/webservice/rest/server.php",
    }

    updated = apply_directory_entry_overrides(resolved, directory_entry)

    assert updated["school_slug"] == "yonsei_learnus"
    assert updated["display_name"] == "연세대학교 LearnUs"
    assert updated["ws_base_url"] == "https://ys.learnus.org/webservice/rest/server.php"


def test_build_school_account_completion_message_for_shared_account_school() -> None:
    message = build_school_account_completion_message(
        connection={
            "school_slug": "yonsei_learnus",
            "display_name": "연세대학교 LearnUs",
            "username": "student-demo-001",
            "login_secret_kind": None,
            "login_secret_ref": None,
        },
        directory_entry={
            "school_slug": "yonsei_learnus",
            "display_name": "연세대학교 LearnUs",
        },
        portal_info={
            "display_name": "연세포털서비스",
            "login_url": "https://portal.yonsei.ac.kr/main/index.jsp",
            "constraints": "연세포털 시간표 페이지와 수강 데이터 엔드포인트는 아직 학교별 자동화가 필요합니다.",
            "timetable_support": "planned",
        },
        portal_browser_session=None,
        portal_login_error="",
        portal_prime_result=None,
        school_support_summary_fn=lambda entry: {"official_user_support": False},
    )

    assert message == (
        "[Sidae] 학교 계정 연결 완료\n\n"
        "- 학교: 연세대학교 LearnUs\n"
        "- 포털: 연세포털서비스\n"
        "- 포털 로그인: https://portal.yonsei.ac.kr/main/index.jsp\n"
        "- ID: student-demo-001\n"
        "- 온라인강의실 접근 token을 이 사용자용 보안 저장소에 저장했습니다.\n"
        "- 이 학교는 같은 계정으로 포털을 사용합니다.\n"
        "- 제약: 연세포털 시간표 페이지와 수강 데이터 엔드포인트는 아직 학교별 자동화가 필요합니다.\n"
        "- 현재 사용자-facing 공식 지원 학교는 서울시립대학교입니다."
    )
