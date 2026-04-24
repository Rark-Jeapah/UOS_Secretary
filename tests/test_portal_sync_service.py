from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.portal_sync_service import prime_post_connect_portal_sync


class _FakePortalSyncAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def prime_uos_portal_timetable_for_user(self, settings, db, **kwargs):
        self.calls.append(("prime", dict(kwargs)))
        return {"ok": True, "status": "success", "source": "prime"}

    def record_uos_portal_timetable_fetch_for_user(self, settings, db, **kwargs):
        self.calls.append(("record", dict(kwargs)))
        return {"ok": True, "status": "success", "source": "record"}


def test_prime_post_connect_portal_sync_uses_prefetched_result_when_available(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    api = _FakePortalSyncAPI()

    result = prime_post_connect_portal_sync(
        settings=SimpleNamespace(timezone="Asia/Seoul"),
        db=db,
        chat_id="77777",
        user_id=1,
        fetched={"ok": True, "events": [{"external_id": "portal:1"}]},
        portal_sync_api=api,
    )

    assert result["source"] == "record"
    assert api.calls == [
        (
            "record",
            {
                "fetched": {"ok": True, "events": [{"external_id": "portal:1"}]},
                "chat_id": "77777",
                "user_id": 1,
            },
        )
    ]


def test_onboarding_module_has_no_direct_pipeline_import() -> None:
    onboarding_path = Path(__file__).resolve().parents[1] / "src/sidae_secretary/onboarding.py"
    tree = ast.parse(onboarding_path.read_text())

    direct_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "sidae_secretary.jobs.pipeline":
                direct_imports.append(node.module)
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "sidae_secretary.jobs.pipeline":
                    direct_imports.append(item.name)

    assert direct_imports == []
