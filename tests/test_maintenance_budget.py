"""Maintenance plans complete work before dispatch through the service boundary."""
import json

import pytest
import httpx

from test_auxiliary_service import select
from test_ordinary_proposals import CompactProvider, refresh
from test_service import make_config, room
from test_current_life_policy import seed_note
from eidolon.config import MaintenanceOutputSupport
from eidolon.providers.openai import OpenAIProvider


@pytest.mark.parametrize("mode", ["inherit", "separate"])
async def test_impossible_instruction_reserve_is_reported_without_dispatch(
    tmp_path, monkeypatch, mode,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, mode=mode, provider="second", model="synthetic-auxiliary",
                     context_window=4096, max_output_tokens=4096)
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        provider = CompactProvider(lambda doc: pytest.fail("Impossible request dispatched"))
        await refresh(service, client, provider)
        task = service.store.pending_maintenance_task()
        status = service.store.maintenance_status()
        assert provider.calls == []
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.input_overhead"
        assert status["model_calls"] == 0 and status["latest_attempt"] is None
        assert status["unresolved"]["configuration_attention"] == 1
        assert service.store.continuity_state()["watermark"] == 0


@pytest.mark.parametrize("mode", ["inherit", "separate"])
async def test_response_allowance_bounds_complete_batches_without_raising_caps(
    tmp_path, monkeypatch, mode,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, mode=mode, provider="second", model="synthetic-auxiliary",
                     max_output_tokens=512, timeout_seconds=17)
        texts = [f"My creative project has unfinished stage {i}." for i in range(3)]
        for text in texts:
            service.store.complete_turn(service.current_session["id"], text, "We can revisit it.")
        seen = []

        def respond(doc):
            assert len(doc["exchanges"]) == 1
            assert doc["response_limits"]["max_operations"] == 1
            assert 0 < doc["response_limits"]["max_content_chars"] < 1200
            seen.extend(row["user_content"] for row in doc["exchanges"])
            return {"operations": [{"op": "no_change"}]}

        provider = CompactProvider(respond)
        for _ in texts:
            await refresh(service, client, provider)
        assert seen == texts
        assert service.store.pending_maintenance_task() is None
        assert service._auxiliary_execution.profile().parameters["max_tokens"] == 512
        assert service.continuity_worker.timeout == 17


async def test_required_topic_index_is_reported_as_state_overhead(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, context_chars=10_000)
        for i in range(30):
            service.store.create_continuity_item(
                "Saved background.", "thread", topic=f"Project {i} " + "x" * 80,
                importance="moderate", max_notes=64)
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        provider = CompactProvider(lambda doc: pytest.fail("Incomplete index dispatched"))
        await refresh(service, client, provider)
        task = service.store.pending_maintenance_task()
        assert provider.calls == []
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.input_state"


async def test_omitted_details_cannot_hide_duplicates_or_be_changed(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, context_chars=16_000)
        note = seed_note(service.store)
        # Existing long background can be too large for this smaller route.
        with service.store.db:
            data = {**note, "content": "Creative project background. " * 1000}
            service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                     (json.dumps(data), note["id"]))
        service.store.complete_turn(service.current_session["id"],
                                    "My creative project now has a sketch.", "Understood.")
        before = service.store.continuity_state()

        def duplicate(doc):
            assert doc["topic_index"][0]["topic"] == "Creative project"
            assert not doc["topic_index"][0]["detail_available"]
            assert doc["items"] == []
            return {"operations": [{"op": "upsert", "topic": "Creative project",
                                    "importance": "moderate", "category": "thread",
                                    "content": "The user's creative project has a sketch.",
                                    "sources": [doc["exchanges"][0]["source_refs"]["user"]]}]}

        provider = CompactProvider(duplicate)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        assert service.store.continuity_state() == before
        assert service.store.pending_maintenance_task() is not None

        def defer(doc):
            return {"operations": [{"op": "defer", "reason": "context_required",
                                    "note_ref": doc["topic_index"][0]["note_ref"]}]}

        await refresh(service, client, CompactProvider(defer))
        task = service.store.pending_maintenance_task()
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.input_state"
        assert service.store.continuity_state() == before


@pytest.mark.parametrize("mode", ["inherit", "separate"])
async def test_response_too_small_for_substantive_work_is_not_dispatched(
    tmp_path, monkeypatch, mode,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, mode=mode, provider="second", model="synthetic-auxiliary",
                     max_output_tokens=128)
        service.store.complete_turn(service.current_session["id"], "My project is unfinished.", "Okay.")
        provider = CompactProvider(lambda doc: pytest.fail("Impossible response dispatched"))
        await refresh(service, client, provider)
        task = service.store.pending_maintenance_task()
        assert provider.calls == []
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.output_budget"
        assert service.store.maintenance_status()["unresolved"]["configuration_attention"] == 1


async def test_compact_index_keeps_every_topic_and_protection_when_details_do_not_fit(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, context_chars=16_000)
        notes = [service.store.create_continuity_item(
            f"Project {i} background. " + "Complete background. " * 120,
            "thread", topic=f"Project {i}", importance="high" if i == 11 else "low",
        ) for i in range(12)]
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")

        def respond(doc):
            index = doc["topic_index"]
            assert {entry["topic"] for entry in index} == {note["topic"] for note in notes}
            assert all(entry["user_created"] and entry["protected"] for entry in index)
            assert next(entry for entry in index if entry["topic"] == "Project 11")[
                "importance"] == "high"
            assert len(doc["items"]) < len(index)
            refs = {entry["topic"]: entry["note_ref"] for entry in index}
            assert all(item["note_ref"] == refs[item["topic"]] for item in doc["items"])
            return {"operations": [{"op": "no_change"}]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        assert service.store.pending_maintenance_task() is None


@pytest.mark.parametrize("kind", ["legacy", "capacity"])
async def test_specialized_work_uses_its_response_budget_and_makes_useful_progress(
    tmp_path, monkeypatch, kind,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        notes = [seed_note(service.store, topic=f"Creative project {i}") for i in range(3)]
        if kind == "legacy":
            with service.store.db:
                for note in notes:
                    data = {key: value for key, value in note.items()
                            if key not in {"topic", "importance"}}
                    service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                             (json.dumps(data), note["id"]))
        else:
            await client.request("save_preferences", preferences={"continuity_max_notes": 1})
        await select(service, max_output_tokens=512 if kind == "legacy" else 256)

        def respond(doc):
            assert doc["response_limits"]["max_operations"] == 1
            if kind == "legacy":
                assert len(doc["target_refs"]) == 1
                target = next(item for item in doc["items"]
                              if item["note_ref"] == doc["target_refs"][0])
                topic = next(note["topic"] for note in notes if note["id"] == target["id"])
                return {"operations": [{"op": "upsert", "note_ref": target["note_ref"],
                                        "topic": topic, "importance": "moderate"}]}
            return {"operations": [{"op": "archive", "note_ref": doc["items"][0]["note_ref"],
                                    "decision_reason": "Less continuing relevance."}]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        state = service.store.continuity_state()
        if kind == "legacy":
            assert sum(bool(note.get("metadata_pending")) for note in state["items"]) == 2
        else:
            assert state["active_count"] == 2


async def test_settings_test_reports_preflight_limit_without_connectivity_claim(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        def unexpected(request):
            pytest.fail("Impossible synthetic request dispatched")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
            monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider",
                                lambda profile, **kwargs: OpenAIProvider(profile, client=http, **kwargs))
            result = await client.request("test_auxiliary_model", auxiliary={
                "mode": "inherit", "context_window": 4096, "max_output_tokens": 4096})
        assert result["ok"] is False and result["connected"] is False
        assert result["reason_code"] == "validation.input_overhead"
        assert "instructions" in result["notice"] and "Preferences" in result["notice"]


@pytest.mark.parametrize("unit", ["characters", "tokens"])
@pytest.mark.parametrize("structured", [False, True])
async def test_schema_overhead_is_part_of_both_allowances_and_wire_caps_stay_exact(
    tmp_path, monkeypatch, unit, structured,
):
    config = make_config(tmp_path)
    profile = config.selected_provider()
    if structured:
        profile.maintenance_output_support = [MaintenanceOutputSupport(
            endpoint=profile.endpoint, model=profile.model, json_schema="enforced")]
    monkeypatch.setattr("test_service.make_config", lambda _: config)
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        packet = {"choices": [{"delta": {"content": '{"operations":[{"op":"no_change"}]}'},
                               "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n")

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, max_output_tokens=512,
                     **({"context_chars": 8_000} if unit == "characters" else
                        {"context_window": 2400}))
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            provider = OpenAIProvider(service._auxiliary_execution.profile(), client=http)
            await refresh(service, client, provider)
        if structured:
            assert payloads == []
            task = service.store.pending_maintenance_task()
            assert task["stages"]["continuity"]["reason"]["code"] == "validation.input_overhead"
        else:
            assert len(payloads) == 1 and payloads[0]["max_tokens"] == 512
            assert service.store.pending_maintenance_task() is None


async def test_single_oversized_exchange_is_retained_whole_with_source_reason(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, context_chars=8_000)
        text = "An unfinished project. " * 2000
        service.store.complete_turn(service.current_session["id"], text, "Complete companion answer.")
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        provider = CompactProvider(lambda doc: pytest.fail("Oversized source dispatched"))
        await refresh(service, client, provider)
        task = service.store.pending_maintenance_task()
        assert provider.calls == [] and len(task["inputs"]) == 1
        assert task["inputs"][0]["user_content"] == text
        assert task["inputs"][0]["assistant_content"] == "Complete companion answer."
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.input_limit"
        assert service.store.continuity_state()["watermark"] == 0


@pytest.mark.parametrize("excess", ["operations", "prose"])
async def test_response_that_ignores_planned_limits_cannot_complete_evidence(
    tmp_path, monkeypatch, excess,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, max_output_tokens=512)
        service.store.complete_turn(service.current_session["id"], "My project is unfinished.", "Okay.")

        def respond(doc):
            if excess == "operations":
                return {"operations": [{"op": "no_change"}] * 2}
            return {"operations": [{"op": "upsert", "topic": "Creative project",
                                    "category": "thread", "importance": "moderate",
                                    "content": "Project background. " * 30,
                                    "sources": [doc["exchanges"][0]["source_refs"]["user"]]}]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        assert service.store.continuity_state()["watermark"] == 0
        task = service.store.pending_maintenance_task()
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.output_plan"


async def test_reducing_output_splits_a_captured_batch_without_dispatching_over_plan(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        for _ in range(3):
            service.store.complete_turn(service.current_session["id"], "My project is unfinished.",
                                        "We can revisit it.")
        await refresh(service, client, CompactProvider(lambda doc: {}))
        original = service.store.pending_maintenance_task()
        assert len(original["inputs"]) == 3
        await select(service, max_output_tokens=512)
        provider = CompactProvider(lambda doc: pytest.fail("Unbounded retry dispatched"))
        await refresh(service, client, provider)
        held = service.store.pending_maintenance_task()
        assert provider.calls == [] and held["inputs"] == original["inputs"][:1]
        assert held["parent_id"] == original["id"]
        assert held["stages"]["continuity"]["reason"]["code"] == "validation.output_budget"
        assert held["stages"]["continuity"]["budget"]["used"] == 0
        parent = service.store.get_maintenance_task(original["id"])
        assert parent["inputs"] == original["inputs"]
        assert parent["stages"]["continuity"]["status"] == "split"


async def test_explicit_thinking_reserve_cannot_leave_no_room_for_an_answer(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.selected_provider().parameters["thinking"] = {"type": "enabled", "budget_tokens": 4000}
    monkeypatch.setattr("test_service.make_config", lambda _: config)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "My project is unfinished.", "Okay.")
        provider = CompactProvider(lambda doc: pytest.fail("No answer allowance remains"))
        await refresh(service, client, provider)
        assert provider.calls == []
        assert service._auxiliary_execution.profile().parameters["thinking"]["budget_tokens"] == 4000
        task = service.store.pending_maintenance_task()
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.output_budget"


async def test_legacy_target_details_are_required_before_dispatch(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store)
        with service.store.db:
            data = {key: value for key, value in note.items() if key not in {"topic", "importance"}}
            data["content"] = "Existing narrative background. " * 1000
            service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                     (json.dumps(data), note["id"]))
        await select(service, context_chars=8_000)
        provider = CompactProvider(lambda doc: pytest.fail("Missing legacy target dispatched"))
        await refresh(service, client, provider)
        task = service.store.pending_maintenance_task()
        assert provider.calls == [] and task["id"].startswith("narrative:")
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.input_state"
        assert service.store.get_continuity_item(note["id"])["content"] == data["content"]


async def test_existing_collection_over_inspector_limit_still_has_a_complete_topic_index(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        prototype = service.store.create_continuity_item(
            "Saved project background.", "thread", topic="Project 0", importance="moderate")
        # Older versions allowed collections larger than the current admission cap.
        with service.store.db:
            for i in range(1, 105):
                note = {**prototype, "id": f"legacy-project-{i}", "topic": f"Project {i}"}
                service.store.db.execute(
                    "INSERT INTO continuity_items VALUES (?,?,?,?,?)",
                    (note["id"], "real_life", 1, json.dumps(note), note["observed_at"]))
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        seen = []

        def respond(doc):
            seen.extend(doc["topic_index"])
            return {"operations": [{"op": "no_change"}]}

        await refresh(service, client, CompactProvider(respond))
        assert {note["topic"] for note in seen} == {f"Project {i}" for i in range(105)}
        assert all(note["protected"] for note in seen)
        assert len(service.store.continuity_state()["items"]) == 100
        assert service.store.pending_maintenance_task() is None


async def test_optional_background_reference_limit_keeps_the_index_and_allows_progress(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        prototype = service.store.create_continuity_item(
            "Saved project background.", "thread", topic="Project 0", importance="moderate")
        for _ in range(512):
            service.store.complete_turn(service.current_session["id"],
                                        "My project is ongoing.", "We can revisit it.")
        evidence = service.store.continuity_inputs(limit=32)
        # continuity_inputs is intentionally bounded, so read completed originals in pages.
        while len(evidence) < 512:
            evidence += service.store.continuity_inputs(after=evidence[-1]["exchange_id"], limit=32)
        with service.store.db:
            service.store.db.execute("DELETE FROM continuity_items")
            for i in range(64):
                sources = [{"exchange_id": row["exchange_id"], "document_id": row["document_id"],
                            "role": "user", "origin": "user_statement", "excerpt": row["user_content"],
                            "input_hash": service.store._continuity_hash(row)}
                           for row in evidence[i * 8:(i + 1) * 8]]
                note = {**prototype, "id": f"project-{i}", "topic": f"Project {i}",
                        "origin": "derived", "content_format": "narrative", "sources": sources,
                        "locked": False, "user_created": False}
                service.store.db.execute("INSERT INTO continuity_items VALUES (?,?,?,?,?)",
                                         (note["id"], "real_life", 1, json.dumps(note),
                                          note["observed_at"]))
            service.store.db.execute("UPDATE continuity_scopes SET watermark=?",
                                     (evidence[-1]["exchange_id"],))
            # This fixture represents completed maintenance, including its durable
            # evidence accounting; the watermark alone no longer proves completion.
            service.store.db.executemany(
                "INSERT INTO continuity_evidence(scope,exchange_id,processed) "
                "VALUES ('real_life',?,1)", [(row["exchange_id"],) for row in evidence])
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        seen = []

        def respond(doc):
            seen.append(doc)
            return {"operations": [{"op": "no_change"}]}

        await refresh(service, client, CompactProvider(respond))
        assert len(seen) == 1
        assert len(seen[0]["topic_index"]) == 64
        assert len(seen[0]["items"]) == 63
        assert all(row["user_content"] == "My project is ongoing."
                   and row["assistant_content"] == "We can revisit it."
                   for row in seen[0]["supporting_exchanges"])
        assert service.store.pending_maintenance_task() is None
