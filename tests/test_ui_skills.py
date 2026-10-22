import asyncio

import pytest
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Static, TextArea
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.skills import SkillLibrary
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import CommandMenu, find_command, matching_commands
from eidolon.ui.forms import FormCheckbox
from eidolon.ui.skills import SkillsScreen
from test_ui import FakeClient


class SkillsClient(FakeClient):
    def __init__(self, tmp_path):
        super().__init__()
        self.library = SkillLibrary(tmp_path)
        self.failure: str | None = None
        self.save_gate = None
        self.toggle_gate = None

    async def request(self, method, **params):
        if method not in {"skills", "get_skill", "save_skill", "toggle_skill"}:
            return await super().request(method, **params)
        self.calls.append((method, params))
        if method == self.failure:
            raise ConnectionError("Service disconnected")
        if method == "skills":
            return {"skills": self.library.list_skills()}
        if method == "get_skill":
            return self.library.get(params["name"])
        if method == "toggle_skill":
            if self.toggle_gate:
                await self.toggle_gate.wait()
            return self.library.set_enabled(params["name"], params["enabled"])
        if self.save_gate:
            await self.save_gate.wait()
        return self.library.save(**params)


def install(client, name="planning", *, enabled=True):
    return client.library.save(name, "Use when planning the day.", "Ask for priorities.",
                               create=True, enabled=enabled)


def test_command_vocabulary_advertises_skills():
    assert find_command("drawer") is None
    assert not matching_commands("drawer")
    assert find_command("skills").key == "alt+3"


# Layout boundaries in Unicode; ASCII appearance also exercised at the floor.
@pytest.mark.parametrize("size,ascii_mode", [
    *((size, False) for size in [(140, 40), (80, 24), (60, 18), (48, 12)]),
    ((48, 12), True),
])
async def test_skills_overlay_preserves_composer_and_fits_floor(tmp_path, size, ascii_mode, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    if ascii_mode:
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = SkillsClient(tmp_path)
    install(client)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 🌸")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        await pilot.press("alt+3")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SkillsScreen)
        assert app.query_one(ContentSwitcher).current == "room"
        assert not app.query("#drawer")
        dialog = screen.query_one(".form-dialog")
        table = screen.query_one(DataTable)
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        assert dialog.styles.border.top[0] == ("ascii" if ascii_mode else "solid")
        assert dialog.styles.border.top[1].hex == app.theme_variables["foreground"]
        assert table.region.height >= (2 if table.show_header else 1)
        assert table.virtual_size.width <= table.size.width
        hint = screen.query_one("#skills-hint")
        assert hint.region.bottom <= dialog.content_region.bottom
        assert hint.region.right <= dialog.content_region.right
        await pilot.press("tab", "shift+tab")
        assert app.focused is table
        await pilot.press("ctrl+f", "x", "ctrl+enter")
        assert app.focused is screen.query_one("#skills-filter")
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection
        assert not app.busy
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app.focused is composer
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection


async def test_create_edit_toggle_and_draft_restore_through_keyboard(tmp_path):
    client = SkillsClient(tmp_path)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.execute_command("/skills")
        await pilot.pause()
        screen = app.screen
        assert "No skills installed" in str(screen.query_one("#skills-count", Static).render())
        await pilot.press("ctrl+n")
        screen.query_one("#skill-name", Input).value = "daily-plan"
        screen.query_one("#skill-description", Input).value = "Use when planning the day."
        screen.query_one(TextArea).load_text("Ask for priorities, then make a plan.")
        screen.query_one(FormCheckbox).value = False
        await pilot.press("escape", "escape")
        assert not client.library.directory.exists()
        await pilot.press("alt+3", "ctrl+n")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one(TextArea).text == "Ask for priorities, then make a plan."
        assert not screen.query_one(FormCheckbox).value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not screen.editing
        assert screen.query_one(DataTable).row_count == 1
        assert not client.library.get("daily-plan")["enabled"]
        assert not app.skills_state.drafts
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert client.library.get("daily-plan")["enabled"]
        assert screen.query_one(DataTable).get_row("daily-plan")[0] == "On"
        assert app.focused is screen.query_one(DataTable)
        await pilot.press("enter")
        await pilot.pause()
        assert screen.editing and screen.query_one("#skill-name", Input).disabled
        screen.query_one(TextArea).load_text("Start with one priority.")
        await pilot.press("escape", "enter")
        await pilot.pause()
        assert screen.query_one(TextArea).text == "Start with one priority."
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.query_one(TextArea).text == "Ask for priorities, then make a plan."
        screen.query_one(TextArea).load_text("Start with one priority.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.library.get("daily-plan")["instructions"] == "Start with one priority."
        await pilot.press("escape", "alt+3", "ctrl+t")
        await pilot.pause()
        assert not client.library.get("daily-plan")["enabled"]


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_editor_keyboard_fields_are_reachable_after_resize(tmp_path, size):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=SkillsClient(tmp_path))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("alt+3", "ctrl+n")
        await pilot.pause()
        screen = app.screen
        for key in ("skill-name", "skill-description", "skill-instructions", "skill-enabled"):
            assert app.focused is screen.query_one("#" + key)
            control = app.focused
            body = screen.query_one(".form-body")
            assert control.region.intersection(body.region).height >= 1
            await pilot.press("tab")
            await pilot.pause()
        await pilot.resize_terminal(40, 10)
        await pilot.pause()
        assert screen.has_class("too-small")
        assert screen.query_one(".form-too-small").visible
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert not screen.has_class("too-small") and screen.editing


async def test_validation_and_service_failures_keep_edits_and_switch_state(tmp_path):
    client = SkillsClient(tmp_path)
    install(client)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+3")
        await pilot.pause()
        screen = app.screen
        client.failure = "toggle_skill"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen.selected["enabled"] and client.library.get("planning")["enabled"]
        assert "Service disconnected" in str(screen.query_one(".form-status", Static).render())
        client.failure = None
        await pilot.press("ctrl+n", "ctrl+s")
        await pilot.pause()
        assert screen.query_one("#skill-name-error").has_class("problem")
        assert not any(method == "save_skill" for method, _ in client.calls)
        screen.query_one("#skill-name", Input).value = "new-skill"
        screen.query_one("#skill-description", Input).value = "Use for a workflow."
        screen.query_one(TextArea).load_text("Some instructions.")
        client.failure = "save_skill"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert screen.editing and screen.query_one(TextArea).text == "Some instructions."
        assert not screen.committing


async def test_save_in_progress_blocks_duplicate_submission_and_retains_draft(tmp_path):
    client = SkillsClient(tmp_path)
    install(client)
    client.save_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+3", "enter")
        await pilot.pause()
        screen = app.screen
        screen.query_one(TextArea).load_text("Updated instructions.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert screen.committing and screen.query_one("#skills-save", Button).disabled
        assert screen.query_one(TextArea).is_disabled
        await pilot.press("ctrl+s", "escape", "ctrl+p", "alt+2")
        await pilot.pause()
        assert app.screen is screen
        assert len([call for call in client.calls if call[0] == "save_skill"]) == 1
        client.save_gate.set()
        await pilot.pause()
        assert not screen.editing and not screen.committing
        assert client.library.get("planning")["instructions"] == "Updated instructions."


async def test_offline_list_retry_and_background_response_do_not_steal_focus(tmp_path):
    client = SkillsClient(tmp_path)
    client.failure = "skills"
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Composer).load_text("Hello")
        await pilot.press("enter", "alt+3")
        await pilot.pause()
        screen = app.screen
        assert "unavailable" in str(screen.query_one("#skills-count", Static).render())
        client.failure = None
        await pilot.press("ctrl+l", "ctrl+n")
        await pilot.pause()
        field = screen.query_one("#skill-name", Input)
        field.focus()
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert app.screen is screen and app.focused is field
        assert not app.busy


async def test_toggle_in_progress_keeps_list_focus_and_blocks_opening_editor(tmp_path):
    client = SkillsClient(tmp_path)
    install(client)
    client.toggle_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+3", "ctrl+t")
        await pilot.pause()
        screen = app.screen
        table = screen.query_one(DataTable)
        assert screen.committing and app.focused is table
        await pilot.press("enter", "ctrl+n", "escape")
        await pilot.pause()
        assert app.screen is screen and not screen.editing
        client.toggle_gate.set()
        await pilot.pause()
        assert app.focused is table and not screen.committing
        await pilot.press("enter")
        await pilot.pause()
        assert screen.editing and not screen.query_one(FormCheckbox).value


async def test_command_palette_preserves_skill_draft_without_stacking_menus(tmp_path):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=SkillsClient(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Conversation draft")
        await pilot.press("alt+3", "ctrl+n")
        await pilot.pause()
        app.screen.query_one("#skill-name", Input).value = "writing"
        app.screen.query_one(TextArea).load_text("A skill draft.")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandMenu) and len(app.screen_stack) == 2
        app.screen.query_one(Input).value = "/skills"
        await pilot.press("enter", "ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, SkillsScreen) and len(app.screen_stack) == 2
        assert app.screen.query_one(TextArea).text == "A skill draft."
        await pilot.press("escape", "escape")
        assert app.focused is composer and composer.text == "Conversation draft"
