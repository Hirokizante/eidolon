"""Routing saves and obsolete attempt rejection through the real service/store."""
import asyncio
import json

import pytest
import httpx

from eidolon.config import (
    AuxiliaryModelConfig, IdentityConfig, ModelMetadata, load_config, save_config,
)
from eidolon.providers.routing import configured_route_fingerprint
from test_service import make_config, room


async def select(service, **values):
    return await service.handle("save_auxiliary_model", {"auxiliary": values})


@pytest.mark.parametrize("mode", ["inherit", "separate"])
async def test_configured_identity_reaches_initial_and_rebuilt_workers(tmp_path, monkeypatch, mode):
    from test_memory_maintenance import Provider

    config = make_config(tmp_path)
    config.identity = IdentityConfig(user_name="Aster 雪", companion_name="[blue]Vale[/blue]")
    monkeypatch.setattr("test_service.make_config", lambda _: config)
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        assert s.continuity_worker.identity == config.identity
        initial = s.continuity_worker
        await select(s, mode=mode, provider="second", model="synthetic-auxiliary",
                     thinking_mode="disabled")
        assert s.continuity_worker is not initial
        s.store.complete_turn(s.current_session["id"], "Synthetic unfinished project.", "Understood.")
        provider = Provider()
        s.continuity_worker.provider = provider
        assert await s.continuity_worker.run_once(force=True)
        document = json.loads(provider.calls[0][-1]["content"])
        assert document["current_identity"] == config.identity.model_dump()
        assert "identity" not in document["exchanges"][0]


@pytest.mark.parametrize("mode", ["inherit", "separate"])
@pytest.mark.parametrize("retry", [False, True])
async def test_rename_preserves_active_attempt_and_updates_next_request(
    tmp_path, monkeypatch, mode, retry,
):
    from test_memory_maintenance import Provider

    started, release = asyncio.Event(), asyncio.Event()

    class HeldProvider(Provider):
        async def stream(self, messages, **kwargs):
            started.set()
            await release.wait()
            async for event in super().stream(messages, **kwargs):
                yield event

    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        await select(s, mode=mode, provider="second", model="synthetic-auxiliary")
        recorded = {"user_name": "River", "companion_name": "Sol"}
        s.store.complete_turn(s.current_session["id"], "Synthetic project remains open.",
                              "Understood.", metadata={"identity": recorded})
        worker, execution = s.continuity_worker, s._auxiliary_execution
        provider = HeldProvider(result="invalid JSON" if retry else None)
        worker.provider = provider
        original = s.config.identity.model_dump()
        route = s.store.auxiliary_route()
        controls = s.memory_state()["controls"]
        saved_rows = {table: [dict(row) for row in s.store.db.execute(f"SELECT * FROM {table}")]
                      for table in ("generations", "messages", "outbox")}
        await client.request("memory_refresh")
        await asyncio.wait_for(started.wait(), 5)
        running = s._continuity_task
        task = s.store.pending_maintenance_task()
        attempt = s.store.maintenance_status()["latest_attempt"]
        names = {"user_name": "界" * 80, "companion_name": '雪[bold] "{companion_name}" 🧑‍🚀'}
        try:
            for _ in range(2):
                await client.request("save_preferences", preferences=names)
                assert s.continuity_worker is worker and s._auxiliary_execution is execution
                assert s._continuity_task is running and not running.done()
                assert worker.identity.model_dump() == names
                assert s.store.auxiliary_route() == route
                assert s.store.pending_maintenance_task() == task
                assert s.store.maintenance_status()["latest_attempt"] == attempt
                assert s.memory_state()["controls"] == controls
        finally:
            release.set()
            await asyncio.wait_for(running, 5)
        first = json.loads(provider.calls[0][-1]["content"])
        assert first["current_identity"] == original
        assert first["exchanges"][0]["identity"] == recorded
        assert s.store.maintenance_attempts(task["id"])[0]["outcome"] == (
            "failed" if retry else "committed"
        )
        assert {table: [dict(row) for row in s.store.db.execute(f"SELECT * FROM {table}")]
                for table in saved_rows} == saved_rows
        if retry:
            pending = s.store.pending_maintenance_task()
            assert pending["id"] == task["id"] and pending["inputs"] == task["inputs"]
        else:
            assert s.store.pending_maintenance_task() is None
            s.store.complete_turn(s.current_session["id"], "Another synthetic project.", "Understood.")
        provider.result = None
        await client.request("memory_refresh")
        if s._continuity_task is not None:
            await asyncio.wait_for(s._continuity_task, 5)
        second = json.loads(provider.calls[1][-1]["content"])
        assert second["current_identity"] == names
        if retry:
            assert second["exchanges"] == first["exchanges"]
            attempts = s.store.maintenance_attempts(task["id"])
            assert [row["outcome"] for row in attempts] == ["committed", "failed"]
            assert all(row["provenance"] == execution.provenance() for row in attempts)
        assert s.store.pending_maintenance_task() is None


@pytest.mark.parametrize("mode", ["inherit", "separate"])
@pytest.mark.parametrize("context_chars,context_window,expected", [
    (None, None, True), (8_000, None, False), (None, 32_768, False),
])
async def test_auxiliary_input_allowance_and_token_window_are_independent(
    tmp_path, monkeypatch, mode, context_chars, context_window, expected,
):
    from test_memory_maintenance import Provider

    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, mode=mode, provider="second", model="synthetic-auxiliary")
        await select(s, context_chars=context_chars, context_window=context_window)
        text = "Synthetic project detail. " * 12_000
        s.store.complete_turn(s.current_session["id"], text, "Acknowledged.")
        row = s.store.recent_continuity_inputs(limit=1)[0]
        provider = Provider()
        provider.config = s._auxiliary_execution.profile()
        worker = s.continuity_worker
        worker.provider = provider

        assert await worker.run_once(force=True) is expected
        if expected:
            assert len(provider.calls) == 1
            assert json.loads(provider.calls[0][-1]["content"])["exchanges"][0]["user_content"] == text
            assert s.store.continuity_state()["watermark"] == row["exchange_id"]
            assert s.store.pending_maintenance_task() is None
            attempt = s.store.maintenance_status()["latest_attempt"]
            assert attempt["provenance"]["request_options"]["context_chars"] is None
        else:
            assert provider.calls == []
            assert s.store.continuity_state()["watermark"] == 0
            task = s.store.pending_maintenance_task()
            assert task["input_ids"] == [row["exchange_id"]]
            assert task["stages"]["continuity"]["status"] == "failed"


@pytest.mark.parametrize("mode", ["inherit", "separate"])
async def test_clearing_auxiliary_input_allowance_persists_and_reloads(tmp_path, monkeypatch, mode):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, mode=mode, provider="second", model="synthetic-auxiliary", context_chars=32_000)
        assert s.continuity_worker.max_input_chars == 32_000
        revision = s.store.auxiliary_route()["revision"]

        await select(s, context_chars=None)

        assert load_config(s.configuration).auxiliary.context_chars is None
        assert s.continuity_worker.max_input_chars is None
        assert s.auxiliary_status()["context_chars"] is None
        assert s.store.auxiliary_route()["revision"] > revision
        await s._reload_connections()
        assert s.continuity_worker.max_input_chars is None


async def test_completed_legacy_rewrite_cannot_start_another_paid_attempt(tmp_path, monkeypatch):
    from test_continuity_narrative import legacy
    from test_memory_maintenance import Provider

    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        legacy(s.store)
        await s.handle("save_preferences", {"preferences": {"continuity_enabled": True}})
        provider = Provider()
        s.continuity_worker.provider = provider
        assert s._start_continuity()
        await s._continuity_task
        task_id = s.store.maintenance_status()["latest_attempt"]["task_id"]
        calls = s.store.maintenance_status()["model_calls"]
        with pytest.raises(ValueError, match="completed"):
            s.store.start_maintenance_attempt(task_id, s._auxiliary_execution.provenance())
        assert s.store.maintenance_status()["model_calls"] == calls

        for _ in range(3):
            assert s._start_continuity()
            await s._continuity_task
        status = s.memory_state()["continuity"]["maintenance"]
        assert not status["running"]
        assert status["latest_attempt"]["outcome"] == "committed"
        assert status["model_calls"] == calls
        assert len(provider.calls) == 1


async def test_independent_saves_preserve_stale_preferences_and_dialogue(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        engine, memory, mcp = s.engine, s.memory, s.mcp
        stale = s.config.model_dump(mode="json")
        await select(s, mode="separate", provider="second", model="manual-aux")
        assert (s.engine, s.memory, s.mcp) == (engine, memory, mcp)
        assert s.config.selected_provider().model == "first-model"
        revision = s.store.auxiliary_route()["revision"]
        worker = s.continuity_worker
        await s.handle("select_model", {"name": "first", "model": "new-dialogue"})
        assert s.store.auxiliary_route()["revision"] == revision
        assert s.continuity_worker is worker
        await s.handle("save_preferences", {"preferences": {"follow_replies": False}})
        await s.handle("save_settings", {"config": stale})
        saved = load_config(s.configuration)
        assert saved.auxiliary.model == "manual-aux"
        assert saved.auxiliary.provider == "second"


@pytest.mark.parametrize("values", [
    {"mode": "separate"}, {"mode": "separate", "provider": "missing", "model": "manual"},
    {"mode": "separate", "provider": "first", "model": ""},
])
async def test_invalid_save_preserves_last_valid_file(tmp_path, monkeypatch, values):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        before = s.configuration.read_bytes()
        with pytest.raises(ValueError):
            await select(s, **values)
        assert s.configuration.read_bytes() == before


async def test_manual_override_clears_when_binding_changes(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, mode="separate", provider="first", model="aux-a")
        await select(s, context_window=32000)
        assert load_config(s.configuration).auxiliary.context_window == 32000
        await select(s, model="aux-b")
        assert load_config(s.configuration).auxiliary.context_window is None


@pytest.mark.parametrize("method", ["save_auxiliary_model", "save_preferences_with_auxiliary"])
async def test_new_auxiliary_binding_keeps_explicit_context_override(tmp_path, monkeypatch, method):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, mode="separate", provider="first", model="aux-a", context_window=32000)
        params = {"auxiliary": {"provider": "second", "model": "aux-b", "context_window": 32000}}
        if method == "save_preferences_with_auxiliary":
            params["preferences"] = {"follow_replies": False}
        await s.handle(method, params)
        saved = load_config(s.configuration).auxiliary
        assert (saved.provider, saved.model, saved.context_window) == ("second", "aux-b", 32000)
        assert s.continuity_worker.provider.config.context_window_for() == 32000


async def test_combined_preferences_save_merges_latest_configuration_once(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, context_window=32000)
        latest = load_config(s.configuration)
        latest.providers[0].model = "dialogue-changed-elsewhere"
        latest.theme.name = "nord"
        latest.auxiliary.max_output_tokens = 2048
        save_config(latest, s.configuration)
        persisted = []
        persist = s._persist_configuration

        def record_persist(config, path):
            persisted.append(config.model_copy(deep=True))
            persist(config, path)

        monkeypatch.setattr(s, "_persist_configuration", record_persist)
        result = await s.handle("save_preferences_with_auxiliary", {
            "preferences": {"follow_replies": False},
            "auxiliary": {"mode": "separate", "provider": "second", "model": "synthetic/aux"},
        })
        saved = load_config(s.configuration)
        assert len(persisted) == 1
        assert saved.model_dump(mode="json") == result
        assert not saved.preferences.follow_replies
        assert saved.auxiliary.model == "synthetic/aux"
        assert saved.auxiliary.max_output_tokens == 2048
        assert saved.auxiliary.context_window is None
        assert saved.selected_provider().model == "dialogue-changed-elsewhere"
        assert saved.theme.name == "nord"


@pytest.mark.parametrize("preferences,auxiliary", [
    ({"follow_replies": False}, {"mode": "separate", "provider": "missing", "model": "aux"}),
    ({"follow_replies": False}, {"max_output_tokens": 8193}),
    ({"reset_hour": 24}, {"mode": "disabled"}),
])
async def test_invalid_combined_preferences_save_preserves_both_categories(
    tmp_path, monkeypatch, preferences, auxiliary,
):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        before = s.configuration.read_bytes()
        with pytest.raises(ValueError):
            await s.handle("save_preferences_with_auxiliary", {
                "preferences": preferences, "auxiliary": auxiliary,
            })
        assert s.configuration.read_bytes() == before
        assert s.config.preferences.follow_replies
        assert s.config.auxiliary.mode == "inherit"


async def test_disabled_and_dangling_binding_hold_work_without_affecting_recall(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, provider, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic eligible project.", "Acknowledged.")
        controls = s.config.memory.model_dump()
        await select(s, mode="disabled")
        state = await s.handle("memory_refresh", {})
        assert not state["refresh_queued"]
        assert "off" in state["continuity"]["maintenance"]["auxiliary"]["notice"]
        assert s.config.memory.model_dump() == controls and not provider.requests
        saved = load_config(s.configuration)
        saved.auxiliary = AuxiliaryModelConfig(mode="separate", provider="missing", model="aux")
        save_config(saved, s.configuration)
        await s._reload_connections()
        assert s.auxiliary_status()["configuration_error"]
        assert not (await s.handle("memory_refresh", {}))["refresh_queued"]
        assert s.memory_state()["readiness"]["status"] == "not checked"
        assert s.store.continuity_state()["watermark"] == 0


async def test_switch_during_stream_closes_and_retries_original_task(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project remains open.", "Acknowledged.")
        started, stopped = asyncio.Event(), asyncio.Event()
        class SlowProvider:
            async def stream(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                    yield {}
                finally:
                    stopped.set()
        s.continuity_worker.provider = SlowProvider()
        await s.handle("memory_refresh", {})
        await started.wait()
        original = s.store.pending_maintenance_task()
        await select(s, mode="separate", provider="first", model="aux-new",
                     thinking_mode="disabled")
        assert stopped.is_set() and s._continuity_task is None
        assert s.store.pending_maintenance_task()["inputs"] == original["inputs"]
        attempts = s.store.maintenance_attempts(original["id"])
        assert attempts[0]["outcome"] == "interrupted" and attempts[0]["usage"] is None
        class CompleteProvider:
            async def stream(self, messages, **kwargs):
                yield {"type": "token", "text": json.dumps({"operations": [{
                    "op": "no_change",
                }]})}
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                                   "private": "must not be retained"}}
                yield {"type": "finish", "reason": "stop"}
        s.continuity_worker.provider = CompleteProvider()
        assert await s.continuity_worker.run_once(force=True)
        attempts = s.store.maintenance_attempts(original["id"])
        assert len(attempts) == 2 and attempts[0]["outcome"] == "committed"
        assert attempts[0]["provenance"]["model"] == "aux-new"
        assert attempts[0]["provenance"]["request_options"]["thinking_mode"] == "disabled"
        assert attempts[1]["provenance"]["model"] == "first-model"
        assert attempts[0]["usage"] == {"prompt_tokens": 10, "completion_tokens": 2}
        status = s.store.maintenance_status()
        assert status["model_calls"] == 2
        assert status["last_successful_execution"]["id"] == attempts[0]["id"]
        assert not s.store.pending_maintenance_task()


async def test_aba_and_external_file_change_reject_obsolete_result(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic source.", "Acknowledged.")
        state = s.store.continuity_state()
        task = s.store.create_maintenance_task(s.store.continuity_inputs(), expected_revision=0,
                                               control_revision=0)
        execution = s._auxiliary_execution
        attempt = s.store.start_maintenance_attempt(task["id"], execution.provenance())
        await select(s, mode="separate", provider="first", model="other")
        await select(s, mode="inherit", provider=None, model=None)
        assert configured_route_fingerprint(s.config) == execution.route_fingerprint
        assert s.store.auxiliary_route()["revision"] > execution.routing_revision
        with pytest.raises(ValueError):
            await s._commit_auxiliary(execution, attempt, task, state, [
                {"op": "no_change", "expected_revision": 0},
            ])
        execution = s._auxiliary_execution
        attempt = s.store.start_maintenance_attempt(task["id"], execution.provenance())
        saved = load_config(s.configuration)
        saved.auxiliary.mode = "disabled"
        save_config(saved, s.configuration)  # Simulate file persisted before ledger/crash.
        with pytest.raises(ValueError):
            await s._commit_auxiliary(execution, attempt, task, state, [
                {"op": "no_change", "expected_revision": 0},
            ])
        assert s.store.continuity_state()["watermark"] == 0


async def test_unrelated_preferences_and_metadata_do_not_cancel_attempt(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, mode="separate", provider="first", model="aux")
        original = s._auxiliary_execution
        await s.handle("save_preferences", {"preferences": {"follow_replies": False}})
        assert s._auxiliary_execution is original
        saved = load_config(s.configuration)
        saved.providers[0].model_metadata["aux"] = ModelMetadata(
            context_window=32000, source="synthetic", updated_at=1,
        )
        save_config(saved, s.configuration)
        await s._reload_connections()
        assert s.store.auxiliary_route()["revision"] == original.routing_revision


async def test_restart_reconciles_file_before_recovering_attempts(tmp_path, monkeypatch):
    from eidolon.service.runtime import RoomService
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        task = s.store.create_maintenance_task(s.store.continuity_inputs(), expected_revision=0,
                                               control_revision=0)
        execution = s._auxiliary_execution
        attempt = s.store.start_maintenance_attempt(task["id"], execution.provenance())
        saved = load_config(s.configuration)
        saved.auxiliary.mode = "disabled"
        save_config(saved, s.configuration)
        await s.close()
        reopened = RoomService(saved, s.configuration, engine_factory=s.engine_factory)
        try:
            assert reopened.store.auxiliary_route()["revision"] > execution.routing_revision
            assert reopened.store.get_maintenance_attempt(attempt)["outcome"] == "interrupted"
            assert reopened.store.pending_maintenance_task()["inputs"] == task["inputs"]
            assert not reopened._start_continuity(force=True)
        finally:
            await reopened.close()


async def test_attempt_history_is_bounded_and_aggregates_survive_pruning(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        task = s.store.create_maintenance_task(s.store.continuity_inputs(), expected_revision=0,
                                               control_revision=0)
        for index in range(105):
            if index % 6 == 0:
                s.store.prepare_maintenance_recovery(
                    task["id"], call_limit=6, fingerprint="synthetic", retry=index > 0)
            attempt = s.store.start_maintenance_attempt(task["id"], s._auxiliary_execution.provenance())
            s.store.record_maintenance_usage(task["id"], usage={"prompt_tokens": 10}, attempt_id=attempt)
            s.store.record_maintenance_usage(task["id"], usage={"prompt_tokens": 12}, attempt_id=attempt)
            s.store.finish_maintenance_attempt(attempt, "failed")
        assert len(s.store.maintenance_attempts(task["id"])) == 100
        status = s.store.maintenance_status()
        assert status["model_calls"] == 105
        stage = next(row for row in status["stages"] if row["stage"] == "continuity")
        assert stage["usage_totals"] == {"prompt_tokens": 1260}
        assert stage["attempt_counts"] == {"failed": 105}
        assert status["latest_attempt"]["elapsed_seconds"] >= 0


async def test_smaller_binding_holds_immutable_batch_without_dispatch(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic " * 1000, "Acknowledged.")
        task = s.store.create_maintenance_task(s.store.continuity_inputs(), expected_revision=0,
                                               control_revision=0)
        await select(s, mode="separate", provider="first", model="small")
        await select(s, context_window=1000)
        assert not await s.continuity_worker.run_once(force=True)
        pending = s.store.pending_maintenance_task()
        assert pending["inputs"] == task["inputs"] and pending["id"] == task["id"]
        assert s.store.maintenance_status()["model_calls"] == 0
        assert s.store.continuity_state()["watermark"] == 0
        assert "allowance" in pending["stages"]["continuity"]["error"]


async def test_reload_merges_auxiliary_save_during_adapter_close(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        saved = load_config(s.configuration)
        saved.providers[0].model = "changed-dialogue"
        save_config(saved, s.configuration)
        started, release = asyncio.Event(), asyncio.Event()
        original = s.close_components
        async def close(**kwargs):
            started.set()
            await release.wait()
            await original(**kwargs)
        monkeypatch.setattr(s, "close_components", close)
        async def reload():
            async with s.generation_lock:
                await s._reload_connections()
        reload_task = asyncio.create_task(reload())
        await started.wait()
        await select(s, mode="separate", provider="second", model="latest-auxiliary")
        revision = s.store.auxiliary_route()["revision"]
        release.set()
        await reload_task
        assert s._auxiliary_execution.model == "latest-auxiliary"
        assert s.config.selected_provider().model == "changed-dialogue"
        assert s.store.auxiliary_route()["revision"] == revision
        assert s.store.auxiliary_route()["fingerprint"] == configured_route_fingerprint(load_config(s.configuration))


async def test_shared_connection_edit_clears_capacity_without_dialogue_replacement(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        await select(s, mode="separate", provider="second", model="aux")
        await select(s, context_window=32000)
        engine, avatar, mcp = s.engine, s.avatar, s.mcp
        revision = s.store.auxiliary_route()["revision"]
        async def discover(*args):
            return {"models": [], "error": "Synthetic offline discovery."}
        monkeypatch.setattr(s, "_discover_models", discover)
        profile = s.config.providers[1].model_dump()
        profile.update(endpoint="http://localhost:1235/v1", model="accidental-dialogue-edit")
        await s.handle("save_provider", {"provider": profile, "connection_only": True})
        saved = load_config(s.configuration)
        assert saved.providers[1].model == "second-model"
        assert saved.auxiliary.context_window is None
        assert s.store.auxiliary_route()["revision"] > revision
        assert (s.engine, s.avatar, s.mcp) == (engine, avatar, mcp)
        assert s.continuity_worker.provider.config.endpoint == "http://localhost:1235/v1"


async def test_synthetic_test_is_preemptible_and_sends_no_personal_evidence(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        started, closed = asyncio.Event(), asyncio.Event()
        seen = []
        class SyntheticProvider:
            def __init__(self, *args, **kwargs):
                pass
            async def stream(self, messages, **kwargs):
                seen.extend(messages)
                started.set()
                try:
                    await asyncio.Event().wait()
                    yield {}
                finally:
                    closed.set()
        monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider", SyntheticProvider)
        s.store.complete_turn(s.current_session["id"], "Private synthetic marker never sent.", "Acknowledged.")
        testing = asyncio.create_task(s.handle("test_auxiliary_model", {"auxiliary": {"mode": "inherit"}}))
        await started.wait()
        assert not s.generation_lock.locked()
        async with s._foreground_generation():
            assert closed.is_set()
            assert not s._start_continuity(force=True)
        result = await testing
        assert not result["ok"] and "interrupted" in result["notice"]
        assert len(seen) == 2 and "Private synthetic marker" not in json.dumps(seen)
        assert s.store.maintenance_status()["model_calls"] == 0
        assert s.store.continuity_state()["watermark"] == 0


async def test_synthetic_test_can_be_preempted_during_cold_tokenizer_initialization(tmp_path, monkeypatch):
    import threading
    from eidolon.providers.context import TokenCounter

    entered, released, completed = threading.Event(), threading.Event(), threading.Event()

    def cold_counter(model):
        entered.set()
        released.wait(timeout=2)
        completed.set()
        return TokenCounter(model)

    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        class NeverDispatchProvider:
            config = s._auxiliary_execution.profile()

            async def stream(self, *args, **kwargs):
                yield {"type": "finish", "reason": "stop"}

        monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider",
                            lambda *args, **kwargs: NeverDispatchProvider())
        monkeypatch.setattr("eidolon.memory.maintenance.TokenCounter", cold_counter)
        testing = asyncio.create_task(s.handle("test_auxiliary_model", {"auxiliary": {"mode": "inherit"}}))
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait), 3)
            assert not completed.is_set(), "Tokenizer initialization blocked foreground work"
            async with s._foreground_generation():
                result = await testing
            assert not result["ok"] and "interrupted" in result["notice"]
            assert s.store.maintenance_status()["model_calls"] == 0
        finally:
            released.set()
            if not testing.done():
                testing.cancel()
                await asyncio.gather(testing, return_exceptions=True)


@pytest.mark.parametrize("reply,compatible", [
    ('{"operations":[{"op":"upsert","topic":"Creative project","importance":"moderate",'
     '"category":"thread","content":"The user has an unfinished creative project.",'
     '"sources":["s1"]}]}', True),
    ('{"operations":[{"op":"no_change"}]}', True),
    ('OK', False),
    ('{"operations":[{"op":"upsert","topic":"Invented","sources":["s999"]}]}', False),
])
async def test_synthetic_test_validates_real_contract_without_writing(
    tmp_path, monkeypatch, reply, compatible,
):
    from eidolon.providers.openai import OpenAIProvider
    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        s.store.complete_turn(s.current_session["id"], "Private synthetic marker.", "Saved privately.")
        before = s.configuration.read_bytes()
        state = s.store.continuity_state()
        maintenance = s.store.maintenance_status()
        seen = []

        def respond(request):
            seen.append(json.loads(request.content))
            packet = {"choices": [{"delta": {"content": reply}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider",
                                lambda profile, **kwargs: OpenAIProvider(profile, client=http, **kwargs))
            result = await client.request("test_auxiliary_model", auxiliary={
                "mode": "separate", "provider": "second", "model": "synthetic-draft-model",
                "max_output_tokens": 512,
            })
        assert result["connected"] is True
        assert result["contract_compatible"] is compatible and result["ok"] is compatible
        assert result["output_mode"] == "prompt"
        assert result["contract"] == "current-life-ordinary-v1"
        assert len(seen) == 1 and seen[0]["model"] == "synthetic-draft-model"
        assert seen[0]["max_tokens"] == 512
        assert "Private synthetic marker" not in json.dumps(seen)
        document = json.loads(seen[0]["messages"][-1]["content"])
        assert document["contract"] == "current-life-ordinary-v1"
        assert document["exchanges"][0]["source_refs"]["user"] == "s1"
        assert s.configuration.read_bytes() == before
        assert s.store.continuity_state() == state
        assert s.store.maintenance_status() == maintenance


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt"])
async def test_maintenance_constraints_and_effective_provenance_survive_restart(
    tmp_path, monkeypatch, mode,
):
    from eidolon.config import MaintenanceOutputSupport
    from eidolon.providers.openai import OpenAIProvider

    config = make_config(tmp_path)
    profile = config.selected_provider()
    profile.maintenance_output_support = [MaintenanceOutputSupport(
        endpoint=profile.endpoint, model=profile.model,
        json_schema="enforced" if mode == "json_schema" else "unsupported",
        json_object=mode == "json_object",
    )]
    monkeypatch.setattr("test_service.make_config", lambda _: config)
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        proposal = {"operations": [{"op": "upsert", "topic": "Creative project",
                    "importance": "moderate", "category": "thread",
                    "content": "The user has an unfinished creative project.", "sources": ["s1"]}]}
        if mode == "json_schema":
            proposal["operations"][0].update(note_ref=None, replaces_ref=None, decision_reason=None,
                                            event_at=None, follow_up_at=None, promise_source=None)
        packet = {"choices": [{"delta": {"content": json.dumps(proposal)}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n")

    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            s.continuity_worker.provider = OpenAIProvider(s._auxiliary_execution.profile(), client=http)
            s.store.complete_turn(s.current_session["id"], "My creative project is unfinished.",
                                  "We can return to the creative project later.")
            await client.request("memory_refresh")
            if s._continuity_task:
                await asyncio.wait_for(s._continuity_task, 5)
        latest = (await client.request("status"))["maintenance"]["latest_attempt"]
        assert latest["outcome"] == "committed" and latest["output_mode"] == mode
        assert latest["contract"] == "current-life-ordinary-v1"
        assert latest["capability_source"] == "configured_verification"
        assert len(s.store.continuity_state()["items"]) == 1
        assert len(payloads) == 1
        if mode == "prompt":
            assert "response_format" not in payloads[0]
        else:
            assert payloads[0]["response_format"]["type"] == mode
        task_id = latest["task_id"]
    from eidolon.storage import Store
    reopened = Store(config.database_path)
    try:
        assert reopened.maintenance_attempts(task_id)[0]["provenance"]["output_mode"] == mode
    finally:
        reopened.close()


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt"])
@pytest.mark.parametrize("failure", ["refusal", "length", "unfinished", "empty", "rejected_options"])
async def test_synthetic_test_does_not_mistake_http_success_for_contract_compatibility(
    tmp_path, monkeypatch, mode, failure,
):
    from eidolon.config import MaintenanceOutputSupport
    from eidolon.providers.openai import OpenAIProvider

    config = make_config(tmp_path)
    profile = config.selected_provider()
    profile.maintenance_output_support = [MaintenanceOutputSupport(
        endpoint=profile.endpoint, model=profile.model,
        json_schema="enforced" if mode == "json_schema" else "unsupported",
        json_object=mode == "json_object",
    )]
    monkeypatch.setattr("test_service.make_config", lambda _: config)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if failure == "rejected_options":
            return httpx.Response(400, json={"error": {"param": "reasoning_effort",
                                  "code": "unsupported_parameter", "message": "PRIVATE body"}})
        delta = {"content": '{"operations":[{"op":"no_change"}]}'}
        if failure == "refusal":
            delta["refusal"] = "PRIVATE refusal body"
        if failure == "empty":
            delta = {}
        choice = {"delta": delta, "finish_reason": None if failure == "unfinished" else
                  "length" if failure == "length" else "stop"}
        return httpx.Response(200, text="data: " + json.dumps({"choices": [choice]}) + "\n\n")

    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider",
                                lambda profile, **kwargs: OpenAIProvider(profile, client=http, **kwargs))
            result = await client.request("test_auxiliary_model", auxiliary={"mode": "inherit"})
        assert result["connected"] and not result["contract_compatible"] and not result["ok"]
        assert result["output_mode"] == mode
        assert "PRIVATE" not in json.dumps(result)
        assert len(calls) == 1
        assert s.store.maintenance_status()["model_calls"] == 0
        assert not s.store.continuity_state()["items"]


async def test_recall_preferences_do_not_interrupt_unrelated_maintenance(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        started, closed = asyncio.Event(), asyncio.Event()
        class SlowProvider:
            async def stream(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                    yield {}
                finally:
                    closed.set()
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        worker = s.continuity_worker
        worker.provider = SlowProvider()
        await s.handle("memory_refresh", {})
        await started.wait()
        revision = s.store.auxiliary_route()["revision"]
        await s.handle("save_preferences", {"preferences": {"memory_enabled": False}})
        assert not closed.is_set() and s.continuity_worker is worker
        assert s.store.auxiliary_route()["revision"] == revision
        await s._cancel_continuity()


async def test_safe_attempt_reasons_survive_success_and_restart(tmp_path, monkeypatch):
    from eidolon.storage import Store
    from test_memory_maintenance import Provider

    hostile = "PRIVATE prompt response sk-synthetic https://private.invalid/?credential=secret"
    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        s.continuity_worker.provider = Provider(result=hostile)
        await client.request("memory_refresh")
        if s._continuity_task is not None:
            await asyncio.wait_for(s._continuity_task, 5)
        status = (await client.request("status"))["maintenance"]
        assert status["unresolved"]["recovering"] == 1
        task_id = status["latest_attempt"]["task_id"]
        failed = (await client.request("memory_continuity_run", task_id=task_id))["attempts"][0]
        assert failed["reason"] == {
            "version": 1, "code": "validation.output_json",
            "explanation": "The model did not return a valid JSON object.",
        }
        assert failed["usage"] is None
        assert failed["elapsed_seconds"] >= 0
        s.continuity_worker.provider = Provider()
        await client.request("memory_refresh")
        if s._continuity_task is not None:
            await asyncio.wait_for(s._continuity_task, 5)
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert [a["outcome"] for a in detail["attempts"]] == ["committed", "failed"]
        assert detail["attempts"][1]["reason"] == failed["reason"]
        assert detail["attempt_counts"] == {"committed": 1, "failed": 1}
        assert detail["last_successful_at"]
        assert detail["next_action"] == "complete"
        assert detail["next_retry_at"] is None
        assert hostile not in json.dumps(detail)
        assert (await client.request("status"))["maintenance"]["unresolved"]["total"] == 0
        path = s.store.path
    reopened = Store(path)
    try:
        assert reopened.maintenance_attempts(task_id)[1]["reason"] == failed["reason"]
    finally:
        reopened.close()


@pytest.mark.parametrize("status_code,code,condition", [
    (400, "configuration", "configuration_attention"),
    (401, "authentication", "configuration_attention"),
    (403, "authentication", "configuration_attention"),
    (429, "rate_or_credit_limit", "recovering"),
    (503, "unavailable", "recovering"),
])
async def test_hostile_provider_bodies_never_become_maintenance_diagnostics(
    tmp_path, monkeypatch, status_code, code, condition,
):
    import httpx
    from eidolon.providers import OpenAIProvider

    hostile = "PRIVATE prompt response credential https://private.invalid/?key=synthetic"
    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, text=hostile)
        )) as http:
            s.continuity_worker.provider = OpenAIProvider(
                s._auxiliary_execution.profile(), client=http,
            )
            await client.request("memory_refresh")
            if s._continuity_task:
                await asyncio.wait_for(s._continuity_task, 5)
        status = (await client.request("status"))["maintenance"]
        assert status["unresolved"][condition] == 1
        task_id = status["latest_attempt"]["task_id"]
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["attempts"][0]["reason"]["code"] == code
        assert detail["reason"]["code"] == code
        assert detail["next_action"] == ("configure" if condition == "configuration_attention"
                                         else "refresh")
        assert detail["next_retry_at"] is None
        attempts = await client.request("maintenance_attempts", task_id=task_id)
        assert hostile not in json.dumps([status, detail, attempts])
        assert s.store.continuity_state()["watermark"] == 0


@pytest.mark.parametrize("kind", ["exception", "timeout", "output_limit", "validation"])
async def test_attempt_causes_are_typed_and_never_copy_exception_text(tmp_path, monkeypatch, kind):
    from eidolon.memory.continuity import ContinuityValidationError
    from eidolon.providers.openai import ProviderOutputLimitError
    from test_memory_maintenance import Provider

    hostile = "[red]PRIVATE[/red] https://private.invalid/?credential=secret \x1b[2J"
    failures = {
        "exception": (RuntimeError(hostile), "unknown"),
        "timeout": (TimeoutError(hostile), "timeout"),
        "output_limit": (ProviderOutputLimitError(hostile), "validation.output_tokens"),
        "validation": (ContinuityValidationError(hostile), "validation.unsupported"),
    }
    failure, code = failures[kind]
    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        s.continuity_worker.provider = Provider(failure=failure)
        await client.request("memory_refresh")
        if s._continuity_task:
            await asyncio.wait_for(s._continuity_task, 5)
        status = (await client.request("status"))["maintenance"]
        assert status["latest_attempt"]["reason"]["code"] == code
        assert hostile not in json.dumps(status)


async def test_new_success_preserves_an_older_unresolved_condition(tmp_path, monkeypatch):
    from test_memory_maintenance import Provider

    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic first project.", "Acknowledged.")
        s.continuity_worker.provider = Provider()
        await client.request("memory_refresh")
        if s._continuity_task:
            await asyncio.wait_for(s._continuity_task, 5)
        older_id = s.store.maintenance_status()["latest_attempt"]["task_id"]
        # Existing optional stages can remain unresolved after continuity commits.
        s.store.fail_maintenance_stage(older_id, "reflection", "PRIVATE old diagnostic",
                                       reason_code="unavailable")
        s.store.complete_turn(s.current_session["id"], "Synthetic second project.", "Acknowledged.")
        await client.request("memory_refresh")
        if s._continuity_task:
            await asyncio.wait_for(s._continuity_task, 5)
        status = (await client.request("status"))["maintenance"]
        assert status["latest_attempt"]["outcome"] == "committed"
        assert status["latest_attempt"]["task_id"] != older_id
        assert status["unresolved"]["recovering"] == status["unresolved"]["total"] == 1
        assert "PRIVATE" not in json.dumps(status)
        detail = await client.request("memory_continuity_run", task_id=older_id)
        assert detail["unresolved_conditions"][0]["stage"] == "reflection"
        assert detail["unresolved_conditions"][0]["reason"]["code"] == "unavailable"


async def test_v9_history_upgrade_keeps_missing_causes_unknown(tmp_path, monkeypatch):
    from eidolon.storage import Store

    async with room(tmp_path, monkeypatch) as (s, _, _, _):
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        inputs = s.store.continuity_inputs()
        task = s.store.create_maintenance_task(inputs, expected_revision=0, control_revision=0)
        attempt = s.store.start_maintenance_attempt(task["id"], s._auxiliary_execution.provenance())
        s.store.finish_maintenance_attempt(attempt, "failed")
        s.store.fail_maintenance_stage(task["id"], "continuity", "PRIVATE legacy exception")
        path = s.store.path
        # Recreate the v9 diagnostic schema, including a legacy error without a cause.
        with s.store.db:
            s.store.db.execute("ALTER TABLE maintenance_attempts DROP COLUMN reason")
            s.store.db.execute("ALTER TABLE maintenance_stages DROP COLUMN reason")
            s.store.db.execute("ALTER TABLE maintenance_stages DROP COLUMN attempt_counts")
            s.store.db.execute("PRAGMA user_version=9")
    reopened = Store(path)
    try:
        assert reopened.migration_backup_path and ".pre-v11-" in str(reopened.migration_backup_path)
        assert reopened.maintenance_attempts(task["id"])[0]["reason"] is None
        stage = reopened.get_maintenance_stage(task["id"])
        assert stage["reason"] is None
        assert stage["attempt_counts"] == {"failed": 1}
        assert reopened.pending_maintenance_task()["inputs"] == inputs
        assert reopened.continuity_state()["watermark"] == 0
        assert reopened.maintenance_status()["unresolved"]["recovering"] == 1
    finally:
        reopened.close()


async def test_automatic_retry_time_and_configuration_warnings_resolve_independently(
    tmp_path, monkeypatch,
):
    from datetime import datetime, timezone
    from test_memory_maintenance import Provider

    async with room(tmp_path, monkeypatch) as (s, client, _, _):
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        s.store.complete_turn(s.current_session["id"], "Synthetic project.", "Acknowledged.")
        provider = Provider(result="invalid JSON")
        s.continuity_worker.provider = provider
        await client.request("memory_refresh")
        if s._continuity_task:
            await asyncio.wait_for(s._continuity_task, 5)
        status = (await client.request("status"))["maintenance"]
        task_id = status["latest_attempt"]["task_id"]
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["next_action"] == "retry_scheduled"
        remaining = (datetime.fromisoformat(detail["next_retry_at"])
                     - datetime.now(timezone.utc)).total_seconds()
        assert 20 < remaining <= 30  # Existing first-failure backoff.
        assert not await s.continuity_worker.run_once()
        assert len(provider.calls) == 1
        # An externally edited connection adds its own condition to the older failed batch.
        saved = load_config(s.configuration)
        saved.auxiliary = AuxiliaryModelConfig(
            mode="separate", provider="missing", model="synthetic",
        )
        save_config(saved, s.configuration)
        await s._refresh_auxiliary()
        status = (await client.request("status"))["maintenance"]
        assert status["unresolved"]["configuration_attention"] == 1
        assert status["unresolved"]["recovering"] == 1
        detail = await client.request("memory_continuity_run", task_id=task_id)
        assert detail["next_action"] == "configure" and detail["next_retry_at"] is None
        await select(s, mode="inherit")
        status = (await client.request("status"))["maintenance"]
        assert status["unresolved"]["configuration_attention"] == 0
        assert status["unresolved"]["recovering"] == 1
        s.continuity_worker.provider = Provider()
        await client.request("memory_refresh")
        if s._continuity_task:
            await asyncio.wait_for(s._continuity_task, 5)
        assert (await client.request("status"))["maintenance"]["unresolved"]["total"] == 0
