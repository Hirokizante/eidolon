import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from eidolon.storage import Store, daily_key


def test_upgrade_removes_output_paths_and_preserves_runs_results_and_files(tmp_path):
    path = tmp_path / "room.db"
    existing = tmp_path / "kept.md"
    existing.write_text("Existing output remains private.")
    store = Store(path)
    store.save_job("daily", {"id": "daily"})
    stamp = "2026-10-10T14:00:00+00:00"
    store.record_job_plan("daily", [
        {"id": "saved", "scheduled_at": stamp, "status": "completed"},
        {"id": "interrupted", "scheduled_at": "2026-10-11T14:00:00+00:00",
         "status": "running"},
    ], stamp)
    store.update_job_run("saved", result="Saved result")
    store.update_job_run("interrupted", result="Recoverable result")
    with store.db:
        store.db.execute("ALTER TABLE job_runs ADD COLUMN note_path TEXT NOT NULL DEFAULT ''")
        store.db.execute("UPDATE job_runs SET note_path=?", (str(existing),))
        store.db.execute("PRAGMA user_version=3")
    store.close()

    for _ in range(2):
        upgraded = Store(path)
        try:
            runs = {run["id"]: run for run in upgraded.list_job_runs()}
            assert runs["saved"]["status"] == "completed"
            assert runs["saved"]["result"] == "Saved result"
            assert runs["interrupted"]["result"] == "Recoverable result"
            assert all("note_path" not in run for run in runs.values())
            assert upgraded.job_cursor("daily") == stamp
            assert not upgraded.db.execute("PRAGMA foreign_key_check").fetchall()
            assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 14
            assert existing.read_text() == "Existing output remains private."
        finally:
            upgraded.close()


def test_daily_reset_and_same_day_restart(tmp_path):
    assert daily_key(datetime.fromisoformat("2026-10-10T03:59:59+08:00"), timezone="Asia/Manila") == "2026-10-09"
    assert daily_key(datetime.fromisoformat("2026-10-10T04:00:00+08:00"), timezone="Asia/Manila") == "2026-10-10"
    assert daily_key(datetime.fromisoformat("2026-10-09T20:00:00+00:00"), timezone="Asia/Manila") == "2026-10-10"
    path = tmp_path / "room.db"
    store = Store(path)
    old = store.session_for(datetime.fromisoformat("2026-10-10T03:59:00+08:00"), timezone="Asia/Manila")
    today = store.session_for(datetime.fromisoformat("2026-10-10T04:00:00+08:00"), timezone="Asia/Manila")
    store.close()
    store = Store(path)
    assert store.session_for(datetime.fromisoformat("2026-10-10T20:00:00+08:00"), timezone="Asia/Manila")["id"] == today["id"]
    assert len(store.list_sessions()) == 2
    assert store.get_session(old["id"])
    with pytest.raises(ValueError):
        daily_key(datetime(2026, 10, 10), timezone="Asia/Manila")
    store.close()


def test_completed_exchange_outbox_is_atomic_and_replay_safe(tmp_path):
    store = Store(tmp_path / "room.db")
    session = store.session_for(timezone="Asia/Manila")["id"]
    store.db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'disk test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_turn(session, "Hi", "Hello", generation_id="one")
    assert store.list_messages(session) == []
    store.db.execute("DROP TRIGGER fail_outbox")
    store.complete_turn(session, "Hi", "Hello", generation_id="one", metadata={"prompt_hash": "abc"})
    store.complete_turn(session, "Hi", "Hello", generation_id="one")
    assert len(store.list_messages(session)) == 2
    entries = store.pending_outbox()
    assert len(entries) == 1
    assert entries[0]["document_id"] == "eidolon-room:exchange:one"
    assert "prompt_hash" not in entries[0]["payload"]
    assert "You (" in entries[0]["payload"]["content"]
    assert "Companion (" in entries[0]["payload"]["content"]
    with pytest.raises(ValueError):
        store.complete_turn(session, "Different", "Hello", generation_id="one")
    store.close()
    recovered = Store(tmp_path / "room.db")
    assert recovered.pending_outbox()[0]["operation_id"] == entries[0]["operation_id"]
    recovered.close()


def test_messages_keep_send_and_completion_times_including_older_exchanges(tmp_path, monkeypatch):
    path = tmp_path / "room.db"
    user_at = "2026-10-18T23:58:12.123456+08:00"
    reply_at = "2026-10-19T00:01:09.654321+08:00"
    monkeypatch.setattr("eidolon.storage.store.utc_now", lambda: reply_at)
    store = Store(path)
    session = store.session_for(timezone="Asia/Manila")["id"]
    try:
        store.complete_turn(session, "Question", "Answer", generation_id="dated",
                            metadata={"user_message_at": user_at})
        messages = store.list_messages(session)
        assert [m["created_at"] for m in messages] == [user_at, reply_at]
        rows = store.db.execute("SELECT created_at FROM messages ORDER BY id").fetchall()
        assert [row["created_at"] for row in rows] == [user_at, reply_at]
        assert store.pending_outbox()[0]["payload"]["content"] == (
            f"You ({user_at}): Question\n\nCompanion ({reply_at}): Answer"
        )
        # Reproduce the older storage format: both rows used the completion time.
        with store.db:
            store.db.execute("UPDATE messages SET created_at=?", (reply_at,))
    finally:
        store.close()
    restarted = Store(path)
    try:
        messages = restarted.list_messages(session)
        assert [m["created_at"] for m in messages] == [user_at, reply_at]
        assert [m["content"] for m in messages] == ["Question", "Answer"]
        restarted.complete_turn(session, "Older question", "Older answer")
        assert [m["created_at"] for m in restarted.list_messages(session)[-2:]] == [reply_at] * 2
    finally:
        restarted.close()


@pytest.mark.parametrize("kind", ["chat", "job"])
@pytest.mark.parametrize("identity", [
    {"user_name": "Rowan", "companion_name": "Aster"},
    {"user_name": "陽 [bold]", "companion_name": "Lune ${name} 👩‍🚀"},
    {"user_name": "Echo", "companion_name": "Echo"},
    {"user_name": "U" * 80, "companion_name": "C" * 80},
])
def test_exchange_payload_and_message_projections_use_recorded_identity(
    tmp_path, monkeypatch, kind, identity,
):
    user_at = "2026-10-18T23:58:12.123456+08:00"
    reply_at = "2026-10-19T00:01:09.654321+08:00"
    monkeypatch.setattr("eidolon.storage.store.utc_now", lambda: reply_at)
    store = Store(tmp_path / "room.db")
    try:
        session = store.create_session("named-session", kind=kind)["id"]
        metadata = {"identity": identity, "user_message_at": user_at,
                    "prompt_hashes": {"SYSTEM": "synthetic-hash"}}
        turn = store.complete_turn(session, "Keep [literal] content.", "Original answer.",
                                   generation_id="named", metadata=metadata)
        saved = store.db.execute("SELECT metadata FROM generations WHERE id='named'").fetchone()
        assert json.loads(saved["metadata"]) == metadata
        payload = store.pending_outbox()[0]["payload"]
        assert payload["content"] == (
            f"{identity['user_name']} ({user_at}): Keep [literal] content.\n\n"
            f"{identity['companion_name']} ({reply_at}): Original answer."
        )
        assert payload["timestamp"] == reply_at
        assert payload["document_id"] == turn["document_id"]
        assert payload["metadata"] == {"source": "eidolon-room", "session_id": session}
        for messages in (store.list_messages(session), store.memory_query_history(session)):
            assert [message["identity"] for message in messages] == [identity, identity]
            assert [message["role"] for message in messages] == ["user", "assistant"]
            assert [message["content"] for message in messages] == [
                "Keep [literal] content.", "Original answer.",
            ]
    finally:
        store.close()


def test_legacy_identity_absence_does_not_rewrite_metadata_messages_or_outbox(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    session = store.session_for(timezone="Asia/Manila")["id"]
    store.complete_turn(session, "Older question.", "Older answer.", generation_id="older",
                        metadata={"prompt_hashes": {"SYSTEM": "older-hash"}})
    # A pre-feature payload is private saved content, regardless of current defaults.
    with store.db:
        payload = store.pending_outbox()[0]["payload"]
        payload["content"] = "Prior user: Older question.\n\nPrior companion: Older answer."
        store.db.execute("UPDATE outbox SET payload=?", (json.dumps(payload),))
    original = {table: [dict(row) for row in store.db.execute(f"SELECT * FROM {table}")]
                for table in ("generations", "messages", "outbox")}
    store.close()
    restarted = Store(path)
    try:
        for messages in (restarted.list_messages(session), restarted.memory_query_history(session)):
            assert all("identity" not in message for message in messages)
            assert [message["content"] for message in messages] == ["Older question.", "Older answer."]
        assert restarted.pending_outbox()[0]["payload"] == payload
        for table, rows in original.items():
            assert [dict(row) for row in restarted.db.execute(f"SELECT * FROM {table}")] == rows
    finally:
        restarted.close()


def test_identity_snapshot_uses_shared_validation_without_mutating_supplied_metadata(tmp_path):
    store = Store(tmp_path / "room.db")
    try:
        session = store.session_for(timezone="Asia/Manila")["id"]
        metadata = {"identity": {"user_name": "  Rowan  ", "companion_name": "  Aster  "},
                    "prompt_hash": "unchanged"}
        original = json.dumps(metadata)
        store.complete_turn(session, "Question.", "Answer.", metadata=metadata)
        expected = {"user_name": "Rowan", "companion_name": "Aster"}
        assert json.dumps(metadata) == original
        assert json.loads(store.db.execute("SELECT metadata FROM generations").fetchone()[0]) == {
            "identity": expected, "prompt_hash": "unchanged",
        }
        assert all(message["identity"] == expected for message in store.list_messages(session))
        assert store.pending_outbox()[0]["payload"]["content"].startswith("Rowan (")
    finally:
        store.close()


@pytest.mark.parametrize("identity", [None, {"user_name": "Invalid\nname"}])
def test_invalid_identity_snapshot_cannot_commit_a_partial_exchange(tmp_path, identity):
    store = Store(tmp_path / "room.db")
    try:
        session = store.session_for(timezone="Asia/Manila")["id"]
        with pytest.raises(ValueError):
            store.complete_turn(session, "Question.", "Answer.", generation_id="invalid",
                                metadata={"identity": identity})
        assert store.db.execute("SELECT count(*) FROM generations").fetchone()[0] == 0
        assert store.list_messages(session) == []
        assert store.pending_outbox() == []
    finally:
        store.close()


@pytest.mark.parametrize("retain", [True, False])
def test_duplicate_completion_preserves_original_identity_metadata_and_eligibility(tmp_path, retain):
    store = Store(tmp_path / "room.db")
    try:
        session = store.session_for(timezone="Asia/Manila")["id"]
        metadata = {"identity": {"user_name": "Rowan", "companion_name": "Aster"},
                    "user_message_at": "2026-10-18T23:58:12+08:00", "prompt_hash": "original"}
        store.complete_turn(session, "Question.", "Answer.", generation_id="stable",
                            metadata=metadata, retain=retain)
        original = dict(store.db.execute("SELECT * FROM generations").fetchone())
        messages = store.list_messages(session)
        outbox = store.pending_outbox()
        # A later identity snapshot cannot relabel a previously committed generation.
        metadata["identity"] = {"user_name": "Sage", "companion_name": "Moss"}
        metadata["prompt_hash"] = "changed"
        for replacement in (metadata, None, {"identity": None}):
            replay = store.complete_turn(session, "Question.", "Answer.", generation_id="stable",
                                         metadata=replacement, retain=not retain)
            assert replay == original
            assert store.list_messages(session) == messages
            assert store.pending_outbox() == outbox
        assert len(store.memory_query_history(session)) == (2 if retain else 0)
        with pytest.raises(ValueError, match="different exchange"):
            store.complete_turn(session, "Different question.", "Answer.", generation_id="stable",
                                metadata=metadata)
    finally:
        store.close()


def test_outbox_retry_after_rename_and_restart_preserves_exact_payload_and_ids(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    session = store.session_for(timezone="Asia/Manila")["id"]
    first_identity = {"user_name": "Rowan", "companion_name": "Aster"}
    first = store.complete_turn(session, "First question.", "First answer.", generation_id="first",
                                metadata={"identity": first_identity})
    original = dict(store.db.execute("SELECT * FROM outbox").fetchone())
    store.update_outbox(first["document_id"], status="failed", attempted=True, error="Retry later")
    store.close()
    restarted = Store(path)
    try:
        later_identity = {"user_name": "Sage", "companion_name": "Moss"}
        restarted.complete_turn(session, "Later question.", "Later answer.", generation_id="later",
                                metadata={"identity": later_identity})
        restarted.complete_turn(session, "First question.", "First answer.", generation_id="first",
                                metadata={"identity": later_identity})
        for status in ("submitted", "failed", "queued"):
            restarted.update_outbox(first["document_id"], status=status, attempted=True)
            replay = next(row for row in restarted.pending_outbox(ready_only=True)
                          if row["generation_id"] == "first")
            assert replay["payload"]["content"].startswith("Rowan (")
            saved = dict(restarted.db.execute(
                "SELECT * FROM outbox WHERE document_id=?", (first["document_id"],),
            ).fetchone())
            for field in ("generation_id", "document_id", "operation_id", "payload"):
                assert saved[field] == original[field]
        assert len(restarted.pending_outbox()) == 2
        assert [message["identity"] for message in restarted.list_messages(session)] == [
            first_identity, first_identity, later_identity, later_identity,
        ]
    finally:
        restarted.close()


def test_summary_preserves_full_transcript_and_job_context(tmp_path):
    store = Store(tmp_path / "room.db")
    chat = store.session_for(timezone="Asia/Manila")["id"]
    job = store.create_session("job:1")["id"]
    store.complete_turn(chat, "user original", "assistant original")
    store.complete_turn(job, "background", "note")
    store.set_summary(chat, "Short summary")
    assert store.get_summary(chat) == "Short summary"
    assert [m["content"] for m in store.list_messages(chat)] == ["user original", "assistant original"]
    assert len(store.list_messages(job)) == 2
    store.close()


def test_request_schema_upgrade_preserves_history_and_exact_body_after_restart(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    session = store.session_for(timezone="Asia/Manila")["id"]
    store.complete_turn(session, "Original question", "Original answer")
    store.db.execute("DROP TABLE model_requests")
    store.db.execute("PRAGMA user_version=1")
    store.db.commit()
    store.close()
    upgraded = Store(path)
    raw = '{"model":"test", "messages":[{"role":"user","content":"🌸"}]}'
    capture = upgraded.record_model_request(
        session, generation_id="failed-turn", attempt_id="attempt1", round_index=0,
        provider="test", model="test", user_message="🌸", method="POST",
        endpoint="http://localhost/v1/chat/completions", body=raw,
    )
    assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 14
    assert "body" not in upgraded.list_model_requests(session)[0]
    assert [message["content"] for message in upgraded.list_messages(session)] == [
        "Original question", "Original answer"]
    upgraded.close()
    restarted = Store(path)
    assert restarted.get_model_request(capture["id"])["body"] == raw
    assert restarted.list_model_requests(session)[0]["generation_id"] == "failed-turn"
    restarted.close()


def test_outbox_retry_migration_preserves_payload_and_identity(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    store.complete_turn(store.session_for(timezone="Asia/Manila")["id"], "Hi", "Hello", generation_id="legacy")
    original = store.pending_outbox()[0]
    store.db.execute("ALTER TABLE outbox DROP COLUMN next_retry_at")
    store.db.execute("PRAGMA user_version=4")
    store.db.commit()
    store.close()

    for _ in range(2):
        store = Store(path)
        upgraded = store.pending_outbox()[0]
        assert upgraded == {**original, "next_retry_at": None}
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 14
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
        store.close()


def test_outbox_retry_filter_preserves_diagnostics_and_filters_before_limit(tmp_path):
    store = Store(tmp_path / "room.db")
    session = store.session_for(timezone="Asia/Manila")["id"]
    now = datetime.now(timezone.utc)
    for generation in ["delayed", "ready"]:
        store.complete_turn(session, generation, "Hello", generation_id=generation)
    delayed, ready = store.pending_outbox()
    store.update_outbox(delayed["document_id"], status="failed", attempted=True,
                        next_retry_at=(now + timedelta(seconds=60)).isoformat())
    store.db.execute("UPDATE outbox SET updated_at='2026-01-01T00:00:00+00:00' WHERE document_id=?",
                     (delayed["document_id"],))
    store.db.commit()
    assert len(store.pending_outbox()) == 2
    assert store.outbox_status()["pending"] == 2
    assert store.pending_outbox(1, ready_only=True, now=now)[0]["document_id"] == ready["document_id"]
    assert len(store.pending_outbox(ready_only=True, now=now + timedelta(seconds=60))) == 2
    store.update_outbox(delayed["document_id"], status="completed")
    completed = store.db.execute("SELECT next_retry_at FROM outbox WHERE document_id=?",
                                 (delayed["document_id"],)).fetchone()
    assert completed["next_retry_at"] is None
    with pytest.raises(ValueError, match="timezone"):
        store.pending_outbox(ready_only=True, now=datetime(2026, 10, 14))
    with pytest.raises(ValueError, match="timezone"):
        store.update_outbox(ready["document_id"], status="failed", next_retry_at="2026-10-14")
    store.close()


def test_latest_request_replaces_prior_capture_across_sessions_atomically(tmp_path):
    store = Store(tmp_path / "room.db")
    first_session = store.create_session("first")["id"]
    second_session = store.create_session("second")["id"]
    assert store.latest_model_request() is None

    def capture(session, attempt):
        return store.record_model_request(
            session, generation_id="failed-turn", attempt_id=attempt, round_index=0,
            provider="test", model="test", user_message=attempt, method="POST",
            endpoint="http://localhost/v1/chat/completions", body='{"message":"' + attempt + '"}',
        )

    first = capture(first_session, "first")
    store.db.execute(
        "CREATE TRIGGER fail_request BEFORE INSERT ON model_requests "
        "BEGIN SELECT RAISE(ABORT, 'disk test'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        capture(second_session, "second")
    assert store.latest_model_request() == first
    store.db.execute("DROP TRIGGER fail_request")
    second = capture(second_session, "second")
    assert second["id"] > first["id"]
    assert store.latest_model_request() == second
    assert store.list_model_requests(first_session) == []
    assert len(store.list_model_requests(second_session)) == 1
    with pytest.raises(KeyError):
        store.get_model_request(first["id"])
    store.close()


def test_request_upgrade_discards_older_captures_and_preserves_latest(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    session = store.create_session("first")["id"]
    first = store.record_model_request(
        session, generation_id="failed-turn", attempt_id="first", round_index=0,
        provider="test", model="test", user_message="First", method="POST",
        endpoint="http://localhost/v1/chat/completions", body='{"message":"first"}',
    )
    # Simulate a database created by the version which retained every capture.
    store.db.execute(
        "INSERT INTO model_requests SELECT id+1,session_id,generation_id,'second',round_index,"
        "provider,model,user_message,method,endpoint,'{\"message\":\"latest\"}',created_at "
        "FROM model_requests"
    )
    store.db.execute("PRAGMA user_version=2")
    store.db.commit()
    store.close()
    upgraded = Store(path)
    assert upgraded.latest_model_request()["body"] == '{"message":"latest"}'
    assert upgraded.db.execute("SELECT COUNT(*) FROM model_requests").fetchone()[0] == 1
    with pytest.raises(KeyError):
        upgraded.get_model_request(first["id"])
    upgraded.close()


def test_memory_eligibility_is_atomic_durable_and_never_upgraded_on_retry(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    session = store.session_for(timezone="Asia/Manila")["id"]
    store.complete_turn(session, "Private turn", "Local answer", generation_id="off", retain=False)
    store.complete_turn(session, "Private turn", "Local answer", generation_id="off", retain=True)
    assert store.pending_outbox() == []
    assert store.db.execute(
        "SELECT memory_eligible FROM generations WHERE id='off'"
    ).fetchone()[0] == 0
    store.complete_turn(session, "Accepted", "Answer", generation_id="on", retain=True)
    store.complete_turn(session, "Accepted", "Answer", generation_id="on", retain=False)
    accepted = store.pending_outbox()[0]
    store.close()
    store = Store(path)
    assert store.pending_outbox(ready_only=True) == [accepted]
    assert len(store.list_messages(session)) == 4
    assert store.db.execute(
        "SELECT memory_eligible FROM generations WHERE id='on'"
    ).fetchone()[0] == 1
    store.close()


def test_memory_query_history_excludes_opted_out_turns_but_keeps_local_transcript(tmp_path):
    store = Store(tmp_path / "room.db")
    session = store.session_for(timezone="Asia/Manila")["id"]
    other = store.create_session("other")["id"]
    for generation, eligible in [("legacy", True), ("off", False), ("on", True)]:
        store.complete_turn(session, generation, "Answer", generation_id=generation, retain=eligible)
    with store.db:
        store.db.execute("UPDATE generations SET memory_eligible=NULL WHERE id='legacy'")
    store.complete_turn(other, "Other room", "Answer")
    assert [row["generation_id"] for row in store.memory_query_history(session)] == [
        "legacy", "legacy", "on", "on",
    ]
    assert len(store.list_messages(session)) == 6
    store.close()


def _legacy_memory_database(path):
    store = Store(path)
    session = store.session_for(timezone="Asia/Manila")["id"]
    for generation in ["queued", "submitted", "failed", "completed"]:
        turn = store.complete_turn(session, generation, "Answer", generation_id=generation)
        store.update_outbox(turn["document_id"], status=generation if generation != "queued"
                            else "queued")
    originals = store.pending_outbox()
    with store.db:
        store.db.execute("ALTER TABLE generations DROP COLUMN memory_eligible")
        store.db.execute("ALTER TABLE outbox DROP COLUMN review_state")
        store.db.execute("PRAGMA user_version=5")
    store.close()
    return session, originals


def test_memory_migration_backs_up_and_holds_ambiguous_work_until_review(tmp_path):
    path = tmp_path / "room.db"
    session, originals = _legacy_memory_database(path)
    store = Store(path)
    backup_path = store.migration_backup_path
    assert backup_path and backup_path.is_file()
    assert backup_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert len(backup.execute("SELECT * FROM messages").fetchall()) == 8
        assert "memory_eligible" not in {
            column[1] for column in backup.execute("PRAGMA table_info(generations)")
        }
    assert len(store.list_messages(session)) == 8
    assert store.pending_outbox(ready_only=True) == []
    assert store.outbox_status()["held"] == 3
    assert store.outbox_status()["review_polling"] == 2
    assert all(row["memory_eligible"] is None for row in store.db.execute(
        "SELECT memory_eligible FROM generations"
    ))
    for before, after in zip(originals, store.pending_outbox(), strict=True):
        assert before["payload"] == after["payload"]
        assert before["operation_id"] == after["operation_id"]
        assert after["review_state"] == "pending"
    # An explicit choice cannot authorize uncertain operations before polling.
    assert store.review_legacy_memory(approve=True) == 1
    assert [row["generation_id"] for row in store.pending_outbox(ready_only=True)] == ["queued"]
    assert store.outbox_status()["eligible"] == 1
    assert store.outbox_status()["held"] == 2
    store.set_memory_delivery_paused(True)
    store.close()
    store = Store(path)
    assert store.migration_backup_path is None
    assert store.memory_delivery_paused()
    assert store.outbox_status()["delivery_paused"]
    assert store.outbox_status()["held"] == 2
    store.set_memory_delivery_paused(False)
    assert not store.memory_delivery_paused()
    store.close()


def test_legacy_review_polling_respects_deadline_and_rejection_is_final(tmp_path):
    path = tmp_path / "room.db"
    _legacy_memory_database(path)
    store = Store(path)
    now = datetime.now(timezone.utc)
    submitted = "eidolon-room:exchange:submitted"
    store.update_outbox(submitted, status="submitted",
                        next_retry_at=(now + timedelta(seconds=30)).isoformat())
    assert [row["generation_id"] for row in store.pending_review_operations(now=now)] == ["failed"]
    assert len(store.pending_review_operations(now=now + timedelta(seconds=31))) == 2
    assert store.review_legacy_memory(approve=False) == 1
    assert store.outbox_status()["rejected"] == 1
    assert store.review_legacy_memory(approve=True) == 0
    assert store.pending_outbox(ready_only=True) == []
    # Once polling establishes that the operation is absent, review may proceed.
    store.update_outbox(submitted, status="queued")
    assert store.review_legacy_memory(approve=True) == 1
    assert [row["generation_id"] for row in store.pending_outbox(ready_only=True)] == ["submitted"]
    with pytest.raises(ValueError, match="timezone"):
        store.pending_review_operations(now=datetime(2026, 10, 14))
    store.close()


def test_legacy_review_is_scoped_to_sources_shown_and_filters_before_limit(tmp_path):
    path = tmp_path / "room.db"
    session, _ = _legacy_memory_database(path)
    store = Store(path)
    store.complete_turn(session, "Accepted later", "Answer", generation_id="new")
    store.db.execute("UPDATE outbox SET updated_at='2000-01-01' WHERE generation_id='new'")
    store.db.commit()
    assert len(store.list_pending_legacy(limit=1)) == 1
    assert store.list_pending_legacy(limit=1)[0]["review_state"] == "pending"
    assert store.review_legacy_memory(approve=True, document_ids=[]) == 0
    assert store.review_legacy_memory(approve=True, document_ids=["unknown"]) == 0
    assert store.outbox_status()["held"] == 3
    assert store.review_legacy_memory(
        approve=False, document_ids=["eidolon-room:exchange:queued"]
    ) == 1
    assert store.outbox_status()["held"] == 2
    assert store.outbox_status()["rejected"] == 1
    assert {row["generation_id"] for row in store.list_pending_legacy()} == {"submitted", "failed"}
    assert [row["generation_id"] for row in store.pending_outbox(ready_only=True)] == ["new"]
    store.close()


def test_memory_manifests_bound_private_attempt_evidence_and_survive_restart(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    first = store.create_session("first")["id"]
    second = store.create_session("second")["id"]
    manifest = {"status": "supplied", "records": [{"id": "memory:1", "text": "🌸"}]}
    oldest = store.record_memory_manifest(first, "failed", "attempt:0", manifest)
    assert oldest["manifest"] == manifest
    updated = store.record_memory_manifest(first, "failed", "attempt:0", {"status": "failed"})
    assert updated["id"] == oldest["id"]
    assert len(store.list_memory_manifests(first)) == 1
    for index in range(1, 101):
        store.record_memory_manifest(second, "cancelled", f"attempt:{index}", manifest)
    assert len(store.list_memory_manifests(limit=1000)) == 100
    assert store.list_memory_manifests(first) == []
    with pytest.raises(KeyError):
        store.get_memory_manifest(oldest["id"])
    latest = store.list_memory_manifests()[0]
    assert store.list_messages(first) == [] and store.pending_outbox() == []
    store.close()
    store = Store(path)
    assert store.get_memory_manifest(latest["id"]) == latest
    assert len(store.list_memory_manifests(second)) == 100
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    store.close()
