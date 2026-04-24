from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


SYSTEM_PROMPT = (
    "You are an academic assistant. Summarize updates into exactly 3 bullet points and 1 action item. "
    "Keep each bullet under 20 words. Format:\n"
    "- ...\n- ...\n- ...\nAction: ..."
)
_FILECITE_RE = re.compile(r"\s*filecite.*?\s*")
_REASONING_PREFIX_PATTERNS = (
    re.compile(r"^\s*<think>[\s\S]*?</think>\s*", re.IGNORECASE),
    re.compile(r"^\s*<thought>[\s\S]*?</thought>\s*", re.IGNORECASE),
    re.compile(r"^\s*<\|channel\|>thought[\s\S]*?<channel\|>\s*", re.IGNORECASE),
)
LOCAL_ONLY_PROVIDER = "local"


@dataclass
class LLMConfig:
    provider: str
    model: str
    timeout_sec: int
    local_endpoint: str = "http://127.0.0.1:11434/api/generate"


@dataclass
class SummaryResult:
    bullets: list[str]
    action_item: str
    raw_text: str


def _normalize_model_output(text: str) -> str:
    cleaned = str(text or "").strip()
    while cleaned:
        changed = False
        for pattern in _REASONING_PREFIX_PATTERNS:
            normalized, count = pattern.subn("", cleaned, count=1)
            if count:
                cleaned = normalized.strip()
                changed = True
        if not changed:
            break
    return cleaned


def _normalize_summary_text(text: str) -> str:
    cleaned = _FILECITE_RE.sub(" ", _normalize_model_output(text)).strip()
    if "\n" not in cleaned and cleaned.startswith("- "):
        cleaned = re.sub(r"\s+Action:\s+", "\nAction: ", cleaned)
        cleaned = re.sub(r"\s+-\s+", "\n- ", cleaned)
    return cleaned.strip()


def _ollama_tags_url(endpoint: str) -> str | None:
    parsed = urlparse(str(endpoint or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/api/tags", "", "", ""))


def _select_ollama_family_model(requested_model: str, available_models: list[str]) -> str:
    requested = str(requested_model or "").strip()
    if not requested:
        return requested
    available = [str(name or "").strip() for name in available_models if str(name or "").strip()]
    if requested in available:
        return requested
    family_prefix = f"{requested}:"
    family_matches = [name for name in available if name.startswith(family_prefix)]
    if not family_matches:
        return requested
    for candidate in (
        f"{requested}:latest",
        f"{requested}:e4b",
        f"{requested}:e2b",
    ):
        if candidate in family_matches:
            return candidate
    return sorted(family_matches)[0]


@lru_cache(maxsize=32)
def _resolve_local_model_name(endpoint: str, requested_model: str, timeout_sec: int) -> str:
    requested = str(requested_model or "").strip()
    if not requested or ":" in requested:
        return requested
    tags_url = _ollama_tags_url(endpoint)
    if not tags_url:
        return requested
    try:
        response = requests.get(tags_url, timeout=max(int(timeout_sec or 1), 1))
        response.raise_for_status()
        body = response.json()
    except Exception:
        return requested
    items = body.get("models")
    if not isinstance(items, list):
        return requested
    available_models = [str(item.get("name") or "").strip() for item in items if isinstance(item, dict)]
    return _select_ollama_family_model(requested, available_models)


def parse_summary_text(text: str) -> SummaryResult:
    normalized = _normalize_summary_text(text)
    bullets: list[str] = []
    action_item = ""
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("action:"):
            action_item = line.split(":", 1)[1].strip()
            continue
        if line.startswith(("-", "*")):
            bullets.append(line[1:].strip())
            continue
        if line[:2].isdigit() and line[2:3] in {".", ")"}:
            bullets.append(line[3:].strip())
            continue
        if len(bullets) < 3:
            bullets.append(line)
    bullets = [item for item in bullets if item][:3]
    while len(bullets) < 3:
        bullets.append("No additional update.")
    if not action_item:
        action_item = "Review updates and schedule next step."
    return SummaryResult(
        bullets=bullets,
        action_item=action_item,
        raw_text=normalized,
    )


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def summarize(
        self,
        payload: dict[str, Any],
        *,
        system_prompt: str = SYSTEM_PROMPT,
        attachment_paths: list[str] | None = None,
    ) -> SummaryResult:
        prompt = (
            "Summarize the following JSON payload for a student dashboard.\n\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        text = self.generate_text(
            system_prompt=system_prompt,
            prompt=prompt,
            attachment_paths=attachment_paths,
        )
        return parse_summary_text(text)

    def generate_text(
        self,
        system_prompt: str,
        prompt: str,
        attachment_paths: list[str] | None = None,
    ) -> str:
        provider = self.config.provider.strip().lower()
        if provider != LOCAL_ONLY_PROVIDER:
            raise ValueError(
                "Only the local LLM provider is enabled. Set LLM_PROVIDER=local."
            )
        if provider == "local":
            return self._call_local(system_prompt=system_prompt, prompt=prompt)
        raise ValueError(f"unsupported LLM provider: {self.config.provider}")

    def _call_local(self, system_prompt: str, prompt: str) -> str:
        endpoint = self.config.local_endpoint
        model_name = _resolve_local_model_name(
            endpoint,
            self.config.model,
            self.config.timeout_sec,
        ) or self.config.model
        if endpoint.rstrip("/").endswith("/api/generate"):
            response = requests.post(
                endpoint,
                json={
                    "model": model_name,
                    "prompt": f"{system_prompt}\n\n{prompt}",
                    "stream": False,
                },
                timeout=self.config.timeout_sec,
            )
            response.raise_for_status()
            body = response.json()
            text = body.get("response")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("local LLM response missing 'response'")
            return _normalize_model_output(text)

        if endpoint.rstrip("/").endswith("/api/chat"):
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2},
            }
            try:
                body = self._post_local_chat(endpoint, payload)
            except requests.HTTPError as exc:
                response = exc.response
                if response is None or response.status_code != 400:
                    raise
                payload.pop("think", None)
                body = self._post_local_chat(endpoint, payload)
            message = body.get("message", {})
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("local LLM response missing message content")
            return _normalize_model_output(content)

        response = requests.post(
            endpoint,
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=self.config.timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("local LLM response missing choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("local LLM response missing content")
        return _normalize_model_output(content)

    def _post_local_chat(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            endpoint,
            json=payload,
            timeout=self.config.timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("local LLM response must be an object")
        return body
