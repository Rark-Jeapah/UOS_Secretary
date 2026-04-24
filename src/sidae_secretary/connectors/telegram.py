from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dt_parser
import requests


DATE_TOKEN_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
TIME_TOKEN_RE = re.compile(r"\b\d{1,2}:\d{2}(?:\s*[APMapm]{2})?\b")
TASK_HINT_RE = re.compile(r"\b(due|by)\b", re.IGNORECASE)
ONBOARDING_COMMANDS = {"start", "help", "setup", "connect_moodle"}


@dataclass
class TelegramInboxItem:
    external_id: str
    received_at: str
    title: str
    body: str
    item_type: str
    draft: dict[str, Any]
    metadata: dict[str, Any]

def parse_command_message(text: str) -> dict[str, Any] | None:
    body = str(text or "").strip()
    if not body.startswith("/"):
        return None
    parts = body.split()
    if not parts:
        return None
    command = parts[0].split("@", 1)[0].strip().lower()

    if command == "/status":
        return {"command": "status", "ok": True}
    if command == "/today":
        return {"command": "today", "ok": True}
    if command == "/tomorrow":
        return {"command": "tomorrow", "ok": True}
    if command in {"/weather", "/todayweather"}:
        return {"command": "weather", "ok": True}
    if command in {"/region", "/setregion"}:
        query = body[len(parts[0]) :].strip()
        payload = {"command": "region", "ok": True}
        if query:
            payload["query"] = query
        return payload
    if command in {"/todaysummary", "/todaybrief"}:
        return {"command": "today_summary", "ok": True}
    if command in {"/tomorrowsummary", "/tomorrowbrief"}:
        return {"command": "tomorrow_summary", "ok": True}
    if command in {"/notice_general", "/generalnotice"}:
        return {"command": "notice_general", "ok": True}
    if command in {"/notice_academic", "/academicnotice"}:
        return {"command": "notice_academic", "ok": True}
    if command in {"/notice_uclass", "/uclassnotice", "/uclassnotices"}:
        return {"command": "notice_uclass", "ok": True}
    if command == "/start":
        return {"command": "start", "ok": True}
    if command == "/help":
        return {"command": "help", "ok": True}
    if command == "/setup":
        return {"command": "setup", "ok": True}
    if command in {"/connect", "/connect_moodle"}:
        school_query = body[len(parts[0]) :].strip()
        payload = {"command": "connect_moodle", "ok": True}
        if school_query:
            payload["school_query"] = school_query
        return payload
    if command == "/inbox":
        return {"command": "inbox", "ok": True}
    if command == "/apply":
        if len(parts) < 2:
            return {"command": "apply", "ok": False, "error": "missing id or 'all'"}
        target = parts[1].strip()
        if target.lower() == "all":
            return {"command": "apply", "ok": True, "scope": "all"}
        return {"command": "apply", "ok": True, "scope": "id", "id": target}
    if command == "/done":
        if len(parts) < 3:
            return {"command": "done", "ok": False, "error": "expected '/done task <id>'"}
        target = parts[1].strip().lower()
        if target != "task":
            return {"command": "done", "ok": False, "error": "target must be task"}
        item_id = " ".join(parts[2:]).strip()
        if not item_id:
            return {"command": "done", "ok": False, "error": "missing id"}
        return {"command": "done", "ok": True, "target": target, "id": item_id}
    if command == "/plan":
        instruction = body[len(parts[0]) :].strip()
        if not instruction:
            return {
                "command": "plan",
                "ok": False,
                "error": "expected '/plan <instruction>'",
            }
        return {"command": "plan", "ok": True, "instruction": instruction}
    if command in {"/bot", "/assistant", "/asis"}:
        request_text = body[len(parts[0]) :].strip()
        if not request_text:
            return {
                "command": "assistant",
                "ok": False,
                "error": "expected '/bot <request>'",
            }
        return {
            "command": "assistant",
            "ok": True,
            "request": request_text,
        }
    return {"command": "unknown", "ok": False, "error": f"unsupported command: {command}"}


def _parse_datetime_hint(text: str, timezone_name: str) -> str | None:
    if not DATE_TOKEN_RE.search(text):
        return None
    default = datetime.now(ZoneInfo(timezone_name)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    try:
        parsed = dt_parser.parse(text, fuzzy=True, default=default)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(ZoneInfo(timezone_name)).isoformat()


def classify_message(text: str, timezone_name: str) -> tuple[str, dict[str, Any]]:
    body = text.strip()
    command = parse_command_message(body)
    if command is not None:
        return ("command", command)
    dt_hint = _parse_datetime_hint(body, timezone_name)
    if dt_hint:
        start = dt_parser.isoparse(dt_hint)
        end = start + timedelta(hours=1)
        return (
            "event_draft",
            {
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "title": body[:120] or "Telegram Draft Event",
            },
        )

    if TASK_HINT_RE.search(body):
        due_at = _parse_datetime_hint(body, timezone_name)
        return (
            "task_draft",
            {
                "title": body[:120] or "Telegram Draft Task",
                "due_at": due_at,
                "status": "open",
            },
        )

    return (
        "note",
        {
            "title": body[:120] or "Telegram Note",
            "body": body,
        },
    )


class TelegramBotClient:
    def __init__(self, bot_token: str, timeout_sec: int = 30):
        self.bot_token = bot_token
        self.timeout_sec = timeout_sec
        self.api_base = f"https://api.telegram.org/bot{bot_token}"

    def _long_poll_request_timeout(self, poll_timeout: int) -> tuple[int, int]:
        connect_timeout = max(int(self.timeout_sec), 1)
        # Telegram may legally hold getUpdates open until the requested poll timeout.
        read_timeout = max(connect_timeout, int(poll_timeout) + 10)
        return (connect_timeout, read_timeout)

    def get_updates(
        self,
        offset: int | None = None,
        limit: int = 100,
        timeout: int = 10,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"limit": int(limit), "timeout": int(timeout)}
        if offset is not None:
            payload["offset"] = int(offset)
        try:
            response = requests.get(
                f"{self.api_base}/getUpdates",
                params=payload,
                timeout=self._long_poll_request_timeout(timeout),
            )
        except requests.exceptions.ReadTimeout:
            # Long-poll timeouts are recoverable; the next poll can continue with the same offset.
            return []
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not body.get("ok"):
            return []
        result = body.get("result")
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, chat_id: str | int, text: str) -> bool:
        response = requests.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": str(chat_id),
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
        return bool(isinstance(body, dict) and body.get("ok"))

    def send_chat_action(self, chat_id: str | int, action: str = "typing") -> bool:
        response = requests.post(
            f"{self.api_base}/sendChatAction",
            json={
                "chat_id": str(chat_id),
                "action": str(action or "typing").strip() or "typing",
            },
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
        return bool(isinstance(body, dict) and body.get("ok"))

    def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        payload = []
        for item in commands:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip().lower()
            description = str(item.get("description") or "").strip()
            if not command or not description:
                continue
            payload.append({"command": command, "description": description})
        response = requests.post(
            f"{self.api_base}/setMyCommands",
            json={"commands": payload},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
        return bool(isinstance(body, dict) and body.get("ok"))


def normalize_updates(
    updates: list[dict[str, Any]],
    timezone_name: str,
    allowed_chat_ids: list[str] | None = None,
) -> list[TelegramInboxItem]:
    allowed = {str(item) for item in (allowed_chat_ids or []) if str(item).strip()}
    normalized: list[TelegramInboxItem] = []
    for update in updates:
        update_id = update.get("update_id")
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "")
        command_payload = parse_command_message(text)
        command_name = str(command_payload.get("command") or "").strip().lower() if command_payload else ""
        chat_allowed = (not allowed) or (chat_id in allowed)
        if allowed and chat_id not in allowed and command_name not in ONBOARDING_COMMANDS:
            continue

        date_raw = message.get("date")
        received = datetime.now(ZoneInfo(timezone_name))
        if isinstance(date_raw, (int, float)):
            received = datetime.fromtimestamp(float(date_raw), tz=ZoneInfo(timezone_name))
        item_type, draft = classify_message(text, timezone_name=timezone_name)
        title = text.strip().splitlines()[0][:120]
        normalized.append(
            TelegramInboxItem(
                external_id=f"telegram:update:{update_id}",
                received_at=received.isoformat(),
                title=title or "Telegram message",
                body=text.strip(),
                item_type=item_type,
                draft=draft,
                metadata={
                    "update_id": update_id,
                    "chat_id": chat_id,
                    "chat_type": chat.get("type"),
                    "chat_allowed": chat_allowed,
                    "from": message.get("from"),
                },
            )
        )
    return normalized
