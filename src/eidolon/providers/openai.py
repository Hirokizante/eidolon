"""OpenAI-compatible Chat Completions without a dependency on one vendor SDK."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .maintenance import MaintenanceOutput, OutputSupportCache, maintenance_output


class ProviderError(RuntimeError):
    """An actionable provider/network failure, without request credentials."""

    def __init__(self, message: str, *, category: str = "unavailable") -> None:
        super().__init__(message)
        self.category = category if category in {
            "unavailable", "authentication", "rate_or_credit_limit", "timeout",
        } else "unavailable"


class ProviderCapabilityError(ProviderError):
    """The selected profile does not support a requested capability."""


class ProviderOutputLimitError(ProviderError):
    """Generation reached its output-token limit before completing."""


class OpenAIProvider:
    def __init__(self, config: Any, *, credential_resolver: Callable | None = None,
                 client: httpx.AsyncClient | None = None, timeout: float = 90,
                 stream_usage: bool = True,
                 output_support_cache: OutputSupportCache | None = None):
        self.config = config
        if credential_resolver is None:
            from eidolon.config import resolve_secret
            credential_resolver = resolve_secret
        self.credential_resolver = credential_resolver
        self.client = client
        self.timeout = timeout
        self.stream_usage = stream_usage
        self.output_support_cache = output_support_cache or OutputSupportCache()
        self._session_id: str | None = None
        self.model_inventory: list[dict] = []
        self._vision_lock = asyncio.Lock()
        self._vision_attempts: dict[tuple[str, str], float] = {}

    def for_session(self, session_id: str) -> OpenAIProvider:
        """Bind routing to a conversation without mutating a shared provider."""
        provider = copy.copy(self)
        provider._session_id = hashlib.sha256(
            ("eidolon-room:session:" + session_id).encode("utf-8")
        ).hexdigest()
        return provider

    def _headers(self, *, api_key: str | None = None) -> dict[str, str]:
        try:
            headers = {name: self.credential_resolver(value)
                       if value.startswith(("env:", "keyring:")) else value
                       for name, value in self.config.headers.items()}
            key = api_key
            if key is None and self.config.api_key_ref:
                key = self.credential_resolver(self.config.api_key_ref)
            if self.config.api_key_ref and not key:
                raise ValueError("Missing credential")
            if key:
                # A provided API key overrides custom Authorization in any casing.
                headers = {name: value for name, value in headers.items()
                           if name.lower() != "authorization"}
                headers["Authorization"] = "Bearer " + key
            if any(value is None for value in headers.values()):
                raise ValueError("Missing header credential")
            return headers
        except Exception:
            raise ProviderError(
                "Provider credential is unavailable; check its Keychain or environment reference.",
                category="authentication",
            ) from None

    def _endpoint(self, resource: str) -> str:
        parts = urlsplit(self.config.endpoint)
        path = parts.path.rstrip("/")
        if path.endswith("/chat/completions"):
            path = path[:-len("/chat/completions")]
        return urlunsplit(parts._replace(path=path + "/" + resource))

    async def list_models(self, *, api_key: str | None = None,
                          timeout: float = 8) -> list[str]:
        """Fetch a compatible inventory without revealing credentials or response bodies."""
        headers = await asyncio.to_thread(self._headers, api_key=api_key)
        headers.setdefault("Accept", "application/json")
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        try:
            response = await client.get(self._endpoint("models"), headers=headers,
                                        timeout=timeout, follow_redirects=False)
            if not 200 <= response.status_code < 300:
                hint = " Check the base URL and model inventory support."
                if response.status_code in (401, 403):
                    hint = " Check the API key and access permissions."
                elif response.status_code == 429:
                    hint = " The provider rate or credit limit was reached; retry later."
                elif 300 <= response.status_code < 400:
                    hint = " Redirects are disabled; enter the provider's final base URL."
                raise ProviderError(f"Model discovery returned HTTP {response.status_code}.{hint}")
            try:
                packet = response.json()
            except ValueError:
                raise ProviderError("Provider sent invalid model inventory JSON; enter a model ID manually.") from None
            if not isinstance(packet, dict) or not isinstance(packet.get("data"), list):
                raise ProviderError("Provider returned an unsupported model inventory; enter a model ID manually.")
            self.model_inventory = packet["data"]
            models = sorted({item["id"] for item in packet["data"]
                             if isinstance(item, dict) and isinstance(item.get("id"), str)
                             and item["id"].strip()})
            if not models:
                raise ProviderError("Provider returned no model IDs; enter a model ID manually.")
            return models
        except httpx.TimeoutException:
            raise ProviderError("Model discovery timed out; retry or enter a model ID manually.") from None
        except httpx.HTTPError:
            raise ProviderError("Model discovery could not connect; check the base URL and connectivity.") from None
        except (ValueError, TypeError):
            raise ProviderError("Model discovery could not send the request; check the base URL and headers.") from None
        finally:
            if own_client:
                await client.aclose()

    async def supports_vision(self) -> bool | None:
        """Return explicit image-input support, or None if discovery is inconclusive.

        Discovery uses exact model IDs and capability metadata. A compatible
        endpoint that omits these fields can use the profile's vision override.
        """
        from eidolon.config import default_state_dir
        from eidolon.providers.metadata import ModelMetadataResolver, inventory_vision

        override = getattr(self.config, "vision", None)
        if type(override) is bool:
            return override
        async with self._vision_lock:
            model = self.config.model
            discovered = inventory_vision(self.model_inventory).get(model)
            if discovered is not None:
                return discovered
            metadata = getattr(self.config, "model_metadata", {}).get(model)
            if metadata is not None and type(metadata.vision) is bool:
                return metadata.vision
            identity = (self.config.endpoint, model)
            previous_attempt = self._vision_attempts.get(identity)
            if previous_attempt is not None and monotonic() - previous_attempt < 30:
                return None
            self._vision_attempts[identity] = monotonic()
            if not self.model_inventory:
                try:
                    await self.list_models(timeout=3)
                except ProviderError:
                    pass  # Native or public metadata may still identify this model.
                discovered = inventory_vision(self.model_inventory).get(model)
                if discovered is not None:
                    return discovered
            resolver = ModelMetadataResolver(default_state_dir() / "cache/models-dev.json",
                                             client=self.client)
            result = await resolver.resolve(
                self, [model], inventory=self.model_inventory,
            )
            if result:
                self.config.model_metadata.update(result)
            selected = result.get(model)
            return selected.vision if selected else None

    @property
    def presentation_mode(self) -> str:
        mode = self.config.presentation
        if mode == "auto":
            return "json" if self.config.structured_verified else "prefix"
        if mode == "json" and not self.config.structured_verified:
            raise ProviderCapabilityError(
                "JSON presentation needs structured_verified=true after testing this endpoint. "
                "Choose prefix or plain for ordinary compatible streaming."
            )
        return mode

    def _payload(self, messages: list[dict], tools: list[dict] | None,
                 presentation: str) -> dict:
        if tools and not self.config.tools:
            raise ProviderCapabilityError("Tool calling is disabled for this provider profile.")
        # Overrides cannot replace the transcript, credential handling, or streaming protocol.
        reserved = {"messages", "model", "stream", "tools", "tool_choice", "response_format"}
        conflicts = reserved.intersection(self.config.parameters)
        if conflicts:
            raise ProviderCapabilityError("Reserved provider parameter override: " + ", ".join(sorted(conflicts)))
        payload = {"model": self.config.model, "messages": messages,
                   "stream": True, **self.config.parameters}
        # This routing field is specific to OpenRouter. Explicit user routing wins.
        if (self._session_id and urlsplit(self.config.endpoint).hostname == "openrouter.ai"
                and "prompt_cache_key" not in payload
                and not any(name.lower() == "x-session-id" for name in self.config.headers)):
            payload.setdefault("session_id", self._session_id)
        # Compatible endpoints may opt out through parameters.stream_options.
        if self.stream_usage:
            payload.setdefault("stream_options", {"include_usage": True})
        if tools:
            payload["tools"] = tools
        if response_format := self.response_format(presentation):
            payload["response_format"] = response_format
        return payload

    @staticmethod
    def response_format(presentation: str) -> dict | None:
        if presentation == "json":
            return {
                "type": "json_schema",
                "json_schema": {"name": "eidolon_presentation", "strict": True, "schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"expression": {"type": ["string", "null"]},
                                   "gesture": {"type": ["string", "null"]},
                                   "reply": {"type": "string"}},
                    "required": ["expression", "gesture", "reply"]}},
            }
        return None

    def maintenance_output(self, task_id: str) -> MaintenanceOutput:
        return maintenance_output(self.config, task_id,
                                  self.output_support_cache.rejected(self.config))

    async def stream(self, messages: list[dict], *, tools: list[dict] | None = None,
                     presentation: str | None = None,
                     output: MaintenanceOutput | None = None) -> AsyncIterator[dict]:
        """Yield request/progress/token/usage/tool_call/finish events; preserve tool IDs.

        The request event contains the serialized body at dispatch, never headers.
        Reasoning progress contains no model text.
        """
        if output is not None and tools:
            raise ProviderCapabilityError("Maintenance output cannot be combined with tools.")
        presentation = "plain" if output is not None else presentation or self.presentation_mode
        payload = self._payload(messages, tools, presentation)
        if output is not None and output.response_format is not None:
            payload["response_format"] = copy.deepcopy(output.response_format)
            if urlsplit(self.config.endpoint).hostname == "openrouter.ai":
                # Require compatible upstream routing, rather than ignored hints.
                payload["provider"] = {"require_parameters": True}
        headers = self._headers()
        headers.setdefault("Accept", "text/event-stream")
        headers.setdefault("Content-Type", "application/json")
        endpoint = self._endpoint("chat/completions")
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        calls: list[dict] = []
        calls_by_index: dict[int, dict] = {}
        calls_by_id: dict[str, dict] = {}
        finish = None
        finished = False
        reasoning_seen = False
        data_lines: list[str] = []
        started = monotonic()

        def decode(lines: list[str]) -> list[dict]:
            nonlocal finish, finished, reasoning_seen
            if not lines:
                return []
            data = "\n".join(lines)
            if data.strip() == "[DONE]":
                finished = True
                return []
            try:
                packet = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ProviderError("Provider sent malformed streaming JSON.") from exc
            if packet.get("error"):
                raise ProviderError("Provider reported an error during the response. Check the profile and provider dashboard.")
            events = []
            for choice in packet.get("choices") or []:
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                if not reasoning_seen and any(
                    isinstance(delta.get(key), str) and delta[key]
                    for key in ("reasoning_content", "reasoning")
                ):
                    reasoning_seen = True
                    events.append({"type": "progress", "phase": "reasoning"})
                content = delta.get("content")
                if content:
                    if not isinstance(content, str):
                        raise ProviderError("Provider returned non-text content; select a chat text model.")
                    events.append({"type": "token", "text": content})
                if delta.get("refusal") and output is not None:
                    raise ProviderError("Provider refused the maintenance request.")
                if delta.get("refusal"):
                    events.append({"type": "token", "text": delta["refusal"]})
                for position, part in enumerate(delta.get("tool_calls") or []):
                    index = part.get("index")
                    if index is None:
                        index = position
                    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                        raise ProviderError("Provider returned an invalid tool-call index.")
                    call_id = part.get("id") or ""
                    if not isinstance(call_id, str):
                        raise ProviderError("Provider returned an invalid tool-call ID.")
                    # Some compatible endpoints omit indices or reuse zero for
                    # separate calls. A new ID starts a new call, never a fragment
                    # of the previous ID/name/arguments. ID-less deltas still use
                    # the index (or array position) to continue their current call.
                    call = calls_by_id.get(call_id) if call_id else calls_by_index.get(index)
                    if call is None:
                        pending = calls_by_index.get(index)
                        if pending is not None and not pending["id"]:
                            call = pending
                        else:
                            call = {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}}
                            calls.append(call)
                    if call_id:
                        call["id"] = call_id
                        calls_by_id[call_id] = call
                    calls_by_index[index] = call
                    function = part.get("function") or {}
                    # Compatible endpoints may send explicit null for fields that
                    # have no fragment in this event (as GLM does).
                    name_fragment = function.get("name") or ""
                    argument_fragment = function.get("arguments") or ""
                    if not isinstance(name_fragment, str) or not isinstance(argument_fragment, str):
                        raise ProviderError("Provider returned invalid tool-call fragments.")
                    call["function"]["name"] += name_fragment
                    call["function"]["arguments"] += argument_fragment
                    if len(call["function"]["arguments"]) > 100_000:
                        raise ProviderError("Provider tool arguments exceed the 100 KB limit.")
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            telemetry = {key: packet[key] for key in ("timings", "stats")
                         if isinstance(packet.get(key), dict)}
            if isinstance(packet.get("usage"), dict) or telemetry:
                usage = packet["usage"] if isinstance(packet.get("usage"), dict) else {}
                events.append({"type": "usage", "usage": usage,
                               "duration": monotonic() - started, **telemetry})
            return events

        try:
            request = client.build_request("POST", endpoint, headers=headers, json=payload)
            yield {"type": "request", "method": request.method, "endpoint": str(request.url),
                   "body": request.content.decode("utf-8")}
            started = monotonic()
            response = await client.send(request, stream=True)
            try:
                if output is not None:
                    yield {"type": "response", "status": response.status_code}
                if response.status_code >= 400:
                    hint = ""
                    if response.status_code in (400, 422):
                        if output is not None and output.mode != "prompt":
                            # Inspect only typed error fields, never infer support
                            # from arbitrary prose or replay a provider's message.
                            await response.aread()
                            try:
                                error = response.json().get("error", {})
                                if (isinstance(error, dict) and error.get("param") in {
                                        "response_format", "response_format.type",
                                        "response_format.json_schema"}
                                        and error.get("code") in {
                                            "unsupported_parameter", "unsupported_value"}):
                                    modes = ({"json_schema", "json_object"}
                                             if error["param"] == "response_format"
                                             and error["code"] == "unsupported_parameter"
                                             else {output.mode})
                                    for mode in modes:
                                        self.output_support_cache.reject(self.config, mode)
                            except (ValueError, AttributeError, TypeError):
                                pass
                        raise ProviderCapabilityError(
                            f"Provider rejected request options (HTTP {response.status_code}); "
                            "check the selected model, output-token field, reasoning effort, tools and presentation."
                        )
                    elif response.status_code in (401, 403):
                        hint = " Check the app-owned credential reference."
                    elif response.status_code == 429:
                        hint = " The provider rate or credit limit was reached; retry later."
                    raise ProviderError(
                        f"Provider returned HTTP {response.status_code}.{hint}",
                        category=("authentication" if response.status_code in (401, 403) else
                                  "rate_or_credit_limit" if response.status_code == 429 else
                                  "unavailable"),
                    )
                async for line in response.aiter_lines():
                    if not line:
                        for event in decode(data_lines):
                            yield event
                        data_lines = []
                        if finished:
                            break
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                        if sum(map(len, data_lines)) > 1_000_000:
                            raise ProviderError("Provider streaming event exceeds the 1 MB limit.")
                    # SSE comments, event labels, IDs, and retry hints do not carry model content.
                if data_lines:
                    for event in decode(data_lines):
                        yield event
            finally:
                await response.aclose()
            if finish is None:
                raise ProviderError("Provider stream ended before completion; the incomplete reply was not saved.")
            if finish == "length":
                raise ProviderOutputLimitError(
                    "Provider stopped with 'length'; the incomplete reply was not saved."
                )
            if finish == "content_filter":
                raise ProviderError(f"Provider stopped with {finish!r}; the incomplete reply was not saved.")
            for call in calls:
                if not call["id"] or not call["function"]["name"]:
                    raise ProviderError("Provider returned an incomplete tool call.")
                yield {"type": "tool_call", "call": call}
            yield {"type": "finish", "reason": finish}
        except httpx.TimeoutException as exc:
            raise ProviderError("Provider timed out; retry this turn.", category="timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("Provider connection failed; check endpoint and connectivity.") from exc
        finally:
            if own_client:
                await client.aclose()
