import json
import time

import httpx
import pytest

from eidolon.config import ModelMetadata, ProviderConfig
from eidolon.providers import OpenAIProvider
from eidolon.providers.metadata import (
    CATALOG_TTL, CATALOG_URL, ModelMetadataResolver, inventory_limits, lmstudio_limits, ollama_limit,
    inventory_vision, lmstudio_vision, server_root,
)


CATALOG = {
    "openai": {"models": {"test-model": {"limit": {"context": 1_000_000}},
                          "test-model-mini": {"limit": {"context": 128_000}}}},
    "other": {"api": "https://other.example/v1",
              "models": {"test-model": {"limit": {"context": 32_000}}}},
}


@pytest.mark.parametrize("field", ["context_length", "context_window", "max_model_len",
                                    "max_context_length", "loaded_context_length"])
def test_model_inventory_context_fields_and_exact_model_ids(field):
    assert inventory_limits([
        {"id": "model-a", field: 8192, "max_tokens": 1024},
        {"id": "model-b", "max_tokens": 4000},
        {"id": "model-c", field: True}, {"id": "model-d", field: -1},
        {"id": "model-e", field: "8192"}, {"id": None, field: 2000},
    ]) == {"model-a": 8192}


def test_loaded_context_and_openrouter_metadata_precedence():
    assert inventory_limits([
        {"id": "local", "loaded_context_length": 8192, "max_context_length": 131072},
        {"id": "router", "top_provider": {"context_length": 200000}},
    ]) == {"local": 8192, "router": 200000}
    assert lmstudio_limits({"models": [{
        "key": "model", "max_context_length": 131072, "loaded_instances": [
            {"id": "small-instance", "config": {"context_length": 8192}},
            {"id": "large-instance", "config": {"context_length": 32000}},
        ],
    }]}, ["model", "large-instance", "unrelated"]) == {"model": 8192, "large-instance": 32000}
    assert lmstudio_limits({"models": None}, ["model"]) == {}


def test_ollama_modelfile_window_precedes_architecture_capacity():
    packet = {"parameters": "num_ctx 8192\nstop <end>",
              "model_info": {"general.architecture": "llama", "llama.context_length": 131072}}
    assert ollama_limit(packet) == 8192
    packet["parameters"] = ""
    assert ollama_limit(packet) == 131072
    assert ollama_limit({"model_info": {"max_tokens": 4000}}) is None


@pytest.mark.parametrize("endpoint", [
    "http://localhost:1234/proxy/v1", "http://localhost:1234/proxy/api/v0",
    "http://localhost:1234/proxy/v1/chat/completions",
])
def test_native_metadata_probes_keep_gateway_path(endpoint):
    assert server_root(endpoint) == "http://localhost:1234/proxy"


async def test_local_runtime_beats_inventory_and_never_queries_cloud_catalog(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "localhost"
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/proxy/api/v1/models":
            return httpx.Response(200, json={"models": [{"key": "test-model",
                "max_context_length": 131072, "loaded_instances": [{"id": "test-model",
                    "config": {"context_length": 16384}}]}]})
        return httpx.Response(404)
    profile = ProviderConfig(endpoint="http://localhost:1234/proxy/v1", model="test-model",
                             api_key_ref="env:KEY")
    provider = OpenAIProvider(profile, credential_resolver=lambda _: "test-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                headers={"Authorization": "Bearer client-secret"}) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        result = await resolver.resolve(provider, ["test-model"], inventory=[
            {"id": "test-model", "context_length": 131072},
        ])
    assert result["test-model"].context_window == 16384
    assert result["test-model"].source == "LM Studio loaded model"
    assert len(requests) == 3 and not resolver.cache_path.exists()


async def test_ollama_running_and_unloaded_models_and_llamacpp_runtime(tmp_path):
    llama_only = False
    def handler(request):
        if llama_only and request.url.path != "/props":
            return httpx.Response(404)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": "running:latest", "context_length": 8192},
            ]})
        if request.url.path == "/api/show":
            assert json.loads(request.content)["model"] in {"running", "unloaded"}
            return httpx.Response(200, json={"parameters": "num_ctx 16384",
                                            "capabilities": ["completion", "vision"]})
        if request.url.path == "/props":
            return httpx.Response(200, json={"model_alias": "llama-model",
                                            "default_generation_settings": {"n_ctx": 32768}})
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        provider = OpenAIProvider(ProviderConfig(model="running"))
        result = await resolver.resolve(provider, ["running", "unloaded"], inventory=[])
        assert {key: info.context_window for key, info in result.items()} == {
            "running": 8192, "unloaded": 16384,
        }
        assert all(info.vision is True for info in result.values())
        # This single-model server has no Ollama model listing.
        llama_only = True
        result = await resolver.resolve(provider, ["llama-model", "different-model"], inventory=[])
        assert set(result) == {"llama-model"}
        assert result["llama-model"].context_window == 32768


async def test_catalog_lookup_is_provider_specific_cached_and_never_receives_credentials(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == CATALOG_URL
        assert "Authorization" not in request.headers and "X-API-Key" not in request.headers
        return httpx.Response(200, json=CATALOG)
    profile = ProviderConfig(name="my-api", endpoint="https://api.openai.com/v1",
                             model="test-model", api_key_ref="env:KEY",
                             headers={"X-API-Key": "env:KEY"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                headers={"Authorization": "Bearer client-secret", "X-API-Key": "secret"},
                                auth=("username", "password")) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        result = await resolver.resolve(OpenAIProvider(profile), ["test-model"], inventory=[])
        assert result["test-model"].context_window == 1_000_000
        other = ProviderConfig(name="openai", endpoint="https://other.example/v1", model="test-model")
        result = await resolver.resolve(OpenAIProvider(other), ["test-model"], inventory=[])
        assert result["test-model"].context_window == 32000
        # A new resolver reuses the private on-disk catalog without another download.
        resolver = ModelMetadataResolver(resolver.cache_path, client=client)
        result = await resolver.resolve(OpenAIProvider(profile), ["test-model"], inventory=[])
        assert result["test-model"].source == "models.dev/openai"
        assert len(requests) == 1
        assert resolver.cache_path.stat().st_mode & 0o777 == 0o600
        assert "env:KEY" not in resolver.cache_path.read_text()


async def test_endpoint_metadata_wins_and_unknown_models_do_not_fuzzy_match(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=CATALOG),
    )) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        profile = ProviderConfig(endpoint="https://api.openai.com/v1", model="test-model")
        result = await resolver.resolve(OpenAIProvider(profile),
            ["test-model", "test-model-mini", "test-model-unknown"],
            inventory=[{"id": "test-model", "context_length": 65536}])
        assert result["test-model"].context_window == 65536
        assert result["test-model-mini"].context_window == 128000
        assert "test-model-unknown" not in result
        profile.endpoint = "https://unrecognized.example/v1"
        assert await resolver.resolve(OpenAIProvider(profile), ["test-model"], inventory=[]) == {}


async def test_expired_catalog_can_refresh_or_remain_available_offline(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"updated_at": time.time() - CATALOG_TTL - 1,
        "providers": {"openai": {"limits": {"test-model": 8192}}}}))
    requests = []
    def offline(request):
        requests.append(request)
        raise httpx.ConnectError("offline")
    profile = ProviderConfig(endpoint="https://api.openai.com/v1", model="test-model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
        resolver = ModelMetadataResolver(path, client=client)
        for _ in range(2):
            result = await resolver.resolve(OpenAIProvider(profile), ["test-model"], inventory=[])
            assert result["test-model"].context_window == 8192
        assert len(requests) == 1  # Failed public refresh has a cooldown.


def test_manual_override_and_model_specific_limits_do_not_cross_models():
    profile = ProviderConfig(model="a", context_window=4096, model_metadata={
        "a": ModelMetadata(context_window=8192, source="provider", updated_at=time.time()),
        "b": ModelMetadata(context_window=32768, source="provider", updated_at=time.time()),
    })
    assert profile.context_window_for() == 4096
    assert profile.context_window_for("b") == 32768
    assert profile.context_window_for("unknown") is None


@pytest.mark.parametrize("metadata, expected", [
    ({"architecture": {"input_modalities": ["text", "image"]}}, True),
    ({"architecture": {"input_modalities": ["text"]}}, False),
    ({"architecture": {"modality": "text+image->text"}}, True),
    ({"architecture": {"modality": "text->image"}}, False),
    ({"modalities": {"input": ["text", "image"], "output": ["text"]}}, True),
    ({"modalities": {"input": ["text"], "output": ["image"]}}, False),
    ({"capabilities": ["completion", "vision"]}, True),
    ({"capabilities": ["completion", "tools"]}, False),
    ({"capabilities": {"vision": True}}, True),
    ({"capabilities": {"vision": False}}, False),
    ({"vision": True}, True),
    ({"supports_vision": False}, False),
    ({"type": "vlm"}, True),
    ({"vision": "true", "capabilities": [True]}, None),
    ({"architecture": {"input_modalities": "text,image"}}, None),
    ({"name": "famous-vision-image-vl-model"}, None),
])
def test_vision_inventory_uses_explicit_input_metadata(metadata, expected):
    inventory = [{"id": "exact-model", **metadata}, {"id": None, "vision": True}]
    expected_result = {} if expected is None else {"exact-model": expected}
    assert inventory_vision(inventory) == expected_result


def test_lmstudio_vision_matches_loaded_ids_without_guessing_aliases():
    packet = {"models": [{"key": "vision-model", "capabilities": {"vision": True},
                           "loaded_instances": [{"id": "custom-instance"}]},
                          {"key": "text-model", "capabilities": {"vision": False}}]}
    assert lmstudio_vision(packet, ["custom-instance", "vision-model", "text-model",
                                    "vision-model-v2"]) == {
        "custom-instance": True, "vision-model": True, "text-model": False,
    }


async def test_catalog_preserves_vision_without_context_and_endpoint_false_wins(tmp_path):
    packet = {"openai": {"models": {
        "image-model": {"modalities": {"input": ["text", "image"]}},
        "text-model": {"modalities": {"input": ["text"]}, "limit": {"context": 8192}},
        "no-metadata": {},
    }}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=packet),
    )) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        provider = OpenAIProvider(ProviderConfig(endpoint="https://api.openai.com/v1"))
        result = await resolver.resolve(provider, ["image-model", "text-model", "no-metadata"],
                                        inventory=[])
        assert result["image-model"].context_window is None
        assert result["image-model"].vision is True
        assert result["text-model"].vision is False
        assert "no-metadata" not in result
        result = await resolver.resolve(provider, ["image-model"], inventory=[
            {"id": "image-model", "context_length": 4096,
             "architecture": {"input_modalities": ["text"]}},
        ])
        assert result["image-model"].vision is False
        assert result["image-model"].context_window == 4096
        # The compact private cache retains both true and false capability facts.
        resolver = ModelMetadataResolver(resolver.cache_path, client=client)
        result = await resolver.resolve(provider, ["image-model", "text-model"], inventory=[])
        assert result["image-model"].vision is True and result["text-model"].vision is False


async def test_lmstudio_native_vision_metadata_without_context_stays_local(tmp_path):
    def handler(request):
        assert request.url.host == "localhost"
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [
                {"key": "local-model", "capabilities": {"vision": False},
                 "loaded_instances": [{"id": "my-instance"}]},
            ]})
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        result = await resolver.resolve(OpenAIProvider(ProviderConfig(
            endpoint="http://localhost:1234/v1", model="my-instance",
        )), ["my-instance"], inventory=[])
    assert result["my-instance"].vision is False
    assert result["my-instance"].context_window is None
    assert not resolver.cache_path.exists()


async def test_partial_discovery_preserves_other_known_metadata_fields(tmp_path):
    profile = ProviderConfig(endpoint="https://example.org/v1", model="test-model",
                             model_metadata={"test-model": ModelMetadata(
                                 context_window=8192, source="old metadata", updated_at=0,
                             )})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(404),
    )) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        result = await resolver.resolve(OpenAIProvider(profile), ["test-model"], inventory=[
            {"id": "test-model", "vision": True},
        ])
        assert result["test-model"].context_window == 8192
        assert result["test-model"].vision is True
        profile.model_metadata = result
        result = await resolver.resolve(OpenAIProvider(profile), ["test-model"], inventory=[
            {"id": "test-model", "context_length": 16384},
        ])
        assert result["test-model"].context_window == 16384
        assert result["test-model"].vision is True


async def test_pre_vision_catalog_cache_upgrades_without_waiting_for_ttl(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"updated_at": time.time(), "providers": {
        "openai": {"limits": {"test-model": 8192}},
    }}))
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"openai": {"models": {
            "test-model": {"modalities": {"input": ["text", "image"]},
                           "limit": {"context": 8192}},
        }}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ModelMetadataResolver(path, client=client)
        result = await resolver.resolve(OpenAIProvider(ProviderConfig(
            endpoint="https://api.openai.com/v1", model="test-model",
        )), ["test-model"], inventory=[])
    assert result["test-model"].vision is True
    assert len(requests) == 1 and json.loads(path.read_text())["version"] == 2


async def test_named_ollama_can_discover_vision_when_running_inventory_is_unavailable(tmp_path):
    def handler(request):
        assert request.url.host == "ollama.example"
        if request.url.path == "/api/show":
            assert json.loads(request.content) == {"model": "chosen"}
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(404)
    profile = ProviderConfig(name="ollama", endpoint="https://ollama.example/v1", model="chosen")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = ModelMetadataResolver(tmp_path / "catalog.json", client=client)
        result = await resolver.resolve(OpenAIProvider(profile), ["chosen"], inventory=[])
    assert result["chosen"].vision is True
    assert not resolver.cache_path.exists()
