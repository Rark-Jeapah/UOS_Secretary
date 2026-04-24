from __future__ import annotations

from types import SimpleNamespace

import requests

from sidae_secretary import cli
from sidae_secretary.connectors import llm as llm_module


def test_llm_client_rejects_non_local_provider() -> None:
    client = llm_module.LLMClient(
        llm_module.LLMConfig(
            provider="remote",
            model="gemma4:e4b",
            timeout_sec=45,
        )
    )

    try:
        client.summarize({"topic": "week1"})
    except ValueError as exc:
        assert str(exc) == "Only the local LLM provider is enabled. Set LLM_PROVIDER=local."
    else:  # pragma: no cover
        raise AssertionError("non-local provider should be rejected")


def test_llm_client_dispatches_local_ollama_chat_endpoint(monkeypatch) -> None:
    called: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "message": {
                    "content": "- alpha\n- beta\n- gamma\nAction: review tonight"
                }
            }

    def _fake_post(url: str, *, json=None, timeout=None):
        called["url"] = url
        called["json"] = json
        called["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(llm_module.requests, "post", _fake_post)
    client = llm_module.LLMClient(
        llm_module.LLMConfig(
            provider="local",
            model="gemma4:e4b",
            timeout_sec=120,
            local_endpoint="http://127.0.0.1:11434/api/chat",
        )
    )

    result = client.summarize({"topic": "week1"})

    assert result.bullets == ["alpha", "beta", "gamma"]
    assert result.action_item == "review tonight"
    assert called["url"] == "http://127.0.0.1:11434/api/chat"
    assert called["timeout"] == 120
    assert called["json"] == {
        "model": "gemma4:e4b",
        "messages": [
            {"role": "system", "content": llm_module.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Summarize the following JSON payload for a student dashboard.\n\n"
                    '{"topic": "week1"}'
                ),
            },
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2},
    }


def test_llm_client_resolves_gemma_family_to_local_tag(monkeypatch) -> None:
    called: dict[str, object] = {}

    class FakeTagsResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"models": [{"name": "gemma4:e4b"}]}

    class FakeChatResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "message": {
                    "content": "- alpha\n- beta\n- gamma\nAction: review tonight"
                }
            }

    def _fake_get(url: str, *, timeout=None):
        called["tags_url"] = url
        called["tags_timeout"] = timeout
        return FakeTagsResponse()

    def _fake_post(url: str, *, json=None, timeout=None):
        called["url"] = url
        called["json"] = json
        called["timeout"] = timeout
        return FakeChatResponse()

    monkeypatch.setattr(llm_module.requests, "get", _fake_get)
    monkeypatch.setattr(llm_module.requests, "post", _fake_post)
    client = llm_module.LLMClient(
        llm_module.LLMConfig(
            provider="local",
            model="gemma4",
            timeout_sec=90,
            local_endpoint="http://127.0.0.1:11434/api/chat",
        )
    )

    result = client.summarize({"topic": "week1"})

    assert result.bullets == ["alpha", "beta", "gamma"]
    assert called["tags_url"] == "http://127.0.0.1:11434/api/tags"
    assert called["json"]["model"] == "gemma4:e4b"


def test_llm_client_retries_chat_without_think_when_server_rejects_it(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FailingResponse:
        status_code = 400

        def raise_for_status(self) -> None:
            raise requests.HTTPError("bad request", response=self)

        def json(self) -> dict[str, object]:
            return {}

    class SuccessResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "message": {
                    "content": "- alpha\n- beta\n- gamma\nAction: review tonight"
                }
            }

    def _fake_post(url: str, *, json=None, timeout=None):
        payload = dict(json or {})
        calls.append(payload)
        if "think" in payload:
            return FailingResponse()
        return SuccessResponse()

    monkeypatch.setattr(llm_module.requests, "post", _fake_post)
    client = llm_module.LLMClient(
        llm_module.LLMConfig(
            provider="local",
            model="gemma4:e4b",
            timeout_sec=60,
            local_endpoint="http://127.0.0.1:11434/api/chat",
        )
    )

    result = client.summarize({"topic": "week1"})

    assert result.action_item == "review tonight"
    assert len(calls) == 2
    assert calls[0]["think"] is False
    assert "think" not in calls[1]


def test_dependency_checks_require_local_provider_when_llm_enabled(
    monkeypatch,
) -> None:
    def _fake_find_spec(name: str):
        if name in {
            "typer",
            "requests",
            "dateutil",
            "icalendar",
        }:
            return object()
        return None

    monkeypatch.setattr(cli.importlib.util, "find_spec", _fake_find_spec)
    deps = cli._dependency_checks(SimpleNamespace(llm_enabled=True, llm_provider="remote"))

    assert deps["playwright_import_ok"] is False
    assert deps["llm_provider_supported"] is False
    assert deps["llm_provider_import_ok"] is False
    assert deps["llm_import_ok"] is False
