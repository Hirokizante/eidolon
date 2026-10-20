import asyncio
import json

import pytest

from eidolon.config import load_config
from eidolon.scheduler import Job
from eidolon.service.runtime import RoomService
from test_service import events, room


@pytest.fixture(autouse=True)
def no_background_metadata_network(monkeypatch):
    monkeypatch.setattr(RoomService, "_schedule_metadata_refresh", lambda *args: None)


def tool_response(provider, name, arguments):
    provider.responses = [
        [{"type": "tool_call", "call": {"id": "tool-one", "function": {
            "name": name, "arguments": json.dumps(arguments),
        }}}, {"type": "finish", "reason": "tool_calls"}],
        [{"type": "token", "text": "Done."}, {"type": "finish", "reason": "stop"}],
    ]


async def test_socket_toggle_context_tools_and_restart_default(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        before = service.configuration.read_bytes()
        configuration = service.configuration
        offered = []
        original = provider.stream
        async def observed(messages, *, tools, presentation):
            offered.append({item["function"]["name"] for item in tools or []})
            async for event in original(messages, tools=tools, presentation=presentation):
                yield event
        provider.stream = observed
        assert not (await client.request("status"))["agent_mode"]
        target = tmp_path / "work" / "task.txt"
        tool_response(provider, "write_file", {"path": str(target), "content": "created"})
        denied = await events(client, agent_mode=True)  # Generate parameters cannot enable the mode.
        assert denied[-1]["type"] == "completed" and not target.exists()
        assert "write_file" not in offered[0]
        assert any(event.get("name") == "write_file" and event.get("status") == "error"
                   for event in denied)
        assert (await client.request("agent_mode")) == {"agent_mode": True}
        assert service.configuration.read_bytes() == before
        preview = await client.request("preview")
        assert "Agent mode is enabled" in json.dumps(preview)
        tool_response(provider, "write_file", {"path": str(target), "content": "created"})
        accepted = await events(client)
        assert accepted[-1]["type"] == "completed" and target.read_text() == "created"
        assert accepted[-1]["metadata"]["agent_mode"]
        assert "write_file" in offered[-1] and "run_command" in offered[-1]
        assert "Agent mode is enabled" in json.dumps(provider.requests[-1])
        await client.request("save_preferences", preferences={"show_service_status": False})
        assert (await client.request("status"))["agent_mode"]
        assert (await client.request("agent_mode", enabled=False)) == {"agent_mode": False}
        tool_response(provider, "read_file", {"path": str(target)})
        restricted = await events(client)
        assert "write_file" not in offered[-1]
        assert any(event.get("name") == "read_file" and event.get("status") == "error"
                   for event in restricted)
        await client.request("agent_mode", enabled=True)
        # Toggling the mode is never written to private configuration.
        assert "agent_mode" not in configuration.read_text()
    restarted = RoomService(load_config(configuration), configuration)
    try:
        assert not restarted.tools.agent_mode
        assert not restarted.status()["agent_mode"]
    finally:
        await restarted.close()


async def test_scheduled_jobs_do_not_receive_agent_tools_even_when_allowlisted(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        await client.request("agent_mode", enabled=True)
        target = tmp_path / "background.txt"
        tool_response(provider, "write_file", {"path": str(target), "content": "job"})
        offered = []
        original = provider.stream
        async def observed(messages, *, tools, presentation):
            offered.extend(item["function"]["name"] for item in tools or [])
            async for event in original(messages, tools=tools, presentation=presentation):
                yield event
        provider.stream = observed
        service.store.create_session("job-session", kind="job")
        job = Job(id="test", name="Test", prompt="Make a note", permitted_tools=["write_file", "run_command"])
        reply = await service._run_job(job, {
            "id": "job-run", "session_id": "job-session",
        })
        assert reply == "Done." and not target.exists()
        assert not offered
        assert "Agent mode is disabled" in json.dumps(provider.requests[-1])
        assert service.tools.agent_mode


@pytest.mark.parametrize("chat_agent", [False, True])
@pytest.mark.parametrize("permissions", [[], ["write_file"]])
async def test_agent_job_uses_its_own_mode_and_tool_allowlist(tmp_path, monkeypatch, chat_agent, permissions):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        await client.request("agent_mode", enabled=chat_agent)
        chat_tools = service.tools
        target = tmp_path / "scheduled-task.txt"
        tool_response(provider, "write_file", {"path": str(target), "content": "job completed"})
        offered = set()
        original = provider.stream
        async def observed(messages, *, tools, presentation):
            offered.update(item["function"]["name"] for item in tools or [])
            async for event in original(messages, tools=tools, presentation=presentation):
                yield event
        provider.stream = observed
        service.store.create_session("agent-job", kind="job")
        job = Job(id="agent", name="Agent task", prompt="Do a task", agent_mode=True,
                  permitted_tools=permissions)
        assert await service._run_job(job, {
            "id": "agent-run", "session_id": "agent-job",
        }) == "Done."
        assert target.read_text() == "job completed"
        assert "write_file" in offered
        assert ("run_command" in offered) is (not permissions)
        assert service.tools is chat_tools and service.tools.agent_mode is chat_agent
        assert "Agent mode is enabled" in json.dumps(provider.requests[-1])


async def test_agent_job_cleans_up_commands_without_touching_chat_commands(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        await client.request("agent_mode", enabled=True)
        chat = await service.tools.call("run_command", {"command": "sleep 60", "yield_time_ms": 0},
                                        {"interactive": True, "agent_mode": True})
        tool_response(provider, "run_command", {"command": "sleep 60", "yield_time_ms": 0})
        service.store.create_session("command-job", kind="job")
        registries = []
        factory = service.engine_factory
        def capture(*args):
            registries.append(args[3])
            return factory(*args)
        service.engine_factory = capture
        try:
            await service._run_job(Job(id="commands", name="Commands", prompt="Run a command", agent_mode=True), {
                "id": "command-run", "session_id": "command-job",
            })
            assert registries[0] is not service.tools
            assert not registries[0].agent.commands and not registries[0].agent_mode
            assert not service.tools.agent.commands[chat["command_id"]].done.is_set()
        finally:
            await client.request("agent_mode", enabled=False)


async def test_agent_job_rejects_provider_without_tools(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, _, _, _):
        service.config.selected_provider().tools = False
        with pytest.raises(ValueError, match="tool calling"):
            await service._run_job(Job(id="agent", name="Agent", prompt="Task", agent_mode=True), {
                "id": "agent-run", "session_id": "missing",
            })
        assert not service.tools.agent_mode


async def test_disabling_mode_cancels_active_agent_turn_and_command(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        await client.request("agent_mode", enabled=True)
        command_started = asyncio.Event()
        release = asyncio.Event()
        rounds = 0
        async def stream(messages, *, tools, presentation):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"type": "tool_call", "call": {"id": "command-one", "function": {
                    "name": "run_command", "arguments": '{"command":"sleep 60","yield_time_ms":0}',
                }}}
                yield {"type": "finish", "reason": "tool_calls"}
            else:
                command_started.set()
                await release.wait()
                yield {"type": "token", "text": "Finished."}
        provider.stream = stream
        active = asyncio.create_task(events(client))
        await asyncio.wait_for(command_started.wait(), 2)
        session = next(iter(service.tools.agent.commands.values()))
        assert not session.done.is_set()
        assert (await client.request("agent_mode", enabled=False)) == {"agent_mode": False}
        result = await asyncio.wait_for(active, 2)
        assert result[-1]["type"] == "error" and result[-1].get("code") == "cancelled", result
        assert session.done.is_set() and session.process.returncode is not None
        assert not service.tools.agent.commands and not service.store.pending_outbox()
        assert not service.store.list_messages(service.current_session["id"])


async def test_mode_survives_connection_reload_and_rejects_invalid_enabling(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        for value in ("yes", 1, None):
            with pytest.raises(RuntimeError, match="boolean"):
                await client.request("agent_mode", enabled=value)
        assert not service.tools.agent_mode
        await client.request("agent_mode", enabled=True)
        old_tools = service.tools
        config = service.config.model_dump(mode="json")
        config["provider"] = "second"
        await client.request("save_settings", config=config)
        assert service.tools is not old_tools
        assert service.tools.agent_mode and (await client.request("status"))["agent_mode"]
        await client.request("agent_mode", enabled=False)
        service.config.selected_provider().tools = False
        with pytest.raises(RuntimeError, match="tool calling"):
            await client.request("agent_mode", enabled=True)
        assert not service.tools.agent_mode
