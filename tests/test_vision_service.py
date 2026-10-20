"""Vision role saves and synthetic checks through the real service and private state."""
import asyncio
import json

import pytest

from eidolon.config import ModelMetadata, load_config, save_config
from eidolon.tools.vision import resolve_vision
from test_service import room


VISION_BINDING = {"mode": "separate", "provider": "second", "model": "synthetic/vision"}


async def test_combined_vision_auxiliary_preferences_merge_latest_in_one_save(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        latest = load_config(service.configuration)
        latest.selected_provider().parameters = {"temperature": 0.7, "max_tokens": 10000}
        latest.vision.max_output_tokens = 2048
        latest.theme.name = "nord"
        save_config(latest, service.configuration)
        persisted = []
        persist = service._persist_configuration

        def record(config, path):
            persisted.append(config.model_copy(deep=True))
            persist(config, path)

        monkeypatch.setattr(service, "_persist_configuration", record)
        result = await service.handle("save_preferences_with_vision", {
            "preferences": {"follow_replies": False},
            "auxiliary": {"mode": "separate", "provider": "first", "model": "synthetic/aux"},
            "vision": VISION_BINDING,
        })
        saved = load_config(service.configuration)
        assert len(persisted) == 1
        assert saved.model_dump(mode="json") == result
        assert saved.vision.max_output_tokens == 2048
        assert saved.vision.model == "synthetic/vision" and saved.auxiliary.model == "synthetic/aux"
        assert not saved.preferences.follow_replies and saved.theme.name == "nord"
        assert saved.selected_provider().model == "first-model"
        assert saved.selected_provider().parameters == {"temperature": 0.7, "max_tokens": 10000}
        assert resolve_vision(saved).parameters == {"max_tokens": 2048}


async def test_vision_only_save_preserves_unrelated_adapters_and_auxiliary_route(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        engine, memory, mcp = service.engine, service.memory, service.mcp
        worker, revision = service.continuity_worker, service.store.auxiliary_route()["revision"]
        await service.handle("save_vision_model", {"vision": VISION_BINDING})
        assert (service.engine, service.memory, service.mcp) == (engine, memory, mcp)
        assert service.continuity_worker is worker
        assert service.store.auxiliary_route()["revision"] == revision


@pytest.mark.parametrize("preferences,auxiliary,vision", [
    ({"follow_replies": False}, {"mode": "disabled"}, VISION_BINDING | {"provider": "missing"}),
    ({"follow_replies": False}, {"mode": "disabled"}, VISION_BINDING | {"max_output_tokens": 8193}),
    ({"follow_replies": False}, {"mode": "separate", "provider": "missing"}, VISION_BINDING),
    ({"reset_hour": 24}, {"mode": "disabled"}, VISION_BINDING),
])
async def test_invalid_combined_save_preserves_all_categories(
    tmp_path, monkeypatch, preferences, auxiliary, vision,
):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        before = service.configuration.read_bytes()
        with pytest.raises(ValueError):
            await service.handle("save_preferences_with_vision", {
                "preferences": preferences, "auxiliary": auxiliary, "vision": vision,
            })
        assert service.configuration.read_bytes() == before
        assert service.config.vision.mode == "disabled"
        assert service.config.auxiliary.mode == "inherit"
        assert service.config.preferences.follow_replies


async def test_stale_settings_and_dialogue_selection_preserve_separate_vision(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        stale = service.config.model_dump(mode="json")
        revision = service.store.auxiliary_route()["revision"]
        await service.handle("save_vision_model", {
            "vision": VISION_BINDING | {"context_window": 64000},
        })
        assert service.store.auxiliary_route()["revision"] == revision
        await service.handle("save_settings", {"config": stale})
        await service.handle("select_model", {"name": "first", "model": "new-dialogue"})
        saved = load_config(service.configuration)
        assert saved.vision.model == "synthetic/vision"
        assert saved.vision.provider == "second" and saved.vision.context_window == 64000
        await service.handle("save_vision_model", {"vision": {"model": "new-vision"}})
        assert load_config(service.configuration).vision.context_window is None


async def test_vision_save_applies_after_current_generation_without_mutating_its_config(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        original = service.engine.config
        async with service.generation_lock:
            result = await service.handle("save_vision_model", {"vision": VISION_BINDING})
            assert result["vision"]["model"] == "synthetic/vision"
            assert original.vision.mode == "disabled"
            assert service.engine.config is original
        await service._reload_connections()
        assert service.engine.config.vision.model == "synthetic/vision"
        assert original.vision.mode == "disabled"


async def test_connection_change_clears_vision_capacity_and_metadata(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        await service.handle("save_vision_model", {
            "vision": VISION_BINDING | {"context_window": 64000},
        })
        candidate = load_config(service.configuration)
        profile = candidate.providers[1]
        profile.model_metadata["synthetic/vision"] = ModelMetadata(
            context_window=64000, vision=True, source="test", updated_at=0,
        )
        save_config(candidate, service.configuration)
        profile.endpoint = "http://localhost:1240/v1"
        service._persist_configuration(candidate, service.configuration)
        saved = load_config(service.configuration)
        assert saved.vision.context_window is None
        assert saved.providers[1].model_metadata == {}
        resolved = resolve_vision(saved)
        assert resolved.endpoint == "http://localhost:1240/v1"
        assert resolved.context_window_for() is None


async def test_synthetic_vision_uses_draft_options_without_private_evidence_or_persistence(
    tmp_path, monkeypatch,
):
    profiles, requests = [], []

    class SyntheticProvider:
        def __init__(self, config, **kwargs):
            profiles.append((config, kwargs))

        async def supports_vision(self):
            return True

        async def stream(self, messages, **kwargs):
            requests.append((messages, kwargs))
            yield {"type": "token", "text": "red"}
            yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr("eidolon.tools.vision.OpenAIProvider", SyntheticProvider)
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        service.store.complete_turn(service.current_session["id"],
                                    "Private marker absent from synthetic check.", "Acknowledged.")
        before_file = service.configuration.read_bytes()
        before_messages = service.store.list_messages(service.current_session["id"])
        result = await service.handle("test_vision_model", {
            "vision": VISION_BINDING | {
                "timeout_seconds": 12, "max_output_tokens": 2048, "reasoning_effort": "low",
                "thinking_mode": "disabled", "output_token_field": "max_completion_tokens",
            },
        })
        assert result["ok"]
        assert service.configuration.read_bytes() == before_file
        assert service.store.list_messages(service.current_session["id"]) == before_messages
        assert service.store.maintenance_status()["model_calls"] == 0
        assert service.store.continuity_state()["watermark"] == 0
        assert service._continuity_task is None and not service._auxiliary_applying
        profile, options = profiles[0]
        assert profile.name == "second" and profile.model == "synthetic/vision"
        assert profile.parameters == {"max_completion_tokens": 2048, "reasoning_effort": "low",
                                      "thinking": {"type": "disabled"}}
        assert options == {"timeout": 12, "stream_usage": True}
        assert "Private marker" not in json.dumps(requests)
        assert "data:image/png;base64," in json.dumps(requests)
        assert requests[0][1] == {"tools": None, "presentation": "plain"}


async def test_synthetic_vision_preempts_for_foreground_and_closes_stream(tmp_path, monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()

    class SlowProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def supports_vision(self):
            return True

        async def stream(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                closed.set()

    monkeypatch.setattr("eidolon.tools.vision.OpenAIProvider", SlowProvider)
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        testing = asyncio.create_task(service.handle("test_vision_model", {"vision": VISION_BINDING}))
        await asyncio.wait_for(started.wait(), 5)
        assert not service.generation_lock.locked()
        async with service._foreground_generation():
            assert closed.is_set()
            assert not service._start_continuity(force=True)
        result = await testing
        assert not result["ok"] and "interrupted" in result["notice"]
        assert service._continuity_task is None


@pytest.mark.parametrize("method", ["save_vision_model", "save_preferences_with_vision"])
async def test_synthetic_vision_is_cancelled_when_vision_settings_apply(
    tmp_path, monkeypatch, method,
):
    started, closed = asyncio.Event(), asyncio.Event()

    class SlowProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def supports_vision(self):
            return True

        async def stream(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                closed.set()

    monkeypatch.setattr("eidolon.tools.vision.OpenAIProvider", SlowProvider)
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        testing = asyncio.create_task(service.handle("test_vision_model", {"vision": VISION_BINDING}))
        await asyncio.wait_for(started.wait(), 5)
        params = {"vision": VISION_BINDING | {"model": "synthetic/updated"}}
        if method == "save_preferences_with_vision":
            params["preferences"] = {"follow_replies": False}
        await service.handle(method, params)
        result = await asyncio.wait_for(testing, 5)
        assert closed.is_set()
        assert not result["ok"] and "interrupted" in result["notice"]
        assert load_config(service.configuration).vision.model == "synthetic/updated"


async def test_known_nonvision_synthetic_target_fails_without_sending_image(tmp_path, monkeypatch):
    class NonVisionProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def supports_vision(self):
            return False

        async def stream(self, *args, **kwargs):
            raise AssertionError("A model known to reject images must not receive them")
            yield {}

    monkeypatch.setattr("eidolon.tools.vision.OpenAIProvider", NonVisionProvider)
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        result = await service.handle("test_vision_model", {"vision": VISION_BINDING})
        assert not result["ok"]
        assert service.config.vision.mode == "disabled"
