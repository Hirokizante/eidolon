"""Versioned, application-owned maintenance diagnostics; never render exception text."""

from __future__ import annotations

import re

from .continuity import ContinuityValidationError
from .proposals import CONTRACT_VERSIONS
from eidolon.providers.maintenance import CAPABILITY_SOURCES, OUTPUT_MODES


REASON_VERSION = 1
REASONS = {
    "unknown": "The refresh could not finish. Its cause is unavailable.",
    "interrupted": "The auxiliary request was interrupted; its evidence is kept for retry.",
    "stale": "The auxiliary route or current life changed; the result was not applied.",
    "timeout": "The auxiliary provider timed out; retry is pending.",
    "thinking_timeout": "The auxiliary model timed out while thinking before producing an answer.",
    "configuration": "The auxiliary request was rejected. Check the model ID, output-token field "
                     "and reasoning effort in /preferences.",
    "authentication": "Auxiliary credentials are unavailable or were rejected. Check /preferences "
                      "and the configured credential reference.",
    "rate_or_credit_limit": "The auxiliary provider's rate or credit limit was reached.",
    "unavailable": "The auxiliary provider is unavailable. Check its connection and model ID.",
    **{"validation." + code: text for code, text in ContinuityValidationError.REASONS.items()},
    "validation.unsupported": "The proposed update did not pass validation.",
}
CONFIGURATION_REASONS = {"configuration", "authentication", "validation.input_limit"}


def safe_reason(code: str | None, version: int | None = REASON_VERSION) -> dict | None:
    """An absent legacy reason stays absent; unknown versions never replay saved prose."""
    if code is None:
        return None
    code = code if (type(version) is int and version == REASON_VERSION and isinstance(code, str)
                    and code in REASONS) else "unknown"
    return {"version": REASON_VERSION, "code": code, "explanation": REASONS[code]}


def read_reason(value: dict | None) -> dict | None:
    return safe_reason(value.get("code"), value.get("version")) if isinstance(value, dict) else None


def problem_state(reason: dict | None) -> str:
    return "configuration_attention" if (reason or {}).get("code") in CONFIGURATION_REASONS else (
        "recovering")


def model_label(value: str | None) -> str:
    # Model identifiers are configuration data, never provider error descriptions.
    if (not isinstance(value, str) or len(value) > 120 or "://" in value
            or "sk-" in value.casefold() or not re.fullmatch(r"[\w .:/-]+", value)):
        return "Unavailable"
    return value


def safe_usage(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    # Bound counts before numeric conversion; hostile JSON integers can exceed float range.
    keys = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
            "cached_tokens", "cache_write_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "reasoning_tokens", "audio_tokens", "text_tokens",
            "image_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"}
    return {key: amount for key, amount in value.items()
            if isinstance(key, str) and key.count(".") <= 1 and key.split(".")[-1] in keys
            and ("." not in key or key.split(".")[0] in {
                "prompt_tokens_details", "completion_tokens_details",
                "input_tokens_details", "output_tokens_details"})
            and type(amount) in {int, float} and 0 <= amount <= 2**63 - 1} or None


def public_attempt(attempt: dict | None) -> dict | None:
    if attempt is None:
        return None
    provenance = attempt.get("provenance", {})
    output = {key: provenance.get(key) if provenance.get(key) in values else None
              for key, values in {
                  "output_mode": OUTPUT_MODES,
                  "contract": CONTRACT_VERSIONS,
                  "capability_source": CAPABILITY_SOURCES,
              }.items()}
    return {**{key: attempt.get(key) for key in (
        "id", "task_id", "stage", "started_at", "ended_at", "outcome", "elapsed_seconds",
    )}, "reason": read_reason(attempt.get("reason")),
        "model": model_label(attempt.get("provenance", {}).get("model")),
        "usage": safe_usage(attempt.get("usage")), **output}
