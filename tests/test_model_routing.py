"""Independent routing, portable payloads and privacy-safe execution snapshots."""
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

from eidolon.config import AppConfig, AuxiliaryModelConfig, ModelMetadata, ProviderConfig
from eidolon.providers.openai import OpenAIProvider
from eidolon.providers.routing import (
    AuxiliaryConfigurationError, configured_route_fingerprint, resolve_auxiliary,
)


def configuration(**auxiliary):
    return AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[ProviderConfig(
        model="dialogue", context_window=128000, presentation="json", structured_verified=True,
        parameters={"stop": ["END"], "temperature": .8, "reasoning_effort": "high",
                    "max_completion_tokens": 16000},
    )], auxiliary=AuxiliaryModelConfig(**auxiliary))


def test_legacy_inheritance_preserves_profile_and_memory_choices():
    config = configuration()
    before = config.model_dump()
    execution = resolve_auxiliary(config, 7)
    profile = execution.profile()
    assert profile.model == "dialogue" and profile.context_window_for() == 128000
    assert profile.parameters == {"reasoning_effort": "high", "max_completion_tokens": 4096}
    assert execution.context_chars is None and execution.routing_revision == 7
    assert not profile.tools and profile.presentation == "plain"
    assert config.model_dump() == before
    assert not config.memory.continuity_enabled and not config.memory.continuity_use_enabled
    with pytest.raises(FrozenInstanceError):
        execution.model = "changed"
    profile.parameters["temperature"] = 0
    assert "temperature" not in execution.profile().parameters


def test_separate_model_has_independent_options_and_capacity():
    config = configuration(mode="separate", provider="local", model="auxiliary")
    execution = resolve_auxiliary(config)
    assert execution.model == "auxiliary" and execution.context_window is None
    assert execution.context_chars is None and execution.stream_usage
    assert execution.profile().parameters == {"max_tokens": 4096}
    config.providers[0].model_metadata["auxiliary"] = ModelMetadata(
        context_window=24000, source="synthetic", updated_at=1,
    )
    assert resolve_auxiliary(config).context_window == 24000
    config.auxiliary.context_window = 16000
    assert resolve_auxiliary(config).context_window == 16000
    assert config.selected_provider().model == "dialogue"


@pytest.mark.parametrize("mode", ["inherit", "separate"])
def test_auxiliary_stream_usage_honors_connection_compatibility_opt_out(mode):
    config = configuration(mode=mode, provider="local", model="manual-auxiliary")
    connection = config.selected_provider()
    connection.parameters["stream_options"] = {"include_usage": False}
    execution = resolve_auxiliary(config)
    assert not execution.stream_usage
    profile = execution.profile()
    assert OpenAIProvider(profile, stream_usage=execution.stream_usage)._payload(
        [], None, "plain")["stream_options"] == {"include_usage": False}
    profile.parameters["stream_options"]["include_usage"] = True
    assert connection.parameters["stream_options"] == {"include_usage": False}


@pytest.mark.parametrize("mode", ["inherit", "separate"])
def test_explicit_thinking_mode_overrides_only_the_auxiliary_request(mode):
    config = configuration(mode=mode, provider="local", model="auxiliary",
                           thinking_mode="disabled")
    config.selected_provider().parameters["thinking"] = {"type": "adaptive"}
    execution = resolve_auxiliary(config)
    assert execution.profile().parameters["thinking"] == {"type": "disabled"}
    assert config.selected_provider().parameters["thinking"] == {"type": "adaptive"}
    assert execution.provenance()["request_options"]["thinking_mode"] == "disabled"


@pytest.mark.parametrize("field,value", [
    ("context_chars", 7999), ("context_chars", 1000001), ("context_window", 0),
    ("timeout_seconds", 0), ("timeout_seconds", 121), ("timeout_seconds", float("inf")),
    ("max_output_tokens", 127), ("max_output_tokens", 8193),
    ("output_token_field", "max_output_tokens"), ("model", " "), ("provider", "bad\nname"),
    ("model", "a" * 501), ("reasoning_effort", "a" * 81),
    ("thinking_mode", "enabled"), ("thinking_mode", {"type": "disabled"}),
])
def test_bounded_configuration(field, value):
    with pytest.raises(ValueError):
        AuxiliaryModelConfig.model_validate({field: value})


@pytest.mark.parametrize("auxiliary", [
    {"mode": "separate"}, {"mode": "separate", "provider": "local"},
    {"mode": "separate", "provider": "missing", "model": "manual-id"},
])
def test_broken_binding_is_isolated_from_app_validation(auxiliary):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, auxiliary=auxiliary)
    assert config.selected_provider()
    with pytest.raises(AuxiliaryConfigurationError):
        resolve_auxiliary(config)


def test_disabled_needs_no_connection_and_does_not_change_memory_controls():
    config = configuration(mode="disabled", provider="missing")
    before = config.memory.model_dump()
    assert resolve_auxiliary(config) is None
    assert config.memory.model_dump() == before


@pytest.mark.parametrize("parameters", [
    {"messages": []}, {"tools": []}, {"response_format": {}}, {"model": "override"},
    {"max_tokens": 1000, "max_completion_tokens": 1000}, {"max_output_tokens": 500},
])
def test_inherited_reserved_or_conflicting_parameters_fail_locally(parameters):
    config = configuration()
    config.providers[0].parameters = parameters
    with pytest.raises(AuxiliaryConfigurationError):
        resolve_auxiliary(config)


def test_fingerprint_tracks_only_configured_route_and_safe_snapshot_identity():
    config = configuration(mode="separate", provider="local", model="aux")
    baseline = configured_route_fingerprint(config)
    execution = resolve_auxiliary(config)
    config.selected_provider().model = "another-dialogue-model"
    config.selected_provider().context_window = 999999
    config.selected_provider().parameters["temperature"] = .1
    config.preferences.follow_replies = False
    config.memory.continuity_use_enabled = True
    config.selected_provider().models = ["aux"]
    config.selected_provider().model_metadata["aux"] = ModelMetadata(
        context_window=32000, source="synthetic", updated_at=1,
    )
    assert configured_route_fingerprint(config) == baseline
    assert resolve_auxiliary(config).execution_fingerprint != execution.execution_fingerprint
    config.selected_provider().headers["X-Test"] = "env:SYNTHETIC_HEADER"
    assert configured_route_fingerprint(config) != baseline
    provenance = json.dumps(resolve_auxiliary(config).provenance())
    assert "SYNTHETIC_HEADER" not in provenance and "headers" not in provenance
    assert "127.0.0.1" not in provenance and "stop" not in provenance


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("limit", [512, 8192])
@pytest.mark.parametrize("thinking_mode", [None, "disabled", "adaptive"])
async def test_exact_separate_payload_and_dispatch_time_credentials(field, limit, thinking_mode):
    config = configuration(mode="separate", provider="local", model="manual-auxiliary",
                           output_token_field=field, max_output_tokens=limit,
                           thinking_mode=thinking_mode)
    config.selected_provider().api_key_ref = "env:SYNTHETIC_KEY"
    config.selected_provider().headers["X-Synthetic"] = "env:SYNTHETIC_HEADER"
    execution = resolve_auxiliary(config)
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = OpenAIProvider(execution.profile(), client=client,
                                  credential_resolver=lambda ref: "synthetic-value",
                                  stream_usage=execution.stream_usage)
        events = [e async for e in provider.stream([{"role": "user", "content": "synthetic"}])]
    payload = json.loads(requests[0].content)
    assert payload == {"model": "manual-auxiliary", "messages": [{"role": "user", "content": "synthetic"}],
                       "stream": True, "stream_options": {"include_usage": True}, field: limit,
                       **({"thinking": {"type": thinking_mode}} if thinking_mode else {})}
    assert execution.provenance()["request_options"].get("thinking_mode") == thinking_mode
    assert requests[0].headers["Authorization"] == "Bearer synthetic-value"
    assert events[-1]["type"] == "finish"


async def test_distinct_source_backlogs_keep_evidence_identical_across_routes(tmp_path):
    """Offline application replay with scripted proposals, not a model quality score."""
    from datetime import datetime
    from pathlib import Path

    from eidolon.memory.continuity import source_is_fictional
    from eidolon.memory.maintenance import ContinuityWorker
    from eidolon.storage import Store

    fixture = json.loads((Path(__file__).parent / "fixtures/memory_baseline_companion.json").read_text())
    assert fixture["synthetic"] and len(fixture["probes"]) >= 60
    # Queries are not inputs to this offline worker replay. Cover each distinct
    # admitted source sequence once, rather than rebuilding it for every query.
    backlogs = {}
    for probe in fixture["probes"]:
        sources = tuple(source["id"] for source in fixture["sources"]
                        if datetime.fromisoformat(source["timestamp"])
                        <= datetime.fromisoformat(probe["timestamp"]))
        backlogs.setdefault(sources, probe)
    assert sorted(map(len, backlogs)) == [3, 12, 21]
    for source_ids, probe in backlogs.items():
        outcomes = []
        for mode in ("inherit", "separate"):
            config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, auxiliary=AuxiliaryModelConfig(
                mode=mode, provider="local", model="synthetic-auxiliary",
            ))
            store = Store(tmp_path / f"{probe['id']}-{mode}.sqlite3")
            try:
                for source in fixture["sources"]:
                    if source["id"] not in source_ids:
                        continue
                    session = store.session_for(datetime.fromisoformat(source["timestamp"]))
                    turn = store.complete_turn(session["id"], source["user"], source["assistant"],
                                               generation_id=source["id"], metadata={
                                                   "memory_scope": source["scope"],
                                                   "user_message_at": source["timestamp"],
                                               })
                    with store.db:
                        store.db.execute("UPDATE generations SET created_at=? WHERE id=?",
                                         (source["timestamp"], turn["id"]))
                        store.db.execute("UPDATE messages SET created_at=? WHERE generation_id=?",
                                         (source["timestamp"], turn["id"]))
                expected_watermark = store.recent_continuity_inputs(limit=1)[0]["id"]
                route = store.reconcile_auxiliary_route(configured_route_fingerprint(config))
                execution = resolve_auxiliary(config, route["revision"])
                class ScriptedProvider:
                    config = execution.profile()
                    async def stream(self, messages, **kwargs):
                        document = json.loads(messages[-1]["content"])
                        operations = [{
                            "op": "upsert", "category": "thread",
                            "topic": " ".join(row["user_content"].split())[:100],
                            "importance": "moderate",
                            "content": "Synthetic summary: " + row["user_content"],
                            "sources": [row["source_refs"]["user"]],
                        } for row in document["exchanges"] if not source_is_fictional(row["user_content"])]
                        yield {"type": "token", "text": json.dumps({"operations": operations or [{
                            "op": "no_change",
                        }]})}
                        yield {"type": "finish", "reason": "stop"}
                worker = ContinuityWorker(store, ScriptedProvider(), execution=execution,
                                          max_input_chars=execution.context_chars, max_notes=64)
                # Backlogs span multiple batches; the original contiguous evidence
                # must be preserved regardless of the role's selected model.
                # This routing replay has room for its complete synthetic corpus;
                # admission and replacement decisions have their own policy tests.
                passes = 0
                while await worker.run_once(force=True):
                    passes += 1
                    assert passes <= 4
                assert store.pending_maintenance_task() is None
                state = store.continuity_state()
                assert state["watermark"] == expected_watermark
                assert state["items"]
                assert all(not item["invalidated"] for item in state["items"])
                outcomes.append({"watermark": state["watermark"],
                                 "notes": sorted((item["content"], item["sources"][0]["document_id"])
                                                 for item in state["items"])})
                assert config.selected_provider().model == "configure-model"
            finally:
                store.close()
        assert outcomes[0] == outcomes[1], probe["id"]
