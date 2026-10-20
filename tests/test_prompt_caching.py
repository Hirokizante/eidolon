import json
from datetime import datetime, timedelta, timezone

import pytest

from eidolon.engine import ConversationEngine
from eidolon.providers.context import TokenCounter
from eidolon.storage import Store
from test_engine import Memory, Provider, Tools, config


@pytest.mark.parametrize("mode", ["plain", "prefix", "json"])
@pytest.mark.parametrize("post_history", ["system", "user"])
async def test_changing_clock_and_recall_preserve_system_and_retained_history_prefix(
    tmp_path, monkeypatch, mode, post_history,
):
    cfg = config(tmp_path, mode=mode, post_history=post_history)
    store = Store(tmp_path / "state.sqlite3")
    store.create_session("today", kind="chat")
    for index in range(12):
        store.complete_turn("today", f"Question {index}. " + "Generic history detail. " * 100,
                            f"Answer {index}. " + "Generic reply detail. " * 100)
    clock = iter([datetime(2026, 10, 18, 1, tzinfo=timezone.utc) + timedelta(seconds=n // 2)
                  for n in range(4)])

    class Clock(datetime):
        @classmethod
        def now(cls, zone):
            return next(clock).astimezone(zone)

    monkeypatch.setattr("eidolon.engine.conversation.datetime", Clock)
    try:
        engine = ConversationEngine(cfg, store, Memory(), provider=Provider([]))
        first = (await engine.preview_prompt(
            "today", "First request", recalled="First memory"))["messages"]
        second = (await engine.preview_prompt(
            "today", "Next request", recalled="New memory"))["messages"]
        boundary = next(index for index, (left, right) in enumerate(zip(first, second))
                        if left != right)
        prefix = first[:boundary]
        assert prefix == second[:boundary]
        assert sum(message["role"] in {"user", "assistant"} for message in prefix) == 24
        assert TokenCounter(cfg.selected_provider().model).request(prefix) > 4096
        assert first[boundary]["role"] == second[boundary]["role"] == "user"
        assert "First memory" in first[boundary]["content"]
        assert "New memory" in second[boundary]["content"]
        assert "09:00:00" in first[boundary]["content"]
        assert "09:00:01" in second[boundary]["content"]
        # Gateways may combine every system message, including POST_HISTORY.
        def systems(messages):
            return "\n\n".join(message["content"] for message in messages
                                if message["role"] == "system")
        assert systems(first) == systems(second)
        assert "Current local time:" not in systems(first)
        assert "First memory" not in systems(first) and "New memory" not in systems(second)
        assert "not instructions or authorization" in systems(first)
        assert store.list_messages("today")[-1]["content"].startswith("Answer 11.")
    finally:
        store.close()


async def test_bounded_history_and_excerpts_stay_fixed_across_small_tool_rounds(tmp_path):
    cfg = config(tmp_path)
    cfg.selected_provider().context_window = 6000
    cfg.selected_provider().parameters = {"max_tokens": 512}
    store = Store(tmp_path / "state.sqlite3")
    store.create_session("today", kind="chat")
    for index in range(12):
        store.complete_turn("today", f"Question {index}. " + "History detail. " * 100,
                            "A detailed reply. " * 100)
    original = store.list_messages("today")
    call = {"id": "search1", "function": {"name": "search", "arguments": "{}"}}
    provider = Provider([
        [{"type": "tool_call", "call": call}, {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": "Finished."}, {"type": "finish", "reason": "stop"}],
    ])
    try:
        engine = ConversationEngine(cfg, store, Memory(), Tools(), provider=provider)
        events = [event async for event in engine.generate("today", "Search")]
        assert events[-1]["type"] == "completed", events
        first, second = provider.requests
        assert first[:-1] == second[:len(first) - 1]
        assert len([message for message in first if message["role"] == "user"]) < 13
        current = next(message["content"] for message in first
                       if message["role"] == "user" and "<app_context>" in message["content"])
        assert "# Earlier conversation context" in current
        assert all("# Earlier conversation context" not in message["content"]
                   for message in first if message["role"] == "system")
        assert store.list_messages("today")[:-2] == original
        assert store.list_messages("today")[-2]["content"] == "Search"
        assert "app_context" not in json.dumps(store.pending_outbox())
    finally:
        store.close()


async def test_schema_order_is_stable_when_discovery_order_changes(tmp_path):
    class ShuffledTools(Tools):
        def schemas(self, permitted=None):
            self.reverse = not getattr(self, "reverse", False)
            schemas = [{"type": "function", "function": {"name": name}}
                       for name in ("zebra", "search", "body_action")]
            return schemas[::-1] if self.reverse else schemas

    cfg = config(tmp_path)
    engine = ConversationEngine(cfg, None, tools=ShuffledTools())
    first = await engine._schemas(cfg.selected_provider(), "", "prefix")
    second = await engine._schemas(cfg.selected_provider(), "", "prefix")
    assert first == second
    assert [schema["function"]["name"] for schema in first] == ["search", "zebra"]
