import asyncio
import json
import os
import shlex
import sys

import pytest

from eidolon.config import AppConfig
from eidolon.tools import FileBoundaryError, ToolRegistry
from eidolon.tools.agent import AgentTools
from eidolon.tools.registry import AGENT_NAMES, BUILTINS


AGENT_CONTEXT = {"interactive": True, "agent_mode": True, "generation_id": "turn-one"}


@pytest.fixture
async def registry(tmp_path):
    root = tmp_path / "permitted"
    root.mkdir()
    instance = ToolRegistry(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state", allowed_roots=[str(root)]))
    instance.agent.cwd = tmp_path
    yield instance
    await instance.close()


def names(schemas):
    return {item["function"]["name"] for item in schemas}


async def test_default_tools_and_guard_do_not_grant_agent_access(registry, tmp_path):
    assert not registry.agent_mode
    expected = names(BUILTINS)
    assert names(registry.schemas()) == names(registry.schemas(agent_mode=True)) == expected
    target = tmp_path / "outside.md"
    for context in ({}, AGENT_CONTEXT, {"interactive": False, "agent_mode": True}):
        with pytest.raises(ValueError, match="Agent mode"):
            await registry.call("write_file", {"path": str(target), "content": "hello"}, context)
    assert not target.exists()
    with pytest.raises(FileBoundaryError):
        await registry.call("read_file", {"path": str(target)})


async def test_enable_grants_unrestricted_files_and_disable_restores_boundaries(registry, tmp_path):
    assert await registry.set_agent_mode() is True
    assert names(registry.schemas()) == names(BUILTINS)
    assert AGENT_NAMES <= names(registry.schemas(agent_mode=True))
    assert names(registry.schemas(permitted=["write_file"], agent_mode=True)) == {"write_file"}
    target = tmp_path / "outside" / "task.md"
    written = await registry.call("write_file", {"path": str(target), "content": "one\ntwo\n"},
                                  AGENT_CONTEXT)
    assert written["created"] and target.read_text() == "one\ntwo\n"
    read = await registry.call("read_file", {"path": str(target), "offset": 2, "limit": 1},
                               AGENT_CONTEXT)
    assert read["content"] == "two\n" and read["next_offset"] is None
    assert await registry.set_agent_mode(False) is False
    with pytest.raises(ValueError, match="Agent mode"):
        await registry.call("edit_file", {"path": str(target), "old_text": "two", "new_text": "three"},
                            AGENT_CONTEXT)
    with pytest.raises(FileBoundaryError):
        await registry.call("read_file", {"path": str(target)})
    assert target.read_text() == "one\ntwo\n"


async def test_background_context_cannot_inherit_enabled_agent_tools(registry, tmp_path):
    await registry.set_agent_mode(True)
    target = tmp_path / "background.md"
    with pytest.raises(ValueError, match="Agent mode"):
        await registry.call("write_file", {"path": str(target), "content": "background"},
                            {"interactive": False, "agent_mode": True})
    with pytest.raises(FileBoundaryError):
        await registry.call("read_file", {"path": str(target)}, {"interactive": False})
    assert not target.exists()


@pytest.mark.parametrize("name,args", [
    ("edit_file", {"path": "test", "old_text": "x", "new_text": "y", "replace_all": "yes"}),
    ("run_command", {"command": "pwd", "tty": 1}),
    ("run_command", {"command": "pwd", "yield_time_ms": True}),
    ("run_command", {"command": "pwd", "yield_time_ms": 10_001}),
    ("run_command", {"command": "pwd", "timeout": 0}),
    ("read_file", {"path": "test", "offset": 0}),
    ("write_file", {"path": "test", "content": "x", "enabled": True}),
])
async def test_agent_argument_validation(registry, name, args):
    await registry.set_agent_mode(True)
    with pytest.raises(ValueError):
        await registry.call(name, args, AGENT_CONTEXT)


def test_agent_edits_are_exact_preserve_permissions_and_follow_symlinks(tmp_path):
    agent = AgentTools(cwd=tmp_path)
    target = tmp_path / "script.py"
    target.write_bytes(b"print('old')\r\nprint('old')\r\n")
    target.chmod(0o755)
    link = tmp_path / "link.py"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="more than once"):
        agent.edit_file("link.py", "old", "new")
    with pytest.raises(ValueError, match="not found"):
        agent.edit_file("link.py", "missing", "new")
    changed = agent.edit_file("link.py", "old", "new", replace_all=True)
    assert changed["replacements"] == 2 and not changed["created"]
    assert target.read_bytes() == b"print('new')\r\nprint('new')\r\n"
    assert target.stat().st_mode & 0o777 == 0o755 and link.is_symlink()
    listed = agent.list_files(".")
    assert next(item for item in listed["entries"] if item["name"] == "link.py")["kind"] == "symlink"
    assert agent.search_files(".", "print.*new", glob="script.py", regex=True)["matches"][0]["line"] == 1
    assert agent.search_files("script.py", "new")["matches"][1]["line"] == 2
    with pytest.raises(ValueError, match="exact text"):
        agent.edit_file("script.py", "", "new")


def test_paged_reads_fit_context_budget_without_skipping_lines(tmp_path):
    agent = AgentTools(cwd=tmp_path)
    lines = [f"{index} " + '"\\' * 20 + "\n" for index in range(100)]
    target = tmp_path / "large.txt"
    target.write_text("".join(lines))
    offset, result = 1, ""
    while offset is not None:
        page = agent.read_file("large.txt", offset=offset, max_result_chars=1000)
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        result += page["content"]
        offset = page["next_offset"]
    assert result == "".join(lines)
    target.write_bytes(b"\x00binary")
    with pytest.raises(ValueError, match="Binary"):
        agent.read_file("large.txt")


async def test_commands_return_cwd_stdout_stderr_exit_status_and_list(registry, tmp_path):
    await registry.set_agent_mode(True)
    result = await registry.call("run_command", {
        "command": "pwd; printf 'hello'; printf 'problem' >&2; exit 7", "cwd": str(tmp_path),
        "yield_time_ms": 1000,
    }, AGENT_CONTEXT)
    assert result["status"] == "completed" and result["exit_code"] == 7
    assert str(tmp_path) in result["stdout"] and result["stdout"].endswith("hello")
    assert result["stderr"] == "problem"
    listing = await registry.call("list_commands", {}, AGENT_CONTEXT)
    assert listing["commands"][0]["command_id"] == result["command_id"]
    assert (await registry.call("read_command_output", {"command_id": result["command_id"]},
                                AGENT_CONTEXT))["stdout"] == ""


@pytest.mark.parametrize("tty", [False, True])
async def test_running_command_accepts_input_and_can_be_polled(registry, tty):
    await registry.set_agent_mode(True)
    result = await registry.call("run_command", {
        "command": "read value" + (" </dev/tty" if tty else "") + "; printf 'answer=%s' \"$value\"",
        "tty": tty, "yield_time_ms": 0,
    }, AGENT_CONTEXT)
    assert result["status"] == "running"
    answer = await registry.call("write_command_input", {
        "command_id": result["command_id"], "text": "hello\n", "yield_time_ms": 1000,
    }, AGENT_CONTEXT)
    assert answer["status"] == "completed" and answer["exit_code"] == 0
    assert "answer=hello" in answer["stdout"]
    assert not answer["stderr"]


async def test_command_eof_and_large_output_are_preserved_across_bounded_reads(registry):
    await registry.set_agent_mode(True)
    script = "import sys; text=sys.stdin.read(); print(text * 2000, end='')"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(script)
    context = AGENT_CONTEXT | {"result_chars": 2000}
    started = await registry.call("run_command", {"command": command, "yield_time_ms": 0}, context)
    result = await registry.call("write_command_input", {
        "command_id": started["command_id"], "text": '"\\🌸', "close_stdin": True,
    }, context)
    output = ""
    while True:
        assert len(json.dumps(result, ensure_ascii=False)) <= 2000
        output += result["stdout"]
        if result["status"] == "completed" and not result["output_remaining"]:
            break
        result = await registry.call("read_command_output", {"command_id": started["command_id"]},
                                     context)
    assert output == '"\\🌸' * 2000


async def test_stop_timeout_mode_off_and_cancellation_clean_up_process_groups(registry, tmp_path):
    await registry.set_agent_mode(True)
    script = ("import os,time; from pathlib import Path; "
              f"Path({str(tmp_path / 'child.pid')!r}).write_text(str(os.getpid())); time.sleep(60)")
    started = await registry.call("run_command", {
        "command": shlex.quote(sys.executable) + " -c " + shlex.quote(script), "yield_time_ms": 100,
    }, AGENT_CONTEXT)
    process = registry.agent.commands[started["command_id"]].process
    stopped = await registry.call("stop_command", {"command_id": started["command_id"]}, AGENT_CONTEXT)
    assert stopped["status"] == "completed" and stopped["exit_code"] != 0
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
    timed = await registry.call("run_command", {
        "command": "sleep 60", "timeout": 1, "yield_time_ms": 1000,
    }, AGENT_CONTEXT)
    if timed["status"] != "completed":
        timed = await registry.call("read_command_output", {"command_id": timed["command_id"]},
                                    AGENT_CONTEXT)
    assert timed["timed_out"] and timed["status"] == "completed"
    active = await registry.call("run_command", {"command": "sleep 60", "yield_time_ms": 0},
                                 AGENT_CONTEXT)
    session = registry.agent.commands[active["command_id"]]
    await registry.set_agent_mode(False)
    assert session.done.is_set() and session.process.returncode is not None
    assert not registry.agent.commands
    await registry.set_agent_mode(True)
    assert (await registry.call("run_command", {"command": "printf 'back'"}, AGENT_CONTEXT))["stdout"] == "back"
    task = asyncio.create_task(registry.call("run_command", {
        "command": "sleep 60", "yield_time_ms": 10_000,
    }, AGENT_CONTEXT))
    while len(registry.agent.commands) < 2:
        await asyncio.sleep(0.01)
    session = list(registry.agent.commands.values())[-1]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.done.is_set() and session.process.returncode is not None


async def test_revocation_during_spawn_cannot_leave_an_unmanaged_command(registry):
    await registry.set_agent_mode(True)
    await registry.agent._spawn_lock.acquire()
    try:
        pending = asyncio.create_task(registry.call("run_command", {"command": "sleep 60"},
                                                   AGENT_CONTEXT))
        await asyncio.sleep(0)
        disabled = asyncio.create_task(registry.set_agent_mode(False))
        await asyncio.sleep(0)
    finally:
        registry.agent._spawn_lock.release()
    with pytest.raises(ValueError, match="Agent mode"):
        await pending
    assert await disabled is False
    assert not registry.agent.commands


async def test_queued_file_write_cannot_resume_after_mode_is_disabled_and_reenabled(
    registry, tmp_path, monkeypatch,
):
    await registry.set_agent_mode(True)
    waiting, release = asyncio.Event(), asyncio.Event()
    async def queued(function, *args, **kwargs):
        waiting.set()
        await release.wait()
        return function(*args, **kwargs)
    monkeypatch.setattr("eidolon.tools.registry.asyncio.to_thread", queued)
    target = tmp_path / "stale.txt"
    operation = asyncio.create_task(registry.call("write_file", {
        "path": str(target), "content": "stale",
    }, AGENT_CONTEXT))
    await waiting.wait()
    await registry.set_agent_mode(False)
    await registry.set_agent_mode(True)
    release.set()
    with pytest.raises(ValueError, match="Agent mode"):
        await operation
    assert not target.exists()


async def test_terminal_eof_finishes_input_without_requiring_a_newline(registry):
    await registry.set_agent_mode(True)
    script = "import sys; print('stdin=' + sys.stdin.read())"
    started = await registry.call("run_command", {
        "command": "stty -echo; printf ready; " + shlex.quote(sys.executable) + " -c " + shlex.quote(script),
        "tty": True, "yield_time_ms": 1000,
    }, AGENT_CONTEXT)
    assert started["status"] == "running" and "ready" in started["stdout"]
    result = await registry.call("write_command_input", {
        "command_id": started["command_id"], "text": "hello", "close_stdin": True,
    }, AGENT_CONTEXT)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert "stdin=hello" in result["stdout"]
