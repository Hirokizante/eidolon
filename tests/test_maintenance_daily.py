"""Paced shared retained-evidence recovery through the service API."""
from datetime import datetime, timezone

import pytest

from test_maintenance_recovery import automatic, clock as recovery_clock
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.fixture
def daily_clock(monkeypatch):
    clock = recovery_clock.__wrapped__(monkeypatch)
    clock.instant = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("eidolon.storage.store.datetime", clock)
    return clock


async def test_daily_allowance_is_shared_and_rotates_behind_new_evidence(
    tmp_path, monkeypatch, daily_clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_idle_seconds": 1,
            "continuity_recovery_calls": 1,
        })
        failed = CompactProvider(lambda doc: {"operations": []})
        for text in ("First project.", "Second project.", "Third project."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")
            daily_clock.advance(2)
            await automatic(service, failed)
        tasks = service.store.retained_maintenance_tasks()
        assert len(tasks) == 3
        originals = {task["id"]: task["inputs"] for task in tasks}
        service.store.complete_turn(service.current_session["id"], "Newest priority evidence.", "Okay.")
        daily_clock.advance(600)
        priority = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await automatic(service, priority)
        assert len(priority.calls) == 1 and "Newest priority evidence." in str(priority.calls)
        assert service.maintenance_status()["daily_allowance"]["used"] == 0
        calls_before = len(failed.calls)
        for _ in range(6):
            await automatic(service, failed)
            await automatic(service, failed)  # Repeated activation grants no extra calls.
            daily_clock.advance(600)
        status = (await client.request("status"))["maintenance"]
        assert len(failed.calls) - calls_before == 6
        assert status["daily_allowance"]["used"] == 6
        assert status["daily_allowance"]["remaining"] == 0
        assert status["daily_allowance"]["exhausted"]
        assert status["evidence"] == {"processed": 1, "unresolved": 3}
        for task in tasks:
            detail = await client.request("memory_continuity_run", task_id=task["id"])
            assert sum(a["recovery_source"] == "automatic" for a in detail["attempts"]) == 2
            assert detail["next_action"] == "daily_exhausted"
            assert detail["daily_allowance"]["used"] == 6
            assert service.store.get_maintenance_task(task["id"])["inputs"] == originals[task["id"]]
        service.store.complete_turn(service.current_session["id"], "A new project.", "Okay.")
        daily_clock.advance(2)
        valid = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await automatic(service, valid)
        assert len(valid.calls) == 1
        assert (await client.request("status"))["maintenance"]["daily_allowance"]["used"] == 6


async def test_restart_edits_and_day_boundaries_cannot_replenish_an_active_period(
    tmp_path, monkeypatch, daily_clock,
):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
            "continuity_daily_retry_calls": 2,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        failed = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, failed)
        task_id = service.store.pending_maintenance_task()["id"]
        daily_clock.advance(600)
        await automatic(service, failed)
        assert (await client.request("status"))["maintenance"]["daily_allowance"]["used"] == 1
        await client.request("save_preferences", preferences={
            "continuity_daily_retry_calls": 9, "timezone": "America/New_York", "reset_hour": 0,
        })
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            assert restarted.maintenance_status()["daily_allowance"]["remaining"] == 1
            await automatic(restarted, failed)
            assert len(failed.calls) == 2
            daily_clock.advance(600)
            await automatic(restarted, failed)
            assert len(failed.calls) == 3
            assert restarted.maintenance_status()["daily_allowance"]["limit"] == 2
            daily_clock.advance(600)
            await automatic(restarted, failed)
            assert len(failed.calls) == 3
            # A backwards clock must not rewind the accounting period.
            daily_clock.advance(-86400)
            await automatic(restarted, failed)
            assert restarted.maintenance_status()["daily_allowance"]["used"] == 2
            daily_clock.instant = datetime(2026, 10, 26, 0, tzinfo=timezone.utc)
            await automatic(restarted, failed)
            daily = restarted.maintenance_status()["daily_allowance"]
            assert daily["period"] == "2026-10-26" and daily["used"] == 1 and daily["limit"] == 9
            detail = await restarted.handle("memory_continuity_run", {"task_id": task_id})
            assert detail["model_calls"] == 4
            assert [a["recovery_source"] for a in detail["attempts"]] == [
                "automatic", "automatic", "automatic", "initial",
            ]
        finally:
            await restarted.close()


@pytest.mark.parametrize("category", ["configuration", "authentication"])
async def test_configuration_changes_release_one_affected_batch_without_daily_probes(
    tmp_path, monkeypatch, daily_clock, category,
):
    from eidolon.providers.openai import ProviderCapabilityError, ProviderError
    from test_auxiliary_service import select
    from eidolon.config import load_config, save_config

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_idle_seconds": 1,
            "continuity_recovery_calls": 1,
        })

        def rejected(doc):
            if category == "configuration":
                raise ProviderCapabilityError("PRIVATE provider details")
            raise ProviderError("PRIVATE provider details", category=category)

        failed = CompactProvider(rejected)
        for text in ("First project.", "Second project.", "Third project."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")
            daily_clock.advance(2)
            await automatic(service, failed)
        assert len(failed.calls) == 3
        for _ in range(3):
            daily_clock.advance(86400)
            await automatic(service, failed)
        assert len(failed.calls) == 3  # No unchanged configuration gets daily probes.
        assert service.maintenance_status()["daily_allowance"]["used"] == 0
        await client.request("save_preferences", preferences={"follow_replies": False})
        await automatic(service, failed)
        assert len(failed.calls) == 3
        await select(service, mode="separate", provider="second", model="changed-model")
        if category == "authentication":
            await automatic(service, failed)
            assert len(failed.calls) == 3  # Model changes cannot fix credentials.
            config = load_config(service.configuration)
            config.providers[1].endpoint = "https://synthetic.invalid/v1"
            save_config(config, service.configuration)
            await service._refresh_auxiliary()
        await automatic(service, failed)
        await automatic(service, failed)
        assert len(failed.calls) == 4
        assert service.maintenance_status()["daily_allowance"]["used"] == 1
        assert service.maintenance_status()["unresolved"]["configuration_attention"] == 3
        daily_clock.advance(600)
        await automatic(service, failed)
        assert len(failed.calls) == 5
        assert service.maintenance_status()["daily_allowance"]["used"] == 2
        # Failure against the changed configuration pauses that batch again.
        daily_clock.advance(86400)
        await automatic(service, failed)
        daily_clock.advance(86400)
        await automatic(service, failed)
        assert len(failed.calls) == 6
        assert "PRIVATE" not in str(service.maintenance_status())


async def test_explicit_retry_keeps_history_and_uses_a_separate_bounded_pass(
    tmp_path, monkeypatch, daily_clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
            "continuity_daily_retry_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        failed = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, failed)
        task_id = service.store.pending_maintenance_task()["id"]
        daily_clock.advance(600)
        await automatic(service, failed)
        await client.request("save_preferences", preferences={"continuity_recovery_calls": 2})
        service.continuity_worker.provider = failed
        daily_clock.advance(1)
        await client.request("memory_retry", task_id=task_id)
        if service._continuity_task:
            await service._continuity_task
        daily_clock.advance(600)
        await automatic(service, failed)
        daily_clock.advance(600)
        await automatic(service, failed)
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert len(failed.calls) == 4
        assert detail["budget"] == {"pass": 3, "limit": 2, "used": 2,
                                    "remaining": 0, "exhausted": True}
        assert detail["daily_allowance"]["used"] == 1
        assert [a["recovery_source"] for a in detail["attempts"]] == [
            "user", "user", "automatic", "initial",
        ]
        assert detail["attempt_counts"] == {"failed": 4}
        assert detail["next_action"] == "daily_exhausted"


async def test_off_foreground_and_interrupted_backlog_calls_preserve_allowance(
    tmp_path, monkeypatch, daily_clock,
):
    import asyncio
    from test_service import events
    from test_auxiliary_service import select

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": []}))
        task_id = service.store.pending_maintenance_task()["id"]
        daily_clock.advance(600)
        await select(service, mode="disabled")
        assert not service._start_continuity()
        assert not (await client.request("memory_retry", task_id=task_id))["refresh_queued"]
        assert service.maintenance_status()["daily_allowance"]["used"] == 0
        await select(service, mode="inherit")
        await client.request("save_preferences", preferences={"continuity_enabled": False})
        assert not service._start_continuity()
        assert service.maintenance_status()["daily_allowance"]["used"] == 0
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        async with service._foreground_generation():
            assert not service._start_continuity()
        dispatched = asyncio.Event()

        class Held(CompactProvider):
            async def stream(self, messages, **kwargs):
                dispatched.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover

        service.continuity_worker.provider = Held(None)
        assert service._start_continuity()
        await asyncio.wait_for(dispatched.wait(), 5)
        await events(client)
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["attempts"][0]["outcome"] == "interrupted"
        assert detail["attempts"][0]["recovery_source"] == "automatic"
        assert detail["daily_allowance"]["used"] == 1
        # Foreground created new evidence; it wins the next opportunity.
        newer = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        daily_clock.advance(601)
        await automatic(service, newer)
        assert len(newer.calls) == 1
        assert service.maintenance_status()["daily_allowance"]["used"] == 1
        assert service.maintenance_status()["evidence"] == {"processed": 1, "unresolved": 1}


async def test_predispatch_configuration_failure_does_not_spend_daily_allowance(
    tmp_path, monkeypatch, daily_clock,
):
    from test_auxiliary_service import select

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        await select(service, context_chars=8000)
        service.store.complete_turn(service.current_session["id"], "Long original. " * 20000, "Okay.")
        provider = CompactProvider(lambda doc: pytest.fail("Impossible request dispatched"))
        await refresh(service, client, provider)
        task_id = service.store.pending_maintenance_task()["id"]
        daily_clock.advance(86400)
        await automatic(service, provider)
        # A relevant change allows exactly one new planning opportunity, still too small.
        await select(service, context_chars=9000)
        await automatic(service, provider)
        await automatic(service, provider)
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert not provider.calls and detail["model_calls"] == 0
        assert detail["daily_allowance"]["used"] == 0
        assert detail["paused"] and detail["next_action"] == "configure"
        assert detail["budget"]["pass"] == 1


async def test_split_children_share_daily_calls_and_rotate_with_other_original_families(
    tmp_path, monkeypatch, daily_clock,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 3,
            "continuity_idle_seconds": 1,
        })
        for text in ("First half.", "Second half."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")
        failed = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, failed)
        parent_id = service.store.pending_maintenance_task()["id"]
        for _ in range(2):
            daily_clock.advance(600)
            await automatic(service, failed)
        parent = await client.request("memory_continuity_run", task_id=parent_id)
        assert parent["split"]["unresolved"] == 2 and parent["budget"]["exhausted"]
        await client.request("save_preferences", preferences={"continuity_recovery_calls": 1})
        service.store.complete_turn(service.current_session["id"], "Another family.", "Okay.")
        daily_clock.advance(2)
        await automatic(service, failed)
        calls_before = len(failed.calls)
        for _ in range(4):
            daily_clock.advance(600)
            await automatic(service, failed)
        import json
        texts = [json.loads(call[-1]["content"])["exchanges"][0]["user_content"]
                 for call in failed.calls[calls_before:]]
        assert texts == ["First half.", "Another family.", "Second half.", "Another family."]
        status = (await client.request("status"))["maintenance"]
        assert status["daily_allowance"]["used"] == 4
        assert status["evidence"] == {"processed": 0, "unresolved": 3}
        parent = await client.request("memory_continuity_run", task_id=parent_id)
        assert parent["next_action"] == "retry_scheduled" and parent["next_retry_at"]
        assert parent["daily_allowance"]["used"] == 4


@pytest.mark.parametrize("value", [-1, 101, True, 2.5, "6"])
async def test_daily_limit_rejects_invalid_settings(tmp_path, monkeypatch, value):
    from eidolon.config import load_config

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        with pytest.raises(RuntimeError):
            await client.request("save_preferences", preferences={"continuity_daily_retry_calls": value})
        assert load_config(service.configuration).memory.continuity_daily_retry_calls == 6


async def test_v13_upgrade_preserves_unresolved_evidence_and_unknown_attempt_sources(
    tmp_path, monkeypatch, daily_clock,
):
    import sqlite3
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "An old project.", "Okay.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": []}))
        task = service.store.pending_maintenance_task()
        detail = await client.request("memory_continuity_run", task_id=task["id"])
        path = service.store.path
        await service.close()
        with sqlite3.connect(path) as database:
            database.execute("DROP TABLE maintenance_daily_allowance")
            for table, columns in (
                ("maintenance_stages", ("recovery_source", "automatic_last_at")),
                ("maintenance_attempts", ("recovery_source", "recovery_pass", "daily_period")),
            ):
                for column in columns:
                    database.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            database.execute("PRAGMA user_version=13")
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            migrated = await restarted.handle("memory_continuity_run", {"task_id": task["id"]})
            assert migrated["budget"] == detail["budget"]
            assert migrated["model_calls"] == 1 and migrated["attempt_counts"] == {"failed": 1}
            assert migrated["attempts"][0]["recovery_source"] is None
            assert migrated["attempts"][0]["daily_period"] is None
            assert restarted.store.get_maintenance_task(task["id"])["inputs"] == task["inputs"]
            assert restarted.store.migration_backup_path.exists()
            daily_clock.advance(600)
            valid = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
            await automatic(restarted, valid)
            assert len(valid.calls) == 1
            assert restarted.maintenance_status()["evidence"] == {"processed": 1, "unresolved": 0}
            assert restarted.maintenance_status()["daily_allowance"]["used"] == 1
        finally:
            await restarted.close()
