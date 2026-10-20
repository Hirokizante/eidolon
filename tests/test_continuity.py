from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from eidolon.memory.continuity import (
    ContinuityValidationError, active_items, follow_up_due, resolve_event_time,
    validate_operations, whole_source_excerpt,
)


def exchange(identifier=2, *, user="My interview is tomorrow.", assistant="Good luck.",
             timestamp="2026-10-14T03:00:00+08:00"):
    return {"exchange_id": identifier, "document_id": f"test:{identifier}",
            "created_at": timestamp, "user_content": user, "assistant_content": assistant}


def operation(row=None, *, role="user", content=None, **changes):
    row = row or exchange()
    content = content or row[role + "_content"]
    origin = role + "_statement"
    return {"op": "upsert", "expected_revision": 0, "category": "situation",
            "topic": "Interview", "importance": "moderate",
            "content": content, "origin": origin, "scope": "real_life",
            "sources": [{"exchange_id": row["exchange_id"], "document_id": row["document_id"],
                         "role": role, "excerpt": content, "origin": origin}], **changes}


def validate(op, *, rows=None, state=None, scope="real_life"):
    return validate_operations({"operations": [op]}, inputs=rows or [exchange()],
                               state=state or {"revision": 0, "items": []}, scope=scope, timezone="Asia/Manila")


@pytest.mark.parametrize("action", ["upsert", "resolve", "archive"])
def test_user_created_notes_are_protected_even_when_unlocked(action):
    item = {"id": "authored", "revision": 1, "content": "My interview is tomorrow.",
            "user_created": True, "locked": False, "scope": "real_life", "state": "open"}
    with pytest.raises(ContinuityValidationError, match="user-created"):
        validate(operation(item_id=item["id"], expected_revision=1, op=action),
                 state={"revision": 1, "items": [item]})


def test_relative_day_uses_actual_local_midnight_not_room_boundary():
    at = "2026-10-14T03:00:00+08:00"
    assert resolve_event_time("today", at, timezone="Asia/Manila") == "2026-10-14T00:00:00+08:00"
    assert resolve_event_time("tomorrow", at, timezone="Asia/Manila") == "2026-10-15T00:00:00+08:00"
    assert resolve_event_time("yesterday", at, timezone="Asia/Manila") == "2026-10-13T00:00:00+08:00"
    assert resolve_event_time("today", "2026-10-13T19:00:00+00:00", timezone="Asia/Manila") == (
        "2026-10-14T00:00:00+08:00"
    )


@pytest.mark.parametrize("value", ["next week", "2026-99-20", "2026-10-14T03:00:00"])
def test_invalid_or_naive_event_dates_are_rejected(value):
    with pytest.raises(ContinuityValidationError):
        resolve_event_time(value, "2026-10-14T03:00:00+08:00", timezone="Asia/Manila")


def test_source_dates_and_origins_survive_normalization():
    result = validate(operation(event_at="tomorrow"))[0]
    assert result["event_at"] == "2026-10-15T00:00:00+08:00"
    assert result["observed_at"] == "2026-10-14T03:00:00+08:00"
    assert result["origin"] == "user_statement"
    assert result["sources"][0]["excerpt"] == "My interview is tomorrow."


def test_calendar_day_evidence_cannot_assert_invented_hour_or_timezone():
    row = exchange(user="My interview is on 2026-10-15.")
    assert validate(operation(row, event_at="2026-10-15"), rows=[row])[0]["event_at"] == (
        "2026-10-15T00:00:00+08:00"
    )
    with pytest.raises(ContinuityValidationError):
        validate(operation(row, event_at="2026-10-15T13:00:00+00:00"), rows=[row])


def test_relative_date_anchors_to_actual_matching_source_not_earliest_citation():
    first = exchange(user="My creative project is unfinished.")
    second = exchange(4, user="My project review is tomorrow.",
                      timestamp="2026-10-16T03:00:00+08:00")
    op = operation(first, content=first["user_content"] + "\n" + second["user_content"],
                   event_at="tomorrow")
    op["sources"] = [operation(first)["sources"][0], operation(second)["sources"][0]]
    result = validate(op, rows=[first, second])[0]
    assert result["event_at"] == "2026-10-17T00:00:00+08:00"


def test_different_relative_anchors_cannot_silently_choose_one_date():
    first = exchange()
    second = exchange(4, timestamp="2026-10-16T03:00:00+08:00")
    op = operation(first, event_at="tomorrow")
    op["sources"].append(operation(second)["sources"][0])
    with pytest.raises(ContinuityValidationError, match="ambiguous"):
        validate(op, rows=[first, second])


def test_each_speaker_uses_original_message_time_and_not_completion_time():
    row = exchange()
    row.update(user_created_at="2026-10-14T23:59:00+08:00",
               assistant_created_at="2026-10-15T00:02:00+08:00")
    assert validate(operation(row, event_at="tomorrow"), rows=[row])[0]["event_at"] == (
        "2026-10-15T00:00:00+08:00"
    )
    row["assistant_content"] = "I will ask about the interview tomorrow."
    result = validate(operation(row, role="assistant", event_at="tomorrow"), rows=[row])[0]
    assert result["event_at"] == "2026-10-16T00:00:00+08:00"
    assert result["observed_at"] == "2026-10-15T00:02:00+08:00"


@pytest.mark.parametrize("changes", [
    {"content": "The user has an interview with a large employer tomorrow."},
    {"scope": "fiction"}, {"origin": "assistant_statement"},
    {"event_at": "2027-01-01"}, {"trust_score": 90}, {"op": "delete"},
    {"expected_revision": True},
])
def test_unsupported_claims_and_schema_fields_are_rejected(changes):
    with pytest.raises(ContinuityValidationError):
        validate(operation(**changes))


@pytest.mark.parametrize("key,value", [
    ("operation", "upsert"), ("action", "upsert"), ("type", "upsert"),
    ("private_model_field", "private model output"), ("op", "private model output"),
])
def test_invalid_op_key_or_value_gets_safe_actionable_schema_feedback(key, value):
    op = operation()
    op.pop("op")
    op[key] = value
    with pytest.raises(ContinuityValidationError) as caught:
        validate(op)
    reason = caught.value.public_reason
    assert reason == ContinuityValidationError.REASONS["schema_op"]
    assert "JSON key op" in reason
    assert "private" not in reason


def test_valid_op_does_not_allow_extra_operation_alias_or_untrusted_schema_details():
    op = operation(operation="upsert", private_model_field="private model output")
    with pytest.raises(ContinuityValidationError) as caught:
        validate(op)
    assert caught.value.public_reason == ContinuityValidationError.REASONS["schema"]


@pytest.mark.parametrize("field,value", [
    ("exchange_id", 999), ("document_id", "invented"), ("excerpt", "An invented event."),
    ("origin", "assistant_statement"), ("role", "assistant"),
])
def test_valid_ids_cannot_replace_exact_inspectable_evidence(field, value):
    op = operation()
    op["sources"][0][field] = value
    with pytest.raises(ContinuityValidationError):
        validate(op)


def test_speaker_mixing_needs_separate_claims():
    op = operation()
    op["sources"].append(operation(role="assistant")["sources"][0])
    with pytest.raises(ContinuityValidationError):
        validate(op)


def test_unrelated_extra_citation_cannot_make_an_older_claim_look_newer():
    first = exchange()
    newer = exchange(4, user="I prefer coffee in the morning.",
                     timestamp="2026-10-16T03:00:00+08:00")
    op = operation(first)
    op["sources"].append(operation(newer)["sources"][0])
    with pytest.raises(ContinuityValidationError):
        validate(op, rows=[first, newer])


@pytest.mark.parametrize("original,excerpt", [
    ("I never said my interview is tomorrow.", "my interview is tomorrow."),
    ("My interview is tomorrow, in a hypothetical scenario.", "My interview is tomorrow"),
])
def test_clipped_negation_or_qualifier_cannot_create_an_assertion(original, excerpt):
    row = exchange(user=original)
    with pytest.raises(ContinuityValidationError):
        validate(operation(row, content=excerpt), rows=[row])
    assert not whole_source_excerpt(excerpt, original)


@pytest.mark.parametrize("original", [
    "In our story, a character attends an interview. My interview is tomorrow.",
    "I am joking. My interview is tomorrow.",
    "Pretend we have new jobs. My interview is tomorrow.",
])
def test_fictional_qualifier_elsewhere_in_source_prevents_real_life_learning(original):
    row = exchange(user=original)
    with pytest.raises(ContinuityValidationError):
        validate(operation(row, content="My interview is tomorrow."), rows=[row])
    op = operation(row, scope="fiction")
    assert validate(op, rows=[row], scope="fiction")[0]["scope"] == "fiction"


def test_complete_message_preserves_distant_third_party_attribution():
    original = "My brother, not me, sent this message:\nI have an interview tomorrow."
    row = exchange(user=original)
    with pytest.raises(ContinuityValidationError):
        validate(operation(row, content="I have an interview tomorrow."), rows=[row])
    quoted = validate(operation(row), rows=[row])[0]
    assert quoted["content"] == original
    assert quoted["sources"][0]["excerpt"] == original


def test_no_change_is_valid_and_does_not_manufacture_content():
    assert validate({"op": "no_change", "expected_revision": 3},
                    state={"revision": 3, "items": []}) == [
        {"op": "no_change", "expected_revision": 3, "sources": []},
    ]
    with pytest.raises(ContinuityValidationError):
        validate({"op": "no_change", "expected_revision": 0, "content": "extra"})


def test_locked_notes_and_stale_revisions_cannot_be_replaced():
    item = {"id": "stable", "revision": 2, "content": "A reviewed note.", "locked": True}
    op = operation(item_id="stable", expected_revision=2)
    with pytest.raises(ContinuityValidationError, match="locked"):
        validate(op, state={"revision": 2, "items": [item]})
    item["locked"] = False
    op["expected_revision"] = 1
    with pytest.raises(ContinuityValidationError, match="revision"):
        validate(op, state={"revision": 2, "items": [item]})


def test_resolve_needs_explicit_topic_matched_evidence_and_inactive_item_is_excluded():
    item = {"id": "stable", "revision": 1, "content": "My interview is tomorrow.",
            "scope": "real_life", "state": "open"}
    row = exchange(user="My interview is finished.")
    op = operation(row, op="resolve", item_id="stable", expected_revision=1)
    assert validate(op, rows=[row], state={"revision": 1, "items": [item]})[0]["op"] == "resolve"
    for text in ("My interview is tomorrow.", "My project is finished."):
        row = exchange(user=text)
        with pytest.raises(ContinuityValidationError):
            validate(operation(row, op="resolve", item_id="stable", expected_revision=1),
                     rows=[row], state={"revision": 1, "items": [item]})
    assert active_items([{**item, "state": "resolved"}, {**item, "state": "archived"}]) == []


@pytest.mark.parametrize("content", [
    "My interview is not finished.", "My interview will be completed tomorrow.",
    "If my interview finished, I could start the project.", "My interview is done?",
    "My interview isn't over.", "My interview might be finished.",
    "My interview is over the bridge.", "We discussed the finished interview notes.",
    "My interview is no longer scheduled for Monday; it is on Friday.",
])
def test_negated_conditional_questioned_or_future_events_cannot_resolve_a_thread(content):
    row = exchange(user=content)
    item = {"id": "stable", "revision": 1, "content": "My interview is tomorrow."}
    with pytest.raises(ContinuityValidationError):
        validate(operation(row, op="resolve", item_id="stable", expected_revision=1),
                 rows=[row], state={"revision": 1, "items": [item]})


@pytest.mark.parametrize("content", [
    "I will not ask about your project tomorrow.", "I think I will ask about your project tomorrow.",
    "I will ask about your project tomorrow?",
])
def test_negated_or_tentative_commitment_does_not_become_a_promise(content):
    row = exchange(assistant=content)
    with pytest.raises(ContinuityValidationError):
        validate(operation(row, role="assistant", category="relationship",
                           promise_owner="assistant", promise_kind="conversation"), rows=[row])


def test_negated_date_or_request_to_refrain_cannot_create_an_event_or_followup():
    for content, field in (("My interview is not tomorrow.", "event_at"),
                           ("Don't ask about my interview tomorrow.", "follow_up_at")):
        row = exchange(user=content)
        with pytest.raises(ContinuityValidationError):
            validate(operation(row, **{field: "tomorrow"}), rows=[row])


def test_upserts_cannot_replace_an_existing_item_with_an_unrelated_supported_quote():
    item = {"id": "stable", "revision": 1, "content": "My interview is tomorrow."}
    row = exchange(user="I prefer coffee in the morning.")
    with pytest.raises(ContinuityValidationError, match="same topic"):
        validate(operation(row, item_id="stable", expected_revision=1), rows=[row],
                 state={"revision": 1, "items": [item]})
    item["content"] = "Please give concise replies."
    row = exchange(user="Please give detailed replies.")
    assert validate(operation(row, category="relationship", item_id="stable", expected_revision=1),
                    rows=[row], state={"revision": 1, "items": [item]})


def test_emotional_context_is_dated_expires_on_read_and_preserves_history():
    row = exchange(user="I feel anxious about the interview today.")
    item = validate(operation(row, category="emotion"), rows=[row])[0]
    item.update(id="emotion", state="open")
    original = deepcopy(item)
    assert item["expires_at"] == "2026-10-16T03:00:00+08:00"
    assert active_items([item], now="2026-10-15T03:00:00+08:00") == [item]
    assert active_items([item], now="2026-10-16T03:00:00+08:00") == []
    assert item == original
    assert active_items([{**item, "expires_at": None}], now="2026-10-14T03:00:00+08:00") == []


def test_excluded_or_other_scope_items_never_become_working_context():
    item = {"scope": "real_life", "state": "open", "content": "An open project."}
    assert active_items([item]) == [item]
    assert active_items([{**item, "invalidated": True}, {**item, "scope": "fiction"}]) == []


def test_promise_author_and_future_conversation_are_explicit():
    row = exchange(assistant="I will ask about your project tomorrow.")
    op = operation(row, role="assistant", category="relationship", promise_owner="assistant",
                   promise_kind="conversation", follow_up_at="tomorrow")
    assert validate(op, rows=[row])[0]["promise_owner"] == "assistant"
    for changes in ({"promise_owner": "user"}, {"promise_kind": "scheduled"},
                    {"promise_kind": None}):
        with pytest.raises(ContinuityValidationError):
            validate({**op, **changes}, rows=[row])


def test_existing_scheduled_action_is_labelled_without_a_conversational_followup():
    row = exchange(assistant="I will use the already configured project reminder tomorrow.")
    item = validate(operation(row, role="assistant", category="relationship",
                              promise_owner="assistant", promise_kind="scheduled",
                              follow_up_at="tomorrow"), rows=[row])[0]
    assert not follow_up_due(item, now="2026-10-16T00:00:00+08:00")


def test_quoted_third_party_promise_cannot_be_assigned_to_the_message_author():
    row = exchange(user="My brother said: I will finish the garden illustration tomorrow.")
    with pytest.raises(ContinuityValidationError, match="explicit commitment"):
        validate(operation(row, category="relationship", promise_owner="user",
                           promise_kind="conversation"), rows=[row])


def test_later_commitment_preserves_complete_note_without_promise_metadata():
    row = exchange(user="The garden illustration is ready. I will show it tomorrow.")
    op = operation(row, category="thread", promise_owner="user", promise_kind="conversation")
    with pytest.raises(ContinuityValidationError) as caught:
        validate(op, rows=[row])
    assert "beginning with an explicit first-person commitment" in caught.value.public_reason
    del op["promise_owner"], op["promise_kind"]
    assert validate(op, rows=[row])[0]["content"] == row["user_content"]


def test_future_intention_to_configure_scheduled_action_is_a_conversation_promise():
    row = exchange(assistant="I will set up a scheduled project reminder tomorrow.")
    op = operation(row, role="assistant", category="relationship", promise_owner="assistant",
                   promise_kind="scheduled")
    with pytest.raises(ContinuityValidationError, match="existing-action"):
        validate(op, rows=[row])
    op["promise_kind"] = "conversation"
    assert validate(op, rows=[row])[0]["promise_kind"] == "conversation"


def test_followup_cooldown_checks_actual_offer_not_context_selection():
    now = datetime.fromisoformat("2026-10-15T10:00:00+08:00")
    item = {"scope": "real_life", "state": "open", "follow_up_at": (now - timedelta(days=1)).isoformat()}
    assert follow_up_due(item, now=now)
    item["last_followup_at"] = now.isoformat()
    assert not follow_up_due(item, now=now + timedelta(hours=1))
    assert follow_up_due(item, now=now + timedelta(hours=24))


def test_undated_open_topic_tracks_followup_cooldown_without_inventing_a_date():
    now = datetime.fromisoformat("2026-10-15T10:00:00+08:00")
    item = {"scope": "real_life", "state": "open", "category": "thread"}
    assert follow_up_due(item, now=now)
    item["last_followup_at"] = now.isoformat()
    assert not follow_up_due(item, now=now + timedelta(hours=1))
    assert "follow_up_at" not in item
