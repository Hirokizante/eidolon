"""Bounded recovery through the service with complete evidence and real SQLite state."""
from datetime import datetime, timedelta, timezone

import pytest

from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        instant = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

        @classmethod
        def advance(cls, seconds=7200):
            cls.instant += timedelta(seconds=seconds)

    monkeypatch.setattr("eidolon.memory.maintenance.datetime", Clock)
    return Clock


async def automatic(service, provider):
    service.continuity_worker.provider = provider
    assert service._start_continuity()
    await service._continuity_task


async def test_default_budget_exhausts_and_manual_retry_keeps_history(tmp_path, monkeypatch, clock):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        service.store.complete_turn(service.current_session["id"], "An unfinished project.", "Okay.")
        provider = CompactProvider(lambda doc: {"operations": []})
        await refresh(service, client, provider)
        for _ in range(8):
            clock.advance()
            await automatic(service, provider)
        assert len(provider.calls) == 6
        status = (await client.request("status"))["maintenance"]
        assert status["unresolved"]["set_aside"] == 1
        stage = status["stages"][0]
        assert stage["budget"] == {"limit": 6, "used": 6, "remaining": 0,
                                   "exhausted": True, "pass": 1}
        assert stage["next_action"] == "retry_required"
        assert stage["next_retry_at"] is None
        task_id = stage["task_id"]
        original = service.store.pending_maintenance_task()["inputs"]
        service.store.complete_turn(service.current_session["id"], "A newer subject.", "Okay.")
        await automatic(service, provider)
        assert len(provider.calls) == 6
        assert service.store.continuity_state()["watermark"] == 0
        assert service.store.pending_maintenance_task()["inputs"] == original

        valid = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await refresh(service, client, valid)
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["status"] == "completed"
        assert detail["model_calls"] == 7 and len(detail["attempts"]) == 7
        assert detail["budget"]["pass"] == 2 and detail["budget"]["remaining"] == 5
        assert detail["attempt_counts"] == {"failed": 6, "committed": 1}
        assert all(a["reason"] for a in detail["attempts"][1:])


@pytest.mark.parametrize("category", ["configuration", "authentication"])
async def test_configuration_pauses_until_relevant_change_without_resetting_budget(
    tmp_path, monkeypatch, clock, category,
):
    from eidolon.providers.openai import ProviderCapabilityError, ProviderError
    from test_auxiliary_service import select

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 3,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")

        def reject(doc):
            if category == "configuration":
                raise ProviderCapabilityError("PRIVATE provider body")
            raise ProviderError("PRIVATE credentials", category="authentication")

        provider = CompactProvider(reject)
        await refresh(service, client, provider)
        status = (await client.request("status"))["maintenance"]
        stage = status["stages"][0]
        assert stage["paused"] is True
        assert stage["next_action"] == "configure" and stage["next_retry_at"] is None
        assert stage["budget"]["remaining"] == 2
        await client.request("save_preferences", preferences={
            "follow_replies": False, "continuity_idle_seconds": 1, "continuity_recovery_calls": 9,
        })
        clock.advance()
        await automatic(service, provider)
        assert len(provider.calls) == 1
        await select(service, mode="separate", provider="second", model="synthetic-auxiliary",
                     max_output_tokens=2048)
        await automatic(service, provider)
        if category == "authentication":
            # Model/output edits cannot fix credentials on the same connection.
            assert len(provider.calls) == 1
            from eidolon.config import load_config, save_config
            saved = load_config(service.configuration)
            saved.providers[1].endpoint = "https://synthetic.invalid/v1"
            save_config(saved, service.configuration)
            await service._refresh_auxiliary()
            await automatic(service, provider)
        assert len(provider.calls) == 2
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["budget"]["limit"] == 3 and stage["budget"]["remaining"] == 1
        assert stage["budget"]["pass"] == 1
        assert "PRIVATE" not in str(stage)


async def test_transient_delay_and_safe_correction_feedback(tmp_path, monkeypatch, clock):
    from eidolon.providers.openai import ProviderError

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")

        def unavailable(doc):
            raise ProviderError("PRIVATE transient body")

        provider = CompactProvider(unavailable)
        await refresh(service, client, provider)
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["next_action"] == "retry_scheduled" and not stage["paused"]
        assert datetime.fromisoformat(stage["next_retry_at"]) == clock.instant + timedelta(seconds=30)
        await automatic(service, provider)
        clock.advance(29)
        await automatic(service, provider)
        assert len(provider.calls) == 1
        clock.advance(1)
        malformed = CompactProvider(lambda doc: {"operations": []})
        await automatic(service, malformed)
        clock.advance()

        def corrected(doc):
            assert doc["previous_rejection"] == "The model returned malformed continuity operations."
            assert "PRIVATE" not in str(doc)
            return {"operations": [{"op": "no_change"}]}

        await automatic(service, CompactProvider(corrected))
        assert service.store.pending_maintenance_task() is None
        assert service.store.maintenance_status()["model_calls"] == 3


@pytest.mark.parametrize("failure", ["invalid", "configuration"])
async def test_restart_preserves_pass_pause_and_lifetime_usage(tmp_path, monkeypatch, clock, failure):
    from eidolon.config import load_config
    from eidolon.providers.openai import ProviderCapabilityError
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 2,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")

        class MeteredProvider(CompactProvider):
            async def stream(self, messages, **kwargs):
                yield {"type": "usage", "usage": {"prompt_tokens": 42, "completion_tokens": 7}}
                async for event in super().stream(messages, **kwargs):
                    yield event

        def fail(doc):
            if failure == "configuration":
                raise ProviderCapabilityError("PRIVATE configuration body")
            return {"operations": []}

        provider = MeteredProvider(fail)
        await refresh(service, client, provider)
        if failure == "invalid":
            clock.advance()
            await automatic(service, provider)
        task_id = service.store.pending_maintenance_task()["id"]
        before = await client.request("memory_continuity_run", task_id=task_id)
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            clock.advance()
            await automatic(restarted, provider)
            after = await restarted.handle("memory_continuity_run", {"task_id": task_id})
            assert after["budget"] == before["budget"]
            assert after["paused"] == before["paused"]
            assert after["usage_totals"] == before["usage_totals"]
            assert after["model_calls"] == (2 if failure == "invalid" else 1)
            assert len(provider.calls) == after["model_calls"]
            restarted.continuity_worker.provider = MeteredProvider(
                lambda doc: {"operations": [{"op": "no_change"}]})
            await restarted.handle("memory_refresh", {})
            await restarted._continuity_task
            final = await restarted.handle("memory_continuity_run", {"task_id": task_id})
            assert final["budget"]["pass"] == 2 and final["budget"]["remaining"] == 1
            assert final["usage_totals"]["prompt_tokens"] == (126 if failure == "invalid" else 84)
        finally:
            await restarted.close()


async def test_missing_credentials_pause_without_consuming_a_call(tmp_path, monkeypatch, clock):
    import httpx
    from eidolon.providers.openai import OpenAIProvider

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        profile = service._auxiliary_execution.profile()
        profile.api_key_ref = "env:SYNTHETIC_MISSING_KEY"
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("Request with missing credential was sent")
        )) as http:
            provider = OpenAIProvider(profile, client=http, credential_resolver=lambda ref: None)
            await refresh(service, client, provider)
        status = (await client.request("status"))["maintenance"]
        assert status["model_calls"] == 0 and status["latest_attempt"] is None
        assert status["stages"][0]["budget"]["remaining"] == 6
        assert status["stages"][0]["paused"]


async def test_foreground_interruption_consumes_last_call_and_preserves_notes(
    tmp_path, monkeypatch, clock,
):
    import asyncio
    from test_service import events

    dispatched = asyncio.Event()

    class HeldProvider(CompactProvider):
        async def stream(self, messages, **kwargs):
            self.calls.append(messages)
            dispatched.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        note = service.store.create_continuity_item("A protected note.", "thread", topic="A note",
                                                     importance="moderate")
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        service.continuity_worker.provider = provider = HeldProvider(None)
        await client.request("memory_refresh")
        await asyncio.wait_for(dispatched.wait(), 5)
        await events(client)
        status = (await client.request("status"))["maintenance"]
        task_id = status["latest_attempt"]["task_id"]
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["attempts"][0]["outcome"] == "interrupted"
        assert detail["budget"]["remaining"] == 0 and detail["next_action"] == "retry_required"
        assert service.store.get_continuity_item(note["id"]) == note
        assert service.store.continuity_state()["watermark"] == 0
        clock.advance()
        await automatic(service, provider)
        assert len(provider.calls) == 1


async def test_predispatch_limit_failure_keeps_configured_budget_across_restart(
    tmp_path, monkeypatch, clock,
):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService
    from test_auxiliary_service import select

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 2,
        })
        await select(service, context_window=4096, max_output_tokens=4096)
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        provider = CompactProvider(lambda doc: pytest.fail("Impossible input dispatched"))
        await refresh(service, client, provider)
        task_id = service.store.pending_maintenance_task()["id"]
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["budget"]["limit"] == 2 and detail["budget"]["remaining"] == 2
        assert detail["paused"] and detail["reason"]["code"] == "validation.input_overhead"
        assert detail["model_calls"] == 0
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            clock.advance()
            await automatic(restarted, provider)
            after = await restarted.handle("memory_continuity_run", {"task_id": task_id})
            assert after["budget"] == detail["budget"]
            assert len(provider.calls) == 0
        finally:
            await restarted.close()


async def test_stale_dispatched_result_uses_the_last_slot(tmp_path, monkeypatch, clock):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")

        def obsolete(doc):
            service.store.reconcile_auxiliary_route("synthetic-obsolete-route")
            return {"operations": [{"op": "no_change"}]}

        provider = CompactProvider(obsolete)
        await refresh(service, client, provider)
        status = (await client.request("status"))["maintenance"]
        assert status["latest_attempt"]["outcome"] == "stale"
        assert status["stages"][0]["budget"]["exhausted"]
        assert service.store.continuity_state()["watermark"] == 0
        clock.advance()
        await automatic(service, provider)
        assert len(provider.calls) == 1


async def test_abandoned_dispatched_call_is_interrupted_and_exhausted_on_restart(tmp_path, monkeypatch):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_recovery_calls": 1,
        })
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        task = service.store.create_maintenance_task(service.store.continuity_inputs(),
                                                       expected_revision=0, control_revision=0)
        service.store.prepare_maintenance_recovery(task["id"], call_limit=1, fingerprint="synthetic")
        attempt = service.store.start_maintenance_attempt(
            task["id"], service._auxiliary_execution.provenance())
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            detail = await restarted.handle("memory_continuity_run", {"task_id": task["id"]})
            assert detail["attempts"][0]["id"] == attempt
            assert detail["attempts"][0]["outcome"] == "interrupted"
            assert detail["budget"]["remaining"] == 0
            assert detail["next_action"] == "retry_required"
            assert restarted.maintenance_status()["unresolved"]["set_aside"] == 1
        finally:
            await restarted.close()


async def test_upgrade_counts_old_calls_without_minting_allowance(tmp_path, monkeypatch):
    from eidolon.config import load_config
    from eidolon.service.runtime import RoomService

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")
        task = service.store.create_maintenance_task(service.store.continuity_inputs(),
                                                       expected_revision=0, control_revision=0)
        service.store.prepare_maintenance_recovery(task["id"], call_limit=100, fingerprint="synthetic")
        for _ in range(8):
            attempt = service.store.start_maintenance_attempt(
                task["id"], service._auxiliary_execution.provenance())
            service.store.record_maintenance_usage(task["id"], usage={"prompt_tokens": 7},
                                                   attempt_id=attempt)
            service.store.finish_maintenance_attempt(attempt, "failed", reason_code="unknown")
        service.store.fail_maintenance_stage(task["id"], "continuity", "Synthetic failure",
                                              reason_code="unknown")
        # Reproduce a v10 database before recovery accounting existed.
        with service.store.db:
            for column in ("recovery_limit", "pass_start_calls", "recovery_pass", "pause_fingerprint"):
                service.store.db.execute(f"ALTER TABLE maintenance_stages DROP COLUMN {column}")
            service.store.db.execute("PRAGMA user_version=10")
        await service.close()
        restarted = RoomService(load_config(service.configuration), service.configuration)
        try:
            provider = CompactProvider(lambda doc: pytest.fail("Exhausted upgraded batch dispatched"))
            await automatic(restarted, provider)
            detail = await restarted.handle("memory_continuity_run", {"task_id": task["id"]})
            assert detail["budget"] == {"limit": 6, "used": 8, "remaining": 0,
                                        "exhausted": True, "pass": 1}
            assert detail["usage_totals"] == {"prompt_tokens": 56}
            assert restarted.store.pending_maintenance_task()["inputs"] == task["inputs"]
            assert restarted.store.migration_backup_path.exists()
        finally:
            await restarted.close()


@pytest.mark.parametrize("value", [0, 101, True, 2.5, "6"])
async def test_recovery_limit_rejects_invalid_settings_without_changing_saved_config(
    tmp_path, monkeypatch, value,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        with pytest.raises(RuntimeError):
            await client.request("save_preferences", preferences={"continuity_recovery_calls": value})
        from eidolon.config import load_config
        assert load_config(service.configuration).memory.continuity_recovery_calls == 6


async def test_unrelated_model_capabilities_cannot_release_configuration_pause(
    tmp_path, monkeypatch, clock,
):
    from eidolon.config import MaintenanceOutputSupport, load_config, save_config
    from eidolon.providers.openai import ProviderCapabilityError

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        service.store.complete_turn(service.current_session["id"], "An open project.", "Okay.")

        def reject(doc):
            raise ProviderCapabilityError("PRIVATE configuration error")

        provider = CompactProvider(reject)
        await refresh(service, client, provider)
        saved = load_config(service.configuration)
        saved.providers[0].maintenance_output_support.append(MaintenanceOutputSupport(
            endpoint=saved.providers[0].endpoint, model="unrelated-model", json_schema="enforced"))
        save_config(saved, service.configuration)
        await service._refresh_auxiliary()
        clock.advance()
        await automatic(service, provider)
        assert len(provider.calls) == 1
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["paused"] and stage["budget"]["remaining"] == 5
        saved.providers[0].maintenance_output_support.append(MaintenanceOutputSupport(
            endpoint=saved.providers[0].endpoint, model=saved.providers[0].model, json_object=True))
        save_config(saved, service.configuration)
        await service._refresh_auxiliary()
        await automatic(service, provider)
        assert len(provider.calls) == 2
        stage = (await client.request("status"))["maintenance"]["stages"][0]
        assert stage["paused"] and stage["budget"]["remaining"] == 4
        assert stage["budget"]["pass"] == 1
