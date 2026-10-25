"""Smaller atomic recovery through the service and durable store interfaces."""
import json
import asyncio

import pytest

import test_maintenance_recovery as recovery
from test_maintenance_recovery import automatic
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room

clock = recovery.clock


async def test_malformed_correction_splits_complete_evidence_under_one_budget(
    tmp_path, monkeypatch, clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        for text in ("My garden project is unfinished.", "My interview is tomorrow."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")

        def respond(doc):
            if len(doc["exchanges"]) > 1:
                return {"operations": []}
            row = doc["exchanges"][0]
            garden = "garden" in row["user_content"]
            return {"operations": [{
                "op": "upsert", "category": "thread", "importance": "moderate",
                "topic": "Garden project" if garden else "Interview preparation",
                "content": ("The user's garden project remains unfinished." if garden else
                            "The user's interview is tomorrow."),
                "sources": [row["source_refs"]["user"]],
            }]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        parent_id = service.store.pending_maintenance_task()["id"]
        originals = service.store.get_maintenance_task(parent_id)["inputs"]
        for _ in range(3):
            clock.advance()
            await automatic(service, provider)

        detail = await client.request("memory_continuity_run", task_id=parent_id)
        assert detail["status"] == "completed"
        assert detail["budget"]["used"] == 4
        assert detail["budget"]["remaining"] == 2
        assert detail["split"]["completed"] == 2
        assert detail["split"]["unresolved"] == 0
        children = detail["split"]["children"]
        assert len(children) == 2
        saved = [service.store.get_maintenance_task(child["task_id"]) for child in children]
        assert [row for child in saved for row in child["inputs"]] == originals
        assert all(child["parent_id"] == parent_id and child["root_id"] == parent_id
                   for child in saved)
        assert len(service.store.continuity_state()["items"]) == 2
        assert service.store.maintenance_status()["evidence"] == {"processed": 2, "unresolved": 0}
        assert service.store.maintenance_status()["model_calls"] == 4
        assert [len(json.loads(call[-1]["content"])["exchanges"]) for call in provider.calls] == [
            2, 2, 1, 1]
        history = service.store.continuity_history()
        for child in saved:
            assert not await service.continuity_worker.run_once(retry_task_id=child["id"])
            service.store.commit_continuity(
                child["id"], [{"op": "no_change", "expected_revision": 0}],
                expected_revision=0, control_revision=0)
        assert len(provider.calls) == 4
        assert service.store.continuity_history() == history


async def test_exhaustion_mid_split_retains_only_unfinished_child_and_new_work_progresses(
    tmp_path, monkeypatch, clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 3,
        })
        for text in ("A garden project.", "An interview."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        provider = CompactProvider(lambda doc: {"operations": []} if len(doc["exchanges"]) > 1
                                   else {"operations": [{"op": "no_change"}]})
        await refresh(service, client, provider)
        parent_id = service.store.pending_maintenance_task()["id"]
        for _ in range(2):
            clock.advance()
            await automatic(service, provider)
        status = (await client.request("status"))["maintenance"]
        assert status["evidence"] == {"processed": 1, "unresolved": 1}
        assert status["retained_batches"] == 1
        assert status["unresolved"] == {
            "recovering": 0, "configuration_attention": 0, "set_aside": 1, "total": 1}
        detail = await client.request("memory_continuity_run", task_id=parent_id)
        assert detail["split"]["completed"] == 1 and detail["split"]["unresolved"] == 1
        assert detail["budget"]["remaining"] == 0
        service.store.complete_turn(service.current_session["id"], "New subject.", "Okay.")
        clock.advance()
        await automatic(service, provider)
        assert len(provider.calls) == 4
        assert service.store.maintenance_status()["evidence"] == {"processed": 2, "unresolved": 1}
        assert service.store.maintenance_status()["unresolved"]["set_aside"] == 1


@pytest.mark.parametrize("committed_children", [0, 1, 2])
async def test_restart_at_each_child_commit_boundary_never_pays_for_completed_work(
    tmp_path, monkeypatch, clock, committed_children,
):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        for text in ("A garden project.", "An interview."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        provider = CompactProvider(lambda doc: {"operations": []} if len(doc["exchanges"]) > 1
                                   else {"operations": [{"op": "no_change"}]})
        await refresh(service, client, provider)
        parent_id = service.store.pending_maintenance_task()["id"]
        clock.advance()
        await automatic(service, provider)
        if committed_children == 0:
            split = service.store.maintenance_split(parent_id)
            assert service.store.split_maintenance_task(parent_id) == [
                child["task_id"] for child in split["children"]]
            service.store.fail_maintenance_stage(
                parent_id, "continuity", "A delayed duplicate failure", reason_code="unknown")
            assert service.store.get_maintenance_stage(parent_id)["status"] == "split"
            with pytest.raises(ValueError, match="completed or split"):
                service.store.start_maintenance_attempt(
                    parent_id, service._auxiliary_execution.provenance())
            with pytest.raises(ValueError, match="Split maintenance parents"):
                service.store.commit_continuity(
                    parent_id, [{"op": "no_change", "expected_revision": 0}],
                    expected_revision=0, control_revision=0)
        for _ in range(committed_children):
            clock.advance()
            await automatic(service, provider)
        before = await client.request("memory_continuity_run", task_id=parent_id)
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            after = await restarted.handle("memory_continuity_run", {"task_id": parent_id})
            assert after["budget"] == before["budget"]
            assert after["split"] == before["split"]
            for _ in range(3):
                clock.advance()
                await automatic(restarted, provider)
            final = await restarted.handle("memory_continuity_run", {"task_id": parent_id})
            assert final["status"] == "completed"
            assert final["budget"]["used"] == 4 and final["budget"]["pass"] == 1
            assert len(provider.calls) == 4
            assert restarted.maintenance_status()["evidence"] == {"processed": 2, "unresolved": 0}
        finally:
            await restarted.close()


async def test_unprocessable_single_child_is_set_aside_while_sibling_commits(
    tmp_path, monkeypatch, clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_daily_retry_calls": 0,
        })
        for text in ("A difficult project.", "An ordinary project."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        provider = CompactProvider(lambda doc: {"operations": []} if (
            len(doc["exchanges"]) > 1 or "difficult" in doc["exchanges"][0]["user_content"]
        ) else {"operations": [{"op": "no_change"}]})
        await refresh(service, client, provider)
        parent_id = service.store.pending_maintenance_task()["id"]
        for _ in range(5):
            clock.advance()
            await automatic(service, provider)
        detail = await client.request("memory_continuity_run", task_id=parent_id)
        assert detail["split"]["completed"] == 1 and detail["split"]["unresolved"] == 1
        assert detail["budget"]["used"] == 5 and detail["budget"]["remaining"] == 1
        assert detail["next_action"] == "automatic_disabled"
        assert len(provider.calls) == 5
        assert service.maintenance_status()["evidence"] == {"processed": 1, "unresolved": 1}
        assert service.maintenance_status()["unresolved"]["set_aside"] == 1
        # Retrying the parent starts exactly one fresh family pass, preserving
        # the successful sibling and lifetime counts.
        service.continuity_worker.provider = CompactProvider(
            lambda doc: {"operations": [{"op": "no_change"}]})
        await client.request("memory_retry", task_id=parent_id)
        if service._continuity_task is not None:
            await service._continuity_task
        final = await client.request("memory_continuity_run", task_id=parent_id)
        assert final["status"] == "completed"
        assert final["budget"]["pass"] == 2 and final["budget"]["used"] == 1
        assert service.maintenance_status()["model_calls"] == 6


async def test_cancelled_child_consumes_shared_slot_and_duplicate_dispatch_is_rejected(
    tmp_path, monkeypatch, clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 3,
        })
        for text in ("A garden project.", "An interview."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        invalid = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, invalid)
        clock.advance()
        await automatic(service, invalid)
        child_id = service.store.pending_maintenance_task()["id"]
        dispatched = asyncio.Event()

        class HangingProvider:
            async def stream(self, messages, **kwargs):
                with pytest.raises(ValueError, match="already has a dispatched attempt"):
                    service.store.start_maintenance_attempt(
                        child_id, service._auxiliary_execution.provenance())
                dispatched.set()
                await asyncio.Event().wait()
                yield {}

        service.continuity_worker.provider = HangingProvider()
        assert service._start_continuity()
        await asyncio.wait_for(dispatched.wait(), timeout=5)
        service.continuity_worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service._continuity_task
        detail = await client.request("memory_continuity_run", task_id=child_id)
        assert detail["budget"]["remaining"] == 0
        assert detail["attempts"][0]["outcome"] == "interrupted"
        assert service.maintenance_status()["unresolved"]["set_aside"] == 2
        assert service.maintenance_status()["evidence"] == {"processed": 0, "unresolved": 2}


@pytest.mark.parametrize("protected", [False, True])
async def test_child_replacement_commits_the_whole_dependency_group_or_preserves_old_note(
    tmp_path, monkeypatch, clock, protected,
):
    from test_current_life_policy import seed_note

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_max_notes": 1,
        })
        old = seed_note(service.store)
        if protected:
            old = service.store.edit_continuity_item(
                old["id"], {"locked": True}, expected_revision=old["revision"])
        for text in ("Our anniversary is approaching.", "An ordinary subject."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        child_calls = 0

        def respond(doc):
            nonlocal child_calls
            if len(doc["exchanges"]) > 1:
                return {"operations": []}
            if "anniversary" not in doc["exchanges"][0]["user_content"]:
                return {"operations": [{"op": "no_change"}]}
            child_calls += 1
            # An orphan archive must never remove the old note while waiting for
            # a corrected replacement, even when an earlier child can commit.
            archive = {"op": "archive", "note_ref": "n1", "archive_reason": "replaced",
                       "decision_reason": "The anniversary is more consequential.", "sources": []}
            if child_calls == 1:
                return {"operations": [archive]}
            return {"operations": [archive, {
                "op": "upsert", "topic": "Our anniversary", "importance": "high",
                "category": "thread", "content": "The user's anniversary is approaching.",
                "replaces_ref": "n1", "decision_reason": "The anniversary matters more.",
                "sources": [doc["exchanges"][0]["source_refs"]["user"]],
            }]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        parent_id = service.store.pending_maintenance_task()["id"]
        for _ in range(2):
            clock.advance()
            await automatic(service, provider)
        assert service.store.get_continuity_item(old["id"]) == old
        for _ in range(2):
            clock.advance()
            await automatic(service, provider)
        current = service.store.get_continuity_item(old["id"])
        detail = await client.request("memory_continuity_run", task_id=parent_id)
        if protected:
            assert current == old
            assert detail["split"]["completed"] == 1 and detail["split"]["unresolved"] == 1
            assert service.maintenance_status()["evidence"]["unresolved"] == 1
        else:
            assert current["state"] == "archived", [
                service.store.get_maintenance_stage(child["task_id"])["reason"]
                for child in detail["split"]["children"]]
            assert current["archive_reason"] == "replaced"
            assert detail["status"] == "completed"
            notes = service.store.list_continuity_items(include_inactive=False)
            assert len(notes) == 1 and notes[0]["topic"] == "Our anniversary"


async def test_nested_children_share_the_root_budget_and_complete_all_ancestors(
    tmp_path, monkeypatch, clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 8,
        })
        for number in range(3):
            service.store.complete_turn(service.current_session["id"],
                                        f"An unfinished project {number}.", "Understood.")
        provider = CompactProvider(lambda doc: {"operations": []} if len(doc["exchanges"]) > 1
                                   else {"operations": [{"op": "no_change"}]})
        await refresh(service, client, provider)
        root_id = service.store.pending_maintenance_task()["id"]
        for _ in range(7):
            clock.advance()
            await automatic(service, provider)
        detail = await client.request("memory_continuity_run", task_id=root_id)
        assert detail["status"] == "completed"
        assert detail["split"]["completed"] == 3 and detail["split"]["unresolved"] == 0
        assert detail["budget"]["used"] == 7 and detail["budget"]["remaining"] == 1
        assert len(provider.calls) == 7
        assert service.maintenance_status()["evidence"] == {"processed": 3, "unresolved": 0}
        for child in detail["split"]["children"]:
            saved = service.store.get_maintenance_task(child["task_id"])
            assert saved["stages"]["continuity"]["budget"] == detail["budget"]
            assert saved["stages"]["continuity"]["status"] == "completed"


async def test_v12_upgrade_preserves_original_recovery_allowance_and_evidence(
    tmp_path, monkeypatch, clock,
):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 3,
        })
        for text in ("A garden project.", "An interview."):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        provider = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, provider)
        original = service.store.pending_maintenance_task()
        # Recreate the v12 boundary without lineage. Existing state and calls
        # must survive adding that table; there is no new initial allowance.
        with service.store.db:
            service.store.db.execute("DROP TABLE maintenance_lineage")
            service.store.db.execute("PRAGMA user_version=12")
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            assert restarted.store.migration_backup_path.exists()
            assert restarted.store.get_maintenance_task(original["id"])["inputs"] == original["inputs"]
            clock.advance()
            await automatic(restarted, provider)
            detail = await restarted.handle("memory_continuity_run", {"task_id": original["id"]})
            assert detail["budget"]["used"] == 2 and detail["budget"]["remaining"] == 1
            assert detail["budget"]["pass"] == 1 and detail["split"]["unresolved"] == 2
            assert restarted.maintenance_status()["evidence"] == {"processed": 0, "unresolved": 2}
        finally:
            await restarted.close()
