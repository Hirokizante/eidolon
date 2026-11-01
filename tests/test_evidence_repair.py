"""Durable note support and bounded recovery through the service interface."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_ordinary_proposals import CompactProvider, refresh
from test_maintenance_recovery import automatic
from test_service import room
from test_current_life_policy import seed_note


def age_latest_exchange(service):
    service.continuity_worker.idle_seconds = 1
    with service.store.db:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        row = service.store.recent_continuity_inputs(limit=1)[0]
        service.store.db.execute("UPDATE messages SET created_at=? WHERE generation_id=?",
                                 (stamp, row["generation_id"]))
        service.store.db.execute("UPDATE generations SET created_at=? WHERE id=?",
                                 (stamp, row["generation_id"]))


async def test_selected_citations_stay_small_without_erasing_associations(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "Creative project", "importance": "moderate",
            "category": "thread", "content": "The user's creative project has a new sketch.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
            **({"note_ref": doc["items"][0]["note_ref"]} if doc["items"] else {}),
        }]})
        for index in range(10):
            service.store.complete_turn(service.current_session["id"],
                                        f"My creative project has sketch number {index}.", "Okay.")
            await refresh(service, client, provider)
        note_id = service.store.continuity_state()["items"][0]["id"]
        note = await client.request("memory_continuity_item", item_id=note_id)
        assert len(note["sources"]) == 1
        assert len(note["evidence_associations"]) == 10
        assert [entry["disposition"] for entry in note["evidence_associations"]].count("current") == 1
        assert all(entry["quality"] == "unknown" for entry in note["evidence_associations"])
        assert len(service.store.continuity_history(note_id)) == 10
        document = json.loads(provider.calls[-1][-1]["content"])
        assert len(document["supporting_exchanges"]) == 1
        assert len(provider.calls) == service.store.maintenance_status()["model_calls"] == 10


async def test_reported_saved_support_is_repaired_from_history_then_update_resumes(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        def update(doc):
            return {"operations": [{
                "op": "upsert", "topic": "Interview preparation", "importance": "moderate",
                "category": "thread", "content": "The user has an upcoming Ember Library interview.",
                "sources": [doc["exchanges"][0]["source_refs"]["user"]],
                **({"note_ref": doc["items"][0]["note_ref"]} if doc["items"] else {}),
            }]}

        for text in ("My Ember Library interview is tomorrow.", "My interview notebook is blue."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")
            await refresh(service, client, CompactProvider(update))
        note = service.store.continuity_state()["items"][0]
        service.store.complete_turn(service.current_session["id"],
                                    "My Ember Library interview now has a practice session.", "Okay.")
        reported = CompactProvider(lambda doc: {"operations": [{
            "op": "report_support", "note_ref": doc["items"][0]["note_ref"],
        }]})
        await refresh(service, client, reported)
        status = (await client.request("status"))["maintenance"]
        task_id = status["latest_attempt"]["task_id"]
        assert status["latest_attempt"]["reason"]["code"] == "validation.saved_support"
        assert status["unresolved"]["total"] == 1

        def repair(doc):
            assert doc["contract"] == "current-life-evidence-repair-v1"
            assert len(doc["items"]) == 1
            original = next(row for row in doc["exchanges"]
                            if "is tomorrow" in row["user_content"])
            return {"operations": [{"op": "repair_support", "note_ref": doc["items"][0]["note_ref"],
                                     "sources": [original["source_refs"]["user"]],
                                     "dispositions": [{"source_ref": doc["items"][0]["sources"][0]["source_ref"],
                                                       "disposition": "retracted"}]}]}

        repaired = CompactProvider(repair)
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        await automatic(service, repaired)
        saved = await client.request("memory_continuity_item", item_id=note["id"])
        assert saved["content"] == note["content"]
        assert saved["sources"][0]["excerpt"] == "My Ember Library interview is tomorrow."
        assert saved["revision"] == note["revision"] + 1
        assert {entry["disposition"] for entry in saved["evidence_associations"]} == {"current", "retracted"}
        assert service.store.get_maintenance_stage(task_id)["budget"]["used"] == 2
        assert service.store.get_maintenance_stage(task_id)["budget"]["pass"] == 1
        final = CompactProvider(update)
        await refresh(service, client, final)
        assert service.store.pending_maintenance_task() is None
        run = await client.request("memory_continuity_run", task_id=task_id)
        assert run["model_calls"] == 3
        assert run["attempt_counts"] == {"failed": 1, "repaired": 1, "committed": 1}
        assert service.store.get_continuity_item(note["id"])["revision"] == note["revision"] + 2


@pytest.mark.parametrize("failure", ["unresolved", "malformed", "foreign", "changed", "locked", "deleted"])
async def test_unsuccessful_repair_preserves_note_and_stops_equivalent_dispatches(
    tmp_path, monkeypatch, failure,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        note = seed_note(service.store, topic="Interview preparation", text="My interview is tomorrow.")
        service.store.complete_turn(service.current_session["id"], "My interview needs practice.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "report_support", "note_ref": doc["items"][0]["note_ref"],
        }]}))
        before = service.store.get_continuity_item(note["id"])

        def fail(doc):
            ref = doc["items"][0]["note_ref"]
            if failure == "unresolved":
                return {"operations": [{"op": "unresolved", "note_ref": ref}]}
            if failure == "malformed":
                return {"operations": [{"op": "repair_support", "note_ref": ref,
                                         "content": "Invented rewrite", "sources": ["s1"]}]}
            if failure == "changed":
                with service.store.db:
                    service.store.db.execute("UPDATE messages SET content='A changed original.' WHERE id=?",
                                             (note["sources"][0]["exchange_id"] - 1,))
            if failure == "locked":
                service.store.edit_continuity_item(note["id"], {"locked": True},
                                                   expected_revision=note["revision"])
            if failure == "deleted":
                service.store.delete_continuity_item(note["id"], expected_revision=note["revision"])
            # The assistant role was supplied as context but never associated
            # with this note. It cannot be adopted as historical evidence.
            source = doc["exchanges"][0]["source_refs"]["assistant" if failure == "foreign" else "user"]
            return {"operations": [{"op": "repair_support", "note_ref": ref, "sources": [source]}]}

        provider = CompactProvider(fail)
        await automatic(service, provider)
        after = service.store.get_continuity_item(note["id"])
        if failure in {"unresolved", "malformed", "foreign"}:
            assert after == before
        elif failure == "deleted":
            assert after is None
        else:
            assert after["content"] == before["content"] and after["sources"] == before["sources"]
        stage = next(row for row in (await client.request("status"))["maintenance"]["stages"]
                     if row["stage"] == "continuity" and row["status"] != "completed")
        assert stage["model_calls"] == 2 and stage["budget"]["pass"] == 1
        assert stage["reason"]["disposition"] == "defer"
        assert stage["reason"]["code"] == ("validation.repair_unresolved" if failure == "unresolved" else
                                            "validation.schema" if failure == "malformed" else
                                            "validation.source" if failure in {"foreign", "changed"} else
                                            "validation.revision")
        for _ in range(3):
            await automatic(service, provider)
        assert len(provider.calls) == 1
        assert (await client.request("status"))["maintenance"]["unresolved"]["total"] == 1
        if failure in {"unresolved", "malformed", "foreign"}:
            valid = CompactProvider(lambda doc: {"operations": [{
                "op": "repair_support", "note_ref": doc["items"][0]["note_ref"],
                "sources": [doc["candidate_source_refs"][0]],
            }]})
            await refresh(service, client, valid)
            repaired = service.store.get_continuity_item(note["id"])
            assert repaired["revision"] == before["revision"] + 1
            assert repaired["content"] == before["content"]
            assert service.store.maintenance_attempts(stage["task_id"])[0]["outcome"] == "repaired"


async def test_carried_claim_support_must_fit_before_overwriting_a_note(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview preparation", text="My interview is tomorrow.")
        for number in range(8):
            service.store.complete_turn(service.current_session["id"],
                                        f"My interview practice has step {number}.", "Okay.")

        def update(doc, count):
            return {"operations": [{
                "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
                "topic": note["topic"], "importance": "moderate", "category": "thread",
                "content": "The user's interview is approaching and they are practicing.",
                "sources": [row["source_refs"]["user"] for row in doc["exchanges"][:count]],
                "retained_sources": [doc["items"][0]["sources"][0]["source_ref"]],
            }]}

        await refresh(service, client, CompactProvider(lambda doc: update(doc, 8)))
        assert service.store.get_continuity_item(note["id"]) == note
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["reason"]["code"] == "validation.support_preservation"
        assert stage["reason"]["disposition"] == "defer"
        await refresh(service, client, CompactProvider(lambda doc: update(doc, 1)))
        saved = service.store.get_continuity_item(note["id"])
        assert len(saved["sources"]) == 2
        assert note["sources"][0] in saved["sources"]
        assert service.store.pending_maintenance_task() is None


async def test_unresolved_damage_does_not_redirect_unrelated_new_batches(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        note = seed_note(service.store)
        damaged = {**note, "sources": [{**note["sources"][0], "excerpt": "Damaged saved excerpt."}]}
        with service.store.db:
            service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                     (json.dumps(damaged), note["id"]))
        # No new evidence: a standalone repair must be bounded and held visibly.
        await refresh(service, client, CompactProvider(lambda doc: pytest.fail("No diagnosis needed")))
        unresolved = CompactProvider(lambda doc: {"operations": [{
            "op": "unresolved", "note_ref": doc["items"][0]["note_ref"],
        }]})
        await automatic(service, unresolved)
        old_task = service.store.pending_maintenance_task()

        def unrelated(doc):
            assert doc["contract"] == "current-life-ordinary-v1"
            return {"operations": [{
                "op": "upsert", "topic": "Garden supplies", "importance": "moderate",
                "category": "thread", "content": "The user's garden needs new supplies.",
                "sources": [doc["exchanges"][0]["source_refs"]["user"]],
                **({"note_ref": doc["items"][0]["note_ref"]} if doc["items"] else {}),
            }]}

        provider = CompactProvider(unrelated)
        for number in range(3):
            service.store.complete_turn(service.current_session["id"],
                                        f"My garden needs supply number {number}.", "Okay.")
            # User refresh intentionally prioritizes a retained batch; normal
            # automatic scheduling must let fresh evidence make progress.
            age_latest_exchange(service)
            await automatic(service, provider)
        assert len(provider.calls) == 3
        assert service.store.get_maintenance_stage(old_task["id"])["model_calls"] == 1
        assert service.store.get_continuity_item(note["id"])["revision"] == note["revision"]
        assert len(service.store.continuity_state()["items"]) == 2


async def test_repeated_semantic_reports_do_not_create_another_repair_root(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        note = seed_note(service.store)
        service.store.complete_turn(service.current_session["id"], "My creative project has sketches.", "Okay.")
        report = CompactProvider(lambda doc: {"operations": [{
            "op": "report_support", "note_ref": doc["items"][0]["note_ref"],
        }]})
        unresolved = CompactProvider(lambda doc: {"operations": [{
            "op": "unresolved", "note_ref": doc["items"][0]["note_ref"],
        }]})
        await refresh(service, client, report)
        await automatic(service, unresolved)
        service.store.complete_turn(service.current_session["id"], "My creative project needs supplies.", "Okay.")
        age_latest_exchange(service)
        await automatic(service, report)
        await automatic(service, unresolved)
        assert len(report.calls) == 2 and len(unresolved.calls) == 1
        stages = [row for row in service.store.maintenance_status()["stages"]
                  if (row.get("recovery_state") or {}).get("repair_target") == note["id"]]
        assert len(stages) == 1
        assert service.store.get_continuity_item(note["id"])["revision"] == note["revision"]


async def test_unresolved_damaged_note_does_not_hide_another_repair_target(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        notes = [seed_note(service.store, topic=topic) for topic in ("Creative project", "Garden project")]
        with service.store.db:
            service.store.db.execute("UPDATE continuity_items SET data=json_set(data,'$.sources',json('[]'))")
        unresolved = CompactProvider(lambda doc: {"operations": [{
            "op": "unresolved", "note_ref": doc["items"][0]["note_ref"],
        }]})
        for _ in range(4):
            await automatic(service, unresolved)
        stages = [row for row in service.store.maintenance_status()["stages"]
                  if row["stage"] == "continuity" and row["status"] == "failed"]
        assert len(unresolved.calls) == len(stages) == 2
        assert {(row.get("recovery_state") or {}).get("repair_target") for row in stages} == {
            note["id"] for note in notes}


@pytest.mark.parametrize("sources", [[], None, [{"exchange_id": "malformed"}]])
async def test_missing_working_support_can_restore_identified_historical_originals(
    tmp_path, monkeypatch, sources,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store)
        with service.store.db:
            service.store.db.execute("UPDATE continuity_items SET data=json_set(data,'$.sources',json(?))",
                                     (json.dumps(sources),))
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "repair_support", "note_ref": doc["items"][0]["note_ref"],
            "sources": [doc["candidate_source_refs"][0]],
        }]})
        await refresh(service, client, provider)  # Local diagnosis, no dispatch.
        assert not provider.calls
        await refresh(service, client, provider)
        saved = service.store.get_continuity_item(note["id"])
        assert not saved["invalidated"]
        assert saved["content"] == note["content"] and saved["sources"] == note["sources"]
        assert saved["revision"] == note["revision"] + 1


async def test_repair_of_missing_archived_support_preserves_terminal_evidence(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview preparation", text="My interview is tomorrow.")
        service.store.complete_turn(service.current_session["id"], "My interview is finished.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "archive", "note_ref": doc["items"][0]["note_ref"],
            "archive_reason": "completed", "decision_reason": "The interview finished.",
            "content": "The user's interview has concluded.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        closed = service.store.get_continuity_item(note["id"])
        assert closed["state"] == "archived"
        with service.store.db:
            service.store.db.execute("UPDATE continuity_items SET data=json_set(data,'$.sources',json('[]'))")
        await refresh(service, client, CompactProvider(lambda doc: pytest.fail("Local diagnosis expected")))

        def repair(doc, terminal):
            refs = [row["source_refs"]["user"] for row in doc["exchanges"]
                    if terminal or "tomorrow" in row["user_content"]]
            return {"operations": [{"op": "repair_support", "note_ref": doc["items"][0]["note_ref"],
                                     "sources": refs}]}

        await refresh(service, client, CompactProvider(lambda doc: repair(doc, False)))
        unresolved = service.store.get_continuity_item(note["id"])
        assert unresolved["revision"] == closed["revision"] and unresolved["sources"] == []
        stage = service.store.pending_maintenance_task()["stages"]["continuity"]
        assert stage["reason"]["code"] == "validation.repair_unresolved"
        await refresh(service, client, CompactProvider(lambda doc: repair(doc, True)))
        restored = service.store.get_continuity_item(note["id"])
        assert restored["state"] == "archived" and restored["content"] == closed["content"]
        assert restored["sources"] == closed["sources"] and restored["claim_source_count"] == 1


async def test_failed_repair_survives_restart_and_can_be_explicitly_retried(tmp_path, monkeypatch):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store)
        service.store.complete_turn(service.current_session["id"], "My creative project has sketches.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "report_support", "note_ref": doc["items"][0]["note_ref"],
        }]}))
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "unresolved", "note_ref": doc["items"][0]["note_ref"],
        }]}))
        task_id = service.store.pending_maintenance_task()["id"]
        before = service.store.get_maintenance_stage(task_id)
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            after = restarted.store.get_maintenance_stage(task_id)
            assert after["budget"] == before["budget"]
            assert after["recovery_state"] == before["recovery_state"]
            provider = CompactProvider(lambda doc: {"operations": [{
                "op": "repair_support", "note_ref": doc["items"][0]["note_ref"],
                "sources": [doc["candidate_source_refs"][0]],
            }]})
            restarted.continuity_worker.provider = provider
            await restarted.handle("memory_refresh", {})
            await restarted._continuity_task
            assert restarted.store.get_continuity_item(note["id"])["revision"] == note["revision"] + 1
            attempts = restarted.store.maintenance_attempts(task_id)
            assert [entry["outcome"] for entry in attempts] == ["repaired", "failed", "failed"]
            assert attempts[1]["reason"]["code"] == "validation.repair_unresolved"
            assert restarted.store.get_maintenance_stage(task_id)["model_calls"] == 3
        finally:
            await restarted.close()
