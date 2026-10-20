"""Isolation, evidence scoring and replay tests; no live memory or provider requests."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from eidolon.config import AppConfig, MemoryConfig
from eidolon.memory.hindsight import HindsightMemory
from eidolon.storage.store import Store


spec = importlib.util.spec_from_file_location(
    "memory_baseline", Path(__file__).parents[1] / "scripts/memory_baseline.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
sys.modules["memory_baseline"] = baseline
comparison_spec = importlib.util.spec_from_file_location(
    "memory_baseline_compare", Path(__file__).parents[1] / "scripts/memory_baseline_compare.py")
comparison = importlib.util.module_from_spec(comparison_spec)
comparison_spec.loader.exec_module(comparison)


def test_fixture_contains_dated_synthetic_evidence_and_all_probe_categories():
    fixture, digest = baseline.load_fixture()
    assert len(digest) == 64
    assert len(fixture["probes"]) == 72
    assert {p["category"] for p in fixture["probes"]} == {
        "daily_boundary", "restart", "temporal", "preferences", "shared_episodes", "fiction",
        "attribution", "contradictions", "multilingual", "weeks", "irrelevance", "unknown",
    }
    assert sum(p["restart"] for p in fixture["probes"]) == 12
    assert all(p["reply_rubric"] for p in fixture["probes"])
    assert all(not p["expected_atoms"] for p in fixture["probes"] if p["category"] == "unknown")


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:9191", "http://127.0.0.1:9177", "http://localhost",
    "https://memory.example:9183", "http://user:password@localhost:9183",
    "http://localhost:9183/v1", "http://localhost:9183?key=secret",
])
def test_baseline_refuses_live_or_ambiguous_endpoint(endpoint):
    with pytest.raises(ValueError):
        baseline.isolated_endpoint(endpoint)


def test_baseline_requires_empty_outside_git_state_and_synthetic_marker(tmp_path):
    fixture_hash = "fixture-sha256"
    with pytest.raises(ValueError, match="Explicit"):
        baseline.isolated_home(None, fixture_hash)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="outside"):
        baseline.isolated_home(str(checkout / "state"), fixture_hash)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "config.yaml").write_text("private-placeholder")
    with pytest.raises(ValueError, match="without"):
        baseline.isolated_home(str(unrelated), fixture_hash)
    state = tmp_path / "synthetic-state"
    assert baseline.isolated_home(str(state), fixture_hash) == state.resolve()
    assert baseline.isolated_home(str(state), fixture_hash) == state.resolve()
    with pytest.raises(ValueError, match="match"):
        baseline.isolated_home(str(state), "different-fixture")
    (state / "config.yaml").write_text("private-placeholder")
    with pytest.raises(ValueError, match="unrelated"):
        baseline.isolated_home(str(state), fixture_hash)


def test_live_app_tree_and_bank_are_refused():
    with pytest.raises(ValueError, match="live"):
        baseline.isolated_home(str(Path.home() / "Library/Application Support/Eidolon/test"), "h")
    for bank in ["hermes", "synthetic", "eidolon-phase0-synthetic-", "eidolon-phase0-synthetic-a/b"]:
        with pytest.raises(ValueError):
            baseline.synthetic_bank(bank)
    assert baseline.synthetic_bank("eidolon-phase0-synthetic-test") == "eidolon-phase0-synthetic-test"


async def test_fresh_synthetic_banks_have_unique_names_and_matching_identity():
    banks = []
    names = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": list(banks)})
        body = json.loads(request.content)
        assert body["name"] not in names  # Identity markers must distinguish fixture banks.
        names.append(body["name"])
        bank = request.url.path.rsplit("/", 1)[-1]
        created = {"bank_id": bank, "name": body["name"]}
        banks.append(created)
        return httpx.Response(200, json=created)

    async with httpx.AsyncClient(base_url="http://synthetic.test",
                                transport=httpx.MockTransport(handler)) as client:
        for bank in ("eidolon-phase0-synthetic-first", "eidolon-phase0-synthetic-second"):
            memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank=bank), client=client)
            await baseline.prepare_bank(memory, reuse=False)
            await baseline.prepare_bank(memory, reuse=True)
    assert len(set(names)) == 2


@pytest.mark.parametrize("created", [{"bank_id": "another-bank"}, {}])
async def test_bank_creation_alias_redirect_or_missing_identity_is_refused(created):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"banks": []} if request.method == "GET" else created)

    async with httpx.AsyncClient(base_url="http://synthetic.test",
                                transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="eidolon-phase0-synthetic-new"), client=client)
        with pytest.raises(ValueError, match="bank identity"):
            await baseline.prepare_bank(memory, reuse=False)
    assert [r.method for r in requests] == ["GET", "PUT"]


def test_atoms_are_evidence_coverage_not_answer_correctness():
    score = baseline.atom_score("Old jasmine; NEW PEPPERMINT", [["peppermint"], ["Jan 15"]])
    assert score == {"matched": [True, False], "coverage": .5}
    assert baseline.atom_score("some unrelated text", []) == {"matched": [], "coverage": None}


def test_seeding_preserves_source_dates_room_grouping_and_restart(tmp_path):
    fixture, digest = baseline.load_fixture()
    bank = "eidolon-phase0-synthetic-unit"
    path = tmp_path / "synthetic.sqlite"
    store = Store(path)
    documents = baseline.seed_store(store, fixture, bank=bank, fixture_hash=digest)
    store.close()
    store = Store(path)
    try:
        assert len(store.pending_outbox(limit=100)) == len(fixture["sources"])
        messages = store.list_messages("day:2026-01-02")
        assert len(messages) == 6  # Headphones, interview, then 3:30 AM calendar statement.
        assert messages[-2]["created_at"] == "2026-01-03T03:30:00+08:00"
        source = next(s for s in fixture["sources"] if s["id"] == "headphones")
        assert documents["headphones"].endswith(baseline.source_identity(
            fixture, source, bank=bank, fixture_hash=digest))
        filtered = baseline.ProbeStore(store, baseline.parse_time("2026-01-03T04:05:00+08:00"))
        assert not filtered.list_messages("day:2026-01-03")  # Evening resolution is future.
    finally:
        store.close()


def test_source_documents_and_operation_uuids_are_bank_scoped_and_stable(tmp_path):
    fixture, digest = baseline.load_fixture()
    identities = []
    for index, bank in enumerate(["eidolon-phase0-synthetic-one", "eidolon-phase0-synthetic-two",
                                  "eidolon-phase0-synthetic-one"]):
        store = Store(tmp_path / f"synthetic-{index}.sqlite")
        try:
            documents = baseline.seed_store(store, fixture, bank=bank, fixture_hash=digest)
            operations = {row["operation_id"] for row in store.pending_outbox(limit=100)}
            identities.append((set(documents.values()), operations))
        finally:
            store.close()
    assert identities[0][0].isdisjoint(identities[1][0])
    assert identities[0][1].isdisjoint(identities[1][1])
    assert identities[0] == identities[2]
    source = fixture["sources"][0]
    assert baseline.source_identity(fixture, source, bank="eidolon-phase0-synthetic-one",
                                    fixture_hash=digest) != baseline.source_identity(
        fixture, source, bank="eidolon-phase0-synthetic-one", fixture_hash="revised-fixture")


@pytest.mark.parametrize("recalled", ["Ember Library interview at 10 AM on January 3.", ""])
async def test_real_engine_recall_and_prompt_use_frozen_clock_without_retaining_probes(
        tmp_path, recalled):
    fixture, digest = baseline.load_fixture()
    store = Store(tmp_path / "synthetic.sqlite")
    bank = "eidolon-phase0-synthetic-unit"
    documents = baseline.seed_store(store, fixture, bank=bank, fixture_hash=digest)
    probe = fixture["probes"][1]  # Its question itself contains both expected evidence atoms.
    before = store.db.execute("SELECT count(*) FROM generations").fetchone()[0]
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": bank}]})
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"results": [{"id": "test-fact", "text": recalled}]})

    profile = baseline.provider_profile(None, fixture)
    config = AppConfig(state_dir=tmp_path, provider=profile.name, providers=[profile],
                       prompts={name: str(baseline.ROOT / "src/eidolon/defaults" / (name + ".md"))
                                for name in ("SYSTEM", "SOUL", "USER", "POST_HISTORY")},
                       memory=MemoryConfig(enabled=True, save_enabled=True, bank=bank))
    try:
        async with httpx.AsyncClient(base_url="http://synthetic.test",
                                    transport=httpx.MockTransport(handler)) as client:
            memory = HindsightMemory(config.memory, client=client)
            row = await baseline.evaluate_probe(config, store, memory, probe, fixture, documents)
        assert "error" not in row
        assert row["session_id"] == "day:2026-01-03"
        assert "Recent conversation:" not in row["query"]
        assert seen[0]["query_timestamp"] == "2026-01-02T20:05:00+00:00"
        assert any("2026-01-03T04:05:00+08:00" in message["content"]
                   for message in row["messages"]
                   if message["role"] == "user" and "# Runtime context\n" in message["content"])
        assert row["recall_evidence"]["coverage"] == (1 if recalled else 0)
        assert row["supplied_evidence"]["coverage"] == (1 if recalled else 0)
        assert row["question_atom_overlap"]["coverage"] == 1
        assert row["context_tokens"] is None
        assert row["recall_latency_ms"] >= 0
        assert store.db.execute("SELECT count(*) FROM generations").fetchone()[0] == before
        assert store.outbox_status()["pending"] == len(fixture["sources"])
    finally:
        store.close()


async def test_final_replies_are_bounded_and_scored_separately(monkeypatch):
    active, maximum = 0, 0

    async def reply(profile, messages):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.01)
        active -= 1
        return {"text": "peppermint", "usage": {"prompt_tokens": 10}, "latency_ms": 1}

    monkeypatch.setattr(baseline, "final_reply", reply)
    rows = [{"id": f"p{i}", "messages": [], "expected_atoms": [["peppermint"]],
             "forbidden_reply_atoms": ["jasmine"]} for i in range(5)]
    updates = []
    await baseline.evaluate_replies(rows, None, concurrency=2, on_update=updates.append)
    assert maximum == 2
    assert len(updates) == 5
    assert all(row["reply"]["atom_checks"]["coverage"] == 1 for row in rows)
    assert all(row["reply"]["forbidden_atom_hits"] == [] for row in rows)
    assert all("recall_evidence" not in row for row in rows)


@pytest.mark.parametrize("separate_context", [False, True])
def test_comparison_requires_same_corpus_model_and_questions_and_rescores_overlap(separate_context):
    fixture, digest = baseline.load_fixture()
    report = {"synthetic": True, "fixture_sha256": digest, "hindsight_version": "0.10.2",
              "bank": "eidolon-phase0-synthetic-unit", "provider": {"model": "test-model"},
              "rows": [dict(p, query=p["question"], expected_document_ids=p["expected_source_ids"],
                            recalled_context="", messages=[
                                {"role": "system", "content": "generic"},
                                {"role": "system", "content": "generic"},
                                {"role": "system", "content": "generic"},
                                {"role": "user", "content": p["question"]},
                                {"role": "system", "content": "generic"}])
                       for p in fixture["probes"]]}
    if separate_context:
        for row in report["rows"]:
            row["messages"].insert(3, {"role": "system", "content": (
                "# Application context\n\n" + row["question"]
            )})
    candidate = copy.deepcopy(report)
    candidate["hindsight_version"] = "1.0.0"
    result = comparison.compare_reports(copy.deepcopy(report), candidate)
    assert result["baseline_version"] == "0.10.2"
    assert result["candidate_version"] == "1.0.0"
    assert result["rows"][1]["baseline_recall_coverage"] == 0
    assert candidate["rows"][1]["question_atom_overlap"]["coverage"] == 1
    assert candidate["rows"][1]["supplied_evidence"]["coverage"] == 0
    candidate["provider"]["model"] = "different-model"
    with pytest.raises(ValueError, match="model"):
        comparison.compare_reports(report, candidate)
    candidate["provider"]["model"] = "test-model"
    candidate["rows"][0]["query"] = "changed question"
    with pytest.raises(ValueError, match="query"):
        comparison.compare_reports(report, candidate)


def test_no_replies_cli_preserves_provider_profile_without_completion_requests(tmp_path, monkeypatch):
    bank = "eidolon-phase0-synthetic-timing"
    state = tmp_path / "timing-home"
    provider_path = tmp_path / "provider.yaml"
    provider_path.write_text("""provider: evaluation
providers:
  - name: evaluation
    endpoint: http://provider.invalid/v1
    model: stable-evaluation-model
    context_chars: 32000
    parameters:
      temperature: 0.25
      seed: 8
""")
    monkeypatch.setenv("EIDOLON_HOME", str(state))
    banks = []
    completed_requests = []

    def backend(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"info": {"version": "0.10.2"}})
        if request.method == "GET":
            return httpx.Response(200, json={"banks": list(banks)})
        if request.method == "PUT":
            created = {"bank_id": bank, "name": json.loads(request.content)["name"]}
            banks.append(created)
            return httpx.Response(200, json=created)
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": []})
        assert request.url.path.endswith("/memories")
        return httpx.Response(200, json={"async": False, "success": True})

    original_client = httpx.AsyncClient

    def mocked_client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(backend))

    async def unexpected_reply(*args, **kwargs):
        completed_requests.append(True)
        raise AssertionError("Timing-only mode must not generate final replies")

    monkeypatch.setattr(baseline.httpx, "AsyncClient", mocked_client)
    monkeypatch.setattr(baseline, "final_reply", unexpected_reply)
    result = baseline.main(["--url", "http://127.0.0.1:9183", "--bank", bank,
                            "--label", "timing-only", "--provider-config", str(provider_path),
                            "--no-replies"])
    assert result == 0
    assert completed_requests == []
    report = json.loads((state / "runs" / bank / "timing-only.json").read_text())
    assert report["provider"]["model"] == "stable-evaluation-model"
    assert report["provider"]["context_window"] == 32768
    assert report["provider"]["parameters"] == {"temperature": .25, "seed": 8}
    assert report["provider"]["replies_enabled"] is False
    assert len(report["rows"]) == 72
    assert all(row["reply"] is None and row["messages"] for row in report["rows"])
    assert report["summary"]["reply_latency_ms"]["count"] == 0
