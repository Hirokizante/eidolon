import asyncio
from xml.etree.ElementTree import fromstring

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, ContentSwitcher, Input, Select, Static, TextArea
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import find_command, matching_commands
from eidolon.ui.preferences import PreferencesScreen
from test_ui import FakeClient


def test_preferences_replaces_settings_in_all_command_discovery():
    assert find_command("preferences").key == "alt+6"
    assert find_command("settings") is None
    assert matching_commands("/pref")[0].name == "preferences"


@pytest.mark.parametrize("size", [(140, 40), (80, 24), (60, 18), (48, 12)])
async def test_preferences_modal_keyboard_navigation_and_draft_restoration(size):
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("my next message 🌸")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        await pilot.press("alt+6")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PreferencesScreen)
        assert app.current_page == "room"
        assert not list(screen.query(TextArea))
        dialog = screen.query_one(".form-dialog")
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        assert screen.query_one("#preference-conversation").region.height >= 1
        for button in screen.query(".form-actions Button"):
            if not button.display:
                continue
            assert button.region.height == 1
            assert button.region.bottom <= dialog.content_region.bottom
            assert button.region.right <= dialog.content_region.right
        await pilot.press("tab", "tab")
        assert app.focused is screen.query_one("#pref-follow_replies", Checkbox)
        await pilot.press("space")
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer and composer.selection == selection
        assert composer.text == "my next message 🌸"
        assert not any(method == "save_preferences" for method, _ in client.calls)
        await pilot.press("alt+6")
        await pilot.pause()
        assert not app.screen.query_one("#pref-follow_replies", Checkbox).value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert not app.config.preferences.follow_replies
        assert ("save_preferences", {"preferences": {"follow_replies": False}}) in client.calls
        assert app.focused is composer and composer.text == "my next message 🌸"


async def test_preferences_apply_keys_status_and_daily_values_and_preserve_other_changes():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"})
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.execute_command("/preferences")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#pref-bindings", Select).value = "ctrl+enter"
        screen.query_one("#pref-show_service_status", Checkbox).value = False
        screen.query_one("#preference-category", Select).value = "daily"
        screen.query_one("#pref-timezone", Input).value = "UTC"
        screen.query_one("#pref-reset_hour", Input).value = "7"
        client.configuration["providers"][0]["model"] = "changed-elsewhere"
        client.configuration["theme"]["name"] = "nord"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.timezone == "UTC" and app.config.reset_hour == 7
        assert app.config.selected_provider().model == "changed-elsewhere"
        assert app.config.theme.name == "nord"
        assert not app.query_one("#service-status", Static).display
        assert app.config.bindings == {"send": "ctrl+enter", "newline": "enter"}
        composer = app.query_one(Composer)
        composer.load_text("first line")
        composer.move_cursor((0, 10))
        await pilot.press("enter")
        assert composer.text == "first line\n"
        assert not any(method == "generate" for method, _ in client.calls)


@pytest.mark.parametrize("field,value,group", [
    ("timezone", "Not/AZone", "daily"), ("reset_hour", "24", "daily"),
    ("max_memories", "many", "memory"),
    ("continuity_batch_exchanges", "33", "continuity"),
    ("continuity_idle_seconds", "0", "continuity"),
    ("continuity_max_notes", "0", "continuity"),
    ("continuity_max_notes", "65", "continuity"),
])
async def test_preferences_invalid_values_reveal_field_without_saving(field, value, group):
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        screen.query_one(f"#pref-{field}", Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen
        assert screen.query_one("#preference-pages", ContentSwitcher).current == "preference-" + group
        assert screen.query_one(f"#pref-{field}-error").has_class("problem")
        assert screen.query_one(f"#pref-{field}-error").region.overlaps(
            screen.query_one(f"#preference-{group}").region)
        assert not any(method == "save_preferences" for method, _ in client.calls)


async def test_preferences_discard_edits_and_no_change_save_do_not_write():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#pref-follow_replies", Checkbox).value = False
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.query_one("#pref-follow_replies", Checkbox).value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert not any(method == "save_preferences" for method, _ in client.calls)


async def test_preferences_save_failure_keeps_draft_and_retry_stays_responsive():
    class FailingClient(FakeClient):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def request(self, method, **params):
            if method == "save_preferences":
                self.started.set()
                await self.gate.wait()
                raise RuntimeError("Service unavailable; retry saving.")
            return await super().request(method, **params)

    client = FailingClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#pref-follow_replies", Checkbox).value = False
        await pilot.press("ctrl+s")
        await client.started.wait()
        await pilot.press("ctrl+s")
        await pilot.press("escape")
        assert app.screen is screen
        client.gate.set()
        await pilot.pause()
        assert screen.query_one(".form-status").has_class("problem")
        assert not screen.query_one("#preferences-apply", Button).disabled
        await pilot.press("escape", "alt+6")
        await pilot.pause()
        assert not app.screen.query_one("#pref-follow_replies", Checkbox).value


async def test_preferences_resize_ascii_and_category_focus_remain_usable(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        assert screen.has_class("monochrome") and screen.has_class("ascii")
        category = screen.query_one("#preference-category", Select)
        category.value = "web"
        await pilot.pause()
        screen.query_one("#web-tavily-max_results", Input).focus()
        for size in [(60, 18), (48, 12), (40, 10), (80, 24)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            if size[0] < 48:
                assert screen.query_one(".form-too-small").display
                assert not screen.query_one(".form-dialog").display
            else:
                assert screen.query_one("#web-tavily-max_results").region.overlaps(
                    screen.query_one("#preference-web").region)
        assert category.value == "web"
        assert screen.query_one("#web-tavily-max_results", Input).value == "5"


async def test_preference_draft_does_not_overwrite_preferences_changed_elsewhere():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        app.screen.query_one("#pref-follow_replies", Checkbox).value = False
        await pilot.press("escape")
        client.configuration["reset_hour"] = 8
        app.refresh_data()
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        assert app.screen.query_one("#pref-reset_hour", Input).value == "8"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.reset_hour == 8 and not app.config.preferences.follow_replies
        assert ("save_preferences", {"preferences": {"follow_replies": False}}) in client.calls


async def test_disabled_reply_following_does_not_scroll_new_assistant_message(monkeypatch):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, preferences={"follow_replies": False})
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    assistant_scroll = []
    append = app.append_message

    async def observe_message(role, text, *, scroll=True, **kwargs):
        if role == "assistant":
            assistant_scroll.append(scroll)
        return await append(role, text, scroll=scroll, **kwargs)

    monkeypatch.setattr(app, "append_message", observe_message)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Composer).load_text("hello")
        await pilot.press("enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert assistant_scroll == [False]


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_identity_preferences_keyboard_apply_preserves_chat_state(size):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity={"user_name": "Aster", "companion_name": "Moss"})
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    client.messages = [{"role": "user", "content": f"Earlier message {index}"}
                       for index in range(16)]
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent message 🌱")
        composer.selection = Selection((0, 2), (0, 6))
        selection = composer.selection
        conversation = app.query_one("#conversation", VerticalScroll)
        conversation.scroll_home(animate=False)
        await pilot.pause()
        position, frames = conversation.scroll_y, list(conversation.children)
        await pilot.press("alt+6", "enter", "home", "enter", "tab")
        screen = app.screen
        assert isinstance(screen, PreferencesScreen)
        assert screen.state.category == "identity"
        user = screen.query_one("#pref-user_name", Input)
        companion = screen.query_one("#pref-companion_name", Input)
        assert (user.value, companion.value) == ("Aster", "Moss")
        assert app.focused is user
        assert user.region.overlaps(screen.query_one("#preference-identity").region)
        await pilot.press("end", "ctrl+u", *"Kai", "tab")
        assert app.focused is companion
        assert companion.region.overlaps(screen.query_one("#preference-identity").region)
        await pilot.press("end", "ctrl+u", *"Fern", "escape")
        await pilot.pause()
        assert app.focused is composer and composer.selection == selection
        assert composer.text == "Unsent message 🌱"
        assert conversation.scroll_y == position and list(conversation.children) == frames
        assert app.config.identity.model_dump() == {"user_name": "Aster", "companion_name": "Moss"}
        assert not any(method == "save_preferences" for method, _ in client.calls)
        await pilot.press("alt+6")
        await pilot.pause()
        assert app.screen.query_one("#pref-user_name", Input).value == "Kai"
        assert app.screen.query_one("#pref-companion_name", Input).value == "Fern"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert app.config.identity.model_dump() == {"user_name": "Kai", "companion_name": "Fern"}
        assert client.configuration["identity"] == app.config.identity.model_dump()
        assert ("save_preferences", {"preferences": {
            "user_name": "Kai", "companion_name": "Fern",
        }}) in client.calls
        assert app.focused is composer
        assert composer.text == "Unsent message 🌱" and composer.selection == selection
        assert conversation.scroll_y == position
        assert list(conversation.children) == frames


@pytest.mark.parametrize("field", ["user_name", "companion_name"])
@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12), (80, 12)])
async def test_identity_preferences_validate_beside_field_without_echoing_input(field, size):
    value = "private-test-marker\u202e"
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        control = screen.query_one(f"#pref-{field}", Input)
        control.value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen
        assert screen.state.category == "identity"
        assert app.focused is control and control.value == value
        error = screen.query_one(f"#pref-{field}-error", Static)
        assert error.has_class("problem")
        if screen.has_class("short"):
            dialog = screen.query_one(".form-dialog")
            assert str(error.render()) == dialog.border_subtitle
            label = "Your name" if field == "user_name" else "Companion's name"
            assert label in dialog.border_title
            assert control.region.overlaps(screen.query_one("#preference-identity").region)
        else:
            assert error.region.overlaps(screen.query_one("#preference-identity").region)
        assert "private-test-marker" not in str(error.render())
        assert "Error:" in str(error.render())
        assert not any(method == "save_preferences" for method, _ in client.calls)
        assert app.config.identity.model_dump() == client.configuration["identity"] == {
            "user_name": "You", "companion_name": "Companion",
        }
        for button in screen.query(".form-actions Button"):
            if button.display:
                dialog = screen.query_one(".form-dialog")
                assert button.region.bottom <= dialog.content_region.bottom


@pytest.mark.parametrize("field,other", [("user_name", "companion_name"),
                                        ("companion_name", "user_name")])
async def test_identity_preferences_only_save_changed_name_and_preserve_concurrent_edits(
    field, other,
):
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#preference-category", Select).value = "identity"
        name = "[bold]Kai[/] {{name}} 界 e\u0301 👩\u200d💻"
        screen.query_one(f"#pref-{field}", Input).value = "  " + name + "  "
        original_other = client.configuration["identity"][other]
        screen.query_one(f"#pref-{other}", Input).value = "  " + original_other + "  "
        client.configuration["identity"][other] = "Saved elsewhere"
        client.configuration["providers"][0]["model"] = "changed-elsewhere"
        client.configuration["reset_hour"] = 8
        await pilot.click("#preferences-apply")
        await pilot.pause()
        assert ("save_preferences", {"preferences": {field: name}}) in client.calls
        assert app.config.identity.model_dump() == {field: name, other: "Saved elsewhere"}
        assert app.config.selected_provider().model == "changed-elsewhere"
        assert app.config.reset_hour == 8
        app.open_preferences()
        await pilot.pause()
        assert app.screen.query_one(f"#pref-{field}", Input).value == name
        assert app.screen.query_one(f"#pref-{other}", Input).value == "Saved elsewhere"


@pytest.mark.parametrize("size", [(80, 24), (48, 12)])
async def test_identity_preferences_failed_save_retains_names_draft_and_can_retry(size):
    class FailingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.started, self.gate = asyncio.Event(), asyncio.Event()
            self.fail = True

        async def request(self, method, **params):
            if method == "save_preferences" and self.fail:
                self.calls.append((method, params))
                self.started.set()
                await self.gate.wait()
                raise RuntimeError("Service [bold]offline[/]; retry saving.")
            return await super().request(method, **params)

    client = FailingClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    names = {"user_name": "Ari 雪", "companion_name": "[bold]Fern[/]"}
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Keep this draft")
        composer.selection = Selection((0, 1), (0, 4))
        selection = composer.selection
        await pilot.press("alt+6")
        screen = app.screen
        screen.query_one("#preference-category", Select).value = "identity"
        for field, value in names.items():
            screen.query_one(f"#pref-{field}", Input).value = value
        await pilot.press("ctrl+s")
        await asyncio.wait_for(client.started.wait(), 3)
        await pilot.press("ctrl+s", "escape")
        assert app.screen is screen
        assert sum(method == "save_preferences" for method, _ in client.calls) == 1
        client.gate.set()
        await pilot.pause()
        status = screen.query_one(".form-status", Static)
        dialog = screen.query_one(".form-dialog")
        assert status.has_class("problem")
        if screen.has_class("short"):
            assert Text.from_markup(dialog.border_subtitle).plain == str(status.render())
        else:
            assert status.visible and status.region.overlaps(dialog.region)
        assert "[bold]offline[/]" in "".join(fromstring(app.export_screenshot()).itertext())
        for button in screen.query(".form-actions Button"):
            if button.display:
                assert not button.disabled
                assert button.region.bottom <= dialog.content_region.bottom
        assert app.config.identity.model_dump() == client.configuration["identity"] == {
            "user_name": "You", "companion_name": "Companion",
        }
        await pilot.press("escape", "alt+6")
        await pilot.pause()
        assert {field: app.screen.query_one(f"#pref-{field}", Input).value
                for field in names} == names
        client.fail = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.identity.model_dump() == names
        assert client.configuration["identity"] == names
        assert app.preferences_state.draft is None
        assert app.focused is composer and composer.text == "Keep this draft"
        assert composer.selection == selection


@pytest.mark.parametrize("monochrome", [False, True])
async def test_identity_preferences_literal_drafts_resize_and_discard(
    monkeypatch, monochrome,
):
    for variable in ("NO_COLOR", "EIDOLON_ASCII"):
        if monochrome:
            monkeypatch.setenv(variable, "1")
        else:
            monkeypatch.delenv(variable, raising=False)
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    names = {"user_name": "[bold]Kai[/] {{user}} 雪",
             "companion_name": "[red]Fern[/] {{companion}}"}
    names = {field: (value + " 界e\u0301👩\u200d💻" * 20)[:79] + "界"
             for field, value in names.items()}
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Keep my next message 🌱")
        composer.selection = Selection((0, 3), (0, 8))
        selection = composer.selection
        app.preferences_state.category = "identity"
        await pilot.press("alt+6")
        screen = app.screen
        assert screen.has_class("monochrome") == monochrome
        assert screen.has_class("ascii") == monochrome
        for field, value in names.items():
            screen.query_one(f"#pref-{field}", Input).value = value
        for size in [(80, 24), (60, 18), (48, 12)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            dialog = screen.query_one(".form-dialog")
            assert dialog.region.bottom == composer.region.y
            for field, prefix in (("user_name", "[bold]Kai[/]"),
                                  ("companion_name", "[red]Fern[/]")):
                control = screen.query_one(f"#pref-{field}", Input)
                control.focus()
                await pilot.pause()
                await pilot.press("home")
                assert control.value == names[field]
                assert control.region.overlaps(screen.query_one("#preference-identity").region)
                assert prefix in "".join(fromstring(app.export_screenshot()).itertext())
            for button in screen.query(".form-actions Button"):
                if button.display:
                    assert button.region.bottom <= dialog.content_region.bottom
                    assert button.region.right <= dialog.content_region.right
            await pilot.press("tab")
            assert app.focused is not composer and screen in app.focused.ancestors
        focused = app.focused
        await pilot.resize_terminal(40, 10)
        await pilot.pause()
        assert screen.query_one(".form-too-small").display
        assert not screen.query_one(".form-dialog").display
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.focused is focused
        await pilot.press("escape")
        await pilot.pause()
        assert app.preferences_state.draft == names
        assert app.focused is composer and composer.selection == selection
        assert composer.text == "Keep my next message 🌱"
        assert app.config.identity.model_dump() == client.configuration["identity"] == {
            "user_name": "You", "companion_name": "Companion",
        }
        await pilot.press("alt+6")
        await pilot.pause()
        assert {field: app.screen.query_one(f"#pref-{field}", Input).value
                for field in names} == names
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.preferences_state.draft is None
        for field, value in client.configuration["identity"].items():
            assert app.screen.query_one(f"#pref-{field}", Input).value == value
            app.screen.query_one(f"#pref-{field}", Input).value = "  " + value + "  "
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert not any(method == "save_preferences" for method, _ in client.calls)
        assert app.focused is composer and composer.selection == selection
        await pilot.press("alt+6")
        await pilot.pause()
        for field, value in names.items():
            app.screen.query_one(f"#pref-{field}", Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.identity.model_dump() == client.configuration["identity"] == names
        assert ("save_preferences", {"preferences": names}) in client.calls
        assert app.focused is composer and composer.selection == selection


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_current_life_recovery_budget_saves_by_keyboard_without_losing_draft(
    size, monkeypatch,
):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    config = AppConfig()
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Draft 雨 café")
        await pilot.press("alt+6")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#preference-category", Select).value = "continuity"
        await pilot.pause()
        field = screen.query_one("#pref-continuity_recovery_calls", Input)
        field.focus()
        await pilot.press("end", "backspace", "3")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert ("save_preferences", {"preferences": {
            "continuity_recovery_calls": 3}}) in client.calls
        assert app.config.memory.continuity_recovery_calls == 3
        assert app.focused is composer and composer.text == "Draft 雨 café"
