"""Narrative working context with original evidence and deterministic metadata.

Source identity, eligibility, dates and state transitions are checked locally.
Narrative faithfulness is a model responsibility; citations are provenance, not
a deterministic proof of semantic support. Legacy extractive records remain readable.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone as dt_timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError


Scope = Literal["real_life", "fiction", "unknown"]
Category = Literal["situation", "thread", "relationship", "emotion"]
Importance = Literal["low", "moderate", "high"]
Origin = Literal["user_statement", "assistant_statement", "derived"]
NARRATIVE_MAX_CHARS = 1200
TOPIC_MAX_CHARS = 100
DEFAULT_MAX_NOTES = 12
MAX_NOTES = 64
IMPORTANCE_RANK = {"low": 0, "moderate": 1, "high": 2}
POLICY_ARCHIVE_REASONS = {"replaced", "capacity"}
THINKING_TIMEOUT_NOTICE = (
    "Auxiliary model timed out while thinking; disable thinking or increase its timeout "
    "in /preferences. Retry pending."
)


class ContinuityValidationError(ValueError):
    """An unsupported proposal; none of the batch should be committed."""

    REASONS = {
        "reference": "Use only the note_ref and source_refs supplied in this request.",
        "schema": "The model returned malformed continuity operations.",
        "schema_op": "Each operation must use the JSON key op with a value of upsert, resolve, "
                     "archive or no_change. Do not name this key operation, action or type.",
        "source": "Evidence must copy a complete original message with its correct source identity.",
        "batch_source": "Updates must cite an exchange from this batch. Prior sources alone "
                        "may only convert an open legacy note to narrative while preserving "
                        "all its sources, category, scope and optional metadata.",
        "content": "Note content must exactly match its complete cited messages.",
        "narrative": "Write a compact third-person narrative summary, not copied exchanges, "
                     "with content_format narrative and origin derived.",
        "promise_commitment": "Promise metadata requires a message beginning with an explicit "
                              "first-person commitment. Omit promise metadata otherwise.",
        "promise_qualified": "Qualified or negated statements cannot carry promise metadata.",
        "promise_scheduled": "Scheduled promises require explicit existing-action evidence.",
        "date": "Event and follow-up dates need unambiguous, unqualified source evidence.",
        "terminal": "Resolve/archive requires a positive current/past statement naming the "
                    "same topic. Questions, qualifiers, future intentions, and vague references "
                    "cannot close a note. Leave it unchanged when evidence does not qualify.",
        "topic": "Every upsert needs a short, single-subject topic and importance low, moderate "
                 "or high. Keep an existing note's topic unchanged; put unrelated developments "
                 "in a separate note. Do not duplicate an active topic.",
        "capacity": "Current life is full. Archive concluded topics first. Each additional "
                    "admission must name replaces_item_id and decision_reason, paired with "
                    "an archive using archive_reason replaced. The replaced note must have "
                    "strictly lower importance. Never exceed max_notes or evict protected notes.",
        "archive_reason": "Archiving requires an archive_reason and a concise decision_reason. "
                          "Use completed, broken_promise, no_longer_current, conversation_ended, "
                          "replaced or capacity. Capacity cleanup is only for an existing excess.",
        "output_json": "The model did not return a valid JSON object.",
        "output_incomplete": "The maintenance output did not finish completely.",
        "output_tokens": "The maintenance response reached its output-token limit. "
                         "Increase the auxiliary output limit or lower reasoning effort.",
        "output_limit": "The maintenance output exceeded its size allowance.",
        "input_limit": "The oldest complete source batch exceeds the input allowance.",
    }
    DETAILS = frozenset({
        "Invalid source timestamp",
        "A timestamp must include its timezone",
        "Unsupported event date",
        "A changed item requires original evidence",
        "Source origin disagrees with the original speaker",
        "Changed content must preserve exact same-speaker evidence",
        "Claim origin disagrees with its evidence",
        "A terminal operation requires an existing item",
        "A terminal operation needs positive, topic-matched evidence",
        "An existing item needs evidence about the same topic",
        "A promise must identify both author and kind",
        "A promise requires the original author's statement",
        "no_change must be the sole, empty operation",
        "An item cannot be changed twice in one batch",
        "An operation names an unknown item",
        "A locked or excluded item cannot be changed automatically",
        "A user-created note cannot be changed automatically",
        "Item revision changed before proposal validation",
        "Separate notes are required for different speakers",
        "An operation cannot redirect a task to another scope",
        "Fictional evidence cannot become real-life state",
        "A new or updated note requires category, origin, and scope",
        "An item's expiry must be after its observation",
        "Automatic expiry exceeds the transient policy",
        "The maintenance output is not text",
        "Maintenance cannot call tools",
    })

    def __init__(self, message: str, *, code: str = "unsupported") -> None:
        super().__init__(message)
        # Never persist arbitrary exception text, source text, or model output.
        self.code = code if code in self.REASONS else "unsupported"
        self.public_reason = self.REASONS.get(code, message if message in self.DETAILS else
                                             "The proposed update did not pass validation.")


class ContinuitySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    exchange_id: int = Field(gt=0)
    document_id: str = Field(min_length=1, max_length=200)
    role: Literal["user", "assistant"]
    # Narrative proposals may cite identities only; the application supplies the
    # complete original text. Never ask the model to echo transcripts in its output.
    excerpt: str | None = Field(default=None, min_length=1)
    origin: Literal["user_statement", "assistant_statement"]


class ContinuityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    op: Literal["upsert", "resolve", "archive", "no_change"]
    expected_revision: int = Field(ge=0)
    item_id: str | None = Field(default=None, min_length=1, max_length=200)
    sources: list[ContinuitySource] = Field(default_factory=list, max_length=8)
    category: Category | None = None
    topic: str | None = Field(default=None, min_length=1, max_length=TOPIC_MAX_CHARS)
    importance: Importance | None = None
    replaces_item_id: str | None = Field(default=None, min_length=1, max_length=200)
    archive_reason: Literal["completed", "broken_promise", "no_longer_current",
                            "conversation_ended", "replaced", "capacity"] | None = None
    decision_reason: str | None = Field(default=None, min_length=1, max_length=600)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    content_format: Literal["narrative"] | None = None
    origin: Origin | None = None
    scope: Scope | None = None
    observed_at: str | None = None
    event_at: str | None = None
    expires_at: str | None = None
    follow_up_at: str | None = None
    last_mention_at: str | None = None
    promise_owner: Literal["user", "assistant"] | None = None
    promise_kind: Literal["conversation", "scheduled"] | None = None


class ContinuityProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operations: list[ContinuityOperation] = Field(min_length=1, max_length=24)


def topic_key(topic: str) -> str:
    return " ".join(topic.split()).casefold()


def valid_topic(topic: object) -> bool:
    return (isinstance(topic, str) and bool(topic.strip()) and len(topic) <= TOPIC_MAX_CHARS
            and not any(ord(char) < 32 or ord(char) == 127 for char in topic))


def needs_note_metadata(item: dict) -> bool:
    return (bool(item.get("metadata_pending")) or not valid_topic(item.get("topic"))
            or not isinstance(item.get("importance"), str)
            or item["importance"] not in IMPORTANCE_RANK)


def with_note_metadata(item: dict) -> dict:
    """Keep old notes readable until maintenance can deliberately classify them.

    A provisional title and neutral importance never invent a model judgment.
    Existing originals and history remain intact; unlocked learned notes receive
    a real topic and importance at their next bounded maintenance pass.
    """
    if not needs_note_metadata(item):
        return item
    result = {**item, "metadata_pending": True}
    if not valid_topic(result.get("topic")):
        first_line = " ".join(str(item.get("content", "")).split())
        result["topic"] = (re.split(r"[.!?](?:\s|$)", first_line, maxsplit=1)[0]
                           [:TOPIC_MAX_CHARS].strip() or "Current life note")
    if (not isinstance(result.get("importance"), str)
            or result["importance"] not in IMPORTANCE_RANK):
        result["importance"] = "moderate"
    return result


def validate_note_metadata(operation: dict, item: dict | None) -> None:
    if operation.get("topic") is not None and not valid_topic(operation["topic"]):
        raise ContinuityValidationError("Invalid note topic", code="topic")
    if operation.get("importance") is not None and (
            not isinstance(operation["importance"], str)
            or operation["importance"] not in IMPORTANCE_RANK):
        raise ContinuityValidationError("Invalid note importance", code="topic")
    for key, maximum in (("replaces_item_id", 200), ("decision_reason", 600)):
        value = operation.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or len(value) > maximum):
            raise ContinuityValidationError("Invalid allocation decision", code="capacity")
    if operation["op"] == "upsert":
        if (not valid_topic(operation.get("topic"))
                or not isinstance(operation.get("importance"), str)
                or operation["importance"] not in IMPORTANCE_RANK):
            raise ContinuityValidationError("A note requires topic and importance", code="topic")
    if item and not needs_note_metadata(item) and operation.get("topic") is not None:
        if topic_key(operation["topic"]) != topic_key(item["topic"]):
            raise ContinuityValidationError("A note's topic cannot change automatically", code="topic")
    if operation["op"] in {"resolve", "archive"}:
        reason = operation.get("archive_reason")
        if reason is not None and reason not in (
                "completed", "broken_promise", "no_longer_current", "conversation_ended",
                "replaced", "capacity"):
            raise ContinuityValidationError("Invalid archive reason", code="archive_reason")
        # The old extractive operation format remains readable for integrations;
        # model-written narrative changes always make their decision explicit.
        if (operation.get("content_format") == "narrative"
                and (not reason or not (operation.get("decision_reason") or "").strip())):
            raise ContinuityValidationError("An archive requires an explicit decision",
                                            code="archive_reason")
    elif operation.get("archive_reason") is not None:
        raise ContinuityValidationError("Only an archive can have an archive reason",
                                        code="archive_reason")


def validate_note_capacity(operations: list[dict], items: list[dict], *,
                           max_notes: int = DEFAULT_MAX_NOTES, scope: Scope = "real_life") -> None:
    """Validate admissions and explicit replacement pairs without evicting anything.

    An existing excess may shrink over several bounded runs. Protected notes
    still occupy slots; new admissions are blocked until the result fits.
    """
    if type(max_notes) is not int or not 1 <= max_notes <= MAX_NOTES:
        raise ValueError("Maximum active notes must be an integer from 1 to 64")
    if any(not isinstance(op, dict) or op.get("op", op.get("operation"))
           not in ("upsert", "archive", "resolve", "no_change") for op in operations):
        raise ContinuityValidationError("Malformed continuity operations", code="schema")
    operations = [{**op, "op": op.get("op", op.get("operation"))} for op in operations]
    existing = {item["id"]: item for item in items}
    for op in operations:
        if op["op"] == "upsert":
            validate_note_metadata(op, existing.get(op.get("item_id")))
    current = {item["id"]: item for item in active_items(items, scope=scope)}
    archives = {op.get("item_id"): op for op in operations
                if op["op"] in {"resolve", "archive"}}
    final = {key: item for key, item in current.items() if key not in archives}
    admissions = []
    for index, op in enumerate(operations):
        if op["op"] != "upsert":
            if op.get("replaces_item_id"):
                raise ContinuityValidationError("Only an admission can replace a note", code="capacity")
            continue
        key = op.get("item_id") or f"new:{index}"
        candidate = {**current.get(key, {}), **op, "id": key, "state": "open", "scope": scope}
        if not active_items([candidate], scope=scope):
            final.pop(key, None)
            continue
        final[key] = candidate
        if key not in current:
            admissions.append(op)
        elif op.get("replaces_item_id"):
            raise ContinuityValidationError("Only an admission can replace a note", code="capacity")
    replaced = set()
    for op in admissions:
        victim_id = op.get("replaces_item_id")
        if not victim_id:
            continue
        victim = current.get(victim_id)
        archive = archives.get(victim_id, {})
        if (victim is None or victim_id in replaced
                or archive.get("archive_reason") != "replaced"
                or not (op.get("decision_reason") or "").strip()
                or IMPORTANCE_RANK.get(op.get("importance"), -1)
                <= IMPORTANCE_RANK.get(victim.get("importance"), 1)):
            raise ContinuityValidationError("Replacement needs a lower-importance archive pair",
                                            code="capacity")
        replaced.add(victim_id)
    capacity_archives = 0
    for item_id, op in archives.items():
        if op.get("archive_reason") == "replaced" and item_id not in replaced:
            raise ContinuityValidationError("A replacement archive needs its new note", code="capacity")
        if op.get("archive_reason") == "capacity":
            capacity_archives += 1
            if item_id not in current:
                raise ContinuityValidationError("Capacity cleanup requires an active note", code="capacity")
    if capacity_archives > max(0, len(current) - max_notes):
        raise ContinuityValidationError("Capacity cleanup only removes an existing excess", code="capacity")
    if admissions and len(final) > max_notes:
        raise ContinuityValidationError("The proposed admissions exceed max_notes", code="capacity")
    topics: dict[str, str] = {}
    changed = {op.get("item_id") or f"new:{index}" for index, op in enumerate(operations)
               if op["op"] == "upsert"}
    for key, item in final.items():
        topic = topic_key(with_note_metadata(item)["topic"])
        if topic in topics and (key in changed or topics[topic] in changed):
            raise ContinuityValidationError("An active note already covers this topic", code="topic")
        topics[topic] = key


def aware_time(value: datetime | str) -> datetime:
    """Parse only an actual timezone-aware instant, never a room/day key."""
    try:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        raise ContinuityValidationError("Invalid source timestamp") from None
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ContinuityValidationError("A timestamp must include its timezone")
    return instant


def resolve_event_time(value: str, source_timestamp: str | datetime,
                       timezone: str = "UTC") -> str:
    """Resolve relative dates from the message's actual local calendar date.

    In particular, a 3 AM message uses that morning's date, independently of
    which room it belongs to under the 4 AM navigation boundary.
    """
    local = aware_time(source_timestamp).astimezone(ZoneInfo(timezone))
    relative = {"yesterday": -1, "today": 0, "tomorrow": 1}
    if value.casefold() in relative:
        day = local.date() + timedelta(days=relative[value.casefold()])
        return datetime.combine(day, time.min, local.tzinfo).isoformat()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(datetime.fromisoformat(value).date(), time.min,
                                    local.tzinfo).isoformat()
        return aware_time(value).isoformat()
    except (TypeError, ValueError):
        raise ContinuityValidationError("Unsupported event date") from None


_STOP_WORDS = {
    "a", "an", "the", "i", "me", "my", "we", "our", "you", "your", "it", "its",
    "is", "was", "are", "be", "been", "am", "to", "for", "of", "and", "or", "in",
    "on", "with", "have", "has", "had", "this", "that", "will", "tomorrow", "today",
    "yesterday", "done", "finished", "completed", "resolved", "cancelled", "canceled",
    "please", "give", "like", "want", "need", "prefer", "feel", "about", "later",
}


def subject_tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)
            if len(token) > 2 and token not in _STOP_WORDS}


def same_topic(content: str, previous: str) -> bool:
    overlap = subject_tokens(content).intersection(subject_tokens(previous))
    return len(overlap) >= 2 or any(len(token) >= 7 for token in overlap)


def _source_rows(inputs: list[dict]) -> dict[int, dict]:
    return {row.get("exchange_id", row.get("id")): row for row in inputs}


def _source_text(row: dict, role: str) -> str:
    return row.get(role + "_content", row.get(role, ""))


def whole_source_excerpt(excerpt: str, original: str) -> bool:
    """Copy the full message so attribution and distant qualifiers cannot vanish.

    Leading/trailing whitespace is immaterial. Every internal word, quote,
    paragraph, and qualifier must survive in the evidence record.
    """
    return excerpt in {original, original.strip()}


def source_is_fictional(original: str) -> bool:
    return bool(re.search(
        r"\b(?:roleplay|role.play|pretend|in (?:our|the|this) story|my character|"
        r"joking|hypothetical)\b", original, re.IGNORECASE,
    ))


def source_timestamp(row: dict, role: str) -> str:
    return row.get(role + "_created_at", row["created_at"])


def _qualified_claim(content: str) -> bool:
    """Conservative qualifier guard for metadata that changes future behavior."""
    return bool("?" in content or re.search(
        r"\b(?:not|never|if|unless|whether|would|could|might|may|perhaps|maybe|think|hope|plan|intend)\b"
        r"|n['’]t\b", content, re.IGNORECASE,
    ))


def transition_supported(content: str, op: str, item_content: str,
                          archive_reason: str | None = None) -> bool:
    """A terminal patch needs a positive, current/past, topic-matched statement."""
    # Check each complete sentence without dropping the full original from
    # provenance. A question about tomorrow need not invalidate a completed
    # development reported in a different sentence of the same message.
    sentences = re.split(r"(?<=[.!?])\s+|\n+", content)
    if len(sentences) > 1:
        return any(transition_supported(sentence, op, item_content, archive_reason)
                   for sentence in sentences if sentence.strip())
    if not same_topic(content, item_content):
        return False
    if archive_reason == "broken_promise":
        return bool("?" not in content and not re.search(
            r"\b(?:if|unless|would|could|might|maybe|perhaps)\b", content, re.IGNORECASE,
        ) and re.search(
            r"\b(?:broke|broken|failed to|forgot to|missed|cancelled|canceled|"
            r"cannot|unable to)\b|\b(?:didn['’]t|won['’]t|can['’]t)\b|\bwill not\b",
            content, re.IGNORECASE,
        ))
    if archive_reason == "no_longer_current":
        return bool("?" not in content and not re.search(
            r"\b(?:if|unless|would|could|might|maybe|perhaps|will|shall)\b",
            content, re.IGNORECASE,
        ) and re.search(
            r"\bno longer\b|\bnot relevant\b|\bnot\b.+\banymore\b|\b(?:ended|stopped|recovered|"
            r"expired|ceased|cancelled|canceled)\b|\b(?:is|was|are|were)\s+over[.!]?$",
            content, re.IGNORECASE,
        ))
    if archive_reason in {"completed", "conversation_ended"}:
        op = "resolve"
    qualified_text = re.sub(r"\bnot relevant\b", "irrelevant", content,
                            flags=re.IGNORECASE) if op == "archive" else content
    if (op not in {"resolve", "archive"} or not same_topic(content, item_content)
            or _qualified_claim(qualified_text) or re.search(
                r"\b(?:will|shall|going to|tomorrow|next (?:week|month|year))\b",
                content, re.IGNORECASE,
            )):
        return False
    if op == "archive":
        return bool(re.search(
            r"\b(?:archive|not relevant|stop following|leave it|(?:is|was|has been) archived)\b",
            content, re.IGNORECASE,
        ))
    adjective = r"(?:resolved|done|finished|completed|cancelled|canceled|delivered|fulfilled|ended)"
    current_or_past = (
        rf"\b(?:is|was|are|were|am|got|has been|have been)\s+(?:now\s+|already\s+|finally\s+)?{adjective}\b"
        rf"|\b(?:I|we|they|he|she)\s+(?:(?:have|has)\s+)?(?:already\s+|finally\s+)?{adjective}\b"
        rf"|\b{adjective}(?=[.!]|$)"
        r"|\b(?:is|was|are|were)\s+over(?=[.!]|$|\s+(?:now|today|yesterday)\b)"
    )
    return bool(re.search(current_or_past, content, re.IGNORECASE))


def _validate_sources(operation: ContinuityOperation, rows: dict[int, dict]) -> list[dict]:
    if not operation.sources:
        raise ContinuityValidationError("A changed item requires original evidence")
    found = []
    for source in operation.sources:
        row = rows.get(source.exchange_id)
        if (row is None or source.document_id != row.get("document_id")
                or not _source_text(row, source.role).strip()
                or source.excerpt is None and operation.content_format != "narrative"
                or source.excerpt is not None and not whole_source_excerpt(
                    source.excerpt, _source_text(row, source.role))):
            raise ContinuityValidationError("An excerpt is not present in the task's source",
                                            code="source")
        expected_origin = "user_statement" if source.role == "user" else "assistant_statement"
        if source.origin != expected_origin:
            raise ContinuityValidationError("Source origin disagrees with the original speaker")
        found.append(row)
    # Exact extraction is intentional. Arbitrary prose that happens to cite a
    # valid source is not automatically trustworthy.
    excerpts = [source.excerpt for source in operation.sources]
    if (operation.content_format != "narrative"
            and not all(operation.content == excerpt for excerpt in excerpts)
            and operation.content != "\n".join(excerpts)):
        raise ContinuityValidationError("Claim content must consist of exact cited excerpts",
                                        code="content")
    return found


def _supported_date(value: str, sources: list[ContinuitySource], rows: dict[int, dict],
                    timezone: str) -> str:
    """Only accept an explicitly mentioned date or relative calendar expression."""
    resolved: set[str] = set()
    for source in sources:
        if value.casefold() in {"today", "tomorrow", "yesterday"}:
            present = re.search(r"\b" + value.casefold() + r"\b", source.excerpt.casefold())
        else:
            # A written calendar date cannot establish a model-invented hour.
            present = value in source.excerpt
        if present:
            if _qualified_claim(source.excerpt):
                raise ContinuityValidationError("A qualified date cannot be asserted as an event",
                                                code="date")
            timestamp = source_timestamp(rows[source.exchange_id], source.role)
            resolved.add(resolve_event_time(value, timestamp, timezone))
    if len(resolved) != 1:
        raise ContinuityValidationError("An event date has missing or ambiguous source evidence",
                                        code="date")
    return resolved.pop()


def is_legacy_rewrite(operation: dict, item: dict | None) -> bool:
    """Recognize a format conversion that needs no newly observed evidence.

    Preserve the complete existing support and metadata. Any other change still
    needs this batch's sources, including updates to an existing narrative note.
    Callers independently validate the prose, originals, locks and revisions.
    """
    if (not item or item.get("content_format") == "narrative" and not needs_note_metadata(item)
            or item.get("user_created")
            or item.get("origin") == "user_note"
            or item.get("locked", item.get("manual_lock"))
            or not active_items([item], scope=item.get("scope", "unknown"))
            or operation.get("op") != "upsert"
            or operation.get("content_format") != "narrative"
            or operation.get("origin") != "derived"
            or operation.get("category") != item.get("category")
            or operation.get("scope") != item.get("scope")):
        return False
    identity = ("exchange_id", "document_id", "role", "origin")
    prior = {tuple(source.get(key) for key in identity) for source in item.get("sources", [])}
    proposed = {tuple(source.get(key) for key in identity)
                for source in operation.get("sources", [])}
    metadata = ("event_at", "expires_at", "follow_up_at", "promise_owner", "promise_kind")
    return bool(prior) and proposed == prior and all(
        key not in operation or operation[key] == item.get(key) for key in metadata
    )


def validate_transition_evidence(operation: dict, item: dict | None = None, *,
                                 new_exchange_ids: set[int] | None = None) -> None:
    """Shared persistence defense after original source identity/excerpt checks.

    This does not parse timestamps, so Store can recheck normalized operations
    without trying to infer the original relative expression again.
    """
    sources = operation.get("sources", [])
    if (operation["op"] in {"archive", "resolve"}
            and operation.get("archive_reason") in POLICY_ARCHIVE_REASONS):
        # This is an explicit allocation decision, not a claim that the old
        # situation ended. The whole patch must separately pass capacity and
        # lower-importance replacement checks at validation AND persistence.
        if item is None or not (operation.get("decision_reason") or "").strip():
            raise ContinuityValidationError("A policy archive needs its existing note and reason",
                                            code="capacity")
        return
    content = operation.get("content")
    excerpts = [source.get("excerpt") for source in sources]
    roles = {source.get("role") for source in sources}
    narrative = operation.get("content_format") == "narrative"
    if (not excerpts or not roles <= {"user", "assistant"}
            or any(not isinstance(excerpt, str) or not excerpt.strip() for excerpt in excerpts)):
        raise ContinuityValidationError("A changed item requires original evidence")
    if narrative:
        if (not isinstance(content, str) or not content.strip()
                or len(content) > NARRATIVE_MAX_CHARS or operation.get("origin") != "derived"
                or len(sources) > 8
                or content in [*excerpts, "\n".join(excerpts)]):
            raise ContinuityValidationError("Invalid narrative summary", code="narrative")
    elif (len(roles) != 1 or not all(content == excerpt for excerpt in excerpts)
            and content != "\n".join(excerpts)):
        raise ContinuityValidationError("Changed content must preserve exact same-speaker evidence")
    origin = "derived" if narrative else (
        "user_statement" if roles == {"user"} else "assistant_statement")
    if operation.get("origin") is not None and operation["origin"] != origin:
        raise ContinuityValidationError("Claim origin disagrees with its evidence")
    rewrite = is_legacy_rewrite(operation, item)
    fresh = [source["excerpt"] for source in sources
             if rewrite or new_exchange_ids is None or source["exchange_id"] in new_exchange_ids]
    if operation["op"] in {"resolve", "archive"}:
        if item is None:
            raise ContinuityValidationError("A terminal operation requires an existing item")
        # Generated prose must never manufacture a terminal transition. One
        # complete original message must independently support closing the topic.
        if not any(transition_supported(excerpt, operation["op"], item["content"],
                                       operation.get("archive_reason"))
                   for excerpt in fresh):
            raise ContinuityValidationError("A terminal operation needs positive, topic-matched evidence",
                                            code="terminal")
    elif (operation["op"] == "upsert" and item
          and not any(same_topic(excerpt, item["content"]) for excerpt in fresh)):
        raise ContinuityValidationError("An existing item needs evidence about the same topic")
    owner, kind = operation.get("promise_owner"), operation.get("promise_kind")
    if bool(owner) != bool(kind):
        raise ContinuityValidationError("A promise must identify both author and kind")
    if owner:
        if owner not in roles:
            raise ContinuityValidationError("A promise requires the original author's statement")
        # Check the author's evidence, never the generated summary's wording.
        commitments = [source["excerpt"] for source in sources if source["role"] == owner
                       and re.match(r"\s*(?:I\s+(?:promise|promised|will|shall)|I['’]ll)\b",
                                    source["excerpt"], re.IGNORECASE)]
        if not commitments:
            raise ContinuityValidationError("A promise requires an explicit commitment",
                                            code="promise_commitment")
        if all(_qualified_claim(excerpt) for excerpt in commitments):
            raise ContinuityValidationError("A qualified or negated statement is not an open promise",
                                            code="promise_qualified")
        if kind == "scheduled" and not any(not _qualified_claim(excerpt) and re.search(
            r"\b(?:already\s+(?:scheduled|configured|set)|existing\s+(?:scheduled|configured)|"
            r"(?:is|was|has been|have been)\s+(?:already\s+)?(?:scheduled|configured))\b",
            excerpt, re.IGNORECASE,
        ) for excerpt in commitments):
            raise ContinuityValidationError("A scheduled promise requires existing-action evidence",
                                            code="promise_scheduled")


def validate_operations(proposal: dict, *, inputs: list[dict], state: dict,
                        scope: Scope = "real_life", timezone: str = "UTC",
                        supporting_inputs: list[dict] | None = None,
                        require_narrative: bool = False,
                        max_notes: int = DEFAULT_MAX_NOTES) -> list[dict]:
    """Validate a whole proposal before the Store performs its atomic CAS commit.

    State transitions require an explicit source statement naming the same topic.
    Omitted items do not participate. Timestamps of observation are assigned from
    source rows rather than accepting the model's claim about when it learned them.
    """
    try:
        parsed = ContinuityProposal.model_validate(proposal)
    except ValidationError as exc:
        # Inspect only the schema location, never include model values, unknown
        # field names or Pydantic's error text in persisted/replayed diagnostics.
        invalid_op = any(
            len(error["loc"]) == 3 and error["loc"][0] == "operations"
            and type(error["loc"][1]) is int and error["loc"][2] == "op"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ContinuityValidationError("Malformed continuity operations",
                                        code="schema_op" if invalid_op else "schema") from None
    operations = parsed.operations
    if any(operation.op == "no_change" for operation in operations):
        only = operations[0]
        if (len(operations) != 1 or only.op != "no_change"
                or only.expected_revision != state["revision"]
                or any(value for key, value in only.model_dump().items()
                       if key not in {"op", "expected_revision"})):
            raise ContinuityValidationError("no_change must be the sole, empty operation")
        return [{"op": "no_change", "expected_revision": only.expected_revision, "sources": []}]

    new_rows = _source_rows(inputs)
    supporting_rows = _source_rows(supporting_inputs or [])
    items = {item["id"]: item for item in state.get("items", [])}
    touched: set[str] = set()
    normalized = []
    for operation in operations:
        item = items.get(operation.item_id) if operation.item_id else None
        if operation.item_id:
            if operation.item_id in touched:
                raise ContinuityValidationError("An item cannot be changed twice in one batch")
            touched.add(operation.item_id)
            if item is None:
                raise ContinuityValidationError("An operation names an unknown item")
        if item and item.get("user_created"):
            raise ContinuityValidationError("A user-created note cannot be changed automatically")
        if item and (item.get("locked", item.get("manual_lock")) or item.get("invalidated")
                     or item.get("suppressed")):
            raise ContinuityValidationError("A locked or excluded item cannot be changed automatically")
        expected = item["revision"] if item else 0
        if operation.expected_revision != expected:
            raise ContinuityValidationError("Item revision changed before proposal validation")
        policy_archive = (operation.op in {"resolve", "archive"}
                          and operation.archive_reason in POLICY_ARCHIVE_REASONS)
        if (require_narrative and operation.content_format != "narrative"
                and not policy_archive):
            raise ContinuityValidationError("Narrative summaries are required", code="narrative")
        validate_note_metadata(operation.model_dump(), item)
        # Older support is usable only for this same item's existing evidence.
        # Pure legacy conversions can reuse their complete original support;
        # all other changes must also cite this task's evidence.
        prior_ids = {source["exchange_id"] for source in item.get("sources", [])} if item else set()
        prior_keys = {(source["exchange_id"], source["document_id"], source["role"])
                      for source in item.get("sources", [])} if item else set()
        if any(source.exchange_id not in new_rows
               and (source.exchange_id, source.document_id, source.role) not in prior_keys
               for source in operation.sources):
            raise ContinuityValidationError("A changed item requires original evidence")
        rows = {key: row for key, row in supporting_rows.items() if key in prior_ids}
        rows.update(new_rows)
        source_rows = ([] if policy_archive and not operation.sources
                       else _validate_sources(operation, rows))
        if (not any(source.exchange_id in new_rows for source in operation.sources)
                and not is_legacy_rewrite(operation.model_dump(exclude_unset=True), item)
                and not policy_archive):
            raise ContinuityValidationError("An update needs this batch's evidence",
                                            code="batch_source")
        roles = {source.role for source in operation.sources}
        if len(roles) != 1 and operation.content_format != "narrative" and not policy_archive:
            raise ContinuityValidationError("Separate notes are required for different speakers")
        origin = "derived" if operation.content_format == "narrative" else (
            "user_statement" if roles == {"user"} else "assistant_statement")
        if operation.origin is not None and operation.origin != origin:
            raise ContinuityValidationError("Claim origin disagrees with its evidence")
        if operation.scope is not None and operation.scope != scope:
            raise ContinuityValidationError("An operation cannot redirect a task to another scope")
        if scope == "real_life":
            for source in operation.sources:
                original = _source_text(rows[source.exchange_id], source.role)
                if source_is_fictional(original):
                    raise ContinuityValidationError("Fictional evidence cannot become real-life state")
        timestamps = [aware_time(source_timestamp(row, source.role))
                      for row, source in zip(source_rows, operation.sources, strict=True)]
        observed = max(timestamps).isoformat() if timestamps else item.get("observed_at")
        result = operation.model_dump(exclude_none=True)
        result["sources"] = [{**source.model_dump(),
                              "excerpt": _source_text(row, source.role)}
                             for row, source in zip(source_rows, operation.sources, strict=True)]
        # Explicit null is a supported removal of optional metadata; omitted
        # fields leave existing metadata unchanged in the patch.
        for field in operation.model_fields_set:
            if getattr(operation, field) is None:
                result[field] = None
        result.update(origin=origin, scope=scope, observed_at=observed, last_mention_at=observed)
        validate_transition_evidence(result, item, new_exchange_ids=set(new_rows))
        if (operation.op == "upsert" and (operation.category is None
                or operation.origin is None or operation.scope is None)):
            raise ContinuityValidationError("A new or updated note requires category, origin, and scope")
        for field in ("event_at", "follow_up_at"):
            value = getattr(operation, field)
            if value:
                result[field] = _supported_date(
                    value, [ContinuitySource.model_validate(source) for source in result["sources"]],
                    rows, timezone)
        if operation.expires_at and not policy_archive:
            expiry = aware_time(operation.expires_at)
            if expiry <= max(timestamps):
                raise ContinuityValidationError("An item's expiry must be after its observation")
            # Expiry is an application policy, not a new event/date assertion.
            if expiry > max(timestamps) + timedelta(days=30):
                raise ContinuityValidationError("Automatic expiry exceeds the transient policy")
            result["expires_at"] = expiry.isoformat()
        if operation.category == "emotion" and not policy_archive:
            # A temporary feeling can never turn into an undated permanent trait.
            result.setdefault("expires_at", (max(timestamps) + timedelta(hours=48)).isoformat())
        normalized.append(result)
    validate_note_capacity(normalized, state.get("items", []), max_notes=max_notes, scope=scope)
    return normalized


def optional_metadata_candidates(source: dict, row: dict, *, timezone: str = "UTC") -> dict:
    """Offer literals and commitment kinds that pass the existing evidence policy.

    Candidates establish eligibility only. The model must still choose which
    date/commitment belongs to the note rather than treating any mention as an event.
    """
    original = _source_text(row, source["role"])
    citation = ContinuitySource.model_validate({**source, "excerpt": original})
    candidates: dict = {}
    dates = []
    for value in dict.fromkeys(re.findall(
        r"\b(?:\d{4}-\d{2}-\d{2}|today|tomorrow|yesterday)\b", original, re.IGNORECASE,
    )):
        try:
            _supported_date(value, [citation], {row["exchange_id"]: row}, timezone)
        except ContinuityValidationError:
            continue
        dates.append(value)
    if dates:
        candidates["dates"] = dates
    for kind in ("conversation", "scheduled"):
        try:
            validate_transition_evidence({
                "op": "upsert", "content": original, "sources": [citation.model_dump()],
                "promise_owner": source["role"], "promise_kind": kind,
            })
        except ContinuityValidationError:
            continue
        candidates["promise_kind"] = kind
    return candidates


def active_items(items: list[dict], *, now: datetime | str | None = None,
                 scope: Scope = "real_life") -> list[dict]:
    """Expiry is checked on every read, even when there have been no new writes."""
    instant = aware_time(now or datetime.now(dt_timezone.utc))
    result = []
    for item in items:
        if (item.get("scope", "unknown") != scope
                or item.get("state", item.get("status", "open")) != "open"
                or item.get("invalidated") or item.get("suppressed")):
            continue
        try:
            if item.get("expires_at") and aware_time(item["expires_at"]) <= instant:
                continue
            if (item.get("category") == "emotion" and not item.get("expires_at")
                    and not item.get("user_created")):
                continue
        except ContinuityValidationError:
            continue
        result.append(item)
    return result


def follow_up_due(item: dict, *, now: datetime | str | None = None,
                  cooldown: timedelta = timedelta(hours=24)) -> bool:
    """Only conversational opportunities; this helper cannot create a notification."""
    instant = aware_time(now or datetime.now(dt_timezone.utc))
    if not active_items([item], now=instant, scope=item.get("scope", "unknown")):
        return False
    if item.get("promise_kind") == "scheduled":
        return False
    if item.get("importance") == "low":
        return False
    try:
        if item.get("follow_up_at") and aware_time(item["follow_up_at"]) > instant:
            return False
        previous = item.get("last_followup_at", item.get("last_follow_up_at"))
        return not previous or aware_time(previous) + cooldown <= instant
    except ContinuityValidationError:
        return False
