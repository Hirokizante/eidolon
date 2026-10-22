"""Identity flows and supporting copy stay usable at supported terminal sizes."""

import asyncio
import copy
from xml.etree.ElementTree import fromstring

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, Input, Markdown, Select, Static, TextArea
from textual.widgets._markdown import MarkdownParagraph
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.jobs import JobsScreen
from eidolon.ui.memory import MemoryScreen, run_details
from eidolon.ui.memory_notes import NoteEditorScreen
from eidolon.ui.preferences import FIELDS, PreferencesScreen, PreferencesState
from test_ui import FakeClient
from test_ui_jobs import JobsClient, install
from test_ui_memory import MemoryClient


@pytest.mark.parametrize("ascii_mode", [False, True], ids=["color", "ascii-monochrome"])
async def test_complete_identity_flow_at_terminal_floor(
    ascii_mode, monkeypatch, tmp_path, request,
):
    """Complete flow at the floor; focused preference tests cover larger layouts."""
    size = (48, 12)
    names = ("[bold]Aster[/bold] 雨 é 🌿" + "界" * 55,
             "{{Cedar}} *雪* é 👩‍💻" + "語" * 60)
    for variable in ("NO_COLOR", "EIDOLON_ASCII"):
        monkeypatch.delenv(variable, raising=False)
        if ascii_mode:
            monkeypatch.setenv(variable, "1")
    names = dict(zip(("user_name", "companion_name"), names))
    recorded = {"user_name": "[i]Noor[/i] 雪", "companion_name": "Fern {{past}} 🌱"}
    config = AppConfig(memory={"bank": "hermes", "enabled": False})

    class IdentityFlowClient(FakeClient):
        async def stream(self, method, **params):
            snapshot = dict(self.configuration["identity"])
            yield {"type": "turn_started", "turn_id": "synthetic-turn", "identity": snapshot}
            yield {"type": "model_request"}
            await self.response_gate.wait()
            yield {"type": "token", "text": "A **synthetic** reply. 雨 é 🌿"}
            yield {"type": "completed", "text": "A **synthetic** reply. 雨 é 🌿", "metadata": {
                "identity": snapshot, "user_message_at": "2026-10-19T01:00Z",
            }, "turn": {"created_at": "2026-10-19T01:01Z"}}

    client = IdentityFlowClient()
    client.configuration = config.model_dump(mode="json")
    client.response_gate = asyncio.Event()
    client.messages = [
        {"role": role, "content": f"Synthetic **message {index}**. 雨 é 🌿",
         "created_at": "2026-10-19T00:00Z",
         **({"identity": recorded} if index % 2 else {})}
        for index in range(4) for role in ("user", "assistant")
    ]
    original = copy.deepcopy(client.messages)
    app = RoomApp(config, client=client)
    app.animation_level = "none"
    (tmp_path / "case.txt").write_text(request.node.callspec.id, encoding="utf-8")

    def capture(name: str) -> str:
        svg = app.export_screenshot(title=f"Identity review {size[0]}×{size[1]}")
        (tmp_path / f"{name}.svg").write_text(svg, encoding="utf-8")
        return "".join(fromstring(svg).itertext()).replace("\xa0", " ")

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 雨 é 🌿")
        composer.selection = Selection((0, 2), (0, 7))
        selection = composer.selection
        conversation = app.query_one("#conversation", VerticalScroll)
        conversation.scroll_home(animate=False)
        await pilot.pause()
        position = conversation.scroll_y
        frames = list(conversation.children)
        app.preferences_state.category = "identity"
        await pilot.press("alt+6", "tab")
        screen = app.screen
        assert isinstance(screen, PreferencesScreen)
        assert screen.has_class("ascii") == ascii_mode
        assert screen.has_class("monochrome") == ascii_mode
        for index, (field, value) in enumerate(names.items()):
            control = screen.query_one(f"#pref-{field}", Input)
            assert app.focused is control
            control.value = value
            control.cursor_position = 0
            await pilot.pause()
            assert screen.query_one("#preference-identity").content_region.contains_region(
                control.region,
            )
            rendered = capture(f"preferences-{index}")
            assert value[:10] in rendered
            if index == 0:
                await pilot.press("tab")
        for button in screen.query(".form-actions Button"):
            if button.display:
                assert screen.query_one(".form-dialog").content_region.contains_region(
                    button.region,
                )
        assert screen.query_one("#preferences-apply", Button).display
        await pilot.press("escape", "alt+6")
        await pilot.pause()
        for field, value in names.items():
            assert app.screen.query_one(f"#pref-{field}", Input).value == value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.identity.model_dump() == names
        assert client.configuration["identity"] == names
        assert app.focused is composer
        assert composer.text == "Unsent draft 雨 é 🌿" and composer.selection == selection
        assert conversation.scroll_y == position and list(conversation.children) == frames
        assert [Text.from_markup(frame.border_title).plain for frame in frames] == [
            (recorded if index % 2 else names)[key]
            for index in range(4) for key in ("user_name", "companion_name")
        ]

        worker = app.generate("A synthetic live question.")
        await pilot.pause()
        assert app.busy
        assert "Ctrl+C cancels" in app.query_one("#notice").render_line(0).text
        conversation.scroll_home(animate=False)  # Read earlier messages during the reply.
        await pilot.pause()
        position = conversation.scroll_y
        client.response_gate.set()
        await worker.wait()
        await pilot.pause()
        assert not app.busy and conversation.scroll_y == position
        live_frames = list(app.query(".message-frame"))[-2:]
        assert [Text.from_markup(frame.border_title).plain for frame in live_frames] == list(
            names.values(),
        )
        conversation.scroll_end(animate=False)
        await pilot.pause()
        assert conversation.is_vertical_scroll_end
        assert composer.region.bottom <= size[1] and app.focused is composer
        assert composer.text == "Unsent draft 雨 é 🌿" and composer.selection == selection

        await pilot.press("alt+2", "enter")
        await pilot.pause()
        preview = app.screen.query_one(Markdown)
        assert [p._content.plain for p in preview.query(MarkdownParagraph)][::2] == [
            (recorded if index % 2 else names)[key]
            for index in range(4) for key in ("user_name", "companion_name")
        ]
        assert all(message["content"] in preview.source for message in original)
        scroller = app.screen.query_one(VerticalScroll)
        assert app.focused is scroller
        await pilot.press("end")
        await pilot.pause()
        assert scroller.is_vertical_scroll_end
        assert "Esc back" in str(app.screen.query_one("#history-hint", Static).render())
        await pilot.press("escape", "escape")
        assert app.focused is composer
        await pilot.press("alt+6", "tab", "tab")
        screen = app.screen
        assert app.focused is screen.query_one("#pref-companion_name")
        screen.query_one("#pref-companion_name", Input).value = "Resize draft 雪"
        await pilot.resize_terminal(47, 11)
        await pilot.pause()
        assert screen.query_one(".form-too-small").display
        assert not screen.query_one(".form-dialog").display
        assert "Terminal too small" in capture("preferences-too-small")
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert app.focused is screen.query_one("#pref-companion_name")
        assert app.focused.value == "Resize draft 雪"
        assert app.config.identity.model_dump() == names
        await pilot.press("escape")
        await pilot.resize_terminal(47, 11)
        await pilot.pause()
        assert app.screen.has_class("too-small")
        assert "Terminal too small" in capture("room-too-small")
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert app.focused is composer and conversation.is_vertical_scroll_end
        assert composer.text == "Unsent draft 雨 é 🌿" and composer.selection == selection
    assert client.messages == original


@pytest.fixture
def identity_config(monkeypatch):
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    return AppConfig(
        memory={"enabled": True, "save_enabled": True, "bank": "hermes"},
        identity={"user_name": "界" * 80, "companion_name": "語" * 80},
    )


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("owner,label", [("user", "User"), ("assistant", "Companion")])
@pytest.mark.parametrize("kind,suffix", [("conversational", ""),
                                         ("scheduled", " (reported as scheduled)")])
def test_run_review_uses_neutral_promise_roles_without_rewriting_versions(
    compact, owner, label, kind, suffix,
):
    note = {"topic": "Synthetic plan", "content": "[bold]Keep this wording[/bold] 🌿",
            "promise_owner": owner, "promise_kind": kind, "importance": "high"}
    row = {"status": "completed", "changes": [{
        "after": note, "before": {**note, "content": "Original wording é"}, "created": False,
    }]}
    original = copy.deepcopy(row)

    text = run_details(row, "UTC", compact=compact)

    assert text.count("Commitment: " + label + suffix) == 2
    assert note["content"] in text and "Original wording é" in text
    assert row == original


@pytest.mark.parametrize("owner", [None, "tool"])
def test_run_review_does_not_invent_a_promise_owner(owner):
    row = {"status": "completed", "changes": [{
        "after": {"content": "Synthetic note", "promise_owner": owner}, "created": True,
    }]}
    assert "Commitment:" not in run_details(row, "UTC")


def test_vision_preference_help_is_neutral():
    screen = PreferencesScreen(FakeClient(), AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), PreferencesState())
    assert screen.category_help("vision") == (
        "A separate model analyzes attached images when more detail is needed."
    )


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_job_help_keeps_keyboard_editing_and_saved_prompt_intact(identity_config, size):
    client = JobsClient()
    client.configuration = identity_config.model_dump(mode="json")
    job = install(client, prompt="Keep [bold]this[/bold] prompt and {{template}} literally.")
    original = job.model_dump()
    app = RoomApp(identity_config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft é 🌿")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        await pilot.press("alt+7", "enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobsScreen) and screen.editing
        labels = [str(label.render()) for label in screen.query(".field-label")]
        assert "Prompt (what should happen?)" in labels
        for _ in range(12):
            if app.focused is screen.query_one("#job-prompt"):
                break
            await pilot.press("tab")
        prompt = screen.query_one("#job-prompt", TextArea)
        assert app.focused is prompt
        assert prompt.region.overlaps(screen.query_one(".form-body").region)
        assert prompt.text == original["prompt"]
        await pilot.press("end", "!")
        edited = prompt.text
        await pilot.press("escape", "escape", "alt+7", "enter")
        await pilot.pause()
        assert app.screen.query_one("#job-prompt", TextArea).text == edited
        await pilot.press("escape", "escape")
        assert app.focused is composer
        assert composer.text == "Unsent draft é 🌿" and composer.selection == selection
    assert job.model_dump() == original
    assert not any(method == "save_job" for method, _ in client.calls)


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_note_help_and_run_review_preserve_content_and_controls(identity_config, size):
    client = MemoryClient()
    client.configuration = identity_config.model_dump(mode="json")
    for change in client.run_changes["synthetic-task"]:
        change["after"]["promise_owner"] = "assistant"
        change["after"]["content"] += "\n[bold]Literal[/bold] {{kept}}"
        change["before"]["promise_owner"] = "user"
    original = copy.deepcopy((client.memory, client.run_changes))
    app = RoomApp(identity_config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 🌿")
        app.open_memory()
        await pilot.pause()
        memory = app.screen
        assert isinstance(memory, MemoryScreen)
        memory.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        memory.read_selection("continuity:thread-1")
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, NoteEditorScreen)
        labels = [str(label.render()) for label in editor.query(".field-label")]
        assert "Importance to your companion and relationship" in labels
        await pilot.press("tab")
        importance = editor.query_one("#note-importance", Select)
        assert app.focused is importance
        assert importance.region.overlaps(editor.query_one(".form-body").region)
        await pilot.press("enter", "down", "enter", "tab", "tab", "tab")
        assert app.focused is editor.query_one("#note-content", TextArea)
        assert app.focused.text == client.memory["continuity"]["items"][0]["content"]
        await pilot.press("escape", "escape")
        memory.state.life_section = "history"
        memory.render_list()
        memory.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        text = memory.query_one("#memory-text", Static)
        assert "Commitment: Companion" in str(text.render())
        assert "Commitment: User" in str(text.render())
        assert "[bold]Literal[/bold] {{kept}}" in str(text.render())
        scroll = memory.query_one("#memory-detail-scroll")
        scroll.focus()
        await pilot.press("end")
        assert scroll.scroll_y == scroll.max_scroll_y
        await pilot.press("escape", "escape")
        assert app.focused is composer and composer.text == "Unsent draft 🌿"
    assert (client.memory, client.run_changes) == original
    assert not any(method in {"memory_edit_note", "memory_create_note", "memory_delete_note"}
                   for method, _ in client.calls)


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_preference_help_keeps_follow_reply_control_usable(identity_config, size):
    client = FakeClient()
    client.configuration = identity_config.model_dump(mode="json")
    app = RoomApp(identity_config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        follow = next(field for field in FIELDS if field.name == "follow_replies")
        assert follow.help == "Scroll with the reply while you are at the bottom."
        assert follow.help in [str(help.render()) for help in screen.query(".field-help")]
        await pilot.press("tab", "tab")
        checkbox = screen.query_one("#pref-follow_replies", Checkbox)
        assert app.focused is checkbox
        assert checkbox.region.height == 1
        assert screen.query_one("#preference-conversation").content_region.contains_region(
            checkbox.content_region,
        )
        await pilot.press("space", "ctrl+s")
        await pilot.pause()
        assert not app.config.preferences.follow_replies
        assert app.config.identity == identity_config.identity
