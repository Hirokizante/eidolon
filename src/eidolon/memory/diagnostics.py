"""Versioned, application-owned maintenance diagnostics; never render exception text."""

from __future__ import annotations

import re

from .continuity import ContinuityValidationError
from .proposals import CONTRACT_VERSIONS
from eidolon.providers.maintenance import CAPABILITY_SOURCES, OUTPUT_MODES


REASON_VERSION = 2
LEGACY_REASON_CODES = frozenset({
    "authentication", "configuration", "interrupted", "rate_or_credit_limit", "stale",
    "thinking_timeout", "timeout", "unavailable", "unknown", "validation.archive_reason",
    "validation.batch_source", "validation.capacity", "validation.capacity_task",
    "validation.chronology", "validation.content", "validation.date",
    "validation.evidence_changed", "validation.input_limit", "validation.input_overhead",
    "validation.input_state", "validation.legacy_task", "validation.narrative",
    "validation.output_budget", "validation.output_incomplete", "validation.output_json",
    "validation.output_limit", "validation.output_plan", "validation.output_tokens",
    "validation.promise_commitment", "validation.promise_qualified",
    "validation.promise_scheduled", "validation.reference", "validation.schema",
    "validation.schema_op", "validation.source", "validation.terminal", "validation.topic",
    "validation.unsupported",
})
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
CONFIGURATION_REASONS = {"configuration", "authentication", "validation.input_limit",
                         "validation.input_overhead", "validation.input_state",
                         "validation.output_budget"}


DISPOSITIONS = {"correct", "resnapshot", "resize", "repair", "defer", "pause", "transport_retry"}
RECOVERY_OUTCOMES = {"unchanged", "repair", "deferred", "pending", "resolved"}
DIAGNOSTIC_FIELDS = {"op", "sources", "note_ref", "topic", "importance", "content", "category",
                     "origin", "scope", "event_at", "follow_up_at", "expires_at", "promise_source",
                     "promise_owner", "promise_kind", "expected_revision", "archive_reason",
                     "decision_reason", "replaces_ref", "content_format"}
RECOVERY_LABELS = {
    "correct": "Correct the proposal using the supplied evidence.",
    "resnapshot": "Take a fresh snapshot before requesting another proposal.",
    "resize": "Use a smaller complete-evidence group or adjust response capacity.",
    "repair": "Saved evidence needs repair before this update can proceed.",
    "defer": "Wait until relevant evidence or target state changes, or request an explicit retry.",
    "pause": "Check the auxiliary settings before retrying.",
    "transport_retry": "Retry the provider request after its backoff delay.",
}


def recovery_disposition(code: str) -> str:
    if code in {"stale", "validation.revision"}:
        return "resnapshot"
    if code == "validation.saved_support":
        return "repair"
    if code == "validation.evidence_changed":
        return "defer"
    if code in {"validation.input_limit", "validation.output_budget", "validation.output_tokens",
                "validation.output_limit", "validation.output_plan"}:
        return "resize"
    if code in CONFIGURATION_REASONS:
        return "pause"
    return "correct" if code.startswith("validation.") else "transport_retry"


def recovery_outcome(value: dict | None) -> str | None:
    outcome = value.get("outcome") if isinstance(value, dict) else None
    return outcome if isinstance(outcome, str) and outcome in RECOVERY_OUTCOMES else None


def safe_context(value: dict) -> dict:
    result = {}
    index = value.get("operation_index")
    if type(index) is int and 1 <= index <= 32:
        result["operation_index"] = index
    if isinstance(value.get("field"), str) and value["field"] in DIAGNOSTIC_FIELDS:
        result["field"] = value["field"]
    target = value.get("target_ref")
    if isinstance(target, str) and re.fullmatch(r"n[1-9][0-9]{0,2}", target):
        result["target_ref"] = target
    sources = value.get("source_refs")
    if isinstance(sources, list):
        result["source_refs"] = list(dict.fromkeys(ref for ref in sources[:8]
            if isinstance(ref, str) and re.fullmatch(r"(?:s|background)[1-9][0-9]{0,3}", ref)
            and int(re.search(r"[0-9]+$", ref)[0]) <= 1024))
    attempt = value.get("attempt_id")
    if isinstance(attempt, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", attempt):
        result["attempt_id"] = attempt
    return result


def safe_reason(code: str | None, version: int | None = REASON_VERSION, *,
                context: dict | None = None, disposition: str | None = None) -> dict | None:
    """Reconstruct only allowlisted rules and bounded application-owned context."""
    if code is None:
        return None
    if type(version) is not int or version not in {1, REASON_VERSION}:
        return {"version": 1, "code": "unknown", "explanation": REASONS["unknown"]}
    code = code if isinstance(code, str) and code in REASONS else "unknown"
    if version == 1:
        if code not in LEGACY_REASON_CODES or code == "validation.unsupported":
            code = "unknown"
        return {"version": 1, "code": code, "explanation": REASONS[code]}
    return {"version": REASON_VERSION, "code": code, "explanation": REASONS[code],
            "disposition": disposition if isinstance(disposition, str) and disposition in DISPOSITIONS
                           else recovery_disposition(code), **safe_context(context or {})}


def read_reason(value: dict | None) -> dict | None:
    return (safe_reason(value.get("code"), value.get("version"), context=value,
                        disposition=value.get("disposition")) if isinstance(value, dict) else None)


def reason_details(value: dict | None) -> str:
    """Shared safe Run details text, including the scope of local references."""
    reason = read_reason(value)
    if reason is None:
        return "No safe cause was recorded."
    parts = [reason["explanation"]]
    if reason.get("disposition"):
        parts.append("Recovery: " + RECOVERY_LABELS[reason["disposition"]])
    context = []
    if "operation_index" in reason:
        context.append("Operation " + str(reason["operation_index"]))
    if "target_ref" in reason:
        context.append("target " + reason["target_ref"])
    if reason.get("source_refs"):
        context.append("sources " + ", ".join(reason["source_refs"]))
    if "field" in reason:
        context.append("field " + reason["field"])
    if context:
        parts.append("; ".join(context) + ".")
        if reason.get("attempt_id"):
            parts.append("References belong to attempt " + reason["attempt_id"] + ".")
    return "\n".join(parts)


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
        "recovery_source", "recovery_pass", "daily_period",
    )}, "reason": read_reason(attempt.get("reason")),
        "model": model_label(attempt.get("provenance", {}).get("model")),
        "usage": safe_usage(attempt.get("usage")), **output}
