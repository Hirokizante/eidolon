"""Prior-version migration and commit defenses outside the refresh seam."""
from contextlib import closing
import sqlite3

import pytest

from eidolon.storage import Store
from eidolon.storage.support import associations
from test_continuity_storage import exchange, task_for, upsert, commit


def prior_database(path):
    with closing(Store(path)) as store:
        first = exchange(store)
        commit(store, task_for(store), [upsert(first, item_id="interview")])
        second = exchange(store, "My interview now includes a practice session.")
        commit(store, task_for(store), [upsert(second, item_id="interview", expected_revision=1)])
        exchange(store, "My interview has another preparation step.")
        task = task_for(store)
        store.prepare_maintenance_recovery(task["id"], call_limit=3, fingerprint="synthetic")
        store.record_maintenance_usage(task["id"], called=True)
        before = store.continuity_state(), store.continuity_history(), store.get_maintenance_task(task["id"])
        with store.db:
            store.db.execute("DROP TABLE continuity_support")
            store.db.execute("PRAGMA user_version=15")
    return before


def test_v15_backfill_preserves_history_pending_work_accounting_and_backup(tmp_path):
    path = tmp_path / "room.db"
    state, history, task = prior_database(path)
    with closing(Store(path)) as store:
        assert store.continuity_state() == state
        assert store.continuity_history() == history
        assert store.get_maintenance_task(task["id"]) == task
        evidence = associations(store, "interview", "real_life")
        assert [entry["disposition"] for entry in evidence] == ["background", "current"]
        assert {entry["revision"] for entry in evidence} == {2}
        assert all(entry["quality"] == "unknown" and entry["eligible"] for entry in evidence)
        assert store.migration_backup_path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(store.migration_backup_path) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 15
            assert backup.execute("SELECT count(*) FROM continuity_history").fetchone()[0] == 2
            assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='continuity_support'").fetchone()
    with closing(Store(path)) as restarted:
        assert associations(restarted, "interview", "real_life") == evidence
        assert restarted.migration_backup_path is None
        assert restarted.get_maintenance_task(task["id"]) == task


def test_v15_backfill_rolls_back_schema_and_rows_on_failure(tmp_path, monkeypatch):
    from eidolon.storage import support

    path = tmp_path / "room.db"
    state, history, task = prior_database(path)
    original = support.record
    calls = 0

    def fail(store, item, **kwargs):
        nonlocal calls
        original(store, item, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("Synthetic migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(support, "record", fail)
        with pytest.raises(RuntimeError, match="Synthetic migration failure"):
            Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 15
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='continuity_support'").fetchone()
        assert len(db.execute("SELECT * FROM continuity_history").fetchall()) == len(history)
    with closing(Store(path)) as recovered:
        assert recovered.continuity_state() == state
        assert recovered.get_maintenance_task(task["id"]) == task


def test_backfill_never_adopts_changed_or_ineligible_historical_originals(tmp_path):
    path = tmp_path / "room.db"
    state, history, task = prior_database(path)
    first_id = history[-1]["data"]["sources"][0]["exchange_id"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE generations SET memory_eligible=0 WHERE id="
                   "(SELECT generation_id FROM messages WHERE id=?)", (first_id,))
    with closing(Store(path)) as store:
        assert len(associations(store, "interview", "real_life")) == 1
        assert store.continuity_history() == history
        assert store.get_maintenance_task(task["id"]) == task
        assert store.continuity_state() == state


@pytest.mark.parametrize("attack", ["unreported", "foreign", "changed", "protected", "rewrite", "ordinary_bypass"])
def test_direct_repair_commit_cannot_bypass_support_identity_or_protection(tmp_path, attack):
    from eidolon.memory.continuity import ContinuityValidationError
    from eidolon.storage.support import candidates

    with closing(Store(tmp_path / "room.db")) as store:
        first = exchange(store)
        commit(store, task_for(store), [upsert(first, item_id="interview")])
        second = exchange(store, "My interview has a practice session.")
        task = task_for(store)
        source = candidates(store, "interview", "real_life")[0]
        operation = {"op": "repair_support", "item_id": "interview", "expected_revision": 1,
                     "sources": [source], "dispositions": []}
        if attack != "unreported":
            store.fail_maintenance_stage(task["id"], "continuity", "Saved support needs review",
                                         reason_code="validation.saved_support", recovery_state={
                                             "code": "validation.saved_support", "targets": ["interview"],
                                             "repair_target": "interview"})
        if attack == "foreign":
            operation["sources"] = [{**source, "exchange_id": second["id"], "document_id": second["document_id"]}]
        if attack == "changed":
            with store.db:
                store.db.execute("UPDATE messages SET content='Changed source.' WHERE id=?", (first["id"],))
        if attack == "protected":
            # Preserve the scope revision to exercise the protection defense
            # independently of the CAS defense tested through the service.
            with store.db:
                store.db.execute("UPDATE continuity_items SET data=json_set(data,'$.locked',json('true'))")
        if attack == "rewrite":
            operation["content"] = "Invented replacement prose."
        if attack == "ordinary_bypass":
            operation = {"op": "no_change", "expected_revision": task["expected_revision"]}
        before = store.continuity_state(), store.continuity_history(), associations(store, "interview", "real_life")
        with pytest.raises(ContinuityValidationError):
            commit(store, task, [operation])
        assert (store.continuity_state(), store.continuity_history(),
                associations(store, "interview", "real_life")) == before
