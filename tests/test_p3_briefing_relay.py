from __future__ import annotations

from sidae_secretary.briefing_relay import (
    BriefingRelayStateStore,
    build_signed_briefing_delivery_request,
    deliver_signed_briefing_request,
)


def test_deliver_signed_briefing_request_dedupes_item_key(
    tmp_path,
) -> None:
    sent_messages: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            sent_messages.append((str(chat_id), text))
            return True

    payload = build_signed_briefing_delivery_request(
        endpoint="https://relay.example.com/briefing",
        shared_secret="relay-secret",
        payload={
            "item_key": "2026-03-08-evening",
            "slot": "evening",
            "send_at_local": "2026-03-08T21:00:00+09:00",
            "message": "[Sidae] 저녁 브리핑",
            "chat_ids": ["12345"],
        },
    )["body"]
    store = BriefingRelayStateStore(tmp_path / "relay_state.json")

    first = deliver_signed_briefing_request(
        payload=payload,
        shared_secret="relay-secret",
        bot_token="token",
        state_store=store,
        allowed_chat_ids=["12345"],
        client_factory=FakeTelegram,
    )
    second = deliver_signed_briefing_request(
        payload=payload,
        shared_secret="relay-secret",
        bot_token="token",
        state_store=store,
        allowed_chat_ids=["12345"],
        client_factory=FakeTelegram,
    )

    assert first["ok"] is True
    assert first["duplicate"] is False
    assert first["sent_to"] == ["12345"]
    assert second["ok"] is True
    assert second["duplicate"] is True
    assert len(sent_messages) == 1


def test_deliver_signed_briefing_request_rejects_item_key_conflict(
    tmp_path: Path,
) -> None:
    store = BriefingRelayStateStore(tmp_path / "relay_state.json")
    first_payload = build_signed_briefing_delivery_request(
        endpoint="https://relay.example.com/briefing",
        shared_secret="relay-secret",
        payload={
            "item_key": "2026-03-08-evening",
            "slot": "evening",
            "send_at_local": "2026-03-08T21:00:00+09:00",
            "message": "[Sidae] 저녁 브리핑",
            "chat_ids": ["12345"],
        },
    )["body"]
    second_payload = build_signed_briefing_delivery_request(
        endpoint="https://relay.example.com/briefing",
        shared_secret="relay-secret",
        payload={
            "item_key": "2026-03-08-evening",
            "slot": "evening",
            "send_at_local": "2026-03-08T21:00:00+09:00",
            "message": "[Sidae] 저녁 브리핑 수정본",
            "chat_ids": ["12345"],
        },
    )["body"]

    class FakeTelegram:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            self.bot_token = bot_token

        def send_message(self, chat_id: str | int, text: str) -> bool:
            return True

    first = deliver_signed_briefing_request(
        payload=first_payload,
        shared_secret="relay-secret",
        bot_token="token",
        state_store=store,
        allowed_chat_ids=["12345"],
        client_factory=FakeTelegram,
    )
    second = deliver_signed_briefing_request(
        payload=second_payload,
        shared_secret="relay-secret",
        bot_token="token",
        state_store=store,
        allowed_chat_ids=["12345"],
        client_factory=FakeTelegram,
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "item_key_conflict"
