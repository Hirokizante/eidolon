import json
import plistlib
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from eidolon.memory.runtime import build_environment, ensure_database, write_launchagent
from eidolon.memory import runtime
from eidolon.memory import recovery


def test_independent_runtime_uses_own_credentials_and_scrubs_inherited_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/old/environment")
    monkeypatch.setenv("HERMES_HOME", "/old/profile")
    monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", "postgresql://wrong")
    settings = {"environment": {"HINDSIGHT_API_LLM_PROVIDER": "openai"},
                "credential_refs": {"HINDSIGHT_API_LLM_API_KEY": "keyring:memory-llm"},
                "database": {"username": "hindsight", "port": 5432, "database": "hindsight", "password_ref": "keyring:memory-db"}}
    environment = build_environment(settings, lambda ref: {"keyring:memory-llm": "secret-llm", "keyring:memory-db": "private-password"}[ref])
    assert "PYTHONPATH" not in environment and "HERMES_HOME" not in environment
    assert environment["HINDSIGHT_API_LLM_API_KEY"] == "secret-llm"
    assert environment["HINDSIGHT_API_DATABASE_URL"].endswith("@127.0.0.1:5432/hindsight")
    assert environment["HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP"] == "false"


def test_standby_database_manager_has_no_stop_or_drop_and_rejects_missing_identity(tmp_path):
    settings = {"runtime_python": "/own/runtime/python"}
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        assert "p.stop(" not in command[-1] and "p.drop(" not in command[-1]
        assert "refusing to create a replacement" in command[-1]
        return subprocess.CompletedProcess(command, 0, json.dumps({"running": True, "started": False, "pid": 123}), "")
    status = ensure_database(settings, tmp_path / "settings.json", {"EIDOLON_MEMORY_DATABASE_PASSWORD": "hidden"}, runner)
    assert status["started"] is False
    assert "hidden" not in str(commands)


def test_launchagent_contains_paths_and_credential_references_only(tmp_path):
    settings_path = tmp_path / "settings.json"
    path = write_launchagent(settings_path, python=tmp_path / "own-python", launchagents=tmp_path / "LaunchAgents")
    data = plistlib.loads(path.read_bytes())
    assert data["KeepAlive"] and data["RunAtLoad"]
    assert data["ProgramArguments"][-1] == str(settings_path)
    assert "EnvironmentVariables" not in data


class FakeProcess:
    def __init__(self, pid, *, hung=False):
        self.pid, self.hung = pid, hung
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.hung:
            self.returncode = -15

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("api", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    settings = {"api_binary": "/own/runtime/hindsight-api", "api_port": 9191}
    processes = []
    def spawn(command, **kwargs):
        process = FakeProcess(len(processes) + 100)
        processes.append(process)
        return process
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "ensure_database", lambda *_: {"running": True, "pid": 50})
    monkeypatch.setattr(runtime, "api_is_live", lambda _: True)
    instance = runtime.ApiSupervisor(settings, tmp_path / "settings.json", {})
    return instance, processes, clock


@pytest.mark.parametrize("exit_code", [0, 1, -9, -15])
def test_supervisor_restarts_api_for_any_exit(supervisor, exit_code):
    instance, processes, clock = supervisor
    instance.step()
    processes[0].returncode = exit_code
    instance.step()
    assert len(processes) == 2
    assert instance.api is processes[1]
    status = json.loads((instance.settings_path.parent / "status.json").read_text())
    assert status["restart_count"] == 1 and status["pid"] == 50


def test_watchdog_allows_startup_then_kills_a_hung_api(supervisor, monkeypatch):
    instance, processes, clock = supervisor
    monkeypatch.setattr(runtime, "api_is_live", lambda _: False)
    instance.step()
    processes[0].hung = True
    clock.now = runtime.STARTUP_GRACE - 1
    for _ in range(5):
        instance.step()
    assert len(processes) == 1 and not processes[0].terminated
    clock.now = runtime.STARTUP_GRACE + 1
    for _ in range(runtime.FAILURE_THRESHOLD):
        instance.step()
    assert processes[0].terminated and processes[0].killed
    assert len(processes) == 2


def test_transient_liveness_failures_do_not_restart_healthy_api(supervisor, monkeypatch):
    instance, processes, clock = supervisor
    instance.step()
    clock.now = runtime.STARTUP_GRACE + 1
    monkeypatch.setattr(runtime, "api_is_live", lambda _: False)
    instance.step()
    instance.step()
    monkeypatch.setattr(runtime, "api_is_live", lambda _: True)
    instance.step()
    monkeypatch.setattr(runtime, "api_is_live", lambda _: False)
    instance.step()
    assert len(processes) == 1 and not processes[0].terminated
    assert instance.failures == 1


@pytest.mark.parametrize("error", [RuntimeError("postgresql://private"), subprocess.TimeoutExpired("pg0", 90)])
def test_database_manager_failures_are_retried_without_stopping_api(supervisor, monkeypatch, error):
    instance, processes, clock = supervisor
    instance.step()
    def unavailable(*_):
        raise error
    monkeypatch.setattr(runtime, "ensure_database", unavailable)
    instance.step()
    assert len(processes) == 1 and not processes[0].terminated
    status = (instance.settings_path.parent / "status.json").read_text()
    assert "will retry" in status and "private" not in status
    monkeypatch.setattr(runtime, "ensure_database", lambda *_: {"running": True, "pid": 50})
    instance.step()
    assert json.loads((instance.settings_path.parent / "status.json").read_text())["error"] is None


def test_failed_api_launch_is_retried(supervisor, monkeypatch):
    instance, processes, clock = supervisor
    spawn = runtime.subprocess.Popen
    def unavailable(*args, **kwargs):
        raise OSError("private launcher details")
    monkeypatch.setattr(runtime.subprocess, "Popen", unavailable)
    instance.step()
    assert instance.api is None
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    instance.step()
    assert len(processes) == 1


@pytest.mark.parametrize("live_status,ready_status,expected", [(200, 503, True), (503, 200, False), (404, 503, True)])
def test_liveness_probe_does_not_treat_readiness_as_a_restart_signal(monkeypatch, live_status, ready_status, expected):
    client_class = httpx.Client
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(live_status if request.url.path.endswith("live") else ready_status)
    monkeypatch.setattr(runtime.httpx, "Client", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs))
    assert runtime.api_is_live({"api_port": 9191}) is expected
    assert paths[0] == "/health/live"


@pytest.fixture
def managed_service(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"api_port": 9191}))
    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    monkeypatch.setattr(recovery, "write_launchagent", lambda _: tmp_path / "agent.plist")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(recovery.subprocess, "run", run)
    return recovery.ManagedMemoryService("http://127.0.0.1:9191", settings), commands


@pytest.mark.asyncio
async def test_app_recovers_an_unloaded_supervisor_without_forcing_a_healthy_restart(managed_service, monkeypatch):
    service, commands = managed_service
    probes = iter([False, True])
    monkeypatch.setattr(recovery, "api_is_live", lambda _: next(probes))
    assert await service.recover()
    assert [command[1] for command in commands] == ["bootstrap", "kickstart"]
    assert all("-k" not in command for command in commands)
    monkeypatch.setattr(recovery, "api_is_live", lambda _: True)
    assert await service.recover()
    assert len(commands) == 2
    monkeypatch.setattr(recovery, "api_is_live", lambda _: False)
    assert not await service.recover()
    assert len(commands) == 2


@pytest.mark.parametrize("url", [
    "https://remote:9191", "http://127.0.0.1:9177", "http://127.0.0.1:9191/proxy",
    "http://user:password@127.0.0.1:9191", "http://:password@127.0.0.1:9191",
    "http://@127.0.0.1:9191", "http://127.0.0.1:9191?proxy=1", "http://127.0.0.1:9191#proxy",
])
@pytest.mark.asyncio
async def test_app_does_not_restart_unmanaged_memory_services(managed_service, monkeypatch, url):
    service, commands = managed_service
    service.url = url
    monkeypatch.setattr(recovery, "api_is_live", lambda _: pytest.fail("Unmanaged service was probed"))
    assert not await service.recover()
    assert not commands


@pytest.mark.asyncio
async def test_missing_runtime_settings_cannot_create_a_replacement(managed_service):
    service, commands = managed_service
    service.settings_path.unlink()
    assert not await service.recover()
    assert not commands
