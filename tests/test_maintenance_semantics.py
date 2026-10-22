"""Semantic developments through the service with real temporary persistence."""
import pytest

from test_current_life_policy import seed_note
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.mark.parametrize("text", [
    "That meeting at Ember Library? All wrapped up; they offered me the catalog assistant role.",
    "My Ember Library interview went well.",
    "I aced my Ember Library interview.",
    "I got through my Ember Library interview.",
    "My Ember Library interview wrapped up this afternoon.",
    "Ember Library offered me the catalog assistant role.",
    "That meeting at Ember Library? It is finished.",
])
async def test_anchored_interview_success_closes_in_one_call(tmp_path, monkeypatch, text):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview preparation",
                         text="My Ember Library interview is tomorrow.")
        service.store.complete_turn(service.current_session["id"], text, "Congratulations.")
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "archive", "note_ref": doc["items"][0]["note_ref"],
            "archive_reason": "completed", "decision_reason": "The user reported the outcome.",
            "content": "The user's Ember Library interview has concluded.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        await refresh(service, client, provider)
        saved = service.store.get_continuity_item(note["id"])
        assert saved["state"] == "archived"
        assert saved["revision"] == note["revision"] + 1
        assert text in [source["excerpt"] for source in saved["sources"]]
        assert len(service.store.continuity_history(note["id"])) == 2
        assert service.store.pending_maintenance_task() is None
        assert len(provider.calls) == 1


@pytest.mark.parametrize("text", [
    "All wrapped up.",
    "That thing we discussed is done.",
    "My garden project is all wrapped up.",
    "Did my Ember Library interview go well?",
    "My Ember Library interview went well?",
    "I will ace my Ember Library interview.",
    "My Ember Library interview will be all wrapped up tomorrow.",
    "My Ember Library interview did not go well; it has not happened yet.",
    "My Ember Library interview isn't all wrapped up.",
    "If my Ember Library interview went well, they would offer me the role.",
    "Maybe my Ember Library interview is all wrapped up.",
    "My brother said:\nMy Ember Library interview is all wrapped up.",
    'My brother sent this message: "My Ember Library interview went well."',
    "My brother's Ember Library interview went well.",
    "My manager's Ember Library interview went well.",
    "Aster's Ember Library interview went well.",
    "My cousin's Ember Library interview is all wrapped up.",
    "In our story my Ember Library interview went well. My real interview is tomorrow.",
    "That meeting at Ember Library? Will it be all wrapped up tomorrow?",
    "My Ember Library interview preparation went well.",
    "My Ember Library interview is tomorrow. My garden project is all wrapped up.",
    "My Ember Library interview is tomorrow; my garden project is all wrapped up.",
    "My Ember Library interview preparation is all wrapped up.",
    "I got through my Ember Library interview practice.",
    "My Ember Library interview was tough; my garden project is all wrapped up.",
    "That meeting at Ember Library? It isn't finished. All wrapped up was just a wish.",
    "I wish my Ember Library interview wrapped up already.",
    "My Ember Library interview was hard, but my garden project wrapped up.",
    "My garden project wrapped up after I discussed my Ember Library interview.",
    "I got through my garden project after discussing my Ember Library interview.",
    "I got through my preparation for the Ember Library interview.",
    "My practice for the Ember Library interview wrapped up.",
    "I got through a mock Ember Library interview.",
])
async def test_unsupported_closure_preserves_the_entire_batch(tmp_path, monkeypatch, text):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview preparation",
                         text="My Ember Library interview is tomorrow.")
        service.store.complete_turn(service.current_session["id"], text, "Understood.")
        before = service.store.continuity_state()
        provider = CompactProvider(lambda doc: {"operations": [
            {"op": "upsert", "topic": "An additional subject", "importance": "low",
             "category": "thread", "content": "A second proposed note must not partially commit.",
             "sources": [doc["exchanges"][0]["source_refs"]["user"]]},
            {"op": "archive", "note_ref": doc["items"][0]["note_ref"],
             "archive_reason": "completed", "decision_reason": "A generated success claim.",
             "content": "The user's interview is finished.",
             "sources": [doc["exchanges"][0]["source_refs"]["user"]]},
        ]})
        await refresh(service, client, provider)
        assert service.store.continuity_state() == before
        assert service.store.get_continuity_item(note["id"]) == note
        assert service.store.pending_maintenance_task() is not None
        assert service.store.maintenance_status()["latest_attempt"]["outcome"] == "failed"
        assert len(provider.calls) == 1


@pytest.mark.parametrize("topic,original,text,prose", [
    ("Submission deadline", "My submission deadline is approaching.", text,
     "The user's submission deadline has moved to February 9.")
    for text in ("The cutoff has shifted to February 9.",
                 "The closing date has shifted to February 9.",
                 "The due date has shifted to February 9.")
] + [
    ("Interview preparation", "My job interview is approaching.",
     "The hiring conversation has been moved to Friday.",
     "The user's interview has moved to Friday."),
    ("House move", "My house move is ongoing.",
     "The relocation now includes temporary storage.",
     "The user's move now requires temporary storage."),
    ("Medical visit", "My doctor's appointment is tomorrow.",
     "The medical appointment has been postponed.",
     "The user's appointment has been postponed."),
    ("Interview preparation", "My Ember Library interview is tomorrow.",
     "About Ember Library: the panel wants another conversation.",
     "The user's interview now includes another conversation with the panel."),
])
async def test_equivalent_wording_updates_the_existing_note(
    tmp_path, monkeypatch, topic, original, text, prose,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic=topic, text=original)
        service.store.complete_turn(service.current_session["id"], text, "Understood.")
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
            "topic": note["topic"], "importance": "moderate", "category": "thread",
            "content": prose, "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        await refresh(service, client, provider)
        saved = service.store.get_continuity_item(note["id"])
        assert saved["content"] == prose and saved["revision"] == 2
        assert saved["sources"][0]["excerpt"] == text
        assert service.store.continuity_state()["active_count"] == 1
        assert service.store.pending_maintenance_task() is None
        assert len(provider.calls) == 1


@pytest.mark.parametrize("original,text", [
    ("My Ember Library interview is tomorrow.", "My dentist appointment is ongoing."),
    ("My Ember Library interview is tomorrow.", "That thing we discussed has changed."),
    ("My Ember Library interview is tomorrow.", "My garden project has a new deadline."),
    ("My Ember Library interview is tomorrow. My garden project is ongoing.",
     "My garden project has a new deadline."),
    ("My Ember Library interview is tomorrow, and my garden project needs attention.",
     "My garden project now has a greenhouse."),
])
async def test_unrelated_update_remains_pending(tmp_path, monkeypatch, original, text):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview preparation",
                         text=original)
        service.store.complete_turn(service.current_session["id"], text, "Understood.")
        before = service.store.continuity_state()
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
            "topic": note["topic"], "importance": "moderate", "category": "thread",
            "content": "The user has a new dentist appointment.",
            "sources": [doc["items"][0]["sources"][0]["source_ref"],
                        doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        await refresh(service, client, provider)
        assert service.store.continuity_state() == before
        assert service.store.pending_maintenance_task() is not None


@pytest.mark.parametrize("reason,text", [
    ("broken_promise", "I failed to finish my Ember Library interview preparation; "
     "I missed the deadline."),
    ("no_longer_current", "My Ember Library interview preparation is no longer relevant; "
     "the employer cancelled the process."),
])
async def test_preparation_can_be_retired_without_claiming_interview_completion(
    tmp_path, monkeypatch, reason, text,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview preparation",
                         text="I promise to finish my Ember Library interview preparation.")
        service.store.complete_turn(service.current_session["id"], text, "Understood.")
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "archive", "note_ref": doc["items"][0]["note_ref"],
            "archive_reason": reason, "decision_reason": "The user reported retirement evidence.",
            "content": "The user's interview preparation is no longer active.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        await refresh(service, client, provider)
        saved = service.store.get_continuity_item(note["id"])
        assert saved["state"] == "archived" and saved["archive_reason"] == reason
        assert service.store.pending_maintenance_task() is None
        assert len(provider.calls) == 1
