"""Exercise lifecycle contracts without touching the user's LaunchAgents or processes."""

import plistlib
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

from eidolon.config import AppConfig
from eidolon.service import launchd


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    config = AppConfig(state_dir=tmp_path / "state")
    configuration = tmp_path / "config.yaml"
    target = tmp_path / "LaunchAgents" / f"{launchd.LABEL}.plist"
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    monkeypatch.setattr(launchd, "plist_path", lambda: target)
    control = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(launchd, "launchctl", control)
    spawn = Mock()
    monkeypatch.setattr(launchd.subprocess, "Popen", spawn)
    request = AsyncMock(return_value={})
    monkeypatch.setattr(launchd, "RoomClient", Mock(return_value=Mock(request=request)))
    monkeypatch.setattr(launchd.asyncio, "sleep", AsyncMock())
    return config, configuration, target, control, spawn, request


def test_install_writes_login_agent_without_activating_it(lifecycle):
    config, configuration, target, control, spawn, _ = lifecycle
    assert launchd.install(config, configuration) == target
    payload = plistlib.loads(target.read_bytes())
    assert payload["RunAtLoad"] and payload["KeepAlive"]
    assert payload["ProgramArguments"] == [
        launchd.sys.executable, "-m", "eidolon", "--config", str(configuration), "service", "run",
    ]
    assert payload["StandardErrorPath"] == str(config.state_dir / "service.log")
    assert target.stat().st_mode & 0o777 == 0o600
    control.assert_not_called()
    spawn.assert_not_called()


async def test_start_reuses_running_service_without_reloading_agent(lifecycle):
    config, configuration, _, control, spawn, request = lifecycle
    launchd.install(config, configuration)
    await launchd.start(config, configuration)
    request.assert_awaited_once_with("status")
    control.assert_not_called()
    spawn.assert_not_called()


async def test_start_activates_installed_agent_and_waits_for_socket(lifecycle, monkeypatch):
    config, configuration, target, control, spawn, request = lifecycle
    launchd.install(config, configuration)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    request.side_effect = [ConnectionError("stopped"), {}]
    await launchd.start(config, configuration)
    assert [call.args for call in control.call_args_list] == [
        ("bootstrap", "gui/501", str(target)),
        ("kickstart", "gui/501/local.eidolon.service"),
    ]
    assert control.call_args_list[0].kwargs == {"check": False}
    spawn.assert_not_called()


async def test_start_without_agent_detaches_service_and_reports_failure_log(lifecycle):
    config, configuration, _, control, spawn, request = lifecycle
    request.side_effect = ConnectionError("stopped")
    with pytest.raises(RuntimeError, match="Service did not start.*service.log"):
        await launchd.start(config, configuration)
    assert spawn.call_args.args[0] == [
        launchd.sys.executable, "-m", "eidolon", "--config", str(configuration), "service", "run",
    ]
    assert spawn.call_args.kwargs["start_new_session"]
    assert spawn.call_args.kwargs["cwd"] == config.state_dir
    assert (config.state_dir / "service.log").stat().st_mode & 0o777 == 0o600
    control.assert_not_called()


async def test_stop_and_uninstall_preserve_state_and_only_remove_app_agent(lifecycle, monkeypatch):
    config, configuration, target, control, _, request = lifecycle
    launchd.install(config, configuration)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    database = config.database_path
    database.write_bytes(b"synthetic preserved state")
    await launchd.stop(config)
    assert target.exists()
    control.assert_called_once_with("bootout", "gui/501/local.eidolon.service", check=False)
    request.assert_awaited_once_with("shutdown")
    request.side_effect = ConnectionError("already stopped")
    await launchd.uninstall(config)
    assert not target.exists()
    assert database.read_bytes() == b"synthetic preserved state"
