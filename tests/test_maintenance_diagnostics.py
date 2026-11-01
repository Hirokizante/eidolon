"""Cause-directed recovery observed through refresh, status and Run history."""
import json

import pytest

from test_current_life_policy import seed_note
from test_maintenance_recovery import automatic, clock  # noqa: F401
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.mark.parametrize("version", [None, 0, 3, 999, "2", True, 2.0])
def test_unsupported_reason_version_returns_legacy_unknown_without_recovery(version):
    from eidolon.memory.diagnostics import REASONS, read_reason, reason_details

    value = {"version": version, "code": "validation.revision", "disposition": "resnapshot",
             "explanation": "PRIVATE diagnostic text", "operation_index": 1,
             "target_ref": "n1", "source_refs": ["s1"]}
    assert read_reason(value) == {"version": 1, "code": "unknown",
                                  "explanation": REASONS["unknown"]}
    assert reason_details(value) == REASONS["unknown"]


async def test_topic_rejection_identifies_failed_attempt_and_corrects_safely(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store)
        service.store.complete_turn(service.current_session["id"], "My garden needs soil.", "Okay.")

        def mismatch(doc):
            return {"operations": [{
                "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
                "topic": note["topic"], "importance": "low", "category": "thread",
                "content": "The user's garden needs soil.", "sources": ["s1"],
            }]}

        await refresh(service, client, CompactProvider(mismatch))
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        detail = await client.request("memory_continuity_run", task_id=stage["task_id"])
        failed = detail["attempts"][0]
        reason = failed["reason"]
        assert reason["code"] == "validation.topic_support"
        assert reason["disposition"] == "correct"
        assert reason["operation_index"] == 1
        assert reason["target_ref"] == "n1" and reason["source_refs"] == ["s1"]
        assert reason["field"] == "sources"
        assert reason["attempt_id"] == failed["id"]

        def corrected(doc):
            assert doc["previous_diagnostic"] == reason
            assert doc["previous_rejection"] == reason["explanation"]
            return {"operations": [{
                "op": "upsert", "topic": "Garden supplies", "importance": "low",
                "category": "thread", "content": "The user's garden needs soil.", "sources": ["s1"],
            }]}

        await refresh(service, client, CompactProvider(corrected))
        detail = await client.request("memory_continuity_run", task_id=stage["task_id"])
        assert detail["status"] == "completed"
        assert detail["attempts"][1]["reason"] == reason
        assert service.store.get_continuity_item(note["id"])["revision"] == note["revision"]
        assert "garden needs soil" not in json.dumps(detail["attempts"])


async def test_unchanged_semantic_failure_defers_across_restart_and_newer_success(
    tmp_path, monkeypatch, clock,  # noqa: F811
):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        note = seed_note(service.store)
        service.store.complete_turn(service.current_session["id"], "My garden needs soil.", "Okay.")
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
            "topic": note["topic"], "importance": "low", "category": "thread",
            "content": "The user's garden needs soil.", "sources": ["s1"],
        }]})
        await refresh(service, client, provider)
        clock.advance()
        await automatic(service, provider)
        task_id = service.store.pending_maintenance_task()["id"]
        before = await client.request("memory_continuity_run", task_id=task_id)
        assert before["next_action"] == "defer"
        assert before["attempts"][0]["reason"]["disposition"] == "defer"
        assert before["budget"]["remaining"] == 4
        assert before["recovery_outcome"] == "unchanged"
        assert len(provider.calls) == 2
        assert service.store.mark_continuity_followup([note["id"]]) == 1
        clock.advance(86400)
        await automatic(service, provider)
        assert len(provider.calls) == 2  # Foreground follow-up bookkeeping is not new support.
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            for _ in range(3):
                clock.advance(86400)
                await automatic(restarted, provider)
            after = await restarted.handle("memory_continuity_run", {"task_id": task_id})
            assert after["budget"] == before["budget"]
            assert after["reason"] == before["reason"]
            assert after["next_action"] == "defer"
            assert len(provider.calls) == 2
            restarted.store.complete_turn(
                restarted.current_session["id"], "An unrelated newer subject.", "Okay.")
            await automatic(restarted, CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]}))
            status = (await restarted.handle("status", {}))["maintenance"]
            assert status["unresolved"]["total"] == 1
            assert status["error"]
            assert (await restarted.handle("memory_continuity_run", {"task_id": task_id}))["reason"] == before["reason"]
        finally:
            await restarted.close()


async def test_damaged_saved_support_routes_to_repair_without_futile_corrections(
    tmp_path, monkeypatch, clock,  # noqa: F811
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        note = seed_note(service.store)
        # Synthetic corruption fixture; normal application writes prohibit this.
        damaged = {**note, "sources": [{**note["sources"][0], "excerpt": "PRIVATE damaged support"}]}
        with service.store.db:
            service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                     (json.dumps(damaged), note["id"]))
        service.store.complete_turn(service.current_session["id"],
                                    "My creative project now has sketches.", "Okay.")
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
            "topic": note["topic"], "importance": "low", "category": "thread",
            "content": "The user's creative project now has sketches.", "sources": ["s1"],
        }]})
        await refresh(service, client, provider)
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["reason"]["code"] == "validation.saved_support"
        assert stage["reason"]["disposition"] == "repair"
        assert stage["next_action"] == "repair"
        assert stage["budget"]["remaining"] == 5
        clock.advance(86400)
        await automatic(service, provider)
        assert len(provider.calls) == 1
        detail = await client.request("memory_continuity_run", task_id=stage["task_id"])
        assert "PRIVATE" not in json.dumps(detail)


async def test_state_race_requests_a_new_snapshot_without_correction_feedback(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "My garden needs soil.", "Okay.")

        def raced(doc):
            service.store.create_continuity_item("An authored project note.", "thread",
                                                 topic="Project", importance="low")
            return {"operations": [{"op": "no_change"}]}

        await refresh(service, client, CompactProvider(raced))
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["reason"]["code"] == "validation.revision"
        assert stage["reason"]["disposition"] == "resnapshot"

        def fresh(doc):
            assert "previous_rejection" not in doc
            assert "previous_diagnostic" not in doc
            return {"operations": [{"op": "no_change"}]}

        await refresh(service, client, CompactProvider(fresh))
        detail = await client.request("memory_continuity_run", task_id=stage["task_id"])
        assert [attempt["outcome"] for attempt in detail["attempts"]] == ["committed", "stale"]


@pytest.mark.parametrize("mutation,code,field", [
    ({"category": None}, "schema", "category"),
    ({"note_ref": ["PRIVATE hostile reference"]}, "schema", "note_ref"),
    ({"sources": ["PRIVATE source https://private.invalid/"]}, "schema", "sources"),
    ({"PRIVATE unknown field": "PRIVATE value"}, "schema", None),
])
async def test_hostile_operation_values_never_enter_diagnostics(
    tmp_path, monkeypatch, mutation, code, field,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "My garden needs soil.", "Okay.")
        operation = {"op": "upsert", "topic": "Garden", "importance": "low", "category": "thread",
                     "content": "The user's garden needs soil.", "sources": ["s1"], **mutation}
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [operation]}))
        status = (await client.request("status"))["maintenance"]
        detail = await client.request("memory_continuity_run", task_id=status["stages"][0]["task_id"])
        reason = detail["attempts"][0]["reason"]
        assert reason["code"] == "validation." + code
        assert reason["operation_index"] == 1
        assert reason.get("field") == field
        assert "PRIVATE" not in json.dumps(status) + json.dumps(detail)
        assert "private.invalid" not in json.dumps(detail)


async def test_new_snapshot_never_reinterprets_previous_attempt_references(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        first = seed_note(service.store)
        seed_note(service.store, topic="Garden supplies", importance="moderate")
        service.store.complete_turn(service.current_session["id"], "My garden needs soil.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": "n1", "topic": first["topic"], "importance": "low",
            "category": "thread", "content": "The user's garden needs soil.", "sources": ["s1"],
        }]}))
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["reason"]["target_ref"] == "n1"
        await client.request("memory_edit_note", item_id=first["id"],
                             expected_revision=first["revision"], importance="high")

        def fresh(doc):
            assert doc["items"][0]["topic"] == "Garden supplies"
            assert "target_ref" not in doc["previous_diagnostic"]
            assert "source_refs" not in doc["previous_diagnostic"]
            assert doc["previous_diagnostic"]["attempt_id"] == stage["reason"]["attempt_id"]
            return {"operations": [{"op": "no_change"}]}

        await refresh(service, client, CompactProvider(fresh))
        detail = await client.request("memory_continuity_run", task_id=stage["task_id"])
        assert detail["status"] == "completed"
        assert detail["attempts"][1]["reason"] == stage["reason"]


async def test_captured_batch_resizes_before_dispatch_when_response_plan_shrinks(
    tmp_path, monkeypatch, clock,  # noqa: F811
):
    from test_auxiliary_service import select

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        for text in ("An open garden project.", "An interview tomorrow."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")
        invalid = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, invalid)
        task_id = service.store.pending_maintenance_task()["id"]
        await select(service, mode="separate", provider="second", model="synthetic-auxiliary",
                     max_output_tokens=600)
        clock.advance()
        valid = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await automatic(service, valid)
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["status"] == "split" and detail["split"]["unresolved"] == 2
        assert detail["reason"]["disposition"] == "resize"
        assert not valid.calls and detail["budget"]["used"] == 1
        for _ in range(2):
            clock.advance()
            await automatic(service, valid)
        final = await client.request("memory_continuity_run", task_id=task_id)
        assert final["status"] == "completed" and final["budget"]["used"] == 3


async def test_implicit_topic_conflict_releases_when_affected_note_is_removed(
    tmp_path, monkeypatch, clock,  # noqa: F811
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        note = seed_note(service.store)
        service.store.complete_turn(service.current_session["id"], "My creative project needs planning.",
                                    "Okay.", metadata={"user_message_at": "2020-01-01T00:00:00+00:00"})
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": note["topic"], "importance": "low", "category": "thread",
            "content": "The user's creative project needs planning.", "sources": ["s1"],
        }]})
        await refresh(service, client, provider)
        task_id = service.store.pending_maintenance_task()["id"]
        clock.advance()
        await automatic(service, provider)
        assert (await client.request("memory_continuity_run", task_id=task_id))["next_action"] == "defer"
        await client.request("memory_delete_note", item_id=note["id"], expected_revision=note["revision"])
        clock.advance()
        await automatic(service, provider)
        final = await client.request("memory_continuity_run", task_id=task_id)
        assert final["status"] == "completed" and len(provider.calls) == 3
        assert final["budget"]["pass"] == 1 and final["budget"]["used"] == 3
        assert final["recovery_outcome"] == "resolved"


async def test_legacy_conversion_holds_damaged_saved_support_for_repair(tmp_path, monkeypatch):
    from test_continuity_narrative import legacy

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = legacy(service.store)
        damaged = {**note, "sources": [{**note["sources"][0], "excerpt": "PRIVATE damaged support"}]}
        with service.store.db:
            service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                     (json.dumps(damaged), note["id"]))
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["target_refs"][0], "topic": "Interview preparation",
            "importance": "moderate", "content": "The user's interview is approaching.",
        }]}))
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["reason"]["code"] == "validation.saved_support"
        assert stage["next_action"] == "repair"
        assert service.store.get_continuity_item(note["id"]).get("content_format") != "narrative"
