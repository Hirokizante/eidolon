"""Recover only the Room's configured local Hindsight LaunchAgent."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import LABEL, api_is_live, write_launchagent


class ManagedMemoryService:
    def __init__(self, url: str, settings_path: Path):
        self.url, self.settings_path = url, settings_path
        self._lock = asyncio.Lock()
        self._last_attempt = float("-inf")

    async def recover(self) -> bool:
        async with self._lock:
            try:
                settings = json.loads(self.settings_path.read_text())
                url = urlsplit(self.url)
                if (sys.platform != "darwin" or url.scheme != "http"
                        or url.hostname not in {"127.0.0.1", "localhost"}
                        or url.port != settings["api_port"]
                        or url.username is not None or url.password is not None
                        or url.path not in {"", "/"} or url.query or url.fragment):
                    return False
                if await asyncio.to_thread(api_is_live, settings):
                    return True
                if time.monotonic() - self._last_attempt < 15:
                    return False
                self._last_attempt = time.monotonic()
                target = write_launchagent(self.settings_path)
                domain = f"gui/{os.getuid()}"
                # Bootstrap also restores an unloaded agent. Kickstart leaves an
                # active supervisor alone; its watchdog owns API restarts.
                await asyncio.to_thread(
                    subprocess.run, ["/bin/launchctl", "bootstrap", domain, str(target)],
                    capture_output=True, timeout=5,
                )
                result = await asyncio.to_thread(
                    subprocess.run, ["/bin/launchctl", "kickstart", f"{domain}/{LABEL}"],
                    capture_output=True, timeout=5,
                )
                if result.returncode:
                    return False
                # Give a stopped API a bounded chance to accept the retry. Cold
                # model loading may take longer; background checks keep trying.
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if await asyncio.to_thread(api_is_live, settings):
                        return True
                    await asyncio.sleep(0.5)
                return False
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                return False
