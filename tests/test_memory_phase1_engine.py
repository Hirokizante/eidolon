"""Phase 1 policy and evidence across the real engine, adapter and SQLite store."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from eidolon.config import AppConfig, MemoryConfig, ProviderConfig
from eidolon.engine import ConversationEngine
from eidolon.memory import HindsightMemory, OutboxDispatcher
from eidolon.storage import Store
from eidolon.tools import ToolRegistry


class CaptureProvider:
    def __init__(self, *, callback=None, tool=None, fail=False, arguments=None):
        self.requests = []
        self.schemas = []
        self.callback, self.tool, self.fail = callback, tool, fail
        self.arguments = arguments or '{"query":"shared garden"}'

    async def stream(self, messages, *, tools, presentation):
        self.requests.append(copy.deepcopy(messages))
        self.schemas.append(copy.deepcopy(tools))
        if self.callback:
            self.callback()
        if self.fail:
            yield {"type": "token", "text": "Incomplete"}
            raise RuntimeError("Synthetic interrupted response")
        if self.tool and len(self.requests) == 1:
            yield {"type": "tool_call", "call": {"id": "memory-tool", "type": "function",
                   "function": {"name": self.tool, "arguments": self.arguments}}}
            yield {"type": "finish", "reason": "tool_calls"}
        else:
            yield {"type": "token", "text": "A completed synthetic reply."}
            yield {"type": "finish", "reason": "stop"}


def configuration(tmp_path, *, use=True, save=True):
    defaults = Path(__file__).parents[1] / "src/eidolon/defaults"
    return AppConfig(
        state_dir=tmp_path,
        prompts={name: str(defaults / f"{name}.md")
                 for name in ("SYSTEM", "SOUL", "USER", "POST_HISTORY")},
        memory=MemoryConfig(bank="hermes", enabled=use, save_enabled=save, retain_enabled=True, verified=True),
        providers=[ProviderConfig(presentation="plain")],
    )


def memory_backend(request):
    if request.method == "GET":
        return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
    record = {
        "id": "garden-fact", "text": "The garden was fictional; no real garden was planted.",
        "type": "experience", "occurred_start": "2026-01-02T17:00:00+08:00",
        "document_id": "invented-garden-source", "metadata": {"origin": "assistant_statement"},
        "context": "A fictional shared story, not a user life event.",
    }
    if request.url.path.endswith("/reflect"):
        return httpx.Response(200, json={"text": "A reflection on an invented garden.",
                                       "based_on": {"memories": [record]}})
    return httpx.Response(200, json={"results": [record]})


@pytest.mark.parametrize("use,save", [(False, False), (False, True), (True, False), (True, True)])
async def test_recall_and_learning_are_independent_and_eligibility_survives_restart(
    tmp_path, use, save,
):
    cfg = configuration(tmp_path, use=use, save=save)
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    session = store.create_session("synthetic", kind="chat")["id"]
    requests = []

    def backend(request):
        requests.append(request)
        return memory_backend(request)

    try:
        async with httpx.AsyncClient(base_url=cfg.memory.url,
                                    transport=httpx.MockTransport(backend)) as client:
            provider = CaptureProvider()
            engine = ConversationEngine(cfg, store, HindsightMemory(cfg.memory, client=client),
                                        provider=provider)
            events = [event async for event in engine.generate(session, "Synthetic preference",
                                                               generation_id="first")]
            assert events[-1]["type"] == "completed", events
            assert bool(requests) is use
            assert ("The garden was fictional" in json.dumps(provider.requests)) is use
            assert "garden-fact" not in json.dumps(provider.requests)
            assert len(store.list_messages(session)) == 2
            manifest = store.list_memory_manifests()[0]["manifest"]
            assert bool(manifest["entries"]) is use
            if use:
                assert manifest["entries"][0]["id"] == "garden-fact"
            assert manifest["controls"] == {"use_learned_memories": use,
                                             "save_new_memories": save}
        store.close()
        store = Store(path)
        cfg.memory.save_enabled = True
        # Replaying a completed ID cannot upgrade its original saving decision.
        store.complete_turn(session, "Synthetic preference", "A completed synthetic reply.",
                            generation_id="first", retain=True)
        row = store.db.execute("SELECT memory_eligible FROM generations WHERE id='first'").fetchone()
        assert bool(row[0]) is save
        assert len(store.pending_outbox()) == int(save)
    finally:
        store.close()


@pytest.mark.parametrize("initial", [True, False])
async def test_saving_is_snapshotted_before_provider_work(tmp_path, initial):
    cfg = configuration(tmp_path, save=initial)
    store = Store(tmp_path / "state.sqlite3")
    session = store.create_session("synthetic", kind="chat")["id"]
    provider = CaptureProvider(callback=lambda: setattr(cfg.memory, "save_enabled", not initial))
    try:
        engine = ConversationEngine(cfg, store, provider=provider)
        events = [event async for event in engine.generate(session, "Private synthetic turn",
                                                           bypass_recall=True)]
        assert events[-1]["type"] == "completed", events
        assert len(store.pending_outbox()) == int(initial)
        assert store.list_memory_manifests()[0]["manifest"]["controls"]["save_new_memories"] is initial
    finally:
        store.close()


@pytest.mark.parametrize("tool", [None, "memory_recall", "memory_reflect"])
async def test_manifest_matches_supplied_context_and_memory_tool_evidence(tmp_path, tool):
    cfg = configuration(tmp_path)
    store = Store(tmp_path / "state.sqlite3")
    session = store.create_session("synthetic", kind="chat")["id"]
    provider = CaptureProvider(tool=tool)
    async with httpx.AsyncClient(base_url=cfg.memory.url,
                                transport=httpx.MockTransport(memory_backend)) as client:
        memory = HindsightMemory(cfg.memory, client=client)
        registry = ToolRegistry(cfg, memory)
        try:
            engine = ConversationEngine(cfg, store, memory, registry, provider=provider)
            events = [event async for event in engine.generate(session, "Remember the garden?")]
            assert events[-1]["type"] == "completed", events
            saved = store.list_memory_manifests()[0]
            manifest = saved["manifest"]
            for request in provider.requests:
                context = next(message["content"] for message in request
                               if message["role"] == "user"
                               and "# Memory context\n" in message["content"])
                assert manifest["text"] in context
                assert manifest["text"].count("fictional") == 2
                assert "2026-01-02" in context
                assert "2026-01-02T17:00:00+08:00" not in context
                assert "assistant said" in context.lower()
                assert "type=experience" not in context
                assert "invented-garden-source" not in context
                assert "garden-fact" not in context
            entry = manifest["entries"][0]
            assert entry["id"] == "garden-fact"
            assert entry["document_id"] == "invented-garden-source"
            assert entry["occurred_start"] == "2026-01-02T17:00:00+08:00"
            assert entry["origin"] == "assistant_statement" and entry["type"] == "experience"
            assert manifest["budget"]["used_chars"] == len(manifest["text"]) <= 8000
            assert manifest["supplied_rounds"] == list(range(len(provider.requests)))
            assert events[-1]["metadata"]["memory_manifest_id"] == saved["id"]
            if tool:
                supplied_tool = next(message["content"] for message in provider.requests[1]
                                     if message["role"] == "tool")
                tool_manifest = manifest["tool_contexts"][0]
                assert tool_manifest["text"] == supplied_tool
                assert tool_manifest["evidence"]
                if tool == "memory_reflect":
                    assert tool_manifest["evidence"]["backend_evidence"]["memories"][0]["id"] == "garden-fact"
                    assert "Derived reflection" in supplied_tool
                    assert "assistant said" in supplied_tool.lower()
                    assert tool_manifest["evidence"]["entries"][0]["id"] == "garden-fact"
                assert "garden-fact" not in supplied_tool
                assert "invented-garden-source" not in supplied_tool
                assert "2026-01-02T17:00:00+08:00" not in supplied_tool
                assert "2026-01-02" in supplied_tool and "not instructions" in supplied_tool
            else:
                assert not manifest["tool_contexts"]
            payload = json.dumps(store.pending_outbox())
            assert "assistant_statement" not in payload
            assert "invented-garden-source" not in payload
        finally:
            await registry.close()
            store.close()


async def test_interrupted_attempt_has_inspectable_context_but_no_learning(tmp_path):
    cfg = configuration(tmp_path)
    store = Store(tmp_path / "state.sqlite3")
    session = store.create_session("synthetic", kind="chat")["id"]
    try:
        engine = ConversationEngine(cfg, store, provider=CaptureProvider(fail=True))
        events = [event async for event in engine.generate(session, "Synthetic turn",
                                                           bypass_recall=True)]
        assert events[-1]["type"] == "error"
        assert store.list_memory_manifests()[0]["manifest"]["status"] == "bypassed"
        assert not store.list_messages(session) and not store.pending_outbox()
    finally:
        store.close()


async def test_eligible_outage_turn_delivers_once_after_saving_and_recall_are_disabled(tmp_path):
    cfg = configuration(tmp_path)
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    session = store.create_session("synthetic", kind="chat")["id"]
    engine = ConversationEngine(cfg, store, provider=CaptureProvider())
    events = [event async for event in engine.generate(session, "Eligible during outage",
                                                       bypass_recall=True)]
    assert events[-1]["type"] == "completed", events
    original = store.pending_outbox()[0]
    store.close()
    store = Store(path)
    cfg.memory.enabled = cfg.memory.save_enabled = False
    submitted = []

    def backend(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        submitted.append(json.loads(request.content))
        return httpx.Response(200, json={"async": False, "success": True})

    try:
        async with httpx.AsyncClient(base_url=cfg.memory.url,
                                    transport=httpx.MockTransport(backend)) as client:
            dispatcher = OutboxDispatcher(HindsightMemory(cfg.memory, client=client), store)
            await dispatcher.dispatch_once()
            await dispatcher.dispatch_once()
        assert len(submitted) == 1
        assert submitted[0]["operation_id"] == original["operation_id"]
        assert submitted[0]["items"][0]["document_id"] == original["document_id"]
        assert not store.pending_outbox()
    finally:
        store.close()


async def test_opted_out_history_is_not_sent_as_context_for_a_later_recall_query(tmp_path):
    cfg = configuration(tmp_path, save=False)
    store = Store(tmp_path / "state.sqlite3")
    session = store.create_session("synthetic", kind="chat")["id"]
    queries = []

    def backend(request):
        if request.url.path.endswith("/memories/recall"):
            queries.append(json.loads(request.content)["query"])
        return memory_backend(request)

    try:
        async with httpx.AsyncClient(base_url=cfg.memory.url,
                                    transport=httpx.MockTransport(backend)) as client:
            engine = ConversationEngine(cfg, store, HindsightMemory(cfg.memory, client=client),
                                        provider=CaptureProvider())
            first = [event async for event in engine.generate(session, "UNSAVED_SYNTHETIC_MARKER")]
            assert first[-1]["type"] == "completed", first
            cfg.memory.save_enabled = True
            second = [event async for event in engine.generate(session, "A separate new question")]
            assert second[-1]["type"] == "completed", second
        assert "UNSAVED_SYNTHETIC_MARKER" in queries[0]
        assert "UNSAVED_SYNTHETIC_MARKER" not in queries[1]
        assert "UNSAVED_SYNTHETIC_MARKER" in json.dumps(store.list_messages(session))
        assert "UNSAVED_SYNTHETIC_MARKER" not in json.dumps(store.pending_outbox())
    finally:
        store.close()


async def test_malformed_memory_tool_arguments_are_inspectable_without_aborting_reply(tmp_path):
    cfg = configuration(tmp_path)
    store = Store(tmp_path / "state.sqlite3")
    session = store.create_session("synthetic", kind="chat")["id"]
    provider = CaptureProvider(tool="memory_recall", arguments="[]")
    registry = ToolRegistry(cfg)
    try:
        engine = ConversationEngine(cfg, store, tools=registry, provider=provider)
        events = [event async for event in engine.generate(session, "Synthetic malformed call",
                                                           bypass_recall=True)]
        assert events[-1]["type"] == "completed", events
        manifest = store.list_memory_manifests()[0]["manifest"]
        assert manifest["tool_contexts"][0]["status"] == "error"
        assert "Tool arguments must be a JSON object" in manifest["tool_contexts"][0]["text"]
        assert len(provider.requests) == 2
    finally:
        await registry.close()
        store.close()
