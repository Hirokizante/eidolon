"""Out-of-order original evidence through the service and transactional store."""
from contextlib import closing

import pytest

from eidolon.memory.continuity import ContinuityValidationError
from eidolon.storage import Store
from test_continuity_narrative import add, narrative

from test_current_life_policy import seed_note
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("fresh_text", [
    None, "My garden project needs a greenhouse.",
    "My house move included boxes from my grandmother.",
])
async def test_old_preparation_cannot_replace_newer_supported_state(
    tmp_path, monkeypatch, closed, fresh_text,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        seed_note(service.store, topic="House move", text="My house move is ongoing.")
        if closed:
            service.store.complete_turn(service.current_session["id"],
                                        "My house move is finished.", "Congratulations.")
            await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
                "op": "archive", "note_ref": doc["items"][0]["note_ref"],
                "archive_reason": "completed", "decision_reason": "The move has concluded.",
                "content": "The user's house move has concluded.",
                "sources": [doc["exchanges"][0]["source_refs"]["user"]],
            }]}))
        before = service.store.continuity_state()
        service.store.complete_turn(
            service.current_session["id"], "I am preparing for my house move.", "Understood.",
            metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        if fresh_text:
            service.store.complete_turn(service.current_session["id"], fresh_text, "Understood.")

        def respond(doc):
            sources = [row["source_refs"]["user"] for row in doc["exchanges"]]
            return {"operations": [{
                "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
                "topic": "House move", "importance": "moderate", "category": "thread",
                "content": "The user is still preparing for a house move.", "sources": sources,
            }]}

        await refresh(service, client, CompactProvider(respond))
        assert service.store.continuity_state() == before
        task = service.store.pending_maintenance_task()
        assert task is not None
        attempt = service.store.maintenance_attempts(task["id"])[-1]
        assert attempt["outcome"] == "failed"
        assert attempt["reason"]["code"] == "validation.chronology"


async def test_old_preparation_cannot_reopen_a_closed_topic_as_a_new_note(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        seed_note(service.store, topic="House move", text="My house move is ongoing.")
        service.store.complete_turn(service.current_session["id"],
                                    "My house move is finished.", "Congratulations.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "archive", "note_ref": doc["items"][0]["note_ref"],
            "archive_reason": "completed", "decision_reason": "The move has concluded.",
            "content": "The user's house move has concluded.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        before = service.store.continuity_state()
        service.store.complete_turn(
            service.current_session["id"], "I am preparing for my house move.", "Understood.",
            metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "House move", "importance": "moderate", "category": "thread",
            "content": "The user is preparing for a house move.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        assert service.store.continuity_state() == before
        assert service.store.pending_maintenance_task() is not None


async def test_old_completion_cannot_borrow_a_later_preparation_citation(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        seed_note(service.store, topic="House move", text="My house move is ongoing.")
        before = service.store.continuity_state()
        for text, timestamp in [
            ("My house move is finished.", "2020-01-01T00:00:00+00:00"),
            ("I am still preparing for my house move.", "2090-01-01T00:00:00+00:00"),
        ]:
            service.store.complete_turn(service.current_session["id"], text, "Understood.",
                                        metadata={"user_message_at": timestamp})
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "archive", "note_ref": doc["items"][0]["note_ref"],
            "archive_reason": "completed", "decision_reason": "A completion was reported.",
            "content": "The user's house move has concluded.",
            "sources": [row["source_refs"]["user"] for row in doc["exchanges"]],
        }]}))
        assert service.store.continuity_state() == before
        assert service.store.pending_maintenance_task() is not None


async def test_older_evidence_fills_a_missing_background_topic_without_changing_current_state(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="House move", text="My house move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"], "My grandmother taught me woodworking.", "Understood.",
            metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "Woodworking background", "importance": "moderate",
            "category": "relationship", "content": "The user learned woodworking from a grandparent.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        assert service.store.get_continuity_item(note["id"]) == note
        state = service.store.continuity_state()
        assert state["active_count"] == 2
        assert next(item for item in state["items"] if item["topic"] == "Woodworking background")[
            "sources"][0]["excerpt"] == "My grandmother taught me woodworking."
        assert service.store.pending_maintenance_task() is None


async def test_unrelated_saved_citation_does_not_make_current_support_newer(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        for text, timestamp in [
            ("My house move is ongoing.", "2020-01-01T00:00:00+00:00"),
            ("My garden project needs a greenhouse.", "2090-01-01T00:00:00+00:00"),
        ]:
            service.store.complete_turn(service.current_session["id"], text, "Understood.",
                                        metadata={"user_message_at": timestamp})
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "House move", "importance": "moderate", "category": "thread",
            "content": "The user's house move is ongoing.",
            "sources": [row["source_refs"]["user"] for row in doc["exchanges"]],
        }]}))
        before = service.store.continuity_state()["items"][0]
        assert before["observed_at"] == "2090-01-01T00:00:00+00:00"
        service.store.complete_turn(
            service.current_session["id"], "My house move now requires temporary storage.",
            "Understood.", metadata={"user_message_at": "2021-01-01T00:00:00+00:00"},
        )
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"], "topic": "House move",
            "importance": "moderate", "category": "thread",
            "content": "The user's house move now requires temporary storage.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        saved = service.store.get_continuity_item(before["id"])
        assert saved["content"] == "The user's house move now requires temporary storage."
        assert saved["revision"] == before["revision"] + 1
        assert service.store.pending_maintenance_task() is None


async def test_restart_and_explicit_retry_keep_old_evidence_unresolved(tmp_path, monkeypatch):
    def respond(doc):
        return {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"], "topic": "House move",
            "importance": "moderate", "category": "thread",
            "content": "The user is still preparing for a house move.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        seed_note(service.store, topic="House move", text="My house move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"], "I am preparing for my house move.", "Understood.",
            metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        before = service.store.continuity_state()
        await refresh(service, client, CompactProvider(respond))
        task = service.store.pending_maintenance_task()
        original_inputs = task["inputs"]
        task_id = task["id"]

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await refresh(service, client, CompactProvider(respond))
        pending = service.store.pending_maintenance_task()
        assert pending["id"] == task_id and pending["inputs"] == original_inputs
        assert service.store.continuity_state() == before
        details = await client.request("memory_continuity_run", task_id=task_id)
        assert len(details["attempts"]) == 2
        assert all(attempt["reason"]["code"] == "validation.chronology"
                   for attempt in details["attempts"])
        assert details["changes"] == []
        assert "originals" in details["attempts"][-1]["reason"]["explanation"]


async def test_concurrent_note_edit_invalidates_the_whole_prepared_patch(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="House move", text="My house move is ongoing.")
        service.store.complete_turn(service.current_session["id"],
                                    "My house move now requires temporary storage.", "Understood.")
        edited = None

        def respond(doc):
            nonlocal edited
            edited = service.store.edit_continuity_item(
                note["id"], {"content": "The move needs personal review.", "locked": True},
                expected_revision=note["revision"],
            )
            return {"operations": [
                {"op": "upsert", "topic": "Temporary storage", "importance": "low",
                 "category": "thread", "content": "The user needs temporary storage.",
                 "sources": [doc["exchanges"][0]["source_refs"]["user"]]},
                {"op": "upsert", "note_ref": doc["items"][0]["note_ref"], "topic": "House move",
                 "importance": "moderate", "category": "thread",
                 "content": "The user's house move now requires storage.",
                 "sources": [doc["exchanges"][0]["source_refs"]["user"]]},
            ]}

        await refresh(service, client, CompactProvider(respond))
        state = service.store.continuity_state()
        assert state["items"] == [edited]
        assert service.store.pending_maintenance_task() is not None
        assert len(service.store.continuity_history(note["id"])) == 2


@pytest.mark.parametrize("action", ["upsert", "archive"])
@pytest.mark.parametrize("fresh_background", [False, True])
def test_direct_commit_rechecks_original_chronology_and_rolls_back_all_operations(
    tmp_path, action, fresh_background,
):
    with closing(Store(tmp_path / "room.sqlite3")) as store:
        note = seed_note(store, topic="House move", text="My house move is ongoing.")
        text = "My house move is finished." if action == "archive" else "My house move is upcoming."
        row = add(store, text, metadata={"user_message_at": "2020-01-01T00:00:00+00:00"})
        inputs = [row]
        if fresh_background:
            inputs.append(add(store, "My house move included boxes from my grandmother."))
        state = store.continuity_state()
        task = store.create_maintenance_task(inputs, expected_revision=state["revision"],
                                             control_revision=state["control_revision"])
        update = narrative(row, "The user's house move is pending.", op=action,
                           topic=note["topic"], item_id=note["id"],
                           expected_revision=note["revision"])
        admission = narrative(row, "The user may need storage space.", topic="Storage space")
        for operation in (admission, update):
            operation["sources"][0]["excerpt"] = row["user"]
        if fresh_background:
            update["sources"].append({
                "exchange_id": inputs[1]["exchange_id"], "document_id": inputs[1]["document_id"],
                "role": "user", "origin": "user_statement", "excerpt": inputs[1]["user"],
            })
        with pytest.raises(ContinuityValidationError) as error:
            store.commit_continuity(task["id"], [admission, update],
                                    expected_revision=state["revision"],
                                    control_revision=state["control_revision"])
        assert error.value.code == "chronology"
        assert store.continuity_state() == state
        assert len(store.continuity_history()) == 1
        assert store.pending_maintenance_task()["id"] == task["id"]


async def test_older_background_can_extend_support_without_replacing_the_current_claim(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="House move", text="My house move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"], "My house move includes boxes from my grandmother.",
            "Understood.", metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"], "topic": note["topic"],
            "importance": note["importance"], "category": note["category"], "content": note["content"],
            "sources": [doc["items"][0]["sources"][0]["source_ref"],
                        doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        saved = service.store.get_continuity_item(note["id"])
        assert saved["content"] == note["content"] and saved["state"] == "open"
        assert saved["observed_at"] == note["observed_at"]
        assert saved["sources"][0] == note["sources"][0]
        assert saved["sources"][1]["excerpt"] == "My house move includes boxes from my grandmother."
        assert service.store.pending_maintenance_task() is None
