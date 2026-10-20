import asyncio
import json
from xml.etree.ElementTree import fromstring

import pytest
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Input,
    Markdown,
    OptionList,
    Static,
    TextArea,
)

from eidolon.config import AppConfig
from eidolon.ui.activity import ActivityScreen
from eidolon.ui.app import Composer, RoomApp, display_error
from eidolon.ui.commands import COMMANDS, CommandMenu, HelpScreen, find_command, matching_commands
from eidolon.ui.history import HistoryScreen
from eidolon.ui.requests import RequestInspector


class FakeClient:
    def __init__(self):
        self.session = {
            "id": "day:2026-10-10",
            "day_key": "2026-10-10",
            "title": "2026-10-10",
            "kind": "chat",
        }
        self.messages = []
        self.sessions = [self.session]
        self.calls = []
        self.fail_recall = False
        self.completed = asyncio.Event()
        self.model_requests = []
        self.response_gate = None
        self.configuration = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}).model_dump(mode="json")

    async def request(self, method, **params):
        self.calls.append((method, params))
        if method == "status":
            return {
                "session": self.session,
                "provider": "local",
                "memory": "connected",
                "avatar": "offline",
                "outbox": {"pending": 0},
            }
        if method == "overview":
            return {
                "sessions": self.sessions,
                "runs": [],
                "jobs": [],
                "config": self.configuration,
                "prompts": {"SYSTEM": "Initial prompt"},
            }
        if method == "history":
            return {"session": self.session, "messages": self.messages}
        if method == "latest_model_request":
            return {"request": self.model_requests[-1] if self.model_requests else None}
        if method == "model_requests":
            return {
                "session": self.session,
                "requests": [
                    {key: value for key, value in request.items() if key != "body"}
                    for request in reversed(self.model_requests)
                ],
            }
        if method == "model_request":
            return next(
                request for request in self.model_requests if request["id"] == params["request_id"]
            )
        if method == "export_model_request":
            return {"path": f"/test/diagnostics/requests/{params['request_id']}.json"}
        if method in {"save_preferences", "save_preferences_with_auxiliary"}:
            from eidolon.config import AuxiliaryModelConfig
            from eidolon.preferences import UserPreferences
            config = AppConfig.model_validate(self.configuration)
            preferences = UserPreferences.model_validate({
                **UserPreferences.from_config(config).model_dump(), **params["preferences"],
            })
            config = preferences.apply_to(config)
            if "auxiliary" in params:
                config.auxiliary = AuxiliaryModelConfig.model_validate({
                    **config.auxiliary.model_dump(), **params["auxiliary"],
                })
            self.configuration = config.model_dump(mode="json")
            return self.configuration
        if method == "save_theme":
            self.configuration["theme"].update(params)
            return self.configuration
        return {}

    async def stream(self, method, **params):
        self.calls.append((method, params))
        if self.fail_recall and not params.get("bypass_recall"):
            yield {"type": "recall_required", "message": "Memory unavailable"}
            return
        yield {"type": "activity", "name": "memory_recall", "status": "completed"}
        request = make_request(len(self.model_requests) + 1, params["text"])
        self.model_requests.append(request)
        yield {"type": "model_request", "request_id": request["id"], "round_index": 0}
        if self.response_gate:
            await self.response_gate.wait()
        for text in ["Hello ", "**Alex** ", "(˵ ¬ᴗ¬˵)"]:
            yield {"type": "token", "text": text}
            await asyncio.sleep(0)
        self.messages = [
            {"role": "user", "content": params["text"]},
            {"role": "assistant", "content": "Hello **Alex** (˵ ¬ᴗ¬˵)"},
        ]
        yield {"type": "completed"}
        self.completed.set()


async def test_identity_copy_refreshes_without_replacing_the_empty_room_or_composer():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        welcome = app.query_one(".empty-chat", Static)
        composer = app.query_one(Composer)
        assert "Companion" in str(welcome.render())
        assert "Companion" in str(composer.placeholder)
        names = {"user_name": "Ari", "companion_name": "[red]Fern[/] {{name}} 雪"}
        client.configuration["identity"] = names
        app.preferences_saved(client.configuration)
        await pilot.pause()
        assert app.query_one(".empty-chat") is welcome
        assert app.query_one(Composer) is composer
        assert names["companion_name"] in str(welcome.render())
        assert names["companion_name"] in str(composer.placeholder)
        assert names["companion_name"] in composer.render_line(0).text
        assert app.focused is composer
        screenshot = app.export_screenshot()
        rendered = "".join(fromstring(screenshot).itertext()).replace("\xa0", " ")
        assert names["companion_name"] in rendered


def make_request(request_id, text, round_index=0):
    return {
        "id": request_id,
        "session_id": "day:2026-10-10",
        "generation_id": "turn1",
        "attempt_id": "attempt1",
        "round_index": round_index,
        "provider": "local",
        "model": "test-model",
        "user_message": text,
        "method": "POST",
        "endpoint": "http://localhost/v1/chat/completions",
        "created_at": "2026-10-10T04:00:00+00:00",
        "body": json.dumps(
            {
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Context\n🌸"},
                    {"role": "user", "content": text},
                ],
                "stream": True,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }


@pytest.mark.parametrize("size", [(80, 24), (100, 32), (140, 40), (60, 18), (48, 12)])
async def test_inspect_shows_latest_formatted_request_and_restores_draft(size, monkeypatch):
    client = FakeClient()
    client.model_requests = [
        make_request(1, 'Hello "Eidolon"'),
        make_request(2, "Next 🌸", round_index=1),
    ]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Composer)
        composer.load_text("Unsent draft 🌸")
        composer.move_cursor((0, 5))
        selection = composer.selection
        app.execute_command("/inspect")
        await pilot.pause()
        inspector = app.screen
        assert isinstance(inspector, RequestInspector)
        assert not inspector.query("Select") and not inspector.query("TabbedContent")
        formatted = inspector.query_one("#request-formatted", TextArea)
        assert formatted.read_only and app.focused is formatted
        assert formatted.text == json.dumps(
            json.loads(client.model_requests[-1]["body"]), ensure_ascii=False, indent=2
        )
        assert "2026-10-10 12:00:00" in str(
            inspector.query_one("#request-details", Static).render()
        )
        dialog = inspector.query_one("#request-dialog")
        assert dialog.region.bottom <= composer.region.y
        assert formatted.region.height > 0
        assert inspector.query_one("#request-actions").region.bottom <= dialog.region.bottom
        await pilot.press("tab", "x", "enter")
        assert composer.text == "Unsent draft 🌸"
        assert not app.busy
        await pilot.press("ctrl+y", "ctrl+s")
        await pilot.pause()
        assert copied == [client.model_requests[-1]["body"]]
        assert ("export_model_request", {"request_id": 2}) in client.calls
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.screen is inspector
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RequestInspector)
        assert app.focused is composer
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection


async def test_inspector_empty_history_and_refresh_during_stream():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        app.action_open_tab("history")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, RequestInspector)
        assert app.screen.request is None
        assert not app.screen.check_action("copy_request", ())
        assert "No request captured" in str(
            app.screen.query_one("#request-details", Static).render()
        )
        assert ("latest_model_request", {}) in client.calls
        await pilot.press("escape")
        app.enter_room()
        await pilot.pause()
        client.response_gate = asyncio.Event()
        app.query_one("#composer", Composer).load_text("Check actual request")
        app.submit()
        await pilot.pause()
        assert app.busy
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert (
            json.loads(app.screen.query_one("#request-formatted", TextArea).text)
            == json.loads(client.model_requests[0]["body"])
        )
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert not app.busy and app.reply_text == "Hello **Alex** (˵ ¬ᴗ¬˵)"
        client.model_requests.append(make_request(2, "A tool follow-up", round_index=1))
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert (
            json.loads(app.screen.query_one("#request-formatted", TextArea).text)
            == json.loads(client.model_requests[1]["body"])
        )


async def test_inspector_uses_latest_request_even_from_past_room_history():
    client = FakeClient()
    past = {**client.session, "id": "day:past", "day_key": "2026-10-09", "title": "Past room"}
    client.sessions.append(past)
    client.model_requests = [make_request(1, "Latest request in today’s room")]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2")
        await pilot.pause()
        history = app.screen.query_one("#history-table", DataTable)
        history.move_cursor(row=1)
        history.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.history_id == "day:past"
        assert app.history_screen.selected_session == "day:past"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert ("latest_model_request", {}) in client.calls
        assert app.screen.request == client.model_requests[-1]


@pytest.mark.parametrize("ascii_mode", [False, True])
async def test_inspector_error_resize_and_monochrome_border(ascii_mode, monkeypatch):
    if ascii_mode:
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    monkeypatch.setenv("NO_COLOR", "1")

    class OfflineClient(FakeClient):
        offline = True

        async def request(self, method, **params):
            if method == "latest_model_request" and self.offline:
                raise ConnectionError("Room service is unavailable")
            return await super().request(method, **params)

    client = OfflineClient()
    client.model_requests = [make_request(1, "雨 🌸")]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        inspector = app.screen
        assert inspector.has_class("monochrome")
        assert inspector.has_class("ascii") == ascii_mode
        assert "Room service is unavailable" in str(
            inspector.query_one("#request-details", Static).render()
        )
        assert not inspector.check_action("save_request", ())
        client.offline = False
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert inspector.request == client.model_requests[-1]
        formatted = inspector.query_one(TextArea).text
        await pilot.resize_terminal(47, 11)
        await pilot.pause()
        assert inspector.has_class("too-small")
        assert not inspector.query_one("#request-dialog").display
        assert inspector.query_one(".form-too-small").display
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not inspector.has_class("too-small")
        assert inspector.query_one("#request-dialog").display
        assert inspector.query_one(TextArea).text == formatted
        await pilot.press("escape")
        assert app.focused is app.query_one("#composer", Composer)


async def test_inspector_explains_when_background_service_needs_restart():
    class LegacyClient(FakeClient):
        async def request(self, method, **params):
            if method == "latest_model_request":
                raise RuntimeError("Unknown service request")
            return await super().request(method, **params)

    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=LegacyClient())
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        message = str(app.screen.query_one("#request-details", Static).render())
        assert "background service needs a restart" in message
        assert "eidolon service stop" in message and "eidolon service start" in message
        assert app.screen.request is None


@pytest.mark.parametrize("size", [(80, 24), (100, 32), (140, 40), (60, 18)])
async def test_layout_opens_directly_into_room_and_streams(size):
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert len(app.overview["sessions"]) == 1
        assert app.query_one("#pages", ContentSwitcher).current == "room"
        assert app.focused is app.query_one("#composer", Composer)
        app.query_one("#composer", Composer).load_text("hello")
        await pilot.press("enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert app.reply_text == "Hello **Alex** (˵ ¬ᴗ¬˵)"
        assert not app.busy
        assert app.current_page == "room"
        assert len(app.activity.entries) == 1
        assert client.messages[0]["content"] == "hello"


async def test_memory_failure_has_explicit_bypass_and_retry():
    client = FakeClient()
    client.fail_recall = True
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        app.enter_room()
        await pilot.pause()
        app.query_one("#composer", Composer).load_text("remember")
        app.submit()
        await pilot.pause()
        assert app.awaiting_recall
        assert "/retry without-memory" in str(app.query_one("#notice", Static).render())
        assert not client.messages
        app.query_one("#composer", Composer).load_text("/retry")
        await pilot.press("enter")
        await pilot.pause()
        assert app.awaiting_recall and not client.messages
        app.query_one("#composer", Composer).load_text("/retry without-memory")
        await pilot.press("enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        calls = [params for method, params in client.calls if method == "generate"]
        assert [call["bypass_recall"] for call in calls] == [False, False, True]
        assert app.pending_text is None and not app.awaiting_recall
        app.query_one(Composer).load_text("next message")
        await pilot.press("enter")
        await pilot.pause()
        assert app.awaiting_recall
        calls = [params for method, params in client.calls if method == "generate"]
        assert calls[-1]["bypass_recall"] is False


async def test_custom_theme_override_validation():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.apply_custom_theme({"accent": "#de90bd"})
        assert app.theme == "eidolon-custom"
        with pytest.raises(ValueError):
            app.apply_custom_theme({"unknown": "pink"})


@pytest.mark.parametrize("name", ["room", "save", "preview", "resume", "continue"])
async def test_removed_commands_are_not_registered_or_sent_as_messages(name):
    assert find_command(name) is None
    assert all(command.name != name for command in matching_commands(name))
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("/" + name)
        await pilot.pause()
        # Dismiss description matches to submit the typed command itself.
        await pilot.press("escape", "enter")
        assert composer.text == "/" + name
        assert "Unknown command" in str(app.query_one("#notice", Static).render())
        assert not any(method == "generate" for method, _ in client.calls)


@pytest.mark.parametrize("size", [(80, 24), (60, 18)])
async def test_command_menu_and_help_use_cleaned_command_list(size):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    removed = {"room", "save", "preview", "resume", "continue"}
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert {options.get_option_at_index(i).id for i in range(options.option_count)} == {
            command.name for command in COMMANDS
        }
        assert not removed.intersection(command.name for command in COMMANDS)
        await pilot.press("escape", "f1")
        await pilot.pause()
        help_text = app.screen.query_one(Markdown)._markdown
        assert all(f"/{name}" not in help_text for name in removed)
        assert "**Ctrl+S** saves" in help_text
        assert "**Ctrl+Enter** continues" in help_text
        assert "/retry without-memory" in help_text


@pytest.mark.parametrize("argument", ["without-memory", "invalid"])
async def test_retry_without_recall_failure_does_not_bypass_memory(argument):
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.pending_text = "waiting after a provider error"
        app.execute_command("/retry " + argument)
        await pilot.pause()
        assert not any(method == "generate" for method, _ in client.calls)
        assert app.pending_text == "waiting after a provider error" and not app.busy


async def test_slash_completion_keyboard_navigation_and_unknown_command():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        await pilot.press("/", "h")
        await pilot.pause()
        assert app.slash_visible
        assert (
            app.query_one("#slash-suggestions", OptionList).get_option_at_index(0).id == "history"
        )
        await pilot.press("tab")
        assert composer.text == "/history"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        assert not any(method == "generate" for method, _ in client.calls)
        await pilot.press("escape")
        assert app.focused is composer
        composer.load_text("/does-not-exist")
        await pilot.press("enter")
        assert composer.text == "/does-not-exist"
        assert "Unknown command" in str(app.query_one("#notice", Static).render())
        assert not any(method == "generate" for method, _ in client.calls)


async def test_palette_keeps_draft_filters_and_executes_arguments():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("a draft 🌸")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandMenu)
        app.screen.query_one(Input).value = "/prompts soul"
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_page == "prompts"
        assert app.query_one("#prompt-pages", ContentSwitcher).current == "prompt-SOUL"
        assert composer.text == "a draft 🌸"
        await pilot.press("escape")
        assert app.focused is composer
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert app.focused is composer


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12), (140, 40)])
async def test_history_drill_down_filter_and_resize(size):
    client = FakeClient()
    client.sessions += [
        {**client.session, "id": "past", "title": "雨 🌸 cafe\u0301", "day_key": "2026-10-09"}
    ]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "ctrl+f")
        await pilot.pause()
        field = app.screen.query_one("#history-filter", Input)
        assert app.focused is field
        field.value = "雨"
        await pilot.pause()
        table = app.screen.query_one("#history-table", DataTable)
        assert table.row_count == 1
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#history-browser").has_class("detail")
        assert app.screen.query_one("#history-preview").region.height > 0
        if size[0] < 120:
            assert not table.display or table.region.width == 0
        await pilot.press("escape")
        assert app.focused is table
        await pilot.resize_terminal(40, 10)
        await pilot.pause()
        assert app.screen.has_class("too-small")
        assert app.screen.query_one(".form-too-small").region.height > 0
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert not app.screen.has_class("too-small")
        assert isinstance(app.screen, HistoryScreen)
        assert table.row_count == 1


async def test_editor_shortcuts_and_refresh_preserve_other_drafts():
    client = FakeClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+5")
        app.query_one("#edit-SYSTEM", TextArea).load_text("My new prompt")
        app.jobs_state.drafts[""] = {"prompt": "Scheduled task draft"}
        app.refresh_data()
        await pilot.pause()
        assert app.query_one("#edit-SYSTEM", TextArea).text == "My new prompt"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert ("save_prompt", {"name": "SYSTEM", "text": "My new prompt"}) in client.calls
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.query_one("#edit-SYSTEM", TextArea).text == "Initial prompt"
        assert app.jobs_state.drafts[""] == {"prompt": "Scheduled task draft"}


async def test_commands_work_while_replying_and_keep_next_message():
    client = FakeClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.busy
        composer.load_text("/activity")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ActivityScreen) and app.current_page == "room"
        await pilot.press("escape")
        composer.load_text("next message")
        await pilot.press("enter")
        assert composer.text == "next message"
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert ("cancel", {}) in client.calls
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert composer.text == "next message"


async def test_literal_slash_and_custom_send_keys():
    client = FakeClient()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, bindings={"send": "ctrl+enter", "newline": "enter"})
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("//a literal slash")
        await pilot.press("enter")
        assert "\n" in composer.text
        assert not app.busy
        await pilot.press("ctrl+enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        assert client.messages[0]["content"] == "/a literal slash"


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_command_menu_controls_remain_visible_at_the_floor(size):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        dialog = app.screen.query_one("#command-dialog")
        hint = app.screen.query_one("#command-hint")
        assert hint.region.bottom <= dialog.content_region.bottom
        assert app.screen.query_one("#command-options").region.height >= 1
        await pilot.press("down", "up", "enter")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)


async def test_slash_arrow_selection_runs_without_tab_completion():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/", "down", "up", "enter")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)


async def test_editor_undo_and_global_shortcuts_from_modals():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+5")
        editor = app.query_one("#edit-SYSTEM", TextArea)
        editor.focus()
        editor.clear()
        await pilot.press("a", "b", "c", "ctrl+z")
        assert editor.text == ""
        await pilot.press("ctrl+p", "alt+2")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen) and not isinstance(app.screen, CommandMenu)
        await pilot.press("f1", "ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandMenu)


async def test_saved_theme_overrides_survive_mount_and_reload():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, theme={"name": "solarized-light", "colors": {"accent": "#aabbcc"}})
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "eidolon-custom"
        await pilot.press("alt+8")
        await pilot.press("ctrl+e")
        app.screen.query_one("#color-accent", Input).value = "#de90bd"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.theme.name == "solarized-light"
        assert app.config.theme.colors["accent"] == "#de90bd"
        await pilot.press("alt+8", "ctrl+e")
        app.screen.query_one("#color-accent", Input).value = "#111111"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.screen.query_one("#color-accent", Input).value == "#de90bd"


async def test_failure_keeps_a_new_draft_and_next_turn_clears_unsaved_attempt():
    class FailingClient(FakeClient):
        failed = False

        async def stream(self, method, **params):
            if not self.failed:
                self.failed = True
                self.calls.append((method, params))
                await self.response_gate.wait()
                yield {"type": "token", "text": "partial unsaved reply"}
                yield {"type": "error", "message": "Connection lost"}
            else:
                async for event in super().stream(method, **params):
                    yield event

    client = FailingClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("first attempt")
        await pilot.press("enter")
        await pilot.pause()
        composer.load_text("next message")
        client.response_gate.set()
        await pilot.pause()
        assert composer.text == "next message" and not app.busy
        await pilot.press("enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        assert client.messages[0]["content"] == "next message"
        transcript = " ".join(m._markdown for m in app.query("#conversation Markdown"))
        assert "first attempt" not in transcript and "partial unsaved reply" not in transcript


async def test_no_color_ascii_and_disconnected_recovery(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")

    class OfflineClient(FakeClient):
        offline = True

        async def request(self, method, **params):
            if self.offline:
                raise ConnectionError("Service stopped")
            return await super().request(method, **params)

    client = OfflineClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.no_color and "nocolor" in app.pseudo_classes
        assert app.screen.has_class("ascii")
        assert app.query_one(Composer).styles.border.top[0] == "ascii"
        assert "disconnected" in str(app.query_one("#service-status", Static).render())
        await pilot.press("alt+2")
        await pilot.pause()
        assert "Could not load" in str(app.screen.query_one("#history-count", Static).render())
        await pilot.press("escape")
        client.offline = False
        app.execute_command("/reload")
        await pilot.pause()
        assert app.session_id == client.session["id"] and app.loaded_session == app.session_id


def test_json_error_is_actionable_and_contains_no_input_dump():
    with pytest.raises(json.JSONDecodeError) as error:
        json.loads('{\n  "key":\n}')
    assert display_error(error.value).startswith("Invalid JSON at line 3, column 1:")


async def test_activity_table_fits_after_resize_and_results_stay_readable():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        app.record_activity({"name": "web_search", "status": "started"})
        app.record_activity(
            {
                "name": "web_search",
                "status": "completed",
                "duration": 1.2,
                "inputs": {"query": "rain"},
                "result": '{"results":[{"title":"A source","url":"https://example.com","content":"A paragraph."}]}',
            }
        )
        await pilot.press("alt+4")
        screen = app.screen
        assert isinstance(screen, ActivityScreen)
        table = screen.query_one("#activity-table", DataTable)
        for size in [(140, 40), (80, 24), (48, 12), (60, 18)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert table.virtual_size.width <= table.size.width
        assert table.row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        detail = screen.query_one("#activity-text", Markdown)._markdown
        assert "Sources" in detail and "A paragraph." in detail
        assert '{"results"' not in detail


async def test_history_filter_escape_with_many_rooms():
    client = FakeClient()
    client.sessions = [
        {**client.session, "id": str(i), "title": f"Room {i:04d}"} for i in range(1500)
    ]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "ctrl+f")
        field = app.screen.query_one("#history-filter", Input)
        field.value = "1499"
        await pilot.pause()
        assert app.screen.query_one("#history-table", DataTable).row_count == 1
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen) and field.value == ""
        assert app.screen.query_one("#history-table", DataTable).row_count == 1500
        await pilot.press("escape")
        assert app.current_page == "room"
