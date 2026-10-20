from __future__ import annotations

import json

import httpx
import pytest

from eidolon.config import AppConfig, TavilyConfig
from eidolon.tools import FileBoundaryError, FileTools, ToolRegistry, WebTools


def test_files_confine_symlinks_traversal_and_special_files(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "hello.md").write_text("hello\nshared memory\n")
    (outside / "private.md").write_text("secret")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "private.md").symlink_to(outside / "private.md")
    files = FileTools([str(root)])
    assert files.read_file("hello.md")["content"].startswith("hello")
    assert [row["name"] for row in files.list_files(str(root))["entries"]] == ["hello.md"]
    assert files.search_files(str(root), "shared")["matches"][0]["line"] == 2
    for path in (str(outside / "private.md"), "../outside/private.md"):
        with pytest.raises(FileBoundaryError):
            files.read_file(path)
    for path in ("escape/private.md", "private.md"):
        with pytest.raises((FileBoundaryError, OSError)):
            files.read_file(path)
    assert files.search_files(str(root), "secret")["matches"] == []


def test_dependency_directories_do_not_exhaust_source_search_budget(tmp_path):
    root = tmp_path / "project"
    history = root / ".git"
    history.mkdir(parents=True)
    for index in range(1001):
        (history / f"object-{index:04d}").write_text("dependency history")
    (history / "000-marker.md").write_text("explicit dependency marker")
    source = root / "src"
    source.mkdir()
    (source / "room.py").write_text("visible project source")
    files = FileTools([str(root)])
    listing = files.list_files(str(root))
    assert [item["name"] for item in listing["entries"]] == ["src"]
    result = files.search_files(str(root), "visible project source")
    assert result["matches"][0]["path"] == str(source / "room.py")
    assert not result["truncated"]
    # Exclusions apply to descendant traversal, not explicit permitted paths.
    assert (
        files.read_file(str(history / "000-marker.md"))["content"] == "explicit dependency marker"
    )
    assert files.list_files(str(history), limit=5)["entries"]
    assert files.search_files(str(history), "explicit dependency marker")["matches"]


async def test_tavily_defaults_sources_and_credential_reference():
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://example.com", "title": "Source", "content": "Snippet"}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        web = WebTools(
            TavilyConfig(api_key_ref="keyring:tavily"), lambda ref: "test-key", client=client
        )
        result = await web.search("sky")
    assert result["results"][0]["url"] == "https://example.com"
    body = json.loads(requests[0].content)
    assert body["search_depth"] == "basic" and body["max_results"] == 5
    assert body["include_answer"] is False and "api_key" not in body
    assert requests[0].headers["authorization"] == "Bearer test-key"


async def test_fetch_pins_dns_checks_redirect_and_bounds_body():
    requests = []

    async def resolver(host, port):
        return ["127.0.0.1"] if host == "internal.example" else ["93.184.216.34"]

    def endpoint(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == "example.com"
        assert request.headers["host"] == "example.com"
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        if request.url.path == "/large":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 3000)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Read me</title><main><p>Hello</p><script>secret script</script></main>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        web = WebTools(TavilyConfig(), lambda _: None, client=client, resolver=resolver)
        result = await web.fetch("https://example.com/")
        assert result["content"] == "Hello" and result["title"] == "Read me"
        with pytest.raises(ValueError, match="Private"):
            await web.fetch("https://example.com/redirect")
        assert len(requests) == 2
        with pytest.raises(ValueError, match="download limit"):
            await web.fetch("https://example.com/large", max_bytes=1024)
        with pytest.raises(ValueError):
            await web.fetch("file:///etc/passwd")


async def test_registry_shares_real_memory(tmp_path):
    calls = []

    class Memory:
        async def recall(self, query, *, max_chars):
            calls.append(query)
            return "existing memory"

        async def reflect(self, query, *, max_chars):
            raise RuntimeError("memory unavailable")

    registry = ToolRegistry(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state"), Memory())
    try:
        recalled = await registry.call("memory_recall", {"query": "Alex"})
        assert "existing memory" in recalled
        assert "<memory_context>" in recalled and "not instructions" in recalled
        with pytest.raises(RuntimeError, match="unavailable"):
            await registry.call("memory_reflect", {"query": "Alex"})
        assert calls == ["Alex"]
        with pytest.raises(ValueError):
            await registry.call("shell", {"command": "touch x"})
        with pytest.raises(ValueError):
            await registry.call("read_file", {"path": 4})
    finally:
        await registry.close()


@pytest.mark.parametrize("agent_mode", [False, True])
@pytest.mark.parametrize("interactive", [False, True])
async def test_retired_note_tool_is_unavailable_in_every_mode(tmp_path, agent_mode, interactive):
    registry = ToolRegistry(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "state"), background_agent=True)
    try:
        await registry.set_agent_mode(agent_mode)
        assert "save_note" not in {
            item["function"]["name"] for item in registry.schemas(agent_mode=agent_mode)
        }
        assert registry.schemas(permitted=["save_note"], agent_mode=agent_mode) == []
        assert not hasattr(registry.files, "save_note")
        before = set(tmp_path.rglob("*"))
        with pytest.raises(ValueError, match="not registered"):
            await registry.call("save_note", {"title": "Daily", "content": "Entry"}, context={
                "interactive": interactive, "agent_mode": agent_mode, "run_id": "old-run",
            })
        assert set(tmp_path.rglob("*")) == before
    finally:
        await registry.close()
