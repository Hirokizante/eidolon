import asyncio
from copy import deepcopy

import pytest
from textual.containers import VerticalScroll
from textual.widgets import ContentSwitcher, DataTable, Input, Markdown, Static
from textual.widgets._markdown import MarkdownParagraph
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.history import HistoryScreen, session_date
from test_ui import FakeClient


@pytest.mark.parametrize("names", [
    ("Ari 雪 e\u0301", "Fern 👩\u200d💻"),
    ("[bold]Kai[/] **name** &copy; <i>", "![Moss](https://example.invalid) `x` {{name}}"),
    ("Same name", "Same name"),
    ("界" * 80, "雪" * 80),
])
async def test_history_preview_uses_literal_recorded_names_and_preserves_body_markdown(names):
    recorded = dict(zip(("user_name", "companion_name"), names))
    current = {"user_name": "Noor", "companion_name": "Willow"}
    client = FakeClient()
    client.configuration["identity"] = current
    client.messages = [
        {"role": role, "content": f"Body **{role}** with [link](https://example.invalid).",
         **({"identity": recorded} if saved else {})}
        for saved in (False, True) for role in ("user", "assistant")
    ] + [{"role": "system", "content": "Other role"}]
    original = deepcopy(client.messages)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=current), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "enter")
        await pilot.pause()
        preview = app.screen.query_one(Markdown)
        paragraphs = list(preview.query(MarkdownParagraph))
        assert [p._content.plain for p in paragraphs[::2]] == [
            *current.values(), *names, "System",
        ]
        for message in original:
            assert message["content"] in preview.source
        for heading in paragraphs[::2]:
            assert [token.type for token in heading._inline_token.children
                    if token.type != "text"] == ["strong_open", "strong_close"]
        assert client.messages == original


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
@pytest.mark.parametrize("ascii_mode", [False, True])
async def test_history_fallback_rename_keeps_reading_position_and_composer(
    size, ascii_mode, monkeypatch,
):
    if ascii_mode:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    current = {"user_name": "Ari", "companion_name": "Fern"}
    recorded = {"user_name": "Kai", "companion_name": "Moss"}
    renamed = {"user_name": "[bold]Noor[/]", "companion_name": "Willow 雪"}
    client = FakeClient()
    client.configuration["identity"] = current
    client.messages = [
        {"role": role, "content": f"Body **{index}**.\n\nMore text.",
         **({"identity": recorded} if index % 2 else {})}
        for index in range(8) for role in ("user", "assistant")
    ]
    original = deepcopy(client.messages)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=current), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 🌱")
        composer.selection = Selection((0, 1), (0, 5))
        selection = composer.selection
        await pilot.press("alt+2", "enter")
        await pilot.pause()
        screen = app.screen
        preview = screen.query_one(Markdown)
        scroller = screen.query_one(VerticalScroll)
        scroller.scroll_to(y=9, animate=False)
        await pilot.pause()
        position = scroller.scroll_y
        calls = sum(method == "history" for method, _ in client.calls)
        client.configuration["identity"] = renamed
        await app.refresh_data().wait()
        await pilot.pause()
        labels = [p._content.plain for p in preview.query(MarkdownParagraph)
                  if p._content.plain in {*renamed.values(), *recorded.values()}]
        assert labels == [
            (recorded if index % 2 else renamed)[key]
            for index in range(8) for key in ("user_name", "companion_name")
        ]
        assert app.screen is screen and app.focused is scroller
        assert scroller.scroll_y == position
        assert screen.selected_session == client.session["id"]
        assert composer.text == "Unsent draft 🌱" and composer.selection == selection
        assert sum(method == "history" for method, _ in client.calls) == calls
        assert client.messages == original
        await pilot.press("escape", "escape")
        assert app.focused is composer and composer.selection == selection


async def test_history_rename_during_load_uses_latest_fallback_without_reloading():
    class HeldHistoryClient(FakeClient):
        async def request(self, method, **params):
            if method == "history" and self.hold_history:
                self.history_started.set()
                await self.history_gate.wait()
            return await super().request(method, **params)

    client = HeldHistoryClient()
    client.hold_history = False
    client.history_started, client.history_gate = asyncio.Event(), asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        client.messages = [{"role": "assistant", "content": "Unchanged **body**"}]
        client.hold_history = True
        await pilot.press("alt+2", "enter")
        await asyncio.wait_for(client.history_started.wait(), 3)
        screen = app.screen
        client.configuration["identity"] = {"user_name": "Noor", "companion_name": "Willow"}
        await app.refresh_data().wait()
        await pilot.pause()
        assert "Loading" in str(screen.query_one("#history-heading", Static).render())
        assert screen.query_one(Markdown).source == ""
        client.history_gate.set()
        await pilot.pause()
        assert screen.query_one(Markdown).source == "**Willow**\n\nUnchanged **body**"
        assert app.focused is screen.query_one(VerticalScroll)


async def test_repeated_refresh_during_history_render_finishes_latest_labels():
    client = FakeClient()
    client.messages = [{"role": "assistant", "content": "Unchanged **body**"}]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "enter")
        await pilot.pause()
        preview = app.screen.query_one(Markdown)
        async with preview.lock:
            client.configuration["identity"] = {"user_name": "Noor", "companion_name": "Willow"}
            await app.refresh_data().wait()
            await pilot.pause()
            assert preview.source == "**Willow**\n\nUnchanged **body**"
            await app.refresh_data().wait()
            await pilot.pause()
        await pilot.pause()
        assert [p._content.plain for p in preview.query(MarkdownParagraph)] == [
            "Willow", "Unchanged body",
        ]


async def test_history_reselection_replaces_cached_attribution_and_preserves_empty_state():
    class RoomsClient(FakeClient):
        async def request(self, method, **params):
            if method == "history":
                session = next(s for s in self.sessions if s["id"] == params["session_id"])
                return {"session": session, "messages": self.rooms.get(session["id"], [])}
            return await super().request(method, **params)

    client = RoomsClient()
    client.sessions += [{**client.session, "id": key, "title": key} for key in ("saved", "empty")]
    client.rooms = {
        client.session["id"]: [{"role": "assistant", "content": "Legacy body"}],
        "saved": [{"role": "assistant", "content": "Saved body",
                   "identity": {"user_name": "Kai", "companion_name": "Moss"}}],
    }
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "enter")
        await pilot.pause()
        await pilot.press("escape", "down", "enter")
        await pilot.pause()
        screen = app.screen
        client.configuration["identity"] = {"user_name": "Noor", "companion_name": "Willow"}
        await app.refresh_data().wait()
        await pilot.pause()
        assert screen.selected_session == "saved"
        assert screen.query_one(Markdown).source == "**Moss**\n\nSaved body"
        await pilot.press("escape", "down", "enter")
        await pilot.pause()
        await app.refresh_data().wait()
        await pilot.pause()
        assert screen.selected_session == "empty"
        assert screen.query_one(Markdown).source == "No exchanges in this room yet."


def test_history_date_preserves_chat_day_and_uses_local_job_start():
    session = {"kind": "job", "created_at": "2026-10-10T20:30:00+00:00"}
    assert session_date(session, "Asia/Manila") == "2026-10-11"
    assert session_date(session, "America/Los_Angeles") == "2026-10-10"
    session.update(kind="chat", day_key="2026-10-09")
    assert session_date(session, "Asia/Manila") == "2026-10-09"
    assert session_date({"created_at": "invalid"}, "Asia/Manila", ascii=True) == "-"


# Layout boundaries in Unicode; ASCII appearance also exercised at the floor.
@pytest.mark.parametrize("size,ascii_mode", [
    *((size, False) for size in [(140, 40), (80, 24), (60, 18), (48, 12)]),
    ((48, 12), True),
])
async def test_history_overlay_controls_focus_and_draft(size, ascii_mode, monkeypatch):
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
        await pilot.press("alt+2")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)
        assert app.query_one(ContentSwitcher).current == "room"
        dialog = screen.query_one(".form-dialog")
        table = screen.query_one(DataTable)
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        assert dialog.styles.border.top[0] == ("ascii" if ascii_mode else "solid")
        assert dialog.styles.border.top[1].hex == app.theme_variables["foreground"]
        assert table.region.height >= (2 if table.show_header else 1)
        assert table.virtual_size.width <= table.size.width
        hint = screen.query_one("#history-hint")
        assert hint.region.bottom <= dialog.content_region.bottom
        assert hint.region.right <= dialog.content_region.right
        await pilot.press("tab", "shift+tab")
        assert app.focused is table
        await pilot.press("ctrl+f", "x", "ctrl+enter")
        assert app.focused is screen.query_one(Input)
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection
        assert not app.busy
        await pilot.press("escape", "escape")  # clear filter, then dismiss
        await pilot.pause()
        assert app.focused is composer
        assert composer.selection == selection and composer.text == "Unsent draft 🌸"


async def test_job_date_filter_refresh_and_resume_from_detail():
    client = FakeClient()
    job = {"id": "job:run", "title": "Evening note", "kind": "job", "day_key": None,
           "created_at": "2026-10-10T20:30:00+00:00"}
    client.sessions.append(job)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "ctrl+f")
        screen = app.screen
        screen.query_one(Input).value = "2026-10-11"
        await pilot.pause()
        table = screen.query_one(DataTable)
        assert table.row_count == 1
        assert table.get_row("job:run")[0] == "2026-10-11"
        await pilot.press("enter", "enter")
        await pilot.pause()
        assert screen.selected_session == "job:run"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.selected_session == "job:run"
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert app.session_id == "job:run" and app.resuming_past
        assert not isinstance(app.screen, HistoryScreen)


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_history_continue_from_list_keeps_draft_and_today_returns(size):
    client = FakeClient()
    client.sessions.append({**client.session, "id": "past", "title": "Past room"})
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("draft for later")
        await pilot.press("alt+2", "down")
        await pilot.pause()
        screen = app.screen
        assert screen.selected_session == "past"
        assert "Ctrl+Enter continue" in str(screen.query_one("#history-hint", Static).render())
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert app.session_id == "past" and app.resuming_past
        assert app.focused is composer and composer.text == "draft for later"
        assert not any(method == "generate" for method, _ in client.calls)
        app.execute_command("/today")
        await pilot.pause()
        assert app.session_id == client.session["id"] and not app.resuming_past
        assert composer.text == "draft for later"


async def test_history_read_error_and_late_reply_do_not_steal_focus():
    class SlowClient(FakeClient):
        gate = asyncio.Event()

        async def request(self, method, **params):
            if method == "history":
                await self.gate.wait()
                raise ConnectionError("Service stopped")
            return await super().request(method, **params)

    client = SlowClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "enter")
        await pilot.pause()
        screen = app.screen
        assert "Loading" in str(screen.query_one("#history-heading", Static).render())
        client.gate.set()
        await pilot.pause()
        assert "Service stopped" in str(screen.query_one("#history-heading", Static).render())
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app.focused is app.query_one(Composer)


async def test_background_reply_keeps_history_focus():
    client = FakeClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.query_one(Composer).load_text("Hello")
        app.submit()
        await pilot.pause()
        assert app.busy
        await pilot.press("alt+2", "ctrl+f")
        screen = app.screen
        field = screen.query_one(Input)
        await pilot.press("ctrl+enter")
        assert app.screen is screen and app.session_id == client.session["id"]
        assert not app.resuming_past
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert app.screen is screen and app.focused is field
        assert not app.busy
        await pilot.press("escape")
        assert app.focused is app.query_one(Composer)
