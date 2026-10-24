"""Retained gaps, newer progress and bounded replay through the service API."""
import pytest

import test_maintenance_recovery as recovery
from test_maintenance_recovery import automatic
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.fixture
def recovery_clock(monkeypatch):
    return recovery.clock.__wrapped__(monkeypatch)


async def test_exhausted_old_batch_does_not_block_new_evidence_after_restart(
    tmp_path, monkeypatch, recovery_clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_idle_seconds": 1,
            "continuity_recovery_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "My house move is upcoming.",
                                    "Understood.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": []}))
        task = service.store.pending_maintenance_task()
        original = task["inputs"]
        task_id = task["id"]
        service.store.complete_turn(service.current_session["id"], "My house move is finished.",
                                    "Congratulations.")

    from eidolon.config import load_config
    configuration = service.configuration
    monkeypatch.setattr("test_service.make_config", lambda _: load_config(configuration))
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        recovery_clock.advance()
        newer = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "House move", "importance": "moderate", "category": "thread",
            "content": "The user's house move is finished.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        await automatic(service, newer)
        assert len(newer.calls) == 1
        status = (await client.request("status"))["maintenance"]
        assert status["unresolved"]["set_aside"] == 1
        assert status["evidence"] == {"processed": 1, "unresolved": 1}
        assert service.store.pending_maintenance_task()["inputs"] == original
        note = service.store.continuity_state()["items"][0]
        watermark = service.store.continuity_state()["watermark"]
        await automatic(service, newer)
        assert len(newer.calls) == 1
        # Old preparation cannot overwrite newer supported state on targeted retry.
        replay = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"], "topic": "House move",
            "importance": "moderate", "category": "thread",
            "content": "The user's house move is upcoming.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        service.continuity_worker.provider = replay
        result = await client.request("memory_retry", task_id=task_id)
        assert result["refresh_queued"]
        if service._continuity_task is not None:
            await service._continuity_task
        assert service.store.get_continuity_item(note["id"]) == note
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["attempts"][0]["reason"]["code"] == "validation.chronology"
        assert detail["budget"]["pass"] == 2
        assert detail["budget"]["exhausted"]
        assert service.store.continuity_state()["watermark"] == watermark
        # An explicit supported no-change resolves just this gap, once.
        service.continuity_worker.provider = CompactProvider(
            lambda doc: {"operations": [{"op": "no_change"}]})
        await client.request("memory_retry", task_id=task_id)
        if service._continuity_task is not None:
            await service._continuity_task
        status = (await client.request("status"))["maintenance"]
        assert status["evidence"] == {"processed": 2, "unresolved": 0}
        assert status["unresolved"]["total"] == 0
        assert service.store.continuity_state()["watermark"] == watermark
        with pytest.raises(RuntimeError, match="completed"):
            await client.request("memory_retry", task_id=task_id)


async def test_oversized_original_is_retained_without_blocking_a_smaller_source(
    tmp_path, monkeypatch, recovery_clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_idle_seconds": 1,
        })
        from test_auxiliary_service import select
        await select(service, context_chars=8000)
        service.store.complete_turn(service.current_session["id"], "Large original. " * 20000, "Okay.")
        valid = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await refresh(service, client, valid)
        retained = service.store.pending_maintenance_task()
        assert not valid.calls
        service.store.complete_turn(service.current_session["id"], "A small new exchange.", "Okay.")
        recovery_clock.advance()
        await automatic(service, valid)
        assert len(valid.calls) == 1
        assert service.store.pending_maintenance_task()["inputs"] == retained["inputs"]
        status = (await client.request("status"))["maintenance"]
        assert status["evidence"] == {"processed": 1, "unresolved": 1}
        assert status["unresolved"]["total"] == 1
        assert status["retained_batches"] == 1


async def test_retry_resolves_only_the_selected_gap_and_cannot_reset_a_busy_pass(
    tmp_path, monkeypatch, recovery_clock,
):
    import asyncio

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        failed = CompactProvider(lambda doc: {"operations": []})
        service.store.complete_turn(service.current_session["id"], "First original.", "Okay.")
        await refresh(service, client, failed)
        first = service.store.pending_maintenance_task()["id"]
        service.store.complete_turn(service.current_session["id"], "Second original.", "Okay.")
        recovery_clock.advance()
        await automatic(service, failed)
        status = (await client.request("status"))["maintenance"]
        assert status["retained_batches"] == 2
        second = next(row["task_id"] for row in status["stages"]
                      if row["stage"] == "continuity" and row["task_id"] != first)
        dispatched, release = asyncio.Event(), asyncio.Event()

        class HeldNoChange(CompactProvider):
            async def stream(self, messages, **kwargs):
                dispatched.set()
                await release.wait()
                async for event in super().stream(messages, **kwargs):
                    yield event

        provider = HeldNoChange(lambda doc: {"operations": [{"op": "no_change"}]})
        service.continuity_worker.provider = provider
        await client.request("memory_retry", task_id=second)
        await asyncio.wait_for(dispatched.wait(), 5)
        assert not (await client.request("memory_retry", task_id=second))["refresh_queued"]
        assert service.store.get_maintenance_stage(second)["budget"]["pass"] == 2
        release.set()
        if service._continuity_task:
            await service._continuity_task
        status = (await client.request("status"))["maintenance"]
        assert status["evidence"] == {"processed": 1, "unresolved": 1}
        assert status["retained_batches"] == 1 and status["unresolved"]["total"] == 1
        assert service.store.get_maintenance_stage(first)["budget"]["pass"] == 1
        assert len(provider.calls) == 1


async def test_ineligible_retained_original_cannot_block_new_work_after_explicit_retry(
    tmp_path, monkeypatch, recovery_clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "An earlier original.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": []}))
        task = service.store.pending_maintenance_task()
        with service.store.db:
            service.store.db.execute("UPDATE generations SET memory_eligible=0 WHERE id=?",
                                     (task["inputs"][0]["generation_id"],))
        provider = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        service.continuity_worker.provider = provider
        await client.request("memory_retry", task_id=task["id"])
        if service._continuity_task:
            await service._continuity_task
        stage = service.store.get_maintenance_stage(task["id"])
        assert stage["set_aside"] and stage["budget"]["used"] == 0
        assert stage["reason"]["code"] == "validation.evidence_changed"
        service.store.complete_turn(service.current_session["id"], "A later original.", "Okay.")
        recovery_clock.advance()
        await automatic(service, provider)
        assert len(provider.calls) == 1
        assert service.store.maintenance_status()["evidence"] == {"processed": 1, "unresolved": 1}


async def test_captured_batch_exceeding_new_response_plan_does_not_block_smaller_work(
    tmp_path, monkeypatch, recovery_clock,
):
    from test_auxiliary_service import select

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        await select(service, max_output_tokens=2048)
        for text in ("First original.", "Second original."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": []}))
        original = service.store.pending_maintenance_task()
        assert len(original["inputs"]) == 2
        await select(service, max_output_tokens=512)
        valid = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await refresh(service, client, valid)
        assert not valid.calls
        assert service.store.get_maintenance_stage(original["id"])["status"] == "split"
        service.store.complete_turn(service.current_session["id"], "Third original.", "Okay.")
        recovery_clock.advance()
        await automatic(service, valid)
        assert len(valid.calls) == 1
        # The sibling is assigned unresolved evidence; the newest exchange is
        # still unassigned until the next ordinary batch is captured.
        assert service.store.maintenance_status()["evidence"] == {"processed": 1, "unresolved": 1}
        assert service.store.pending_maintenance_task()["inputs"] == original["inputs"][1:]
        for _ in range(2):
            recovery_clock.advance()
            await automatic(service, valid)
        assert len(valid.calls) == 3
        assert service.store.maintenance_status()["evidence"] == {"processed": 3, "unresolved": 0}
        assert service.store.get_maintenance_stage(original["id"])["status"] == "completed"
        assert service.store.get_maintenance_task(original["id"])["inputs"] == original["inputs"]
