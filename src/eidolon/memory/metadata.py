"""Application-owned outcomes for optional current-life metadata selections."""

METADATA_KEYS = {
    "event_at": "event_at",
    "follow_up_at": "follow_up_at",
    "promise_source": "promise_owner",
}
METADATA_REASONS = {
    "unsupported_date": "no supported date literal in the cited originals",
    "ambiguous_date": "the literal resolves to different dates",
    "unsupported_promise": "no attributable commitment of a single kind in the selected source",
    "correction_required": "removal needs an evidenced correction",
    "background": "historical context preserves current metadata",
    "awaiting_evidence": "awaiting supporting evidence",
}


def field_outcome(index: int, field: str, reason: str) -> dict:
    return {"operation": index, "field": field, "reason": reason,
            "outcome": "deferred" if reason == "awaiting_evidence" else "omitted"}
