from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def _digest_settings(include_identity: bool) -> SimpleNamespace:
    return SimpleNamespace(
        digest_enabled=True,
        digest_channel="telegram",
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_allowed_chat_ids=["12345"],
        digest_time_local="00:00",
        timezone="Asia/Seoul",
        digest_task_lookahead_days=3,
        include_identity=include_identity,
    )


def test_digest_proceeds_when_include_identity_is_false(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    result = pipeline.send_daily_digest(settings=_digest_settings(False), db=db)

    assert result["sent_to"] == ["12345"]
    assert len(sent_messages) == 1


def test_digest_blocks_with_warning_gate_when_include_identity_is_true_without_ack(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)
    result = pipeline.send_daily_digest(settings=_digest_settings(True), db=db)

    assert result["blocked"] is True
    assert result["error"] == "identity_ack_required"
    assert result["warning_gate"]["gate"] == "identity_ack"
    assert result["warning_gate"]["destination"] == "telegram"
    assert result["warning_gate"]["ack_required"] is True
    assert len(sent_messages) == 0


def test_digest_requires_non_expired_ack_when_include_identity_is_true(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    monkeypatch.setattr(pipeline, "TelegramBotClient", FakeTelegram)

    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
        microsecond=0
    )
    db.record_identity_ack(token="expired", expires_at=expired_at.isoformat())
    blocked = pipeline.send_daily_digest(settings=_digest_settings(True), db=db)

    assert blocked["blocked"] is True
    assert blocked["error"] == "identity_ack_required"

    valid_until = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        microsecond=0
    )
    db.record_identity_ack(token="active", expires_at=valid_until.isoformat())
    allowed = pipeline.send_daily_digest(settings=_digest_settings(True), db=db)

    assert allowed["sent_to"] == ["12345"]
    assert len(sent_messages) == 1


def test_llm_summary_step_blocks_when_identity_ack_is_missing(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    db.upsert_notification(
        external_id="uclass:notif:1",
        source="uclass",
        created_at="2026-03-05T00:00:00+00:00",
        title="Notice",
        body="Body",
        url=None,
        metadata_json={},
    )

    class _UnexpectedLLMClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLM client must not be constructed when gate is blocking")

    monkeypatch.setattr(pipeline, "LLMClient", _UnexpectedLLMClient)
    settings = SimpleNamespace(
        llm_enabled=True,
        llm_provider="local",
        llm_model="gemma4",
        llm_timeout_sec=30,
        llm_local_endpoint="http://127.0.0.1:11434/api/chat",
        include_identity=True,
    )

    result = pipeline.sync_llm_summaries(settings=settings, db=db)

    assert result["blocked"] is True
    assert result["error"] == "identity_ack_required"
    assert result["warning_gate"]["destination"] == "llm"
