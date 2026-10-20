"""Safety and assertion regressions for opt-in real synthetic capability checks."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "memory_capabilities", Path(__file__).resolve().parents[1] / "scripts/memory_capabilities.py")
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
CONTRACT = _module.CONTRACT
ITEMS = _module.ITEMS
CapabilityRunner = _module.CapabilityRunner
ProbeError = _module.ProbeError
contract_differences = _module.contract_differences
validate_isolation = _module.validate_isolation
validate_structured_reflection = _module.validate_structured_reflection


BANK = "eidolon-phase0-synthetic-contract-test"


def test_contract_fixture_records_deployed_schema_without_personal_data():
    fixture = json.loads(CONTRACT.read_text())
    assert fixture["provenance"]["api_version"] == "0.10.2"
    assert fixture["provenance"]["kind"] == "read-only deployed schema subset; no memory data"
    models = fixture["components"]["schemas"]
    assert models["RecallResult"]["required"] == ["id", "text"]
    assert {"document_id", "chunk_id", "occurred_start", "source_fact_ids"} <= set(
        models["RecallResult"]["properties"])
    assert {"based_on", "structured_output"} <= set(models["ReflectResponse"]["properties"])
    assert {"is_stale", "reflect_response"} <= set(models["MentalModelResponse"]["properties"])
    assert set(models["CreateMentalModelResponse"]["properties"]) == {
        "mental_model_id", "operation_id"}
    assert contract_differences(fixture, fixture) == []


def test_contract_allows_extra_optional_fields_but_detects_removed_or_changed_shapes():
    fixture = json.loads(CONTRACT.read_text())
    current = copy.deepcopy(fixture)
    properties = current["components"]["schemas"]["RecallResult"]["properties"]
    properties["future_hint"] = {"type": "string"}
    properties["text"]["description"] = "New prose does not change the wire contract."
    assert contract_differences(current, fixture) == []
    del properties["document_id"]
    properties["id"] = {"type": "integer"}
    differences = contract_differences(current, fixture)
    assert "RecallResult.document_id missing" in differences
    assert "RecallResult.id changed wire shape" in differences


def test_contract_detects_new_required_fields():
    fixture = json.loads(CONTRACT.read_text())
    current = copy.deepcopy(fixture)
    current["components"]["schemas"]["RecallRequest"]["required"].append("new_required")
    assert any("new_required" in difference for difference in contract_differences(current, fixture))


def test_isolation_accepts_separate_endpoint_and_home(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path))
    assert validate_isolation("http://127.0.0.1:9184", BANK, tmp_path, True) == tmp_path.resolve()


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:9191", "http://localhost:9191", "http://[::1]:9191",
    "http://localhost:9177",
    "http://example.com:9184", "http://user:secret@localhost:9184",
    "http://localhost:9184/personal", "http://localhost:9184?key=secret",
])
def test_isolation_refuses_live_remote_or_credential_bearing_urls(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path))
    with pytest.raises(ProbeError):
        validate_isolation(endpoint, BANK, tmp_path, True)


def test_isolation_requires_explicit_synthetic_bank_and_dedicated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path))
    with pytest.raises(ProbeError, match="Confirm|Pass"):
        validate_isolation("http://localhost:9184", BANK, tmp_path, False)
    with pytest.raises(ProbeError, match="new eidolon-phase0"):
        validate_isolation("http://localhost:9184", "personal-bank", tmp_path, True)
    (tmp_path / "config.yaml").write_text("generic: synthetic\n")
    with pytest.raises(ProbeError, match="dedicated"):
        validate_isolation("http://localhost:9184", BANK, tmp_path, True)


def test_isolation_refuses_any_git_checkout_and_mismatched_home(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: /somewhere\n")
    state = tmp_path / "state"
    monkeypatch.setenv("EIDOLON_HOME", str(state))
    with pytest.raises(ProbeError, match="all Git checkouts"):
        validate_isolation("http://localhost:9184", BANK, state, True)
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path / "another-home"))
    with pytest.raises(ProbeError, match="same isolated directory"):
        validate_isolation("http://localhost:9184", BANK, state, True)


def test_reflection_requires_structured_answer_with_resolvable_evidence_ids():
    response = {"structured_output": {"answer": "The notebook is violet.", "source_ids": ["fact-1"]},
                "based_on": {"memories": [{"id": "fact-1", "text": "The notebook is violet."}]}}
    assert validate_structured_reflection(response) == ["fact-1"]
    response["structured_output"]["source_ids"] = ["invented-fact"]
    with pytest.raises(ProbeError, match="absent from its evidence"):
        validate_structured_reflection(response)
    response["structured_output"]["source_ids"] = []
    with pytest.raises(ProbeError, match="nonempty source IDs"):
        validate_structured_reflection(response)


@pytest.mark.parametrize("answer", [{}, {"structured_output": {"answer": "blue", "source_ids": []}},
                                    {"structured_output_error": "synthetic malformed provider output"}])
def test_reflection_http_success_without_required_content_is_failed(answer):
    with pytest.raises(ProbeError):
        validate_structured_reflection(answer)


async def test_reused_synthetic_bank_is_refused_before_mutations(tmp_path):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"bank_id": BANK, "total_documents": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("seed", runner.seed) is False
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].url.path.endswith("/stats")


async def test_async_retain_requires_replay_operation_identity(tmp_path):
    count = 0

    def handle(request):
        nonlocal count
        if request.method == "GET":
            return httpx.Response(404, json={})
        if request.method == "PUT":
            return httpx.Response(200, json={"bank_id": BANK})
        count += 1
        body = json.loads(request.content)
        assert body["async"] is True
        assert all(item["update_mode"] == "replace" for item in body["items"])
        return httpx.Response(200, json={"async": True,
            "operation_id": body["operation_id"] if count == 1 else "different-operation"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("seed", runner.seed) is False
    assert "different operation identity" in runner.report["checks"][0]["reason"]


async def test_source_expansion_rejects_mismatched_original_document(tmp_path):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"results": [{"id": "fact", "document_id": "synthetic-current",
                "chunk_id": "chunk"}], "chunks": {"chunk": {"text": ITEMS[1]["content"]}}})
        return httpx.Response(200, json={"document_id": "another-source", "chunk_text": ITEMS[1]["content"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("sources", runner.source_expansion) is False
    assert "source identity" in runner.report["checks"][0]["reason"]


@pytest.mark.parametrize("generated_ids", [[], ["invented-fact"]])
async def test_valid_reflection_evidence_with_unbound_generated_ids_is_a_limitation(tmp_path, generated_ids):
    resolved = []

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={
                "structured_output": {"answer": "The notebook is violet.", "source_ids": generated_ids},
                "based_on": {"memories": [{"id": "real-memory-uuid", "text": "Violet notebook"}]}})
        resolved.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"document_id": "synthetic-current"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("reflection", runner.reflection) is False
    result = runner.report["checks"][0]
    assert result["status"] == "limitation"
    assert result["structured_source_ids_rejected"] is True
    assert result["observed_structured_source_ids"] == generated_ids
    assert result["evidence_source_ids_verified"] == ["real-memory-uuid"]
    assert resolved == ["real-memory-uuid"]


async def test_model_creation_wrapper_is_polled_before_preview_or_refresh(tmp_path):
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/mental-models"):
            return httpx.Response(200, json={"mental_model_id": "synthetic-keepsake",
                                             "operation_id": "creation-op"})
        if "/operations/" in path:
            return httpx.Response(200, json={"operation_id": path.rsplit("/", 1)[-1],
                                             "status": "completed"})
        if path.endswith("/dry-run-refresh"):
            return httpx.Response(200, json={"preview_content": "A striped obsidian pebble."})
        if path.endswith("/refresh"):
            return httpx.Response(200, json={"operation_id": "refresh-op"})
        return httpx.Response(200, json={"id": "synthetic-keepsake", "content": "An obsidian pebble.",
            "last_refreshed_at": "2026-10-14T01:00:00Z", "last_memory_seen_at": "2026-10-14T00:59:00Z",
            "reflect_response": {"based_on": {"world": [{"id": "fact"}]}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("model", runner.refresh_model) is True
    creation_poll = next(index for index, (_, path) in enumerate(requests)
                         if path.endswith("/operations/creation-op"))
    preview = next(index for index, (_, path) in enumerate(requests)
                   if path.endswith("/dry-run-refresh"))
    assert creation_poll < preview


async def test_deletion_stale_summary_is_a_limitation_and_clear_is_verified(tmp_path):
    cleared = False

    def handle(request):
        nonlocal cleared
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        if "/documents/" in request.url.path:
            return httpx.Response(404, json={})
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": []})
        if request.url.path.endswith("/clear"):
            cleared = True
            return httpx.Response(200, json={"content": None})
        return httpx.Response(200, json={"content": "The keepsake is an obsidian pebble.", "is_stale": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("delete", runner.deletion) is False
    result = runner.report["checks"][0]
    assert result["status"] == "limitation"
    assert result["summary_retained_deleted_claim"] is True
    assert result["stale_flag_before_deletion"] is False
    assert result["explicit_clear_verified"] is True and cleared


async def test_scoped_restriction_is_not_claimed_without_scoped_credentials(tmp_path):
    def handle(request):
        pytest.fail("No network request should occur without a scoped credential")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        assert await runner.check("scope", runner.scoped_admin) is False
    assert runner.report["checks"][0]["status"] == "not_run"


async def test_error_reports_never_echo_backend_bodies_or_credentials(tmp_path):
    def handle(request):
        return httpx.Response(503, json={"detail": "postgres://secret:credential@private.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)

        async def request():
            await runner.request("GET", runner.path + "/stats")

        assert await runner.check("request", request) is False
    text = (tmp_path / (BANK + "-capabilities.json")).read_text()
    assert "HTTP 503" in text
    assert "credential" not in text and "private.example" not in text


async def test_report_writes_do_not_follow_a_predictable_temporary_symlink(tmp_path):
    protected = tmp_path / "another-file"
    protected.write_text("Keep this unrelated file intact.")
    target = tmp_path / (BANK + "-capabilities.json")
    target.with_suffix(".tmp").symlink_to(protected)
    async with httpx.AsyncClient(base_url="http://localhost:9184") as client:
        runner = CapabilityRunner(client, BANK, tmp_path)
        runner.save()
    assert protected.read_text() == "Keep this unrelated file intact."
    assert json.loads(target.read_text())["bank"] == BANK
    assert target.stat().st_mode & 0o777 == 0o600
