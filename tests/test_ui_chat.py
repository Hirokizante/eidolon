import asyncio

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.document._document import Selection
from textual.geometry import Region
from textual.worker import WorkerCancelled
from textual.widgets import Input, Markdown, Static

from eidolon.config import AppConfig
from eidolon.ui.app import Composer, RoomApp, ThinkingSpinner, message_timestamp
from test_ui import FakeClient


@pytest.mark.parametrize("names", [
    ("Ari 雪 e\u0301", "Fern 👩\u200d💻"),
    ("[bold]Kai[/] {{user}}", "[red]Moss[/] {{companion}}"),
    ("Same name", "Same name"),
    ("界" * 80, "雪" * 80),
])
@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_configured_live_headings_are_literal_and_keep_compact_frames(names, size):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=dict(zip(("user_name", "companion_name"), names)))
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for role in ("user", "assistant"):
            await app.append_message(role, "Unchanged **message**", created_at="2026-10-19T00:00Z")
        await pilot.pause()
        conversation = app.query_one("#conversation", VerticalScroll)
        for frame, name in zip(app.query(".message-frame"), names, strict=True):
            assert Text.from_markup(frame.border_title).plain == name
            title = frame.render_lines(Region(0, 0, frame.region.width, 1))[0].text
            if Text(name).cell_len <= frame.content_region.width:
                assert name in title
            else:
                assert title.startswith("╭") and "…" in title
            assert frame.border_subtitle == "2026-10-19 08:00"
            assert frame.region.width <= conversation.content_region.width
            assert frame.region.height == frame.query_one(Markdown).region.height + 2
            assert frame.query_one(Markdown).source == "Unchanged **message**"
        other = await app.append_message("system", "A system message")
        assert other.parent.border_title == "System"
        assert app.query_one(Composer).region.bottom <= size[1]
        assert app.config.identity.model_dump() == client.configuration["identity"]


@pytest.mark.parametrize("stream_tokens", [False, True])
async def test_turn_started_reconciles_optimistic_names_and_survives_keyboard_rename(stream_tokens):
    original = {"user_name": "Ari", "companion_name": "Fern"}
    captured = {"user_name": "[bold]Kai[/]", "companion_name": "[red]Moss[/]"}
    renamed = {"user_name": "Noor 雪", "companion_name": "Willow"}
    config = AppConfig(identity=original, memory={"bank": "hermes", "enabled": False})

    class IdentityClient(FakeClient):
        async def stream(self, method, **params):
            await self.start_gate.wait()
            snapshot = dict(captured if not self.turns else self.configuration["identity"])
            self.turns += 1
            yield {"type": "turn_started", "turn_id": f"turn-{self.turns}", "identity": snapshot}
            yield {"type": "model_request"}
            await self.response_gate.wait()
            if stream_tokens:
                yield {"type": "token", "text": "A **reply**"}
                await self.tool_gate.wait()
            yield {"type": "model_request", "round_index": 1}
            await self.finish_gate.wait()
            yield {"type": "completed", "text": "A **reply**", "metadata": {
                "identity": snapshot, "user_message_at": "2026-10-19T01:00Z",
            }, "turn": {"created_at": "2026-10-19T01:01Z"}}

    client = IdentityClient()
    client.turns = 0
    client.start_gate, client.response_gate, client.tool_gate, client.finish_gate = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    )
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        worker = app.generate("Hello")
        await pilot.pause()
        user_frame = app.query_one(".message-frame")
        assert user_frame.border_title == original["user_name"]
        stamp = user_frame.border_subtitle
        client.start_gate.set()
        await pilot.pause()
        assert Text.from_markup(user_frame.border_title).plain == captured["user_name"]
        assert user_frame.border_subtitle == stamp
        notice = app.query_one("#notice", Static)
        assert captured["companion_name"] in str(notice.render())
        composer = app.query_one(Composer)
        composer.load_text("Keep this draft 🌱")
        composer.selection = Selection((0, 1), (0, 5))
        selection = composer.selection
        app.preferences_state.category = "identity"
        await pilot.press("alt+6")
        for field, value in renamed.items():
            app.screen.query_one(f"#pref-{field}", Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.identity.model_dump() == renamed
        assert app.query_one(".message-frame") is user_frame
        assert Text.from_markup(user_frame.border_title).plain == captured["user_name"]
        assert captured["companion_name"] in str(notice.render())
        assert renamed["companion_name"] in str(composer.placeholder)
        assert app.focused is composer and composer.text == "Keep this draft 🌱"
        assert composer.selection == selection
        await pilot.press("enter")
        assert "draft is kept" in str(notice.render())
        client.response_gate.set()
        await pilot.pause()
        assert captured["companion_name"] in str(notice.render())
        if stream_tokens:
            assert "is replying" in str(notice.render())
            assert Text.from_markup(app.query(".message-frame")[-1].border_title).plain == (
                captured["companion_name"]
            )
            client.tool_gate.set()
            await pilot.pause()
            assert captured["companion_name"] in str(notice.render())
            assert "is thinking" in str(notice.render())
        client.finish_gate.set()
        await worker.wait()
        await pilot.pause()
        assert [Text.from_markup(frame.border_title).plain
                for frame in app.query(".message-frame")] == list(captured.values())
        assert [frame.border_subtitle for frame in app.query(".message-frame")] == [
            "2026-10-19 09:00", "2026-10-19 09:01",
        ]
        assert app.query(".message-frame")[-1].query_one(Markdown).source == "A **reply**"
        await app.generate("Next turn").wait()
        await pilot.pause()
        assert [Text.from_markup(frame.border_title).plain
                for frame in app.query(".message-frame")][-2:] == list(renamed.values())


@pytest.mark.parametrize("size", [(140, 40), (80, 24), (60, 18), (48, 12)])
async def test_saved_and_streamed_messages_have_compact_sender_frames(size):
    client = FakeClient()
    client.messages = [
        {"role": "user", "content": "Hello 雨 🌸 cafe\u0301", "images": [{"id": "example"}],
         "created_at": "2026-10-18T15:59:00+00:00"},
        {"role": "assistant", "content": "A little **welcome**.\n\nAnother paragraph.",
         "created_at": "2026-10-18T16:01:06+00:00"},
    ]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        frames = list(app.query("#conversation .message-frame"))
        assert [frame.border_title for frame in frames] == ["You", "Companion"]
        assert [frame.border_subtitle for frame in frames] == [
            "2026-10-18 23:59", "2026-10-19 00:01",
        ]
        assert frames[0].query_one(Markdown).source == client.messages[0]["content"]
        indicator = frames[0].query_one(".message-attachments", Static)
        assert "1 image attached" in str(indicator.render())
        assert frames[1].query_one(Markdown).source == client.messages[1]["content"]
        conversation = app.query_one("#conversation", VerticalScroll)
        for frame in frames:
            assert frame.region.width <= conversation.content_region.width
            assert frame.query_one(Markdown).region.width == frame.content_region.width
            assert frame.region.height == sum(child.region.height for child in frame.children) + 2
            footer = frame.render_lines(Region(0, frame.region.height - 1, frame.region.width, 1))
            assert frame.border_subtitle in footer[0].text
        assert indicator.region.height == 1
        assert indicator.region.width == frames[0].content_region.width
        assert not app.query(".speaker")
        composer = app.query_one(Composer)
        composer.load_text("A new message")
        await pilot.press("enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        frames = list(app.query("#conversation .message-frame"))
        assert [frame.border_title for frame in frames] == ["You", "Companion", "You", "Companion"]
        assert all(frame.border_subtitle for frame in frames)
        assert frames[-1].query_one(Markdown).source == app.reply_text
        assert frames[-1].region.bottom <= conversation.content_region.bottom
        assert composer.region.bottom <= size[1]
        assert app.focused is composer


async def test_history_preview_uses_the_same_user_attribution():
    client = FakeClient()
    client.messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Welcome back"},
    ]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+2", "enter")
        await pilot.pause()
        preview = app.screen.query_one(Markdown).source
        assert "**You**\n\nHello" in preview
        assert "**Companion**\n\nWelcome back" in preview
        assert "**Alex**" not in preview


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_loaded_identity_and_fallback_rename_preserve_chat_state(size, ascii_mode, monkeypatch):
    if ascii_mode:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    original = {"user_name": "Ari 雪", "companion_name": "Fern e\u0301"}
    recorded = {"user_name": "[bold]Kai[/]", "companion_name": "[red]Moss[/]"}
    renamed = {"user_name": "Noor", "companion_name": "Willow 👩\u200d💻"}
    client = FakeClient()
    client.configuration["identity"] = original
    client.messages = [
        {"role": role, "content": f"**Saved** body {index}\n\nSecond paragraph.",
         "created_at": "2026-10-19T01:00Z", "images": [{"id": "synthetic"}],
         **({"identity": recorded.copy()} if index % 2 else {})}
        for index in range(6) for role in ("user", "assistant")
    ] + [{"role": "system", "content": "System **body**"}]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=original), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        conversation = app.query_one("#conversation", VerticalScroll)
        frames = list(app.query(".message-frame"))
        expected = [
            (recorded if index % 2 else original)[key]
            for index in range(6) for key in ("user_name", "companion_name")
        ] + ["System"]
        assert [Text.from_markup(frame.border_title).plain for frame in frames] == expected
        bodies = [frame.query_one(Markdown) for frame in frames]
        stamps = [frame.border_subtitle for frame in frames]
        attachments = list(app.query(".message-attachments"))
        composer = app.query_one(Composer)
        composer.load_text("Keep draft 🌱")
        composer.selection = Selection((0, 1), (0, 5))
        selection = composer.selection
        conversation.scroll_to(y=7, animate=False)
        await pilot.pause()
        position = conversation.scroll_y
        client.configuration["identity"] = renamed
        app.preferences_saved(client.configuration)
        await pilot.pause()
        assert list(app.query(".message-frame")) == frames
        assert [Text.from_markup(frame.border_title).plain for frame in frames] == [
            (recorded if index % 2 else renamed)[key]
            for index in range(6) for key in ("user_name", "companion_name")
        ] + ["System"]
        assert [frame.query_one(Markdown) for frame in frames] == bodies
        assert [body.source for body in bodies] == [m["content"] for m in client.messages]
        assert [frame.border_subtitle for frame in frames] == stamps
        assert list(app.query(".message-attachments")) == attachments
        assert app.focused is composer and composer.text == "Keep draft 🌱"
        assert composer.selection == selection and conversation.scroll_y == position
        assert all("identity" not in m for m in client.messages[:2])
        assert client.messages[2]["identity"] == recorded
        await app.load_conversation(client.session["id"])
        await pilot.pause()
        assert [Text.from_markup(frame.border_title).plain
                for frame in app.query(".message-frame")] == [
            (recorded if index % 2 else renamed)[key]
            for index in range(6) for key in ("user_name", "companion_name")
        ] + ["System"]


@pytest.mark.parametrize("stream_tokens", [False, True])
async def test_live_timestamps_use_saved_times_on_completion_and_reload(stream_tokens):
    user_at = "2026-10-18T15:59:00+00:00"
    reply_at = "2026-10-18T16:01:06+00:00"

    class TimestampClient(FakeClient):
        async def stream(self, method, **params):
            if stream_tokens:
                yield {"type": "token", "text": "Welcome back"}
            await self.response_gate.wait()
            self.messages = [
                {"role": "user", "content": params["text"], "created_at": user_at},
                {"role": "assistant", "content": "Welcome back", "created_at": reply_at},
            ]
            yield {"type": "completed", "text": "Welcome back",
                   "metadata": {"user_message_at": user_at}, "turn": {"created_at": reply_at}}

    client = TimestampClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        worker = app.generate("Hello")
        await pilot.pause()
        frames = list(app.query(".message-frame"))
        assert len(frames) == (2 if stream_tokens else 1)
        assert all(frame.border_subtitle for frame in frames)
        client.response_gate.set()
        await worker.wait()
        await pilot.pause()
        frames = list(app.query(".message-frame"))
        expected = ["2026-10-18 23:59", "2026-10-19 00:01"]
        assert [frame.border_subtitle for frame in frames] == expected
        assert [frame.query_one(Markdown).source for frame in frames] == ["Hello", "Welcome back"]
        await app.load_conversation(app.session_id)
        await pilot.pause()
        assert [frame.border_subtitle for frame in app.query(".message-frame")] == expected


@pytest.mark.parametrize("value", ["", "invalid", "2026-10-19T10:00:00"])
def test_unavailable_message_time_is_not_replaced_with_the_current_time(value):
    assert message_timestamp(value, "Asia/Manila") == ""


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("monochrome", [False, True])
async def test_sender_frames_follow_live_theme_and_accessibility_modes(
    ascii_mode, monochrome, monkeypatch
):
    monkeypatch.delenv("EIDOLON_ASCII", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    if ascii_mode:
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    if monochrome:
        monkeypatch.setenv("NO_COLOR", "1")
    names = {"user_name": "[bold]Kai[/]", "companion_name": "[red]Fern[/]"}
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=names)
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await app.append_message("user", "Hello", images=[{"id": "example"}])
        await app.append_message("assistant", "Welcome")
        app.apply_custom_theme({"primary": "#7aa2f7", "accent": "#de90bd"})
        await pilot.pause()
        frames = list(app.query(".message-frame"))
        for frame, token in zip(frames, ("accent", "primary"), strict=True):
            color = app.theme_variables["foreground" if monochrome else token]
            assert frame.styles.border.top[0] == ("ascii" if ascii_mode else "round")
            assert frame.styles.border.top[1].hex == color
            assert frame.styles.border_title_color.hex == color
            assert frame.styles.border_subtitle_color == app.query_one("#notice").styles.color
            assert frame.styles.border_subtitle_align == "right"
            assert not frame.styles.border_subtitle_style.bold
        assert [Text.from_markup(frame.border_title).plain
                for frame in frames] == list(names.values())
        for frame, name in zip(frames, names.values(), strict=True):
            assert name in frame.render_lines(Region(0, 0, frame.region.width, 1))[0].text
        indicator = frames[0].query_one(".message-attachments", Static)
        color = app.theme_variables["foreground" if monochrome else "accent"]
        assert indicator.styles.color.hex == color
        assert indicator.rich_style.italic
        assert str(indicator.render()) == (
            "[1 image attached]" if ascii_mode else "[📷 1 image attached]"
        )
        assert not frames[1].query(".message-attachments")


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
@pytest.mark.parametrize("ascii_mode,monochrome", [(False, False), (True, True)])
async def test_long_names_keep_progress_and_composer_usable(
    size, ascii_mode, monochrome, monkeypatch,
):
    for variable, enabled in (("EIDOLON_ASCII", ascii_mode), ("NO_COLOR", monochrome)):
        monkeypatch.delenv(variable, raising=False)
        if enabled:
            monkeypatch.setenv(variable, "1")
    config = AppConfig(identity={"user_name": "界" * 80, "companion_name": "雪" * 80},
                       memory={"bank": "hermes", "enabled": False})
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    client.response_gate = asyncio.Event()
    app = RoomApp(config, client=client)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        worker = app.generate("Hello")
        await pilot.pause()
        await pilot.resize_terminal(*size)
        await pilot.pause()
        notice = app.query_one("#notice", Static)
        assert "Ctrl+C cancels" in str(notice.render())
        assert notice.region.height == 1
        assert "Ctrl+C cancels" in notice.render_line(0).text
        assert ("..." if ascii_mode else "…") in str(notice.render())
        composer = app.query_one(Composer)
        assert "/ for commands" in str(composer.placeholder)
        assert "/ for commands" in composer.render_line(0).text
        assert app.query_one("#conversation").region.height > 0
        assert composer.region.bottom <= size[1]
        assert app.focused is composer
        client.response_gate.set()
        await worker.wait()
        await pilot.pause()
        assert [Text.from_markup(frame.border_title).plain
                for frame in app.query(".message-frame")] == ["界" * 80, "雪" * 80]
        assert app.config.identity.model_dump() == client.configuration["identity"]


@pytest.mark.parametrize("outcome", ["completed", "error", "recall_required", "exception", "cancel"])
@pytest.mark.parametrize("ascii_mode", [False, True])
async def test_thinking_spinner_animates_only_during_active_work(outcome, ascii_mode, monkeypatch):
    monkeypatch.delenv("EIDOLON_ASCII", raising=False)
    if ascii_mode:
        monkeypatch.setenv("EIDOLON_ASCII", "1")

    reply_gate = asyncio.Event()

    class GatedClient(FakeClient):
        async def stream(self, method, **params):
            yield {"type": "model_request"}
            await self.response_gate.wait()
            if outcome == "exception":
                raise ConnectionError("Disconnected")
            if outcome == "completed":
                yield {"type": "token", "text": "A reply"}
                await reply_gate.wait()
            yield {"type": outcome, "message": "Please retry"}

    client = GatedClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        spinner = app.query_one(ThinkingSpinner)
        assert not spinner.display and spinner.auto_refresh is None
        clock = 10.0
        monkeypatch.setattr(app.console, "get_time", lambda: clock)
        worker = app.generate("Hello")
        await pilot.pause()
        assert app.busy and spinner.display
        assert spinner.auto_refresh is not None
        notice = app.query_one("#notice", Static)
        assert "thinking" in str(notice.render())
        assert spinner.region.right <= notice.region.x
        assert spinner.region.y == notice.region.y
        before = spinner.render_line(0).text
        clock += spinner.spinner.interval / 1000 + 0.001
        await pilot.pause(0.15)
        assert spinner.render_line(0).text != before
        if ascii_mode:
            assert before.isascii() and spinner.render_line(0).text.isascii()
        if outcome == "cancel":
            worker.cancel()
            with pytest.raises(WorkerCancelled):
                await worker.wait()
        else:
            client.response_gate.set()
            if outcome == "completed":
                await pilot.pause()
                assert spinner.display and spinner.auto_refresh is not None
                assert "replying" in str(notice.render())
                reply_gate.set()
            await worker.wait()
        await pilot.pause()
        assert not app.busy
        assert not spinner.display and spinner.auto_refresh is None
        if outcome == "completed":
            assert not app.query_one("#turn-status").display
        elif outcome != "cancel":
            assert notice.has_class("problem") and notice.display


async def test_thinking_spinner_respects_disabled_animations():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.animation_level = "none"
        app.busy = True
        app.set_notice("Eidolon is thinking…")
        spinner = app.query_one(ThinkingSpinner)
        assert spinner.display and spinner.auto_refresh is None
        assert spinner.render() == "*"
        app.busy = False
        app.set_notice("")
        assert not spinner.display


@pytest.mark.parametrize("reading_history", [False, True])
@pytest.mark.parametrize("follow_replies", [False, True])
async def test_streaming_message_frames_preserve_scroll_intent(reading_history, follow_replies):
    class StreamingClient(FakeClient):
        async def stream(self, method, **params):
            yield {"type": "turn_started", "turn_id": "scroll-turn",
                   "identity": {"user_name": "Ari", "companion_name": "Fern"}}
            yield {"type": "token", "text": "A reply is starting."}
            await self.response_gate.wait()
            yield {"type": "token", "text": "\n\n" + "More of the reply.\n\n" * 20}
            yield {"type": "completed"}

    client = StreamingClient()
    client.messages = [
        {"role": role, "content": f"Earlier message {index}"}
        for index in range(8)
        for role in ("user", "assistant")
    ]
    client.response_gate = asyncio.Event()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, preferences={"follow_replies": follow_replies})
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        worker = app.generate("Hello")
        await pilot.pause()
        conversation = app.query_one("#conversation", VerticalScroll)
        if reading_history:
            conversation.scroll_home(animate=False)
            await pilot.pause()
        position = conversation.scroll_y
        frames = list(conversation.children)
        client.configuration["identity"] = {"user_name": "Noor", "companion_name": "Willow"}
        app.preferences_saved(client.configuration)
        await pilot.pause()
        assert list(conversation.children) == frames
        assert conversation.scroll_y == position
        assert [frame.border_title for frame in frames[:-2]] == [
            name for _ in range(8) for name in ("Noor", "Willow")
        ]
        assert [frame.border_title for frame in frames[-2:]] == ["Ari", "Fern"]
        client.response_gate.set()
        await worker.wait()
        await pilot.pause()
        if reading_history or not follow_replies:
            assert conversation.scroll_y == position
        else:
            assert conversation.is_vertical_scroll_end
        assert app.reply_widget.source == app.reply_text
        assert [frame.border_title for frame in app.query(".message-frame")][-2:] == ["Ari", "Fern"]
