"""Synthetic continuity assembly and the actual foreground conversation path."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eidolon.config import AppConfig, MemoryConfig, ProviderConfig
from eidolon.engine import ConversationEngine
from eidolon.memory.continuity import validate_operations
from eidolon.memory.coordinator import MemoryCoordinator, combine_context, assemble_context
from eidolon.memory.models import MemoryRecord, RecallResult
from eidolon.storage import Store


class Provider:
    def __init__(self, reply="A completed reply.", fail=False):
        self.requests = []
        self.reply, self.fail = reply, fail

    async def stream(self, messages, *, tools, presentation):
        self.requests.append(copy.deepcopy(messages))
        yield {"type": "token", "text": self.reply}
        if self.fail:
            raise RuntimeError("Synthetic interruption")
        yield {"type": "finish", "reason": "stop"}


class Memory:
    def __init__(self, callback=None, text="Historical source."):
        self.callback, self.text = callback, text
        self.budgets = []

    async def recall_result(self, query, *, max_chars, enabled):
        self.budgets.append(max_chars)
        if self.callback:
            self.callback()
        return RecallResult(records=(MemoryRecord(id="historic", text=self.text),), query=query)


def configuration(tmp_path, **memory):
    defaults = Path(__file__).parents[1] / "src/eidolon/defaults"
    return AppConfig(
        state_dir=tmp_path,
        prompts={name: str(defaults / f"{name}.md")
                 for name in ("SYSTEM", "SOUL", "USER", "POST_HISTORY")},
        memory=MemoryConfig(**{"enabled": True, "save_enabled": True, "bank": "hermes",
                               "continuity_use_enabled": True, **memory}),
        providers=[ProviderConfig(presentation="plain")],
    )


def seed(store, text="The garden illustration is unfinished.", **fields):
    store.create_session("previous", kind="chat")
    store.create_session("current", kind="chat")
    store.complete_turn("previous", text, "We can revisit it.")
    state = store.continuity_state()
    inputs = store.continuity_inputs(after=state["watermark"])
    source = inputs[-1]
    operation = {
        "op": "upsert", "expected_revision": 0, "category": "thread",
        "topic": "Garden illustration", "importance": "moderate",
        "content": "The user reported: " + text, "origin": "derived", "scope": "real_life",
        "content_format": "narrative",
        "sources": [{"exchange_id": source["id"], "document_id": source["document_id"],
                     "role": "user", "origin": "user_statement", "excerpt": text}],
        **fields,
    }
    task = store.create_maintenance_task(inputs, expected_revision=state["revision"],
                                         control_revision=state["control_revision"])
    validated = validate_operations({"operations": [operation]}, inputs=inputs, state=state)
    store.commit_continuity(task["id"], validated, expected_revision=state["revision"],
                            control_revision=state["control_revision"])
    return store.continuity_state()["items"][-1]


@pytest.mark.parametrize("use,shadow,bypass", [(False, False, False), (True, True, False),
                                               (True, False, True), (True, False, False)])
async def test_candidate_requires_both_use_controls_and_bypass_omits_it(tmp_path, use, shadow, bypass):
    store = Store(tmp_path / "state.sqlite3")
    try:
        seed(store)
        cfg = configuration(tmp_path, enabled=use)
        cfg.memory.continuity_use_enabled = not shadow
        provider = Provider()
        events = [event async for event in ConversationEngine(cfg, store, Memory(), provider=provider)
                  .generate("current", "A new room", bypass_recall=bypass)]
        assert events[-1]["type"] == "completed", events
        supplied = use and not shadow and not bypass
        assert ("garden illustration" in json.dumps(provider.requests)) is supplied
        current = next(message["content"] for message in provider.requests[0]
                       if message["role"] == "user")
        assert ("# Continuity context\n" in current) is supplied
        assert ("# Memory context\n" in current) is (use and not bypass)
        manifest = store.list_memory_manifests()[0]["manifest"]
        assert ("continuity" in manifest) is supplied
        assert manifest["budget"]["used_chars"] == len(manifest["text"]) <= 8000
    finally:
        store.close()


async def test_frozen_snapshot_survives_overlapping_edit_and_manifest_matches_request(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        item = seed(store)
        initial = store.continuity_state()
        memory = Memory(callback=lambda: store.edit_continuity_item(
            item["id"], {"content": "User revised the topic.", "manual_lock": True},
            expected_revision=item["revision"],
        ))
        provider = Provider()
        engine = ConversationEngine(configuration(tmp_path), store, memory, provider=provider)
        events = [event async for event in engine.generate("current", "New question")]
        assert events[-1]["type"] == "completed", events
        manifest = store.list_memory_manifests()[0]["manifest"]
        current = next(message["content"] for message in provider.requests[0]
                       if message["role"] == "user")
        supplied = "\n\n".join(start + current.split(start, 1)[1].split(end, 1)[0] + end
                               for start, end in (("<continuity_context>", "</continuity_context>"),
                                                  ("<memory_context>", "</memory_context>")))
        assert manifest["text"] == supplied
        assert "garden illustration" in manifest["text"]
        assert "User revised" not in manifest["text"]
        assert manifest["continuity"]["revision"] == initial["revision"]
        assert memory.budgets == [8000 - len(manifest["continuity"]["text"]) - 2]
        # The next generation receives the new user version.
        preview = await engine.preview_prompt("current")
        assert "User revised the topic." in json.dumps(preview)
        assert not any("# Memory context\n" in message["content"]
                       for message in preview["messages"])
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["plain", "prefix", "json"])
async def test_fresh_context_is_framed_in_current_user_content_in_preview_and_generation(
    tmp_path, mode,
):
    store = Store(tmp_path / "state.sqlite3")
    try:
        seed(store)
        cfg = configuration(tmp_path)
        cfg.providers[0].presentation = mode
        reply = "A completed reply."
        if mode == "json":
            reply = json.dumps({"expression": None, "gesture": None, "reply": reply})
        elif mode == "prefix":
            reply = '<eidolon-cues>{"expression":null,"gesture":null}</eidolon-cues>\n' + reply
        provider = Provider(reply=reply)
        engine = ConversationEngine(cfg, store, Memory(), provider=provider)
        recalled = "<memory_context>\nHistorical source.\n</memory_context>"
        preview = await engine.preview_prompt("current", "New question", recalled=recalled)
        events = [event async for event in engine.generate("current", "New question")]
        assert events[-1]["type"] == "completed", events
        profile = Path(cfg.prompts["USER"]).read_text()
        for messages in (preview["messages"], provider.requests[0]):
            headings = [message["content"].split("\n", 1)[0] for message in messages]
            expected = {
                "# User profile (USER.md)": (profile, "Use this profile as background"),
                "# Application context": ("<app_context>", "understand the current runtime"),
            }
            if mode != "plain":
                expected["# Optional cosmetic presentation"] = (
                    "active catalog:", "format your reply and optionally add avatar cues",
                )
            else:
                assert "# Optional cosmetic presentation" not in headings
            for heading, (payload, instruction) in expected.items():
                assert headings.count(heading) == 1
                section = messages[headings.index(heading)]
                assert section["role"] == "system"
                assert payload in section["content"] and instruction in section["content"]
            current = next(message["content"] for message in messages
                           if message["role"] == "user"
                           and message["content"].partition("\n")[2].startswith("<app_context>\n"))
            stamp = current.split("\n", 1)[0]
            assert stamp.startswith("[") and stamp.endswith("]")
            assert datetime.fromisoformat(stamp[1:-1]).tzinfo is not None
            for heading, payload in (("# Runtime context", "Current local time:"),
                                     ("# Continuity context", "<continuity_context>"),
                                     ("# Memory context", "<memory_context>")):
                assert current.count(heading) == 1 and payload in current
                assert all(payload not in message["content"] for message in messages
                           if message["role"] == "system")
            assert current.endswith("</app_context>\n\n# Current user request\n\nNew question")
        assert recalled in next(message["content"] for message in preview["messages"]
                                if message["role"] == "user"
                                and message["content"].partition("\n")[2]
                                .startswith("<app_context>\n"))
    finally:
        store.close()


async def test_original_received_time_survives_generation_and_source_reconstruction(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_session("current", kind="chat")
    received = datetime.fromisoformat("2026-10-14T23:59:58+08:00")
    try:
        engine = ConversationEngine(configuration(tmp_path), store, Memory(), provider=Provider())
        events = [event async for event in engine.generate(
            "current", "My interview is tomorrow.", user_message_at=received)]
        assert events[-1]["type"] == "completed", events
        source = store.continuity_inputs()[0]
        assert source["created_at"] == source["user_created_at"] == received.isoformat()
        from eidolon.memory.continuity import resolve_event_time
        assert resolve_event_time("tomorrow", source["created_at"]).startswith("2026-10-15T")
    finally:
        store.close()


@pytest.mark.parametrize("save,fail", [(True, False), (False, False), (True, True)])
async def test_only_completed_eligible_actual_questions_record_followups(tmp_path, save, fail):
    store = Store(tmp_path / "state.sqlite3")
    try:
        item = seed(store)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        item = store.edit_continuity_item(item["id"], {"follow_up_at": past.isoformat()},
                                           expected_revision=item["revision"])
        provider = Provider("How is the garden illustration going?", fail=fail)
        engine = ConversationEngine(configuration(tmp_path, save_enabled=save), store, Memory(),
                                    provider=provider)
        events = [event async for event in engine.generate("current", "Hello")]
        assert events[-1]["type"] == ("error" if fail else "completed"), events
        current = store.continuity_state()["items"][0]
        assert bool(current.get("last_followup_at")) is (save and not fail)
        if save and not fail:
            packed = engine.memory_coordinator.prepare_continuity(store, "current", enabled=True)
            assert packed.manifest["entries"][0]["followup_due"] is False
            assert "do not repeat yet" in packed.text
    finally:
        store.close()


async def test_actual_questions_on_undated_threads_update_last_offer_each_time(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        seed(store)
        provider = Provider("How is the garden illustration going?")
        engine = ConversationEngine(configuration(tmp_path), store, Memory(), provider=provider)
        for _ in range(2):
            events = [event async for event in engine.generate("current", "Hello")]
            assert events[-1]["type"] == "completed", events
            item = store.continuity_state()["items"][0]
            assert item["last_followup_evidence"]["generation_id"] == events[-1]["turn_id"]
            assert "follow_up_at" not in item
        assert not engine.memory_coordinator.prepare_continuity(
            store, "current", enabled=True).manifest["entries"][0]["followup_due"]
    finally:
        store.close()


def test_unprocessed_exchanges_are_not_duplicated_in_continuity_context(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        seed(store)
        store.complete_turn("previous", "Eligible new detail.", "Assistant suggestion.")
        store.complete_turn("previous", "UNSAVED_MARKER", "Unsaved reply.", retain=False)
        store.create_session("job", kind="job")
        store.complete_turn("job", "JOB_MARKER", "Generated job.")
        coordinator = MemoryCoordinator(None)
        context = coordinator.prepare_continuity(store, "current", enabled=True)
        assert "Eligible new detail." not in context.text
        assert "Assistant suggestion." not in context.text
        assert "UNSAVED_MARKER" not in context.text and "JOB_MARKER" not in context.text
        assert context.manifest["recent_exchanges"] == []
        store.complete_turn("current", "This room now has exact history.", "Reply.")
        assert not coordinator.prepare_continuity(store, "current", enabled=True).manifest["recent_exchanges"]
        assert not coordinator.prepare_continuity(store, "job", enabled=True).text
    finally:
        store.close()


def test_pending_cross_midnight_exchange_is_not_supplied_without_model_summary(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.create_session("previous", kind="chat")
        store.create_session("current", kind="chat")
        monkeypatch.setattr("eidolon.storage.store.utc_now",
                            lambda: "2026-10-15T00:01:00+08:00")
        store.complete_turn("previous", "My event is tomorrow.", "I will ask tomorrow.",
                            metadata={"user_message_at": "2026-10-14T23:59:00+08:00"})
        context = MemoryCoordinator(None).prepare_continuity(store, "current", enabled=True)
        assert not context.text
        inputs = store.continuity_inputs()
        assert inputs[0]["user_created_at"] == "2026-10-14T23:59:00+08:00"
        assert inputs[0]["assistant_created_at"] == "2026-10-15T00:01:00+08:00"
    finally:
        store.close()


def test_continuity_survives_restart_and_expiry_is_checked_without_refresh(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    now = datetime.now(timezone.utc)
    seed(store, "I feel tense today.", category="emotion",
         expires_at=(now + timedelta(hours=1)).isoformat())
    store.close()
    store = Store(path)
    try:
        coordinator = MemoryCoordinator(None)
        assert "I feel tense today." in coordinator.prepare_continuity(
            store, "current", enabled=True, now=now, local_timezone="Asia/Manila").text
        assert "I feel tense today." not in coordinator.prepare_continuity(
            store, "current", enabled=True, now=now + timedelta(hours=2)).text
        assert store.continuity_state()["items"][0]["sources"][0]["excerpt"] == "I feel tense today."
    finally:
        store.close()


@pytest.mark.parametrize("state", ["resolved", "archived"])
def test_persisted_terminal_states_leave_active_prompt(tmp_path, state):
    store = Store(tmp_path / "state.sqlite3")
    try:
        item = seed(store)
        store.edit_continuity_item(item["id"], {"state": state},
                                    expected_revision=item["revision"])
        context = MemoryCoordinator(None).prepare_continuity(store, "current", enabled=True)
        assert not context.text
        assert store.continuity_state()["items"][0]["state"] == state
    finally:
        store.close()


def test_continuity_context_is_ready_when_historical_recall_is_empty(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        seed(store)
        local = MemoryCoordinator(None).prepare_continuity(store, "current", enabled=True)
        combined = combine_context(local, assemble_context(RecallResult()))
        assert combined.manifest["status"] == "ready"
        assert combined.manifest["historical_status"] == "empty"
        assert combined.text == local.text
    finally:
        store.close()


class SnapshotStore:
    """Fixed synthetic evidence; production storage validation is tested separately."""
    def __init__(self, items):
        self.items = items

    def get_session(self, session_id):
        return {"kind": "chat"}

    def list_messages(self, session_id):
        return [{"role": "user", "content": "Current room history"}]

    def continuity_state(self, scope):
        return {"revision": 1, "control_revision": 0, "watermark": 2, "items": self.items}


@pytest.mark.parametrize("probe", range(60))
def test_sixty_offline_source_preservation_and_scope_probes(probe):
    now = datetime(2026, 10, 15, 4, 1, tzinfo=timezone(timedelta(hours=8)))
    scenario = probe % 10
    text = ["I prefer brief replies.", "My interview is tomorrow.",
            "The garden illustration remains unfinished.", "I feel tense today.",
            "In our story we are astronauts.", "The illustration is completed.",
            "I promise to revisit the project tomorrow.", "The old event is expired.",
            "忘れないで、明日は面接です。", "I prefer tea; my sibling prefers coffee."][scenario]
    prose = ["The user prefers brief replies.", "The user has an interview on October 16.",
             "The user's garden illustration remains unfinished.",
             "The user felt tense on October 15; this is temporary context.",
             "The characters are astronauts in a fictional story.",
             "The user's illustration was completed.",
             "The user promised to revisit the project on October 16.",
             "The old event has expired.", "ユーザーは10月16日に面接があると述べた。",
             "The user prefers tea, while their sibling prefers coffee."][scenario]
    item = {"id": str(probe), "category": "emotion" if scenario == 3 else "thread",
            "content": prose, "scope": "fiction" if scenario == 4 else "real_life",
            "origin": "derived", "content_format": "narrative",
            "status": "resolved" if scenario == 5 else "open",
            "observed_at": "2026-10-14T19:00:00+00:00", "revision": 1,
            "sources": [{"excerpt": text, "role": "user"}],
            "expires_at": (now + timedelta(hours=-1 if scenario == 7 else 1)).isoformat()}
    if scenario == 1:
        item["event_at"] = "2026-10-16T00:00:00+08:00"
    if scenario == 6:
        item.update(promise_owner="user", promise_kind="conversation")
    local = MemoryCoordinator(None).prepare_continuity(SnapshotStore([item]), "current",
                                                      enabled=True, now=now, local_timezone="Asia/Manila")
    historical = assemble_context(RecallResult(records=tuple(
        MemoryRecord(id=f"historical-{n}", text=f"Source {n}: " + "x" * 740)
        for n in range(12))), max_chars=8000 - len(local.text) - (2 if local.text else 0))
    combined = combine_context(local, historical)
    assert len(combined.text) <= 8000
    assert (prose in combined.text) is (scenario not in {4, 5, 7})
    assert text not in combined.text
    assert all(entry["text"] in combined.text for entry in combined.manifest["entries"])
    if local.text:
        assert "observed 2026-10-15" in local.text  # 3 AM actual calendar date
        assert local.manifest["entries"][0]["sources"][0]["excerpt"] == text
    if scenario == 1:
        assert "event 2026-10-16" in local.text
