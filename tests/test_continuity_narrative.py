"""Narrative context, evidence boundaries, upgrades and configured inference routes."""
import asyncio
import json

import httpx
import pytest

from eidolon.config import AppConfig, AuxiliaryModelConfig, IdentityConfig, ProviderConfig
from eidolon.memory.continuity import ContinuityValidationError, validate_operations
from eidolon.memory.coordinator import MemoryCoordinator
from eidolon.memory.maintenance import ContinuityWorker
from eidolon.providers.openai import OpenAIProvider
from eidolon.providers.routing import configured_route_fingerprint, resolve_auxiliary
from eidolon.storage import Store

from test_memory_maintenance import Provider


@pytest.fixture
def store(tmp_path):
    storage = Store(tmp_path / "room.sqlite3")
    try:
        yield storage
    finally:
        storage.close()


def add(store, user="My interview is tomorrow.", assistant="We could practice together.", **kwargs):
    session = store.session_for()["id"]
    store.complete_turn(session, user, assistant, **kwargs)
    return store.recent_continuity_inputs(limit=1)[0]


def cite(row, role="user"):
    return {"exchange_id": row["exchange_id"], "document_id": row["document_id"],
            "role": role, "origin": role + "_statement"}


def narrative(row, text="The user's interview is approaching; practice is still undecided.",
              **changes):
    if changes.get("op") in {"archive", "resolve"}:
        changes = {"archive_reason": "completed", "decision_reason": "The interview concluded.",
                   **changes}
    return {"op": "upsert", "expected_revision": 0, "category": "thread",
            "topic": "Interview preparation", "importance": "moderate",
            "content": text, "content_format": "narrative", "origin": "derived",
            "scope": "real_life", "sources": [cite(row)], **changes}


def proposal(operation):
    return {"operations": [operation]}


def legacy(store):
    row = add(store)
    state = store.continuity_state()
    task = store.create_maintenance_task([row], expected_revision=0, control_revision=0)
    op = narrative(row, row["user"], event_at="tomorrow")
    op.pop("content_format")
    op["origin"] = "user_statement"
    op["sources"][0]["excerpt"] = row["user"]
    ops = validate_operations(proposal(op), inputs=[row], state=state)
    store.commit_continuity(task["id"], ops, expected_revision=0, control_revision=0)
    return store.continuity_state()["items"][0]


async def test_cohesive_summary_combines_speakers_without_echoing_long_originals(store):
    first = add(store, "My interview is tomorrow. " + "A long preparation detail. " * 190)
    second = add(store, "I feel overwhelmed by interview practice. Encouragement would help more.",
                 "We can pause practice and focus on encouragement.")
    prose = ("The user has an upcoming interview and initially discussed preparation with Eidolon. "
             "They later said practice felt overwhelming and asked for encouragement instead. "
             "Eidolon offered to pause practice; resuming it remains undecided.")
    provider = Provider(result=proposal(narrative(
        first, prose, sources=[cite(first), cite(second), cite(second, "assistant")])))
    assert await ContinuityWorker(store, provider).run_once(force=True)
    item = store.continuity_state()["items"][0]
    assert item["content"] == prose
    assert item["origin"] == "derived"
    assert len(item["sources"]) == 3
    assert item["sources"][0]["excerpt"] == first["user"]
    assert len(item["sources"][0]["excerpt"]) > 4000
    assert item["sources"][2]["excerpt"] == second["assistant"]
    session = store.create_session(kind="chat")["id"]
    context = MemoryCoordinator(None).prepare_continuity(store, session, enabled=True)
    assert prose in context.text
    assert first["user"] not in context.text and second["user"] not in context.text
    assert "We can pause practice" not in context.text
    assert context.manifest["recent_exchanges"] == []
    assert context.manifest["entries"][0]["sources"] == item["sources"]


async def test_updates_rebuild_from_original_support_and_keep_one_topic(store):
    first = add(store)
    initial = narrative(first)
    assert await ContinuityWorker(store, Provider(result=proposal(initial))).run_once(force=True)
    item = store.continuity_state()["items"][0]
    second = add(store, "My interview preparation can wait; please offer encouragement.")
    prose = ("The user's interview is approaching, but they now prefer encouragement. "
             "Interview preparation can wait until they want to resume.")

    def update(document):
        supplied = document["items"][0]
        assert supplied["content"] == initial["content"]
        assert supplied["sources"][0]["excerpt"] == first["user"]
        assert supplied["sources"][0]["timestamp"] == first["user_created_at"]
        return proposal(narrative(second, prose, item_id=item["id"], expected_revision=1,
                                  sources=[cite(first), cite(second)]))

    assert await ContinuityWorker(store, Provider(transform=update)).run_once(force=True)
    state = store.continuity_state()
    assert len(state["items"]) == 1
    assert state["items"][0]["content"] == prose
    assert len(state["items"][0]["sources"]) == 2
    assert len(store.continuity_history(item["id"])) == 2
    assert state["watermark"] == second["id"]


async def test_maintenance_separates_current_names_from_recorded_and_legacy_sources(store):
    recorded = {"user_name": "Aster", "companion_name": "Vale"}
    first = add(store, metadata={"identity": recorded})
    second = add(store, "My interview practice is undecided.")
    prose = "Aster's interview is approaching; practice with Vale remains undecided."
    initial = narrative(first, prose, sources=[cite(first), cite(first, "assistant"), cite(second)])
    assert await ContinuityWorker(store, Provider(result=proposal(initial))).run_once(force=True)
    item = store.continuity_state()["items"][0]
    current = IdentityConfig(user_name="River", companion_name="Sol")
    third = add(store, "My interview preparation is still uncertain.",
                metadata={"identity": current.model_dump()})
    evidence_before = store.continuity_evidence([first["exchange_id"], second["exchange_id"],
                                                third["exchange_id"]])
    saved_before = {table: [tuple(row) for row in store.db.execute("SELECT * FROM " + table)]
                    for table in ("generations", "messages", "outbox")}
    provider = Provider()
    worker = ContinuityWorker(store, provider, identity=current)
    assert await worker.run_once(force=True)
    document = json.loads(provider.calls[0][-1]["content"])
    assert document["current_identity"] == current.model_dump()
    assert document["exchanges"][0]["identity"] == current.model_dump()
    sources = document["items"][0]["sources"]
    for source in sources:
        if source["exchange_id"] == first["exchange_id"]:
            assert source["identity"] == recorded
            assert source["excerpt"] == first[source["role"] + "_content"]
            assert source["timestamp"] == first[source["role"] + "_created_at"]
        else:
            assert source["exchange_id"] == second["exchange_id"]
            assert "identity" not in source
    assert "recorded attribution" in provider.calls[0][0]["content"]
    assert store.continuity_state()["items"] == [item]
    assert store.continuity_evidence([first["exchange_id"], second["exchange_id"],
                                      third["exchange_id"]]) == evidence_before
    assert {table: [tuple(row) for row in store.db.execute("SELECT * FROM " + table)]
            for table in saved_before} == saved_before


async def test_prior_source_changes_during_inference_reject_the_whole_update(store):
    first = add(store, generation_id="earlier")
    assert await ContinuityWorker(store, Provider(result=proposal(narrative(first)))).run_once(force=True)
    item = store.continuity_state()["items"][0]
    second = add(store, "My interview preparation is still unfinished.")

    def update(document):
        with store.db:
            store.db.execute("UPDATE generations SET memory_eligible=0 WHERE id='earlier'")
        return proposal(narrative(second, item_id=item["id"], expected_revision=1,
                                  sources=[cite(first), cite(second)]))

    assert not await ContinuityWorker(store, Provider(transform=update)).run_once(force=True)
    state = store.continuity_state()
    assert state["watermark"] == first["id"]
    assert state["items"][0]["invalidated"]
    assert state["items"][0]["revision"] == 1


async def test_existing_extractive_notes_are_rewritten_without_new_chats(store):
    old = legacy(store)
    before = store.continuity_state()
    assert old.get("content_format") is None
    context = MemoryCoordinator(None).prepare_continuity(store, store.session_for()["id"],
                                                        enabled=True)
    assert old["content"] not in context.text
    assert context.manifest["omitted"][0]["reason"] == "awaiting model-written narrative summary"

    def rewrite(document):
        assert document["rewrite_existing"]
        assert document["legacy_rewrite_ids"] == [old["id"]]
        row = document["exchanges"][0]
        return proposal(narrative(row, item_id=old["id"], expected_revision=old["revision"]))

    provider = Provider(transform=rewrite)
    worker = ContinuityWorker(store, provider)
    assert await worker.run_once(force=True)
    after = store.continuity_state()
    assert after["watermark"] == before["watermark"]
    assert len(after["items"]) == 1
    item = after["items"][0]
    assert item["id"] == old["id"] and item["revision"] == old["revision"] + 1
    assert item["event_at"] == old["event_at"]
    assert item["sources"] == old["sources"]
    assert item["content"] != old["content"]
    assert not await worker.run_once(force=True)
    assert len(provider.calls) == 1


async def test_pending_new_chats_can_convert_legacy_notes_using_their_original_sources(
    store, tmp_path,
):
    from eidolon.engine import ConversationEngine
    from test_memory_phase2_engine import Memory, configuration
    from test_provider import sse

    old = legacy(store)
    before = store.continuity_state()
    fresh = add(store, "I prefer coffee in the morning.")
    prose = "The user's interview is approaching; practice is still undecided."

    def rewrite(document):
        assert not document["rewrite_existing"]
        assert document["legacy_rewrite_ids"] == [old["id"]]
        assert [row["exchange_id"] for row in document["exchanges"]] == [fresh["exchange_id"]]
        supplied = document["items"][0]
        assert supplied["sources"][0]["excerpt"] == old["sources"][0]["excerpt"]
        return proposal(narrative(
            fresh, prose, item_id=old["id"], expected_revision=old["revision"],
            sources=[{key: old["sources"][0][key] for key in
                      ("exchange_id", "document_id", "role", "origin")}],
        ))

    summarizer = Provider(transform=rewrite)
    worker = ContinuityWorker(store, summarizer)
    assert await worker.run_once(force=True)
    after = store.continuity_state()
    assert after["watermark"] == fresh["id"]
    assert after["revision"] == before["revision"] + 1
    assert len(after["items"]) == 1
    item = after["items"][0]
    assert item["id"] == old["id"] and item["content"] == prose
    assert item["sources"] == old["sources"]
    assert item["event_at"] == old["event_at"]
    assert item["observed_at"] == old["observed_at"]
    assert len(store.continuity_history(old["id"])) == 2
    assert len(summarizer.calls) == 1
    assert not await worker.run_once(force=True)

    current = store.create_session(kind="chat")["id"]
    cfg = configuration(tmp_path)
    requests = []

    def capture(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse({"content": "A completed reply."}, "stop"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        dialogue = OpenAIProvider(cfg.providers[0], client=client)
        engine = ConversationEngine(cfg, store, Memory(), provider=dialogue)
        try:
            events = [event async for event in engine.generate(current, "Let's talk.")]
            assert events[-1]["type"] == "completed", events
            section = next(message["content"] for message in requests[0]["messages"]
                           if message["role"] == "user"
                           and "# Continuity context\n" in message["content"])
            assert prose in section and "<continuity_context>" in section
            assert old["content"] not in section
            manifest = store.list_memory_manifests()[0]["manifest"]["continuity"]
            assert manifest["status"] == "ready" and manifest["text"] in section
            assert json.loads(store.latest_model_request()["body"]) == requests[0]
        finally:
            await engine.close()


@pytest.mark.parametrize("changes", [
    {"category": "relationship"},
    {"event_at": None},
    {"follow_up_at": "today"},
    {"promise_owner": "user", "promise_kind": "conversation"},
    {"op": "resolve"},
    {"op": "archive"},
])
def test_legacy_conversion_without_batch_evidence_cannot_change_metadata_or_state(store, changes):
    old = legacy(store)
    before = store.continuity_state()
    fresh = add(store, "I prefer coffee in the morning.")
    original = store.continuity_evidence([old["sources"][0]["exchange_id"]])[0]
    task = store.create_maintenance_task([fresh], expected_revision=before["revision"],
                                         control_revision=before["control_revision"])
    operation = narrative(
        original, item_id=old["id"], expected_revision=old["revision"],
        sources=[{key: old["sources"][0][key] for key in
                  ("exchange_id", "document_id", "role", "origin", "excerpt")}], **changes,
    )
    with pytest.raises(ContinuityValidationError, match="this batch's evidence"):
        validate_operations(proposal(operation), inputs=[fresh], state=before,
                            supporting_inputs=[original], require_narrative=True)
    # Direct persistence callers must pass the same barrier as the worker.
    with pytest.raises(ValueError, match="evidence from this task"):
        store.commit_continuity(task["id"], [operation], expected_revision=before["revision"],
                                control_revision=before["control_revision"])
    assert store.continuity_state() == before
    assert store.pending_maintenance_task()["id"] == task["id"]


async def test_existing_narrative_update_still_requires_evidence_from_the_current_batch(store):
    original = add(store)
    assert await ContinuityWorker(store, Provider(result=proposal(narrative(original)))).run_once(
        force=True,
    )
    before = store.continuity_state()
    item = before["items"][0]
    add(store, "I prefer coffee in the morning.")
    operation = narrative(original, item_id=item["id"], expected_revision=item["revision"])
    worker = ContinuityWorker(store, Provider(result=proposal(operation)))
    assert not await worker.run_once(force=True)
    assert store.continuity_state() == before
    assert "must cite an exchange from this batch" in worker.status["last_result"]


def test_legacy_conversion_without_batch_evidence_cannot_drop_prior_support(store):
    first = add(store)
    second = add(store, "My interview preparation is still unfinished.")
    before = store.continuity_state()
    task = store.create_maintenance_task([first, second], expected_revision=0, control_revision=0)
    old_operation = narrative(
        first, first["user"] + "\n" + second["user"], origin="user_statement",
        sources=[{**cite(row), "excerpt": row["user"]} for row in (first, second)],
    )
    old_operation.pop("content_format")
    normalized = validate_operations(proposal(old_operation), inputs=[first, second], state=before)
    store.commit_continuity(task["id"], normalized, expected_revision=0, control_revision=0)
    before = store.continuity_state()
    item = before["items"][0]
    fresh = add(store, "I prefer coffee in the morning.")
    task = store.create_maintenance_task([fresh], expected_revision=before["revision"],
                                         control_revision=before["control_revision"])
    operation = narrative(first, item_id=item["id"], expected_revision=item["revision"],
                          sources=[{**cite(first), "excerpt": first["user"]}])
    with pytest.raises(ContinuityValidationError, match="this batch's evidence"):
        validate_operations(proposal(operation), inputs=[fresh], state=before,
                            supporting_inputs=[first, second], require_narrative=True)
    with pytest.raises(ValueError, match="evidence from this task"):
        store.commit_continuity(task["id"], [operation], expected_revision=before["revision"],
                                control_revision=before["control_revision"])
    assert store.continuity_state() == before


async def test_completed_no_change_rewrite_is_not_redispatched_after_restart(store):
    old = legacy(store)
    before = store.continuity_state()
    provider = Provider()
    worker = ContinuityWorker(store, provider, enabled=True)
    assert await worker.run_once()
    task_count = store.db.execute("SELECT count(*) FROM maintenance_tasks").fetchone()[0]
    for _ in range(4):
        assert not await worker.run_once()
    assert len(provider.calls) == 1
    assert not worker.status["running"]
    assert store.continuity_state() == before
    assert store.continuity_state()["items"] == [old]
    assert store.db.execute("SELECT count(*) FROM maintenance_tasks").fetchone()[0] == task_count

    restarted_store = Store(store.path)
    try:
        replacement = ContinuityWorker(restarted_store, Provider(), enabled=True)
        assert not await replacement.run_once()
        assert replacement.provider.calls == []
        assert restarted_store.pending_maintenance_task() is None
    finally:
        restarted_store.close()


async def test_manual_refresh_retries_a_completed_no_change_rewrite_once(store):
    old = legacy(store)
    provider = Provider()
    worker = ContinuityWorker(store, provider, enabled=True)
    assert await worker.run_once()
    assert not await worker.run_once()

    def rewrite(document):
        return proposal(narrative(document["exchanges"][0], item_id=old["id"],
                                  expected_revision=old["revision"]))

    provider.transform = rewrite
    assert await worker.run_once(force=True)
    assert len(provider.calls) == 2
    assert store.continuity_state()["items"][0]["content_format"] == "narrative"
    assert not await worker.run_once()
    assert len(provider.calls) == 2


async def test_interrupted_legacy_rewrite_resumes_after_revision_change_without_skipping_new_input(store):
    old = legacy(store)
    provider = Provider(failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ContinuityWorker(store, provider).run_once(force=True)
    pending = store.pending_maintenance_task()
    assert pending["id"].startswith("narrative:")
    newer = add(store, "My interview preparation is unfinished.")
    edited = store.edit_continuity_item(old["id"], {"locked": False}, expected_revision=1)

    def rewrite(document):
        return proposal(narrative(document["exchanges"][0], item_id=old["id"],
                                  expected_revision=edited["revision"]))

    worker = ContinuityWorker(store, Provider(transform=rewrite))
    assert await worker.run_once(force=True)
    assert store.continuity_state()["watermark"] == pending["input_watermark"]
    assert store.continuity_inputs(after=pending["input_watermark"]) == [newer]
    assert store._maintenance_task(pending["id"])["stages"]["continuity"]["status"] == "completed"


async def test_pending_rewrite_can_complete_without_change_after_user_locks_the_note(store):
    old = legacy(store)
    with pytest.raises(asyncio.CancelledError):
        await ContinuityWorker(store, Provider(failure=asyncio.CancelledError())).run_once(force=True)
    locked = store.edit_continuity_item(old["id"], {"locked": True}, expected_revision=1)
    assert await ContinuityWorker(store, Provider()).run_once(force=True)
    assert store.pending_maintenance_task() is None
    assert store.continuity_state()["items"] == [locked]


async def test_oversized_legacy_conversion_is_held_with_its_complete_sources(store):
    old = legacy(store)
    provider = Provider()
    worker = ContinuityWorker(store, provider, max_input_chars=100)
    assert not await worker.run_once(force=True)
    pending = store.pending_maintenance_task()
    assert pending["id"].startswith("narrative:")
    assert pending["inputs"][0]["user"] == old["content"]
    assert pending["stages"]["continuity"]["status"] == "failed"
    assert not provider.calls


@pytest.mark.parametrize("metadata,source", [
    ({"promise_owner": "user", "promise_kind": "conversation"}, "My interview is tomorrow."),
    ({"event_at": "tomorrow"}, "My interview is not tomorrow."),
    ({"op": "resolve"}, "My interview will be finished tomorrow."),
])
def test_narrative_cannot_launder_promises_dates_or_completion(metadata, source):
    row = {"exchange_id": 4, "document_id": "new", "created_at": "2026-10-14T03:00:00+08:00",
           "user_content": source, "assistant_content": "Good luck."}
    item = {"id": "interview", "revision": 1, "content": "The user's interview is pending."}
    op = narrative(row, "The user promised to finish the interview; it is now finished.",
                   item_id="interview", expected_revision=1, **metadata)
    with pytest.raises(ContinuityValidationError):
        validate_operations(proposal(op), inputs=[row], state={"revision": 1, "items": [item]},
                            require_narrative=True)


def test_old_citation_cannot_authorize_unrelated_new_evidence_or_another_speaker():
    first = {"exchange_id": 2, "document_id": "old", "created_at": "2026-10-14T03:00:00+08:00",
             "user_content": "My interview is tomorrow.", "assistant_content": "An unused suggestion."}
    second = {**first, "exchange_id": 4, "document_id": "new", "user_content": "I prefer coffee."}
    item = {"id": "interview", "revision": 1, "content": "The user's interview is pending.",
            "sources": [cite(first)]}
    for sources in ([cite(first), cite(second)], [cite(first, "assistant"), cite(second)]):
        with pytest.raises(ContinuityValidationError):
            validate_operations(proposal(narrative(second, item_id="interview", expected_revision=1,
                                                   sources=sources)), inputs=[second],
                                supporting_inputs=[first], state={"revision": 1, "items": [item]})


async def test_worker_rejects_copied_exchange_instead_of_publishing_it(store):
    row = add(store)
    for op in (narrative(row, row["user"]),
               {**narrative(row), "content_format": None, "origin": "user_statement"}):
        provider = Provider(result=proposal(op))
        assert not await ContinuityWorker(store, provider).run_once(force=True)
        assert store.continuity_state()["watermark"] == 0
        assert store.continuity_state()["items"] == []


@pytest.mark.parametrize("mode,expected_model", [("inherit", "dialogue"), ("separate", "summarizer")])
async def test_narrative_is_generated_by_the_configured_model(store, mode, expected_model):
    row = add(store)
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[ProviderConfig(model="dialogue")],
                       identity=IdentityConfig(user_name="Aster", companion_name="Vale"),
                       auxiliary=AuxiliaryModelConfig(mode=mode, provider="local", model="summarizer"))
    route = store.reconcile_auxiliary_route(configured_route_fingerprint(config))
    execution = resolve_auxiliary(config, route["revision"])
    requests = []
    prose = "The user is preparing for an upcoming interview; practice remains undecided."

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        content = json.dumps(proposal(narrative(row, prose)))
        event = {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(execution.profile(), client=client, stream_usage=execution.stream_usage)
        worker = ContinuityWorker(store, provider, execution=execution, identity=config.identity)
        assert await worker.run_once(force=True)
    assert len(requests) == 1 and requests[0]["model"] == expected_model
    instructions = next(message["content"] for message in requests[0]["messages"]
                        if message["role"] == "system")
    assert "user_content contains the user role's messages" in instructions
    assert "assistant_content contains the assistant role's messages" in instructions
    assert "user/user_statement and assistant/assistant_statement" in instructions
    document = json.loads(requests[0]["messages"][-1]["content"])
    assert document["current_identity"] == config.identity.model_dump()
    assert document["exchanges"][0]["user_content"] == row["user"]
    assert document["exchanges"][0]["assistant_content"] == row["assistant"]
    assert store.continuity_state()["items"][0]["content"] == prose
    assert store.maintenance_status()["last_successful_execution"]["provenance"]["model"] == expected_model


async def test_continuity_prompt_defines_labels_and_separates_summary_from_evidence(store):
    row = add(store)
    prose = "The user's interview is upcoming; they have not decided whether to practice."
    assert await ContinuityWorker(store, Provider(result=proposal(narrative(row, prose)))).run_once(force=True)
    context = MemoryCoordinator(None).prepare_continuity(store, store.session_for()["id"], enabled=True)
    for definition in ("situation =", "thread =", "relationship =", "emotion =", "observed =",
                       "event =", "conversation =", "scheduled =", "current user corrections"):
        assert definition in context.text
    assert context.text.count(prose) == 1
    assert row["user"] not in context.text and row["assistant"] not in context.text
    assert len(context.text) <= 3000


async def test_chat_receives_narrative_and_label_guide_without_raw_cross_room_exchange(store, tmp_path):
    from eidolon.engine import ConversationEngine
    from test_memory_phase2_engine import Memory, Provider as DialogueProvider, configuration

    row = add(store)
    prose = "The user's interview is approaching, with practice still undecided."
    summarizer = Provider(result=proposal(narrative(row, prose)))
    assert await ContinuityWorker(store, summarizer).run_once(force=True)
    current = store.create_session(kind="chat")["id"]
    dialogue = DialogueProvider()
    engine = ConversationEngine(configuration(tmp_path), store, Memory(), provider=dialogue)
    try:
        events = [event async for event in engine.generate(current, "Let's talk.")]
        assert events[-1]["type"] == "completed"
        section = next(message["content"] for message in dialogue.requests[0]
                       if message["role"] == "user"
                       and "# Continuity context\n" in message["content"])
        assert prose in section and "thread = unfinished topic/task/decision" in section
        assert row["user"] not in json.dumps(dialogue.requests)
        assert row["assistant"] not in json.dumps(dialogue.requests)
        assert len(summarizer.calls) == 1
        manifest = store.list_memory_manifests()[0]["manifest"]["continuity"]
        assert manifest["entries"][0]["sources"][0]["excerpt"] == row["user"]
        assert manifest["text"] in section
    finally:
        await engine.close()
