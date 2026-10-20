"""Persistent official-SDK MCP clients, each owned by one task.

The owner task enters and exits SDK transport contexts so AnyIO cancel scopes
remain valid. An uncertain tool outcome is surfaced and never automatically
replayed; the next request may reconnect and rediscover the server.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta

import httpx

from eidolon.config import resolve_secret


class MCPError(RuntimeError):
    pass


def tool_name(server: str, tool: str) -> str:
    raw = f"mcp__{server}__{tool}"
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{clean[:53]}_{digest}"


def _settings(values: dict[str, str]) -> dict[str, str]:
    return {
        key: (resolve_secret(value) or "") if value.startswith(("env:", "keyring:")) else value
        for key, value in values.items()
    }


@asynccontextmanager
async def sdk_connection(config):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async with AsyncExitStack() as stack:
        if config.transport == "stdio":
            if not config.command:
                raise MCPError("Stdio MCP server has no configured command")
            streams = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=config.command, args=config.args, env=_settings(config.env)
                    )
                )
            )
        else:
            if not config.url:
                raise MCPError("HTTP MCP server has no configured URL")
            try:
                from mcp.client.streamable_http import streamable_http_client
            except ImportError:  # Supported older 1.x SDK releases.
                from mcp.client.streamable_http import streamablehttp_client

                transport = streamablehttp_client(
                    config.url, headers=_settings(config.headers), timeout=config.timeout
                )
            else:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=_settings(config.headers),
                        timeout=httpx.Timeout(config.timeout, read=300),
                        trust_env=False,
                    )
                )
                transport = streamable_http_client(config.url, http_client=client)
            streams = await stack.enter_async_context(transport)
        session = await stack.enter_async_context(
            ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=config.timeout)
            )
        )
        await session.initialize()
        yield session


def unpack_result(result) -> dict:
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structuredContent", None)
    content = [
        {"type": getattr(part, "type", "unknown"), "text": part.text[:30_000]}
        for part in getattr(result, "content", [])
        if hasattr(part, "text")
    ]
    data = {"content": content, "is_error": bool(getattr(result, "isError", False))}
    if structured is not None:
        data["structured"] = structured
    return data


class _Server:
    def __init__(self, config, connect):
        self.config, self.connect = config, connect
        self.queue = asyncio.Queue(maxsize=64)
        self.tools: dict = {}
        self.error: str | None = None
        self.generation = 0
        self.task: asyncio.Task | None = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"mcp-{self.config.name}")

    async def request(self, operation, *args):
        self.start()
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((future, operation, args))
        except asyncio.QueueFull:
            raise MCPError("MCP request queue is full") from None
        try:
            # A queue deadline prevents a slow server accumulating stale work.
            return await asyncio.wait_for(future, self.config.timeout + 2)
        except TimeoutError:
            raise MCPError("MCP deadline exceeded; command outcome may be unknown") from None

    async def _run(self):
        stack = AsyncExitStack()
        session = None
        current = None
        try:
            while True:
                future, operation, args = await self.queue.get()
                current = future
                if future.cancelled():
                    self.queue.task_done()
                    current = None
                    continue
                try:
                    if session is None:
                        async with asyncio.timeout(self.config.timeout):
                            session = await stack.enter_async_context(self.connect(self.config))
                            self.generation += 1
                            await self._discover(session)
                    async with asyncio.timeout(self.config.timeout):
                        if operation == "discover":
                            result = await self._discover(session)
                        else:
                            name, arguments = args
                            if name not in self.config.enabled_tools or name not in self.tools:
                                raise MCPError(
                                    "MCP tool is not explicitly enabled or was not discovered"
                                )
                            result = unpack_result(await session.call_tool(name, arguments))
                    self.error = None
                    if not future.done():
                        future.set_result(result)
                except MCPError as exc:
                    self.error = str(exc)
                    if not future.done():
                        future.set_exception(exc)
                except Exception as exc:
                    # Do not include SDK/transport exception text: it may contain headers.
                    self.error = f"MCP server unavailable ({type(exc).__name__}); command outcome may be unknown"
                    if not future.done():
                        future.set_exception(MCPError(self.error))
                    try:
                        await stack.aclose()
                    except Exception:
                        pass
                    stack, session = AsyncExitStack(), None
                finally:
                    self.queue.task_done()
                    current = None
        finally:
            try:
                await stack.aclose()
            except Exception:
                pass
            if current and not current.done():
                current.set_exception(MCPError("MCP connection closed"))
            while not self.queue.empty():
                future, _, _ = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(MCPError("MCP connection closed"))
                self.queue.task_done()

    async def _discover(self, session):
        tools = {}
        cursor = None
        for _ in range(20):
            result = (
                await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
            )
            for tool in result.tools:
                if tool.name in self.config.enabled_tools:
                    tools[tool.name] = tool
            cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
        self.tools = tools
        return sorted(tools)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


class MCPManager:
    def __init__(self, servers, *, connect=sdk_connection):
        configs = [config for config in servers if config.enabled]
        if len({config.name for config in configs}) != len(configs):
            raise ValueError("MCP server names must be unique")
        self.servers = {config.name: _Server(config, connect) for config in configs}

    async def start(self):
        # Optional integrations failing do not prevent chat from opening.
        await asyncio.gather(
            *(server.request("discover") for server in self.servers.values()),
            return_exceptions=True,
        )

    async def close(self):
        await asyncio.gather(*(server.close() for server in self.servers.values()))

    async def discover(self, server: str):
        if server not in self.servers:
            raise MCPError("MCP server is not configured or enabled")
        return await self.servers[server].request("discover")

    async def call(self, server: str, tool: str, arguments: dict):
        if server not in self.servers:
            raise MCPError("MCP server is not configured or enabled")
        if tool not in self.servers[server].config.enabled_tools:
            raise MCPError("MCP tool is not explicitly enabled")
        return await self.servers[server].request("call", tool, arguments)

    def schemas(self):
        return sorted([
            {
                "type": "function",
                "function": {
                    "name": tool_name(name, tool.name),
                    "description": (tool.description or f"{name}: {tool.name}")[:3000],
                    "parameters": tool.inputSchema,
                },
            }
            for name, server in self.servers.items()
            for tool in server.tools.values()
        ], key=lambda schema: schema["function"]["name"])

    def resolve(self, name: str) -> tuple[str, str] | None:
        for server_name, server in self.servers.items():
            for raw_name in server.tools:
                if tool_name(server_name, raw_name) == name:
                    return server_name, raw_name
        return None

    def status(self) -> dict:
        return {
            name: {"tools": sorted(server.tools), "error": server.error}
            for name, server in self.servers.items()
        }

    def generation(self, server: str) -> int:
        return self.servers[server].generation if server in self.servers else 0
