from __future__ import annotations

from sidae_secretary.assistant.registry import (
    ACTION_SCHEMA_VERSION,
    CAPABILITY_REGISTRY_VERSION,
    get_capability,
    serialize_action_schema,
    serialize_capability_registry,
    validate_action_payload,
)


def test_capability_registry_serialization_contains_required_capabilities() -> None:
    payload = serialize_capability_registry()

    assert payload["version"] == CAPABILITY_REGISTRY_VERSION
    assert [item["name"] for item in payload["capabilities"]] == [
        "query_today",
        "query_tomorrow",
        "query_weather",
        "query_tomorrow_weather",
        "set_weather_region",
        "create_one_time_reminder",
        "set_notification_policy",
        "explain_capability",
    ]
    assert get_capability("query_today") is not None
    assert (
        get_capability("set_notification_policy").action_schema()["required"]
        == ["policy_kind", "enabled"]
    )


def test_action_schema_serialization_is_stable() -> None:
    payload = serialize_action_schema()

    assert payload["version"] == ACTION_SCHEMA_VERSION
    assert payload["required"] == ["version", "capability", "arguments"]
    assert payload["properties"]["version"]["const"] == ACTION_SCHEMA_VERSION
    assert "create_one_time_reminder" in payload["properties"]["capability"]["enum"]


def test_validate_action_payload_accepts_valid_write_actions() -> None:
    reminder = validate_action_payload(
        {
            "version": ACTION_SCHEMA_VERSION,
            "capability": "create_one_time_reminder",
            "arguments": {
                "run_at_local": "2026-04-01T08:30",
                "message": "과제 제출 알림",
                "timezone": "Asia/Seoul",
            },
        }
    )
    policy = validate_action_payload(
        {
            "version": ACTION_SCHEMA_VERSION,
            "capability": "set_notification_policy",
            "arguments": {
                "policy_kind": "daily_digest",
                "enabled": True,
                "days_of_week": ["mon", "wed"],
                "time_local": "08:30",
                "timezone": "Asia/Seoul",
            },
        }
    )

    assert reminder.ok is True
    assert reminder.normalized_action["capability"] == "create_one_time_reminder"
    assert reminder.normalized_action["arguments"]["message"] == "과제 제출 알림"
    assert policy.ok is True
    assert policy.normalized_action["arguments"]["days_of_week"] == ["mon", "wed"]


def test_validate_action_payload_rejects_unknown_capability() -> None:
    result = validate_action_payload(
        {
            "version": ACTION_SCHEMA_VERSION,
            "capability": "delete_everything",
            "arguments": {},
        }
    )

    assert result.ok is False
    assert "unknown capability: delete_everything" in result.errors


def test_validate_action_payload_rejects_invalid_arguments() -> None:
    result = validate_action_payload(
        {
            "version": ACTION_SCHEMA_VERSION,
            "capability": "set_notification_policy",
            "arguments": {
                "policy_kind": "daily_digest",
                "enabled": "yes",
                "days_of_week": ["monday"],
                "time_local": "25:00",
                "unexpected": 1,
            },
        }
    )

    assert result.ok is False
    assert "arguments.enabled must be a boolean" in result.errors
    assert "arguments.days_of_week[0] must be one of ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']" in result.errors
    assert "arguments.time_local does not match required format" in result.errors
    assert "arguments.unexpected is not allowed for capability set_notification_policy" in result.errors
