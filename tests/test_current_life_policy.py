"""Current Life admission, topic ownership, retirement, and legacy classification."""
import json

import pytest

from eidolon.memory.continuity import (
    ContinuityValidationError, active_items, follow_up_due, validate_operations,
)
from eidolon.memory.coordinator import MemoryCoordinator
from eidolon.memory.maintenance import ContinuityWorker
from eidolon.storage import Store

from test_continuity_narrative import add, cite, narrative
from test_memory_maintenance import Provider


@pytest.fixture
def store(tmp_path):
    storage = Store(tmp_path / "room.sqlite3")
    yield storage
    storage.close()


def seed_note(store, *, topic="Creative project", importance="low", text=None, max_notes=12):
    text = text or f"My {topic.lower()} is ongoing."
    row = add(store, text)
    state = store.continuity_state()
    inputs = store.continuity_inputs(after=state["watermark"])
    operation = narrative(row, f"The user's {topic.lower()} is ongoing.",
                          topic=topic, importance=importance)
    operations = validate_operations({"operations": [operation]}, inputs=inputs, state=state,
                                     max_notes=max_notes, require_narrative=True)
    task = store.create_maintenance_task(inputs, expected_revision=state["revision"],
                                         control_revision=state["control_revision"])
    result = store.commit_continuity(task["id"], operations, expected_revision=state["revision"],
                                     control_revision=state["control_revision"], max_notes=max_notes)
    return next(item for item in result["items"] if item["topic"] == topic)


def archive(note, *, reason="replaced", sources=None):
    return {"op": "archive", "item_id": note["id"], "expected_revision": note["revision"],
            "topic": note["topic"], "importance": note["importance"],
            "content": note["content"], "content_format": "narrative", "origin": "derived",
            "scope": "real_life", "sources": sources or [], "archive_reason": reason,
            "decision_reason": "Keep the more consequential ongoing subject in the limited space."}


def proposal_task(store, operations, *, max_notes=1):
    state = store.continuity_state()
    inputs = store.continuity_inputs(after=state["watermark"])
    task = store.create_maintenance_task(inputs, expected_revision=state["revision"],
                                         control_revision=state["control_revision"])
    result = validate_operations({"operations": operations}, inputs=inputs, state=state,
                                 max_notes=max_notes, require_narrative=True,
                                 supporting_inputs=store.continuity_evidence([
                                     source["exchange_id"] for note in state["items"]
                                     for source in note.get("sources", [])]))
    return state, task, result


@pytest.mark.parametrize("field,value", [
    ("topic", None), ("topic", ""), ("topic", "  "), ("topic", "two\nsubjects"),
    ("topic", "x" * 101), ("importance", None), ("importance", "medium"),
    ("importance", "critical"), ("importance", 3),
])
def test_new_notes_require_valid_topic_and_importance_at_both_boundaries(store, field, value):
    row = add(store)
    state = store.continuity_state()
    operation = narrative(row, **{field: value})
    with pytest.raises(ContinuityValidationError):
        validate_operations({"operations": [operation]}, inputs=[row], state=state)
    task = store.create_maintenance_task([row], expected_revision=0, control_revision=0)
    # Direct callers already supply resolved, complete source excerpts.
    operation["sources"][0]["excerpt"] = row["user"]
    with pytest.raises(ValueError):
        store.commit_continuity(task["id"], [operation], expected_revision=0, control_revision=0)
    assert store.continuity_state() == state


def test_an_update_keeps_its_topic_and_requires_a_development_in_that_topic(store):
    note = seed_note(store)
    row = add(store, "My creative project now has a first sketch.")
    update = narrative(row, "The user's creative project now has a first sketch.",
                       topic=note["topic"], importance="moderate", item_id=note["id"],
                       expected_revision=note["revision"])
    state, task, operations = proposal_task(store, [update])
    saved = store.commit_continuity(task["id"], operations,
                                    expected_revision=state["revision"],
                                    control_revision=state["control_revision"])
    current = saved["items"][0]
    assert current["topic"] == note["topic"] and current["importance"] == "moderate"
    unrelated = add(store, "My dentist appointment is tomorrow.")
    wrong = narrative(unrelated, "The user has an upcoming dentist appointment.",
                      topic=note["topic"], importance="moderate", item_id=note["id"],
                      expected_revision=current["revision"])
    with pytest.raises(ContinuityValidationError, match="same topic"):
        proposal_task(store, [wrong])
    wrong["topic"] = "Dentist appointment"
    with pytest.raises(ContinuityValidationError, match="topic cannot change"):
        proposal_task(store, [wrong])


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("minimal_archive", [False, True])
def test_full_current_life_accepts_only_explicit_lower_importance_replacement(
    store, reverse_order, minimal_archive,
):
    old = seed_note(store)
    row = add(store, "Our anniversary is approaching; I promised to celebrate it with you.")
    new = narrative(row, "The user and Eidolon have an upcoming anniversary celebration.",
                    topic="Our anniversary", importance="high", replaces_item_id=old["id"],
                    decision_reason="Their anniversary carries more relational weight than a routine project.")
    operations = [archive(old), new]
    if minimal_archive:
        operations[0] = {key: value for key, value in operations[0].items() if key in {
            "op", "item_id", "expected_revision", "archive_reason", "decision_reason", "sources",
        }}
    if reverse_order:
        operations.reverse()
    state, task, checked = proposal_task(store, operations)
    result = store.commit_continuity(task["id"], checked,
                                     expected_revision=state["revision"],
                                     control_revision=state["control_revision"], max_notes=1)
    assert result["active_count"] == 1
    retired = store.get_continuity_item(old["id"])
    assert retired["state"] == "archived" and retired["archive_reason"] == "replaced"
    assert retired["sources"] == old["sources"] and retired["content"] == old["content"]
    admitted = active_items(result["items"])[0]
    assert admitted["topic"] == "Our anniversary" and admitted["replaces_item_id"] == old["id"]
    assert admitted["decision_reason"] == new["decision_reason"]
    context = MemoryCoordinator(None).prepare_continuity(
        store, store.session_for()["id"], enabled=True)
    assert "Our anniversary" in context.text and "high importance" in context.text
    assert old["content"] not in context.text


@pytest.mark.parametrize("case", ["no_archive", "no_link", "no_reason", "equal", "lower", "orphan"])
def test_rejected_replacement_cannot_partially_archive_or_advance_the_watermark(store, case):
    old = seed_note(store, importance="moderate")
    row = add(store, "Our anniversary is approaching.")
    new = narrative(row, "The user and Eidolon's anniversary is approaching.",
                    topic="Our anniversary", importance="high", replaces_item_id=old["id"],
                    decision_reason="The anniversary matters more to their relationship.")
    retired = archive(old)
    operations = [retired, new]
    if case == "no_archive":
        operations = [new]
    elif case == "no_link":
        new.pop("replaces_item_id")
    elif case == "no_reason":
        new.pop("decision_reason")
    elif case in {"equal", "lower"}:
        new["importance"] = "moderate" if case == "equal" else "low"
    else:
        operations = [retired]
    before = store.continuity_state()
    with pytest.raises(ContinuityValidationError):
        proposal_task(store, operations)
    task = store.pending_maintenance_task()
    for operation in operations:
        for source in operation.get("sources", []):
            source["excerpt"] = row["user"]
    with pytest.raises(ValueError):
        store.commit_continuity(task["id"], operations, expected_revision=before["revision"],
                                control_revision=before["control_revision"], max_notes=1)
    assert store.continuity_state() == before
    assert store.pending_maintenance_task()["id"] == task["id"]


def test_a_changed_limit_is_rechecked_at_commit_time(store):
    seed_note(store)
    row = add(store, "Our anniversary is approaching.")
    new = narrative(row, "The user and Eidolon's anniversary is approaching.",
                    topic="Our anniversary", importance="high")
    before, task, checked = proposal_task(store, [new], max_notes=2)
    with pytest.raises(ContinuityValidationError, match="exceed max_notes"):
        store.commit_continuity(task["id"], checked, expected_revision=before["revision"],
                                control_revision=before["control_revision"], max_notes=1)
    assert store.continuity_state() == before


@pytest.mark.parametrize("reason,text", [
    ("completed", "I delivered the creative project."),
    ("broken_promise", "I broke my creative project promise."),
    ("no_longer_current", "My creative project is no longer ongoing."),
    ("conversation_ended", "Our creative project discussion is finished. Can we discuss something else?"),
])
async def test_maintenance_archives_ended_topics_without_an_archive_request(store, reason, text):
    continuing = seed_note(store) if reason == "conversation_ended" else None
    old = seed_note(store, topic="Creative project discussion") if continuing else seed_note(store)
    row = add(store, text)
    operation = archive(old, reason=reason, sources=[cite(row)])
    worker = ContinuityWorker(store, Provider(result={"operations": [operation]}))
    assert await worker.run_once(force=True), worker.status
    note = store.get_continuity_item(old["id"])
    assert note["state"] == "archived" and note["archive_reason"] == reason
    assert note["content"] == old["content"] and len(note["sources"]) == 2
    active = active_items(store.continuity_state()["items"])
    assert [note["id"] for note in active] == ([continuing["id"]] if continuing else [])


async def test_lowered_limit_triggers_deliberate_cleanup_without_new_exchanges(store):
    low = seed_note(store)
    high = seed_note(store, topic="Our anniversary", importance="high")
    protected = store.create_continuity_item("An authored subject.", "relationship",
                                             topic="Authored subject", importance="low")
    before = store.continuity_state()

    def choose(document):
        assert document["review_existing"] and document["exchanges"] == []
        assert document["active_count"] == 3 and document["max_notes"] == 2
        return {"operations": [{"op": "archive", "item_id": low["id"],
                                 "expected_revision": low["revision"], "sources": [],
                                 "archive_reason": "capacity",
                                 "decision_reason": "The other subjects carry more importance."}]}

    worker = ContinuityWorker(store, Provider(transform=choose), enabled=True, max_notes=2)
    assert await worker.run_once(), worker.status
    after = store.continuity_state()
    assert after["active_count"] == 2 and after["watermark"] == before["watermark"]
    assert store.get_continuity_item(low["id"])["state"] == "archived"
    assert store.get_continuity_item(low["id"])["content"] == low["content"]
    assert store.get_continuity_item(low["id"])["sources"] == low["sources"]
    assert store.get_continuity_item(high["id"]) == high
    assert store.get_continuity_item(protected["id"]) == protected
    assert not await worker.run_once()


async def test_protected_notes_over_the_cap_survive_and_block_admissions(store):
    for number in range(2):
        store.create_continuity_item("An authored subject.", "relationship",
                                     topic=f"Authored subject {number}", importance="low")
    before = store.continuity_state()
    worker = ContinuityWorker(store, Provider(), enabled=True, max_notes=1)
    assert not await worker.run_once() and worker.provider.calls == []
    with pytest.raises(ValueError, match="Current life is full"):
        store.create_continuity_item("Another subject.", "thread", topic="Another subject",
                                     importance="high", max_notes=1)
    assert store.continuity_state() == before


async def test_bounded_maintenance_finds_cleanup_candidates_beyond_a_protected_prefix(store):
    eligible = seed_note(store)
    # Simulate a collection saved before Current Life had an active-note cap.
    with store.db:
        for number in range(100):
            store._save_continuity_item({
                **eligible, "id": f"protected-{number}", "topic": f"Authored subject {number}",
                "user_created": True, "locked": True, "origin": "user_note", "sources": [],
            }, task_id=None, change_origin="user")
    assert store.continuity_state()["active_count"] == 101
    assert eligible["id"] not in {item["id"] for item in store.continuity_state()["items"]}

    def choose(document):
        assert document["active_count"] == 101 and document["max_notes"] == 1
        assert any(item["id"] == eligible["id"] for item in document["items"])
        return {"operations": [archive(eligible, reason="capacity")]}

    worker = ContinuityWorker(store, Provider(transform=choose), enabled=True, max_notes=1)
    assert await worker.run_once(), worker.status
    assert store.continuity_state()["active_count"] == 100
    assert store.get_continuity_item(eligible["id"])["state"] == "archived"
    assert not await worker.run_once()


async def test_legacy_narratives_get_deliberate_metadata_without_replaying_chat(store):
    note = seed_note(store)
    with store.db:
        legacy = {key: value for key, value in note.items() if key not in {"topic", "importance"}}
        store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                         (json.dumps(legacy), note["id"]))
    before = store.continuity_state()
    provisional = before["items"][0]
    assert provisional["topic"] and provisional["importance"] == "moderate"
    assert provisional["metadata_pending"]

    def classify(document):
        assert document["rewrite_existing"]
        assert document["legacy_rewrite_ids"] == [note["id"]]
        original = document["exchanges"][0]
        return {"operations": [narrative(
            original, note["content"], topic="Creative project", importance="high",
            item_id=note["id"], expected_revision=note["revision"], category=note["category"])]}

    worker = ContinuityWorker(store, Provider(transform=classify))
    assert await worker.run_once(force=True), worker.status
    after = store.continuity_state()
    classified = after["items"][0]
    assert classified["topic"] == "Creative project" and classified["importance"] == "high"
    assert not classified.get("metadata_pending") and classified["sources"] == note["sources"]
    assert after["watermark"] == before["watermark"]
    assert not await worker.run_once(force=True)


async def test_legacy_classification_is_bounded_and_preserves_existing_notes_across_passes(store):
    notes = [seed_note(store, topic=f"Creative project {number}") for number in range(6)]
    with store.db:
        for note in notes:
            legacy = {key: value for key, value in note.items() if key not in {"topic", "importance"}}
            store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                             (json.dumps(legacy), note["id"]))
    before = store.continuity_state()

    def classify(document):
        targets = document["legacy_rewrite_ids"]
        assert 1 <= len(targets) <= 4
        items = {item["id"]: item for item in document["items"]}
        task_ids = {row["exchange_id"] for row in document["exchanges"]}
        result = []
        for item_id in targets:
            item = items[item_id]
            assert {source["exchange_id"] for source in item["sources"]} <= task_ids
            original = next(note for note in notes if note["id"] == item_id)
            result.append({
                "op": "upsert", "item_id": item_id, "expected_revision": item["revision"],
                "topic": original["topic"], "importance": "high", "category": item["category"],
                "scope": item["scope"], "content": item["content"],
                "content_format": "narrative", "origin": "derived",
                "sources": [{key: source[key] for key in
                             ("exchange_id", "document_id", "role", "origin")}
                            for source in item["sources"]],
            })
        return {"operations": result}

    worker = ContinuityWorker(store, Provider(transform=classify))
    # Resume a persisted task created before conversion was limited to four
    # notes. Its complete immutable evidence must survive the smaller passes.
    old_inputs = worker._source_exchanges(before)
    pending = store.create_maintenance_task(
        old_inputs, expected_revision=before["revision"],
        control_revision=before["control_revision"], rewrite=True,
    )
    assert len(pending["inputs"]) == 6
    assert await worker.run_once(force=True), worker.status
    assert store._maintenance_task(pending["id"])["inputs"] == old_inputs
    assert sum(bool(note.get("metadata_pending"))
               for note in store.continuity_state()["items"]) == 2
    assert await worker.run_once(force=True), worker.status
    after = store.continuity_state()
    assert not any(note.get("metadata_pending") for note in after["items"])
    assert after["watermark"] == before["watermark"]
    for original in notes:
        classified = store.get_continuity_item(original["id"])
        for key in ("content", "category", "scope", "sources"):
            assert classified[key] == original[key]
    assert not await worker.run_once(force=True)


def test_archived_notes_free_slots_and_cannot_be_restored_into_a_full_working_set(store):
    first = store.create_continuity_item("First subject.", "thread", topic="First subject",
                                         importance="low", max_notes=1)
    retired = store.edit_continuity_item(first["id"], {"state": "archived"},
                                         expected_revision=1, max_notes=1)
    store.create_continuity_item("Second subject.", "thread", topic="Second subject",
                                 importance="moderate", max_notes=1)
    with pytest.raises(ValueError, match="Current life is full"):
        store.edit_continuity_item(first["id"], {"state": "open"},
                                   expected_revision=retired["revision"], max_notes=1)
    assert store.continuity_state()["active_count"] == 1
    assert store.get_continuity_item(first["id"])["state"] == "archived"


def test_low_importance_does_not_request_routine_followups():
    note = {"scope": "real_life", "state": "open", "importance": "low"}
    assert not follow_up_due(note)
    assert follow_up_due({**note, "importance": "moderate"})
