"""Pure auxiliary routing. Snapshots contain references, never resolved secrets."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from eidolon.config import AppConfig, ProviderConfig


class AuxiliaryConfigurationError(ValueError):
    """A safe, actionable error isolated from dialogue startup."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def connection_digest(profile: ProviderConfig) -> str:
    return digest({"endpoint": profile.endpoint, "headers": profile.headers,
                   "api_key_ref": profile.api_key_ref})


def configured_route_fingerprint(config: AppConfig) -> str:
    """Exclude inventories, estimates and timestamps from the commit identity."""
    role = config.auxiliary
    identity = {"auxiliary": role.model_dump(), "timezone": config.timezone}
    name = config.provider if role.mode == "inherit" else role.provider
    profile = next((p for p in config.providers if p.name == name), None)
    if role.mode != "disabled":
        identity["connection"] = connection_digest(profile) if profile else None
        if role.mode == "inherit" and profile:
            identity["inherited"] = {"provider": profile.name, "model": profile.model,
                                      "parameters": profile.parameters,
                                      "context_window": profile.context_window}
    return digest(identity)


@dataclass(frozen=True)
class AuxiliaryExecution:
    mode: str
    provider: str
    model: str
    routing_revision: int
    route_fingerprint: str
    execution_fingerprint: str
    context_chars: int | None
    context_window: int | None
    timeout_seconds: float
    stream_usage: bool
    prompt_hash: str
    schema_hash: str
    _profile_json: str = field(repr=False)
    _options_json: str = field(repr=False)
    role: str = "auxiliary"

    def profile(self) -> ProviderConfig:
        # Fresh mutable adapter config; the execution snapshot remains frozen.
        return ProviderConfig.model_validate_json(self._profile_json)

    def provenance(self) -> dict:
        return {"role": self.role, "mode": self.mode, "provider": self.provider,
                "model": self.model, "routing_revision": self.routing_revision,
                "route_fingerprint": self.route_fingerprint,
                "execution_fingerprint": self.execution_fingerprint,
                "request_options": json.loads(self._options_json),
                "prompt_hash": self.prompt_hash, "schema_hash": self.schema_hash}


def resolve_auxiliary(config: AppConfig, revision: int = 0) -> AuxiliaryExecution | None:
    from eidolon.memory.proposals import ORDINARY_CONTRACT, ORDINARY_INSTRUCTIONS, OrdinaryProposal

    role = config.auxiliary
    if role.mode == "disabled":
        return None
    if role.mode == "separate" and (not role.provider or not role.model):
        raise AuxiliaryConfigurationError("Choose an auxiliary connection and model ID in Preferences.")
    name = config.provider if role.mode == "inherit" else role.provider
    connection = next((p for p in config.providers if p.name == name), None)
    if connection is None:
        raise AuxiliaryConfigurationError("Auxiliary connection is missing; choose a saved connection in Preferences.")
    target_model = role.model if role.mode == "separate" else connection.model
    if (not target_model or len(target_model) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in target_model)):
        raise AuxiliaryConfigurationError("Choose a nonempty bounded auxiliary model ID in Preferences.")
    chars = role.context_chars
    if role.mode == "separate":
        model = role.model
        metadata = connection.model_metadata.get(model)
        # Never inherit a manual capacity for the connection's dialogue model.
        profile = ProviderConfig(
            name=connection.name, endpoint=connection.endpoint,
            api_key_ref=connection.api_key_ref, headers=connection.headers.copy(),
            model=model, tools=False, presentation="plain", context_window=role.context_window,
            model_metadata={model: metadata} if metadata else {},
            parameters={role.output_token_field: role.max_output_tokens},
        )
        if role.reasoning_effort is not None:
            profile.parameters["reasoning_effort"] = role.reasoning_effort
        if "stream_options" in connection.parameters:
            profile.parameters["stream_options"] = copy.deepcopy(
                connection.parameters["stream_options"])
        stream_usage = True
    else:
        profile = connection.model_copy(deep=True)
        reserved = {"model", "messages", "stream", "tools", "tool_choice", "response_format"}
        if reserved.intersection(profile.parameters):
            raise AuxiliaryConfigurationError("Remove reserved provider parameter overrides before auxiliary inference.")
        keys = [key for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
                if key in profile.parameters]
        if len(keys) > 1 or "max_output_tokens" in keys:
            raise AuxiliaryConfigurationError("Auxiliary inference needs exactly one max_tokens or max_completion_tokens field.")
        key = keys[0] if keys else role.output_token_field
        current = profile.parameters.get(key)
        profile.parameters[key] = (min(current, role.max_output_tokens)
                                   if type(current) is int and current > 0
                                   else role.max_output_tokens)
        if "reasoning_effort" in profile.parameters:
            profile.parameters["reasoning_effort"] = "low"
        profile.tools, profile.presentation = False, "plain"
        profile.structured_verified = False
        if role.context_window is not None:
            profile.context_window = role.context_window
        stream_usage = True
    if role.thinking_mode is not None:
        profile.parameters["thinking"] = {"type": role.thinking_mode}
    stream_options = profile.parameters.get("stream_options")
    if isinstance(stream_options, dict) and stream_options.get("include_usage") is False:
        stream_usage = False
    # Diagnostics contain only explicitly safe effective options, never arbitrary
    # inherited parameters (which may include provider-specific private values).
    options = {key: value for key, value in profile.parameters.items()
               if key in {"max_tokens", "max_completion_tokens", "reasoning_effort"}
               and (type(value) in {int, float} or isinstance(value, str) and len(value) <= 80)}
    if role.thinking_mode is not None:
        options["thinking_mode"] = role.thinking_mode
    options.update(context_chars=chars, context_window=profile.context_window_for(),
                   timeout_seconds=role.timeout_seconds, stream_usage=stream_usage)
    prompt_hash = hashlib.sha256(ORDINARY_INSTRUCTIONS.encode()).hexdigest()
    schema_hash = digest({"contract": ORDINARY_CONTRACT,
                          "schema": OrdinaryProposal.model_json_schema()})
    route = configured_route_fingerprint(config)
    execution = digest({"route": route, "options": options,
                        "prompt": prompt_hash, "schema": schema_hash})
    return AuxiliaryExecution(role.mode, profile.name, profile.model, revision, route, execution,
                              chars, profile.context_window_for(), role.timeout_seconds,
                              stream_usage, prompt_hash, schema_hash, profile.model_dump_json(),
                              json.dumps(options))
