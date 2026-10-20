from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from eidolon.config import MCPServerConfig
from eidolon.mcp import MCPError, MCPManager, tool_name


def sample_tool(name):
    return SimpleNamespace(
        name=name, description="Test", inputSchema={"type": "object", "properties": {}}
    )


async def test_mcp_schema_order_survives_reordered_rediscovery_and_honors_permissions():
    discovered = [sample_tool("zebra"), sample_tool("read"), sample_tool("hidden")]

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=discovered)

    @asynccontextmanager
    async def connect(config):
        yield Session()

    manager = MCPManager([
        MCPServerConfig(name="z-server", command="irrelevant", enabled_tools=["zebra", "read"]),
        MCPServerConfig(name="a-server", command="irrelevant", enabled_tools=["read"]),
    ], connect=connect)
    try:
        await manager.start()
        first = manager.schemas()
        await manager.close()
        discovered.reverse()
        manager.servers = dict(reversed(list(manager.servers.items())))
        await manager.start()
        assert manager.schemas() == first
        names = [schema["function"]["name"] for schema in first]
        assert names == sorted(names)
        assert names == [tool_name("a-server", "read"), tool_name("z-server", "read"),
                         tool_name("z-server", "zebra")]
    finally:
        await manager.close()


async def test_persistent_discovery_explicit_enable_and_reconnect_no_replay():
    connections, calls = [], []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[sample_tool("read"), sample_tool("hidden")])

        async def call_tool(self, name, arguments):
            calls.append(name)
            if arguments.get("disconnect"):
                raise ConnectionError("secret transport details")
            return {"ok": True}

    @asynccontextmanager
    async def connect(config):
        owner = asyncio.current_task()
        connections.append(owner)
        try:
            yield Session()
        finally:
            assert asyncio.current_task() is owner

    manager = MCPManager(
        [MCPServerConfig(name="test", command="irrelevant", enabled_tools=["read"], timeout=0.5)],
        connect=connect,
    )
    try:
        await manager.start()
        assert [row["function"]["name"] for row in manager.schemas()] == [tool_name("test", "read")]
        await manager.call("test", "read", {})
        await manager.call("test", "read", {})
        assert len(connections) == 1
        with pytest.raises(MCPError, match="explicitly enabled"):
            await manager.call("test", "hidden", {})
        with pytest.raises(MCPError, match="unknown") as error:
            await manager.call("test", "read", {"disconnect": True})
        assert "secret" not in str(error.value)
        assert calls == ["read", "read", "read"]
        await manager.call("test", "read", {})
        assert len(connections) == 2 and len(calls) == 4
    finally:
        await manager.close()


async def test_timeout_is_contained_and_command_not_replayed():
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[sample_tool("slow")])

        async def call_tool(self, name, arguments):
            calls.append(name)
            await asyncio.sleep(1)

    @asynccontextmanager
    async def connect(config):
        yield Session()

    manager = MCPManager(
        [MCPServerConfig(name="offline", enabled_tools=["slow"], timeout=0.03)], connect=connect
    )
    try:
        await manager.start()
        with pytest.raises(MCPError):
            await manager.call("offline", "slow", {})
        assert calls == ["slow"]
        assert manager.status()["offline"]["error"]
    finally:
        await manager.close()


async def test_official_sdk_stdio_roundtrip(tmp_path):
    import sys

    server = tmp_path / "server.py"
    server.write_text(
        'from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("test")\n@mcp.tool()\ndef echo(text: str) -> str:\n    return text\nif __name__ == "__main__":\n    mcp.run()\n'
    )
    manager = MCPManager(
        [
            MCPServerConfig(
                name="real-sdk",
                command=sys.executable,
                args=[str(server)],
                enabled_tools=["echo"],
                timeout=10,
            )
        ]
    )
    try:
        await manager.start()
        assert manager.resolve(tool_name("real-sdk", "echo")) == ("real-sdk", "echo")
        result = await manager.call("real-sdk", "echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"
    finally:
        await manager.close()


async def test_official_sdk_streamable_http_roundtrip(tmp_path):
    import socket
    import sys

    import httpx

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = tmp_path / "http_server.py"
    server.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        f'mcp = FastMCP("test", host="127.0.0.1", port={port})\n'
        "@mcp.tool()\ndef echo(text: str) -> str:\n    return text\n"
        'if __name__ == "__main__":\n    mcp.run(transport="streamable-http")\n'
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(server),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    manager = MCPManager(
        [
            MCPServerConfig(
                name="http-sdk", transport="http", url=url, enabled_tools=["echo"], timeout=5
            )
        ]
    )
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=0.1) as probe:
            for _ in range(100):
                try:
                    await probe.get(url)
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.02)
            else:
                raise AssertionError("isolated MCP test server did not become available")
        await manager.start()
        result = await manager.call("http-sdk", "echo", {"text": "streamable hello"})
        assert result["content"][0]["text"] == "streamable hello"
    finally:
        await manager.close()
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
