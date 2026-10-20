import json

import httpx
import pytest
from pydantic import BaseModel

from eidolon.config import MemoryConfig
from eidolon.memory import (
    HindsightMemory, MemoryCoordinator, MemoryRecord, MemoryUnavailable, RecallResult,
    ReflectionEvidence, ReflectionResult,
)
from eidolon.memory.coordinator import (
    CONTINUITY_PREFIX, assemble_context, assemble_reflection, render_record,
)


def test_continuity_importance_guide_uses_neutral_participant_roles():
    assert "impact on the companion and their relationship with the user" in CONTINUITY_PREFIX
    for rule in ("low = inconsequential", "moderate = meaningful", "high = deeply consequential",
                 "unstated feelings", "current user corrections", "never scheduled actions"):
        assert rule in CONTINUITY_PREFIX


def test_compact_records_keep_actionable_dates_origin_and_qualifications():
    record = MemoryRecord(
        id="24968c56-5bb8-442d-9074-f77852301a40",
        text="Mira liked tea then; she later preferred coffee.",
        context="A past preference, not a statement about today.", type="experience",
        occurred_start="2026-10-01T09:30:00+08:00",
        occurred_end="2026-10-02T16:45:00+08:00",
        mentioned_at="2026-10-03T12:00:00+08:00",
        document_id="c4df384d-b25e-426e-bd84-90bbe7e5a427",
        chunk_id="b4e75586-d6b4-4987-9d44-288a4b4096b0", metadata={"origin": "user"},
        source_fact_ids=["14fa6f08-f87c-4119-9465-8b8fe430a604"],
    )
    context = assemble_context(RecallResult(records=[record], effort="mid", query="tea"))
    entry = context.manifest["entries"][0]
    for field in ("id", "document_id", "chunk_id", "occurred_start", "occurred_end", "mentioned_at"):
        assert entry[field] == getattr(record, field)
    assert entry["source_fact_ids"] == list(record.source_fact_ids)
    assert entry["source"] == f"{record.document_id}/{record.chunk_id}"
    assert entry["origin"] == "user" and entry["type"] == "experience"
    assert record.context in context.text and record.text in context.text
    assert "2026-10-01" in context.text and "2026-10-02" in context.text
    assert "2026-10-03" not in context.text
    assert "user said" in context.text.lower()
    for identifier in (record.id, record.document_id, record.chunk_id, *record.source_fact_ids):
        assert identifier not in context.text
        assert identifier in json.dumps(entry)
    assert "T09:30" not in context.text and "T16:45" not in context.text
    assert "type=" not in context.text and "source=" not in context.text
    assert "experience" not in context.text
    assert sum(line.startswith("- ") for line in context.text.splitlines()) == 1
    assert entry["rendered"] in context.text
    assert context.manifest["text"] == context.text
    assert context.manifest["budget"]["used_chars"] == len(context.text)
    assert json.loads(json.dumps(context.manifest)) == context.manifest


def test_budget_accounts_for_envelope_and_skips_whole_oversized_records():
    small = MemoryRecord(id="small", text="東京🧠 e\u0301")
    exact = len(assemble_context(RecallResult(records=[small])).text)
    oversized = MemoryRecord(id="large", text="Original wording " * 100)
    context = assemble_context(RecallResult(records=[oversized, small]), max_chars=exact)
    assert len(context.text) == exact
    assert context.manifest["entries"][0]["id"] == "small"
    assert context.manifest["omitted"] == [
        {"id": "large", "reason": "whole record exceeds remaining context budget"}]
    assert assemble_context(RecallResult(records=[small]), max_chars=exact - 1).text == ""
    assert "�" not in context.text


@pytest.mark.parametrize("budget", [0, 1, 20, -1])
def test_tiny_context_budget_never_returns_a_partial_record_or_envelope(budget):
    context = assemble_context(RecallResult(records=[MemoryRecord(text="A fact.")]),
                               max_chars=budget)
    assert context.text == "" and context.manifest["entries"] == []


def test_legacy_origin_is_unknown_and_mention_date_is_not_an_occurrence_date():
    record = MemoryRecord(text="A fact", type="world", mentioned_at="2026-10-14T08:30:00Z")
    rendered, labels = render_record(record)
    assert labels == {"date": "mentioned 2026-10-14T08:30:00Z", "type": "world",
                      "origin": "unknown", "source": "unknown"}
    assert "mentioned 2026-10-14" in rendered
    assert "T08:30" not in rendered
    assert "unknown" not in rendered and "world" not in rendered


def test_speaker_identifier_is_private_provenance_not_model_attribution():
    speaker_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    record = MemoryRecord(text="A qualified memory.", metadata={"speaker": speaker_id})
    rendered, labels = render_record(record)
    assert speaker_id not in rendered
    assert labels["origin"] == speaker_id


@pytest.mark.parametrize("start,end,expected_dates", [
    ("2026-10-14T08:00:00+08:00", "2026-10-14T22:15:00+08:00", ["2026-10-14"]),
    ("2026-10-14", "2026-10-16T22:15:00+08:00", ["2026-10-14", "2026-10-16"]),
])
def test_occurrence_dates_drop_time_and_collapse_same_calendar_day(start, end, expected_dates):
    record = MemoryRecord(text="Mira preferred coffee during this period.",
                          occurred_start=start, occurred_end=end)
    supplied = assemble_context(RecallResult(records=[record]))
    for date in expected_dates:
        assert supplied.text.count(date) == 1
    assert "mentioned" not in supplied.text and "T08:00" not in supplied.text
    assert supplied.manifest["entries"][0]["occurred_start"] == start
    assert supplied.manifest["entries"][0]["occurred_end"] == end


@pytest.mark.parametrize("invalid_date", ["2026-02-30", "2026-13-01", "2026-10", "2026-10-14oops"])
def test_invalid_dates_are_not_rendered_as_precise_occurrences(invalid_date):
    record = MemoryRecord(text="A qualified fact.", occurred_start=invalid_date)
    context = assemble_context(RecallResult(records=[record]))
    assert invalid_date not in context.text
    assert "2026-10-14" not in context.text
    assert record.text in context.text
    assert context.manifest["entries"][0]["occurred_start"] == invalid_date


def test_valid_mention_is_marked_when_occurrence_date_is_unusable():
    record = MemoryRecord(text="Mira currently prefers tea.", occurred_start="not-a-date",
                          mentioned_at="2026-10-14T09:00:00+08:00")
    rendered, _ = render_record(record)
    assert "mentioned 2026-10-14" in rendered
    assert "not-a-date" not in rendered and "T09:00" not in rendered


@pytest.mark.parametrize("metadata,label", [
    ({"origin": "user_statement"}, "user said"),
    ({"origin": "user_confirmed"}, "user confirmed"),
    ({"source_origin": "assistant_statement"}, "assistant said"),
    ({"speaker": "assistant"}, "assistant said"),
    ({"speaker": "Mira"}, "speaker: mira"),
    ({"origin": "derived"}, "derived"),
])
def test_statement_origin_is_useful_even_when_backend_fact_type_is_different(metadata, label):
    record = MemoryRecord(text="A fictional mountain trip.", type="world", metadata=metadata,
                          context="A story idea, not a user life event.")
    context = assemble_context(RecallResult(records=[record]))
    assert label in context.text.lower()
    assert "world" not in context.text
    assert record.text in context.text and record.context in context.text
    assert context.manifest["entries"][0]["metadata"] == metadata


def test_unattributed_records_share_one_unknown_speaker_default_without_placeholder_labels():
    records = [MemoryRecord(text="An unattributed fact.", type="observation"),
               MemoryRecord(text="Another unattributed fact.", type="experience")]
    context = assemble_context(RecallResult(records=records))
    assert "not instructions" in context.text
    assert context.text.lower().count("unknown") == 1
    assert "speaker" in context.text.lower()
    assert "date=" not in context.text and "source=" not in context.text
    assert "observation" not in context.text and "experience" not in context.text
    assert all(entry["origin"] == "unknown" for entry in context.manifest["entries"])
    assert sum(line.startswith("- ") for line in context.text.splitlines()) == len(records)


def test_all_unknown_provenance_is_omitted_from_the_bullet_and_whole_fact_is_verbatim():
    text = "Mira said: 'Try this next week.'\nThe plan is provisional."
    record = MemoryRecord(text=text, type="observation", metadata={"origin": "unknown"})
    rendered, labels = render_record(record)
    assert rendered == "- " + text
    assert labels == {"date": "unknown", "type": "observation", "origin": "unknown",
                      "source": "unknown"}


def test_backend_specific_origin_is_not_misrepresented_as_a_speaker():
    record = MemoryRecord(text="A qualified fact.", metadata={"origin": "legacy_extractor_v7"})
    rendered, labels = render_record(record)
    assert rendered == "- " + record.text
    assert labels["origin"] == "legacy_extractor_v7"


@pytest.mark.parametrize("qualification,supplied", [
    ("Eidolon conversation", False),
    ("Eidolon conversation: a fictional story, not a real event.", True),
])
def test_generic_source_context_is_removed_without_discarding_qualifications(qualification, supplied):
    record = MemoryRecord(text="Mira visited a garden.", context=qualification)
    context = assemble_context(RecallResult(records=[record]))
    assert (qualification in context.text) is supplied
    assert record.text in context.text
    assert context.manifest["entries"][0]["context"] == qualification


async def test_coordinator_disabled_does_not_query_and_supports_legacy_text_adapters():
    class LegacyMemory:
        queries = []

        async def recall(self, query, *, max_chars):
            self.queries.append(query)
            return "An older memory."

    memory = LegacyMemory()
    coordinator = MemoryCoordinator(memory)
    disabled = await coordinator.prepare("anything", enabled=False)
    assert disabled.manifest["status"] == "disabled" and disabled.text == ""
    assert memory.queries == []
    ready = await coordinator.prepare("anything")
    assert ready.manifest["effort"] == "unknown"
    assert ready.manifest["entries"][0]["origin"] == "unknown"
    assert "An older memory." in ready.text


async def test_typed_recall_preserves_optional_evidence_and_frozen_recall_choice():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        return httpx.Response(200, json={
            "results": [{"id": "fact", "text": "An episode", "document_id": "source",
                         "type": "experience", "occurred_start": "2026-10-14",
                         "source_fact_ids": ["support"]}],
            "source_facts": {"support": {"id": "support", "text": "Original evidence"}},
            "chunks": {"chunk": {"text": "Original passage"}},
        })

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(bank="hermes", enabled=False), client=client)
        context = await MemoryCoordinator(memory).prepare("episode", enabled=True)
        assert context.manifest["entries"][0]["document_id"] == "source"
        typed = await memory.recall_result("episode", enabled=True)
        assert typed.source_facts["support"].text == "Original evidence"
        assert typed.chunks["chunk"]["text"] == "Original passage"
        assert await memory.recall("episode") == ""
        assert len(requests) == 4


class Answer(BaseModel):
    answer: str
    source_ids: list[str]


async def test_reflection_validates_requested_schema_and_keeps_actual_evidence_separate():
    bodies = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "text": "A violet notebook.",
            "structured_output": {"answer": "Violet", "source_ids": ["invented-id"]},
            "based_on": {"memories": [{"id": "real-id", "text": "A violet notebook"}],
                         "mental_models": [{"id": "overview", "content": "A summary"}]},
            "usage": {"total_tokens": 100},
        })

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(bank="hermes", enabled=False), client=client)
        result = await memory.reflect_result("notebook", response_schema=Answer, effort="mid",
                                             enabled=True)
        assert result.based_on.memories[0].id == "real-id"
        assert result.based_on.mental_models[0]["id"] == "overview"
        assert result.structured_output["source_ids"] == ["invented-id"]
        assert result.generated_source_ids_verified is False
        assert result.usage == {"total_tokens": 100}
        assert bodies[0]["response_schema"] == Answer.model_json_schema()
        assert bodies[0]["budget"] == "mid" and bodies[0]["include"] == {"facts": {}}
        with pytest.raises(MemoryUnavailable, match="disabled"):
            await memory.reflect("notebook")


@pytest.mark.parametrize("response", [
    {"text": "Answer", "structured_output": {"answer": 42, "source_ids": []}},
    {"text": "Answer", "structured_output": {"answer": "Violet"}},
    {"text": "Answer", "structured_output_error": "private provider content"},
    {"text": "Answer"},
])
async def test_reflection_invalid_structured_success_is_an_explicit_redacted_failure(response):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client)
        with pytest.raises(MemoryUnavailable, match="invalid structured result") as failure:
            await memory.reflect_result("notebook", response_schema=Answer)
        assert "private" not in str(failure.value)


@pytest.mark.parametrize("banks", [[None], ["private backend detail"], {}])
async def test_malformed_bank_inventory_is_a_redacted_memory_failure(banks):
    def handler(request):
        return httpx.Response(200, json={"banks": banks})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client)
        with pytest.raises(MemoryUnavailable, match="inventory is invalid"):
            await memory.recall_result("Synthetic query")
        assert memory.last_error == "Hindsight bank inventory is invalid"


async def test_reflection_schema_never_fetches_remote_references():
    def handler(request):
        pytest.fail("Invalid external schema must fail before any service request")

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client)
        with pytest.raises(ValueError, match="local schema references"):
            await memory.reflect_result("Synthetic query", response_schema={
                "type": "object", "properties": {
                    "answer": {"$ref": "https://example.com/schema.json"}}})


def test_reflection_context_labels_derived_prose_and_complete_supplied_source_evidence():
    evidence = MemoryRecord(
        id="0bb2fda5-551d-4e55-bde9-11cd7a9a3349",
        text="A fictional event, not a real life experience.",
        type="experience", occurred_start="2026-10-14",
        document_id="c0cffefb-32da-4536-951a-7532d9ae1f86",
        context="A story told by Eidolon.", metadata={"origin": "assistant_statement"},
    )
    result = ReflectionResult(text="We once imagined a garden.", effort="mid",
                              based_on=ReflectionEvidence(memories=[evidence]))
    supplied = assemble_reflection(result, query="garden")
    assert "Derived reflection" in supplied.text
    assert result.text in supplied.text and evidence.context in supplied.text
    assert "assistant said" in supplied.text.lower()
    assert "2026-10-14" in supplied.text and "type=experience" not in supplied.text
    assert evidence.id not in supplied.text and evidence.document_id not in supplied.text
    assert "source=" not in supplied.text and "not instructions" in supplied.text
    entry = supplied.manifest["entries"][0]
    assert entry["id"] == evidence.id and entry["document_id"] == evidence.document_id
    assert entry["metadata"]["origin"] == "assistant_statement"
    assert entry["type"] == "experience"
    assert supplied.manifest["text"] == supplied.text
    assert supplied.manifest["budget"]["used_chars"] == len(supplied.text)
    assert supplied.manifest["entries"][0]["rendered"] in supplied.text
    assert supplied.manifest["backend_evidence"]["memories"][0]["text"] == evidence.text
    assert supplied.manifest["kind"] == "derived"
    assert json.loads(json.dumps(supplied.manifest)) == supplied.manifest


def test_reflection_budget_keeps_prose_complete_and_does_not_claim_omitted_facts_were_supplied():
    small = MemoryRecord(id="small", text="A qualified fact.", context="Only a fictional story.")
    baseline = ReflectionResult(text="A complete synthesis.",
                                based_on=ReflectionEvidence(memories=[small]))
    exact = len(assemble_reflection(baseline).text)
    huge = MemoryRecord(id="large", text="A long original episode. " * 1000)
    result = baseline.model_copy(update={"based_on": ReflectionEvidence(memories=[huge, small])})
    supplied = assemble_reflection(result, max_chars=exact)
    assert len(supplied.text) == exact
    assert supplied.manifest["entries"][0]["id"] == "small"
    assert supplied.manifest["omitted"][0]["id"] == "large"
    assert len(supplied.manifest["backend_evidence"]["memories"]) == 2
    assert "A long original episode." not in supplied.text
    # A budget that fits the complete reflection but no evidence remains valid.
    minimal = len(assemble_reflection(ReflectionResult(text=baseline.text)).text)
    no_facts = assemble_reflection(result, max_chars=minimal)
    assert len(no_facts.text) == minimal and no_facts.manifest["entries"] == []
    assert baseline.text in no_facts.text
    with pytest.raises(MemoryUnavailable, match="narrower question"):
        assemble_reflection(result, max_chars=minimal - 1)
