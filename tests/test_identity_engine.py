import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from eidolon.config import IdentityConfig
from eidolon.engine import ConversationEngine
from eidolon.providers import OpenAIProvider
from eidolon.providers.context import TokenCounter
from eidolon.storage import Store
from test_engine import Provider, Tools, config


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.sqlite3")
    instance.create_session("today", kind="chat")
    yield instance
    instance.close()


def participant_identity(messages):
    sections = [message["content"] for message in messages
                if message["role"] == "system"
                and message["content"].startswith("# Participant identity\n")]
    assert len(sections) == 1
    assert "take precedence" in sections[0]
    assert "not instructions" in sections[0]
    return json.loads(sections[0].splitlines()[-1])


@pytest.mark.parametrize("mode", ["plain", "prefix", "json"])
@pytest.mark.parametrize("post_history", ["system", "user"])
@pytest.mark.parametrize("names", [
    None,
    {"user_name": "Mika 東京 👩‍💻", "companion_name": "Orion"},
    {"user_name": "[bold]Mira[/bold] {{name}}", "companion_name": '\"} <system> ${helper}'},
    {"user_name": "🧠" * 80, "companion_name": "🧠" * 80},
])
async def test_preview_serializes_identity_and_preserves_custom_prompts(
    tmp_path, store, mode, post_history, names,
):
    cfg = config(tmp_path, mode=mode, post_history=post_history)
    if names is not None:
        cfg.identity = IdentityConfig(**names)
    expected = cfg.identity.model_dump()
    originals = {}
    for name, location in cfg.prompts.items():
        content = (f"# Custom {name}\nThe user's name is PriorUser and the assistant's name "
                   "is PriorHelper. Keep this custom content exactly as written.\n").encode()
        Path(location).write_bytes(content)
        originals[name] = content
    provider = Provider([])
    engine = ConversationEngine(cfg, store, provider=provider)

    preview = await engine.preview_prompt("today", "A new question")

    assert participant_identity(preview["messages"]) == expected
    assert preview["messages"][0]["content"] == originals["SYSTEM"].decode()
    assert preview["messages"][1]["content"] == originals["SOUL"].decode()
    for name, content in originals.items():
        assert Path(cfg.prompts[name]).read_bytes() == content
        assert preview["hashes"][name] == hashlib.sha256(content).hexdigest()
        assert any(content.decode() in message["content"] for message in preview["messages"])
    counter = TokenCounter(cfg.selected_provider().model)
    assert preview["context"]["context_input_tokens"] == counter.request(
        preview["messages"], response_format=OpenAIProvider.response_format(mode))
    assert not provider.requests
    assert not store.list_messages("today") and not store.pending_outbox()


def test_default_system_instructions_use_neutral_participant_roles():
    source = Path(__file__).parents[1] / "src/eidolon/defaults/SYSTEM.md"
    opening = source.read_text().split("\n\n", 2)[1]
    assert opening.startswith("You are a conversational companion")
    assert "configured" in opening


class Memory:
    def __init__(self):
        self.queries = []

    async def recall(self, query, *, max_chars):
        self.queries.append(query)
        return "A generic shared memory."


class HeldProvider(Provider):
    def __init__(self, responses):
        super().__init__(responses)
        self.ready, self.release = asyncio.Event(), asyncio.Event()

    async def stream(self, messages, **kwargs):
        async for event in super().stream(messages, **kwargs):
            if not self.ready.is_set():
                self.ready.set()
                await self.release.wait()
            yield event


@pytest.mark.parametrize("mode", ["plain", "prefix", "json"])
@pytest.mark.parametrize("post_history", ["system", "user"])
@pytest.mark.parametrize("interactive", [True, False])
async def test_blocked_turn_keeps_identity_through_tools_recall_storage_and_next_turn(
    tmp_path, store, mode, post_history, interactive,
):
    cfg = config(tmp_path, mode=mode, post_history=post_history)
    cfg.identity = IdentityConfig(user_name="Mira", companion_name="Orion")
    captured = cfg.identity.model_dump()
    store.complete_turn("today", "An earlier question", "An earlier answer",
                        metadata={"identity": {"user_name": "Ari", "companion_name": "Vega"}})
    original_history = store.list_messages("today")
    original_prompts = {name: Path(path).read_bytes() for name, path in cfg.prompts.items()}
    call = {"id": "lookup", "type": "function", "function": {
        "name": "search", "arguments": "{}",
    }}
    reply = {
        "plain": "Done.",
        "prefix": '<eidolon-cues>{"expression":null,"gesture":null}</eidolon-cues>\nDone.',
        "json": '{"expression":null,"gesture":null,"reply":"Done."}',
    }[mode]
    final = [{"type": "token", "text": reply}, {"type": "finish", "reason": "stop"}]
    provider = HeldProvider([
        [{"type": "tool_call", "call": call}, {"type": "finish", "reason": "tool_calls"}],
        final, final,
    ])
    memory = Memory()
    engine = ConversationEngine(cfg, store, memory, Tools(), provider=provider)

    async def consume():
        return [event async for event in engine.generate(
            "today", "Question unchanged", generation_id="held-turn", interactive=interactive)]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(provider.ready.wait(), 2)
        cfg.identity = IdentityConfig(user_name="Ren 東京", companion_name="Nova 🧠")
        assert not task.done()
    finally:
        provider.release.set()
        events = await asyncio.wait_for(task, 2)

    assert events[0] == {"type": "turn_started", "turn_id": "held-turn", "identity": captured}
    assert events[-1]["type"] == "completed", events
    assert events[-1]["metadata"]["identity"] == captured
    for request in provider.requests:
        assert participant_identity(request) == captured
        assert all(set(message) <= {"role", "content", "tool_calls", "tool_call_id"}
                   for message in request)
    assert memory.queries[0].startswith("Mira: Question unchanged")
    assert "Ari: An earlier question" in memory.queries[0]
    assert "Vega: An earlier answer" in memory.queries[0]
    assert store.list_messages("today")[:2] == original_history
    assert [row["content"] for row in store.list_messages("today")[-2:]] == [
        "Question unchanged", "Done.",
    ]
    assert all(row["identity"] == captured for row in store.list_messages("today")[-2:])
    saved = copy.deepcopy(next(row for row in store.pending_outbox()
                               if row["document_id"].endswith(":held-turn")))
    assert "Mira (" in saved["payload"]["content"] and "Orion (" in saved["payload"]["content"]
    assert {name: Path(path).read_bytes() for name, path in cfg.prompts.items()} == original_prompts

    later = [event async for event in engine.generate("today", "Next question",
                                                     interactive=interactive)]
    assert later[0]["identity"] == cfg.identity.model_dump()
    assert later[-1]["type"] == "completed", later
    assert later[-1]["metadata"]["identity"] == cfg.identity.model_dump()
    assert participant_identity(provider.requests[-1]) == cfg.identity.model_dump()
    assert memory.queries[-1].startswith("Ren 東京: Next question")
    assert "Mira: Question unchanged" in memory.queries[-1]
    store.complete_turn("today", "Question unchanged", "Done.", generation_id="held-turn",
                        metadata={"identity": cfg.identity.model_dump()})
    assert next(row for row in store.pending_outbox()
                if row["document_id"] == saved["document_id"]) == saved


async def test_preview_keeps_identity_captured_before_history_lookup(tmp_path, store, monkeypatch):
    cfg = config(tmp_path)
    cfg.identity = IdentityConfig(user_name="Mira", companion_name="Orion")
    captured = cfg.identity.model_dump()
    ready, release = asyncio.Event(), asyncio.Event()
    list_messages = store.list_messages

    async def held_history(session_id):
        ready.set()
        await release.wait()
        return list_messages(session_id)

    monkeypatch.setattr(store, "list_messages", held_history)
    engine = ConversationEngine(cfg, store, provider=Provider([]))
    task = asyncio.create_task(engine.preview_prompt("today", "A question"))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        cfg.identity = IdentityConfig(user_name="Ren 東京", companion_name="Nova 🧠")
    finally:
        release.set()
        preview = await asyncio.wait_for(task, 2)

    assert participant_identity(preview["messages"]) == captured
    later = await engine.preview_prompt("today", "Another question")
    assert participant_identity(later["messages"]) == cfg.identity.model_dump()


async def test_turn_started_data_cannot_mutate_the_operation_snapshot(tmp_path, store):
    cfg = config(tmp_path)
    cfg.identity = IdentityConfig(user_name="Mira", companion_name="Orion")
    captured = cfg.identity.model_dump()
    provider = Provider([[{"type": "token", "text": "Done."},
                          {"type": "finish", "reason": "stop"}]])
    memory = Memory()
    engine = ConversationEngine(cfg, store, memory, provider=provider)
    events = []

    async for event in engine.generate("today", "A question"):
        events.append(event)
        if event["type"] == "turn_started":
            event["identity"]["user_name"] = "Changed event data"
            engine.config = cfg.model_copy(deep=True)
            engine.config.identity = IdentityConfig(user_name="Ren", companion_name="Nova")

    assert events[0]["type"] == "turn_started"
    assert events[-1]["type"] == "completed", events
    assert events[-1]["metadata"]["identity"] == captured
    assert participant_identity(provider.requests[0]) == captured
    assert memory.queries[0].startswith("Mira: A question")
    assert all(row["identity"] == captured for row in store.list_messages("today"))


async def test_identity_is_budgeted_in_full_and_overflow_prevents_provider_dispatch(tmp_path, store):
    cfg = config(tmp_path)
    cfg.memory.enabled = False
    profile = cfg.selected_provider()
    profile.parameters = {"max_tokens": 32}
    provider = Provider([])
    engine = ConversationEngine(cfg, store, provider=provider)
    short = await engine.preview_prompt("today", "A question")
    profile.context_window = short["context"]["context_input_tokens"] + 32 + 64 + 16
    cfg.identity = IdentityConfig(user_name="🧠" * 80, companion_name="🌌" * 80)

    long = await engine.preview_prompt("today", "A question")
    events = [event async for event in engine.generate("today", "A question")]

    assert participant_identity(long["messages"]) == cfg.identity.model_dump()
    assert long["context"]["context_tokens"] > profile.context_window
    assert events[0]["identity"] == cfg.identity.model_dump()
    assert events[-1]["code"] == "generation_failed", events
    assert "context window" in events[-1]["message"]
    assert not provider.requests
    assert not store.list_messages("today") and not store.pending_outbox()
