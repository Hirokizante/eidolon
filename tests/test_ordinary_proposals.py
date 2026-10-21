"""Compact maintenance through the service and frozen proposal compiler."""
import hashlib
import json

import pytest

from test_service import room
from eidolon.memory.continuity import ContinuityValidationError
from eidolon.memory.proposals import ORDINARY_CONTRACT, OrdinaryProposal, OrdinaryRequest, parse_proposal
from eidolon.providers.routing import digest


class CompactProvider:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    async def stream(self, messages, **kwargs):
        self.calls.append(messages)
        result = self.respond(json.loads(messages[-1]["content"]))
        yield {"type": "token", "text": json.dumps(result)}
        yield {"type": "finish", "reason": "stop"}


@pytest.fixture
def captured_request():
    row = {"exchange_id": 7, "document_id": "synthetic:7", "user_content": "My interview is tomorrow.",
           "assistant_content": "We could practice.", "created_at": "2026-10-01T12:00:00+00:00",
           "scope": "real_life"}
    state = {"revision": 5, "control_revision": 2, "items": [], "source_exchanges": []}
    document = {"items": [], "exchanges": [row], "scope": "real_life"}
    request = OrdinaryRequest(document, inputs=[row], state=state)
    return request, row, state


def test_compiler_freezes_revision_and_originals(captured_request):
    request, row, state = captured_request
    state["revision"] = 99
    row["document_id"] = "mutated:identity"
    row["user_content"] = "A different source."
    assert request.compile({"operations": [{"op": "no_change"}]}) == [
        {"op": "no_change", "expected_revision": 5, "sources": []},
    ]
    operations = request.compile({"operations": [{
        "op": "upsert", "topic": "Interview preparation", "importance": "moderate",
        "category": "thread", "content": "The user's interview is approaching.", "sources": ["s1"],
    }]})
    assert operations[0]["sources"][0]["document_id"] == "synthetic:7"
    assert operations[0]["sources"][0]["excerpt"] == "My interview is tomorrow."


@pytest.mark.parametrize("proposal", [
    {"operations": []},
    {"operations": [{"op": "no_change", "expected_revision": 5}]},
    {"operations": [{"op": "no_change", "sources": ["s1"]}]},
    {"operations": [{"op": "no_change"}, {"op": "no_change"}]},
    {"operations": [{"op": "upsert", "sources": ["s1"]}]},
    {"operations": [{"op": "archive", "note_ref": "n1", "archive_reason": "completed",
                     "decision_reason": "Finished.", "content": "Interview finished.", "sources": ["s1"]}]},
])
def test_compiler_rejects_incomplete_or_invented_operations(captured_request, proposal):
    request, _, _ = captured_request
    with pytest.raises(ContinuityValidationError):
        request.compile(proposal)


@pytest.mark.parametrize("text", [
    ' {"operations":[{"op":"no_change"}]} \n',
    '```json\n{"operations":[{"op":"no_change"}]}\n```',
    '```\n{"operations":[{"op":"no_change"}]}\n```',
])
def test_complete_harmless_outer_formatting_is_accepted(captured_request, text):
    request, _, _ = captured_request
    assert request.compile(parse_proposal(text))[0]["op"] == "no_change"


@pytest.mark.parametrize("text", [
    'Here is JSON: {"operations":[{"op":"no_change"}]}',
    '{"operations":[{"op":"no_change"}]} trailing prose',
    '```json\n{"operations":[{"op":"no_change"}]}',
    '{"operations":[{"op":"no_change"}],"operations":[]}',
    '{"operations":[{"op":"upsert","op":"no_change"}]}',
    '{"operations":[', '[]', '{"operations":NaN}',
    '```json\n{}\n```\n```json\n{}\n```',
])
def test_parser_rejects_ambiguous_incomplete_or_substantive_repairs(text):
    with pytest.raises(ContinuityValidationError):
        parse_proposal(text)


async def refresh(service, client, provider):
    service.continuity_worker.provider = provider
    await client.request("memory_refresh")
    if service._continuity_task is not None:
        await service._continuity_task
    return await client.request("memory_state")


async def test_compact_admission_commits_original_support_in_one_service_request(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(
            service.current_session["id"], "My interview is tomorrow.", "We could practice.",
            metadata={"identity": {"user_name": "Aster", "companion_name": "Vale"}},
        )
        provider = CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "Interview preparation", "importance": "moderate",
            "category": "thread", "content": "The user has an upcoming interview; practice is undecided.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]})
        await refresh(service, client, provider)
        state = service.store.continuity_state()
        assert len(state["items"]) == 1
        note = state["items"][0]
        assert note["content_format"] == "narrative" and note["origin"] == "derived"
        assert note["scope"] == "real_life"
        assert note["sources"][0]["excerpt"] == "My interview is tomorrow."
        original = service.store.continuity_evidence([note["sources"][0]["exchange_id"]])[0]
        assert original["identity"] == {"user_name": "Aster", "companion_name": "Vale"}
        assert len(provider.calls) == 1
        assert service.store.pending_maintenance_task() is None
        provenance = service.store.maintenance_status()["latest_attempt"]["provenance"]
        assert provenance["prompt_hash"] == hashlib.sha256(provider.calls[0][0]["content"].encode()).hexdigest()
        assert provenance["schema_hash"] == digest({
            "contract": ORDINARY_CONTRACT, "schema": OrdinaryProposal.model_json_schema(),
        })


async def test_specialized_service_conversion_records_its_effective_contract(tmp_path, monkeypatch):
    from test_continuity_narrative import legacy
    from test_memory_maintenance import Provider

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = legacy(service.store)

        def convert(doc):
            assert doc["rewrite_existing"]
            return {"operations": [{
                "op": "upsert", "item_id": note["id"], "expected_revision": note["revision"],
                "topic": "Interview preparation", "importance": "moderate", "category": "thread",
                "content": "The user's interview is approaching; practice remains undecided.",
                "content_format": "narrative", "origin": "derived", "scope": "real_life",
                "sources": [{key: source[key] for key in ("exchange_id", "document_id", "role", "origin")}
                            for source in note["sources"]],
            }]}

        provider = Provider(transform=convert)
        await refresh(service, client, provider)
        assert service.store.get_continuity_item(note["id"])["content_format"] == "narrative"
        attempt = service.store.maintenance_status()["latest_attempt"]
        assert attempt["outcome"] == "committed"
        assert attempt["provenance"]["prompt_hash"] == hashlib.sha256(
            provider.calls[0][0]["content"].encode()).hexdigest()
        assert attempt["provenance"]["schema_hash"] != service._auxiliary_execution.schema_hash


async def test_compact_update_and_closure_preserve_one_topic(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        def add(text):
            service.store.complete_turn(service.current_session["id"], text, "Understood.")

        add("My interview is tomorrow.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "topic": "Interview preparation", "importance": "moderate",
            "category": "thread", "content": "The user's interview is approaching.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        first = service.store.continuity_state()["items"][0]
        add("My interview preparation can wait; I prefer encouragement.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "upsert", "note_ref": doc["items"][0]["note_ref"],
            "topic": "Interview preparation", "importance": "moderate", "category": "thread",
            "content": "The user's interview is approaching; they prefer encouragement over practice.",
            "sources": [doc["items"][0]["sources"][0]["source_ref"],
                        doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        updated = service.store.continuity_state()["items"][0]
        assert updated["id"] == first["id"] and updated["revision"] == 2
        assert len(updated["sources"]) == 2
        add("My interview is finished.")
        await refresh(service, client, CompactProvider(lambda doc: {"operations": [{
            "op": "archive", "note_ref": doc["items"][0]["note_ref"],
            "archive_reason": "completed", "decision_reason": "The user confirmed completion.",
            "content": "The user's interview has finished.",
            "sources": [doc["exchanges"][0]["source_refs"]["user"]],
        }]}))
        closed = service.store.continuity_state()["items"]
        assert len(closed) == 1 and closed[0]["state"] == "archived"
        assert closed[0]["topic"] == "Interview preparation"
        assert service.store.pending_maintenance_task() is None


@pytest.mark.parametrize("failure", ["source", "note", "stale"])
async def test_unsafe_or_stale_compact_proposal_cannot_partially_commit(
    tmp_path, monkeypatch, failure,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "My interview is tomorrow.", "OK.")

        def respond(doc):
            valid = {"op": "upsert", "topic": "Interview preparation", "importance": "moderate",
                     "category": "thread", "content": "The user's interview is approaching.",
                     "sources": [doc["exchanges"][0]["source_refs"]["user"]]}
            if failure == "stale":
                service.store.create_continuity_item("An authored garden note.", "thread")
                return {"operations": [valid]}
            invalid = {**valid, "topic": "Other interview detail"}
            invalid.update({"sources": ["s9999"]} if failure == "source" else {"note_ref": "n999"})
            return {"operations": [valid, invalid]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        state = service.store.continuity_state()
        assert state["watermark"] == 0
        assert all(note.get("user_created") for note in state["items"])
        assert service.store.pending_maintenance_task() is not None
        attempt = service.store.maintenance_status()["latest_attempt"]
        assert attempt["outcome"] in {"failed", "stale"}
        assert len(provider.calls) == 1


@pytest.mark.parametrize("text,has_candidates", [
    ("I will practice for my interview tomorrow.", True),
    ("Maybe I will practice for my interview tomorrow.", False),
])
async def test_optional_candidates_keep_supported_metadata_and_uncertainty_in_prose(
    tmp_path, monkeypatch, text, has_candidates,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], text, "Understood.")

        def respond(doc):
            exchange = doc["exchanges"][0]
            candidates = exchange["metadata_candidates"]["user"]
            assert bool(candidates.get("dates")) is has_candidates
            assert bool(candidates.get("promise_kind")) is has_candidates
            op = {"op": "upsert", "topic": "Interview preparation", "importance": "moderate",
                  "category": "thread", "content": "The user discussed possible interview practice tomorrow.",
                  "sources": [exchange["source_refs"]["user"]]}
            if has_candidates:
                op.update(event_at=candidates["dates"][0], promise_source=exchange["source_refs"]["user"])
            return {"operations": [op]}

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        note = service.store.continuity_state()["items"][0]
        assert "tomorrow" in note["content"]
        assert ("event_at" in note) is has_candidates
        assert note.get("promise_owner") == ("user" if has_candidates else None)
        assert note.get("promise_kind") == ("conversation" if has_candidates else None)
