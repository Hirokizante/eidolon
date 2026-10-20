"""One model token window for prompt assembly and the context meter.

Chat framing and compatible-provider tokenizers differ, so preflight counts are
estimates. Provider-reported usage replaces them after dispatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import tiktoken


@lru_cache(maxsize=16)
def _encoding(model: str) -> tiktoken.Encoding | None:
    try:
        try:
            name = tiktoken.encoding_name_for_model(model)
        except KeyError:
            name = "o200k_base"
        return tiktoken.get_encoding(name)
    except Exception:
        # The vocabulary may be unavailable on first use while offline. UTF-8
        # bytes give a conservative fallback without preventing conversation.
        return None


class TokenCounter:
    def __init__(self, model: str) -> None:
        self.encoding = _encoding(model)

    def text(self, value: str) -> int:
        if self.encoding is None:
            return len(value.encode("utf-8"))
        return len(self.encoding.encode_ordinary(value))

    def value(self, value: Any) -> int:
        return self.text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def message(self, message: dict) -> int:
        content = message.get("content")
        if not isinstance(content, list):
            return self.value(message) + 8
        # An encoded image is transport data, not text tokens. Model-specific
        # vision encoders vary; use a conservative image allowance and let
        # provider-reported prompt usage replace this estimate after dispatch.
        parts, image_tokens = [], 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image_tokens += 4096
                parts.append({"type": "image_url"})
            else:
                parts.append(part)
        return self.value({**message, "content": parts}) + image_tokens + 8

    def request(self, messages: list[dict], tools: list[dict] | None = None,
                response_format: dict | None = None) -> int:
        return (8 + sum(self.message(message) for message in messages)
                + (self.value(tools) + 16 if tools else 0)
                + (self.value(response_format) + 16 if response_format else 0))


@dataclass(frozen=True)
class ContextBudget:
    window: int | None
    output_tokens: int
    safety_tokens: int

    @classmethod
    def for_profile(cls, profile: Any) -> ContextBudget:
        resolver = getattr(profile, "context_window_for", None)
        window = resolver() if resolver else getattr(profile, "context_window", None)
        parameters = getattr(profile, "parameters", {})
        limits = [parameters.get(key) for key in (
            "max_tokens", "max_completion_tokens", "max_output_tokens",
        )]
        output = max((value for value in limits if type(value) is int and value > 0),
                     default=min(4096, max(1, window // 4)) if window else 4096)
        return cls(window, output, max(64, (window + 49) // 50) if window else 0)

    @property
    def reserved_tokens(self) -> int:
        return self.output_tokens + self.safety_tokens

    @property
    def input_tokens(self) -> int | None:
        return max(0, self.window - self.reserved_tokens) if self.window else None

    def fits(self, input_tokens: int) -> bool:
        return self.window is None or input_tokens + self.reserved_tokens <= self.window

    def metrics(self, input_tokens: int) -> dict:
        return {"context_tokens": input_tokens + self.reserved_tokens,
                "context_input_tokens": input_tokens,
                "context_reserved_tokens": self.reserved_tokens,
                "context_window": self.window, "context_estimated": True}

    def error(self, input_tokens: int) -> str:
        return (f"This request needs approximately {input_tokens:,} input tokens plus "
                f"{self.reserved_tokens:,} reserved for the response and token estimation, "
                f"exceeding the model's {self.window:,}-token context window. "
                "Shorten the request or tool output, reduce the configured output limit, "
                "or select a model with a larger context window. If the capacity is incorrect, "
                "update Context window in /connect under Advanced.")
