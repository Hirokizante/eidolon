from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator


class ServiceUnavailable(ConnectionError):
    pass


class RoomClient:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    async def stream(self, method: str, **params) -> AsyncIterator[dict]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    str(self.socket_path),
                    limit=(64_000_000 if method in {"model_request", "latest_model_request"}
                           else 2_000_000),
                ), 3
            )
        except (OSError, TimeoutError) as exc:
            raise ServiceUnavailable("Room service is unavailable. Run eidolon service start.") from exc
        try:
            writer.write((json.dumps({"method": method, "params": params}) + "\n").encode())
            await writer.drain()
            while line := await reader.readline():
                event = json.loads(line)
                if event.get("type") == "done":
                    break
                yield event
        finally:
            writer.close()
            await writer.wait_closed()

    async def request(self, method: str, **params):
        result = None
        async for event in self.stream(method, **params):
            if event.get("type") == "error":
                raise RuntimeError(event.get("message", "Service request failed"))
            if event.get("type") == "result":
                result = event["data"]
        return result
