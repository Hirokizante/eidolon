import json

import httpx
import pytest

from eidolon.config import ModelMetadata, ProviderConfig
from eidolon.providers import OpenAIProvider, ProviderCapabilityError, ProviderError


def sse(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
async def test_reasoning_stream_reports_progress_without_retaining_model_thoughts(field):
    private_thought = "private model reasoning"
    stream = sse({field: private_thought}) + sse({field: private_thought})
    stream += sse({"content": "Answer"}, "stop")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=stream),
    )) as client:
        events = [event async for event in OpenAIProvider(
            ProviderConfig(presentation="plain"), client=client,
        ).stream([])]
    assert [event for event in events if event["type"] == "progress"] == [
        {"type": "progress", "phase": "reasoning"},
    ]
    assert private_thought not in json.dumps(events)
    assert [event["text"] for event in events if event["type"] == "token"] == ["Answer"]
    assert events[-1] == {"type": "finish", "reason": "stop"}


async def test_stream_requests_and_reads_final_usage_chunk_with_no_choices(monkeypatch):
    requests = []
    clock = iter([10, 10, 12])
    monkeypatch.setattr("eidolon.providers.openai.monotonic", lambda: next(clock))
    usage = {"prompt_tokens": 5000, "completion_tokens": 100,
             "prompt_tokens_details": {"cached_tokens": 4000}}
    stream = sse({"content": "Hi"}, "stop")
    stream += "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
    stream += "data: [DONE]\n\n"
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="plain"), client=client)
        events = [event async for event in provider.stream([])]
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert next(event for event in events if event["type"] == "usage") == {
        "type": "usage", "usage": usage, "duration": 2,
    }
    assert events[-1] == {"type": "finish", "reason": "stop"}


def test_compatible_endpoint_can_opt_out_of_requesting_usage():
    profile = ProviderConfig(parameters={"stream_options": {"include_usage": False}})
    assert OpenAIProvider(profile)._payload([], None, "plain")["stream_options"] == {
        "include_usage": False,
    }


def test_openrouter_affinity_is_opaque_stable_and_isolated_per_conversation():
    provider = OpenAIProvider(ProviderConfig(endpoint="https://openrouter.ai/api/v1"))
    first = provider.for_session("synthetic-conversation-a")
    second = provider.for_session("synthetic-conversation-b")
    payload = first._payload([], None, "plain")
    identity = payload["session_id"]
    assert identity == first._payload([], None, "plain")["session_id"]
    again = provider.for_session("synthetic-conversation-a")._payload([], None, "plain")
    assert identity == again["session_id"]
    assert identity != second._payload([], None, "plain")["session_id"]
    assert len(identity) == 64 and "synthetic" not in identity
    assert "session_id" not in provider._payload([], None, "plain")
    assert not provider.config.parameters


@pytest.mark.parametrize("endpoint", ["http://localhost:1234/v1", "https://example.org/v1",
                                     "https://openrouter.ai.example.org/v1"])
def test_automatic_affinity_is_only_sent_to_openrouter(endpoint):
    provider = OpenAIProvider(ProviderConfig(endpoint=endpoint)).for_session("synthetic")
    assert "session_id" not in provider._payload([], None, "plain")


@pytest.mark.parametrize("parameters,headers", [({"session_id": "explicit"}, {}),
    ({"prompt_cache_key": "explicit"}, {}), ({}, {"X-Session-ID": "explicit"})])
def test_explicit_openrouter_affinity_overrides_generated_identity(parameters, headers):
    provider = OpenAIProvider(ProviderConfig(endpoint="https://openrouter.ai/api/v1",
        parameters=parameters, headers=headers)).for_session("synthetic")
    payload = provider._payload([], None, "plain")
    if "session_id" in parameters:
        assert payload["session_id"] == "explicit"
    else:
        assert "session_id" not in payload


async def test_stream_captures_server_timings_without_usage_or_token_fragments():
    timings = {"cache_n": 4000, "prompt_n": 1000, "predicted_n": 100,
               "predicted_per_second": 42.5}
    stream = sse({"content": "Hi"}, "stop")
    stream += "data: " + json.dumps({"choices": [], "timings": timings}) + "\n\n"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=stream),
    )) as client:
        events = [event async for event in OpenAIProvider(
            ProviderConfig(presentation="plain"), client=client,
        ).stream([])]
    telemetry = next(event for event in events if event["type"] == "usage")
    assert telemetry["timings"] == timings and telemetry["usage"] == {}
    assert telemetry["duration"] >= 0


async def test_compatible_stream_reassembles_tools_and_allows_local_no_key():
    requests = []
    stream = ": keepalive\n\n" + sse({"content": "Hello "}) + sse({"content": "Alex"})
    stream += sse({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q":'}}]})
    stream += sse({"tool_calls": [{"index": 0, "function": {"arguments": '"eidolon"}'}}]}, "tool_calls")
    stream += "data: [DONE]\n\n"
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=stream, headers={"Content-Type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="plain"), client=client)
        events = [item async for item in provider.stream([{"role": "user", "content": "Hi"}])]
    assert "Authorization" not in requests[0].headers
    assert str(requests[0].url) == "http://127.0.0.1:1234/v1/chat/completions"
    assert "".join(item["text"] for item in events if item["type"] == "token") == "Hello Alex"
    assert events[-2]["call"]["function"] == {"name": "search", "arguments": '{"q":"eidolon"}'}
    assert events[-1] == {"type": "finish", "reason": "tool_calls"}


@pytest.mark.parametrize("stream", ["data: [DONE]\n\n", "data: broken\n\n", sse({}, "length")])
async def test_stream_failure_does_not_become_success(stream):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=stream))) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="plain"), client=client)
        with pytest.raises(ProviderError):
            _ = [item async for item in provider.stream([])]


async def test_provider_errors_never_echo_body_or_credential():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400, content="secret"))) as client:
        provider = OpenAIProvider(ProviderConfig(api_key_ref="env:KEY", presentation="plain"),
                                  credential_resolver=lambda _: "secret", client=client)
        with pytest.raises(ProviderError) as raised:
            _ = [item async for item in provider.stream([])]
        assert "secret" not in str(raised.value)
        assert "presentation" in str(raised.value)


def test_capabilities_are_explicit_and_overrides_cannot_replace_transcript():
    provider = OpenAIProvider(ProviderConfig(presentation="json"))
    with pytest.raises(ProviderCapabilityError):
        _ = provider.presentation_mode
    provider = OpenAIProvider(ProviderConfig(presentation="auto"))
    assert provider.presentation_mode == "prefix"
    provider = OpenAIProvider(ProviderConfig(parameters={"messages": []}))
    with pytest.raises(ProviderCapabilityError):
        provider._payload([], None, "plain")


async def test_verified_json_and_information_tools_share_one_request():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse({"content": '{"expression":null,"gesture":null,"reply":"Hi"}'}, "stop"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="json", structured_verified=True), client=client)
        _ = [item async for item in provider.stream([], tools=[{"type": "function", "function": {"name": "search"}}])]
    assert len(requests) == 1
    assert requests[0]["tools"]
    assert requests[0]["response_format"]["json_schema"]["strict"] is True


async def test_nullable_tool_fragments_from_compatible_provider():
    stream = sse({"tool_calls": [{"index": 0, "id": "call", "function": {"name": "echo", "arguments": None}}]})
    stream += sse({"tool_calls": [{"index": 0, "id": None, "function": {"name": None, "arguments": "{}"}}]}, "tool_calls")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=stream))) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="plain"), client=client)
        events = [event async for event in provider.stream([])]
    call = next(event["call"] for event in events if event["type"] == "tool_call")
    assert call["function"] == {"name": "echo", "arguments": "{}"}


@pytest.mark.parametrize("index_fields", [{}, {"index": 0}])
async def test_distinct_tool_ids_do_not_merge_when_indices_are_missing_or_reused(index_fields):
    stream = sse({"tool_calls": [{**index_fields, "id": "recall", "function": {
        "name": "memory_", "arguments": '{"query":',
    }}]})
    stream += sse({"tool_calls": [{**index_fields, "function": {
        "name": "recall", "arguments": '"shared garden"}',
    }}]})
    stream += sse({"tool_calls": [{**index_fields, "id": "files", "function": {
        "name": "list_files", "arguments": '{"path":',
    }}]})
    stream += sse({"tool_calls": [{**index_fields, "function": {
        "arguments": '"/permitted"}',
    }}]}, "tool_calls")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=stream),
    )) as client:
        events = [event async for event in OpenAIProvider(
            ProviderConfig(presentation="plain"), client=client,
        ).stream([])]
    calls = [event["call"] for event in events if event["type"] == "tool_call"]
    assert calls == [
        {"id": "recall", "type": "function", "function": {
            "name": "memory_recall", "arguments": '{"query":"shared garden"}',
        }},
        {"id": "files", "type": "function", "function": {
            "name": "list_files", "arguments": '{"path":"/permitted"}',
        }},
    ]


@pytest.mark.parametrize("index_fields", [{}, {"index": 0}, {"index": 7}])
async def test_interleaved_tool_fragments_use_stable_ids(index_fields):
    stream = sse({"tool_calls": [
        {**index_fields, "id": "first", "function": {"name": "memory_recall", "arguments": '{"query":'}},
        {**index_fields, "id": "second", "function": {"name": "memory_recall", "arguments": '{"query":'}},
    ]})
    # IDs may repeat on continuation packets, even when entries change order.
    stream += sse({"tool_calls": [
        {**index_fields, "id": "second", "function": {"arguments": '"second query"}'}},
        {**index_fields, "id": "first", "function": {"arguments": '"first query"}'}},
    ]}, "tool_calls")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=stream),
    )) as client:
        events = [event async for event in OpenAIProvider(
            ProviderConfig(presentation="plain"), client=client,
        ).stream([])]
    calls = [event["call"] for event in events if event["type"] == "tool_call"]
    assert [call["id"] for call in calls] == ["first", "second"]
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [
        {"query": "first query"}, {"query": "second query"},
    ]


@pytest.mark.parametrize("with_indices", [False, True])
async def test_parallel_tool_arguments_without_repeated_ids_use_index_or_position(with_indices):
    def part(index, **fields):
        return {**({"index": index} if with_indices else {}), **fields}

    stream = sse({"tool_calls": [
        part(0, id="first", function={"name": "memory_recall", "arguments": '{"query":'}),
        part(1, id="second", function={"name": "list_files", "arguments": '{"path":'}),
    ]})
    stream += sse({"tool_calls": [
        part(0, function={"arguments": '"garden"}'}),
        part(1, function={"arguments": '"/permitted"}'}),
    ]}, "tool_calls")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=stream),
    )) as client:
        events = [event async for event in OpenAIProvider(
            ProviderConfig(presentation="plain"), client=client,
        ).stream([])]
    calls = [event["call"] for event in events if event["type"] == "tool_call"]
    assert [call["id"] for call in calls] == ["first", "second"]
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [
        {"query": "garden"}, {"path": "/permitted"},
    ]


async def test_nullable_tool_list_and_delta_in_compatible_stream():
    stream = sse({"content": "OK", "tool_calls": None}) + sse(None, "stop")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=stream))) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="plain"), client=client)
        events = [event async for event in provider.stream([])]
    assert [event for event in events if event["type"] != "request"] == [
        {"type": "token", "text": "OK"}, {"type": "finish", "reason": "stop"}]


async def test_request_capture_matches_wire_body_and_excludes_authentication_headers():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse({"content": "Hi"}, "stop"))
    profile = ProviderConfig(
        endpoint="https://example.org/v1/chat/completions", model="test-model",
        api_key_ref="env:KEY", headers={"X-Secret": "env:KEY"},
        presentation="json", structured_verified=True,
        parameters={"temperature": 0.3, "max_tokens": 100},
    )
    messages = [{"role": "system", "content": "Full context\n🌸"},
                {"role": "user", "content": 'Hello "Eidolon"'}]
    tools = [{"type": "function", "function": {"name": "search"}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(profile, client=client, credential_resolver=lambda _: "secret-value")
        events = [event async for event in provider.stream(messages, tools=tools)]
    capture = events[0]
    assert capture["type"] == "request"
    assert capture["body"].encode("utf-8") == requests[0].content
    assert capture["endpoint"] == str(requests[0].url)
    assert capture["method"] == requests[0].method == "POST"
    payload = json.loads(capture["body"])
    assert payload["messages"] == messages and payload["tools"] == tools
    assert payload["temperature"] == 0.3 and payload["max_tokens"] == 100
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert requests[0].headers["Authorization"] == "Bearer secret-value"
    assert requests[0].headers["X-Secret"] == "secret-value"
    assert "secret-value" not in json.dumps(capture)


@pytest.mark.parametrize("endpoint", [
    "https://example.org/v1", "https://example.org/v1/",
    "https://example.org/v1/chat/completions",
])
async def test_discovery_normalizes_endpoint_and_resolves_credentials(endpoint):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [
            {"id": "z-model"}, {"id": "a-model"}, {"id": "z-model"},
            {"id": None}, {}, {"id": ""}]})
    profile = ProviderConfig(endpoint=endpoint, api_key_ref="env:KEY",
                             headers={"X-API-Key": "env:HEADER", "User-Agent": "eidolon"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(profile, client=client,
                                  credential_resolver=lambda ref: {"env:KEY": "key-secret", "env:HEADER": "header-secret"}[ref])
        assert await provider.list_models() == ["a-model", "z-model"]
    request = requests[0]
    assert str(request.url) == "https://example.org/v1/models"
    assert request.method == "GET" and not request.content
    assert request.headers["Authorization"] == "Bearer key-secret"
    assert request.headers["X-API-Key"] == "header-secret"
    assert request.extensions["timeout"] == {"connect": 8, "read": 8, "write": 8, "pool": 8}


async def test_discovery_literal_key_override_is_temporary_and_redirects_are_disabled():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/models"}, text="secret-response")
    profile = ProviderConfig(api_key_ref="env:OLD")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        provider = OpenAIProvider(profile, client=client,
                                  credential_resolver=lambda _: pytest.fail("Old key was resolved"))
        with pytest.raises(ProviderError, match="Redirects are disabled") as error:
            await provider.list_models(api_key="temporary-secret")
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer temporary-secret"
    assert "secret" not in str(error.value)
    assert profile.api_key_ref == "env:OLD"


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(401, text="api-key-secret"), "API key"),
    (httpx.Response(429, text="api-key-secret"), "rate or credit limit"),
    (httpx.Response(200, text="api-key-secret"), "invalid model inventory JSON"),
    (httpx.Response(200, json={"data": {"secret": "api-key-secret"}}), "unsupported model inventory"),
    (httpx.Response(200, json={"data": []}), "no model IDs"),
])
async def test_discovery_failures_have_actionable_safe_messages(response, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        provider = OpenAIProvider(ProviderConfig(), client=client)
        with pytest.raises(ProviderError, match=expected) as error:
            await provider.list_models()
    assert "api-key-secret" not in str(error.value)


@pytest.mark.parametrize("failure, expected", [
    (httpx.ReadTimeout("timeout api-key-secret"), "timed out"),
    (httpx.ConnectError("https://api-key-secret.example"), "could not connect"),
])
async def test_discovery_network_failures_do_not_echo_exception(failure, expected):
    def handler(request):
        raise failure
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match=expected) as error:
            await OpenAIProvider(ProviderConfig(), client=client).list_models()
    assert "api-key-secret" not in str(error.value)


async def test_discovery_unavailable_credential_is_sanitized_before_network_request():
    def resolver(reference):
        raise ValueError("Keychain rejected api-key-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("Request was sent"))) as client:
        provider = OpenAIProvider(ProviderConfig(api_key_ref="keyring:test"), client=client,
                                  credential_resolver=resolver)
        with pytest.raises(ProviderError, match="credential is unavailable") as error:
            await provider.list_models()
    assert "api-key-secret" not in str(error.value)


async def test_multimodal_message_content_is_preserved_on_wire():
    requests = []
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8=",
                                             "detail": "auto"}},
    ]}]
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse({"content": "An image."}, "stop"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(ProviderConfig(presentation="plain"), client=client)
        _ = [event async for event in provider.stream(messages)]
    assert requests[0]["messages"] == messages


@pytest.mark.parametrize("supported", [True, False])
async def test_vision_capability_uses_explicit_profile_override_without_network(supported):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: pytest.fail("Unexpected capability request"),
    )) as client:
        provider = OpenAIProvider(ProviderConfig(vision=supported), client=client)
        assert await provider.supports_vision() is supported


async def test_vision_capability_metadata_is_exact_and_works_with_manual_context_limit():
    profile = ProviderConfig(model="selected", context_window=8192, model_metadata={
        "selected": ModelMetadata(vision=False, source="provider", updated_at=0),
        "other": ModelMetadata(vision=True, source="provider", updated_at=0),
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: pytest.fail("Known metadata should not need a request"),
    )) as client:
        provider = OpenAIProvider(profile, client=client)
        assert await provider.supports_vision() is False
        profile.model = "other"
        assert await provider.supports_vision() is True


@pytest.mark.parametrize("supported", [True, False])
async def test_vision_capability_discovers_exact_model_inventory(supported):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": "exact-model", "architecture": {
                "input_modalities": ["text", "image"] if supported else ["text"],
            }}, {"id": "exact-model-vision", "vision": True},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(ProviderConfig(
            model="exact-model", endpoint="https://example.org/v1",
        ), client=client)
        assert await provider.supports_vision() is supported
        assert await provider.supports_vision() is supported
    assert len(requests) == 1


async def test_unknown_vision_capability_remains_unknown_and_discovery_has_cooldown(tmp_path,
                                                                                 monkeypatch):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path))
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "unverified-vision-model"}]})
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(ProviderConfig(model="unverified-vision-model"), client=client)
        assert await provider.supports_vision() is None
        first_count = len(requests)
        assert await provider.supports_vision() is None
    assert len(requests) == first_count
