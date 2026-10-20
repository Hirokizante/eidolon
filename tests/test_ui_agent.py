import pytest
from rich.cells import cell_len
from textual.widgets import Input, Markdown, Static

from eidolon.config import AppConfig, InterfacePreferences, ProviderConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import CommandMenu, HelpScreen, find_command, matching_commands
from eidolon.ui.status import status_line
from test_ui import FakeClient


class AgentClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.agent_mode = False
        self.unavailable = False

    async def request(self, method, **params):
        if method == "agent_mode":
            self.calls.append((method, params))
            if self.unavailable:
                raise RuntimeError("Service is unavailable")
            self.agent_mode = params.get("enabled", not self.agent_mode)
            return {"agent_mode": self.agent_mode}
        if method == "status" and self.unavailable:
            raise RuntimeError("Service is unavailable")
        result = await super().request(method, **params)
        if method == "status":
            result["agent_mode"] = self.agent_mode
        return result


@pytest.mark.parametrize("width", [76, 58, 46])
@pytest.mark.parametrize("ascii", [False, True])
def test_mode_indicator_stays_first_with_long_models_and_hidden_components(width, ascii):
    profile = ProviderConfig(model="模型e\u0301-" * 20, context_window=1_000_000)
    status = {"agent_mode": True}
    for components in (InterfacePreferences().status_components, [], ["memory", "avatar"]):
        line = status_line(profile, components, status, width=width, ascii=ascii).plain
        assert line.startswith("Agent mode") and cell_len(line) <= width
        if not components:
            assert line == "Agent mode"
    status["agent_mode"] = False
    assert "Agent mode" not in status_line(profile, ["model"], status, width=width).plain


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
@pytest.mark.parametrize("hidden", [False, True])
async def test_agent_toggle_keeps_indicator_visible_and_preserves_draft(size, hidden):
    client = AgentClient()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, preferences=InterfacePreferences(
        show_service_status=not hidden, status_components=[] if hidden else ["model", "context", "cache", "speed"],
    ))
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        status = app.query_one("#service-status", Static)
        assert "Agent mode" not in str(status.render())
        composer = app.query_one("#composer", Composer)
        composer.load_text("Unsent draft 🌸")
        composer.move_cursor((0, 5))
        selection = composer.selection
        app.execute_command("/agent")
        await pilot.pause()
        assert client.agent_mode and app.status["agent_mode"]
        assert status.display and str(status.render()).startswith("Agent mode")
        assert status.region.height == 1 and status.region.right <= app.size.width
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection
        assert app.focused is composer
        app.execute_command("/agent off")
        await pilot.pause()
        assert not client.agent_mode and not app.status["agent_mode"]
        assert "Agent mode" not in str(status.render())
        assert status.display is (not hidden)
        app.execute_command("/agent on")
        await pilot.pause()
        app.execute_command("/agent on")
        await pilot.pause()
        assert client.agent_mode  # Explicit commands are idempotent.
        app.execute_command("/agent")
        await pilot.pause()
        assert not client.agent_mode
        assert not any(method == "generate" for method, _ in client.calls)


async def test_agent_is_discoverable_through_help_palette_and_composer():
    assert find_command("agent").usage == "/agent [on|off]"
    assert matching_commands("/age")[0].name == "agent"
    client = AgentClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Composer)
        composer.load_text("/agent")
        app.submit()
        await pilot.pause()
        assert client.agent_mode and not app.busy and composer.text == ""
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandMenu)
        app.screen.query_one(Input).value = "agent"
        await pilot.press("enter")
        await pilot.pause()
        assert not client.agent_mode
        app.execute_command("/help")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert "/agent [on|off]" in app.screen.query_one(Markdown).source


async def test_invalid_or_failed_toggle_keeps_mode_unchanged_and_disconnect_retains_indicator():
    client = AgentClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        app.execute_command("/agent maybe")
        await pilot.pause()
        assert not client.agent_mode
        assert not any(method == "agent_mode" for method, _ in client.calls)
        app.execute_command("/agent on")
        await pilot.pause()
        client.unavailable = True
        app.execute_command("/agent off")
        await pilot.pause()
        assert app.status["agent_mode"] and client.agent_mode
        app.refresh_status()
        await pilot.pause()
        assert str(app.query_one("#service-status", Static).render()).startswith("Agent mode | Service disconnected")
