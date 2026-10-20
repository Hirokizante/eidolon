from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from eidolon.config import AppConfig
from .client import RoomClient

LABEL = "local.eidolon.service"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def install(config: AppConfig, configuration: Path) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent installation requires macOS")
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "eidolon", "--config", str(configuration), "service", "run"],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "WorkingDirectory": str(config.state_dir),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "StandardOutPath": str(config.state_dir / "service.log"),
        "StandardErrorPath": str(config.state_dir / "service.log"),
        "ProcessType": "Background",
    }
    target.write_bytes(plistlib.dumps(payload))
    target.chmod(0o600)
    return target


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, check=check)


async def start(config: AppConfig, configuration: Path) -> None:
    client = RoomClient(config.socket_path)
    try:
        await client.request("status")
        return
    except (ConnectionError, RuntimeError):
        pass
    target = plist_path()
    if sys.platform == "darwin" and target.exists():
        domain = f"gui/{os.getuid()}"
        launchctl("bootstrap", domain, str(target), check=False)
        launchctl("kickstart", f"{domain}/{LABEL}")
    else:
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = config.state_dir / "service.log"
        with log_path.open("a") as log:
            subprocess.Popen(
                [sys.executable, "-m", "eidolon", "--config", str(configuration), "service", "run"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                cwd=config.state_dir,
            )
        log_path.chmod(0o600)
    for _ in range(100):
        await asyncio.sleep(0.1)
        try:
            await client.request("status")
            return
        except (ConnectionError, RuntimeError):
            continue
    raise RuntimeError(f"Service did not start. Inspect {config.state_dir / 'service.log'}")


async def stop(config: AppConfig) -> None:
    target = plist_path()
    if sys.platform == "darwin" and target.exists():
        launchctl("bootout", f"gui/{os.getuid()}/{LABEL}", check=False)
    try:
        await RoomClient(config.socket_path).request("shutdown")
    except (ConnectionError, RuntimeError):
        pass


async def uninstall(config: AppConfig) -> None:
    await stop(config)
    plist_path().unlink(missing_ok=True)
