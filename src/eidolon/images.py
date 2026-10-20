"""Private clipboard image attachments, referenced by opaque IDs in transcripts."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import os
import re
import struct
import tempfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

from eidolon.config import private_directory


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 4
MAX_REQUEST_IMAGE_BYTES = MAX_IMAGE_BYTES * MAX_IMAGES
MAX_UPLOAD_BYTES = 14 * 1024 * 1024 + 4096
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Validate bounded PNG framing without decompressing untrusted image data."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("Clipboard images must be at most 10 MiB each.")
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("Paste an image from the clipboard as PNG.")
    offset, width, height, has_pixels = 8, 0, 0, False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            break
        payload = data[offset + 8:end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            break
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                break
            width, height = struct.unpack_from(">II", payload)
            if not width or not height or max(width, height) > 32768 or width * height > 50_000_000:
                raise ValueError("Clipboard image dimensions exceed the 50 megapixel limit.")
        elif kind == b"IHDR":
            break
        has_pixels |= kind == b"IDAT" and bool(length)
        if kind == b"IEND":
            if not length and end == len(data) and width and height and has_pixels:
                return width, height
            break
        offset = end
    raise ValueError("The clipboard image is not a complete PNG. Copy it again and retry.")


@dataclass(frozen=True)
class ImageAttachment:
    id: str
    filename: str
    media_type: str
    width: int
    height: int
    size: int

    def metadata(self) -> dict:
        return asdict(self)


class ImageStore:
    def __init__(self, state_dir: Path):
        self.directory = Path(state_dir) / "attachments"

    def _path(self, image_id: str) -> Path:
        if not isinstance(image_id, str) or re.fullmatch(r"[a-f0-9]{64}", image_id) is None:
            raise ValueError("Choose an image attached to this conversation.")
        if self.directory.is_symlink():
            raise ValueError("The private attachments directory must not be a symbolic link.")
        path = self.directory / (image_id + ".png")
        if path.is_symlink():
            raise ValueError("Image attachments must not be symbolic links.")
        return path

    def upload(self, data_base64: str, filename: str = "Clipboard image.png",
               media_type: str = "image/png") -> dict:
        if media_type != "image/png":
            raise ValueError("Clipboard images must be converted to PNG before attaching.")
        if not isinstance(data_base64, str) or len(data_base64) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("Clipboard images must be at most 10 MiB each.")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid clipboard image data.") from None
        width, height = png_dimensions(data)
        image_id = hashlib.sha256(data).hexdigest()
        target = self._path(image_id)
        private_directory(self.directory)
        fd, temporary = tempfile.mkstemp(prefix=".image-", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        # Clipboard labels are app-owned; arbitrary user paths never enter history.
        return ImageAttachment(image_id, "Clipboard image.png", "image/png",
                               width, height, len(data)).metadata()

    def _read(self, image_id: str) -> tuple[ImageAttachment, bytes]:
        path = self._path(image_id)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except FileNotFoundError:
            raise ValueError("An attached image is missing. Paste it again before sending.") from None
        width, height = png_dimensions(data)
        if hashlib.sha256(data).hexdigest() != image_id:
            raise ValueError("An attached image changed. Paste it again before sending.")
        return (ImageAttachment(image_id, "Clipboard image.png", "image/png",
                                width, height, len(data)), data)

    def load(self, image_id: str) -> ImageAttachment:
        return self._read(image_id)[0]

    def content_part(self, image_id: str) -> dict:
        image, data = self._read(image_id)
        return {"type": "image_url", "image_url": {
            "url": f"data:{image.media_type};base64," + base64.b64encode(data).decode("ascii"),
        }}

    def resolve(self, image_ids: list[str]) -> list[ImageAttachment]:
        if not isinstance(image_ids, list) or len(image_ids) > MAX_IMAGES:
            raise ValueError("Attach at most four clipboard images per message.")
        if any(not isinstance(image_id, str) for image_id in image_ids):
            raise ValueError("Choose valid clipboard image attachments.")
        return [self.load(image_id) for image_id in dict.fromkeys(image_ids)]

    def expand_messages(self, messages: list[dict], required_ids: set[str] | None = None) -> list[dict]:
        """Load selected pixels with a wire budget, reserving space for current attachments."""
        result = copy.deepcopy(messages)
        current = max((index for index, message in enumerate(result)
                       if message["role"] == "user"), default=-1)
        required, historical = [], []
        for message_index, message in enumerate(result):
            if not isinstance(message.get("content"), list):
                continue
            for index, part in enumerate(message["content"]):
                url = part.get("image_url", {}).get("url", "")
                if part.get("type") == "image_url" and url.startswith("eidolon-image:"):
                    image_id = url.removeprefix("eidolon-image:")
                    target = required if message_index == current and image_id in (required_ids or ()) \
                        else historical
                    target.append((message, index, image_id))
        remaining = MAX_REQUEST_IMAGE_BYTES
        for message, index, image_id in [*required, *reversed(historical)]:
            is_required = message is result[current] and image_id in (required_ids or ())
            note = "[Previously attached image is unavailable.]"
            try:
                if not is_required and self._path(image_id).stat().st_size > remaining:
                    note = "[Earlier image pixels omitted to fit the image payload budget.]"
                    raise ValueError(note)
                image, data = self._read(image_id)
                if len(data) > remaining:
                    note = "[Earlier image pixels omitted to fit the image payload budget.]"
                    raise ValueError(note)
                remaining -= len(data)
                message["content"][index] = {"type": "image_url", "image_url": {
                    "url": f"data:{image.media_type};base64," + base64.b64encode(data).decode("ascii"),
                }}
            except (ValueError, OSError):
                if is_required:
                    raise ValueError("An attached image is unavailable or changed. Paste it again before sending.") from None
                message["content"][index] = {"type": "text", "text": note}
        return result


def attachment_notice(images: list[dict], *, delegated: bool) -> str:
    if not images:
        return ""
    lines = [f"[Attached image: {image['filename']}; image_id={image['id']}; "
             f"{image['width']}x{image['height']}]" for image in images]
    if delegated:
        lines.append("Image pixels are available through vision_analyze. Compose your own question "
                     "to ask the configured vision model for the details you need. "
                     "You may call it again with follow-up questions. Do not claim to have "
                     "seen an image until you have received its analysis.")
    return "\n".join(lines)
