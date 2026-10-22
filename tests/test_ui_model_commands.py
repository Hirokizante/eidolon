import asyncio
from unittest.mock import Mock

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Input, OptionList, Select, Static, TextArea
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig, DEFAULT_MAX_RESPONSE_TOKENS, ProviderConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import find_command, matching_commands
from eidolon.ui.forms import FormModal
from eidolon.ui.models import NEW_PROVIDER, ConnectScreen, ModelScreen
from test_ui import FakeClient


class ModelClient(FakeClient):
    async def request(self, method, **params):
        if method == "list_models":
            self.calls.append((method, params))
            return {"models": ["alpha", "beta"], "error": None}
        if method == "select_model":
            self.calls.append((method, params))
            config = AppConfig.model_validate(self.configuration)
            config.provider = params["name"]
            profile = config.selected_provider()
            profile.model = params["model"]
            if params["reasoning_effort"] is None:
                profile.parameters.pop("reasoning_effort", None)
            else:
                profile.parameters["reasoning_effort"] = params["reasoning_effort"]
            self.configuration = config.model_dump(mode="json")
            return self.configuration
        if method == "save_provider":
            self.calls.append((method, params))
            config = AppConfig.model_validate(self.configuration)
            profile = ProviderConfig.model_validate(params["provider"])
            profile.model = profile.model or "alpha"
            profile.models = ["alpha", "beta"]
            config.providers.append(profile)
            self.configuration = config.model_dump(mode="json")
            return {"config": self.configuration, "models": profile.models, "warning": None}
        return await super().request(method, **params)


def test_new_commands_are_discoverable():
    assert find_command("connect").description
    assert find_command("models").description
    assert matching_commands("/connect")[0].name == "connect"
    assert matching_commands("reasoning effort")[0].name == "models"
    assert find_command("provider") is None
    assert find_command("providers") is None
    assert {command.name for command in matching_commands("provider")} == {
        "connect", "models", "check",
    }


@pytest.mark.parametrize("command", ["/provider", "/provider other"])
async def test_removed_provider_command_is_rejected(command, monkeypatch):
    client = ModelClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        notify = Mock()
        monkeypatch.setattr(app, "notify", notify)
        await app.execute_command(command).wait()
        notify.assert_called_once_with("Unknown command. Use /help.", severity="warning")
        assert app.screen is screen
        assert not any(method == "select_provider" for method, _ in client.calls)


async def test_models_command_switches_provider_profile():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[
        ProviderConfig(model="alpha"), ProviderConfig(name="other", model="beta"),
    ])
    client = ModelClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.execute_command("/models")
        await pilot.pause()
        app.screen.query_one("#model-provider", Select).value = "other"
        await pilot.pause()
        assert app.screen.query_one("#model-id", Input).value == "beta"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, ModelScreen)
        assert app.config.provider == "other"
        assert app.config.selected_provider().model == "beta"
        assert ("select_model", {"name": "other", "model": "beta",
                                 "reasoning_effort": None, "context_window": None,
                                 "max_response_tokens": DEFAULT_MAX_RESPONSE_TOKENS}) in client.calls


@pytest.mark.parametrize("command,screen_type", [
    ("models", ModelScreen), ("connect", ConnectScreen),
])
@pytest.mark.parametrize("size", [(80, 24), (60, 24), (48, 12)])
async def test_provider_menus_open_before_service_requests_complete(
    command, screen_type, size, monkeypatch,
):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[ProviderConfig(model="alpha", models=["alpha", "cached-model"])])
    client = ModelClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("next message")
        gate = asyncio.Event()
        pending = []
        request = client.request

        async def delayed_request(method, **params):
            if method in {"overview", "list_models"}:
                pending.append(method)
                await gate.wait()
            return await request(method, **params)

        monkeypatch.setattr(client, "request", delayed_request)
        worker = app.execute_command("/" + command)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, screen_type)
        assert worker.is_finished
        assert not gate.is_set()
        assert not screen.loading
        # CPU-idle pause is bounded and can expire during CI layout work.
        # Drain scheduled screen work while discovery is still blocked.
        await pilot.wait_for_scheduled_animations()
        field = app.focused
        visible_field = screen.get_widget_at(*field.region.center)[0]
        assert visible_field is field or field in visible_field.ancestors, (
            field, field.region, screen.query_one(".form-body").region, visible_field,
        )
        if command != "connect":
            assert pending == ["list_models"]
            options = screen.query_one("#model-options", OptionList)
            assert any(
                options.get_option_at_index(i).id == "cached-model"
                for i in range(options.option_count)
            )
            screen.query_one("#model-filter", Input).value = "cached"
            await pilot.pause()
            await pilot.press("enter")
            assert screen.query_one("#model-id", Input).value == "cached-model"
        else:
            assert pending == []
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer
        assert composer.text == "next message"


@pytest.mark.parametrize("command,screen_type", [
    ("connect", ConnectScreen), ("models", ModelScreen),
])
async def test_model_commands_preserve_composer_and_prompt_drafts(command, screen_type):
    client = ModelClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("next message 🌸")
        app.action_open_tab("prompts")
        editor = app.query_one("#edit-SYSTEM", TextArea)
        editor.load_text("unfinished prompt")
        app.execute_command("/" + command)
        await pilot.pause()
        assert isinstance(app.screen, screen_type)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, screen_type)
        assert composer.text == "next message 🌸"
        assert editor.text == "unfinished prompt"
        assert not any(method in {"generate", "save_settings", "save_provider", "select_model"}
                       for method, _ in client.calls)


@pytest.mark.parametrize("command", ["models", "connect"])
@pytest.mark.parametrize("size", [(100, 32), (80, 24), (60, 24), (48, 12)])
async def test_provider_menus_keep_chat_composer_and_flat_controls_visible(command, size):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ModelClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("a draft")
        app.execute_command("/" + command)
        await pilot.pause()
        screen = app.screen
        dialog = screen.query_one(".form-dialog")
        body = screen.query_one(".form-body")
        hint = screen.query_one(".form-hint")
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        assert screen.styles.background.a < 1
        assert body.region.height >= 1
        assert app.focused.region.overlaps(body.region)
        assert hint.region.bottom <= dialog.content_region.bottom
        for button in screen.query(Button):
            assert button.region.height == 1
            assert button.region.right <= dialog.content_region.right
            assert button.region.bottom <= hint.region.y
            assert not any(edge[0] for edge in button.styles.border)
        # Walk every focusable control, including ones in the scrolled form.
        for _ in range(len(screen.focus_chain) + 1):
            await pilot.press("tab")
            assert app.focused in screen.walk_children()
        await pilot.press("escape")
        assert app.focused is composer
        assert composer.text == "a draft"


@pytest.mark.parametrize("size,label_visible", [
    ((48, 12), False), ((60, 24), True), ((80, 24), True),
])
async def test_connect_initial_selector_is_visible_before_deferred_focus_scroll(
    size, label_visible, monkeypatch,
):
    # Anchoring shrinks the initial form to one body row at the floor. Its first
    # control must be visible before any later reveal callback scrolls that row.
    monkeypatch.setattr(FormModal, "reveal_focus", lambda self: None)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ModelClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.execute_command("/connect")
        await pilot.pause()
        screen = app.screen
        body = screen.query_one("#connect-body")
        selector = screen.query_one("#connect-provider", Select)
        label = screen.query_one("#connect-provider-label", Static)
        assert label.display is label_visible
        assert app.focused is selector and selector.region.overlaps(body.region)
        if not label_visible:
            assert next(child for child in body.children if child.display) is selector
            assert selector.region.y == body.region.y


@pytest.mark.parametrize("command", ["models", "connect"])
async def test_provider_menu_close_retains_unsaved_edits_for_reopening(command):
    client = ModelClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        app.execute_command("/" + command)
        await pilot.pause()
        if command == "models":
            app.screen.query_one("#model-id", Input).value = "draft-model"
            app.screen.query_one("#model-filter", Input).value = "beta"
            app.screen.query_one("#model-effort", Select).value = "high"
        else:
            app.screen.query_one("#connect-provider", Select).value = NEW_PROVIDER
            await pilot.pause()
            app.screen.query_one("#connect-url", Input).value = "https://draft.example/v1"
            app.screen.query_one("#connect-key", Input).value = "draft-secret"
            app.screen.query_one("#connect-headers", TextArea).load_text('{"X-App": "draft"}')
        await pilot.press("f1", "escape")
        app.execute_command("/" + command)
        await pilot.pause()
        if command == "models":
            assert app.screen.query_one("#model-id", Input).value == "draft-model"
            assert app.screen.query_one("#model-filter", Input).value == "beta"
            assert app.screen.query_one("#model-effort", Select).value == "high"
        else:
            assert app.screen.query_one("#connect-provider", Select).value == NEW_PROVIDER
            assert app.screen.query_one("#connect-url", Input).value == "https://draft.example/v1"
            key = app.screen.query_one("#connect-key", Input)
            assert key.value == "draft-secret" and key.password
            assert app.screen.query_one("#connect-headers", TextArea).text == '{"X-App": "draft"}'
        assert not any(method in {"save_provider", "select_model"} for method, _ in client.calls)


@pytest.mark.parametrize("command", ["models", "connect"])
async def test_provider_menu_preserves_composer_selection_and_conversation_scroll(command):
    client = ModelClient()
    client.messages = [{"role": "user", "content": "Earlier line\n" * 40}]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("next message 🌸")
        composer.selection = Selection((0, 2), (0, 8))
        conversation = app.query_one("#conversation", VerticalScroll)
        conversation.scroll_to(y=4, animate=False)
        await pilot.pause()
        before = (composer.selection, conversation.scroll_y)
        app.execute_command("/" + command)
        await pilot.pause()
        await pilot.press("x", "ctrl+enter")
        assert composer.text == "next message 🌸"
        assert not any(method == "generate" for method, _ in client.calls)
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer
        assert (composer.selection, conversation.scroll_y) == before


@pytest.mark.parametrize("command", ["models", "connect"])
async def test_provider_menu_tracks_composer_when_resizing_and_keeps_draft(command):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ModelClient())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("next message")
        app.execute_command("/" + command)
        await pilot.pause()
        field_id = "model-id" if command == "models" else "connect-display"
        app.screen.query_one("#" + field_id, Input).value = "unfinished"
        for size in [(80, 24), (60, 24), (48, 12), (40, 10), (80, 24)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#" + field_id, Input).value == "unfinished"
            if size == (40, 10):
                assert screen.has_class("too-small")
                assert screen.query_one(".form-too-small").display
            else:
                assert not screen.has_class("too-small")
                assert screen.query_one(".form-dialog").region.bottom == composer.region.y
        await pilot.press("escape")
        assert composer.text == "next message"


async def test_provider_menu_ascii_and_monochrome_keep_focus_and_checked_state(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ModelClient())
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        app.execute_command("/connect")
        await pilot.pause()
        screen = app.screen
        assert screen.has_class("monochrome") and screen.has_class("ascii")
        assert str(screen.query_one("Select .down-arrow", Static).render()) == "v"
        screen.query_one("#connect-advanced", Collapsible).collapsed = False
        checkbox = screen.query_one("#connect-tools", Checkbox)
        checkbox.focus()
        await pilot.pause()
        assert str(checkbox.render()) == "[x] Enable tool calling"
        assert checkbox.render().style == "bold reverse"
        await pilot.press("space")
        assert str(checkbox.render()) == "[ ] Enable tool calling"
        hint = str(screen.query_one(".form-hint", Static).render())
        assert hint.isascii() and "Esc keeps draft" in hint


async def test_model_picker_can_open_during_a_response_without_interrupting_it():
    client = ModelClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.busy
        composer.load_text("next message")
        app.execute_command("/models")
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen)
        assert app.busy
        picker = app.screen
        focused = app.focused
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert app.screen is picker
        assert app.focused is focused
        await pilot.press("escape")
        assert composer.text == "next message"
        assert not any(method == "cancel" for method, _ in client.calls)


async def test_model_dialog_shortcuts_save_selection_and_preserve_background_editor():
    client = ModelClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_tab("prompts")
        editor = app.query_one("#edit-SYSTEM", TextArea)
        editor.load_text("unfinished prompt")
        app.execute_command("/models")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#model-id", Input).value = "manual-model"
        screen.query_one("#model-effort", Select).value = "high"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.screen is screen
        assert screen.query_one("#model-id", Input).value == "manual-model"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, ModelScreen)
        assert app.config.selected_provider().model == "manual-model"
        assert app.config.selected_provider().parameters["reasoning_effort"] == "high"
        assert ("select_model", {"name": "local", "model": "manual-model",
                                 "reasoning_effort": "high", "context_window": None,
                                 "max_response_tokens": DEFAULT_MAX_RESPONSE_TOKENS}) in client.calls
        assert not any(method in {"save_settings", "latest_model_request"} for method, _ in client.calls)
        assert editor.text == "unfinished prompt"


async def test_connect_hands_new_connection_to_model_picker_before_activation():
    client = ModelClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("next message")
        app.execute_command("/connect")
        await pilot.pause()
        app.screen.query_one("#connect-provider", Select).value = NEW_PROVIDER
        await pilot.pause()
        app.screen.query_one("#connect-name", Input).value = "new-server"
        app.screen.query_one("#connect-url", Input).value = "http://127.0.0.1:1234/v1"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen)
        assert app.screen.query_one("#model-provider", Select).value == "new-server"
        assert app.config.provider == "local"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.provider == "new-server"
        assert app.config.selected_provider().model == "alpha"
        assert composer.text == "next message"
        assert not any(method == "generate" for method, _ in client.calls)
