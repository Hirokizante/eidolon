"""Read the local macOS clipboard without blocking the terminal event loop."""

from __future__ import annotations

import asyncio
import base64
import binascii
import sys


MAX_CLIPBOARD_IMAGE_BYTES = 10 * 1024 * 1024
CLIPBOARD_TIMEOUT = 5

# AppKit can convert screenshot TIFF data into PNG as well as read copied PNGs.
# Returning base64 avoids temporary copies of private images on disk.
_IMAGE_SCRIPT = r"""
ObjC.import('AppKit');
function run() {
    const board = $.NSPasteboard.generalPasteboard;
    let data = board.dataForType($.NSPasteboardTypePNG);
    if (!data || data.isNil()) {
        let tiff = board.dataForType($.NSPasteboardTypeTIFF);
        if (!tiff || tiff.isNil()) {
            const image = $.NSImage.alloc.initWithPasteboard(board);
            if (!image || image.isNil()) return '';
            tiff = image.TIFFRepresentation;
        }
        if (!tiff || tiff.isNil()) return '';
        const bitmap = $.NSBitmapImageRep.imageRepWithData(tiff);
        if (!bitmap || bitmap.isNil()) return '';
        data = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
    }
    if (!data || data.isNil()) return '';
    if (Number(data.length) > 10485760) throw Error('Clipboard image exceeds 10 MiB.');
    return ObjC.unwrap(data.base64EncodedStringWithOptions(0));
}
"""


class ClipboardError(ValueError):
    """A clipboard read failed in a way the user can act on."""


async def _read_command(*arguments: str) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        raise ClipboardError("Could not open the macOS clipboard. Try /paste again.") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), CLIPBOARD_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ClipboardError("Clipboard read timed out. Try /paste again.") from exc
    if process.returncode:
        if b"exceeds 10 MiB" in stderr:
            raise ClipboardError("Clipboard image exceeds 10 MiB. Copy a smaller image.")
        raise ClipboardError("Could not read the macOS clipboard. Copy the image and try /paste.")
    return stdout


async def read_clipboard_image() -> bytes | None:
    """Return a PNG for a clipboard image, or None when no image is present."""
    if sys.platform != "darwin":
        raise ClipboardError("Image clipboard paste is available on macOS.")
    encoded = (await _read_command("osascript", "-l", "JavaScript", "-e", _IMAGE_SCRIPT)).strip()
    if not encoded:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ClipboardError("The clipboard image could not be read. Copy it again.") from exc
    if len(data) > MAX_CLIPBOARD_IMAGE_BYTES:
        raise ClipboardError("Clipboard image exceeds 10 MiB. Copy a smaller image.")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ClipboardError("The clipboard image could not be converted to PNG.")
    return data


async def read_clipboard_text() -> str:
    """Read text for /paste or a terminal that forwards the Command+V key."""
    if sys.platform != "darwin":
        raise ClipboardError("Clipboard paste is available on macOS.")
    return (await _read_command("pbpaste")).decode("utf-8", errors="replace")
