import asyncio
import copy
import json
import os
import tempfile
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import httpx

from eidolon.config import AppConfig, ProviderConfig, load_config, save_config
from eidolon.engine import ConversationEngine
from eidolon.providers import ProviderError
from eidolon.scheduler import Job, load_jobs, save_jobs
from eidolon.service.client import RoomClient
from eidolon.service.runtime import RoomService
from eidolon.providers.metadata import ModelMetadataResolver


SCHEDULE_METADATA_REFRESH = RoomService._schedule_metadata_refresh


@pytest.fixture(autouse=True)
def no_background_metadata_network(monkeypatch):
    monkeypatch.setattr(RoomService, "_schedule_metadata_refresh", lambda *args: None)


class FakeMemory:
    def __init__(self, config, resolver=None, *, recovery=None):
        self.config = config
        self.last_error = None
        self.failure = None
        self.queries = []

    async def validate_bank(self):
        return {"bank_id": "hermes"}

    async def recall(self, query, *, max_chars=8000):
        self.queries.append(query)
        if self.failure:
            raise self.failure
        return "Their shared history remains available."

    async def close(self):
        pass


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.hang = False

    async def stream(self, messages, *, tools, presentation):
        self.requests.append(copy.deepcopy(messages))
        events = self.responses.pop(0) if self.responses else [
            {"type": "token", "text": "Hello, "}, {"type": "token", "text": "Alex."},
            {"type": "finish", "reason": "stop"}]
        for event in events:
            yield event
            if self.hang and event["type"] == "token":
                await asyncio.sleep(30)


def make_config(tmp_path):
    prompts = {}
    for name in ("SYSTEM", "SOUL", "USER", "POST_HISTORY"):
        path = tmp_path / f"{name}.md"
        path.write_text("original " + name)
        prompts[name] = str(path)
    return AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", prompts=prompts,
                     providers=[ProviderConfig(name="first", model="first-model", presentation="plain"),
                                ProviderConfig(name="second", model="second-model", presentation="plain")],
                     provider="first")


@asynccontextmanager
async def room(tmp_path, monkeypatch):
    monkeypatch.setattr("eidolon.service.runtime.HindsightMemory", FakeMemory)
    config = make_config(tmp_path)
    configuration = save_config(config)
    provider = FakeProvider()
    engines = []
    class ObservedEngine(ConversationEngine):
        async def close(self):
            self.was_closed = True
            await super().close()
    def factory(*args):
        engine = ObservedEngine(*args, provider=provider)
        engine.was_closed = False
        engines.append(engine)
        return engine
    service = RoomService(config, configuration, engine_factory=factory)
    async def no_metadata(*args, **kwargs):
        return {}
    monkeypatch.setattr(service.model_metadata, "resolve", no_metadata)
    with tempfile.TemporaryDirectory(prefix="eidolon-test-", dir="/tmp") as socket_dir:
        socket = Path(socket_dir) / "room.sock"
        server = await asyncio.start_unix_server(service.connection, path=str(socket))
        try:
            async with server:
                yield service, RoomClient(socket), provider, engines
        finally:
            for connection in list(service._connections):
                connection.cancel()
            await asyncio.gather(*list(service._connections), return_exceptions=True)
            await service.close()


async def events(client, **params):
    return [item async for item in client.stream("generate", text="Hi Eidolon", **params)]


async def test_secret_cache_invalidation_over_socket_preserves_other_credentials(tmp_path, monkeypatch):
    from eidolon.config import resolve_secret

    monkeypatch.setattr("eidolon.config._keychain_values", {})
    values = {"one": "first-test-value", "two": "second-test-value"}
    reads = []

    def read(service, name):
        reads.append(name)
        return values[name]

    monkeypatch.setattr("keyring.get_password", read)
    async with room(tmp_path, monkeypatch) as (_, client, _, _):
        assert resolve_secret("keyring:one") == "first-test-value"
        assert resolve_secret("keyring:two") == "second-test-value"
        values["one"] = "updated-test-value"
        result = await client.request("clear_secret_cache", reference="keyring:one")
        assert result == {"cleared": True}
        assert resolve_secret("keyring:one") == "updated-test-value"
        assert resolve_secret("keyring:two") == "second-test-value"
        assert reads == ["one", "two", "one"]
        with pytest.raises(RuntimeError, match="references"):
            await client.request("clear_secret_cache", reference="invalid")


async def test_service_no_longer_exposes_note_browsing_or_unread_status(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        assert "unread_notes" not in await client.request("status")
        overview = await client.request("overview")
        assert "notes" not in overview and "drawer_root" not in overview["config"]
        with pytest.raises(RuntimeError, match="Unknown service request"):
            await client.request("read_note", path="kept.md")
        assert not hasattr(service, "seen_notes")


async def test_saved_required_tools_do_not_block_service_start_and_jobs_enforce_them(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        job = Job(id="daily", name="Daily", prompt="Write a note", permitted_tools=["read_file"],
                  required_tools=["read_file"])
        path = tmp_path / "jobs.json"
        save_jobs(path, [job])
        changed = service.config.model_dump(mode="json")
        changed["provider"] = "second"
        changed["jobs_file"] = str(path)
        await client.request("save_settings", config=changed)
        assert service.scheduler.jobs[0].required_tools == ["read_file"]
        assert (await client.request("status"))["busy"] is False
        assert (await client.request("overview"))["jobs"][0]["required_tools"] == ["read_file"]
        service.store.create_session("required-job", kind="job")
        run = {"id": "required-run", "session_id": "required-job"}
        before_files = set(tmp_path.rglob("*.md"))
        with pytest.raises(RuntimeError, match="Required tools"):
            await service._run_job(job, run)
        assert not service.store.list_messages("required-job")
        assert load_jobs(service.config.jobs_file)[0].required_tools == ["read_file"]
        source = tmp_path / "source.txt"
        source.write_text("Source for the scheduled task.")
        service.tools.files.roots = (tmp_path.resolve(),)
        provider.responses = [
            [{"type": "tool_call", "call": {"id": "read-one", "function": {
                "name": "read_file", "arguments": json.dumps({"path": str(source)}),
            }}}, {"type": "finish", "reason": "tool_calls"}],
            [{"type": "token", "text": "A complete note."}, {"type": "finish", "reason": "stop"}],
        ]
        assert await service._run_job(job, run) == "A complete note."
        assert service.store.list_messages("required-job")[-1]["content"] == "A complete note."
        assert service.store.list_messages("required-job")[0]["content"] == job.prompt
        assert set(tmp_path.rglob("*.md")) == before_files


async def test_job_crud_configures_file_and_preserves_other_jobs_and_history(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        job = Job(id="first", name="First", prompt="Write a note", enabled=True, agent_mode=True)
        assert not service.config.jobs_file
        await client.request("save_job", job=job.model_dump(), create=True)
        assert service.config.jobs_file == service.config.state_dir / "jobs.json"
        assert load_config(service.configuration).jobs_file == service.config.jobs_file
        assert load_jobs(service.config.jobs_file) == [job]
        assert service.scheduler.jobs == [job]
        second = Job(id="second", name="Second", prompt="Another note")
        await client.request("save_job", job=second.model_dump(), create=True)
        updated = job.model_copy(update={"prompt": "Updated task"})
        await client.request("save_job", job=updated.model_dump(), expected_job=job.model_dump())
        assert (await client.request("jobs"))["jobs"] == [updated.model_dump(), second.model_dump()]
        with pytest.raises(RuntimeError, match="changed"):
            await client.request("delete_job", id=job.id, expected_job=job.model_dump())
        with pytest.raises(RuntimeError, match="already exists"):
            await client.request("save_job", job=second.model_dump(), create=True)
        toggled = (await client.request("toggle_job", id=job.id, enabled=False,
                                       expected_job=updated.model_dump()))["job"]
        assert not toggled["enabled"] and toggled["agent_mode"]
        assert service.scheduler.jobs[0].model_dump() == toggled
        service.scheduler.plan(service.scheduler.jobs[0],
                               datetime.fromisoformat("2026-10-13T22:00:00+08:00"))
        runs = service.store.list_job_runs()
        await client.request("delete_job", id=job.id, expected_job=toggled)
        assert load_jobs(service.config.jobs_file) == [second]
        assert service.store.list_job_runs() == runs
        with pytest.raises(RuntimeError, match="removed"):
            await client.request("save_job", job=updated.model_dump())


async def test_job_mode_validation_does_not_prevent_pausing(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        config = load_config(service.configuration)
        config.selected_provider().tools = False
        save_config(config, service.configuration)
        agent = Job(id="agent", name="Agent", prompt="Task", agent_mode=True)
        await client.request("save_job", job=agent.model_dump(), create=True)
        with pytest.raises(RuntimeError, match="tool calling"):
            await client.request("toggle_job", id=agent.id, enabled=True)
        assert not load_jobs(service.config.jobs_file)[0].enabled
        # A file edited outside the app may refer to a provider that no longer exists.
        broken = agent.model_copy(update={"provider": "removed", "enabled": True})
        save_jobs(service.config.jobs_file, [broken])
        result = await client.request("toggle_job", id=broken.id, enabled=False)
        assert not result["job"]["enabled"]
        assert load_jobs(service.config.jobs_file)[0].provider == "removed"
        with pytest.raises(RuntimeError, match="configured provider"):
            await client.request("toggle_job", id=broken.id, enabled=True)


async def test_skill_crud_and_switches_over_socket_do_not_replace_engine(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        assert (await client.request("skills"))["skills"] == []
        saved = await client.request(
            "save_skill", name="planning", description="Use when planning.",
            instructions="Ask about priorities.", enabled=True, create=True,
        )
        assert saved["enabled"]
        assert (await client.request("get_skill", name="planning"))["hash"] == saved["hash"]
        assert (await client.request("skills"))["skills"][0]["name"] == "planning"
        await client.request("toggle_skill", name="planning", enabled=False)
        await events(client)
        assert "Use when planning." not in json.dumps(provider.requests[-1])
        await client.request("toggle_skill", name="planning", enabled=True)
        edited = await client.request(
            "save_skill", name="planning", description="Use when making a plan.",
            instructions="Start with a priority.", enabled=True, expected_hash=saved["hash"],
        )
        assert edited["hash"] != saved["hash"]
        await events(client)
        assert "Use when making a plan." in json.dumps(provider.requests[-1])
        assert len(engines) == 1 and not engines[0].was_closed
        assert service.tools.skills.get("planning")["enabled"]


async def test_usage_metrics_stream_and_status_are_scoped_to_room_and_latest_tool_round(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        first_usage = {"prompt_tokens": 100, "completion_tokens": 20,
                       "prompt_tokens_details": {"cached_tokens": 50}}
        provider.responses = [[
            {"type": "usage", "usage": first_usage, "duration": 2},
            {"type": "tool_call", "call": {"id": "call1", "function": {
                "name": "unknown", "arguments": "{}",
            }}},
            {"type": "finish", "reason": "tool_calls"},
        ], [
            {"type": "token", "text": "All done"},
            {"type": "usage", "usage": {"prompt_tokens": 5000, "completion_tokens": 100,
              "prompt_tokens_details": {"cached_tokens": 4000}}, "duration": 2},
            {"type": "finish", "reason": "stop"},
        ]]
        streamed = await events(client)
        assert streamed[-1]["type"] == "completed"
        metrics = [event["metrics"] for event in streamed if event["type"] == "metrics"]
        assert metrics[0]["context_tokens"] > 0 and metrics[0]["context_estimated"]
        assert metrics[1]["context_tokens"] == 120 and metrics[1]["cache_hit_rate"] == 0.5
        assert metrics[2]["tokens_per_second"] is None
        assert metrics[-1] == {
            **metrics[2],
            "provider": "first", "model": "first-model", "reasoning_effort": None,
            "context_tokens": 5100, "cache_hit_rate": 0.8, "tokens_per_second": 50,
            "context_estimated": False,
        }
        assert (await client.request("status"))["metrics"] == metrics[-1]
        assert (await client.request("status", session_id="other-room"))["metrics"] == {}
        await client.request("save_preferences", preferences={"status_components": ["speed"]})
        assert (await client.request("status"))["metrics"] == metrics[-1]
        provider.responses = [[{"type": "token", "text": "No measurements"},
                               {"type": "finish", "reason": "stop"}]]
        await events(client)
        assert (await client.request("status"))["metrics"]["cache_hit_rate"] is None


async def test_request_inspection_and_export_over_socket_preserve_large_raw_body(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        session = service.current_session["id"]
        assert (await client.request("latest_model_request"))["request"] is None
        assert (await client.request("model_requests"))["requests"] == []
        assert (await client.request("model_request_metrics"))["requests"] == []
        service.store.record_request_metrics(role="dialogue", session_id=session,
            provider="test", model="test", outcome="completed",
            metrics={"input_tokens": 100, "cache_read_tokens": 80, "duration_seconds": 1.5})
        measured = (await client.request("model_request_metrics", session_id=session))["requests"]
        assert len(measured) == 1 and measured[0]["metrics"]["cache_read_tokens"] == 80
        raw = json.dumps({"model": "test", "messages": [{"role": "user", "content": "🌸" * 200_000}]})
        assert len(raw) > 2_000_000
        capture = service.store.record_model_request(
            session, generation_id="failed-turn", attempt_id="attempt1", round_index=0,
            provider="test", model="test", user_message="Hi", method="POST",
            endpoint="http://localhost/v1/chat/completions", body=raw,
        )
        past = service.store.create_session("past", kind="chat", title="Past")
        assert (await client.request("model_requests", session_id=past["id"]))["requests"] == []
        listing = await client.request("model_requests", session_id=session)
        assert listing["requests"][0]["id"] == capture["id"]
        assert "body" not in listing["requests"][0]
        saved = await client.request("model_request", request_id=capture["id"])
        assert saved["body"] == raw
        assert (await client.request("latest_model_request"))["request"] == saved
        exported = await client.request("export_model_request", request_id=capture["id"])
        target = Path(exported["path"])
        assert target.read_text() == raw
        assert target.is_relative_to(service.config.state_dir.resolve() / "diagnostics" / "requests")
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.name == "latest.json"
        replacement = service.store.record_model_request(
            past["id"], generation_id="next-turn", attempt_id="attempt2", round_index=0,
            provider="test", model="test", user_message="Next", method="POST",
            endpoint="http://localhost/v1/chat/completions", body='{"messages":[]}',
        )
        assert (await client.request("latest_model_request"))["request"] == replacement
        assert (await client.request("model_requests", session_id=session))["requests"] == []
        with pytest.raises(RuntimeError, match="does not exist"):
            await client.request("model_request", request_id=capture["id"])
        next_export = await client.request("export_model_request", request_id=replacement["id"])
        assert Path(next_export["path"]) == target
        assert target.read_text() == replacement["body"]
        with pytest.raises(RuntimeError, match="does not exist"):
            await client.request("model_request", request_id=-1)
        assert not provider.requests and not service.store.list_messages(session)
        assert not service.store.pending_outbox()


async def test_socket_recall_retry_bypass_prompt_reload_and_durable_outbox(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        session = service.current_session["id"]
        service.memory.failure = OSError("Memory is offline")
        first = await events(client, session_id=session)
        assert first[-1]["type"] == "recall_required"
        assert not provider.requests and not service.store.pending_outbox()
        retry = await events(client, session_id=session)
        assert retry[-1]["type"] == "recall_required"
        bypass = await events(client, session_id=session, bypass_recall=True)
        assert bypass[-1]["type"] == "completed"
        assert bypass[-1]["metadata"]["recall_bypassed"]
        assert len(service.store.pending_outbox()) == 1
        await client.request("save_prompt", name="SOUL", text="edited SOUL")
        preview = await client.request("preview", session_id=session, text="Next")
        assert preview["messages"][1]["content"] == "edited SOUL"
        service.memory.failure = None
        second = await events(client, session_id=session)
        assert second[-1]["type"] == "completed"
        assert provider.requests[-1][1]["content"] == "edited SOUL"
        assert len(service.store.pending_outbox()) == 2
        assert "edited SOUL" not in json.dumps(service.store.pending_outbox())
        history = await client.request("history", session_id=session)
        assert [m["content"] for m in history["messages"]] == ["Hi Eidolon", "Hello, Alex."] * 2
        status = await client.request("status")
        assert status["outbox"]["pending"] == 2
        assert not status["busy"]
    assert all(engine.was_closed for engine in engines)


async def test_all_information_tool_round_text_matches_stream_history_and_memory(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        call = {"id": "recall-specific", "type": "function", "function": {
            "name": "memory_recall", "arguments": '{"query":"garden"}'}}
        provider.responses = [
            [{"type": "token", "text": "Let me remember. "}, {"type": "tool_call", "call": call},
             {"type": "finish", "reason": "tool_calls"}],
            [{"type": "token", "text": "Our garden is here."}, {"type": "finish", "reason": "stop"}]]
        result = await events(client)
        visible = "".join(event["text"] for event in result if event["type"] == "token")
        assert result[-1]["type"] == "completed", result
        assert visible == result[-1]["text"] == "Let me remember. Our garden is here."
        assert service.store.list_messages(service.current_session["id"])[-1]["content"] == visible
        assert visible in service.store.pending_outbox()[0]["payload"]["content"]
        assert len(provider.requests) == 2
        second = provider.requests[-1]
        assert second[-1]["content"] == "original POST_HISTORY"
        assert second[-2]["role"] == "tool"


async def test_socket_cancel_external_connection_task_never_retains_partial(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        provider.hang = True
        received = asyncio.Event()
        async def consume():
            result = []
            async for item in client.stream("generate", text="Hi"):
                result.append(item)
                if item["type"] == "token":
                    received.set()
            return result
        task = asyncio.create_task(consume())
        await asyncio.wait_for(received.wait(), 1)
        cancellation = await client.request("cancel")
        assert cancellation["cancelled"]
        result = await asyncio.wait_for(task, 1)
        assert result[-1]["type"] == "error"
        assert result[-1]["code"] == "cancelled"
        assert not service.store.pending_outbox()
        assert not service.store.list_messages(service.current_session["id"])
        assert not (await client.request("status"))["busy"]


async def test_first_generation_reconciles_rollover_without_maintenance_tick(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        previous = service.current_session["id"]
        next_session = service.store.create_session("day:tomorrow", kind="chat", title="Tomorrow")
        monkeypatch.setattr(service.store, "session_for", lambda **kwargs: next_session)
        result = await events(client)
        assert result[-1]["type"] == "completed"
        assert service.current_session["id"] == "day:tomorrow"
        assert not service.store.list_messages(previous)
        assert len(service.store.list_messages("day:tomorrow")) == 2
        past = await events(client, session_id=previous)
        assert past[-1]["type"] == "error" and "Resume past" in past[-1]["message"]
        resumed = await events(client, session_id=previous, resume=True)
        assert resumed[-1]["type"] == "completed"


async def test_configuration_snapshot_refresh_and_exclusive_ownership_before_recovery(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        old_engine = service.engine
        updated = service.config.model_copy(deep=True)
        updated.provider = "second"
        save_config(updated, service.configuration)
        result = await events(client)
        assert result[-1]["type"] == "completed"
        assert result[-1]["metadata"]["provider"] == "second"
        assert old_engine.was_closed
        assert len(provider.requests) == 1
        monkeypatch.setattr("eidolon.service.runtime.Store", lambda path: pytest.fail("Second service opened the database before ownership"))
        with pytest.raises(RuntimeError, match="already owns"):
            RoomService(updated, service.configuration)
    assert all(engine.was_closed for engine in engines)


async def test_service_run_socket_shutdown_and_same_day_restart_do_not_generate(tmp_path, monkeypatch):
    monkeypatch.setattr("eidolon.service.runtime.HindsightMemory", FakeMemory)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    config = make_config(tmp_path)
    configuration = save_config(config)
    provider = FakeProvider()
    def factory(*args):
        return ConversationEngine(*args, provider=provider)
    service = RoomService(config, configuration, engine_factory=factory)
    session_id = service.current_session["id"]
    runner = asyncio.create_task(service.run())
    try:
        for _ in range(100):
            if config.socket_path.exists():
                break
            await asyncio.sleep(0.01)
        client = RoomClient(config.socket_path)
        status = await client.request("status")
        assert status["session"]["id"] == session_id
        assert not provider.requests
        await client.request("shutdown")
        await asyncio.wait_for(runner, 2)
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    assert not config.socket_path.exists()
    restarted = RoomService(config, configuration, engine_factory=factory)
    try:
        assert restarted.current_session["id"] == session_id
        assert not provider.requests
    finally:
        await restarted.close()


async def test_model_inventory_is_cached_privately_without_restarting_components(tmp_path, monkeypatch):
    async def inventory(adapter, **kwargs):
        return ["a-model", "z-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with room(tmp_path, monkeypatch) as (service, client, _, engines):
        old_engine = service.engine
        result = await client.request("list_models", name="first")
        assert result == {"models": ["a-model", "z-model"], "error": None}
        assert load_config(service.configuration).selected_provider().models == result["models"]
        assert (await client.request("overview"))["config"]["providers"][0]["models"] == result["models"]
        assert service.configuration.stat().st_mode & 0o777 == 0o600
        assert service.engine is old_engine and len(engines) == 1
        assert (await events(client))[-1]["type"] == "completed"
        assert service.engine is old_engine and not old_engine.was_closed


async def test_discovered_per_model_windows_persist_switch_and_do_not_restart_components(
    tmp_path, monkeypatch,
):
    async def inventory(adapter, **kwargs):
        adapter.model_inventory = [{"id": "first-model", "context_length": 32768},
                                   {"id": "second-model", "context_length": 131072}]
        return ["first-model", "second-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as http:
        async with room(tmp_path, monkeypatch) as (service, client, _, engines):
            service.model_metadata = ModelMetadataResolver(tmp_path / "catalog.json", client=http)
            original = service.engine
            result = await client.request("list_models", name="first")
            assert result["model_metadata"]["first-model"]["context_window"] == 32768
            saved = load_config(service.configuration).selected_provider()
            assert saved.context_window is None and saved.context_window_for() == 32768
            assert saved.context_window_for("second-model") == 131072
            assert (await client.request("status"))["model_context"]["context_window"] == 32768
            assert service.engine is original and len(engines) == 1
            selected = await client.request("select_model", name="first", model="second-model",
                                            reasoning_effort=None)
            assert selected["providers"][0]["model_metadata"]["second-model"]["context_window"] == 131072
            assert (await client.request("status"))["model_context"]["context_window"] == 131072


async def test_background_context_discovery_is_deduplicated_responsive_and_preserves_live_turn(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def discover(profile):
            calls.append(profile.model)
            started.set()
            await release.wait()
            return {"models": [profile.model], "error": None, "model_metadata": {
                profile.model: {"context_window": 32768, "source": "test provider", "updated_at": 1},
            }}
        monkeypatch.setattr(service, "_discover_models", discover)
        original = service.engine
        async with service.generation_lock:
            for _ in range(3):
                SCHEDULE_METADATA_REFRESH(service)
            await started.wait()
            status = await asyncio.wait_for(client.request("status"), .5)
            assert status["busy"] and status["model_context"]["context_window"] is None
            assert calls == ["first-model"]
            pending = list(service._integration_tasks)
            release.set()
            await asyncio.gather(*pending)
            assert service.config.selected_provider().context_window_for() == 32768
            assert service.continuity_worker.provider.config.context_window_for() == 32768
            assert service.engine is original and not original.was_closed
            SCHEDULE_METADATA_REFRESH(service)
            assert not service._metadata_pending
        assert load_config(service.configuration).selected_provider().context_window_for() == 32768


async def test_late_context_metadata_does_not_overwrite_a_changed_endpoint(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        profile = service.config.selected_provider().model_copy(deep=True)
        async def discover(profile):
            updated = load_config(service.configuration)
            updated.selected_provider().endpoint = "https://new.example/v1"
            save_config(updated, service.configuration)
            return {"model_metadata": {profile.model: {
                "context_window": 32768, "source": "old endpoint", "updated_at": 1,
            }}}
        monkeypatch.setattr(service, "_discover_models", discover)
        await service.refresh_model_metadata(profile)
        saved = load_config(service.configuration).selected_provider()
        assert saved.endpoint == "https://new.example/v1" and not saved.model_metadata


async def test_provider_context_cache_is_cleared_for_new_credentials_or_endpoint(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        candidate = load_config(service.configuration)
        candidate.selected_provider().model_metadata = {"first-model": {
            "context_window": 32768, "source": "previous account", "updated_at": 1,
        }}
        unchanged, _ = service._provider_form(candidate, {"provider": {"name": "first"}})
        assert unchanged.context_window_for() == 32768
        for params in [
            {"provider": {"name": "first"}, "api_key": "new-test-key"},
            {"provider": {"name": "first"}, "api_key": "env:NEW_ACCOUNT"},
            {"provider": {"name": "first", "endpoint": "https://new.example/v1"}},
        ]:
            changed, _ = service._provider_form(candidate, params)
            assert changed.model_metadata == {}
        assert candidate.selected_provider().context_window_for() == 32768


async def test_discovery_failure_falls_back_to_cached_and_manual_models(tmp_path, monkeypatch):
    async def unavailable(adapter, **kwargs):
        raise ProviderError("Model discovery timed out; retry or enter a model ID manually.")
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", unavailable)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        candidate = load_config(service.configuration)
        candidate.selected_provider().models = ["cached-model", "first-model"]
        save_config(candidate, service.configuration)
        result = await client.request("list_models", name="first")
        assert result["models"] == ["cached-model", "first-model"]
        assert "timed out" in result["error"]
        saved = await client.request("save_provider", provider={"name": "offline", "model": "manual-model"})
        assert saved["models"] == ["manual-model"] and "timed out" in saved["warning"]
        assert saved["config"]["provider"] == "first"
        with pytest.raises(RuntimeError, match="Enter a model ID"):
            await client.request("save_provider", provider={"name": "unconfigured", "model": ""})
        assert "unconfigured" not in {item.name for item in load_config(service.configuration).providers}


async def test_connect_discovers_unsaved_form_then_stores_literal_key_only_in_keychain(tmp_path, monkeypatch):
    discoveries, secrets = [], []
    async def inventory(adapter, *, api_key=None):
        discoveries.append((adapter.config.model_dump(), api_key))
        return ["a-model", "z-model"]
    def keychain(name, value):
        secrets.append((name, value))
        return "keyring:" + name
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    monkeypatch.setattr("eidolon.service.runtime.store_secret", keychain)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        before = service.configuration.read_bytes()
        params = {"provider": {"name": "custom", "display_name": "My endpoint",
                               "endpoint": "https://example.org/v1", "model": ""},
                  "api_key": "literal-private-key"}
        result = await client.request("discover_models", **params)
        assert result == {"models": ["a-model", "z-model"], "error": None}
        assert service.configuration.read_bytes() == before and not secrets
        first = await client.request("save_provider", **params)
        assert first["warning"] is None
        saved = next(item for item in first["config"]["providers"] if item["name"] == "custom")
        assert saved["model"] == "a-model" and saved["display_name"] == "My endpoint"
        assert saved["api_key_ref"] == "keyring:" + secrets[0][0]
        second = await client.request("save_provider", **params)
        assert len(secrets) == 2 and secrets[0][0] != secrets[1][0]
        assert all(value == "literal-private-key" for _, value in secrets)
        assert all(key == "literal-private-key" for _, key in discoveries)
        assert "literal-private-key" not in json.dumps(first) + json.dumps(second)
        assert "literal-private-key" not in service.configuration.read_text()


async def test_connect_preserves_advanced_fields_and_reference_credentials(tmp_path, monkeypatch):
    discoveries = []
    async def inventory(adapter, *, api_key=None):
        discoveries.append((adapter.config, api_key))
        return ["first-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    monkeypatch.setattr("eidolon.service.runtime.store_secret", lambda *args: pytest.fail("Reference was stored as a literal"))
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        candidate = load_config(service.configuration)
        profile = candidate.selected_provider()
        profile.api_key_ref = "env:OLD_KEY"
        profile.tools = False
        profile.post_history = "user"
        profile.context_window = 90000
        profile.parameters = {"temperature": 0.4, "reasoning_effort": "low"}
        profile.headers = {"User-Agent": "custom"}
        save_config(candidate, service.configuration)
        saved = await client.request("save_provider", provider={"name": "first", "display_name": "Friendly"}, api_key="")
        restored = next(item for item in saved["config"]["providers"] if item["name"] == "first")
        assert restored["api_key_ref"] == "env:OLD_KEY"
        assert not restored["tools"] and restored["post_history"] == "user"
        assert restored["context_window"] == 90000 and restored["headers"] == {"User-Agent": "custom"}
        assert restored["parameters"] == {"temperature": 0.4, "reasoning_effort": "low"}
        assert discoveries[-1][0].api_key_ref == "env:OLD_KEY" and discoveries[-1][1] is None
        saved = await client.request("save_provider", provider={"name": "first"}, api_key="env:NEW_KEY")
        assert saved["config"]["providers"][0]["api_key_ref"] == "env:NEW_KEY"


async def test_connect_validates_before_keychain_and_sanitizes_keychain_failure(tmp_path, monkeypatch):
    calls = []
    async def inventory(adapter, **kwargs):
        calls.append(adapter.config)
        return ["first-model"]
    def broken_keychain(*args):
        raise RuntimeError("cannot store literal-private-key")
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    monkeypatch.setattr("eidolon.service.runtime.store_secret", broken_keychain)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        before = service.configuration.read_bytes()
        with pytest.raises(RuntimeError) as error:
            await client.request("save_provider", provider={"name": "first", "endpoint": "file:///bad"}, api_key="literal-private-key")
        assert "literal-private-key" not in str(error.value) and not calls
        with pytest.raises(RuntimeError, match="Could not store the API key in Keychain") as error:
            await client.request("save_provider", provider={"name": "first"}, api_key="literal-private-key")
        assert "literal-private-key" not in str(error.value)
        assert service.configuration.read_bytes() == before


async def test_changed_endpoint_requires_key_and_secret_header_reentry(tmp_path, monkeypatch):
    calls = []
    async def inventory(adapter, **kwargs):
        calls.append(adapter.config)
        return ["first-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        candidate = load_config(service.configuration)
        candidate.selected_provider().api_key_ref = "env:OLD_KEY"
        candidate.selected_provider().headers = {"X-API-Key": "env:HEADER_KEY"}
        save_config(candidate, service.configuration)
        changed = {"name": "first", "endpoint": "https://new.example/v1"}
        with pytest.raises(RuntimeError, match="Reenter the API key"):
            await client.request("discover_models", provider=changed, api_key="")
        with pytest.raises(RuntimeError, match="Clear or reenter secret header"):
            await client.request("save_provider", provider=changed, api_key="env:OLD_KEY")
        assert not calls
        result = await client.request("save_provider", provider=changed, api_key="env:OLD_KEY", confirm_header_credentials=True)
        assert result["config"]["providers"][0]["endpoint"] == changed["endpoint"]
        assert calls[0].api_key_ref == "env:OLD_KEY"


async def test_busy_model_switch_is_persisted_for_next_turn_without_interrupting_reply(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        async def stream(messages, *, tools, presentation):
            yield {"type": "token", "text": "Hello"}
            started.set()
            await release.wait()
            yield {"type": "finish", "reason": "stop"}
        provider.stream = stream
        old_engine = service.engine
        active = asyncio.create_task(events(client))
        await asyncio.wait_for(started.wait(), 1)
        changed = await client.request("select_model", name="second", model="new-model", reasoning_effort="high")
        assert changed["provider"] == "second"
        assert changed["providers"][1]["parameters"]["reasoning_effort"] == "high"
        assert service.config.provider == "first" and service.engine is old_engine
        assert (await client.request("overview"))["config"] == changed
        assert not old_engine.was_closed and (await client.request("status"))["busy"]
        release.set()
        first = await asyncio.wait_for(active, 1)
        assert first[-1]["type"] == "completed" and first[-1]["metadata"]["provider"] == "first"
        second = await events(client)
        assert second[-1]["metadata"]["provider"] == "second" and old_engine.was_closed
        assert len(engines) == 2
        assert (await client.request("status"))["provider"] == "second · new-model · high"


@pytest.mark.parametrize("field", [None, "max_tokens", "max_completion_tokens", "max_output_tokens"])
@pytest.mark.parametrize("context_window", [None, 262144])
async def test_model_selection_persists_token_settings_and_request_parameter(
    tmp_path, monkeypatch, field, context_window,
):
    from eidolon.config import ModelMetadata
    from eidolon.providers.context import ContextBudget
    from eidolon.providers.openai import OpenAIProvider

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        candidate = load_config(service.configuration)
        profile = candidate.selected_provider()
        profile.context_window = 131072
        profile.model_metadata["new-model"] = ModelMetadata(
            context_window=1_000_000, source="synthetic", updated_at=0,
        )
        profile.parameters = {"temperature": 0.3, **({field: 4096} if field else {})}
        save_config(candidate, service.configuration)

        await client.request("select_model", name="first", model="new-model", reasoning_effort="high",
                             context_window=context_window, max_response_tokens=65536)

        saved = load_config(service.configuration).selected_provider()
        expected_field = field or "max_completion_tokens"
        assert saved.context_window == context_window
        assert saved.context_window_for() == (context_window or 1_000_000)
        assert saved.parameters == {"temperature": 0.3, "reasoning_effort": "high", expected_field: 65536}
        payload = OpenAIProvider(saved)._payload([{"role": "user", "content": "Hello"}], None, "plain")
        assert payload[expected_field] == 65536
        assert ContextBudget.for_profile(saved).output_tokens == 65536
        assert service.engine.config.selected_provider().parameters == saved.parameters


@pytest.mark.parametrize("key,value", [
    ("context_window", 0), ("context_window", -1), ("context_window", True),
    ("context_window", "32000"), ("max_response_tokens", 0), ("max_response_tokens", -1),
    ("max_response_tokens", True), ("max_response_tokens", "32000"), ("max_response_tokens", None),
])
async def test_invalid_model_token_settings_preserve_saved_configuration(tmp_path, monkeypatch, key, value):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        before = service.configuration.read_bytes()
        with pytest.raises(RuntimeError, match="positive token count"):
            await client.request("select_model", name="first", model="new-model", **{key: value})
        assert service.configuration.read_bytes() == before


async def test_model_selection_normalizes_conflicting_output_fields(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        candidate = load_config(service.configuration)
        candidate.selected_provider().parameters = {
            "temperature": 0.3, "max_tokens": 4096, "max_completion_tokens": 8192,
        }
        save_config(candidate, service.configuration)
        await client.request("select_model", name="first", model="first-model", max_response_tokens=32768)
        assert load_config(service.configuration).selected_provider().parameters == {
            "temperature": 0.3, "max_completion_tokens": 32768,
        }


async def test_model_selection_preserves_pending_settings_and_default_effort_omits_parameter(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        candidate = load_config(service.configuration)
        candidate.selected_provider().display_name = "Friendly provider"
        candidate.selected_provider().context_window = 1_000_000
        candidate.selected_provider().parameters = {"temperature": 0.3, "reasoning_effort": "saved-custom"}
        candidate.theme.colors = {"accent": "#abcdef"}
        save_config(candidate, service.configuration)
        same_model = await client.request("select_model", name="first", model="first-model",
                                         reasoning_effort="saved-custom")
        assert same_model["providers"][0]["context_window"] == 1_000_000
        unchanged = await client.request("select_model", name="first", model="manual", reasoning_effort="saved-custom")
        assert unchanged["providers"][0]["context_window"] is None
        assert unchanged["providers"][0]["parameters"]["reasoning_effort"] == "saved-custom"
        with pytest.raises(RuntimeError, match="supported reasoning effort"):
            await client.request("select_model", name="first", model="manual", reasoning_effort="unsupported")
        selected = await client.request("select_model", name="first", model="manual", reasoning_effort="max")
        assert selected["providers"][0]["parameters"]["reasoning_effort"] == "max"
        default = await client.request("select_model", name="first", model="manual", reasoning_effort=None)
        assert default["providers"][0]["parameters"] == {"temperature": 0.3}
        assert default["theme"]["colors"] == {"accent": "#abcdef"}
        assert (await client.request("status"))["provider"] == "Friendly provider · manual"


async def test_preference_patch_preserves_latest_models_theme_and_connections(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        latest = load_config(service.configuration)
        latest.selected_provider().model = "selected-elsewhere"
        latest.selected_provider().api_key_ref = "env:EXAMPLE_KEY"
        latest.theme.colors = {"accent": "#abcdef"}
        latest.memory.bank = "existing-bank"
        save_config(latest, service.configuration)
        result = await client.request("save_preferences", preferences={
            "timezone": "UTC", "reset_hour": 7, "follow_replies": False,
            "memory_enabled": False, "recall_budget": "high", "max_memories": 20,
            "search_depth": "advanced", "search_results": 8,
        })
        config = load_config(service.configuration)
        assert config.model_dump(mode="json") == result
        assert config.selected_provider().model == "selected-elsewhere"
        assert config.selected_provider().api_key_ref == "env:EXAMPLE_KEY"
        assert config.theme == latest.theme and config.memory.bank == "existing-bank"
        assert config.timezone == "UTC" and config.reset_hour == 7
        assert not config.preferences.follow_replies and not config.memory.enabled
        assert config.memory.recall_budget == "high" and config.memory.max_memories == 20
        assert config.tavily.depth == "advanced" and config.tavily.max_results == 8
        assert service.configuration.name == "config.yaml"


async def test_interface_preferences_do_not_restart_integrations(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, engines):
        engine, memory = service.engine, service.memory
        await client.request("save_preferences", preferences={
            "follow_replies": False, "show_service_status": False,
            "bindings": {"send": "ctrl+enter", "newline": "enter"},
        })
        assert len(engines) == 1 and service.engine is engine and service.memory is memory
        assert not service.config.preferences.follow_replies
        assert service.config.bindings["send"] == "ctrl+enter"


async def test_identity_saves_preserve_adapters_routes_prompts_and_restart(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        started, release = asyncio.Event(), asyncio.Event()
        async def hold_maintenance(*, force=False):
            started.set()
            await release.wait()
        monkeypatch.setattr(service.continuity_worker, "run_once", hold_maintenance)
        assert service._start_continuity(force=True)
        await asyncio.wait_for(started.wait(), 5)
        maintenance = service._continuity_task
        components = {name: getattr(service, name) for name in (
            "engine", "tools", "memory", "mcp", "avatar", "dispatcher", "scheduler",
            "continuity_worker", "_auxiliary_execution",
        )}
        route = service.store.auxiliary_route()
        prompts = {path: Path(path).read_bytes() for path in service.config.prompts.values()}
        names = {"user_name": "  Ari 雪  ", "companion_name": "[bold]Orbit[/bold]"}
        for _ in range(2):
            result = await client.request("save_preferences", preferences=names)
            assert result["identity"] == {
                "user_name": "Ari 雪", "companion_name": "[bold]Orbit[/bold]",
            }
            assert service.config.identity.model_dump() == result["identity"]
            assert service.engine.config.identity == service.config.identity
            assert service.tools.config.identity == service.config.identity
            assert service._auxiliary_source.identity == service.config.identity
            assert all(getattr(service, name) is component
                       for name, component in components.items())
            assert service.store.auxiliary_route() == route
            assert len(engines) == 1 and not provider.requests
            assert service._continuity_task is maintenance and not maintenance.done()
        result = await client.request("save_preferences", preferences={"companion_name": "Nova"})
        assert result == load_config(service.configuration).model_dump(mode="json")
        assert result["identity"] == {"user_name": "Ari 雪", "companion_name": "Nova"}
        assert {path: Path(path).read_bytes() for path in prompts} == prompts
        assert not service.store.list_messages(service.current_session["id"])
        configuration = service.configuration
        release.set()
        await asyncio.wait_for(maintenance, 5)
    restarted = RoomService(load_config(configuration), configuration)
    try:
        assert restarted.config.identity.model_dump() == result["identity"]
        assert restarted.engine.config.identity == restarted.config.identity
        assert {path: Path(path).read_bytes() for path in prompts} == prompts
        assert not restarted.store.list_messages(restarted.current_session["id"])
    finally:
        await restarted.close()


@pytest.mark.parametrize("recall,save", [(False, True), (True, False)])
async def test_scheduled_runs_freeze_names_through_tools_and_use_later_saved_names(
    tmp_path, monkeypatch, recall, save,
):
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        original = {"user_name": "Aster", "companion_name": "Vale"}
        renamed = {"user_name": "River 雪", "companion_name": "[blue]Sol[/blue]"}
        await client.request("save_preferences", preferences={
            **original, "memory_enabled": recall, "memory_save_enabled": save,
        })
        source = tmp_path / "synthetic-source.txt"
        source.write_text("Synthetic scheduled evidence.")
        service.tools.files.roots = (tmp_path.resolve(),)
        job = Job(id="synthetic-daily", name="Synthetic daily", prompt="Read the synthetic source.",
                  provider="second", enabled=True, permitted_tools=["read_file"],
                  required_tools=["read_file"])
        await client.request("save_job", job=job.model_dump(), create=True)
        scheduler, engine, tools = service.scheduler, service.engine, service.tools
        job_bytes = service.config.jobs_file.read_bytes()
        route = service.store.auxiliary_route()
        started, release = asyncio.Event(), asyncio.Event()
        stream = provider.stream

        async def held_stream(messages, **kwargs):
            if not provider.requests:
                started.set()
                await release.wait()
            async for event in stream(messages, **kwargs):
                yield event

        provider.stream = held_stream
        tool_round = [{"type": "tool_call", "call": {"id": "synthetic-read", "function": {
            "name": "read_file", "arguments": json.dumps({"path": str(source)}),
        }}}, {"type": "finish", "reason": "tool_calls"}]
        provider.responses = [
            tool_round,
            [{"type": "token", "text": "Synthetic first result."}, {"type": "finish", "reason": "stop"}],
            tool_round,
            [{"type": "token", "text": "Synthetic second result."}, {"type": "finish", "reason": "stop"}],
        ]
        now = datetime.fromisoformat("2026-10-20T14:00:00+00:00")
        active = asyncio.create_task(scheduler.tick(now))
        try:
            await asyncio.wait_for(started.wait(), 5)
            first_run = service.store.list_job_runs(job.id)[0]
            for _ in range(2):
                await client.request("save_preferences", preferences=renamed)
                assert not active.done() and service.generation_lock.locked()
                assert service.scheduler is scheduler and service.engine is engine
                assert service.tools is tools and not engines[-1].was_closed
                assert service.store.list_job_runs(job.id)[0] == first_run
                assert service.store.auxiliary_route() == route
        finally:
            release.set()
            await asyncio.wait_for(active, 5)
        completed = service.store.list_job_runs(job.id)[0]
        assert completed["id"] == first_run["id"] and completed["status"] == "completed"
        assert completed["result"] == "Synthetic first result."
        assert len(provider.requests) == 2
        await scheduler.tick(now.replace(day=21))
        runs = service.store.list_job_runs(job.id)
        assert len(runs) == 2 and len({run["id"] for run in runs}) == 2
        assert all(run["status"] == "completed" for run in runs)
        assert len(provider.requests) == 4
        for request, names in zip(provider.requests, (original, original, renamed, renamed), strict=True):
            assert any(json.dumps(names, ensure_ascii=False) in message["content"]
                       for message in request if message["role"] == "system")
            assert all("identity" not in message for message in request)
        for run, names in ((completed, original), (runs[0], renamed)):
            messages = service.store.list_messages(run["session_id"])
            assert [row["role"] for row in messages] == ["user", "assistant"]
            assert all(row["identity"] == names for row in messages)
            generation = service.store.db.execute(
                "SELECT metadata FROM generations WHERE id=?", (run["id"],),
            ).fetchone()
            metadata = json.loads(generation["metadata"])
            assert metadata["identity"] == names and metadata["provider"] == "second"
            assert metadata["memory_eligible"] is save
        assert bool(service.memory.queries) is recall
        assert len(service.store.pending_outbox()) == (2 if save else 0)
        assert service.config.jobs_file.read_bytes() == job_bytes
        await scheduler.tick(now.replace(day=21))
        assert len(service.store.list_job_runs(job.id)) == 2 and len(provider.requests) == 4


async def test_queued_job_reads_saved_names_when_execution_begins(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        job = Job(id="synthetic-queued", name="Synthetic queued", prompt="A synthetic request.",
                  enabled=True)
        await client.request("save_job", job=job.model_dump(), create=True)
        waiting = asyncio.Event()
        foreground = service._foreground_generation

        @asynccontextmanager
        async def observed_foreground():
            waiting.set()
            async with foreground():
                yield

        monkeypatch.setattr(service, "_foreground_generation", observed_foreground)
        provider.responses = [[{"type": "token", "text": "Synthetic queued result."},
                               {"type": "finish", "reason": "stop"}]]
        scheduler = service.scheduler
        now = datetime.fromisoformat("2026-10-20T14:00:00+00:00")
        async with service.generation_lock:
            active = asyncio.create_task(scheduler.tick(now))
            await asyncio.wait_for(waiting.wait(), 5)
            queued = service.store.list_job_runs(job.id)[0]
            assert not provider.requests
            saved = load_config(service.configuration)
            saved.identity.user_name = saved.identity.companion_name = "River 雪"
            save_config(saved, service.configuration)
            assert service.config.identity != saved.identity
        await asyncio.wait_for(active, 5)
        completed = service.store.list_job_runs(job.id)[0]
        assert completed["id"] == queued["id"] and completed["status"] == "completed"
        assert any(json.dumps(saved.identity.model_dump(), ensure_ascii=False) in message["content"]
                   for message in provider.requests[0] if message["role"] == "system")
        assert all(message["identity"] == saved.identity.model_dump()
                   for message in service.store.list_messages(completed["session_id"]))
        await scheduler.tick(now)
        assert len(provider.requests) == 1 and len(service.store.list_job_runs(job.id)) == 1


async def test_concurrent_identity_patches_merge_into_latest_saved_configuration(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        latest = load_config(service.configuration)
        latest.selected_provider().model = "selected-elsewhere"
        latest.selected_provider().api_key_ref = "env:SYNTHETIC_MODEL_KEY"
        latest.theme.colors = {"accent": "#abcdef"}
        latest.memory.bank = "synthetic-bank"
        latest.identity.companion_name = "Saved elsewhere"
        save_config(latest, service.configuration)
        first = await client.request("save_preferences", preferences={"user_name": "Ari"})
        assert first["identity"] == {"user_name": "Ari", "companion_name": "Saved elsewhere"}
        await asyncio.gather(
            client.request("save_preferences", preferences={"user_name": "Bex"}),
            client.request("save_preferences", preferences={"companion_name": "Orbit"}),
            client.request("save_preferences", preferences={"show_service_status": False}),
        )
        expected = latest.model_dump()
        expected["identity"] = {"user_name": "Bex", "companion_name": "Orbit"}
        expected["preferences"]["show_service_status"] = False
        saved = load_config(service.configuration)
        assert saved.model_dump() == expected
        assert service.config.model_dump() == expected
        assert service._auxiliary_source.identity == saved.identity


@pytest.mark.parametrize("inactive_connection_change", [False, True])
async def test_external_identity_edits_reload_without_replacing_adapters(
    tmp_path, monkeypatch, inactive_connection_change,
):
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        components = {name: getattr(service, name) for name in (
            "engine", "tools", "memory", "mcp", "avatar", "dispatcher", "scheduler",
            "continuity_worker", "_auxiliary_execution",
        )}
        route = service.store.auxiliary_route()
        latest = load_config(service.configuration)
        latest.identity.user_name, latest.identity.companion_name = "Bex", "Nova"
        if inactive_connection_change:
            latest.providers[1].model = "future-model"
        save_config(latest, service.configuration)
        provider.responses.append([
            {"type": "token", "text": "Synthetic reply"}, {"type": "finish", "reason": "stop"},
        ])
        result = [event async for event in client.stream("generate", text="Synthetic request")]
        assert result[-1]["type"] == "completed"
        assert result[-1]["metadata"]["identity"] == latest.identity.model_dump()
        assert all(getattr(service, name) is component for name, component in components.items())
        assert len(engines) == 1 and service.store.auxiliary_route() == route
        assert service.config.identity == service._auxiliary_source.identity == latest.identity


@pytest.mark.parametrize("pending_memory_change", [False, True])
async def test_rename_during_generation_preserves_snapshot_and_refreshes_future_identity(
    tmp_path, monkeypatch, pending_memory_change,
):
    started, release = asyncio.Event(), asyncio.Event()
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        async def stream(messages, *, tools, presentation):
            provider.requests.append(copy.deepcopy(messages))
            yield {"type": "token", "text": "Synthetic reply"}
            if len(provider.requests) == 1:
                started.set()
                await release.wait()
            yield {"type": "finish", "reason": "stop"}
        provider.stream = stream
        async def generate():
            return [event async for event in client.stream("generate", text="Synthetic request")]
        engine = service.engine
        original = service.config.identity.model_dump()
        active = asyncio.create_task(generate())
        try:
            await asyncio.wait_for(started.wait(), 5)
            active_task = service._active_task
            if pending_memory_change:
                await client.request("save_preferences", preferences={"memory_enabled": False})
            saved = await client.request("save_preferences", preferences={
                "user_name": "Ari 雪", "companion_name": "[bold]Orbit[/bold]",
            })
            assert service.config.identity.model_dump() == saved["identity"]
            assert service.engine is engine and not engine.was_closed and len(engines) == 1
            assert service._active_task is active_task and not active_task.cancelling()
            assert not active.done() and service.generation_lock.locked()
            assert service.config.memory.enabled
            assert service._auxiliary_source.identity.model_dump() == saved["identity"]
            preview = await client.request("preview", text="Future request")
            assert any(json.dumps(saved["identity"], ensure_ascii=False) in message["content"]
                       for message in preview["messages"])
        finally:
            release.set()
            first = await asyncio.wait_for(active, 5)
        assert first[0]["type"] == "turn_started" and first[0]["identity"] == original
        assert first[-1]["type"] == "completed" and first[-1]["metadata"]["identity"] == original
        second = await generate()
        assert second[0]["identity"] == saved["identity"]
        assert second[-1]["metadata"]["identity"] == saved["identity"]
        for request, names in zip(provider.requests, (original, saved["identity"]), strict=True):
            assert any(json.dumps(names, ensure_ascii=False) in message["content"]
                       for message in request)
        messages = service.store.list_messages(service.current_session["id"])
        assert [message["identity"] for message in messages] == (
            [original] * 2 + [saved["identity"]] * 2
        )
        assert len(engines) == (2 if pending_memory_change else 1)


@pytest.mark.parametrize("patch", [
    {"user_name": "New user", "companion_name": ""},
    {"user_name": "PRIVATE\n", "companion_name": "New companion"},
    {"user_name": True}, {"companion_name": None}, {"companion_name": "x" * 81},
])
async def test_invalid_identity_save_preserves_saved_and_active_state(tmp_path, monkeypatch, patch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={
            "user_name": "Ari", "companion_name": "Orbit",
        })
        original = service.configuration.read_bytes()
        active = service.config.model_dump()
        source = service._auxiliary_source.model_dump()
        route = service.store.auxiliary_route()
        engine = service.engine
        with pytest.raises(RuntimeError) as error:
            await client.request("save_preferences", preferences=patch)
        assert "PRIVATE" not in str(error.value)
        assert service.configuration.read_bytes() == original
        assert service.config.model_dump() == active and service.engine is engine
        assert service.engine.config.model_dump() == active
        assert service._auxiliary_source.model_dump() == source
        assert service.store.auxiliary_route() == route


async def test_identity_write_failure_preserves_saved_and_active_state_then_can_retry(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        original = service.configuration.read_bytes()
        active = service.config.model_dump()
        source = service._auxiliary_source.model_dump()
        route = service.store.auxiliary_route()
        engine = service.engine
        replace = os.replace
        def fail_replace(source, target):
            if Path(target) == service.configuration:
                raise OSError("Synthetic persistence failure")
            return replace(source, target)
        names = {"user_name": "Ari", "companion_name": "Orbit"}
        with monkeypatch.context() as failure:
            failure.setattr("eidolon.config.os.replace", fail_replace)
            with pytest.raises(RuntimeError, match="Synthetic persistence failure"):
                await client.request("save_preferences", preferences=names)
        assert service.configuration.read_bytes() == original
        assert service.config.model_dump() == active and service.engine is engine
        assert service.engine.config.model_dump() == active
        assert service._auxiliary_source.model_dump() == source
        assert service.store.auxiliary_route() == route
        assert not list(service.configuration.parent.glob(".eidolon-*"))
        result = await client.request("save_preferences", preferences=names)
        assert result["identity"] == service.config.identity.model_dump() == names
        assert service.engine is engine


async def test_invalid_or_unrelated_preferences_do_not_modify_saved_config(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        original = service.configuration.read_bytes()
        for patch in ({"reset_hour": 24}, {"provider": "second"}, {"theme": {"name": "nord"}}):
            with pytest.raises(RuntimeError):
                await client.request("save_preferences", preferences=patch)
            assert service.configuration.read_bytes() == original


async def test_preferences_saved_during_response_apply_after_it_finishes(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        engine = service.engine
        async with service.generation_lock:
            await client.request("save_preferences", preferences={"memory_enabled": False})
            assert load_config(service.configuration).memory.enabled is False
            assert service.engine is engine and service.config.memory.enabled
        await service._apply_saved_configuration()
        assert service.engine is not engine and not service.config.memory.enabled


async def test_slow_connect_merges_latest_settings_saved_during_discovery(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def inventory(adapter, **kwargs):
        started.set()
        await release.wait()
        return ["a-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        saving = asyncio.create_task(client.request("save_provider", provider={"name": "custom", "model": ""}))
        await asyncio.wait_for(started.wait(), 1)
        selected = await client.request("select_model", name="second", model="pending-model", reasoning_effort="medium")
        assert selected["provider"] == "second"
        release.set()
        saved = await asyncio.wait_for(saving, 1)
        assert saved["config"]["provider"] == "second"
        latest = load_config(service.configuration)
        assert latest.selected_provider().model == "pending-model"
        assert latest.selected_provider().parameters["reasoning_effort"] == "medium"
        assert next(item for item in latest.providers if item.name == "custom").model == "a-model"


async def test_inventory_completion_does_not_overwrite_changed_connection(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def inventory(adapter, **kwargs):
        started.set()
        await release.wait()
        return ["old-endpoint-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        listing = asyncio.create_task(client.request("list_models", name="first"))
        await asyncio.wait_for(started.wait(), 1)
        candidate = load_config(service.configuration)
        candidate.selected_provider().endpoint = "https://changed.example/v1"
        candidate.selected_provider().models = ["new-endpoint-model"]
        save_config(candidate, service.configuration)
        release.set()
        await asyncio.wait_for(listing, 1)
        restored = load_config(service.configuration).selected_provider()
        assert restored.endpoint == "https://changed.example/v1" and restored.models == ["new-endpoint-model"]


async def test_full_connection_form_preserves_latest_advanced_parameters(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def inventory(adapter, **kwargs):
        started.set()
        await release.wait()
        return ["first-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        form = service.config.selected_provider().model_dump(mode="json")
        form["display_name"] = "Changed label"
        saving = asyncio.create_task(client.request("save_provider", provider=form))
        await asyncio.wait_for(started.wait(), 1)
        await client.request("select_model", name="first", model="first-model", reasoning_effort="high")
        release.set()
        result = await asyncio.wait_for(saving, 1)
        profile = result["config"]["providers"][0]
        assert profile["display_name"] == "Changed label"
        assert profile["parameters"]["reasoning_effort"] == "high"


async def test_full_connection_form_does_not_overwrite_concurrent_same_provider_model(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def inventory(adapter, **kwargs):
        started.set()
        await release.wait()
        return ["first-model"]
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        form = service.config.selected_provider().model_dump(mode="json")
        saving = asyncio.create_task(client.request("save_provider", provider=form))
        await asyncio.wait_for(started.wait(), 1)
        await client.request("select_model", name="first", model="pending-model", reasoning_effort="high")
        release.set()
        with pytest.raises(RuntimeError, match="Provider model changed"):
            await asyncio.wait_for(saving, 1)
        profile = load_config(service.configuration).selected_provider()
        assert profile.model == "pending-model" and profile.parameters["reasoning_effort"] == "high"


async def test_keychain_prompt_does_not_block_service_status(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    async def inventory(adapter, **kwargs):
        return ["first-model"]
    def keychain(name, value):
        entered.set()
        release.wait(timeout=2)
        return "keyring:" + name
    monkeypatch.setattr("eidolon.service.runtime.OpenAIProvider.list_models", inventory)
    monkeypatch.setattr("eidolon.service.runtime.store_secret", keychain)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        saving = asyncio.create_task(client.request("save_provider", provider={"name": "first"}, api_key="private-key"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            status = await asyncio.wait_for(client.request("status"), 0.5)
            assert status["provider"] == "first · first-model" and not saving.done()
        finally:
            release.set()
        assert (await asyncio.wait_for(saving, 1))["warning"] is None


async def test_terminal_themes_persist_in_private_config_and_preserve_custom_slot(tmp_path, monkeypatch):
    from eidolon.themes import PALETTES

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        latest = load_config(service.configuration)
        latest.selected_provider().model = 'changed-elsewhere'
        save_config(latest, service.configuration)
        for name in PALETTES:
            result = await client.request('save_theme', name=name, colors={})
            restored = load_config(service.configuration)
            assert restored.theme.name == name and restored.theme.colors == {}
            assert restored.selected_provider().model == 'changed-elsewhere'
            assert result['theme'] == restored.theme.model_dump()
        colors = {'accent': '#de90bd', 'warning': '#f9e2af'}
        await client.request('save_theme', name='catppuccin-mocha', colors=colors,
                             custom_base='catppuccin-mocha', custom_colors=colors)
        await client.request('save_theme', name='github-light', colors={})
        restored = load_config(service.configuration)
        assert restored.theme.name == 'github-light'
        assert restored.theme.custom_base == 'catppuccin-mocha'
        assert restored.theme.custom_colors == colors
        assert service.config.theme == restored.theme
        assert service.configuration.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('params', [
    {'name': 'unknown', 'colors': {}},
    {'name': 'nord', 'colors': {'dark': 'false'}},
    {'name': 'nord', 'colors': {'accent': 7}},
    {'name': 'nord', 'colors': {'accent': 'bad-color'}},
    {'name': 'nord', 'colors': []},
    {'name': 'nord', 'colors': {}, 'custom_base': 'unknown', 'custom_colors': {}},
    {'name': 'nord', 'colors': {}, 'custom_base': 'nord', 'custom_colors': {'accent': 'bad'}},
])
async def test_invalid_theme_does_not_change_persistent_config(tmp_path, monkeypatch, params):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        original = service.configuration.read_bytes()
        with pytest.raises(RuntimeError):
            await client.request('save_theme', **params)
        assert service.configuration.read_bytes() == original


async def test_switching_theme_keeps_legacy_custom_overrides(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        legacy = load_config(service.configuration)
        legacy.theme.name = 'solarized-light'
        legacy.theme.colors = {'accent': '#de90bd'}
        save_config(legacy, service.configuration)
        await client.request('save_theme', name='nord', colors={})
        saved = load_config(service.configuration)
        assert saved.theme.name == 'nord' and saved.theme.colors == {}
        assert saved.theme.custom_base == 'solarized-light'
        assert saved.theme.custom_colors == {'accent': '#de90bd'}


async def test_memory_inspection_and_independent_saving_over_socket(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        session = service.current_session["id"]
        await client.request("save_preferences", preferences={"memory_save_enabled": False})
        first = [event async for event in client.stream(
            "generate", session_id=session, text="Synthetic unsaved preference")]
        assert first[-1]["type"] == "completed", first
        assert service.memory.queries and not service.store.pending_outbox()
        state = await client.request("memory_state", session_id=session)
        assert not state["controls"]["save_new_memories"]
        assert state["controls"]["use_learned_memories"]
        assert len(state["manifests"]) == 1
        manifest = await client.request("memory_manifest", manifest_id=state["manifests"][0]["id"])
        assert any(manifest["manifest"]["text"] in message["content"]
                   for message in provider.requests[0]
                   if message["role"] == "user" and "# Memory context\n" in message["content"])
        query_count, request_count = len(service.memory.queries), len(provider.requests)
        await client.request("memory_state", session_id=session)
        await client.request("memory_manifest", manifest_id=manifest["id"])
        assert len(service.memory.queries) == query_count and len(provider.requests) == request_count
        await client.request("save_preferences", preferences={
            "memory_save_enabled": True, "memory_enabled": False,
        })
        second = [event async for event in client.stream(
            "generate", session_id=session, text="Synthetic saved exchange")]
        assert second[-1]["type"] == "completed", second
        pending = service.store.pending_outbox()
        assert len(pending) == 1 and "unsaved preference" not in pending[0]["payload"]["content"]
        paused = await client.request("memory_delivery", pause=True, session_id=session)
        assert paused["controls"]["pending_delivery_paused"]
        await client.request("save_preferences", preferences={"max_memories": 15})
        assert (await client.request("memory_state"))["controls"]["pending_delivery_paused"]
        assert not (await client.request("memory_delivery", pause=False))["controls"]["pending_delivery_paused"]
        with pytest.raises(RuntimeError, match="pause"):
            await client.request("memory_delivery", pause="yes")


async def test_legacy_review_polls_uncertain_operations_and_only_reviews_previewed_sources(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        session = service.current_session["id"]
        for index in range(3):
            service.store.complete_turn(session, f"Synthetic source {index}", "Synthetic reply",
                                        generation_id=f"legacy-{index}")
        with service.store.db:
            service.store.db.execute("UPDATE generations SET memory_eligible=NULL")
            service.store.db.execute("UPDATE outbox SET review_state='pending'")
            service.store.db.execute("UPDATE outbox SET status='submitted' WHERE generation_id='legacy-1'")
        polled = []

        async def operation(identity):
            polled.append(identity)
            return {"status": "processing"}

        service.memory.operation = operation
        initial = await client.request("memory_state")
        identities = {entry["generation_id"]: entry["document_id"] for entry in initial["legacy_pending"]}
        result = await client.request("memory_review_legacy", approve=True,
                                      document_ids=[identities["legacy-0"], identities["legacy-1"]])
        assert len(polled) == 1
        assert result["outbox"]["eligible"] == 1 and result["outbox"]["held"] == 2
        assert result["outbox"]["review_polling"] == 1
        remaining = {entry["generation_id"] for entry in result["legacy_pending"]}
        assert remaining == {"legacy-1", "legacy-2"}
        result = await client.request("memory_review_legacy", approve=False,
                                      document_ids=[identities["legacy-2"]])
        assert result["outbox"]["rejected"] == 1 and result["outbox"]["held"] == 1
        with pytest.raises(RuntimeError, match="Select"):
            await client.request("memory_review_legacy", approve=True)


async def test_connection_reload_and_pause_share_inflight_delivery_lock(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.config.memory.retain_enabled = service.config.memory.verified = True
        service.store.complete_turn(service.current_session["id"], "Synthetic pending", "Reply")
        entered, release = asyncio.Event(), asyncio.Event()
        closed = []

        async def retain(payload, operation_id):
            entered.set()
            await release.wait()
            assert not closed
            return {"async": False, "success": True}

        async def close_memory():
            closed.append(True)

        service.memory.retain = retain
        service.memory.close = close_memory
        old_dispatcher = service.dispatcher
        dispatch = asyncio.create_task(old_dispatcher.dispatch_once())
        await entered.wait()
        reload = asyncio.create_task(client.request("save_preferences", preferences={"memory_enabled": False}))
        pause = asyncio.create_task(client.request("memory_delivery", pause=True))
        try:
            await asyncio.sleep(.02)
            assert not closed and not reload.done() and not pause.done()
        finally:
            release.set()
            await asyncio.gather(dispatch, reload, pause)
        assert closed and service.dispatcher is not old_dispatcher
        assert service.store.memory_delivery_paused()
