import json
from contextlib import closing
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from eidolon.storage import Store


@pytest.fixture
def store(tmp_path):
    storage = Store(tmp_path / "room.db")
    yield storage
    storage.close()


def exchange(store, text="My interview is tomorrow.", *, generation=None, **kwargs):
    session = store.session_for(timezone="Asia/Manila")["id"]
    store.complete_turn(session, text, "I will ask how it went.",
                        generation_id=generation, **kwargs)
    return store.recent_continuity_inputs(limit=1)[0]


def task_for(store, inputs=None):
    state = store.continuity_state()
    return store.create_maintenance_task(
        inputs or store.continuity_inputs(after=state["watermark"]),
        expected_revision=state["revision"], control_revision=state["control_revision"],
    )


def upsert(source, *, item_id=None, expected_revision=0, **kwargs):
    result = {
        "op": "upsert", "expected_revision": expected_revision, "category": "thread",
        "topic": "Interview", "importance": "moderate",
        "content": source["user"], "origin": "user_statement", "scope": "real_life",
        "sources": [{"exchange_id": source["id"], "document_id": source["document_id"],
                     "role": "user", "excerpt": source["user"], "origin": "user_statement"}],
    }
    if item_id is not None:
        result["item_id"] = item_id
    return {**result, **kwargs}


def commit(store, task, operations, *, max_notes=12):
    return store.commit_continuity(
        task["id"], operations, expected_revision=task["expected_revision"],
        control_revision=task["control_revision"],
        max_notes=max_notes,
    )


def test_v8_upgrade_has_consistent_backup_and_preserves_exchange_identities(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    original = exchange(store, generation="kept")
    outbox = store.pending_outbox()[0]
    with store.db:
        for table in ("maintenance_stages", "maintenance_tasks", "continuity_history",
                      "continuity_items", "continuity_scopes"):
            store.db.execute("DROP TABLE " + table)
        store.db.execute("PRAGMA user_version=6")
    upgraded = Store(path)  # Original connection still has its WAL open.
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 14
        assert upgraded.continuity_inputs() == [original]
        assert upgraded.pending_outbox()[0] == outbox
        assert upgraded.migration_backup_path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(upgraded.migration_backup_path) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
            assert not backup.execute(
                "SELECT 1 FROM sqlite_master WHERE name='continuity_items'"
            ).fetchone()
            assert backup.execute("SELECT document_id FROM generations").fetchone()[0] == (
                outbox["document_id"])
            assert backup.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert not upgraded.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        upgraded.close()
        store.close()


def test_inputs_are_oldest_eligible_completed_chat_with_explicit_scope(store):
    session = store.session_for(timezone="Asia/Manila")["id"]
    for name, retain, scope in [
        ("first", True, None), ("opted-out", False, None), ("legacy", True, None),
        ("unknown", True, "unknown"), ("fiction", True, "fiction"), ("last", True, None),
    ]:
        store.complete_turn(session, name, "Answer", generation_id=name, retain=retain,
                            metadata={"scope": scope} if scope else {})
    with store.db:
        store.db.execute("UPDATE generations SET memory_eligible=NULL WHERE id='legacy'")
        # Wall-clock timestamps cannot change immutable batch ordering.
        store.db.execute("UPDATE generations SET created_at='2020-01-01T00:00:00+00:00' "
                         "WHERE id='last'")
    job = store.create_session()["id"]
    store.complete_turn(job, "Generated maintenance", "Derived report")
    inputs = store.continuity_inputs(limit=1)
    assert [row["generation_id"] for row in inputs] == ["first"]
    assert [row["generation_id"] for row in store.continuity_inputs(after=inputs[0]["id"])] == [
        "last"]
    assert [row["generation_id"] for row in store.continuity_inputs(scope="fiction")] == ["fiction"]
    assert len(store.list_messages(session)) == 12


def test_recent_bridge_uses_newest_but_maintenance_does_not_skip(store):
    for number in range(12):
        exchange(store, f"Original {number}", generation=f"g{number}")
    inputs = store.continuity_inputs(limit=8)
    assert [row["generation_id"] for row in inputs] == [f"g{number}" for number in range(8)]
    recent = store.recent_continuity_inputs(limit=3)
    assert [row["generation_id"] for row in recent] == ["g9", "g10", "g11"]
    with pytest.raises(ValueError, match="skip"):
        task_for(store, recent)
    task = task_for(store, inputs)
    state = commit(store, task, [{"op": "no_change", "expected_revision": 0}])
    assert state["revision"] == 0
    assert state["watermark"] == inputs[-1]["id"]
    assert [row["generation_id"] for row in store.continuity_inputs(after=state["watermark"])] == [
        "g8", "g9", "g10", "g11"]


def test_original_message_calendar_timestamp_is_preserved_separately_from_completion(store):
    submitted = "2026-10-14T23:59:58+08:00"
    source = exchange(store, metadata={"user_message_at": submitted})
    assert source["created_at"] == submitted
    assert source["user_created_at"] == submitted
    assert source["completed_at"] == source["assistant_created_at"]
    assert source["completed_at"] != submitted
    from eidolon.memory.continuity import resolve_event_time
    assert resolve_event_time("tomorrow", source["user_created_at"], timezone="Asia/Manila") == (
        "2026-10-15T00:00:00+08:00")


def test_continuity_projections_and_pending_batches_preserve_recorded_identity(store):
    legacy = exchange(store, "Earlier project detail.", generation="legacy")
    identity = {"user_name": "Rowan", "companion_name": "Aster"}
    named = exchange(store, "My interview is tomorrow.", generation="named",
                     metadata={"identity": identity, "user_message_at": "2026-10-18T23:59:00+08:00"})
    assert "identity" not in legacy
    assert named["identity"] == identity
    inputs = [legacy, named]
    assert store.continuity_inputs() == inputs
    assert store.recent_continuity_inputs(limit=2) == inputs
    assert store.continuity_evidence([named["id"], legacy["id"], named["id"]]) == inputs
    assert named["user"] == named["user_content"] == "My interview is tomorrow."
    assert named["user_created_at"] == "2026-10-18T23:59:00+08:00"
    assert named["assistant"] == named["assistant_content"] == "I will ask how it went."
    task = task_for(store, inputs)
    assert task["inputs"] == inputs
    raw_inputs = store.db.execute(
        "SELECT inputs FROM maintenance_tasks WHERE id=?", (task["id"],),
    ).fetchone()["inputs"]
    later_identity = {"user_name": "Sage", "companion_name": "Moss"}
    latest = exchange(store, "A later project detail.", generation="later",
                      metadata={"identity": later_identity})
    # Duplicate completion after a rename cannot change source evidence or batch hashes.
    store.complete_turn(named["session_id"], named["user"], named["assistant"],
                        generation_id="named", metadata={"identity": later_identity})
    reopened = Store(store.path)
    try:
        pending = reopened.pending_maintenance_task()
        assert pending["id"] == task["id"]
        assert pending["input_hash"] == task["input_hash"]
        assert pending["inputs"] == inputs
        assert reopened.db.execute(
            "SELECT inputs FROM maintenance_tasks WHERE id=?", (task["id"],),
        ).fetchone()["inputs"] == raw_inputs
        assert reopened.continuity_evidence([named["id"]]) == [named]
        assert reopened.recent_continuity_inputs(limit=1) == [latest]
        assert task_for(reopened, inputs)["input_hash"] == task["input_hash"]
        state = commit(reopened, pending, [upsert(named, item_id="interview")])
        assert state["watermark"] == named["id"]
        assert reopened.continuity_inputs(after=state["watermark"]) == [latest]
        assert state["items"][0]["sources"][0]["exchange_id"] == named["id"]
    finally:
        reopened.close()


def test_pending_batch_identity_prevents_overlapping_new_work(store):
    exchange(store, "One")
    exchange(store, "Two")
    inputs = store.continuity_inputs()
    first = task_for(store, inputs[:1])
    with pytest.raises(ValueError, match="earlier maintenance"):
        task_for(store, inputs)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    store.fail_maintenance_stage(first["id"], "continuity", "Retry later", future)
    assert store.pending_maintenance_task()["id"] == first["id"]


def test_patch_is_atomic_stable_and_duplicate_commit_is_idempotent(store):
    source = exchange(store)
    task = task_for(store)
    malformed = upsert(source, item_id="bad", content="Unsupported personality inference")
    with pytest.raises(ValueError, match="evidence"):
        commit(store, task, [upsert(source, item_id="interview"), malformed])
    assert store.continuity_state()["items"] == []
    assert store.continuity_state()["watermark"] == 0
    assert store.continuity_history() == []
    state = commit(store, task, [upsert(source)])
    assert state["revision"] == 1
    assert len(state["items"]) == 1
    assert state["items"][0]["sources"][0]["excerpt"] == source["user"]
    assert commit(store, task, [upsert(source)]) == state
    assert len(store.continuity_history()) == 1
    assert task_for(store, [source])["id"] == task["id"]


@pytest.mark.parametrize("change", ["eligibility", "content", "scope"])
def test_inflight_commit_rechecks_source_and_read_invalidates_changed_evidence(store, change):
    source = exchange(store, generation="source")
    task = task_for(store)
    if change == "eligibility":
        query = "UPDATE generations SET memory_eligible=0 WHERE id='source'"
    elif change == "scope":
        query = "UPDATE generations SET metadata='{\"scope\":\"fiction\"}' WHERE id='source'"
    else:
        query = "UPDATE messages SET content='Changed original' WHERE generation_id='source' " \
                "AND role='user'"
    with store.db:
        store.db.execute(query)
    with pytest.raises(ValueError, match="evidence"):
        commit(store, task, [upsert(source)])
    assert store.continuity_state()["watermark"] == 0


def test_source_changes_hide_derived_items_but_preserve_history(store):
    source = exchange(store, generation="source")
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    with store.db:
        store.db.execute("UPDATE generations SET memory_eligible=0 WHERE id='source'")
    assert store.continuity_state()["items"][0]["invalidated"]
    assert store.list_continuity_items(include_inactive=False) == []
    assert store.continuity_history("interview")[0]["data"]["content"] == source["user"]
    edited = store.edit_continuity_item("interview", {"content": "My deliberate note"},
                                        expected_revision=1)
    assert edited["origin"] == "user_note"
    assert not edited["locked"]
    assert not edited.get("user_created")
    assert not edited["invalidated"]


def test_user_edits_and_locks_defeat_stale_maintenance(store):
    source = exchange(store)
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    new_source = exchange(store, "My interview is finished.")
    pending = task_for(store)
    edited = store.edit_continuity_item("interview", {"locked": True}, expected_revision=1)
    assert edited["revision"] == 2
    with pytest.raises(ValueError, match="state changed"):
        commit(store, pending, [upsert(new_source, item_id="interview", expected_revision=1)])
    renewed = task_for(store, pending["inputs"])
    assert renewed["id"] == pending["id"]
    with pytest.raises(ValueError, match="locked"):
        commit(store, renewed, [upsert(new_source, item_id="interview", expected_revision=2)])
    assert store.continuity_state()["watermark"] == source["id"]
    assert store.continuity_state()["items"][0]["content"] == source["user"]


@pytest.mark.parametrize("category", ["situation", "thread", "relationship", "emotion"])
def test_user_created_notes_keep_their_label_and_protection_after_edits_and_restart(store, category):
    from eidolon.memory.continuity import active_items

    note = store.create_continuity_item(
        "My creative project needs a rainy-day sketch 雨.", category,
        topic="Creative project", importance="moderate",
    )
    assert note["user_created"] and note["locked"] and note["origin"] == "user_note"
    assert note["category"] == category and note["state"] == "open"
    assert note["sources"] == [] and not note["invalidated"]
    assert store.continuity_state()["revision"] == 1
    assert store.continuity_state()["control_revision"] == 1
    assert active_items([note], now="2099-01-01T00:00:00+00:00") == [note]
    edited = store.edit_continuity_item(note["id"], {"content": "Revised creative project note.",
                                                   "locked": False}, expected_revision=1)
    assert edited["user_created"] and edited["locked"] and edited["revision"] == 2
    reopened = Store(store.path)
    try:
        assert reopened.get_continuity_item(note["id"]) == edited
    finally:
        reopened.close()


@pytest.mark.parametrize("action", ["upsert", "archive", "resolve"])
def test_commit_cannot_change_user_created_notes_even_without_a_lock(store, action):
    note = store.create_continuity_item(
        "My interview is tomorrow.", "thread",
        topic="Creative project", importance="moderate",
    )
    # The immutable ownership flag must protect even imported/unlocked state.
    with store.db:
        store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                         (json.dumps({**note, "locked": False}), note["id"]))
    source = exchange(store, "My interview is finished.")
    before = store.continuity_state()
    with pytest.raises(ValueError, match="user-created"):
        commit(store, task_for(store), [upsert(source, item_id=note["id"],
                                              expected_revision=1, op=action)])
    assert store.continuity_state() == before


def test_create_edit_and_delete_invalidate_in_flight_maintenance(store):
    exchange(store)
    stale = task_for(store)
    note = store.create_continuity_item(
        "My interview is tomorrow.", "situation",
        topic="Creative project", importance="moderate",
    )
    with pytest.raises(ValueError, match="state changed"):
        commit(store, stale, [{"op": "no_change", "expected_revision": 0, "sources": []}])
    stale = task_for(store)
    edited = store.edit_continuity_item(note["id"], {"category": "thread"}, expected_revision=1)
    with pytest.raises(ValueError, match="state changed"):
        commit(store, stale, [{"op": "no_change", "expected_revision": 1, "sources": []}])
    with pytest.raises(ValueError, match="revision changed"):
        store.delete_continuity_item(note["id"], expected_revision=1)
    assert store.get_continuity_item(note["id"]) == edited
    stale = task_for(store)
    store.delete_continuity_item(note["id"], expected_revision=2)
    assert store.get_continuity_item(note["id"]) is None
    assert store.continuity_state()["items"] == []
    with pytest.raises(ValueError, match="state changed"):
        commit(store, stale, [{"op": "no_change", "expected_revision": 2, "sources": []}])


def test_note_edits_and_deletes_reject_foreign_scope_and_stale_edits(store):
    note = store.create_continuity_item(
        "A fictional creative project.", "thread",
        topic="Creative project", importance="moderate", scope="fiction",
    )
    with pytest.raises(KeyError):
        store.edit_continuity_item(note["id"], {"content": "A real project."}, expected_revision=1)
    with pytest.raises(KeyError):
        store.delete_continuity_item(note["id"], expected_revision=1)
    assert store.get_continuity_item(note["id"], scope="fiction") == note
    note = store.create_continuity_item(
        "A real creative project.", "thread",
        topic="Creative project", importance="moderate",
    )
    edited = store.edit_continuity_item(note["id"], {"category": "situation"}, expected_revision=1)
    with pytest.raises(ValueError, match="revision changed"):
        store.edit_continuity_item(note["id"], {"content": "Stale wording."}, expected_revision=1)
    assert store.get_continuity_item(note["id"]) == edited


def test_editing_automatic_note_labels_keeps_feelings_visible_without_expiring_other_labels(store):
    from eidolon.memory.continuity import active_items

    source = exchange(store)
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    feeling = store.edit_continuity_item(
        "interview", {"content": "I feel ready for my interview.", "category": "emotion"},
        expected_revision=1,
    )
    assert active_items([feeling]) == [feeling]
    expiry = datetime.fromisoformat(feeling["expires_at"])
    assert timedelta(hours=47) < expiry - datetime.now(timezone.utc) <= timedelta(hours=48)
    topic = store.edit_continuity_item("interview", {"category": "thread"}, expected_revision=2)
    assert "expires_at" not in topic
    assert active_items([topic], now="2099-01-01T00:00:00+00:00") == [topic]


def test_terminal_operations_keep_prior_wording_and_evidence(store):
    source = exchange(store)
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    resolved = exchange(store, "My interview is finished.")
    operation = upsert(resolved, item_id="interview", expected_revision=1, op="resolve")
    state = commit(store, task_for(store), [operation])
    item = state["items"][0]
    assert item["state"] == "archived"
    assert item["content"] == source["user"]
    assert len(item["sources"]) == 2
    assert store.list_continuity_items(include_inactive=False) == []
    assert len(store.continuity_history("interview")) == 2


def test_independent_stages_retry_durably_without_repeating_continuity(store):
    source = exchange(store)
    task = task_for(store)
    commit(store, task, [upsert(source, item_id="interview")])
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.fail_maintenance_stage(task["id"], "reflection", "Optional failure", future)
    status = store.maintenance_status()
    assert status["counts"]["completed"] == 1
    assert status["counts"]["failed"] == 1
    assert store.pending_maintenance_task() is None
    assert store.pending_maintenance_task(stage="reflection")["id"] == task["id"]
    state = store.continuity_state()
    assert commit(store, task, [upsert(source, item_id="interview")]) == state
    store.fail_maintenance_stage(task["id"], "continuity", "Cannot undo committed stage")
    assert store.maintenance_status()["counts"]["completed"] == 1
    reopened = Store(store.path)
    try:
        retry = reopened.pending_maintenance_task(stage="reflection")
        assert retry["input_ids"] == [source["id"]]
        assert retry["stages"]["reflection"]["next_retry_at"] == future
        assert reopened.continuity_state() == state
    finally:
        reopened.close()


def test_many_unrelated_updates_and_no_change_keep_locked_item(store):
    source = exchange(store)
    commit(store, task_for(store), [upsert(source, item_id="pinned")])
    pinned = store.edit_continuity_item("pinned", {"locked": True}, expected_revision=1)
    for number in range(100):
        current = exchange(store, f"A passing observation {number}.")
        task = task_for(store)
        operation = ([{"op": "no_change", "expected_revision": task["expected_revision"]}]
                     if number % 2 else [upsert(current, item_id=f"new{number}",
                                               topic=f"Observation {number}")])
        commit(store, task, operation, max_notes=64)
    assert next(item for item in store.continuity_state()["items"]
                if item["id"] == "pinned") == pinned
    assert len(store.continuity_history("pinned")) == 2


def test_followup_requires_eligible_saved_evidence_and_stale_revision_is_noop(store):
    source = exchange(store)
    state = commit(store, task_for(store), [upsert(source, item_id="interview")])
    session = store.session_for(timezone="Asia/Manila")["id"]
    reply = "How did the interview go?"
    saved = store.complete_turn(session, "Hello", reply, generation_id="followup")
    assert store.mark_continuity_followup(
        ["interview"], at=saved["created_at"], expected_revision=state["revision"],
        evidence={"generation_id": "followup", "excerpt": reply},
    ) == 1
    updated = store.continuity_state()
    assert updated["items"][0]["last_followup_at"] == saved["created_at"]
    assert updated["items"][0]["last_followup_evidence"]["excerpt"] == reply
    assert store.mark_continuity_followup(["interview"], expected_revision=state["revision"]) == 0
    store.complete_turn(session, "Off", reply, generation_id="off", retain=False)
    assert store.mark_continuity_followup(
        ["interview"], evidence={"generation_id": "off", "excerpt": reply},
    ) == 0
    assert store.continuity_state() == updated


def test_expiry_is_checked_without_new_writes(store):
    source = exchange(store)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    commit(store, task_for(store), [upsert(source, expires_at=past)])
    assert store.list_continuity_items(include_inactive=False) == []
    assert len(store.continuity_state()["items"]) == 1


def test_direct_storage_calls_reject_clipped_negation_fiction_and_false_resolution(store):
    source = exchange(store, "I am not looking for a new job.")
    task = task_for(store)
    clipped = upsert(source, content="looking for a new job.")
    clipped["sources"][0]["excerpt"] = clipped["content"]
    with pytest.raises(ValueError, match="unsupported"):
        commit(store, task, [clipped])
    commit(store, task, [{"op": "no_change", "expected_revision": 0}])
    fictional = exchange(store, "In our story, my interview is tomorrow.")
    task = task_for(store)
    with pytest.raises(ValueError, match="unsupported"):
        commit(store, task, [upsert(fictional)])
    commit(store, task, [{"op": "no_change", "expected_revision": 0}])
    valid = exchange(store)
    commit(store, task_for(store), [upsert(valid, item_id="interview")])
    unsupported = exchange(store, "The interview is still pending.")
    task = task_for(store)
    with pytest.raises(ValueError, match="positive"):
        commit(store, task, [upsert(unsupported, item_id="interview", expected_revision=1,
                                   op="resolve")])
    assert store.list_continuity_items(include_inactive=False)[0]["state"] == "open"


def test_emotion_defaults_to_dated_expiry_and_usage_stays_numeric(store):
    source = exchange(store, "I feel anxious about the interview.")
    task = task_for(store)
    store.record_maintenance_usage(task["id"], called=True)
    store.record_maintenance_usage(task["id"], usage={"prompt_tokens": 200, "completion_tokens": 40})
    with pytest.raises(ValueError, match="numeric"):
        store.record_maintenance_usage(task["id"], usage={"private_content": "not a count"})
    state = commit(store, task, [upsert(source, category="emotion")])
    item = state["items"][0]
    assert datetime.fromisoformat(item["expires_at"]) - datetime.fromisoformat(
        item["observed_at"]) == timedelta(hours=48)
    status = store.maintenance_status()
    assert status["model_calls"] == 1
    stage = next(stage for stage in status["stages"] if stage["stage"] == "continuity")
    assert stage["usage"] == {"prompt_tokens": 200, "completion_tokens": 40}


def test_current_support_stays_bounded_across_hundred_full_evidence_updates(store):
    source = exchange(store, "My interview preparation project remains unfinished.")
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    original_history = store.continuity_history("interview")[0]
    for number in range(100):
        content = (f"My interview preparation project revision {number} remains unfinished: "
                   + "steady progress detail " * 90 + ".")
        current = exchange(store, content)
        item = store.get_continuity_item("interview")
        commit(store, task_for(store), [upsert(current, item_id="interview",
                                               expected_revision=item["revision"])])
        assert len(store.get_continuity_item("interview")["sources"]) == 1
    item = store.get_continuity_item("interview")
    assert item["sources"][0]["excerpt"] == content
    history = store.continuity_history("interview")
    assert len(history) == 100
    assert all(len(entry["data"]["sources"]) == 1 for entry in history)
    # The oldest revision remains inspectable even outside the default list limit.
    assert store.get_continuity_history_entry(original_history["id"]) == original_history
    assert original_history["data"]["sources"][0]["excerpt"] == source["user"]
    import json
    assert len(json.dumps(store.continuity_state()).encode()) < 20_000
    assert len(json.dumps(history).encode()) < 1_000_000


def test_upsert_clears_superseded_temporal_claims_but_keeps_actual_followup(store):
    source = exchange(store, "I will ask about the interview tomorrow.")
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    state = commit(store, task_for(store), [upsert(
        source, item_id="interview", event_at=when, follow_up_at=when,
        expires_at=when, promise_owner="user", promise_kind="conversation",
    )])
    original = store.continuity_history("interview")[0]
    store.mark_continuity_followup(["interview"], expected_revision=state["revision"])
    followed = store.get_continuity_item("interview")["last_followup_at"]
    fresh = exchange(store, "My interview preparations remain unfinished.")
    item = store.get_continuity_item("interview")
    state = commit(store, task_for(store), [upsert(fresh, item_id="interview",
                                                 expected_revision=item["revision"])])
    item = state["items"][0]
    assert item["last_followup_at"] == followed
    assert not {"event_at", "follow_up_at", "expires_at", "promise_owner",
                "promise_kind"}.intersection(item)
    assert item["sources"][0]["exchange_id"] == fresh["id"]
    assert store.get_continuity_history(original["id"])["data"]["event_at"] == when
    assert store.get_continuity_item("interview", scope="fiction") is None
    assert store.get_continuity_history(original["id"], scope="fiction") is None


@pytest.mark.parametrize("changes", [
    {"content": "My reviewed interview note.", "locked": False},
    {"locked": False, "content": "My reviewed interview note."},
])
def test_explicit_unlock_overrides_content_edit_default_regardless_of_dictionary_order(store, changes):
    source = exchange(store)
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    item = store.edit_continuity_item("interview", changes, expected_revision=1)
    assert item["origin"] == "user_note"
    assert not item["locked"]


def test_repeated_terminal_updates_keep_claim_and_latest_transition_with_older_history(store):
    source = exchange(store)
    commit(store, task_for(store), [upsert(source, item_id="interview")])
    for number in range(12):
        fresh = exchange(store, f"My interview is finished. Confirmation {number}.")
        item = store.get_continuity_item("interview")
        commit(store, task_for(store), [upsert(fresh, item_id="interview",
                                               expected_revision=item["revision"], op="resolve")])
        item = store.get_continuity_item("interview")
        assert len(item["sources"]) == 2
        assert item["sources"][0]["exchange_id"] == source["id"]
        assert item["sources"][1]["exchange_id"] == fresh["id"]
    assert len(store.continuity_history("interview")) == 13


def test_old_unresolved_run_stays_discoverable_beyond_recent_history(store):
    source = exchange(store)
    task = task_for(store, [source])
    commit(store, task, [{"op": "no_change", "expected_revision": 0}])
    store.fail_maintenance_stage(task["id"], "reflection", "PRIVATE legacy diagnostic",
                                 reason_code="unavailable")
    for index in range(101):
        source = exchange(store, text=f"Synthetic later project {index}.")
        later = task_for(store, [source])
        commit(store, later, [{"op": "no_change", "expected_revision": 0}])
    status = store.maintenance_status()
    assert len(status["stages"]) == 300
    assert status["unresolved"]["total"] == 1
    older = next(stage for stage in status["stages"]
                 if stage["task_id"] == task["id"] and stage["stage"] == "continuity")
    assert older["status"] == "completed" and older["unresolved_count"] == 1
    assert status["stages_omitted"] == 6


def test_v11_migration_preserves_failed_gap_below_watermark_without_inventing_reason(tmp_path):
    path = tmp_path / "upgrade.db"
    store = Store(path)
    old = exchange(store, "My earlier project needs attention.")
    failed = task_for(store, [old])
    store.fail_maintenance_stage(failed["id"], "continuity", "Legacy diagnostic")
    newer = exchange(store, "A later exchange.")
    # A legacy watermark alone cannot negate the saved failed originals.
    with store.db:
        store.db.execute("UPDATE continuity_scopes SET watermark=?", (newer["id"],))
        store.db.execute("UPDATE maintenance_stages SET reason=NULL,model_calls=6 "
                         "WHERE task_id=? AND stage='continuity'", (failed["id"],))
        store.db.execute("DROP TABLE continuity_evidence")
        store.db.execute("ALTER TABLE maintenance_stages DROP COLUMN set_aside")
        store.db.execute("PRAGMA user_version=11")
    store.close()
    for _ in range(2):
        store = Store(path)
        try:
            assert store.maintenance_status()["evidence"] == {"processed": 1, "unresolved": 1}
            assert store.pending_maintenance_task()["inputs"] == failed["inputs"]
            stage = store.get_maintenance_stage(failed["id"])
            assert stage["reason"] is None and stage["set_aside"]
            assert store.maintenance_inputs() == []
        finally:
            store.close()
    with closing(Store(path)) as store:
        store.prepare_maintenance_recovery(failed["id"], call_limit=2,
                                           fingerprint="synthetic", retry=True)
        resumed = store.create_maintenance_task(
            failed["inputs"], expected_revision=0, control_revision=0,
            resume_task_id=failed["id"],
        )
        operations = [{"op": "no_change", "expected_revision": 0, "sources": []}]
        commit(store, resumed, operations)
        commit(store, resumed, operations)  # Duplicate delivery is harmless.
        assert store.maintenance_status()["evidence"] == {"processed": 2, "unresolved": 0}
        assert store.continuity_state()["watermark"] == newer["id"]
    with closing(Store(path)) as reopened:
        assert reopened.maintenance_status()["evidence"] == {"processed": 2, "unresolved": 0}
        assert reopened.pending_maintenance_task() is None
