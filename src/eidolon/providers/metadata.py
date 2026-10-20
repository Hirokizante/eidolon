"""Endpoint-aware context discovery and a private cache of the public model catalog."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import httpx

from eidolon.config import ModelMetadata, atomic_write

if TYPE_CHECKING:
    from eidolon.providers.openai import OpenAIProvider


CATALOG_URL = "https://models.dev/api.json"
CATALOG_TTL = 86400
LOCAL_TTL = 30
CLOUD_HOSTS = {
    "api.openai.com": "openai", "api.anthropic.com": "anthropic",
    "openrouter.ai": "openrouter", "api.deepseek.com": "deepseek",
    "api.mistral.ai": "mistral", "api.groq.com": "groq", "api.x.ai": "xai",
    "api.cerebras.ai": "cerebras", "api.together.xyz": "togetherai",
    "api.fireworks.ai": "fireworks-ai", "api.moonshot.ai": "moonshotai",
    "api.moonshot.cn": "moonshotai-cn", "api.minimax.io": "minimax",
}


def positive_limit(value: object) -> int | None:
    if type(value) is int and 0 < value <= 1_000_000_000:
        return value
    return None


def local_endpoint(endpoint: str) -> bool:
    host = urlsplit(endpoint).hostname or ""
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def server_root(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/").removesuffix("/chat/completions")
    for suffix in ("/api/v1", "/api/v0", "/v1"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit(parts._replace(path=path, query="", fragment="")).rstrip("/")


def inventory_limits(inventory: list[dict]) -> dict[str, int]:
    """Only documented context fields, never output caps or fuzzy model names."""
    result = {}
    for item in inventory:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        candidates = [item.get(key) for key in (
            "loaded_context_length", "context_length", "context_window", "max_model_len",
            "max_context_length",
        )]
        top = item.get("top_provider")
        if isinstance(top, dict):
            candidates.append(top.get("context_length"))
        limit = next((limit for value in candidates if (limit := positive_limit(value))), None)
        if limit:
            result[item["id"]] = limit
    return result


def vision_support(item: dict) -> bool | None:
    """Read explicit image-input metadata; names never imply capabilities."""
    for key in ("vision", "supports_vision"):
        if type(item.get(key)) is bool:
            return item[key]
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict) and type(capabilities.get("vision")) is bool:
        return capabilities["vision"]
    if isinstance(capabilities, list) and all(isinstance(value, str) for value in capabilities):
        return "vision" in capabilities
    architecture = item.get("architecture")
    modalities = item.get("modalities")
    inputs = [item.get("input_modalities")]
    if isinstance(architecture, dict):
        inputs.append(architecture.get("input_modalities"))
    if isinstance(modalities, dict):
        inputs.append(modalities.get("input"))
    for values in inputs:
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            return "image" in values
    # OpenRouter's older inventory represents modalities as text+image->text.
    modality = architecture.get("modality") if isinstance(architecture, dict) else None
    if isinstance(modality, str) and "->" in modality:
        values = modality.split("->", 1)[0].split("+")
        if values and all(value in {"text", "image", "audio", "video", "file"}
                          for value in values):
            return "image" in values
    # LM Studio's v0 API identifies vision models explicitly as VLMs.
    if item.get("type") == "vlm":
        return True
    return None


def inventory_vision(inventory: list[dict]) -> dict[str, bool]:
    return {item["id"]: supported for item in inventory
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and (supported := vision_support(item)) is not None}


def lmstudio_vision(packet: dict, model_ids: list[str]) -> dict[str, bool]:
    result = {}
    items = packet.get("models")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        supported = vision_support(item)
        if supported is None:
            continue
        aliases = {item["key"]}
        instances = item.get("loaded_instances")
        if isinstance(instances, list):
            aliases.update(instance["id"] for instance in instances if isinstance(instance, dict)
                           and isinstance(instance.get("id"), str))
        for model in model_ids:
            if model in aliases:
                result[model] = supported
    return result


def lmstudio_limits(packet: dict, model_ids: list[str], *, loaded_only: bool = False) -> dict[str, int]:
    result = {}
    items = packet.get("models")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        loaded = []
        instances = item.get("loaded_instances")
        for instance in instances if isinstance(instances, list) else []:
            if not isinstance(instance, dict):
                continue
            config = instance.get("config") or {}
            limit = positive_limit(config.get("context_length")) if isinstance(config, dict) else None
            if limit:
                loaded.append(limit)
                if instance.get("id") in model_ids:
                    result[instance["id"]] = limit
        limit = min(loaded) if loaded else (
            None if loaded_only else positive_limit(item.get("max_context_length"))
        )
        if limit and item["key"] in model_ids and item["key"] not in result:
            result[item["key"]] = limit
    return result


def ollama_limit(packet: dict) -> int | None:
    parameters = packet.get("parameters")
    if isinstance(parameters, str):
        match = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", parameters)
        if match and (limit := positive_limit(int(match[1]))):
            return limit
    info = packet.get("model_info")
    if isinstance(info, dict):
        architecture = info.get("general.architecture")
        if isinstance(architecture, str) and (limit := positive_limit(
            info.get(architecture + ".context_length"),
        )):
            return limit
    return None


class ModelMetadataResolver:
    def __init__(self, cache_path: Path, *, client: httpx.AsyncClient | None = None):
        self.cache_path, self.client = cache_path, client
        self._catalog: dict | None = None
        self._catalog_attempt = 0.0
        self._catalog_lock = asyncio.Lock()

    async def _json(self, client: httpx.AsyncClient, method: str, url: str,
                    headers: dict, **kwargs) -> dict:
        try:
            if url == CATALOG_URL:
                request = httpx.Request("GET", url, headers={"Accept": "application/json"},
                    extensions={"timeout": {key: 3 for key in ("connect", "read", "write", "pool")}})
                response = await client.send(request, auth=None, follow_redirects=False)
            else:
                response = await client.request(method, url, headers=headers, timeout=3,
                                                follow_redirects=False, **kwargs)
            if response.status_code == 200 and len(response.content) <= 16_000_000:
                packet = response.json()
                if isinstance(packet, dict):
                    return packet
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return {}

    async def _load_catalog(self, client: httpx.AsyncClient) -> dict:
        async with self._catalog_lock:
            if self._catalog is None:
                try:
                    self._catalog = json.loads(await asyncio.to_thread(self.cache_path.read_text))
                    if (not isinstance(self._catalog, dict)
                            or not isinstance(self._catalog.get("providers"), dict)
                            or type(self._catalog.get("updated_at")) not in (int, float)
                            or not math.isfinite(self._catalog["updated_at"])):
                        self._catalog = {}
                except (OSError, ValueError):
                    self._catalog = {}
            now = time.time()
            fetched = self._catalog.get("updated_at", 0)
            if (isinstance(fetched, (int, float)) and 0 <= now - fetched < CATALOG_TTL
                    and self._catalog.get("version") == 2):
                return self._catalog.get("providers", {})
            if self._catalog_attempt and time.monotonic() - self._catalog_attempt < 300:
                return self._catalog.get("providers", {})
            self._catalog_attempt = time.monotonic()
            # Public catalog requests never receive provider headers or credentials.
            packet = await self._json(client, "GET", CATALOG_URL, {})
            providers = {}
            for name, item in packet.items():
                if not isinstance(item, dict) or not isinstance(item.get("models"), dict):
                    continue
                limits, visions = {}, {}
                for model, details in item["models"].items():
                    limit = details.get("limit") if isinstance(details, dict) else None
                    value = positive_limit(limit.get("context")) if isinstance(limit, dict) else None
                    if value:
                        limits[model] = value
                    if isinstance(details, dict) and (supported := vision_support(details)) is not None:
                        visions[model] = supported
                providers[name] = {"api": item.get("api", ""), "limits": limits,
                                   "vision": visions}
            if providers:
                self._catalog = {"version": 2, "updated_at": now, "providers": providers}
                try:
                    await asyncio.to_thread(atomic_write, self.cache_path, json.dumps(self._catalog))
                except OSError:
                    pass  # An unwritable cache must not prevent discovery or chat.
            return self._catalog.get("providers", {})

    async def resolve(self, provider: OpenAIProvider, model_ids: list[str], *,
                      inventory: list[dict] | None = None,
                      api_key: str | None = None) -> dict[str, ModelMetadata]:
        profile = provider.config
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=3, follow_redirects=False)
        try:
            if inventory is None:
                try:
                    await provider.list_models(api_key=api_key, timeout=3)
                    inventory = provider.model_inventory
                except Exception:
                    inventory = []
            now = time.time()
            result: dict[str, ModelMetadata] = {}

            def merge(model: str, source: str, *, limit: int | None = None,
                      vision: bool | None = None, updated_at: float = now) -> None:
                previous = result.get(model) or profile.model_metadata.get(model)
                if limit is None and vision is None:
                    return
                result[model] = ModelMetadata(
                    context_window=limit if limit is not None else (
                        previous.context_window if previous else None),
                    vision=vision if vision is not None else (previous.vision if previous else None),
                    source=source, updated_at=updated_at,
                )

            for model, limit in inventory_limits(inventory).items():
                merge(model, "provider /models", limit=limit)
            for model, supported in inventory_vision(inventory).items():
                merge(model, "provider /models", vision=supported)
            if local_endpoint(profile.endpoint) or profile.name.lower() in {
                "lmstudio", "ollama", "llama.cpp", "llamacpp",
            }:
                headers = await asyncio.to_thread(provider._headers, api_key=api_key)
                root = server_root(profile.endpoint)
                lm, ollama, llama = await asyncio.gather(
                    self._json(client, "GET", root + "/api/v1/models", headers),
                    self._json(client, "GET", root + "/api/ps", headers),
                    self._json(client, "GET", root + "/props", headers),
                )
                for model, limit in lmstudio_limits(lm, model_ids, loaded_only=True).items():
                    merge(model, "LM Studio loaded model", limit=limit)
                for model, limit in lmstudio_limits(lm, model_ids).items():
                    if model not in result or result[model].context_window is None:
                        merge(model, "LM Studio model metadata", limit=limit)
                for model, supported in lmstudio_vision(lm, model_ids).items():
                    source = result[model].source if model in result else "LM Studio model metadata"
                    merge(model, source, vision=supported)
                running = ollama.get("models")
                for item in running if isinstance(running, list) else []:
                    if not isinstance(item, dict):
                        continue
                    for model in model_ids:
                        aliases = {model, model + ":latest"} if ":" not in model else {model}
                        limit = positive_limit(item.get("context_length"))
                        if limit and any(isinstance(item.get(key), str) and item[key] in aliases
                                         for key in ("name", "model")):
                            merge(model, "Ollama running model", limit=limit,
                                  vision=vision_support(item))
                if isinstance(running, list) or profile.name.lower() == "ollama":
                    semaphore = asyncio.Semaphore(4)

                    async def show_model(model: str) -> tuple[str, dict]:
                        async with semaphore:
                            packet = await self._json(client, "POST", root + "/api/show", headers,
                                                      json={"model": model})
                            return model, packet

                    shown = await asyncio.gather(*(show_model(model) for model in model_ids
                        if model not in result or result[model].context_window is None
                        or result[model].vision is None))
                    for model, packet in shown:
                        previous = result.get(model)
                        limit = ollama_limit(packet) if previous is None or (
                            previous.context_window is None) else None
                        supported = vision_support(packet)
                        source = previous.source if previous and previous.context_window else (
                            "Ollama model metadata")
                        merge(model, source, limit=limit, vision=supported)
                settings = llama.get("default_generation_settings")
                limit = positive_limit(settings.get("n_ctx")) if isinstance(settings, dict) else None
                # /props describes one server; do not assign its window to unrelated models.
                aliases = {item.get("id") for item in inventory if isinstance(item, dict)}
                for model in model_ids:
                    if limit and (llama.get("model_alias") == model or aliases == {model}):
                        merge(model, "llama.cpp runtime", limit=limit)
                return result  # Local runtime limits must not inherit a cloud catalog's capacity.
            missing = [model for model in model_ids if model not in result
                       or result[model].context_window is None or result[model].vision is None]
            if missing:
                catalog = await self._load_catalog(client)
                host = urlsplit(profile.endpoint).hostname
                endpoint_path = urlsplit(profile.endpoint).path.rstrip("/")
                matches = []
                for key, item in catalog.items():
                    api = item.get("api") if isinstance(item, dict) else None
                    if not isinstance(api, str) or not api:
                        continue
                    try:
                        parts = urlsplit(api)
                    except ValueError:
                        continue
                    path = parts.path.rstrip("/")
                    if parts.hostname == host and (not path or endpoint_path == path
                                                   or endpoint_path.startswith(path + "/")):
                        matches.append((len(path), key))
                name = max(matches)[1] if matches else CLOUD_HOSTS.get(host, profile.name.lower())
                entry = catalog.get(name, {})
                limits = entry.get("limits", {}) if isinstance(entry, dict) else {}
                visions = entry.get("vision", {}) if isinstance(entry, dict) else {}
                for model in missing:
                    key = model
                    if name != "openrouter":
                        key = key.removeprefix(name + "/")
                    elif key.endswith((":nitro", ":floor")) and key not in limits and key not in visions:
                        key = key.rsplit(":", 1)[0]
                    limit = positive_limit(limits.get(key))
                    previous = result.get(model)
                    supported = visions.get(key)
                    if type(supported) is not bool:
                        supported = None
                    merge(model, previous.source if previous else "models.dev/" + name,
                          limit=limit if previous is None or previous.context_window is None else None,
                          vision=supported if previous is None or previous.vision is None else None,
                          updated_at=previous.updated_at if previous else self._catalog["updated_at"])
            return result
        finally:
            if own_client:
                await client.aclose()
