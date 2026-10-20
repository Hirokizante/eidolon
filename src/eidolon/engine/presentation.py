"""Incremental presentation decoding, separate from provider and body transports.

Control fields are only interpreted at the start of an explicitly selected envelope.
Reply escapes are decoded as they arrive, including split UTF-16 surrogate pairs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime


PREFIX_OPEN = "<eidolon-cues>"
PREFIX_CLOSE = "</eidolon-cues>"


@dataclass
class PresentationUpdate:
    text: str = ""
    cues: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class PresentationParser:
    def __init__(self, mode: str = "plain", *, expressions=(), gestures=(),
                 header_limit: int = 4096, raw_limit: int = 1_000_000):
        if mode not in ("plain", "prefix", "json"):
            raise ValueError("Unknown presentation mode")
        self.mode = mode
        self.catalog = {"expression": set(expressions), "gesture": set(gestures)}
        self.header_limit = header_limit
        self.raw_limit = raw_limit
        self.raw = ""
        self.clean = ""
        self.cues: list[dict] = []
        self.errors: list[str] = []
        self.plain_fallback = False
        self._state = "start"
        self._buffer = ""
        self._seen: set[str] = set()
        self._key = ""
        self._string = ""
        self._string_target = ""
        self._escape = False
        self._unicode = None
        self._high_surrogate = None
        self._header_chars = 0
        self._reply_started = False
        self._prefix_done = False
        self._timestamp_done = False
        self._strip_newline = False
        self._separator_cr = False
        self._failure = False

    def _error(self, message: str, update: PresentationUpdate, *, fatal=True):
        self.errors.append(message)
        update.errors.append(message)
        if fatal:
            self._failure = True
            self._state = "error"

    def _text(self, value: str, update: PresentationUpdate):
        self.clean += value
        update.text += value

    def _cue(self, key: str, value, update: PresentationUpdate):
        if value is None:
            return
        if not isinstance(value, str) or len(value) > 96:
            self._error("Invalid presentation cue value.", update)
            return
        if value not in self.catalog[key]:
            self._error(f"Unknown {key} cue: {value[:96]}", update, fatal=False)
            return
        cue = {"kind": key, "name": value}
        self.cues.append(cue)
        update.cues.append(cue)

    def feed(self, chunk: str) -> PresentationUpdate:
        update = PresentationUpdate()
        if len(self.raw) + len(chunk) > self.raw_limit:
            self._error("Presentation exceeded the response size limit.", update)
            return update
        self.raw += chunk
        if self.mode == "plain":
            self._text(chunk, update)
        elif self.mode == "prefix":
            self._feed_prefix(chunk, update)
        else:
            self._feed_json(chunk, update)
        return update

    def _feed_prefix(self, chunk: str, update: PresentationUpdate):
        if self._failure or not chunk:
            return
        if self._prefix_done:
            if self._strip_newline:
                # The one format separator is excluded; intentional prose whitespace stays.
                if self._separator_cr:
                    self._separator_cr = False
                    if chunk.startswith("\n"):
                        chunk = chunk[1:]
                    else:
                        chunk = "\r" + chunk
                elif chunk == "\r":
                    self._separator_cr = True
                    return
                elif chunk.startswith("\r\n"):
                    chunk = chunk[2:]
                elif chunk.startswith("\n"):
                    chunk = chunk[1:]
                self._strip_newline = False
            self._text(chunk, update)
            return
        self._buffer += chunk
        candidate = self._buffer.lstrip()
        leading_whitespace = len(self._buffer) - len(candidate)
        if leading_whitespace > self.header_limit:
            self._error("Presentation cue header exceeds its limit.", update)
            return
        if not candidate:
            return
        if candidate.startswith("[") and not self._timestamp_done:
            # A model may echo the app's input timestamp before text or tool calls.
            # Accept only the exact app format, once, before parsing any controls.
            if "\n" not in candidate:
                if len(self._buffer) > min(self.header_limit, 64):
                    self._error("Missing presentation cue prefix.", update)
                return
            line, _, remainder = candidate.partition("\n")
            if leading_whitespace + len(line) + 1 > min(self.header_limit, 64):
                self._error("Presentation cue header exceeds its limit.", update)
                return
            line = line.removesuffix("\r")
            try:
                stamp = datetime.fromisoformat(line[1:-1])
                if stamp.tzinfo is None or line != f"[{stamp.isoformat(timespec='seconds')}]":
                    raise ValueError
            except ValueError:
                self._error("Missing presentation cue prefix.", update)
                return
            self._timestamp_done = True
            self._buffer = ""
            self._error("Echoed chat timestamp omitted from reply.", update, fatal=False)
            self._feed_prefix(remainder, update)
            return
        if not (PREFIX_OPEN.startswith(candidate) or candidate.startswith(PREFIX_OPEN)):
            # Ordinary prose is still a usable reply when optional cues are omitted.
            # Ambiguous markup/JSON stays fail-closed so control syntax cannot leak
            # into chat or become an avatar action during format recovery.
            if candidate[0] in "<{[`":
                self._error("Missing presentation cue prefix.", update)
                return
            self.plain_fallback = True
            self._prefix_done = True
            self._error("Presentation cue prefix omitted; displaying reply without avatar cues.",
                        update, fatal=False)
            self._text(self._buffer, update)
            self._buffer = ""
            return
        if len(candidate) < len(PREFIX_OPEN):
            if len(self._buffer) > self.header_limit:
                self._error("Presentation cue header exceeds its limit.", update)
            return
        controls_start = leading_whitespace + len(PREFIX_OPEN)
        close = self._buffer.find(PREFIX_CLOSE, controls_start)
        if close < 0:
            if len(self._buffer) > self.header_limit:
                self._error("Presentation cue header exceeds its limit.", update)
            return
        if close + len(PREFIX_CLOSE) > self.header_limit:
            self._error("Presentation cue header exceeds its limit.", update)
            return
        pairs = []
        try:
            controls = json.loads(self._buffer[controls_start:close],
                                  object_pairs_hook=lambda values: pairs.extend(values) or dict(values))
            keys = [key for key, _ in pairs]
            if (not isinstance(controls, dict) or len(keys) != len(set(keys))
                    or set(keys) != {"expression", "gesture"}
                    or any(value is not None and (not isinstance(value, str) or len(value) > 96)
                           for value in controls.values())):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            self._error("Malformed presentation cue header.", update)
            return
        for key, value in controls.items():
            self._cue(key, value, update)
        remainder = self._buffer[close + len(PREFIX_CLOSE):]
        self._buffer = ""
        self._prefix_done = True
        self._strip_newline = True
        if remainder:
            self._feed_prefix(remainder, update)

    def _decoded(self, value: str, update: PresentationUpdate):
        if self._string_target == "reply":
            self._text(value, update)
        else:
            self._string += value
            if len(self._string) > (96 if self._string_target == "cue" else 32):
                self._error("Presentation control string exceeds its limit.", update)

    def _codepoint(self, value: int, update: PresentationUpdate):
        if self._high_surrogate is not None:
            high = self._high_surrogate
            self._high_surrogate = None
            if not 0xDC00 <= value <= 0xDFFF:
                self._error("Unpaired Unicode surrogate in presentation.", update)
                return
            self._decoded(chr(0x10000 + ((high - 0xD800) << 10) + value - 0xDC00), update)
        elif 0xD800 <= value <= 0xDBFF:
            self._high_surrogate = value
        elif 0xDC00 <= value <= 0xDFFF:
            self._error("Unpaired Unicode surrogate in presentation.", update)
        else:
            self._decoded(chr(value), update)

    def _finish_string(self, update: PresentationUpdate):
        if self._high_surrogate is not None:
            self._error("Unpaired Unicode surrogate in presentation.", update)
            return
        if self._string_target == "key":
            self._key = self._string
            if self._key not in {"expression", "gesture", "reply"} or self._key in self._seen:
                self._error("Unknown or duplicate presentation field.", update)
                return
            self._seen.add(self._key)
            self._state = "colon"
        else:
            if self._string_target == "cue":
                self._cue(self._key, self._string, update)
            if not self._failure:
                self._state = "comma"
        self._string = ""

    def _feed_json(self, chunk: str, update: PresentationUpdate):
        for char in chunk:
            if self._failure:
                break
            if not self._reply_started:
                self._header_chars += 1
                if self._header_chars > self.header_limit:
                    self._error("Presentation cue header exceeds its limit.", update)
                    break
            state = self._state
            if state == "string":
                if self._unicode is not None:
                    if char not in "0123456789abcdefABCDEF":
                        self._error("Invalid Unicode escape in presentation.", update)
                        continue
                    self._unicode += char
                    if len(self._unicode) == 4:
                        self._codepoint(int(self._unicode, 16), update)
                        self._unicode = None
                elif self._escape:
                    self._escape = False
                    if char == "u":
                        self._unicode = ""
                    else:
                        escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                                   "n": "\n", "r": "\r", "t": "\t"}
                        if char not in escapes or self._high_surrogate is not None:
                            self._error("Invalid escape in presentation.", update)
                        else:
                            self._decoded(escapes[char], update)
                elif char == "\\":
                    self._escape = True
                elif char == '"':
                    self._finish_string(update)
                elif (ord(char) < 0x20 or 0xD800 <= ord(char) <= 0xDFFF
                      or self._high_surrogate is not None):
                    self._error("Invalid character in presentation string.", update)
                else:
                    self._decoded(char, update)
                continue
            if state == "literal":
                self._buffer += char
                if not "null".startswith(self._buffer):
                    self._error("Invalid presentation cue literal.", update)
                elif self._buffer == "null":
                    self._buffer = ""
                    self._state = "comma"
                continue
            if char.isspace():
                continue
            if state == "start" and char == "{":
                self._state = "key"
            elif state == "key" and char == '"':
                self._string_target = "key"
                self._state = "string"
                self._string = ""
            elif state == "colon" and char == ":":
                self._state = "value"
            elif state == "value" and char == '"':
                self._string_target = "reply" if self._key == "reply" else "cue"
                self._reply_started |= self._key == "reply"
                self._string = ""
                self._state = "string"
            elif state == "value" and char == "n" and self._key != "reply":
                self._state = "literal"
                self._buffer = "n"
            elif state == "comma" and char == ",":
                self._state = "key"
            elif state == "comma" and char == "}":
                if self._seen != {"expression", "gesture", "reply"}:
                    self._error("Presentation envelope is missing fields.", update)
                else:
                    self._state = "done"
            else:
                self._error("Malformed presentation envelope.", update)

    def finish(self) -> PresentationUpdate:
        update = PresentationUpdate()
        timestamp_only = self._timestamp_done and not self._buffer.strip()
        if not self._failure and ((self.mode == "json" and self._state != "done")
                                  or (self.mode == "prefix" and not self._prefix_done
                                      and not timestamp_only)):
            self._error("Incomplete presentation envelope.", update)
        return update

    @property
    def valid(self) -> bool:
        return not self._failure
