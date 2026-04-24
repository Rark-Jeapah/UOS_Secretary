from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


CAPABILITY_REGISTRY_VERSION = "assistant_capability_registry.v1"
ACTION_SCHEMA_VERSION = "assistant_action.v1"
DAY_OF_WEEK_VALUES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
LOCAL_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?$")


@dataclass(frozen=True)
class ActionFieldDefinition:
    name: str
    field_type: str
    description: str
    required: bool = False
    enum: tuple[str, ...] = ()
    items_type: str | None = None
    items_enum: tuple[str, ...] = ()
    pattern: str | None = None

    def to_schema_property(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.field_type,
            "description": self.description,
        }
        if self.enum:
            payload["enum"] = list(self.enum)
        if self.pattern:
            payload["pattern"] = self.pattern
        if self.field_type == "array":
            items: dict[str, Any] = {
                "type": self.items_type or "string",
            }
            if self.items_enum:
                items["enum"] = list(self.items_enum)
            payload["items"] = items
        return payload

    def validate_and_normalize(self, value: Any) -> tuple[Any, list[str]]:
        errors: list[str] = []
        if self.field_type == "string":
            if not isinstance(value, str):
                return None, [f"arguments.{self.name} must be a string"]
            normalized = value.strip()
            if not normalized:
                return None, [f"arguments.{self.name} must not be empty"]
            if self.enum and normalized not in self.enum:
                return None, [f"arguments.{self.name} must be one of {list(self.enum)}"]
            if self.pattern and not re.fullmatch(self.pattern, normalized):
                return None, [f"arguments.{self.name} does not match required format"]
            return normalized, []

        if self.field_type == "boolean":
            if not isinstance(value, bool):
                return None, [f"arguments.{self.name} must be a boolean"]
            return value, []

        if self.field_type == "array":
            if not isinstance(value, list):
                return None, [f"arguments.{self.name} must be an array"]
            normalized_items: list[Any] = []
            for idx, item in enumerate(value):
                if (self.items_type or "string") == "string":
                    if not isinstance(item, str):
                        errors.append(f"arguments.{self.name}[{idx}] must be a string")
                        continue
                    normalized_item = item.strip()
                    if not normalized_item:
                        errors.append(f"arguments.{self.name}[{idx}] must not be empty")
                        continue
                    if self.items_enum and normalized_item not in self.items_enum:
                        errors.append(
                            f"arguments.{self.name}[{idx}] must be one of {list(self.items_enum)}"
                        )
                        continue
                    normalized_items.append(normalized_item)
                    continue
                errors.append(f"arguments.{self.name}[{idx}] has unsupported item type")
            return normalized_items, errors

        return None, [f"arguments.{self.name} uses unsupported field type"]


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    category: str
    summary: str
    side_effect: bool
    fields: tuple[ActionFieldDefinition, ...] = ()

    def action_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [field.name for field in self.fields if field.required],
            "properties": {
                field.name: field.to_schema_property()
                for field in self.fields
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "summary": self.summary,
            "side_effect": self.side_effect,
            "action_schema": self.action_schema(),
        }

    def validate_arguments(self, arguments: Any) -> tuple[dict[str, Any] | None, list[str]]:
        if not isinstance(arguments, dict):
            return None, ["arguments must be an object"]
        field_map = {field.name: field for field in self.fields}
        errors: list[str] = []
        extras = sorted(set(arguments.keys()) - set(field_map.keys()))
        for key in extras:
            errors.append(f"arguments.{key} is not allowed for capability {self.name}")
        normalized: dict[str, Any] = {}
        for field in self.fields:
            if field.name not in arguments:
                if field.required:
                    errors.append(f"arguments.{field.name} is required")
                continue
            normalized_value, field_errors = field.validate_and_normalize(arguments[field.name])
            if field_errors:
                errors.extend(field_errors)
                continue
            normalized[field.name] = normalized_value
        if errors:
            return None, errors
        return normalized, []


@dataclass(frozen=True)
class ActionValidationResult:
    ok: bool
    errors: tuple[str, ...]
    normalized_action: dict[str, Any] | None = None


_BASE_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        name="query_today",
        category="query",
        summary="Read today's schedule, tasks, and related briefing context.",
        side_effect=False,
    ),
    CapabilityDefinition(
        name="query_tomorrow",
        category="query",
        summary="Read tomorrow's schedule, tasks, and related briefing context.",
        side_effect=False,
    ),
    CapabilityDefinition(
        name="query_weather",
        category="query",
        summary="Read the user's current weather summary and forecast snapshot.",
        side_effect=False,
    ),
    CapabilityDefinition(
        name="query_tomorrow_weather",
        category="query",
        summary="Read tomorrow's forecast and weather outlook for the user.",
        side_effect=False,
    ),
    CapabilityDefinition(
        name="set_weather_region",
        category="mutation",
        summary="Update the user's default weather region or district.",
        side_effect=True,
        fields=(
            ActionFieldDefinition(
                name="region_query",
                field_type="string",
                description="Natural-language region name or district query.",
                required=True,
            ),
        ),
    ),
    CapabilityDefinition(
        name="create_one_time_reminder",
        category="mutation",
        summary="Create a single reminder at one local datetime.",
        side_effect=True,
        fields=(
            ActionFieldDefinition(
                name="run_at_local",
                field_type="string",
                description="Local datetime in YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM format.",
                required=True,
                pattern=LOCAL_DATETIME_RE.pattern,
            ),
            ActionFieldDefinition(
                name="message",
                field_type="string",
                description="Reminder message shown to the user.",
                required=True,
            ),
            ActionFieldDefinition(
                name="timezone",
                field_type="string",
                description="IANA timezone name for interpreting run_at_local.",
            ),
        ),
    ),
    CapabilityDefinition(
        name="set_notification_policy",
        category="mutation",
        summary="Create or update a recurring notification policy record.",
        side_effect=True,
        fields=(
            ActionFieldDefinition(
                name="policy_kind",
                field_type="string",
                description="Stable policy identifier such as daily_digest or briefing_morning.",
                required=True,
            ),
            ActionFieldDefinition(
                name="enabled",
                field_type="boolean",
                description="Whether the policy should be enabled.",
                required=True,
            ),
            ActionFieldDefinition(
                name="days_of_week",
                field_type="array",
                description="Optional weekday list in mon-sun form.",
                items_type="string",
                items_enum=DAY_OF_WEEK_VALUES,
            ),
            ActionFieldDefinition(
                name="time_local",
                field_type="string",
                description="Optional local send time in HH:MM format.",
                pattern=HHMM_RE.pattern,
            ),
            ActionFieldDefinition(
                name="timezone",
                field_type="string",
                description="Optional IANA timezone name.",
            ),
        ),
    ),
)

CAPABILITY_NAMES: tuple[str, ...] = tuple(item.name for item in _BASE_CAPABILITIES) + (
    "explain_capability",
)

CAPABILITIES: tuple[CapabilityDefinition, ...] = _BASE_CAPABILITIES + (
    CapabilityDefinition(
        name="explain_capability",
        category="meta",
        summary="Explain what a capability does and what arguments it accepts.",
        side_effect=False,
        fields=(
            ActionFieldDefinition(
                name="capability_name",
                field_type="string",
                description="Capability name to explain.",
                required=True,
                enum=CAPABILITY_NAMES,
            ),
        ),
    ),
)


_CAPABILITY_MAP = {item.name: item for item in CAPABILITIES}


def get_capability(name: str) -> CapabilityDefinition | None:
    return _CAPABILITY_MAP.get(str(name or "").strip())


def serialize_capability_registry() -> dict[str, Any]:
    return {
        "version": CAPABILITY_REGISTRY_VERSION,
        "capabilities": [item.to_dict() for item in CAPABILITIES],
    }


def serialize_action_schema() -> dict[str, Any]:
    return {
        "version": ACTION_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "capability", "arguments"],
        "properties": {
            "version": {"type": "string", "const": ACTION_SCHEMA_VERSION},
            "capability": {
                "type": "string",
                "enum": list(CAPABILITY_NAMES),
            },
            "arguments": {
                "type": "object",
                "description": (
                    "Arguments must validate against the action_schema of the selected capability "
                    f"from registry version {CAPABILITY_REGISTRY_VERSION}."
                ),
            },
        },
    }


def validate_action_payload(payload: Any) -> ActionValidationResult:
    if not isinstance(payload, dict):
        return ActionValidationResult(ok=False, errors=("payload must be an object",))
    errors: list[str] = []
    extras = sorted(set(payload.keys()) - {"version", "capability", "arguments"})
    for key in extras:
        errors.append(f"{key} is not allowed")

    version = str(payload.get("version") or "").strip()
    if version != ACTION_SCHEMA_VERSION:
        errors.append(f"version must equal {ACTION_SCHEMA_VERSION}")

    capability_name = str(payload.get("capability") or "").strip()
    capability = get_capability(capability_name)
    if capability is None:
        errors.append(f"unknown capability: {capability_name or '<empty>'}")

    arguments = payload.get("arguments")
    normalized_arguments: dict[str, Any] | None = None
    if capability is not None:
        normalized_arguments, argument_errors = capability.validate_arguments(arguments)
        errors.extend(argument_errors)
    elif "arguments" not in payload:
        errors.append("arguments is required")
    elif not isinstance(arguments, dict):
        errors.append("arguments must be an object")

    if errors:
        return ActionValidationResult(ok=False, errors=tuple(errors))

    return ActionValidationResult(
        ok=True,
        errors=(),
        normalized_action={
            "version": ACTION_SCHEMA_VERSION,
            "capability": capability_name,
            "arguments": normalized_arguments or {},
        },
    )
