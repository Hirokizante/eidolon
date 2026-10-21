"""First-attempt evaluation and reporting, with no personal state or network."""
import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from eidolon.config import AppConfig, AuxiliaryModelConfig, ProviderConfig


SCRIPTS = Path(__file__).parents[1] / "scripts"
for name in ("memory_baseline", "maintenance_baseline"):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
baseline = sys.modules["maintenance_baseline"]


@pytest.fixture
def workload():
    return baseline.load_workload()


async def test_scripted_workload_exercises_real_storage_without_contacting_providers(
    tmp_path, monkeypatch, workload,
):
    def no_network(*args, **kwargs):
        pytest.fail("Offline baseline attempted an HTTP request")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    cases, sources, _ = workload
    assert {case["category"] for case in cases["cases"]} == {
        "admissions", "updates", "closures", "justified_no_change", "protected_notes",
        "capacity_replacement", "legacy_work", "metadata", "uncertainty", "attribution",
        "paraphrases",
    }
    results = {}
    for case in cases["cases"]:
        row = await baseline.evaluate_case(case, sources, tmp_path / case["id"],
                                            baseline.read_profile(None, live=False))
        results[case["id"]] = row
        assert row["requests"] == 1
        assert row["review"] is None
        assert row["usage"] is None
    for key in ("admission", "correction", "closure", "small-talk", "replacement",
                "capacity-review", "legacy-conversion", "supported-metadata",
                "uncertain-admission", "speaker-ownership", "paraphrased-update"):
        assert results[key]["expectation_match"], (key, results[key]["checks"])
    # Keep the desired closure expectation as the validator improves. Historical
    # failures belong in the captured baseline, never in production requirements.
    paraphrase = results["paraphrased-closure"]
    assert paraphrase["expectation_match"] == (paraphrase["outcome"] == "accepted_change")
    summary = baseline.summarize(list(results.values()))
    assert summary["samples"] == 17
    assert summary["requests"] == 17
    assert summary["useful_faithful_rate"] is None
    assert summary["unsafe_rate"] is None


def sse(text, *, finish="stop", usage=None):
    packets = [{"choices": [{"delta": {"content": text}, "finish_reason": finish}]}]
    if usage is not None:
        packets.append({"choices": [], "usage": usage})
    return "".join("data: " + json.dumps(packet) + "\n\n" for packet in packets) + "data: [DONE]\n\n"


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(200, text=sse("")), "empty"),
    (httpx.Response(200, text=sse("{broken")), "malformed"),
    (httpx.Response(200, text=sse("[]")), "malformed"),
    (httpx.Response(200, text=sse('{"operations": []}')), "validation_rejection"),
    (httpx.Response(200, text=sse("{partial", finish="length")), "truncated"),
    (httpx.Response(200, text=sse('{"operations":[', finish=None)), "truncated"),
    (httpx.Response(422, text="synthetic rejected options"), "provider_rejection"),
    (httpx.Response(401, text="synthetic private provider error"), "provider_rejection"),
    (httpx.Response(429, text="synthetic private rate error"), "provider_rejection"),
    (httpx.Response(503, text="synthetic private unavailable"), "provider_rejection"),
])
async def test_live_adapter_records_one_failed_dispatch_and_never_recovers(
    tmp_path, workload, response, expected,
):
    fixture, sources, _ = workload
    requests = []
    def handler(request):
        requests.append(request)
        return response
    row = await baseline.evaluate_case(fixture["cases"][0], sources, tmp_path / "trial",
        AppConfig(), live=True, transport=httpx.MockTransport(handler))
    assert row["outcome"] == expected
    assert row["requests"] == len(requests) == 1
    assert row["provider_invocations"] == 1
    assert not row["committed"] and row["after"] == row["before"]
    assert row["usage"] is None
    assert "synthetic private" not in json.dumps(row)


async def test_silent_no_change_is_accepted_but_does_not_satisfy_useful_admission(
    tmp_path, workload,
):
    fixture, sources, _ = workload
    row = await baseline.evaluate_case(fixture["cases"][0], sources, tmp_path / "no-change",
        AppConfig(), live=True, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text=sse('{"operations":[{"op":"no_change"}]}',
                usage={"prompt_tokens": 100, "completion_tokens": 25}))))
    assert row["outcome"] == "accepted_no_change"
    assert not row["expectation_match"]
    assert row["usage"] == {"prompt_tokens": 100, "completion_tokens": 25}
    report = {"mode": "live", "complete": True,
              "rows": [{**row, "sample_id": "admission:1"}]}
    reviewed = baseline.apply_reviews(report, {"admission:1": {
        "verdict": "unresolved", "rationale": "The upcoming interview was omitted."}})
    assert reviewed["summary"]["useful_faithful_rate"] == 0
    assert reviewed["summary"]["reviewed_verdicts"]["unresolved"] == 1


async def test_compact_request_fits_8000_characters_but_large_source_remains_unresolved(tmp_path, workload):
    fixture, sources, _ = workload
    config = AppConfig(auxiliary=AuxiliaryModelConfig(context_chars=8000))
    fitting = await baseline.evaluate_case(fixture["cases"][0], sources, tmp_path / "fitting", config)
    assert fitting["outcome"] == "accepted_change"
    assert fitting["requests"] == 1
    sources = copy.deepcopy(sources)
    source_id = fixture["cases"][0]["inputs"][0]
    sources[source_id]["user"] += " Synthetic preparation detail." * 1000
    row = await baseline.evaluate_case(fixture["cases"][0], sources, tmp_path / "limit", config)
    assert row["outcome"] == "unresolved"
    assert row["requests"] == row["provider_invocations"] == 0
    assert row["usage"] is None


async def test_live_mode_requires_explicit_consent_before_reading_state(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path / "uncreated"))
    with pytest.raises(ValueError, match="allow-real-model"):
        await baseline.run(argparse.Namespace(mode="live", allow_real_model=False))
    assert not (tmp_path / "uncreated").exists()
    with pytest.raises(ValueError, match="explicit provider"):
        baseline.read_profile(None, live=True)
    with pytest.raises(ValueError, match="only read"):
        baseline.read_profile(tmp_path / "personal.yaml", live=False)


async def test_report_versions_workload_and_records_each_independent_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path / "synthetic"))
    path = await baseline.run(argparse.Namespace(mode="scripted", allow_real_model=False,
                                               provider_config=None, repetitions=2))
    report = json.loads(path.read_text())
    assert report["schema_version"] == 1
    assert report["identity"]["model"] == "scripted-fixture-v1"
    assert report["recovery_enabled"] is False
    assert len(report["application"]["revision"]) == 40
    assert len(report["workload_sha256"]) == 64
    assert len(report["rows"]) == len({r["sample_id"] for r in report["rows"]}) == 34
    assert report["summary"]["requests"] == 34
    assert report["summary"]["reviewed_samples"] == 0
    assert (path.parent / "sources.json").is_file()
    assert (path.parent / "workload.json").is_file()


def test_partial_usage_and_unreviewed_samples_cannot_be_presented_as_known_totals():
    rows = [{"outcome": "accepted_no_change", "category": "admissions", "requests": 1,
             "expectation_match": False, "latency_seconds": 1, "review": None,
             "usage": {"prompt_tokens": 12}},
            {"outcome": "empty", "category": "admissions", "requests": 1,
             "expectation_match": False, "latency_seconds": 3, "review": None, "usage": None}]
    summary = baseline.summarize(rows)
    assert summary["usage"]["prompt_tokens"] == {"known_samples": 1, "sum_known": 12,
                                                "total": None}
    assert summary["cost"] is None
    assert summary["latency_seconds"] == {"count": 2, "mean": 2, "p95": 3}
    assert summary["useful_faithful_rate"] is None


def test_reviews_do_not_promote_scripted_or_failed_attempts_to_quality_results():
    review = {"a:1": {"verdict": "useful_faithful", "rationale": "A reviewed result."}}
    report = {"mode": "scripted", "rows": [{"sample_id": "a:1", "committed": True}]}
    with pytest.raises(ValueError, match="Scripted"):
        baseline.apply_reviews(report, review)
    failed = copy.deepcopy(report)
    failed.update(mode="live", complete=True)
    failed["rows"][0]["committed"] = False
    with pytest.raises(ValueError, match="unresolved"):
        baseline.apply_reviews(failed, review)


async def test_transport_forbids_extra_http_dispatches():
    calls = []
    transport = baseline.SingleDispatchTransport(httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://synthetic.invalid/v1/chat/completions")
        with pytest.raises(RuntimeError, match="Recovery is disabled"):
            await client.post("https://synthetic.invalid/v1/chat/completions")
    assert len(calls) == 1


def test_profile_uses_explicit_auxiliary_route_without_loading_personal_prompts(tmp_path):
    path = tmp_path / "trial.yaml"
    path.write_text("""provider: dialogue
providers:
  - name: dialogue
    model: unused-dialogue
  - name: auxiliary
    endpoint: https://synthetic.invalid/v1
    model: unused-base
auxiliary:
  mode: separate
  provider: auxiliary
  model: exact-trial-model
prompts:
  SYSTEM: /unread/personal-prompt.md
""")
    config = baseline.read_profile(path, live=True)
    route = baseline.resolve_auxiliary(config, revision=1)
    assert config.prompts == {}
    assert config.identity.user_name == "You"
    assert route.mode == "separate" and route.model == "exact-trial-model"
    assert route.profile().endpoint == "https://synthetic.invalid/v1"
    assert route.profile().api_key_ref is None
    assert isinstance(route.profile(), ProviderConfig)


async def test_unsafe_protected_edit_is_rejected_and_last_valid_note_survives(tmp_path, workload):
    fixture, sources, _ = workload
    case = next(case for case in fixture["cases"] if case["id"] == "authored-protection")
    def handler(request):
        document = json.loads(json.loads(request.content)["messages"][-1]["content"])
        item = document["items"][0]
        source = document["exchanges"][0]
        operation = baseline.upsert({"topic": item["topic"], "content": "Unsupported edit."}, source)
        operation.update(item_id=item["id"], expected_revision=item["revision"])
        operation = baseline.compact_fixture_response({"operations": [operation]}, document)["operations"][0]
        return httpx.Response(200, text=sse(json.dumps({"operations": [operation]})))
    row = await baseline.evaluate_case(case, sources, tmp_path / "protected", AppConfig(),
                                       live=True, transport=httpx.MockTransport(handler))
    assert row["outcome"] == "validation_rejection"
    assert row["before"] == row["after"]
    assert row["checks"]["protected"]
    report = {"mode": "live", "complete": True,
              "rows": [{**row, "sample_id": "authored-protection:1"}]}
    summary = baseline.apply_reviews(report, {"authored-protection:1": {
        "verdict": "unsafe", "rationale": "The proposal attempted a prohibited authored edit; "
                                             "local validation prevented persistence."}})["summary"]
    assert summary["unsafe_rate"] == 1
    assert summary["useful_faithful_rate"] == 0


async def test_live_cost_estimate_uses_only_reported_tokens_and_explicit_prices(
    tmp_path, monkeypatch, workload,
):
    fixture, sources, digest = workload
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path / "cost-state"))
    monkeypatch.setattr(baseline, "load_workload", lambda: (
        {**fixture, "cases": fixture["cases"][:1]}, sources, digest))
    monkeypatch.setattr(baseline, "read_profile", lambda *args, **kwargs: AppConfig())
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text=sse(
            '{"operations":[{"op":"no_change"}]}',
            usage={"prompt_tokens": 100, "completion_tokens": 25}))))
    path = await baseline.run(argparse.Namespace(mode="live", allow_real_model=True,
        provider_config=None, repetitions=1, input_usd_per_million=10, output_usd_per_million=20))
    report = json.loads(path.read_text())
    assert report["complete"]
    assert report["planned_samples"] == 1
    assert report["summary"]["estimated_cost_usd"]["total"] == pytest.approx(0.0015)
    assert report["summary"]["cost"] is None
    assert report["summary"]["useful_faithful_rate"] is None
