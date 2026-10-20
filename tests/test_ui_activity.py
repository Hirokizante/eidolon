import asyncio
import json

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import ContentSwitcher, DataTable, Markdown, Static
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.ui.activity import ActivityLog, ActivityScreen, activity_summary
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import COMMANDS, find_command, matching_commands
from eidolon.ui.requests import RequestInspector
from test_ui import FakeClient


def test_activity_coalesces_started_and_completed_and_bounds_history():
    log = ActivityLog(limit=3)
    started = log.record({"name": "web_search", "status": "started"})
    finished = log.record({"name": "web_search", "status": "completed", "duration": 0.123})
    assert started is finished
    assert len(log.entries) == 1
    assert finished.status == "Done" and finished.duration == "0.1s"
    for _ in range(5):
        log.record({"name": "web_search", "status": "started"})
        log.record({"name": "web_search", "status": "error", "result": '{"error":"No connection"}'})
    assert len(log.entries) == 3
    assert activity_summary(log.entries[-1].event) == "No connection"


def test_activity_formats_nested_results_as_readable_sources_without_transport_metadata():
    event = {
        "type": "activity",
        "name": "web_search",
        "status": "completed",
        "turn_id": "internal-id",
        "inputs": {"query": "雨 🌸"},
        "duration": 1.3,
        "result": json.dumps(
            {
                "results": [
                    {
                        "title": "A source",
                        "url": "https://example.com",
                        "content": "A useful paragraph.",
                    }
                ]
            }
        ),
    }
    activity = ActivityLog().record(event)
    assert activity_summary(event) == "1 source · 雨 🌸"
    detail = activity.detail()
    assert "Search the web" in detail and "1.3s" in detail
    assert "A source" in detail and "https://example.com" in detail
    assert "A useful paragraph." in detail and "雨 🌸" in detail
    assert "internal-id" not in detail and '{"results"' not in detail


def test_command_registry_searches_names_aliases_and_descriptions():
    assert find_command("requests") is None
    assert find_command("inspect").key == "ctrl+r"
    assert find_command("themes").name == "theme"
    assert matching_commands("/h")[0].name == "history"
    assert matching_commands("exact model request")[0].name == "inspect"
    assert matching_commands("unknown command") == []
    assert len({command.key for command in COMMANDS if command.key}) == sum(
        bool(c.key) for c in COMMANDS
    )


@pytest.mark.parametrize("size", [(140, 40), (80, 24), (60, 24), (60, 18), (48, 12)])
@pytest.mark.parametrize("no_color,ascii_mode", [(False, False), (True, False), (True, True)])
async def test_activity_overlay_fits_and_restores_draft(size, no_color, ascii_mode, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("EIDOLON_ASCII", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    if ascii_mode:
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 🌸")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        query = "每日计划 e\u0301 🌸 " * 20
        app.record_activity({"name": "web_search", "status": "unavailable",
                             "inputs": {"query": query}, "message": query + "Service disconnected"})
        await pilot.press("alt+4")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ActivityScreen)
        assert app.query_one(ContentSwitcher).current == "room"
        dialog = screen.query_one(".form-dialog")
        table = screen.query_one(DataTable)
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        assert dialog.styles.border.top[0] == ("ascii" if ascii_mode else "solid")
        assert dialog.styles.border.top[1].hex == app.theme_variables[
            "foreground" if no_color else "primary"
        ]
        assert table.region.height >= (2 if table.show_header else 1)
        assert table.virtual_size.width <= table.size.width
        title = table.get_row("1")[0]
        assert title.cell_len <= next(iter(table.columns.values())).width
        assert str(title).endswith("..." if ascii_mode else "…")
        assert str(table.get_row("1")[1]) == "Unavailable"
        hint = screen.query_one("#activity-hint", Static)
        assert hint.region.bottom <= dialog.content_region.bottom
        assert Text(str(hint.render())).cell_len <= hint.size.width
        await pilot.press("tab", "shift+tab", "x", "ctrl+enter")
        assert app.focused is table
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection
        assert not app.busy
        await pilot.press("enter")
        await pilot.pause()
        preview = screen.query_one("#activity-preview", VerticalScroll)
        assert app.focused is preview and preview.region.height > 0
        detail = screen.query_one(Markdown)._markdown
        assert query in detail and "Service disconnected" in detail
        await pilot.press("escape")
        assert app.screen is screen and app.focused is table
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection


async def test_activity_empty_loading_and_resize_preserve_state():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.execute_command("/activity")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ActivityScreen)
        assert "No activity yet" in str(screen.query_one("#activity-count", Static).render())
        assert not screen.query_one(DataTable).display
        await pilot.press("x", "ctrl+enter")
        assert not app.busy and not app.query_one(Composer).text
        app.busy = True
        app.render_activity()
        assert "Waiting for activity" in str(screen.query_one("#activity-count", Static).render())
        app.record_activity({"name": "read_file", "status": "started",
                             "inputs": {"path": "notes.txt"}})
        await pilot.pause()
        table = screen.query_one(DataTable)
        assert app.focused is table
        await pilot.press("enter")
        await pilot.pause()
        for size in [(47, 24), (80, 11), (48, 12), (60, 18), (140, 40), (80, 24)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert screen.has_class("too-small") == (size[0] < 48 or size[1] < 12)
            assert screen.detail_key == 1
            if screen.has_class("too-small"):
                assert screen.query_one(".form-too-small").display
                assert not screen.query_one(".form-dialog").display
            else:
                assert screen.query_one("#activity-preview").display
                assert app.focused is screen.query_one("#activity-preview")
        await pilot.press("escape")
        assert app.focused is table and table.virtual_size.width <= table.size.width
        app.busy = False


async def test_activity_arriving_while_overlay_mounts_is_available_on_open():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_tab("activity")
        app.record_activity({"name": "read_file", "status": "completed",
                             "inputs": {"path": "notes.txt"}})
        await pilot.pause()
        assert isinstance(app.screen, ActivityScreen)
        assert app.screen.query_one(DataTable).row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert "notes.txt" in app.screen.query_one(Markdown)._markdown


async def test_live_activity_updates_preserve_selection_detail_scroll_and_inspector_focus():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.record_activity({"name": "memory_recall", "status": "completed"})
        app.record_activity({"name": "web_search", "status": "started",
                             "inputs": {"query": "rain"}})
        await pilot.press("alt+4", "down", "enter")
        await pilot.pause()
        screen = app.screen
        preview = screen.query_one("#activity-preview", VerticalScroll)
        await pilot.press("ctrl+r")
        await pilot.pause()
        inspector = app.screen
        assert isinstance(inspector, RequestInspector)
        focus = app.focused
        content = "\n\n".join(f"Paragraph {index}." for index in range(30))
        app.record_activity({"name": "web_search", "status": "completed", "duration": 1.2,
                             "result": {"results": [{"title": "A source",
                                                       "url": "https://example.com",
                                                       "content": content}]}})
        await pilot.pause()
        assert app.screen is inspector and app.focused is focus
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is screen and app.focused is preview
        detail = screen.query_one(Markdown)._markdown
        assert "Done · 1.2s" in detail and "Sources" in detail and content in detail
        preview.scroll_to(y=10, animate=False)
        await pilot.pause()
        scroll_y = preview.scroll_y
        assert scroll_y > 0
        app.record_activity({"name": "avatar", "status": "completed"})
        await pilot.pause()
        assert app.focused is preview and preview.scroll_y == scroll_y
        await pilot.press("escape")
        table = screen.query_one(DataTable)
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value == "2"
        assert str(table.get_row("2")[1]) == "Done"


async def test_background_reply_keeps_activity_focus_and_conversation_scroll():
    client = FakeClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Hello")
        app.submit()
        await pilot.pause()
        for index in range(12):
            await app.append_message("assistant", f"Earlier reply {index}.\n\nMore text.",
                                     scroll=False)
        await pilot.pause()
        conversation = app.query_one("#conversation", VerticalScroll)
        conversation.scroll_to(y=3, animate=False)
        await pilot.pause()
        scroll_y = conversation.scroll_y
        assert not conversation.is_vertical_scroll_end
        composer.load_text("Next message")
        await pilot.press("alt+4", "enter")
        await pilot.pause()
        screen, focus = app.screen, app.focused
        assert isinstance(screen, ActivityScreen)
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert app.screen is screen and app.focused is focus
        assert conversation.scroll_y == scroll_y
        await pilot.press("escape", "escape")
        assert app.focused is composer and composer.text == "Next message"
        assert conversation.scroll_y == scroll_y


async def test_activity_drops_expired_details_and_resets_for_next_reply():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.activity = ActivityLog(limit=2)
        app.record_activity({"name": "web_search", "status": "completed",
                             "message": "Previous reply"})
        await pilot.press("alt+4", "enter")
        await pilot.pause()
        screen = app.screen
        for _ in range(2):
            app.record_activity({"name": "avatar", "status": "completed"})
        await pilot.pause()
        assert screen.detail_key is None and app.focused is screen.query_one(DataTable)
        await pilot.press("enter")
        await pilot.pause()
        app.activity = ActivityLog()
        app.render_activity()
        app.record_activity({"name": "read_file", "status": "completed",
                             "result": "New reply"})
        await pilot.pause()
        assert screen.detail_key is None and screen.query_one(DataTable).row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        detail = screen.query_one(Markdown)._markdown
        assert "New reply" in detail and "Previous reply" not in detail


async def test_activity_updates_keep_list_position_and_palette_returns_to_chat():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        for index in range(40):
            app.record_activity({"name": "read_file", "status": "completed",
                                 "inputs": {"path": f"note-{index}.txt"}})
        await pilot.press("alt+4")
        screen = app.screen
        table = screen.query_one(DataTable)
        table.move_cursor(row=25)
        await pilot.pause()
        scroll_y = table.scroll_y
        assert scroll_y > 0
        app.record_activity({"name": "avatar", "status": "completed"})
        await pilot.pause()
        assert table.cursor_row == 25 and table.scroll_y == scroll_y
        await pilot.press("ctrl+p", "escape")
        await pilot.pause()
        assert app.screen is screen and app.focused is table
        assert table.cursor_row == 25 and table.scroll_y == scroll_y
        await pilot.press("escape")
        await pilot.pause()
        assert app.activity_screen is None and app.focused is app.query_one(Composer)
