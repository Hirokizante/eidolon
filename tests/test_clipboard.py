import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from eidolon import clipboard


PNG = b"\x89PNG\r\n\x1a\nclipboard image"


@pytest.fixture(autouse=True)
def macos(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")


async def test_clipboard_png_and_empty_clipboard(monkeypatch):
    command = AsyncMock(side_effect=[base64.b64encode(PNG) + b"\n", b"\n"])
    monkeypatch.setattr(clipboard, "_read_command", command)
    assert await clipboard.read_clipboard_image() == PNG
    assert await clipboard.read_clipboard_image() is None
    assert command.call_args.args[:3] == ("osascript", "-l", "JavaScript")


@pytest.mark.parametrize("encoded", [b"not base64", base64.b64encode(b"not PNG")])
async def test_invalid_clipboard_image_is_actionable(monkeypatch, encoded):
    monkeypatch.setattr(clipboard, "_read_command", AsyncMock(return_value=encoded))
    with pytest.raises(clipboard.ClipboardError, match="clipboard image"):
        await clipboard.read_clipboard_image()


async def test_clipboard_size_limit(monkeypatch):
    monkeypatch.setattr(clipboard, "MAX_CLIPBOARD_IMAGE_BYTES", len(PNG) - 1)
    monkeypatch.setattr(
        clipboard, "_read_command", AsyncMock(return_value=base64.b64encode(PNG))
    )
    with pytest.raises(clipboard.ClipboardError, match="10 MiB"):
        await clipboard.read_clipboard_image()


async def test_text_paste_keeps_unicode(monkeypatch):
    command = AsyncMock(return_value="雨 🌸 café\n".encode())
    monkeypatch.setattr(clipboard, "_read_command", command)
    assert await clipboard.read_clipboard_text() == "雨 🌸 café\n"
    command.assert_awaited_once_with("pbpaste")


async def test_non_macos_clipboard_explains_requirement(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    with pytest.raises(clipboard.ClipboardError, match="macOS"):
        await clipboard.read_clipboard_image()


@pytest.mark.parametrize("cancel", [False, True])
async def test_clipboard_timeout_and_cancellation_terminate_child(monkeypatch, cancel):
    class Process:
        returncode = None
        killed = False
        reads = 0

        async def communicate(self):
            self.reads += 1
            if not self.killed:
                await asyncio.Event().wait()
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()
    monkeypatch.setattr(
        clipboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(clipboard, "CLIPBOARD_TIMEOUT", 0.01 if not cancel else 5)
    task = asyncio.create_task(clipboard._read_command("osascript"))
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else clipboard.ClipboardError):
        await task
    assert process.killed and process.reads == 2


async def test_clipboard_error_does_not_expose_subprocess_output(monkeypatch):
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (b"", b"sensitive clipboard contents")
    monkeypatch.setattr(
        clipboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(clipboard.ClipboardError, match="Could not read") as error:
        await clipboard._read_command("osascript")
    assert "sensitive" not in str(error.value)
