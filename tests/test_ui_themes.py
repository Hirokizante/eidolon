import asyncio

import pytest
from textual.widgets import Button, Input, Static
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.themes import COLOR_FIELDS, PALETTES, THEME_LABELS, validate_theme
from eidolon.ui.history import HistoryScreen
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.themes import ThemeScreen
from test_ui import FakeClient


def test_ten_terminalcolors_palettes_with_varied_light_and_dark_colors():
    assert len(PALETTES) == len(THEME_LABELS) == 10
    assert sum(not p['dark'] for p in PALETTES.values()) == 2
    assert len({p['background'] for p in PALETTES.values()}) == 10
    for name, palette in PALETTES.items():
        validate_theme(name, {field: palette[field] for field in COLOR_FIELDS})


@pytest.mark.parametrize('size', [(140, 40), (80, 24), (60, 18), (48, 12)])
async def test_theme_modal_preview_cancel_apply_and_focus(size):
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text('my draft 🌸')
        composer.selection = Selection((0, 1), (0, 4))
        selection = composer.selection
        saved = app.theme
        await pilot.press('alt+8')
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThemeScreen)
        assert app.current_page == 'room'
        dialog = screen.query_one('.form-dialog')
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one('#conversation').region.y
        assert screen.query_one('#theme-options').region.height >= 1
        for button in screen.query(Button):
            if button.display:
                assert button.region.height == 1
                assert button.region.bottom <= dialog.content_region.bottom
                assert button.region.right <= dialog.content_region.right
        await pilot.press('down')
        await pilot.pause()
        assert app.theme == 'rose-pine'
        assert app.config.theme.name == saved
        assert not any(m == 'save_theme' for m, _ in client.calls)
        await pilot.press('escape')
        await pilot.pause()
        assert app.theme == saved
        assert app.focused is composer and composer.selection == selection
        assert composer.text == 'my draft 🌸'
        await pilot.press('alt+8', 'down', 'enter')
        await pilot.pause()
        assert not isinstance(app.screen, ThemeScreen)
        assert app.theme == app.config.theme.name == 'rose-pine'
        assert client.configuration['theme']['name'] == 'rose-pine'
        assert app.focused is composer and composer.selection == selection


async def test_custom_theme_draft_validation_persistence_and_builtin_switch():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('alt+8', 'ctrl+e')
        screen = app.screen
        screen.query_one('#color-accent', Input).value = 'invalid-color'
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert screen.query_one('#color-accent-error').has_class('problem')
        assert not any(m == 'save_theme' for m, _ in client.calls)
        screen.query_one('#color-accent', Input).value = '#de90bd'
        await pilot.pause()
        assert app.theme == 'eidolon-custom'
        await pilot.press('escape', 'escape')
        await pilot.pause()
        assert app.theme == 'tokyo-night'
        assert app.theme_draft.values['accent'] == '#de90bd'
        await pilot.press('alt+8', 'ctrl+e')
        await pilot.pause()
        assert app.screen.query_one('#color-accent', Input).value == '#de90bd'
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert app.config.theme.custom_base == 'tokyo-night'
        assert app.config.theme.custom_colors['accent'] == '#de90bd'
        assert app.theme_draft is None
        app.execute_command('/theme github-light')
        await pilot.pause()
        assert app.theme == 'github-light'
        assert app.config.theme.colors == {}
        assert app.config.theme.custom_colors['accent'] == '#de90bd'
        app.execute_command('/theme custom')
        await pilot.pause()
        assert app.theme == 'eidolon-custom'
        assert app.config.theme.colors['accent'] == '#de90bd'
        # Previewing another custom variant must not overwrite the saved palette.
        await pilot.press('alt+8', 'ctrl+e')
        app.screen.query_one('#color-accent', Input).value = '#112233'
        await pilot.pause()
        await pilot.press('escape', 'escape')
        await pilot.pause()
        assert app.current_theme.accent == '#de90bd'
    restarted = RoomApp(AppConfig.model_validate(client.configuration), client=client)
    async with restarted.run_test() as pilot:
        await pilot.pause()
        assert restarted.theme == 'eidolon-custom'
        assert restarted.current_theme.accent == '#de90bd'


async def test_theme_save_failure_keeps_preview_without_changing_saved_state():
    class FailingClient(FakeClient):
        async def request(self, method, **params):
            if method == 'save_theme':
                raise RuntimeError('Service disconnected')
            return await super().request(method, **params)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FailingClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('alt+8', 'down', 'enter')
        await pilot.pause()
        assert isinstance(app.screen, ThemeScreen)
        assert app.config.theme.name == 'tokyo-night'
        assert app.theme == 'rose-pine'
        assert 'disconnected' in str(app.screen.query_one('.form-status', Static).render())
        await pilot.press('escape')
        await pilot.pause()
        assert app.theme == 'tokyo-night'


async def test_theme_save_blocks_dismissal_until_service_finishes():
    class SlowClient(FakeClient):
        gate = asyncio.Event()
        async def request(self, method, **params):
            if method == 'save_theme':
                await self.gate.wait()
            return await super().request(method, **params)
    client = SlowClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('alt+8', 'down', 'enter')
        await pilot.pause()
        assert app.screen.committing
        await pilot.press('escape', 'alt+6')
        assert isinstance(app.screen, ThemeScreen)
        client.gate.set()
        await pilot.pause()
        assert app.theme == 'rose-pine'
        assert not isinstance(app.screen, ThemeScreen)


async def test_theme_resize_ascii_monochrome_and_custom_field_visibility(monkeypatch):
    monkeypatch.setenv('NO_COLOR', '1')
    monkeypatch.setenv('EIDOLON_ASCII', '1')
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('alt+8', 'ctrl+e')
        screen = app.screen
        assert screen.has_class('ascii') and screen.has_class('monochrome')
        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        app.screen.query_one('#color-error', Input).focus()
        await pilot.pause()
        field = screen.query_one('#color-error', Input)
        body = screen.query_one('#theme-custom')
        assert body.region.y <= field.region.y < body.region.bottom
        await pilot.resize_terminal(47, 11)
        await pilot.pause()
        assert screen.has_class('too-small')
        assert screen.query_one('.form-too-small').visible
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not screen.has_class('too-small')
        assert screen.editing


async def test_theme_wheel_previews_without_saving_and_resize_keeps_highlight_visible():
    from textual import events
    from eidolon.ui.themes import ThemeOptions

    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('alt+8')
        await pilot.pause()
        options = app.screen.query_one(ThemeOptions)
        options.post_message(events.MouseScrollDown(options, 0, 0, 0, 1, 0, False, False, False))
        await pilot.pause()
        assert app.theme == 'rose-pine'
        options.post_message(events.MouseScrollUp(options, 0, 0, 0, -1, 0, False, False, False))
        await pilot.pause()
        assert app.theme == 'tokyo-night'
        await pilot.press('end')
        await pilot.resize_terminal(48, 12)
        await pilot.pause()
        assert options.highlighted_option.id == 'github-light'
        assert options.scroll_y + options.scrollable_content_region.height >= options.option_count
        assert app.theme == 'github-light'
        assert not any(method == 'save_theme' for method, _ in client.calls)


@pytest.mark.parametrize('size', [(80, 24), (60, 18), (48, 12)])
async def test_custom_fields_are_keyboard_reachable_and_close_retains_draft(size):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press('alt+8', 'ctrl+e')
        await pilot.pause()
        screen = app.screen
        for name in COLOR_FIELDS:
            field = screen.query_one('#color-' + name, Input)
            assert app.focused is field
            body = screen.query_one('#theme-custom')
            assert body.region.y <= field.region.y < body.region.bottom
            await pilot.press('tab')
            await pilot.pause()
        screen.query_one('#color-warning', Input).value = '#abcdef'
        await pilot.pause()
        # Direct Close retains the draft and cancels the preview in one step.
        screen.query_one('#theme-close', Button).press()
        await pilot.pause()
        assert not isinstance(app.screen, ThemeScreen)
        assert app.theme == 'tokyo-night'
        assert app.theme_draft.values['warning'] == '#abcdef'


async def test_command_menu_dismisses_theme_preview_and_preserves_custom_draft():
    from eidolon.ui.commands import CommandMenu

    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('alt+8', 'ctrl+e')
        app.screen.query_one('#color-accent', Input).value = '#de90bd'
        await pilot.pause()
        await pilot.press('ctrl+p')
        await pilot.pause()
        assert isinstance(app.screen, CommandMenu)
        assert not any(isinstance(screen, ThemeScreen) for screen in app.screen_stack)
        assert app.theme == 'tokyo-night'
        assert app.theme_draft.values['accent'] == '#de90bd'
        app.screen.query_one('#command-input', Input).value = 'history'
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        assert not isinstance(app.screen, (CommandMenu, ThemeScreen))
