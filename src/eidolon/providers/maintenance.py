"""Maintenance output controls for the existing streaming Chat Completions transport.

No discovery request or paid capability probe is performed here. Support must be
verified for the exact route; dialogue's structured_verified flag is unrelated.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import json
from urllib.parse import urlsplit

from eidolon.config import ProviderConfig
from eidolon.memory.proposals import task_contract


OUTPUT_MODES = frozenset({"prompt", "json_object", "json_schema"})
CAPABILITY_SOURCES = frozenset({"unknown", "configured_verification", "documented_ignored",
                                "provider_rejected"})


def portable_schema(schema: dict) -> dict:
    """Use the conservative strict subset; full bounds/semantics stay local.

    Optional fields are required nullable fields on the wire. Pydantic's union
    discriminator, defaults, and validation keywords aren't portable controls.
    """
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "$ref", "$defs", "anyOf", "enum"}
    result = {}
    for key, value in schema.items():
        if key == "const":
            result["enum"] = [value]
        elif key in {"oneOf", "anyOf"}:
            result["anyOf"] = [portable_schema(child) for child in value]
        elif key in {"properties", "$defs"}:
            result[key] = {name: portable_schema(child) for name, child in value.items()}
        elif key == "items":
            result[key] = portable_schema(value)
        elif key in allowed:
            result[key] = value
    if result.get("type") == "object":
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


@dataclass(frozen=True)
class MaintenanceOutput:
    contract: str
    mode: str
    capability_source: str
    # Serialized by callers before dispatch; never contains evidence or credentials.
    response_format: dict | None

    def provenance(self) -> dict:
        return {"contract": self.contract, "output_mode": self.mode,
                "capability_source": self.capability_source,
                "provider_schema_hash": hashlib.sha256(json.dumps(
                    self.response_format, sort_keys=True).encode()).hexdigest()}


class OutputSupportCache:
    """Bound negative evidence to one service lifetime and exact configuration.

    Only explicit machine-readable unsupported-format errors enter this cache.
    Generic 400s, schema mistakes, refusals and successful responses prove nothing.
    It is shared across auxiliary adapter replacements, including optional Tests.
    """

    def __init__(self) -> None:
        self._rejected: OrderedDict[str, set[str]] = OrderedDict()

    @staticmethod
    def _key(profile: ProviderConfig) -> str:
        # References are hashed without resolving secrets; never expose them.
        identity = {key: value for key, value in profile.model_dump().items() if key in {
            "endpoint", "model", "parameters", "headers", "api_key_ref",
            "maintenance_output_support",
        }}
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def rejected(self, profile: ProviderConfig) -> set[str]:
        return self._rejected.get(self._key(profile), set()).copy()

    def reject(self, profile: ProviderConfig, mode: str) -> None:
        key = self._key(profile)
        self._rejected.setdefault(key, set()).add(mode)
        self._rejected.move_to_end(key)
        while len(self._rejected) > 128:
            self._rejected.popitem(last=False)


def maintenance_output(profile: ProviderConfig, task_id: str,
                       rejected: set[str] | None = None) -> MaintenanceOutput:
    contract, _, schema, _ = task_contract(task_id)
    thinking = profile.parameters.get("thinking")
    thinking_mode = thinking.get("type") if isinstance(thinking, dict) else None
    matches = [support for support in profile.maintenance_output_support
               if (support.endpoint, support.model, support.reasoning_effort, support.thinking_mode)
               == (profile.endpoint, profile.model, profile.parameters.get("reasoning_effort"),
                   thinking_mode)]
    mode, source = "prompt", "unknown"
    # Contradictory records never establish enforcement. The native Anthropic
    # compatibility layer documents that response_format is ignored altogether.
    if urlsplit(profile.endpoint).hostname == "api.anthropic.com":
        source = "documented_ignored"
    elif len(matches) == 1:
        support = matches[0]
        source = "configured_verification"
        rejected = rejected or set()
        if support.json_schema == "enforced" and "json_schema" not in rejected:
            mode = "json_schema"
        elif support.json_object is True and "json_object" not in rejected:
            mode = "json_object"
        if rejected:
            source = "provider_rejected"
    response_format = None
    if mode == "json_schema":
        response_format = {"type": mode, "json_schema": {
            "name": contract.replace("-", "_"), "strict": True,
            "schema": portable_schema(schema.model_json_schema()),
        }}
    elif mode == "json_object":
        response_format = {"type": mode}
    return MaintenanceOutput(contract, mode, source, response_format)
