"""Maintenance wire contracts stay independent of dialogue presentation."""
import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from eidolon.config import AppConfig, ProviderConfig
from eidolon.providers.openai import OpenAIProvider
from eidolon.providers.routing import resolve_auxiliary


def profile_with_support(**support):
    return ProviderConfig.model_validate({
        "endpoint": "https://synthetic.example/v1", "model": "synthetic-model",
        "maintenance_output_support": [{
            "endpoint": "https://synthetic.example/v1", "model": "synthetic-model",
            **support,
        }],
    })


@pytest.mark.parametrize("route", ["inherit", "separate"])
@pytest.mark.parametrize("task_id", ["ordinary", "review:synthetic", "narrative:synthetic"])
async def test_verified_route_sends_task_specific_portable_schema(route, task_id):
    profile = profile_with_support(json_schema="enforced", json_object=True)
    config = AppConfig(providers=[profile], auxiliary={
        "mode": route, "provider": "local", "model": "synthetic-model",
    })
    execution = resolve_auxiliary(config)
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":'
                              '"{\\"operations\\":[{\\"op\\":\\"no_change\\"}]}"},'
                              '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(execution.profile(), client=client)
        output = provider.maintenance_output(task_id)
        events = [event async for event in provider.stream(
            [{"role": "user", "content": "Synthetic maintenance JSON"}],
            presentation="plain", output=output,
        )]
    wire = payloads[0]["response_format"]
    assert wire["type"] == "json_schema" and wire["json_schema"]["strict"] is True
    assert wire["json_schema"]["name"].startswith("current_life_")
    assert "eidolon_presentation" not in json.dumps(wire)
    schema = wire["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"operations": [{"op": "no_change"}]})
    assert not {"oneOf", "discriminator", "default", "minLength", "maxLength", "pattern"} & set(
        key for node in walk(schema) for key in node)
    for node in walk(schema):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
    assert output.provenance()["output_mode"] == "json_schema"
    assert output.provenance()["capability_source"] == "configured_verification"
    assert events[-1] == {"type": "finish", "reason": "stop"}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@pytest.mark.parametrize("support,mode,source", [
    ({"json_schema": "unsupported", "json_object": True}, "json_object", "configured_verification"),
    ({"json_schema": "ignored", "json_object": False}, "prompt", "configured_verification"),
    ({}, "prompt", "configured_verification"),
    ({"model": "another-model", "json_schema": "enforced"}, "prompt", "unknown"),
    ({"endpoint": "https://other.example/v1", "json_schema": "enforced"}, "prompt", "unknown"),
    ({"reasoning_effort": "low", "json_schema": "enforced"}, "prompt", "unknown"),
    ({"thinking_mode": "adaptive", "json_schema": "enforced"}, "prompt", "unknown"),
])
async def test_output_support_is_route_and_mode_specific(support, mode, source):
    profile = profile_with_support(**support)
    profile.presentation = "json"
    profile.structured_verified = True
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},'
                              '"finish_reason":"stop"}]}\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(profile, client=client)
        output = provider.maintenance_output("ordinary")
        for _ in range(2):
            _ = [event async for event in provider.stream([], output=output)]
        assert provider.maintenance_output("ordinary").mode == mode
    assert len(payloads) == 2  # No paid capability probes or hidden retries.
    assert output.mode == mode and output.capability_source == source
    assert payloads[0].get("response_format") == (
        {"type": "json_object"} if mode == "json_object" else None)


def test_documented_ignored_transport_and_conflicting_records_cannot_claim_enforcement():
    profile = profile_with_support(endpoint="https://api.anthropic.com/v1", json_schema="enforced")
    profile.endpoint = "https://api.anthropic.com/v1"
    output = OpenAIProvider(profile).maintenance_output("ordinary")
    assert output.mode == "prompt" and output.capability_source == "documented_ignored"
    profile = profile_with_support(json_schema="enforced")
    profile.maintenance_output_support.append(profile.maintenance_output_support[0].model_copy())
    assert OpenAIProvider(profile).maintenance_output("ordinary").mode == "prompt"


@pytest.mark.parametrize("parameter,code,next_mode", [
    ("response_format", "unsupported_parameter", "prompt"),
    ("response_format.json_schema", "unsupported_value", "json_object"),
])
async def test_known_unsupported_format_is_not_reprobed_after_adapter_replacement(
    parameter, code, next_mode,
):
    from eidolon.providers.maintenance import OutputSupportCache
    from eidolon.providers.openai import ProviderCapabilityError

    profile = profile_with_support(json_schema="enforced", json_object=True)
    cache = OutputSupportCache()
    payloads = []

    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"param": parameter,
                                  "code": code, "message": "Private body"}})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},'
                              '"finish_reason":"stop"}]}\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(profile, client=client, output_support_cache=cache)
        with pytest.raises(ProviderCapabilityError, match="rejected request options"):
            _ = [event async for event in provider.stream(
                [], output=provider.maintenance_output("ordinary"))]
        replacement = OpenAIProvider(profile, client=client, output_support_cache=cache)
        output = replacement.maintenance_output("ordinary")
        assert output.mode == next_mode and output.capability_source == "provider_rejected"
        _ = [event async for event in replacement.stream([], output=output)]
    assert len(payloads) == 2
    profile.model = "different-model"
    assert replacement.maintenance_output("ordinary").capability_source == "unknown"
