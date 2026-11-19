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


@pytest.mark.parametrize("usage", [None, 512, 513])
async def test_soft_response_estimate_yields_to_reported_output_cap(tmp_path, monkeypatch, usage):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, max_output_tokens=512, output_token_field="max_completion_tokens",
                     reasoning_effort="low", timeout_seconds=17)
        service.store.complete_turn(service.current_session["id"],
                                    "My creative project and quilt are unfinished.", "Okay.")
        content = "The project remains unfinished. " + "AbC123_xYz 987Q " * 70
        payloads = []

        def respond(request):
            payload = json.loads(request.content)
            payloads.append(payload)
            doc = json.loads(payload["messages"][-1]["content"])
            assert len(content) > doc["response_limits"]["max_content_chars"]
            assert doc["response_limits"]["max_operations"] == 1
            assert payload["max_completion_tokens"] == 512 and payload["reasoning_effort"] == "low"
            source = doc["batch_source_refs"][0]
            proposal = {"operations": [
                {"op": "upsert", "topic": topic, "category": "thread", "importance": "moderate",
                 "content": text, "sources": [source],
                 "change": {"kind": "development", "evidence": [source]}}
                for topic, text in (("Creative project", content), ("Quilt", "The quilt is unfinished."))
            ]}
            packet = {"choices": [{"delta": {"content": json.dumps(proposal)}, "finish_reason": "stop"}]}
            if usage is not None:
                packet["usage"] = {"completion_tokens": usage}
            return httpx.Response(200, text="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            provider = OpenAIProvider(service._auxiliary_execution.profile(), client=http)
            result = await refresh(service, client, provider)
        assert len(payloads) == 1 and service.continuity_worker.timeout == 17
        latest = service.store.maintenance_status()["latest_attempt"]
        assert latest["usage"] == ({"completion_tokens": usage} if usage is not None else None)
        if usage == 513:
            assert result["continuity"]["items"] == []
            assert latest["reason"]["code"] == "validation.output_tokens"
        else:
            notes = {item["topic"]: item["content"] for item in result["continuity"]["items"]}
            assert notes == {"Creative project": content, "Quilt": "The quilt is unfinished."}
            assert latest["outcome"] == "committed"


@pytest.mark.parametrize("excess", ["operations", "prose"])
async def test_response_product_bounds_still_apply(tmp_path, monkeypatch, excess):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, max_output_tokens=512)
        service.store.complete_turn(service.current_session["id"], "My project is unfinished.", "Okay.")

        def respond(doc):
            source = doc["batch_source_refs"][0]
            op = {"op": "upsert", "topic": "Creative project", "category": "thread",
                  "importance": "moderate", "content": "x" * (1201 if excess == "prose" else 50),
                  "sources": [source], "change": {"kind": "development", "evidence": [source]}}
            return {"operations": [op] * (25 if excess == "operations" else 1)}

        provider = CompactProvider(respond, adapt=False)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        assert service.store.continuity_state()["watermark"] == 0
        task = service.store.pending_maintenance_task()
        assert task["stages"]["continuity"]["reason"]["code"] == "validation.schema"


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
    from eidolon.storage.support import record

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
                originals = evidence if i == 0 else evidence[i * 8:(i + 1) * 8]
                sources = [{"exchange_id": row["exchange_id"], "document_id": row["document_id"],
                            "role": "user", "origin": "user_statement", "excerpt": row["user_content"],
                            "input_hash": service.store._continuity_hash(row)} for row in originals]
                note = {**prototype, "id": f"project-{i}", "topic": f"Project {i}",
                        "origin": "derived", "content_format": "narrative", "sources": sources[:8],
                        "locked": False, "user_created": False}
                if i == 0:
                    # A legacy collection can hold more linked history than a
                    # current write admits. Keep every saved identity eligible.
                    note["topic"] = "Geology archive"
                    note["historical_context"] = [
                        {"content": "Older context.", "sources": [source]} for source in sources[8:]
                    ]
                    record(service.store, {**note, "sources": sources},
                           task_id=None, change_origin="migration")
                elif i == 1:
                    note["topic"] = "Mineral survey"
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
        service.store.complete_turn(service.current_session["id"],
                                    "The geology archive and mineral survey are ongoing.", "Okay.")
        seen = []

        def respond(doc):
            seen.append(doc)
            return {"operations": [{"op": "no_change"}]}

        await refresh(service, client, CompactProvider(respond))
        assert len(seen) == 1
        index = {entry["topic"]: entry for entry in seen[0]["topic_index"]}
        assert len(index) == 64 and not index["Geology archive"]["detail_available"]
        assert index["Mineral survey"]["detail_available"]
        assert seen[0]["supporting_exchanges"]
        assert all(row["user_content"] == "My project is ongoing."
                   and row["assistant_content"] == "We can revisit it."
                   for row in seen[0]["supporting_exchanges"])
        assert service.store.pending_maintenance_task() is None


async def test_focused_refresh_expands_one_note_with_complete_background(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_max_notes": 64})
        original = "My ceramics kiln still needs a new element. " + "The studio is quiet. " * 30
        target = seed_note(service.store, topic="Ceramics kiln", text=original)
        related = seed_note(service.store, topic="Beekeeping", text="My apiary needs another hive.")
        for i in range(24):
            service.store.create_continuity_item(
                f"Synthetic unrelated details for exhibit {i}.", "thread",
                topic=f"Exhibit {i}", importance="moderate", max_notes=64)
        batch = "The apiary is unchanged. The other repair now has a delivery date."
        service.store.complete_turn(service.current_session["id"], batch, "I wrote that down.")
        seen = []

        def respond(doc):
            seen.append(doc)
            index = {note["topic"]: note for note in doc["topic_index"]}
            assert len(index) == 26 and index["Exhibit 23"]["protected"] is True
            assert doc["exchanges"][0]["user_content"] == batch
            fresh = doc["exchanges"][0]["source_refs"]["user"]
            assert fresh == "s1" and fresh in doc["batch_source_refs"]
            ref = index[target["topic"]]["note_ref"]
            details = {note["topic"] for note in doc["items"]}
            if len(seen) == 1:
                assert related["topic"] in details
                assert target["topic"] not in details
                assert len(details) < 12
                return {"operations": [{"op": "defer", "reason": "context_required",
                                        "note_ref": ref}]}
            assert ref == next(note["note_ref"] for note in doc["items"]
                               if note["topic"] == target["topic"])
            prior = next(row for row in doc["supporting_exchanges"] if row["user_content"] == original)
            background = prior["source_refs"]["user"]
            assert background.startswith("background") and background not in doc["batch_source_refs"]
            return {"operations": [{"op": "upsert", "note_ref": ref, "topic": target["topic"],
                                    "category": "thread", "importance": "low",
                                    "content": "The user's ceramics kiln element has a delivery date.",
                                    "sources": [fresh, background],
                                    "change": {"kind": "development", "evidence": [fresh]}}]}

        await refresh(service, client, CompactProvider(respond, adapt=False))
        assert len(seen) == 2
        saved = service.store.get_continuity_item(target["id"])
        assert saved["content"] == "The user's ceramics kiln element has a delivery date."
        committed = service.store.maintenance_status()["latest_attempt"]
        stage = service.store.get_maintenance_stage(committed["task_id"])
        assert stage["budget"]["used"] == 2
        attempts = service.store.maintenance_attempts(committed["task_id"])
        assert len(attempts) == 2
        assert attempts[0]["provenance"] == attempts[1]["provenance"]


@pytest.mark.parametrize("allowance,schema_status", [(1, "enforced"), (2, "enforced"), (2, "ignored")])
async def test_historical_expansion_uses_existing_route_and_never_loops(
    tmp_path, monkeypatch, allowance, schema_status,
):
    config = make_config(tmp_path)
    profile = config.selected_provider()
    profile.maintenance_output_support = [MaintenanceOutputSupport(
        endpoint=profile.endpoint, model=profile.model, json_schema=schema_status,
        reasoning_effort="low")]
    monkeypatch.setattr("test_service.make_config", lambda _: config)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_recovery_calls": allowance})
        await select(service, max_output_tokens=1024, reasoning_effort="low", timeout_seconds=17)
        original = "My creative project began as a pen sketch."
        note = seed_note(service.store, text=original)
        session_id = service.current_session["id"]
        recent = "My creative project is now a watercolor."
        service.store.complete_turn(session_id, recent, "That sounds promising.")

        def update(doc):
            source = doc["batch_source_refs"][0]
            return {"operations": [{"op": "upsert", "note_ref": doc["items"][0]["note_ref"],
                                    "topic": note["topic"], "category": "thread", "importance": "low",
                                    "content": "The user's creative project is now a watercolor.",
                                    "sources": [source],
                                    "change": {"kind": "development", "evidence": [source]}}]}

        saved = await refresh(service, client, CompactProvider(update, adapt=False))
        assert saved["continuity"]["items"][0]["content"] == (
            "The user's creative project is now a watercolor."
        )
        batch = "Did my creative project begin with watercolor?"
        service.store.complete_turn(session_id, batch, "Let's check the original.")
        payloads = []

        def respond(request):
            payload = json.loads(request.content)
            payloads.append(payload)
            doc = json.loads(payload["messages"][-1]["content"])
            ref = doc["items"][0]["note_ref"]
            assert doc["exchanges"][0]["user_content"] == batch
            assert doc["supporting_exchanges"][0]["user_content"] == recent
            if len(payloads) == 1:
                assert "historical_originals" not in doc
            else:
                assert doc["expanded_note_ref"] == ref
                prior, = doc["historical_originals"]
                assert prior["user_content"] == original
                assert prior["assistant_content"]
                assert "source_refs" not in prior
            result = {"operations": [{"op": "defer", "reason": "context_required", "note_ref": ref}]}
            packet = {"choices": [{"delta": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await refresh(service, client,
                          OpenAIProvider(service._auxiliary_execution.profile(), client=http))
        assert len(payloads) == allowance
        for payload in payloads:
            assert payload["reasoning_effort"] == "low" and payload["max_tokens"] == 1024
            if schema_status == "enforced":
                assert payload["response_format"] == payloads[0]["response_format"]
                assert payload["response_format"]["type"] == "json_schema"
            else:
                assert "response_format" not in payload
        task = service.store.pending_maintenance_task()
        stage = task["stages"]["continuity"]
        assert stage["reason"]["code"] == "validation.context_required"
        assert stage["budget"]["used"] == allowance
        assert task["inputs"][0]["user_content"] == batch
        attempts = service.store.maintenance_attempts(task["id"])
        assert len(attempts) == allowance
        assert all(a["provenance"] == attempts[0]["provenance"] for a in attempts)
        assert attempts[0]["provenance"]["capability_source"] == "configured_verification"
        assert attempts[0]["provenance"]["output_mode"] == (
            "json_schema" if schema_status == "enforced" else "prompt"
        )


@pytest.mark.parametrize("body,finish,code", [
    ('{"operations":[{"op":"no_change"}]}', "length", "output_incomplete"),
    ('{"operations":[{"op":"no_change"}],"operations":[]}', "stop", "output_json"),
    ('{"operations":[{"op":"upsert","topic":"Creative project"}]}', "stop", "metadata_missing"),
    (" " * 24_001, "stop", "output_limit"),
])
async def test_unusable_stream_keeps_evidence_available(tmp_path, monkeypatch, body, finish, code):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "My project is ongoing.", "Okay.")

        class ScriptedProvider:
            async def stream(self, messages, **options):
                yield {"type": "token", "text": body}
                yield {"type": "finish", "reason": finish}

        result = await refresh(service, client, ScriptedProvider())
        assert result["continuity"]["items"] == []
        task = service.store.pending_maintenance_task()
        assert task["stages"]["continuity"]["reason"]["code"] == "validation." + code
        assert task["inputs"][0]["user_content"] == "My project is ongoing."


@pytest.mark.parametrize("packets,code", [
    ([{"timings": {"predicted_n": 513}}], "output_tokens"),
    ([{"usage": {"completion_tokens": 513}}, {"usage": {"completion_tokens": 7}}],
     "output_tokens"),
    ([{"usage": {"input_tokens": 6000, "cache_read_input_tokens": 4000, "output_tokens": 20}}],
     "context_tokens"),
    ([{"usage": {"prompt_tokens": 9850, "completion_tokens": 50, "total_tokens": 10_001}}],
     "context_tokens"),
])
async def test_normalized_provider_usage_enforces_configured_cap(
    tmp_path, monkeypatch, packets, code,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await select(service, max_output_tokens=512, context_window=10_000)
        service.store.complete_turn(service.current_session["id"], "Hello.", "Hello.")
        complete = {"choices": [{"delta": {"content": '{"operations":[{"op":"no_change"}]}'},
                                 "finish_reason": "stop"}]}
        events = "".join("data: " + json.dumps(packet) + "\n\n"
                         for packet in [complete, *packets]) + "data: [DONE]\n\n"

        def respond(request):
            return httpx.Response(200, text=events)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await refresh(service, client,
                          OpenAIProvider(service._auxiliary_execution.profile(), client=http))
        task = service.store.pending_maintenance_task()
        assert task["stages"]["continuity"]["reason"]["code"] == "validation." + code
        assert task["inputs"][0]["user_content"] == "Hello."
