from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.secret_store import SecretStoreError
from sidae_secretary.telegram_setup_state import (
    build_telegram_setup_state,
    chat_lms_connection_snapshot,
)


def test_chat_lms_connection_snapshot_separates_portal_sessions(tmp_path: Path) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="file",
        secret_ref="uclass.secret",
        status="active",
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_online_class",
        provider="moodle",
        display_name="서울시립대학교 온라인강의실 브라우저",
        login_url="https://uclass.uos.ac.kr/",
        profile_dir=tmp_path / "profiles" / "uclass",
        metadata_json={"browser_result": {"current_url": "https://uclass.uos.ac.kr/my/"}},
        user_id=int(user["id"]),
    )

    snapshot = chat_lms_connection_snapshot(db, "12345")

    assert snapshot.owner_id == int(user["id"])
    assert "서울시립대학교 온라인강의실 (student)" in snapshot.uclass_labels
    assert "서울시립대학교 포털/대학행정 (포털 세션)" in snapshot.portal_labels
    assert len(snapshot.portal_sessions) == 1
    assert len(snapshot.browser_sessions) == 2


def test_build_telegram_setup_state_hides_legacy_portal_session_in_official_api_mode(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="file",
        secret_ref="uclass.secret",
        status="active",
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )
    db.update_sync_state(
        "sync_uos_portal_timetable",
        last_run_at="2026-03-13T14:40:00+09:00",
        last_cursor_json={
            "_sync_dashboard": {
                "status": "success",
                "last_success_at": "2026-03-13T14:40:00+09:00",
            }
        },
        user_id=int(user["id"]),
    )

    state = build_telegram_setup_state(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
            uos_openapi_timetable_url="https://wise.uos.ac.kr/COM/ApiTimeTable/list.do",
            uos_openapi_timetable_api_key="test-key",
        ),
        db=db,
        allowed=True,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert state.official_catalog_mode is True
    assert state.portal_labels == ()
    assert state.portal_level == "OK"
    assert state.show_official_api_connection is True
    assert state.portal_notes == ()


def test_build_telegram_setup_state_warns_when_portal_timetable_has_not_synced_yet(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    profile_dir = tmp_path / "profiles" / "uos"
    profile_dir.mkdir(parents=True)
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir=profile_dir,
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        last_verified_at="2026-03-10T09:03:00+09:00",
        user_id=int(user["id"]),
    )

    state = build_telegram_setup_state(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        allowed=True,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert state.portal_level == "WARN"
    assert state.portal_notes == ("- 시간표 연결은 저장됐지만 아직 첫 확인 기록이 없습니다.",)


def test_build_telegram_setup_state_warns_when_secure_storage_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import sidae_secretary.telegram_setup_state as telegram_setup_state

    db = Database(tmp_path / "sidae.db")
    db.init()
    user = db.ensure_user_for_chat(chat_id="12345", timezone_name="Asia/Seoul")
    db.upsert_moodle_connection(
        chat_id="12345",
        school_slug="uos_online_class",
        display_name="서울시립대학교 온라인강의실",
        ws_base_url="https://uclass.uos.ac.kr/webservice/rest/server.php",
        username="student",
        secret_kind="keychain",
        secret_ref="telegram:12345:moodle:uos_online_class",
        status="active",
        user_id=int(user["id"]),
    )
    db.upsert_lms_browser_session(
        chat_id="12345",
        school_slug="uos_portal",
        provider="uos_portal",
        display_name="서울시립대학교 포털/대학행정",
        login_url="https://portal.uos.ac.kr/p/STUD/",
        profile_dir="",
        secret_kind="keychain",
        secret_ref="telegram:12345:portal:uos_portal",
        metadata_json={"browser_result": {"current_url": "https://wise.uos.ac.kr/index.do"}},
        user_id=int(user["id"]),
    )

    class MissingSecretStore:
        def read_secret(self, *, ref):
            raise SecretStoreError(
                "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
            )

    monkeypatch.setattr(
        telegram_setup_state,
        "default_secret_store",
        lambda settings=None: MissingSecretStore(),
    )

    state = build_telegram_setup_state(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            uclass_username="",
            uclass_password="",
            llm_provider="",
            llm_local_endpoint="",
        ),
        db=db,
        allowed=True,
        chat_id="12345",
        user_id=int(user["id"]),
    )

    assert state.uclass_account_level == "WARN"
    assert state.portal_level == "WARN"
    assert "- 저장된 온라인강의실 연결을 다시 확인해야 합니다. `/connect`로 다시 연결해 주세요." in state.uclass_notes
    assert "- 저장된 시간표 연결을 다시 확인해야 합니다. `/connect`로 다시 연결해 주세요." in state.portal_notes
