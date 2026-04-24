from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
from pathlib import Path
import threading
from typing import Any, Mapping

from sidae_secretary.connectors.telegram import TelegramBotClient
from sidae_secretary.db import now_utc_iso


def _normalize_chat_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                output.append(text)
        return output
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def canonical_briefing_delivery_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_key": str(payload.get("item_key") or "").strip(),
        "slot": str(payload.get("slot") or "").strip().lower(),
        "send_at_local": str(payload.get("send_at_local") or "").strip(),
        "message": str(payload.get("message") or ""),
        "chat_ids": _normalize_chat_ids(payload.get("chat_ids")),
    }


def briefing_message_hash(message: str) -> str:
    return sha256(str(message).encode("utf-8")).hexdigest()


def sign_briefing_delivery_payload(
    payload: Mapping[str, Any],
    *,
    shared_secret: str,
) -> str:
    canonical = canonical_briefing_delivery_payload(payload)
    blob = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        shared_secret.encode("utf-8"),
        blob,
        digestmod=sha256,
    ).hexdigest()


def build_signed_briefing_delivery_request(
    *,
    endpoint: str,
    shared_secret: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    canonical = canonical_briefing_delivery_payload(payload)
    signature = sign_briefing_delivery_payload(
        canonical,
        shared_secret=shared_secret,
    )
    return {
        "url": str(endpoint).strip(),
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": {
            **canonical,
            "algorithm": "hmac-sha256",
            "signature": signature,
        },
    }


@dataclass
class BriefingRelayVerification:
    ok: bool
    http_status: int
    error: str | None
    canonical_payload: dict[str, Any]


def verify_signed_briefing_delivery_payload(
    payload: Mapping[str, Any],
    *,
    shared_secret: str,
) -> BriefingRelayVerification:
    canonical = canonical_briefing_delivery_payload(payload)
    if not canonical["item_key"]:
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="missing_item_key",
            canonical_payload=canonical,
        )
    if canonical["slot"] not in {"morning", "evening"}:
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="invalid_slot",
            canonical_payload=canonical,
        )
    if not canonical["send_at_local"]:
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="missing_send_at_local",
            canonical_payload=canonical,
        )
    if not canonical["message"].strip():
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="missing_message",
            canonical_payload=canonical,
        )
    if not canonical["chat_ids"]:
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="missing_chat_ids",
            canonical_payload=canonical,
        )
    algorithm = str(payload.get("algorithm") or "").strip().lower()
    if algorithm != "hmac-sha256":
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="unsupported_algorithm",
            canonical_payload=canonical,
        )
    signature = str(payload.get("signature") or "").strip().lower()
    if not signature:
        return BriefingRelayVerification(
            ok=False,
            http_status=400,
            error="missing_signature",
            canonical_payload=canonical,
        )
    expected = sign_briefing_delivery_payload(
        canonical,
        shared_secret=shared_secret,
    )
    if not hmac.compare_digest(signature, expected):
        return BriefingRelayVerification(
            ok=False,
            http_status=403,
            error="invalid_signature",
            canonical_payload=canonical,
        )
    return BriefingRelayVerification(
        ok=True,
        http_status=200,
        error=None,
        canonical_payload=canonical,
    )


class BriefingRelayStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": {}}
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"items": {}}
        if not isinstance(parsed, dict):
            return {"items": {}}
        items = parsed.get("items")
        if not isinstance(items, dict):
            parsed["items"] = {}
        return parsed

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, item_key: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._read_unlocked()
            item = payload.get("items", {}).get(str(item_key))
            return item if isinstance(item, dict) else None

    def merge_delivery(
        self,
        *,
        item_key: str,
        slot: str,
        send_at_local: str,
        signature: str,
        message: str,
        sent_to: list[str],
    ) -> dict[str, Any]:
        with self._lock:
            payload = self._read_unlocked()
            items = payload.setdefault("items", {})
            existing = items.get(item_key)
            now = now_utc_iso()
            if not isinstance(existing, dict):
                existing = {
                    "item_key": item_key,
                    "slot": slot,
                    "send_at_local": send_at_local,
                    "signature": signature,
                    "message_hash": briefing_message_hash(message),
                    "sent_to": [],
                    "first_sent_at": now,
                }
            merged_sent_to = sorted(
                {
                    str(chat).strip()
                    for chat in list(existing.get("sent_to") or []) + list(sent_to)
                    if str(chat).strip()
                }
            )
            existing.update(
                {
                    "item_key": item_key,
                    "slot": slot,
                    "send_at_local": send_at_local,
                    "signature": signature,
                    "message_hash": briefing_message_hash(message),
                    "sent_to": merged_sent_to,
                    "last_sent_at": now,
                }
            )
            items[item_key] = existing
            self._write_unlocked(payload)
            return dict(existing)


def deliver_signed_briefing_request(
    *,
    payload: Mapping[str, Any],
    shared_secret: str,
    bot_token: str,
    state_store: BriefingRelayStateStore,
    allowed_chat_ids: list[str] | None = None,
    client_factory: type[TelegramBotClient] = TelegramBotClient,
) -> dict[str, Any]:
    verification = verify_signed_briefing_delivery_payload(
        payload,
        shared_secret=shared_secret,
    )
    if not verification.ok:
        return {
            "ok": False,
            "error": verification.error,
            "_http_status": verification.http_status,
        }

    canonical = verification.canonical_payload
    signature = str(payload.get("signature") or "").strip().lower()
    existing = state_store.get(canonical["item_key"])
    expected_hash = briefing_message_hash(canonical["message"])
    if existing:
        existing_signature = str(existing.get("signature") or "").strip().lower()
        existing_hash = str(existing.get("message_hash") or "").strip().lower()
        if existing_signature and existing_signature != signature:
            return {
                "ok": False,
                "error": "item_key_conflict",
                "_http_status": 409,
            }
        if existing_hash and existing_hash != expected_hash:
            return {
                "ok": False,
                "error": "item_key_conflict",
                "_http_status": 409,
            }

    requested_chat_ids = canonical["chat_ids"]
    allowed = _normalize_chat_ids(allowed_chat_ids)
    if allowed:
        allowed_set = set(allowed)
        requested_chat_ids = [chat for chat in requested_chat_ids if chat in allowed_set]
    if not requested_chat_ids:
        return {
            "ok": False,
            "error": "no_allowed_chat_ids",
            "_http_status": 403,
        }

    already_sent = {
        str(chat).strip()
        for chat in list((existing or {}).get("sent_to") or [])
        if str(chat).strip()
    }
    pending_chat_ids = [chat for chat in requested_chat_ids if chat not in already_sent]
    if not pending_chat_ids:
        return {
            "ok": True,
            "duplicate": True,
            "item_key": canonical["item_key"],
            "sent_to": sorted(already_sent),
            "_http_status": 200,
        }

    client = client_factory(bot_token)
    sent_to: list[str] = []
    errors: list[dict[str, str]] = []
    for chat_id in pending_chat_ids:
        try:
            if client.send_message(chat_id=chat_id, text=canonical["message"]):
                sent_to.append(chat_id)
        except Exception as exc:
            errors.append({"chat_id": chat_id, "error": str(exc)})
    if sent_to:
        stored = state_store.merge_delivery(
            item_key=canonical["item_key"],
            slot=canonical["slot"],
            send_at_local=canonical["send_at_local"],
            signature=signature,
            message=canonical["message"],
            sent_to=sent_to,
        )
        delivered_to = sorted(
            {
                str(chat).strip()
                for chat in list(stored.get("sent_to") or [])
                if str(chat).strip()
            }
        )
        return {
            "ok": True,
            "duplicate": False,
            "item_key": canonical["item_key"],
            "sent_to": delivered_to,
            "sent_now_to": sent_to,
            "remaining_chat_ids": [
                chat for chat in requested_chat_ids if chat not in set(delivered_to)
            ],
            "errors": errors,
            "_http_status": 200 if not errors else 207,
        }
    return {
        "ok": False,
        "error": "telegram_send_failed",
        "errors": errors,
        "_http_status": 502,
    }
