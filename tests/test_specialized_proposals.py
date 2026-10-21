"""Task-specific maintenance through the service with real temporary storage."""
import json

import pytest

from eidolon.memory.continuity import validate_operations
from eidolon.memory.proposals import CapacityProposal, LegacyProposal
from eidolon.providers.routing import digest
from test_continuity_narrative import add, cite, legacy, narrative
from test_current_life_policy import seed_note
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


async def test_capacity_service_archives_only_excess_and_preserves_protected_notes(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        low = seed_note(service.store)
        high = seed_note(service.store, topic="Our anniversary", importance="high")
        protected = service.store.create_continuity_item(
            "An authored subject.", "relationship", topic="Authored subject", importance="low")
        await client.request("save_preferences", preferences={"continuity_max_notes": 2})

        def choose(doc):
            assert doc["contract"] == "current-life-capacity-v1"
            assert doc["exchanges"] == []
            target = next(item for item in doc["items"] if item["id"] == low["id"])
            return {"operations": [{"op": "archive", "note_ref": target["note_ref"],
                                    "decision_reason": "The other subjects matter more."}]}

        provider = CompactProvider(choose)
        await refresh(service, client, provider)
        retired = service.store.get_continuity_item(low["id"])
        assert retired["state"] == "archived" and retired["archive_reason"] == "capacity"
        assert retired["content"] == low["content"] and retired["sources"] == low["sources"]
        assert service.store.get_continuity_item(high["id"]) == high
        assert service.store.get_continuity_item(protected["id"]) == protected
        assert service.store.continuity_state()["active_count"] == 2
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        provenance = service.store.maintenance_status()["latest_attempt"]["provenance"]
        assert provenance["schema_hash"] == digest({
            "contract": "current-life-capacity-v1", "schema": CapacityProposal.model_json_schema(),
        })


@pytest.mark.parametrize("case", ["admission", "completion", "too_many", "locked", "authored",
                                  "mixed", "unknown"])
async def test_capacity_service_rejects_cross_task_or_unsafe_actions_atomically(
    tmp_path, monkeypatch, case,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        low = seed_note(service.store)
        other = seed_note(service.store, topic="Our anniversary", importance="high")
        authored = service.store.create_continuity_item(
            "An authored subject.", "relationship", topic="Authored subject", importance="low")
        locked = service.store.edit_continuity_item(other["id"], {"locked": case != "too_many"},
                                                    expected_revision=other["revision"])
        await client.request("save_preferences", preferences={"continuity_max_notes": 2})
        before = service.store.continuity_state()

        def respond(doc):
            refs = {item["id"]: item["note_ref"] for item in doc["items"]}
            valid = {"op": "archive", "note_ref": refs[low["id"]],
                     "decision_reason": "A lower priority subject."}
            invalid = dict(valid)
            if case in {"admission", "mixed"}:
                invalid = {"op": "upsert", "topic": "New subject", "importance": "high",
                           "category": "thread", "content": "A new subject.", "sources": []}
            elif case == "completion":
                invalid.update(archive_reason="completed", content="The project finished.")
            elif case in {"locked", "authored"}:
                invalid["note_ref"] = refs[(locked if case == "locked" else authored)["id"]]
            elif case == "unknown":
                invalid["note_ref"] = "n999"
            elif case == "too_many":
                invalid["note_ref"] = refs[other["id"]]
            operations = [valid, invalid] if case in {"mixed", "too_many"} else [invalid]
            return {"operations": operations}

        await refresh(service, client, CompactProvider(respond))
        assert service.store.continuity_state() == before
        assert service.store.pending_maintenance_task() is not None
        assert service.store.maintenance_status()["latest_attempt"]["outcome"] == "failed"


@pytest.mark.parametrize("case", ["new", "archive", "category", "sources", "metadata",
                                  "other_note", "unknown", "rewrite_prose", "mixed",
                                  "locked", "authored"])
async def test_legacy_service_rejects_cross_task_or_unsafe_actions_atomically(
    tmp_path, monkeypatch, case,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        old = legacy(service.store)
        protected = None
        if case == "locked":
            protected = service.store.edit_continuity_item(old["id"], {"locked": True},
                                                            expected_revision=old["revision"])
        elif case == "authored":
            protected = service.store.create_continuity_item(
                "An authored subject.", "thread", topic="Authored subject", importance="low")
        other = seed_note(service.store, topic="Creative project")
        # Simulate an older narrative note awaiting one-time classification.
        with service.store.db:
            data = {key: value for key, value in other.items() if key not in {"topic", "importance"}}
            service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                     (json.dumps(data), other["id"]))
        before = service.store.continuity_state()

        def respond(doc):
            items = {item["id"]: item for item in doc["items"]}
            valid = {"op": "upsert", "note_ref": items[old["id"]]["note_ref"],
                     "topic": old["topic"], "importance": "moderate",
                     "content": "The user's interview is approaching; practice remains undecided."}
            invalid = dict(valid)
            if case == "new":
                invalid.pop("note_ref")
            elif case == "archive":
                invalid = {"op": "archive", "note_ref": valid["note_ref"],
                           "decision_reason": "Finished.", "archive_reason": "completed"}
            elif case == "category":
                invalid["category"] = "relationship"
            elif case == "sources":
                invalid["sources"] = []
            elif case == "metadata":
                invalid["event_at"] = None
            elif case == "other_note":
                invalid["note_ref"] = next(item["note_ref"] for item in doc["items"]
                                            if item["note_ref"] not in doc["target_refs"])
            elif case == "unknown":
                invalid["note_ref"] = "n999"
            elif case == "rewrite_prose":
                invalid.update(note_ref=items[other["id"]]["note_ref"], topic=other["topic"])
            elif protected:
                invalid["note_ref"] = items[protected["id"]]["note_ref"]
            return {"operations": [valid, invalid] if case == "mixed" else [invalid]}

        # A classified narrative note is read-only context in the legacy request.
        if case == "other_note":
            seed_note(service.store, topic="Garden illustration")
            before = service.store.continuity_state()
        await refresh(service, client, CompactProvider(respond))
        assert service.store.continuity_state() == before
        assert service.store.pending_maintenance_task() is not None
        assert service.store.maintenance_status()["latest_attempt"]["outcome"] == "failed"


async def test_legacy_service_converts_named_note_and_preserves_saved_support(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        old = legacy(service.store)
        prose = "The user's interview is approaching; practice remains undecided."

        def convert(doc):
            assert doc["contract"] == "current-life-legacy-v1"
            target = next(item for item in doc["items"] if item["id"] == old["id"])
            assert doc["target_refs"] == [target["note_ref"]]
            return {"operations": [{"op": "upsert", "note_ref": target["note_ref"],
                                    "topic": old["topic"], "importance": "moderate",
                                    "content": prose}]}

        provider = CompactProvider(convert)
        await refresh(service, client, provider)
        saved = service.store.get_continuity_item(old["id"])
        assert saved["content"] == prose and saved["content_format"] == "narrative"
        for field in ("sources", "category", "scope", "event_at", "observed_at"):
            assert saved[field] == old[field]
        provenance = service.store.maintenance_status()["latest_attempt"]["provenance"]
        assert provenance["schema_hash"] == digest({
            "contract": "current-life-legacy-v1", "schema": LegacyProposal.model_json_schema(),
        })
        await refresh(service, client, provider)
        assert len(provider.calls) == 1

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        provider = CompactProvider(lambda doc: pytest.fail("Completed conversion was dispatched again"))
        await refresh(service, client, provider)
        assert service.store.get_continuity_item(old["id"])["content"] == prose
        assert provider.calls == []


async def test_legacy_service_keeps_all_original_sources_and_recorded_attribution(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        recorded = {"user_name": "Aster", "companion_name": "Vale"}
        rows = [add(service.store, metadata={"identity": recorded}),
                add(service.store, "My interview preparation is still undecided.")]
        operation = narrative(rows[0], "\n".join(row["user"] for row in rows),
                              origin="user_statement",
                              sources=[{**cite(row), "excerpt": row["user"]} for row in rows])
        operation.pop("content_format")
        operations = validate_operations({"operations": [operation]}, inputs=rows,
                                          state=service.store.continuity_state())
        task = service.store.create_maintenance_task(rows, expected_revision=0, control_revision=0)
        service.store.commit_continuity(task["id"], operations, expected_revision=0, control_revision=0)
        old = service.store.continuity_state()["items"][0]
        evidence = service.store.continuity_evidence([row["exchange_id"] for row in rows])

        def convert(doc):
            item = doc["items"][0]
            assert len(item["sources"]) == 2
            assert doc["exchanges"][0]["identity"] == recorded
            return {"operations": [{"op": "upsert", "note_ref": doc["target_refs"][0],
                                    "topic": old["topic"], "importance": "moderate",
                                    "content": "Aster's interview is approaching; preparation remains undecided."}]}

        await refresh(service, client, CompactProvider(convert))
        saved = service.store.get_continuity_item(old["id"])
        assert saved["content_format"] == "narrative"
        assert saved["sources"] == old["sources"]
        assert service.store.continuity_evidence([row["exchange_id"] for row in rows]) == evidence


async def test_legacy_classification_is_bounded_and_resumes_after_service_restart(tmp_path, monkeypatch):
    originals = []

    def classify(doc):
        return {"operations": [{"op": "upsert", "note_ref": item["note_ref"],
                                "topic": next(note["topic"] for note in originals
                                              if note["id"] == item["id"]), "importance": "high"}
                               for item in doc["items"] if item["note_ref"] in doc["target_refs"]]}

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        originals = [seed_note(service.store, topic=f"Creative project {number}") for number in range(6)]
        with service.store.db:
            for note in originals:
                data = {key: value for key, value in note.items() if key not in {"topic", "importance"}}
                # Existing emotion expiry must survive the normal validator's default.
                data.update(category="emotion", expires_at="2099-01-02T00:00:00+00:00",
                            event_at="2026-10-01T00:00:00+00:00",
                            follow_up_at="2026-10-02T00:00:00+00:00",
                            promise_owner="user", promise_kind="conversation")
                service.store.db.execute("UPDATE continuity_items SET data=? WHERE id=?",
                                         (json.dumps(data), note["id"]))
        before = {note["id"]: note for note in service.store.continuity_state()["items"]}
        watermark = service.store.continuity_state()["watermark"]
        provider = CompactProvider(classify)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        assert sum(bool(note.get("metadata_pending"))
                   for note in service.store.continuity_state()["items"]) == 2

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        provider = CompactProvider(classify)
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
        saved = service.store.continuity_state()
        assert saved["watermark"] == watermark
        assert not any(note.get("metadata_pending") for note in saved["items"])
        for note in saved["items"]:
            for field in ("content", "sources", "category", "scope", "event_at", "expires_at",
                          "follow_up_at", "promise_owner", "promise_kind"):
                assert note[field] == before[note["id"]][field]
        await refresh(service, client, provider)
        assert len(provider.calls) == 1
