import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from eidolon.config import AppConfig, ModelMetadata, ProviderConfig
from eidolon.engine import ConversationEngine
from eidolon.engine.conversation import _bounded_history
from eidolon.providers import OpenAIProvider
from eidolon.providers.context import ContextBudget, TokenCounter
from eidolon.storage import Store
from eidolon.tools import ToolRegistry


class Memory:
    def __init__(self, error=None):
        self.error, self.queries = error, []

    async def recall(self, query, *, max_chars):
        self.queries.append(query)
        if self.error:
            raise self.error
        return "Alex and Eidolon share a garden."


class Provider:
    def __init__(self, responses, callback=None):
        self.responses, self.requests, self.callback = responses, [], callback

    async def stream(self, messages, *, tools, presentation):
        self.requests.append(copy.deepcopy(messages))
        index = len(self.requests) - 1
        if self.callback:
            self.callback(index)
        for event in self.responses[index]:
            await asyncio.sleep(0)
            yield event


class Tools:
    cosmetic_names = {"body_action"}

    def schemas(self, permitted=None):
        return [{"type": "function", "function": {"name": "search"}},
                {"type": "function", "function": {"name": "body_action"}}]

    async def call(self, name, args, context=None):
        return {"source": "https://example.org", "result": "Found it"}


def config(tmp_path, mode="plain", post_history="system"):
    prompts = {}
    for name in ("SYSTEM", "SOUL", "USER", "POST_HISTORY"):
        path = tmp_path / (name + ".md")
        path.write_text("old " + name)
        prompts[name] = str(path)
    return AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path, prompts=prompts,
                     providers=[ProviderConfig(presentation=mode, post_history=post_history)])


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.sqlite3")
    instance.create_session("today", kind="chat")
    yield instance
    instance.close()


async def collect(engine, **kwargs):
    return [item async for item in engine.generate("today", "Hi Alex", **kwargs)]


async def test_send_and_completion_times_survive_tool_rounds_and_preview(tmp_path, store, monkeypatch):
    user_at = "2026-10-18T23:58:12.123456+08:00"
    reply_at = "2026-10-19T00:01:09.654321+08:00"
    monkeypatch.setattr("eidolon.storage.store.utc_now", lambda: reply_at)
    store.complete_turn("today", "Earlier question", "Earlier answer",
                        metadata={"user_message_at": user_at})
    original = store.list_messages("today")
    current_at = datetime.fromisoformat("2026-10-19T10:02:30.987654+08:00")
    monkeypatch.setattr("eidolon.storage.store.utc_now", lambda: "2026-10-19T10:02:40+08:00")
    call = {"id": "lookup", "type": "function", "function": {
        "name": "search", "arguments": "{}",
    }}
    provider = Provider([
        [{"type": "token", "text": "Checking. "}, {"type": "tool_call", "call": call},
         {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": "Done."}, {"type": "finish", "reason": "stop"}],
    ])
    engine = ConversationEngine(config(tmp_path), store, Memory(), Tools(), provider=provider)
    events = await collect(engine, user_message_at=current_at)
    assert events[-1]["type"] == "completed", events
    expected = [
        {"role": "user", "content": "[2026-10-18T23:58:12+08:00]\nEarlier question"},
        {"role": "assistant", "content": "[2026-10-19T00:01:09+08:00]\nEarlier answer"},
    ]
    for request in provider.requests:
        conversation = [m for m in request if m["role"] in {"user", "assistant"}]
        assert conversation[:2] == expected
        assert conversation[2]["content"].startswith("[2026-10-19T10:02:30+08:00]\n")
        assert conversation[2]["content"].count("[2026-10-19T10:02:30+08:00]") == 1
    continuation = next(m for m in provider.requests[1] if m.get("tool_calls"))
    assert continuation["content"].partition("\n")[2] == "Checking. "
    assert datetime.fromisoformat(continuation["content"].split("\n")[0][1:-1]).tzinfo is not None
    preview = await engine.preview_prompt("today", "Next question")
    assert [m for m in preview["messages"] if m["role"] in {"user", "assistant"}][:2] == expected
    assert store.list_messages("today")[:2] == original
    assert store.list_messages("today")[-2]["content"] == "Hi Alex"
    assert store.list_messages("today")[-2]["created_at"] == current_at.isoformat()
    assert store.list_messages("today")[-1]["content"] == "Checking. Done."


def test_history_timestamps_preserve_image_parts_and_dated_excerpts():
    stamp = "2026-10-18T23:58:12+08:00"
    image = {"type": "image_url", "image_url": {"url": "eidolon-image:example"}}
    history = [
        {"role": role, "created_at": stamp, "content": "A short exchange."}
        for _ in range(12) for role in ("user", "assistant")
    ]
    history[-2]["content"] = [{"type": "text", "text": "An image."}, image]
    original = copy.deepcopy(history)
    counter = TokenCounter("test-model")
    unbounded, _ = _bounded_history(history, None, counter)
    assert unbounded[-2]["content"] == [
        {"type": "text", "text": f"[{stamp}]\n"},
        {"type": "text", "text": "An image."}, image,
    ]
    # Leave room for recent complete exchanges and some older excerpts.
    budget = sum(counter.message(m) for m in unbounded[-4:]) + 500
    recent, summary = _bounded_history(history, budget, counter)
    assert recent and summary
    assert f"user: [{stamp}]\n" in summary
    assert f"assistant: [{stamp}]\n" in summary
    assert sum(counter.message(m) for m in recent) + counter.text(summary) <= budget
    assert history == original


async def test_enabled_skill_is_discovered_then_loaded_as_instructions(tmp_path, store):
    cfg = config(tmp_path)
    registry = ToolRegistry(cfg)
    saved = registry.skills.save("planning", "Use when planning.", "Unique planning instructions.",
                                 enabled=True, create=True)
    registry.skills.save("disabled", "Hidden discovery marker", "Hidden instruction marker",
                         enabled=False, create=True)
    provider = Provider([
        [{"type": "tool_call", "call": {"id": "skill1", "type": "function", "function": {
            "name": "read_skill", "arguments": '{"name":"planning"}',
        }}}, {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": "Let's plan."}, {"type": "finish", "reason": "stop"}],
    ])
    try:
        engine = ConversationEngine(cfg, store, Memory(), registry, provider=provider)
        events = await collect(engine)
        assert events[-1]["type"] == "completed", events
        first, second = map(json.dumps, provider.requests)
        assert "Use when planning." in first
        assert "Unique planning instructions." not in first
        assert any("Unique planning instructions." in message.get("content", "")
                   for message in provider.requests[1] if message["role"] == "system")
        assert "Hidden discovery marker" not in first + second
        assert "Hidden instruction marker" not in first + second
        assert events[-1]["metadata"]["skill_hashes"] == {"planning": saved["hash"]}
        assert "Unique planning instructions." not in json.dumps(store.pending_outbox())
        registry.skills.set_enabled("planning", False)
        preview = await engine.preview_prompt("today", "Another plan")
        assert "Use when planning." not in json.dumps(preview)
    finally:
        await registry.close()


@pytest.mark.parametrize("tools_enabled", [True, False])
async def test_skills_respect_provider_tools_and_job_allowlist(tmp_path, store, tools_enabled):
    cfg = config(tmp_path)
    cfg.providers[0].tools = tools_enabled
    registry = ToolRegistry(cfg)
    registry.skills.save("planning", "Use when planning.", "Unique planning instructions.",
                         enabled=True, create=True)
    provider = Provider([[{"type": "token", "text": "Hello"},
                          {"type": "finish", "reason": "stop"}]])
    try:
        engine = ConversationEngine(cfg, store, Memory(), registry, provider=provider)
        preview = json.dumps(await engine.preview_prompt("today", "Plan"))
        assert "Use when planning." in preview
        assert ("Unique planning instructions." in preview) is (not tools_enabled)
        events = await collect(engine, interactive=False, permitted_tools=["read_file"])
        assert events[-1]["type"] == "completed", events
        request = json.dumps(provider.requests[0])
        assert ("Use when planning." in request) is (not tools_enabled)
    finally:
        await registry.close()


async def test_empty_and_disabled_skill_library_do_not_advertise_tool(tmp_path, store):
    cfg = config(tmp_path)
    schemas = []

    class ObservedProvider(Provider):
        async def stream(self, messages, *, tools, presentation):
            schemas.extend(tools)
            async for event in super().stream(messages, tools=tools, presentation=presentation):
                yield event

    registry = ToolRegistry(cfg)
    try:
        for install in (False, True):
            if install:
                registry.skills.save("disabled", "Hidden marker", "Never use this",
                                     create=True, enabled=False)
            provider = ObservedProvider([[{"type": "token", "text": "Hello"},
                                          {"type": "finish", "reason": "stop"}]])
            engine = ConversationEngine(cfg, store, Memory(), registry, provider=provider)
            events = await collect(engine)
            assert events[-1]["type"] == "completed", events
            assert "Hidden marker" not in json.dumps(provider.requests)
        assert "read_skill" not in {item["function"]["name"] for item in schemas}
    finally:
        await registry.close()


async def test_agent_turn_can_complete_more_work_than_companion_tool_round_limit(tmp_path, store):
    cfg = config(tmp_path)
    registry = ToolRegistry(cfg)
    target = tmp_path / "task.txt"
    responses = [[{"type": "tool_call", "call": {"id": f"edit-{index}", "function": {
        "name": "write_file", "arguments": json.dumps({"path": str(target), "content": str(index)}),
    }}}, {"type": "finish", "reason": "tool_calls"}] for index in range(8)]
    responses.append([{"type": "token", "text": "Finished and verified."},
                      {"type": "finish", "reason": "stop"}])
    provider = Provider(responses)
    try:
        await registry.set_agent_mode(True)
        engine = ConversationEngine(cfg, store, Memory(), registry, provider=provider, max_tool_rounds=0)
        events = await collect(engine, agent_mode=True)
        assert events[-1]["type"] == "completed", events
        assert len(provider.requests) == 9 and target.read_text() == "7"
        assert events[-1]["metadata"]["agent_mode"]
        assert "Agent mode is enabled" in json.dumps(provider.requests[0])
        assert store.list_messages("today")[-1]["content"] == "Finished and verified."
    finally:
        await registry.close()


@pytest.mark.parametrize("required_result", ["success", "omitted", "exception", "mcp_error", "error"])
async def test_required_tool_calls_must_succeed_before_completing_turn(tmp_path, store, required_result):
    class RequiredTools(Tools):
        async def call(self, name, args, context=None):
            if required_result == "exception":
                raise RuntimeError("Save unavailable")
            if required_result == "mcp_error":
                return {"is_error": True, "content": [{"type": "text", "text": "Save unavailable"}]}
            if required_result == "error":
                return {"error": "Save unavailable"}
            return {"saved": True}
    responses = [] if required_result == "omitted" else [[
        {"type": "tool_call", "call": {"id": "required-one", "function": {
            "name": "search", "arguments": "{}",
        }}}, {"type": "finish", "reason": "tool_calls"},
    ]]
    responses.append([{"type": "token", "text": "A complete note."},
                      {"type": "finish", "reason": "stop"}])
    provider = Provider(responses)
    engine = ConversationEngine(config(tmp_path), store, Memory(), RequiredTools(), provider=provider)
    events = await collect(engine, interactive=False, required_tools=["search"])
    if required_result == "success":
        assert events[-1]["type"] == "completed", events
        assert events[-1]["metadata"]["required_tools"] == ["search"]
        assert events[-1]["metadata"]["completed_tools"] == ["search"]
        assert store.list_messages("today")[-1]["content"] == "A complete note."
    else:
        assert events[-1]["type"] == "error" and "Required tools" in events[-1]["message"]
        assert not store.list_messages("today") and not store.pending_outbox()
    assert "This task requires successful calls to: search" in json.dumps(provider.requests[0])


async def test_unavailable_required_tool_fails_before_requesting_provider(tmp_path, store):
    provider = Provider([])
    engine = ConversationEngine(config(tmp_path), store, Memory(), Tools(), provider=provider)
    events = await collect(engine, interactive=False, required_tools=["not-available"])
    assert events[-1]["type"] == "error" and "unavailable" in events[-1]["message"]
    assert not provider.requests and not store.list_messages("today")


async def test_failed_agent_turn_stops_its_commands_and_preserves_completed_file_changes(tmp_path, store):
    cfg = config(tmp_path)
    registry = ToolRegistry(cfg)
    target = tmp_path / "changed.txt"
    def fail_after_tools(index):
        if index == 1:
            assert any(not item.done.is_set() for item in registry.agent.commands.values())
            raise RuntimeError("Provider disconnected")
    provider = Provider([[
        {"type": "tool_call", "call": {"id": "file-one", "function": {
            "name": "write_file", "arguments": json.dumps({"path": str(target), "content": "changed"}),
        }}},
        {"type": "tool_call", "call": {"id": "command-one", "function": {
            "name": "run_command", "arguments": '{"command":"sleep 60","yield_time_ms":0}',
        }}}, {"type": "finish", "reason": "tool_calls"},
    ], []], callback=fail_after_tools)
    try:
        await registry.set_agent_mode(True)
        engine = ConversationEngine(cfg, store, Memory(), registry, provider=provider)
        events = await collect(engine, agent_mode=True)
        assert events[-1]["type"] == "error" and "disconnected" in events[-1]["message"]
        assert target.read_text() == "changed"
        assert all(item.done.is_set() and item.process.returncode is not None
                   for item in registry.agent.commands.values())
        assert not store.list_messages("today") and not store.pending_outbox()
    finally:
        await registry.close()


async def test_disabled_memory_preference_skips_recall_even_if_memory_service_is_unavailable(
    tmp_path, store,
):
    cfg = config(tmp_path)
    cfg.memory.enabled = False
    memory = Memory(error=RuntimeError("Offline"))
    provider = Provider([[{"type": "token", "text": "Hello"},
                          {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(cfg, store, memory, provider=provider)
    events = await collect(engine, generation_id="memory-disabled")
    assert events[-1]["type"] == "completed", events
    assert not memory.queries
    recall = [event for event in events if event.get("name") == "memory_recall"]
    assert len(recall) == 1 and recall[0]["status"] == "skipped"
    assert not any(event["type"] == "recall_required" for event in events)
    assert not any(message["content"].startswith(("# Memory context\n", "# Continuity context\n"))
                   for message in provider.requests[0])


def packet(delta, finish):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


@pytest.mark.parametrize("suffix_role", ["system", "user"])
async def test_actual_requests_capture_context_and_retain_only_latest_tool_round(tmp_path, store, suffix_role):
    cfg = config(tmp_path, mode="json", post_history=suffix_role)
    cfg.providers[0].structured_verified = True
    cfg.providers[0].parameters = {"temperature": 0.2}
    cfg.providers[0].endpoint = "https://openrouter.ai/api/v1"
    store.complete_turn("today", "Earlier question", "Earlier answer")
    earlier = store.list_messages("today")
    requests = []
    def handler(request):
        requests.append(request.content.decode("utf-8"))
        if len(requests) == 1:
            Path(cfg.prompts["SOUL"]).write_text("edited after dispatch")
            content = packet({"tool_calls": [{"index": 0, "id": "search1", "type": "function",
                              "function": {"name": "search", "arguments": "{}"}}]}, "tool_calls")
        else:
            content = packet({"content": '{"expression":null,"gesture":null,"reply":"Found it 🌸"}'}, "stop")
        content += "data: " + json.dumps({"choices": [], "usage": {
            "prompt_tokens": 5000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500},
        }}) + "\n\n"
        return httpx.Response(200, content=content)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(cfg.providers[0], client=client)
        engine = ConversationEngine(cfg, store, Memory(), Tools(), provider=provider)
        events = await collect(engine, generation_id="captured")
    assert events[-1]["type"] == "completed", events
    captures = list(reversed(store.list_model_requests("today")))
    assert len(requests) == 2 and len(captures) == 1
    request_ids = [event["request_id"] for event in events if event["type"] == "model_request"]
    assert len(request_ids) == 2 and request_ids[-1] == captures[0]["id"]
    assert captures[0]["round_index"] == 1
    measurements = list(reversed(store.list_request_metrics("today")))
    assert len(measurements) == 2
    assert [row["round_index"] for row in measurements] == [0, 1]
    assert all(row["role"] == "dialogue" and row["outcome"] == "completed" for row in measurements)
    assert all(row["metrics"]["input_tokens"] == 5000
               and row["metrics"]["cache_read_tokens"] == 4000
               and row["metrics"]["cache_write_tokens"] == 500 for row in measurements)
    assert all(row["metrics"]["duration_seconds"] >= 0 for row in measurements)
    with pytest.raises(KeyError):
        store.get_model_request(request_ids[0])
    for capture, wire in zip(captures, requests[-1:]):
        saved = store.get_model_request(capture["id"])
        assert saved["body"] == wire
        payload = json.loads(saved["body"])
        assert payload["temperature"] == 0.2 and payload["stream"] is True
        assert payload["response_format"]["type"] == "json_schema"
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["search"]
        assert payload["messages"][1]["content"] == "old SOUL"
        sections = {message["content"].split("\n", 1)[0]: message["content"]
                    for message in payload["messages"] if message["role"] == "system"}
        assert "old USER" in sections["# User profile (USER.md)"]
        current = next(message["content"] for message in payload["messages"]
                       if message["role"] == "user" and "<app_context>" in message["content"])
        assert "# Memory context\n" in current and "Alex and Eidolon share a garden." in current
        assert "Return one object" in sections["# Optional cosmetic presentation"]
        for message in earlier:
            stamp = datetime.fromisoformat(message["created_at"]).isoformat(timespec="seconds")
            assert {"role": message["role"], "content": f"[{stamp}]\n{message['content']}"} \
                in payload["messages"]
        for message in payload["messages"]:
            if message["role"] in {"user", "assistant"}:
                if message.get("tool_calls"):
                    assert message["content"] == ""
                    continue
                first_line = message["content"].split("\n", 1)[0]
                assert first_line.startswith("[") and first_line.endswith("]")
                assert datetime.fromisoformat(first_line[1:-1]).tzinfo is not None
        if suffix_role == "system":
            assert payload["messages"][-1] == {"role": "system", "content": "old POST_HISTORY"}
        else:
            assert current.endswith("Hi Alex\n\n[Post-history instructions]\nold POST_HISTORY")
    assert any(message["role"] == "tool" and "Found it" in message["content"]
               for message in json.loads(requests[1])["messages"])
    first, second = [json.loads(request)["messages"] for request in requests]
    identities = [json.loads(request)["session_id"] for request in requests]
    assert identities[0] == identities[1] and identities[0] != "today"
    for heading in ("# User profile (USER.md)", "# Application context",
                    "# Optional cosmetic presentation"):
        assert [message for message in first if message["role"] == "system"
                and message["content"].startswith(heading + "\n")] == [
            message for message in second if message["role"] == "system"
            and message["content"].startswith(heading + "\n")]
    assert [message for message in first if message["role"] == "user"] == [
        message for message in second if message["role"] == "user"]
    assert "old SOUL" not in json.dumps(store.pending_outbox())


@pytest.mark.parametrize("network_failure", [False, True])
async def test_failed_provider_requests_remain_inspectable_without_saving_exchange(tmp_path, store, network_failure):
    cfg = config(tmp_path)
    def handler(request):
        if network_failure:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(400, content="invalid request")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = ConversationEngine(cfg, store, Memory(), provider=OpenAIProvider(cfg.providers[0], client=client))
        for _ in range(2):
            events = await collect(engine, generation_id="retry")
            assert events[-1]["type"] == "error"
    captures = store.list_model_requests("today")
    assert len(captures) == 1
    assert {capture["generation_id"] for capture in captures} == {"retry"}
    assert "Hi Alex" in store.get_model_request(captures[0]["id"])["body"]
    assert not store.list_messages("today") and not store.pending_outbox()
    assert [row["outcome"] for row in store.list_request_metrics("today")] == ["failed", "failed"]


async def test_cancelled_stream_preserves_request_and_closes_response(tmp_path, store):
    cfg = config(tmp_path)
    class HangingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield packet({"content": "Partial reply"}, None).encode()
            await asyncio.sleep(30)

        async def aclose(self):
            self.closed = True
    stream = HangingStream()
    received = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:
        engine = ConversationEngine(cfg, store, Memory(), provider=OpenAIProvider(cfg.providers[0], client=client))
        async def consume():
            events = []
            async for event in engine.generate("today", "Hi", generation_id="cancel-capture"):
                events.append(event)
                if event["type"] == "token":
                    received.set()
            return events
        task = asyncio.create_task(consume())
        await asyncio.wait_for(received.wait(), 1)
        engine.cancel("cancel-capture")
        events = await asyncio.wait_for(task, 1)
    assert events[-1]["code"] == "cancelled"
    assert len(store.list_model_requests("today")) == 1
    assert stream.closed
    assert not store.list_messages("today") and not store.pending_outbox()
    measurement = store.list_request_metrics("today")[0]
    assert measurement["outcome"] == "cancelled"
    assert measurement["metrics"]["time_to_first_token_seconds"] is not None


async def test_recall_failure_requires_explicit_bypass_before_any_provider_call(tmp_path, store):
    provider = Provider([[{"type": "token", "text": "Hello"}, {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path), store, Memory(OSError("offline")), provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "recall_required"
    assert not provider.requests
    assert not store.list_model_requests("today")
    assert not store.list_messages("today")
    assert not store.pending_outbox()
    events = await collect(engine, bypass_recall=True, generation_id="bypassed")
    assert events[-1]["type"] == "completed"
    assert events[-1]["metadata"]["recall_bypassed"]
    assert len(store.pending_outbox()) == 1


@pytest.mark.parametrize("suffix_role", ["system", "user"])
async def test_prompt_snapshot_hash_and_suffix_survive_information_tool_loop(tmp_path, store, suffix_role):
    cfg = config(tmp_path, post_history=suffix_role)
    call = {"id": "lookup1", "type": "function", "function": {"name": "search", "arguments": '{"query":"eidolon"}'}}
    def edit_prompt(index):
        if index == 0:
            Path(cfg.prompts["SOUL"]).write_text("new personality")
            Path(cfg.prompts["POST_HISTORY"]).write_text("new suffix")
    provider = Provider([[{"type": "tool_call", "call": call}, {"type": "finish", "reason": "tool_calls"}],
                         [{"type": "token", "text": "Found a memory."}, {"type": "finish", "reason": "stop"}]], edit_prompt)
    engine = ConversationEngine(cfg, store, Memory(), Tools(), provider=provider)
    events = await collect(engine, generation_id="stable")
    assert events[-1]["type"] == "completed", events
    assert len(provider.requests) == 2
    for messages in provider.requests:
        assert messages[1]["content"] == "old SOUL"
        latest_user = next(m for m in messages if m["role"] == "user")
        if suffix_role == "system":
            assert messages[-1] == {"role": "system", "content": "old POST_HISTORY"}
            assert latest_user["content"].endswith("# Current user request\n\nHi Alex")
        else:
            assert latest_user["content"].endswith(
                "# Current user request\n\nHi Alex\n\n"
                "[Post-history instructions]\nold POST_HISTORY")
    second = provider.requests[1]
    assistant_index = next(i for i, m in enumerate(second) if m.get("tool_calls"))
    assert second[assistant_index + 1]["role"] == "tool"
    assert second[assistant_index + 1]["tool_call_id"] == "lookup1"
    assert store.list_messages("today")[0]["content"] == "Hi Alex"
    outbox = store.pending_outbox()[0]
    assert outbox["document_id"] == "eidolon-room:exchange:stable"
    assert "old SOUL" not in json.dumps(outbox)
    preview = await engine.preview_prompt("today", "Next")
    assert preview["messages"][1]["content"] == "new personality"
    assert preview["hashes"]["SOUL"] != events[-1]["metadata"]["prompt_hashes"]["SOUL"]


class SlowAvatar:
    def __init__(self):
        self.started, self.cues, self.ended = [], [], []
        self.blocked = asyncio.Event()

    async def catalog(self):
        return {"expressions": ["happy"], "gestures": ["wave"]}

    async def begin_turn(self, *identity):
        self.started.append(identity)

    async def enqueue(self, *cue):
        self.cues.append(cue)
        await self.blocked.wait()

    async def end_turn(self, *identity):
        self.ended.append(identity)

    cancel_turn = end_turn


async def test_body_ack_never_blocks_reply_and_cues_need_no_extra_request(tmp_path, store):
    body = SlowAvatar()
    raw = '<eidolon-cues>{"expression":"happy","gesture":"wave"}</eidolon-cues>\nHello, Alex 🌸'
    provider = Provider([[*({"type": "token", "text": c} for c in raw), {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path, mode="prefix"), store, Memory(), Tools(), body, provider=provider)
    events = await asyncio.wait_for(collect(engine), 1)
    assert events[-1]["type"] == "completed", events
    assert events[-1]["text"] == "Hello, Alex 🌸"
    assert "".join(item["text"] for item in events if item["type"] == "token") == "Hello, Alex 🌸"
    assert len(provider.requests) == 1
    assert len(body.cues) == 2
    assert len(body.started) == len(body.ended) == 1
    payload = store.pending_outbox()[0]["payload"]["content"]
    assert "eidolon-cues" not in payload
    assert "Hello, Alex 🌸" in payload
    await engine.close()


async def test_background_generations_do_not_animate_body(tmp_path, store):
    body = SlowAvatar()
    provider = Provider([[{"type": "token", "text": "A quiet note"}, {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path), store, Memory(), avatar=body, provider=provider)
    events = await collect(engine, interactive=False)
    assert events[-1]["type"] == "completed"
    assert not body.started and not body.cues


async def test_disconnected_body_enqueue_failure_does_not_break_clean_chat(tmp_path, store):
    class OfflineBody(SlowAvatar):
        def enqueue(self, *args):
            raise OSError("Disconnected")
    provider = Provider([[{"type": "token", "text": '<eidolon-cues>{"expression":"happy","gesture":"wave"}</eidolon-cues>\nHi'},
                          {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path, mode="prefix"), store, Memory(), avatar=OfflineBody(), provider=provider)
    result = await collect(engine)
    assert result[-1]["type"] == "completed" and result[-1]["text"] == "Hi"
    assert [event["status"] for event in result if event.get("name") == "avatar"] == ["unavailable", "unavailable"]


@pytest.mark.parametrize("mode", ["plain", "prefix"])
async def test_cancellation_cleans_lifecycle_and_never_queues_partial_exchange(tmp_path, store, mode):
    body = SlowAvatar()
    class HangingProvider:
        async def stream(self, *args, **kwargs):
            yield {"type": "token", "text": "Partial"}
            await asyncio.sleep(30)
    engine = ConversationEngine(config(tmp_path, mode=mode), store, Memory(), avatar=body, provider=HangingProvider())
    first = asyncio.Event()
    async def consume():
        events = []
        async for event in engine.generate("today", "Hi", generation_id="cancel-me"):
            events.append(event)
            if event["type"] == "token":
                first.set()
        return events
    task = asyncio.create_task(consume())
    await first.wait()
    engine.cancel("cancel-me")
    events = await task
    assert events[-1]["code"] == "cancelled"
    assert not store.pending_outbox() and not store.list_messages("today")
    assert len(body.ended) == 1


@pytest.mark.parametrize("index_fields", [{}, {"index": 0}])
async def test_streamed_memory_and_file_calls_complete_without_retry_loop(tmp_path, store, index_fields):
    cfg = config(tmp_path)
    cfg.allowed_roots = [str(tmp_path)]
    (tmp_path / "shared.txt").write_text("A shared garden plan.")
    memory = Memory()
    registry = ToolRegistry(cfg, memory)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            stream = packet({"tool_calls": [{**index_fields, "id": "recall", "function": {
                "name": "memory_recall", "arguments": '{"query":"shared garden"}',
            }}]}, None)
            stream += packet({"tool_calls": [{**index_fields, "id": "files", "function": {
                "name": "list_files", "arguments": json.dumps({"path": str(tmp_path)}),
            }}]}, "tool_calls")
        else:
            tool_calls = next(m["tool_calls"] for m in payload["messages"] if m.get("tool_calls"))
            assert [call["function"]["name"] for call in tool_calls] == [
                "memory_recall", "list_files",
            ]
            results = {m["tool_call_id"]: m["content"] for m in payload["messages"]
                       if m["role"] == "tool"}
            assert "share a garden" in results["recall"]
            assert "shared.txt" in results["files"]
            stream = packet({"content": "I found our garden memories and shared plan."}, "stop")
        return httpx.Response(200, content=stream)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIProvider(cfg.providers[0], client=client)
            engine = ConversationEngine(cfg, store, memory, registry, provider=provider,
                                        max_tool_rounds=1)
            events = await collect(engine)
        assert events[-1]["type"] == "completed", events
        assert len(requests) == 2
        assert memory.queries[-1] == "shared garden"
        assert not any(e.get("status") == "error" for e in events)
        assert store.list_messages("today")[-1]["content"] == events[-1]["text"]
        manifest = store.list_memory_manifests()[0]["manifest"]
        assert manifest["tool_contexts"][0]["name"] == "memory_recall"
        assert manifest["tool_contexts"][0]["status"] == "completed"
    finally:
        await registry.close()


async def test_tool_round_limit_is_visible_and_does_not_save_partial(tmp_path, store):
    call = {"id": "call", "type": "function", "function": {"name": "search", "arguments": "{}"}}
    provider = Provider([[{"type": "tool_call", "call": call}, {"type": "finish", "reason": "tool_calls"}]])
    engine = ConversationEngine(config(tmp_path), store, Memory(), Tools(), provider=provider, max_tool_rounds=0)
    events = await collect(engine)
    assert events[-1]["type"] == "error"
    assert "tool-round limit" in events[-1]["message"]
    assert not store.pending_outbox()


async def test_malformed_reply_preserves_diagnostics_without_retaining_partial(tmp_path, store):
    provider = Provider([[{"type": "token", "text": '{"expression":null,"gesture":null,"reply":"Hi\\x"}'},
                         {"type": "finish", "reason": "stop"}]])
    cfg = config(tmp_path, mode="json")
    cfg.providers[0].structured_verified = True
    engine = ConversationEngine(cfg, store, Memory(), provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "error"
    assert not store.list_messages("today") and not store.pending_outbox()
    diagnostic = next(e["diagnostic_path"] for e in events if "diagnostic_path" in e)
    assert json.loads(Path(diagnostic).read_text())["raw_response"].startswith('{"expression"')
    assert Path(diagnostic).stat().st_mode & 0o777 == 0o600


async def test_omitted_cosmetic_prefix_preserves_complete_recalled_reply(tmp_path, store):
    reply = "I remember our **garden**. 🌸"
    body = SlowAvatar()
    provider = Provider([[*({"type": "token", "text": char} for char in reply),
                          {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path, mode="prefix"), store, Memory(),
                                avatar=body, provider=provider)
    events = await collect(engine, generation_id="missing-prefix")
    assert events[-1]["type"] == "completed", events
    assert events[-1]["text"] == reply
    assert "".join(e["text"] for e in events if e["type"] == "token") == reply
    assert len(provider.requests) == 1
    assert any(message["role"] == "user" and "# Memory context\n" in message["content"]
               and "<memory_context>" in message["content"]
               for message in provider.requests[0])
    assert not body.cues and len(body.ended) == 1
    assert store.list_messages("today")[-1]["content"] == reply
    assert reply in store.pending_outbox()[0]["payload"]["content"]
    assert events[-1]["metadata"]["presentation_fallback"] == "plain"
    assert "raw_presentation_errors" not in events[-1]["metadata"]
    diagnostic = next(e["diagnostic_path"] for e in events if "diagnostic_path" in e)
    saved_diagnostic = json.loads(Path(diagnostic).read_text())
    assert saved_diagnostic["raw_response"] == reply
    assert saved_diagnostic["attempt_id"] == events[-1]["metadata"]["attempt_id"]
    assert Path(diagnostic).stat().st_mode & 0o777 == 0o600
    await engine.close()


async def test_omitted_prefix_in_tool_round_continues_without_repeating_tools(tmp_path, store):
    body = SlowAvatar()
    call = {"id": "lookup", "type": "function", "function": {"name": "search", "arguments": "{}"}}
    provider = Provider([
        [{"type": "token", "text": "Let me look that up. "}, {"type": "tool_call", "call": call},
         {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": '<eidolon-cues>{"expression":"happy","gesture":null}</eidolon-cues>\nFound it.'},
         {"type": "finish", "reason": "stop"}],
    ])
    engine = ConversationEngine(config(tmp_path, mode="prefix"), store, Memory(), Tools(), body,
                                provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "completed", events
    assert events[-1]["text"] == "Let me look that up. Found it."
    assert len(provider.requests) == 2
    assert len([e for e in events if e.get("name") == "search" and e["status"] == "started"]) == 1
    assert len(body.cues) == 1
    assert len(store.pending_outbox()) == 1
    await engine.close()


@pytest.mark.parametrize("mode", ["plain", "prefix", "json"])
async def test_tool_only_continuation_has_no_synthetic_timestamp(tmp_path, store, mode):
    call = {"id": "lookup", "function": {"name": "search", "arguments": "{}"}}
    reply = {
        "plain": "Done.",
        "prefix": '<eidolon-cues>{"expression":null,"gesture":null}</eidolon-cues>\nDone.',
        "json": '{"expression":null,"gesture":null,"reply":"Done."}',
    }[mode]
    provider = Provider([
        [{"type": "tool_call", "call": call}, {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": reply}, {"type": "finish", "reason": "stop"}],
    ])
    engine = ConversationEngine(config(tmp_path, mode=mode), store, Memory(), Tools(),
                                provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "completed", events
    continuation = next(m for m in provider.requests[1] if m.get("tool_calls"))
    assert continuation["content"] == ""
    assert continuation["tool_calls"] == [call]


async def test_agent_timestamp_echo_keeps_tool_work_and_final_reply(tmp_path, store):
    cfg = config(tmp_path, mode="prefix")
    registry = ToolRegistry(cfg)
    target = tmp_path / "result.txt"
    stamp = '[2026-01-02T03:04:05+00:00]\n'
    call = {"id": "write-once", "function": {
        "name": "write_file", "arguments": json.dumps({"path": str(target), "content": "done"}),
    }}
    provider = Provider([
        [{"type": "token", "text": char} for char in stamp]
        + [{"type": "tool_call", "call": call}, {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": stamp +
          '<eidolon-cues>{"expression":null,"gesture":null}</eidolon-cues>\nDone.'},
         {"type": "finish", "reason": "stop"}],
    ])
    try:
        await registry.set_agent_mode(True)
        engine = ConversationEngine(cfg, store, Memory(), registry, provider=provider)
        events = await collect(engine, agent_mode=True)
        assert events[-1]["type"] == "completed", events
        assert events[-1]["text"] == "Done." and target.read_text() == "done"
        assert len(provider.requests) == 2
        assert len([e for e in events if e.get("name") == "write_file"
                    and e.get("status") == "started"]) == 1
        assert next(m for m in provider.requests[1] if m.get("tool_calls"))["content"] == ""
        assert ''.join(e["text"] for e in events if e["type"] == "token") == "Done."
        assert store.list_messages("today")[-1]["content"] == "Done."
        assert len(store.pending_outbox()) == 1
        assert stamp not in store.pending_outbox()[0]["payload"]["content"]
    finally:
        await registry.close()


async def test_timestamp_only_final_reply_saves_no_empty_exchange(tmp_path, store):
    provider = Provider([[{"type": "token", "text": '[2026-01-02T03:04:05+00:00]\n'},
                          {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path, mode="prefix"), store, Memory(), provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "error" and "no visible reply" in events[-1]["message"]
    assert not store.list_messages("today") and not store.pending_outbox()
    assert not any(e["type"] == "token" for e in events)


@pytest.mark.parametrize("raw", [
    '<eidolon-cues>{"expression":"happy","gesture":null}',
    '<eidolon-cues>{"expression":"happy","gesture":42}</eidolon-cues>Partial',
])
async def test_malformed_prefix_still_saves_no_partial_exchange(tmp_path, store, raw):
    body = SlowAvatar()
    provider = Provider([[{"type": "token", "text": raw}, {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(config(tmp_path, mode="prefix"), store, Memory(), avatar=body,
                                provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "error"
    assert not store.list_messages("today") and not store.pending_outbox()
    assert not body.cues
    await engine.close()


def test_history_digest_bound_retains_full_original_record():
    history = [{"role": role, "content": f"{i} " + "words " * 100}
               for i in range(40) for role in ("user", "assistant")]
    original = copy.deepcopy(history)
    counter = TokenCounter("test-model")
    recent, summary = _bounded_history(history, 2000, counter)
    assert recent[0]["role"] == "user" and recent[-1]["role"] == "assistant"
    assert len(recent) < len(history)
    assert sum(counter.message(m) for m in recent) + counter.text(summary) <= 2000
    assert summary.startswith("Earlier conversation excerpts")
    assert history == original


async def test_million_token_model_retains_history_beyond_retired_character_cap(tmp_path, store):
    cfg = config(tmp_path)
    profile = cfg.selected_provider()
    profile.model_metadata[profile.model] = ModelMetadata(
        context_window=1_000_000, source="provider /models", updated_at=0)
    for index in range(12):
        store.complete_turn("today", f"Exchange {index}. " + "Useful history. " * 250,
                            "A full original answer. " * 200)
    original = store.list_messages("today")
    assert sum(len(message["content"]) for message in original) > 32_000
    provider = Provider([[{"type": "token", "text": "Hello"}, {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(cfg, store, Memory(), provider=provider)
    preview = await engine.preview_prompt("today", "Hi Alex")
    events = await collect(engine)
    assert events[-1]["type"] == "completed", events
    sent = [message for message in provider.requests[0] if message["role"] in {"user", "assistant"}]
    assert len(sent[:-1]) == len(original)
    for message, record in zip(sent[:-1], original, strict=True):
        stamp = datetime.fromisoformat(record["created_at"]).isoformat(timespec="seconds")
        assert message == {"role": record["role"], "content": f"[{stamp}]\n{record['content']}"}
    preview_history = [message for message in preview["messages"]
                       if message["role"] in {"user", "assistant"}]
    assert preview_history[:-1] == sent[:-1]
    assert engine.metrics["today"]["context_window"] == 1_000_000
    assert engine.metrics["today"]["context_estimated"]


async def test_token_window_has_no_million_character_ceiling(tmp_path, store):
    cfg = config(tmp_path)
    cfg.selected_provider().context_window = 1_000_000
    provider = Provider([[{"type": "token", "text": "Read it"}, {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(cfg, store, Memory(), provider=provider)
    message = "A useful sentence. " * 60_000
    assert len(message) > 1_000_000
    events = [event async for event in engine.generate("today", message)]
    assert events[-1]["type"] == "completed", events[-1]
    assert provider.requests[0][-2]["content"].endswith("# Current user request\n\n" + message)
    assert engine.metrics["today"]["context_tokens"] < 1_000_000


async def test_multiple_tool_results_use_discovered_window_instead_of_character_cap(tmp_path, store):
    cfg = config(tmp_path)
    profile = cfg.selected_provider()
    profile.model_metadata[profile.model] = ModelMetadata(
        context_window=1_000_000, source="provider /models", updated_at=0)
    result = "A detailed observation. " * 600

    class LargeTools(Tools):
        async def call(self, name, args, context=None):
            return result

    rounds = [[{"type": "tool_call", "call": {"id": f"call{i}", "function": {
        "name": "search", "arguments": "{}",
    }}}, {"type": "finish", "reason": "tool_calls"}] for i in range(4)]
    rounds.append([{"type": "token", "text": "Done"}, {"type": "finish", "reason": "stop"}])
    provider = Provider(rounds)
    engine = ConversationEngine(cfg, store, Memory(), LargeTools(), provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "completed", events
    results = [m for m in provider.requests[-1] if m["role"] == "tool"]
    assert len(results) == 4 and all(m["content"] == result for m in results)
    assert sum(len(m["content"]) for m in results) > 32_000
    assert all(event["metrics"]["context_window"] == 1_000_000
               for event in events if event["type"] == "metrics")


async def test_history_is_rebudgeted_after_tools_without_losing_saved_exchanges(tmp_path, store):
    cfg = config(tmp_path)
    cfg.selected_provider().context_window = 6000
    cfg.selected_provider().parameters = {"max_tokens": 512}
    for index in range(12):
        store.complete_turn("today", f"Exchange {index}: " + "history detail " * 100,
                            "A full answer. " * 100)
    original = store.list_messages("today")

    class LargeTools(Tools):
        async def call(self, name, args, context=None):
            return "A detailed observation. " * 600

    provider = Provider([
        [{"type": "tool_call", "call": {"id": "call1", "function": {
            "name": "search", "arguments": "{}",
        }}}, {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": "Done"}, {"type": "finish", "reason": "stop"}],
    ])
    tools = LargeTools()
    engine = ConversationEngine(cfg, store, Memory(), tools, provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "completed", events
    first, second = provider.requests
    assert len([m for m in second if m["role"] == "user"]) < len([
        m for m in first if m["role"] == "user"])
    counter = TokenCounter(cfg.selected_provider().model)
    budget = ContextBudget.for_profile(cfg.selected_provider())
    assert all(budget.fits(counter.request(messages, tools.schemas()))
               for messages in provider.requests)
    assert any(m["role"] == "tool" and m["tool_call_id"] == "call1" for m in second)
    assert store.list_messages("today")[:-2] == original


async def test_tool_overflow_replaces_previous_usage_with_the_rejected_request_budget(tmp_path, store):
    cfg = config(tmp_path)
    cfg.selected_provider().context_window = 2000
    cfg.selected_provider().parameters = {"max_tokens": 512}

    class LargeTools(Tools):
        async def call(self, name, args, context=None):
            return "A long tool result with many details. " * 400

    provider = Provider([[
        {"type": "usage", "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        {"type": "tool_call", "call": {"id": "call1", "function": {
            "name": "search", "arguments": "{}",
        }}}, {"type": "finish", "reason": "tool_calls"},
    ]])
    engine = ConversationEngine(cfg, store, Memory(), LargeTools(), provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "error"
    assert "2,000-token context window" in events[-1]["message"]
    assert len(provider.requests) == 1
    metrics = [event["metrics"] for event in events if event["type"] == "metrics"]
    assert metrics[-2]["context_tokens"] == 110 and not metrics[-2]["context_estimated"]
    assert metrics[-1]["context_tokens"] > 2000 and metrics[-1]["context_estimated"]
    assert engine.metrics["today"] == metrics[-1]
    assert not store.list_messages("today") and not store.pending_outbox()


@pytest.mark.parametrize("oversized", ["prompt", "schema", "output"])
async def test_actual_overflow_updates_meter_before_error_and_never_dispatches(tmp_path, store, oversized):
    cfg = config(tmp_path)
    cfg.selected_provider().context_window = 2000
    tools = Tools()
    if oversized == "prompt":
        Path(cfg.prompts["SOUL"]).write_text("Important prompt text. " * 2000)
    elif oversized == "schema":
        tools.schemas = lambda **kwargs: [{"type": "function", "function": {
            "name": "search", "description": "Long schema description. " * 2000,
        }}]
    else:
        cfg.selected_provider().parameters = {"max_completion_tokens": 2500}
    provider = Provider([])
    engine = ConversationEngine(cfg, store, Memory(), tools, provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "error"
    assert "2,000-token context window" in events[-1]["message"]
    assert "context_chars" not in events[-1]["message"]
    assert not provider.requests and not store.list_messages("today")
    assert events[-2]["type"] == "metrics"
    assert events[-2]["metrics"]["context_tokens"] > 2000
    assert engine.metrics["today"]["context_estimated"]


async def test_unknown_capacity_has_no_hidden_history_limit_and_usage_replaces_estimate(tmp_path, store):
    cfg = config(tmp_path)
    original = "A long original message. " * 3000
    store.complete_turn("today", original, "The original reply.")
    provider = Provider([[{"type": "token", "text": "Hello"},
                          {"type": "usage", "usage": {}, "duration": 1},
                          {"type": "usage", "usage": {"prompt_tokens": 24_000,
                                                        "completion_tokens": 50}, "duration": 1},
                          {"type": "finish", "reason": "stop"}]])
    engine = ConversationEngine(cfg, store, Memory(), provider=provider)
    events = await collect(engine)
    assert events[-1]["type"] == "completed", events
    assert any(message["content"].partition("\n")[2] == original
               for message in provider.requests[0])
    metrics = [event["metrics"] for event in events if event["type"] == "metrics"]
    assert metrics[0]["context_window"] is None
    assert metrics[0]["context_estimated"] and metrics[1]["context_estimated"]
    assert metrics[1]["context_tokens"] == metrics[0]["context_tokens"]
    assert metrics[-1]["context_tokens"] == 24_050 and not metrics[-1]["context_estimated"]
