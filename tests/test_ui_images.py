import asyncio
import base64
from unittest.mock import AsyncMock

import pytest
from textual import events
from textual.widgets import Markdown, Static
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import HelpScreen
from test_ui import FakeClient


PNG = b"\x89PNG\r\n\x1a\nclipboard image"


class ImageClient(FakeClient):
    async def request(self, method, **params):
        if method == "upload_image":
            self.calls.append((method, params))
            index = sum(method == "upload_image" for method, _ in self.calls)
            return {"id": f"image{index}", "filename": params["filename"], "media_type": "image/png"}
        return await super().request(method, **params)


@pytest.fixture
def clipboard_image(monkeypatch):
    reader = AsyncMock(return_value=PNG)
    monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", reader)
    return reader


async def test_command_v_and_slash_paste_attach_images_without_changing_text(clipboard_image):
    client = ImageClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("What changed? 雨")
        await pilot.press("super+v")
        await pilot.pause()
        assert len(app.draft_images) == 1 and composer.text == "What changed? 雨"
        app.execute_command("/paste")
        await pilot.pause()
        assert len(app.draft_images) == 2
        upload = [params for method, params in client.calls if method == "upload_image"]
        assert base64.b64decode(upload[0]["data_base64"]) == PNG
        assert upload[0]["media_type"] == "image/png"
        assert "2 images attached" in str(app.query_one("#attachments", Static).render())
        await pilot.press("enter")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        sent = next(params for method, params in client.calls if method == "generate")
        assert sent["images"] == ["image1", "image2"]
        assert sent["text"] == "What changed? 雨"
        frame = app.query("#conversation .message-frame").first()
        assert frame.query_one(Markdown).source == "What changed? 雨"
        assert str(frame.query_one(".message-attachments", Static).render()) == (
            "[📷 2 images attached]"
        )
        assert not app.draft_images and not app.pending_images
        assert not app.query_one("#attachments").display


async def test_terminal_text_paste_never_attaches_stale_clipboard_image(clipboard_image):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ImageClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.post_message(events.Paste("pasted text 雨"))
        await pilot.pause()
        assert composer.text == "pasted text 雨"
        assert not app.draft_images
        clipboard_image.assert_not_awaited()
        composer.post_message(events.Paste(""))
        await pilot.pause()
        assert len(app.draft_images) == 1


async def test_slash_paste_falls_back_to_native_text(monkeypatch):
    monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", AsyncMock(return_value=None))
    monkeypatch.setattr("eidolon.ui.app.read_clipboard_text", AsyncMock(return_value="雨 🌸"))
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ImageClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.execute_command("/paste")
        await pilot.pause()
        assert app.query_one(Composer).text == "雨 🌸"
        assert not app.draft_images


async def test_native_text_paste_replaces_selection_and_can_be_undone(monkeypatch):
    monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", AsyncMock(return_value=None))
    monkeypatch.setattr("eidolon.ui.app.read_clipboard_text", AsyncMock(return_value="雨 🌸"))
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ImageClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Please replace this")
        composer.selection = Selection((0, 7), (0, 14))
        app.execute_command("/paste")
        await pilot.pause()
        assert composer.text == "Please 雨 🌸 this"
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert composer.text == "Please replace this"


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_image_draft_survives_modal_resize_and_clear(size, clipboard_image):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ImageClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent caption 雨")
        app.paste_clipboard()
        await pilot.pause()
        indicator = app.query_one("#attachments", Static)
        assert indicator.region.height == 1
        assert indicator.region.bottom == composer.region.y
        assert composer.region.bottom <= app.size.height - 1
        app.execute_command("/help")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("super+v")
        assert len(app.draft_images) == 1
        await pilot.press("escape")
        await pilot.resize_terminal(47, 11)
        await pilot.pause()
        assert app.screen.has_class("too-small") and app.draft_images
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert composer.text == "Unsent caption 雨" and app.focused is composer
        app.execute_command("/clear-attachments")
        await pilot.pause()
        assert not app.draft_images and not indicator.display
        assert composer.text == "Unsent caption 雨"


async def test_image_only_message_survives_recall_retry(clipboard_image):
    client = ImageClient()
    client.fail_recall = True
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.awaiting_recall and app.pending_text == ""
        assert app.pending_images[0]["id"] == "image1"
        assert "1 image waiting" in str(app.query_one("#attachments", Static).render())
        client.fail_recall = False
        app.execute_command("/retry")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        sent = [params for method, params in client.calls if method == "generate"]
        assert len(sent) == 2
        assert all(params["images"] == ["image1"] and params["text"] == "" for params in sent)
        assert app.pending_text is None and not app.pending_images


async def test_image_limits_upload_failure_and_paste_are_responsive(monkeypatch, clipboard_image):
    class OfflineImageClient(ImageClient):
        fail_upload = False

        async def request(self, method, **params):
            if method == "upload_image" and self.fail_upload:
                raise ConnectionError("The service is offline")
            return await super().request(method, **params)

    client = OfflineImageClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            app.paste_clipboard()
            await pilot.pause()
        assert len(app.draft_images) == 4
        assert "up to 4 images" in str(app.query_one("#notice", Static).render())
        app.execute_command("/clear-attachments")
        await pilot.pause()
        client.fail_upload = True
        app.paste_clipboard()
        await pilot.pause()
        assert not app.draft_images
        assert "offline" in str(app.query_one("#notice", Static).render())
        gate = asyncio.Event()

        async def slow_clipboard():
            await gate.wait()
            return PNG

        monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", slow_clipboard)
        app.paste_clipboard()
        await pilot.pause()
        await pilot.press("a", "enter")
        assert app.query_one(Composer).text == "a" and not app.busy
        assert "Attaching clipboard image" in str(app.query_one("#notice", Static).render())
        gate.set()
        await pilot.pause()


@pytest.mark.parametrize("caption, count", [
    ("", 1),
    ("What changed? **雨**", 2),
    ("```text\nAn unfinished code block", 1),
])
async def test_history_renders_image_attachments_separately_from_message_markdown(caption, count):
    client = ImageClient()
    client.messages = [{
        "role": "user", "content": caption,
        "images": [{"id": f"earlier{index}"} for index in range(count)],
    }]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        frame = app.query_one("#conversation .message-frame")
        assert frame.query_one(Markdown).source == caption
        indicator = frame.query_one(".message-attachments", Static)
        assert str(indicator.render()) == f"[📷 {count} image{'s' if count != 1 else ''} attached]"
        assert indicator.region.height == 1
        assert indicator.region.y >= frame.query_one(Markdown).region.bottom
        if not caption:
            assert frame.region.height == 3


@pytest.mark.parametrize("next_image_draft", [False, True])
async def test_generation_error_preserves_images_and_a_new_draft(clipboard_image, next_image_draft):
    class FailingClient(ImageClient):
        failed = False

        async def stream(self, method, **params):
            if not self.failed:
                self.failed = True
                self.calls.append((method, params))
                await self.response_gate.wait()
                yield {"type": "error", "message": "Connection lost"}
            else:
                async for event in super().stream(method, **params):
                    yield event

    client = FailingClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        app.query_one(Composer).load_text("Original image question")
        await pilot.press("enter")
        await pilot.pause()
        if next_image_draft:
            app.paste_clipboard()
            await pilot.pause()
        client.response_gate.set()
        await pilot.pause()
        if next_image_draft:
            assert app.query_one(Composer).text == ""
            assert [image["id"] for image in app.draft_images] == ["image2"]
        else:
            assert app.query_one(Composer).text == "Original image question"
            assert [image["id"] for image in app.draft_images] == ["image1"]
        app.execute_command("/retry")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        sent = [params for method, params in client.calls if method == "generate"]
        assert sent[-1]["images"] == ["image1"]
        assert [image["id"] for image in app.draft_images] == (["image2"] if next_image_draft else [])


@pytest.mark.parametrize("caption", ["Question", ""])
async def test_clear_attachments_removes_images_from_recall_waiting_message(clipboard_image, caption):
    client = ImageClient()
    client.fail_recall = True
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        app.query_one(Composer).load_text(caption)
        await pilot.press("enter")
        await pilot.pause()
        assert app.awaiting_recall and app.pending_images
        app.execute_command("/clear-attachments")
        await pilot.pause()
        assert not app.pending_images and not app.draft_images
        assert not app.query_one("#attachments").display
        if caption:
            app.execute_command("/retry without-memory")
            await asyncio.wait_for(client.completed.wait(), 3)
            await pilot.pause()
            sent = [params for method, params in client.calls if method == "generate"]
            assert "images" not in sent[-1] and sent[-1]["text"] == caption
        else:
            assert app.pending_text is None and not app.awaiting_recall
            assert not list(app.query("#conversation Markdown"))


async def test_clear_restored_attachments_prevents_retry_from_resending_hidden_images(clipboard_image):
    class ErrorClient(ImageClient):
        failed = False

        async def stream(self, method, **params):
            if not self.failed:
                self.failed = True
                self.calls.append((method, params))
                yield {"type": "error", "message": "Connection lost"}
            else:
                async for event in super().stream(method, **params):
                    yield event

    client = ErrorClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        app.query_one(Composer).load_text("Question")
        await pilot.press("enter")
        await pilot.pause()
        assert app.draft_images and app.pending_images
        app.execute_command("/clear-attachments")
        await pilot.pause()
        app.execute_command("/retry")
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        sent = [params for method, params in client.calls if method == "generate"]
        assert "images" not in sent[-1]


@pytest.mark.parametrize("action", ["clear-attachments", "today"])
@pytest.mark.parametrize("waiting_for", ["clipboard", "upload"])
async def test_clear_or_room_change_invalidates_in_flight_image_paste(monkeypatch, action, waiting_for):
    gate = asyncio.Event()

    async def read_image():
        if waiting_for == "clipboard":
            await gate.wait()
        return PNG

    class SlowClient(ImageClient):
        async def request(self, method, **params):
            if method == "upload_image" and waiting_for == "upload":
                await gate.wait()
            return await super().request(method, **params)

    monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", read_image)
    client = SlowClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        assert app.pasting_image
        app.execute_command("/" + action)
        await pilot.pause()
        assert not app.pasting_image
        gate.set()
        await pilot.pause()
        assert not app.draft_images
        assert not app.query_one("#attachments").display


@pytest.mark.parametrize("change", ["modal", "new_text"])
async def test_late_native_text_paste_does_not_edit_changed_or_hidden_composer(monkeypatch, change):
    gate = asyncio.Event()

    async def read_text():
        await gate.wait()
        return "old clipboard text"

    monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", AsyncMock(return_value=None))
    monkeypatch.setattr("eidolon.ui.app.read_clipboard_text", read_text)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ImageClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Draft")
        app.paste_clipboard()
        await pilot.pause()
        if change == "modal":
            app.execute_command("/help")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
        else:
            await pilot.press("a")
        text_before = composer.text
        gate.set()
        await pilot.pause()
        assert composer.text == text_before
        if change == "modal":
            assert isinstance(app.screen, HelpScreen)


async def test_image_paste_completes_behind_modal_without_stealing_focus(monkeypatch):
    gate = asyncio.Event()

    async def read_image():
        await gate.wait()
        return PNG

    monkeypatch.setattr("eidolon.ui.app.read_clipboard_image", read_image)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=ImageClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        app.execute_command("/help")
        await pilot.pause()
        focused = app.focused
        gate.set()
        await pilot.pause()
        assert app.draft_images
        assert isinstance(app.screen, HelpScreen) and app.focused is focused


async def test_clear_draft_images_preserves_images_in_active_response(clipboard_image):
    client = ImageClient()
    client.response_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.paste_clipboard()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.busy
        app.paste_clipboard()
        await pilot.pause()
        assert app.draft_images[0]["id"] == "image2"
        app.execute_command("/clear-attachments")
        await pilot.pause()
        assert app.pending_images[0]["id"] == "image1" and not app.draft_images
        client.response_gate.set()
        await asyncio.wait_for(client.completed.wait(), 3)
        await pilot.pause()
        sent = next(params for method, params in client.calls if method == "generate")
        assert sent["images"] == ["image1"]
