"""Exercise the deployed memory contract using only a new synthetic, isolated bank.

Run with ``uv run python scripts/memory_capabilities.py --help``. A real run creates
synthetic documents and a model, then removes one source to characterize deletion.
It never loads Eidolon's configuration, enumerates banks, or queries personal memories.
Successful HTTP responses alone do not count as capability verification.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx


CONTRACT = Path(__file__).resolve().parents[1] / "tests/fixtures/hindsight_openapi_0_10_2.json"
BANK_PREFIX = "eidolon-phase0-synthetic-"
SCOPE = "phase0-real-life"
SOURCE_TEXT = "Mira's current notebook has a violet cover and a star shaped clasp."
LEGACY_TEXT = "Mira carries an amber thermos when visiting the Observatory garden."
ERASED_TEXT = "Mira's invented keepsake is a striped obsidian pebble."
ITEMS = [
    {"document_id": "synthetic-legacy", "content": LEGACY_TEXT,
     "timestamp": "2026-09-20T16:30:00Z", "tags": []},
    {"document_id": "synthetic-current", "content": SOURCE_TEXT,
     "timestamp": "2026-10-12T20:05:00Z", "tags": [SCOPE]},
    {"document_id": "synthetic-erased", "content": ERASED_TEXT,
     "timestamp": "2026-10-12T19:55:00Z", "tags": ["phase0-forget"]},
]


class ProbeError(RuntimeError):
    """A sanitized contract or behavioral assertion failed."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def validate_isolation(base_url: str, bank: str, state_dir: Path, acknowledged: bool) -> Path:
    """Reject the live endpoint, reused/non-synthetic banks, and checkout-local state."""
    parsed = urlparse(base_url)
    require(acknowledged, "Pass --isolated-instance only for a separate test service/database")
    require(parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"},
            "Only an explicit local isolated HTTP endpoint is supported")
    require(parsed.port not in {None, 9177, 9191},
            "The live ports 9177 and 9191 cannot be used by this runner")
    require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "The endpoint must not contain credentials, query parameters, or fragments")
    require(parsed.path in {"", "/"}, "The endpoint must be an origin without a path")
    require(re.fullmatch(r"eidolon-phase0-synthetic-[a-z0-9][a-z0-9-]{7,80}", bank),
            "Choose a new eidolon-phase0-synthetic- bank with a unique suffix")
    require(state_dir.is_absolute(), "--state-dir must be absolute")
    state_dir = state_dir.resolve()
    require(os.environ.get("EIDOLON_HOME") is not None,
            "Set EIDOLON_HOME to the same isolated directory as --state-dir")
    require(Path(os.environ["EIDOLON_HOME"]).expanduser().resolve() == state_dir,
            "EIDOLON_HOME and --state-dir must identify the same isolated directory")
    # Check both the state location and our own checkout, including linked worktrees.
    repo = Path(__file__).resolve().parents[1]
    require(not state_dir.is_relative_to(repo), "Synthetic state must be outside this checkout")
    for ancestor in (state_dir, *state_dir.parents):
        require(not (ancestor / ".git").exists(), "Synthetic state must be outside all Git checkouts")
    require(not (state_dir / "config.yaml").exists() and not (state_dir / "room.sqlite3").exists(),
            "Use dedicated evaluation state rather than an existing Eidolon home")
    return state_dir


def contract_differences(schema: dict, baseline: dict) -> list[str]:
    """Compare used HTTP shapes; added optional fields are forward compatible."""
    differences: list[str] = []
    current = schema.get("components", {}).get("schemas", {})
    for name, expected in baseline["components"]["schemas"].items():
        actual = current.get(name)
        if not isinstance(actual, dict):
            differences.append(f"missing schema {name}")
            continue
        # New required fields can break an older request producer/response consumer.
        extra_required = set(actual.get("required", [])) - set(expected.get("required", []))
        if extra_required:
            differences.append(f"{name} added required fields: {', '.join(sorted(extra_required))}")
        for field, shape in expected.get("properties", {}).items():
            observed = actual.get("properties", {}).get(field)
            if observed is None:
                differences.append(f"{name}.{field} missing")
            elif wire_shape(observed) != wire_shape(shape):
                differences.append(f"{name}.{field} changed wire shape")
        if "properties" not in expected and wire_shape(actual) != wire_shape(expected):
            differences.append(f"{name} changed wire shape")
    for path, methods in baseline["paths"].items():
        for method, operation in methods.items():
            actual = schema.get("paths", {}).get(path, {}).get(method)
            if not actual:
                differences.append(f"missing {method.upper()} {path}")
                continue
            for kind in ("requestBody", "responses"):
                if kind == "requestBody":
                    expected_schema = operation.get(kind, {}).get("content", {}).get(
                        "application/json", {}).get("schema")
                    actual_schema = actual.get(kind, {}).get("content", {}).get(
                        "application/json", {}).get("schema")
                    if wire_shape(expected_schema) != wire_shape(actual_schema):
                        differences.append(f"{method.upper()} {path} changed request shape")
                else:
                    for status, expected_response in operation[kind].items():
                        if status.startswith("2"):
                            expected_schema = expected_response.get("content", {}).get(
                                "application/json", {}).get("schema")
                            actual_schema = actual.get(kind, {}).get(status, {}).get("content", {}).get(
                                "application/json", {}).get("schema")
                            if wire_shape(expected_schema) != wire_shape(actual_schema):
                                differences.append(f"{method.upper()} {path} changed {status} shape")
    return differences


def wire_shape(value: Any) -> Any:
    """Discard prose/labels while retaining types, bounds, defaults, and references."""
    ignored = {"title", "description", "example", "examples", "deprecated"}
    if isinstance(value, dict):
        return {key: ({name: wire_shape(shape) for name, shape in item.items()}
                      if key == "properties" else wire_shape(item))
                for key, item in value.items() if key not in ignored}
    if isinstance(value, list):
        return [wire_shape(item) for item in value]
    return value


def validate_structured_reflection(response: dict) -> list[str]:
    output = response.get("structured_output")
    require(isinstance(output, dict), "Reflection did not produce its requested structured output")
    require(set(output) == {"answer", "source_ids"}, "Reflection output does not match the schema")
    require(isinstance(output["answer"], str) and "violet" in output["answer"].lower(),
            "Reflection answer did not preserve the known synthetic notebook fact")
    ids = output["source_ids"]
    require(isinstance(ids, list) and ids and all(isinstance(item, str) and item for item in ids),
            "Reflection requires nonempty source IDs")
    based_on = response.get("based_on")
    require(isinstance(based_on, dict), "Reflection returned no evidence")
    facts = based_on.get("memories", [])
    require(isinstance(facts, list) and facts, "Reflection returned no evidence")
    evidence_ids = {fact.get("id") for fact in facts if isinstance(fact, dict)}
    require(set(ids) <= evidence_ids, "Structured reflection cites IDs absent from its evidence")
    return ids


class CapabilityRunner:
    def __init__(self, client: httpx.AsyncClient, bank: str, state_dir: Path, *,
                 poll_timeout: float = 300, expected_version: str | None = None,
                 scoped_key: str | None = None):
        self.client = client
        self.bank = bank
        self.path = "/v1/default/banks/" + quote(bank, safe="")
        self.state_dir = state_dir
        self.poll_timeout = poll_timeout
        self.expected_version = expected_version
        self.scoped_key = scoped_key
        self.report: dict = {"kind": "synthetic isolated capability checks",
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "bank": bank, "checks": [], "requests": []}
        self.model_id = "synthetic-keepsake"
        self.concurrent_model_id = "synthetic-concurrent-edit"

    async def request(self, method: str, path: str, *, expected: int = 200, **kwargs) -> dict:
        start = time.perf_counter()
        response = await self.client.request(method, path, **kwargs)
        self.report["requests"].append({"method": method, "path": path,
                                        "http_status": response.status_code,
                                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                                        "response_bytes": len(response.content)})
        require(response.status_code == expected,
                f"{method} synthetic endpoint returned HTTP {response.status_code}; expected {expected}")
        if expected == 404 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise ProbeError("Synthetic endpoint returned invalid JSON") from None
        require(isinstance(data, dict), "Synthetic endpoint response must be an object")
        return data

    async def poll(self, operation_id: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            data = await self.request("GET", self.path + "/operations/" + quote(operation_id, safe=""))
            require(data.get("operation_id", data.get("id")) == operation_id,
                    "Operation polling returned a different identity")
            status = data.get("status")
            if status == "completed":
                return data
            require(status in {"pending", "processing"},
                    "Synthetic asynchronous operation failed or had an unknown status")
            require(time.monotonic() < deadline, "Synthetic asynchronous operation did not finish")
            await asyncio.sleep(0.5)

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.state_dir / (self.bank + "-capabilities.json")
        # Random exclusive creation avoids following a preexisting report-temp symlink.
        with tempfile.NamedTemporaryFile(mode="w", dir=self.state_dir,
                                         prefix=".capabilities-", delete=False) as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(json.dumps(self.report, indent=2) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary.name).replace(target)

    async def check(self, name: str, callback) -> bool:
        start = time.perf_counter()
        result = {"name": name}
        try:
            detail = await callback()
            result.update({"status": "passed", **(detail or {})})
        except ProbeError as error:
            result.update(status="failed", reason=str(error))
        except httpx.HTTPError:
            result.update(status="failed", reason="Synthetic service transport failed")
        except (KeyError, TypeError, AttributeError, ValueError):
            result.update(status="failed", reason="Synthetic response shape is invalid")
        result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
        self.report["checks"].append(result)
        self.save()
        print(f"{name}: {result['status']}", flush=True)
        return result["status"] == "passed"

    async def contract(self) -> dict:
        schema = await self.request("GET", "/openapi.json")
        baseline = json.loads(CONTRACT.read_text())
        version = schema.get("info", {}).get("version")
        self.report["api_version"] = version
        self.report["openapi_sha256"] = hashlib.sha256(
            json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        differences = contract_differences(schema, baseline)
        self.report["contract_differences"] = differences
        require(not differences, "Used OpenAPI contract differs; review contract_differences before running")
        require(self.expected_version is None or version == self.expected_version,
                "Candidate API does not report the required version")
        await self.request("GET", "/health/live")
        await self.request("GET", "/health/ready")
        return {"version": version, "deployed_baseline": baseline["provenance"]["api_version"]}

    async def seed(self) -> dict:
        # A reused bank is refused even if its name has the synthetic prefix.
        await self.request("GET", self.path + "/stats", expected=404)
        bank = await self.request("PUT", self.path, json={
            "name": f"Eidolon Phase 0 synthetic capability fixture ({self.bank})",
            "background": "Entirely fictional companion evaluation data."})
        require(bank.get("bank_id") == self.bank, "Bank creation returned a different identity")
        operation_id = str(uuid.uuid4())
        body = {"items": [{**item, "update_mode": "replace",
                           "metadata": {"fixture": "phase0-synthetic-capabilities"}}
                          for item in ITEMS], "async": True, "operation_id": operation_id}
        first = await self.request("POST", self.path + "/memories", json=body)
        require(first.get("operation_id") == operation_id and first.get("async") is True,
                "Retain did not preserve the client asynchronous operation identity")
        second = await self.request("POST", self.path + "/memories", json=body)
        require(second.get("operation_id") == operation_id,
                "Repeated retain created a different operation identity")
        await self.poll(operation_id)
        replay = await self.request("POST", self.path + "/memories", json=body)
        require(replay.get("operation_id") == operation_id, "Completed retain replay changed identity")
        await self.poll(operation_id)
        stats = await self.request("GET", self.path + "/stats")
        require(stats.get("total_documents") == len(ITEMS),
                "Idempotent retain did not leave exactly one document per synthetic source")
        for item in ITEMS:
            document = await self.request("GET", self.path + "/documents/" + item["document_id"])
            require(document.get("id") == item["document_id"] and
                    document.get("original_text") == item["content"],
                    "Retained document identity or original text changed")
        return {"operation_id": operation_id, "document_count": stats["total_documents"]}

    async def recall(self, query: str, **kwargs) -> dict:
        return await self.request("POST", self.path + "/memories/recall", json={
            "query": query, "budget": "low", "max_tokens": 2000,
            "types": ["world", "experience"], **kwargs})

    async def source_expansion(self) -> dict:
        data = await self.recall("What color is Mira's notebook?", tags=[SCOPE],
                                 tags_match="all_strict", include={"chunks": {}, "source_facts": {}})
        matches = [item for item in data.get("results", [])
                   if item.get("document_id") == "synthetic-current"]
        require(matches, "Recall did not return the synthetic notebook source")
        chunk_id = matches[0].get("chunk_id")
        require(isinstance(chunk_id, str) and chunk_id, "Recall omitted a source chunk identity")
        chunks = data.get("chunks")
        require(isinstance(chunks, dict) and chunk_id in chunks,
                "Requested recall source chunks were missing")
        require(SOURCE_TEXT in chunks[chunk_id].get("text", ""),
                "Included source chunk omitted original synthetic wording")
        chunk = await self.request("GET", "/v1/default/chunks/" + quote(chunk_id, safe=""))
        require(chunk.get("document_id") == "synthetic-current" and
                SOURCE_TEXT in chunk.get("chunk_text", ""),
                "Direct chunk expansion did not preserve source identity and wording")
        return {"document_id": "synthetic-current", "chunk_id": chunk_id}

    async def tags(self) -> dict:
        query = "List Mira's notebook and amber thermos facts."
        strict = await self.recall(query, tags=[SCOPE], tags_match="all_strict")
        inclusive = await self.recall(query, tags=[SCOPE], tags_match="all")
        unfiltered = await self.recall(query)
        strict_ids = {item.get("document_id") for item in strict.get("results", [])}
        inclusive_ids = {item.get("document_id") for item in inclusive.get("results", [])}
        unfiltered_ids = {item.get("document_id") for item in unfiltered.get("results", [])}
        require(strict_ids == {"synthetic-current"}, "Strict tag recall leaked or omitted source scope")
        require({"synthetic-current", "synthetic-legacy"} <= inclusive_ids,
                "Inclusive tag recall lost the eligible untagged legacy source")
        require("synthetic-erased" not in inclusive_ids, "Inclusive tags leaked an unrelated tagged source")
        require("synthetic-legacy" in unfiltered_ids, "General recall lost untagged legacy access")
        return {"strict_documents": sorted(strict_ids), "inclusive_documents": sorted(inclusive_ids)}

    async def reflection(self) -> dict:
        response = await self.request("POST", self.path + "/reflect", json={
            "query": "What color is Mira's notebook? Answer from evidence; include its memory IDs.",
            "budget": "low", "max_tokens": 512, "tags": [SCOPE], "tags_match": "all_strict",
            "fact_types": ["world", "experience"], "exclude_mental_models": True,
            "include": {"facts": {}}, "response_schema": {
                "type": "object", "properties": {"answer": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["answer", "source_ids"], "additionalProperties": False}})
        evidence = response.get("based_on")
        require(isinstance(evidence, dict) and isinstance(evidence.get("memories"), list),
                "Reflection returned no evidence")
        evidence_ids = [fact.get("id") for fact in evidence["memories"]]
        require(evidence_ids and all(isinstance(identifier, str) and identifier
                                     for identifier in evidence_ids),
                "Reflection evidence lacks resolvable source identities")
        for identifier in evidence_ids:
            fact = await self.request("GET", self.path + "/memories/" + quote(identifier, safe=""))
            require(fact.get("document_id") == "synthetic-current",
                    "Reflection source did not resolve to the synthetic notebook document")
        # The service's based_on IDs are real memory UUIDs. Structured extraction is
        # a separate model step over the final answer and may invent IDs or omit them.
        # Keep strict validation; record rejection rather than treating those IDs as evidence.
        try:
            ids = validate_structured_reflection(response)
        except ProbeError as error:
            output = response.get("structured_output")
            require(isinstance(output, dict) and set(output) == {"answer", "source_ids"} and
                    isinstance(output.get("answer"), str) and "violet" in output["answer"].lower() and
                    isinstance(output.get("source_ids"), list) and
                    all(isinstance(identifier, str) for identifier in output["source_ids"]),
                    "Reflection did not produce the requested structured answer schema")
            return {"status": "limitation", "reason": str(error),
                    "structured_answer_schema_verified": True, "evidence_source_ids_verified": evidence_ids,
                    "observed_structured_source_ids": output["source_ids"],
                    "structured_source_ids_rejected": True, "usage": response.get("usage"),
                    "scope": "API evidence resolves correctly; generated citation IDs require app validation"}
        return {"source_ids": ids, "evidence_source_ids_verified": evidence_ids,
                "usage": response.get("usage")}

    async def temporal(self) -> dict:
        observations = []
        # Manila midnight changes the civil day; 4 AM changes Eidolon's daily room.
        # The temporal window ranks candidates; it is deliberately not asserted to filter.
        for anchor in ("2026-10-12T15:59:00Z", "2026-10-12T16:01:00Z",
                       "2026-10-12T19:59:00Z", "2026-10-12T20:01:00Z"):
            data = await self.recall("Which notebook did Mira describe yesterday?",
                query_timestamp=anchor,
                temporal_window={"start": "2026-10-12T20:00:00Z", "end": "2026-10-12T20:10:00Z"})
            matches = [item for item in data.get("results", [])
                       if item.get("document_id") == "synthetic-current"]
            require(matches, "Temporal recall omitted the in-window synthetic source")
            dates = [matches[0].get(key) for key in ("occurred_start", "occurred_end", "mentioned_at")]
            require(any(date is not None for date in dates), "Temporal recall omitted all evidence dates")
            observations.append({"query_timestamp": anchor, "evidence_dates": dates,
                "returned_documents": [item.get("document_id") for item in data["results"]]})
        return {"anchors": observations,
                "scope": "timestamp/window acceptance and evidence preservation; ranking accuracy requires baseline scoring"}

    async def long_query(self) -> dict:
        query = "Mira notebook violet. " * 20000
        start = time.perf_counter()
        response = await self.client.post(self.path + "/memories/recall", json={
            "query": query, "budget": "low", "max_tokens": 512})
        self.report["requests"].append({"method": "POST", "path": self.path + "/memories/recall",
            "http_status": response.status_code, "elapsed_ms": round((time.perf_counter()-start)*1000, 2),
            "query_bytes": len(query.encode()), "response_bytes": len(response.content)})
        if response.status_code == 400 and self.report["api_version"] == "0.10.2":
            # Recognize exactly the known compatibility limitation without echoing an error body.
            detail = response.json().get("detail")
            require(isinstance(detail, str) and re.fullmatch(
                r"Query too long: \d+ tokens exceeds maximum of \d+\. Please shorten your query\.", detail),
                "Long query failed for an unexpected reason")
            return {"status": "limitation", "reason": "0.10.2 rejects over-cap queries; Eidolon must retain its bounded fallback"}
        require(response.status_code == 200, "Candidate did not accept/truncate an over-cap query")
        require(isinstance(response.json().get("results"), list), "Long query returned invalid recall results")
        return {"query_bytes": len(query.encode()), "scope": "truncation accepted; semantic accuracy is separate"}

    async def refresh_model(self) -> dict:
        model = await self.request("POST", self.path + "/mental-models", json={
            "id": self.model_id, "name": "Synthetic keepsake overview",
            "source_query": "Describe Mira's invented keepsake using only available evidence.",
            "tags": ["phase0-forget"], "max_tokens": 512,
            "trigger": {"mode": "full", "fact_types": ["world", "experience"],
                        "exclude_mental_models": True, "include_chunks": True,
                        "keep_trace": True, "refresh_after_consolidation": False}})
        require(model.get("mental_model_id") == self.model_id, "Created model identity changed")
        operation_id = model.get("operation_id")
        require(isinstance(operation_id, str) and operation_id,
                "Model creation returned no asynchronous operation identity")
        await self.poll(operation_id)
        path = self.path + "/mental-models/" + self.model_id
        before = await self.request("GET", path)
        preview = await self.request("POST", path + "/dry-run-refresh")
        after = await self.request("GET", path)
        for field in ("content", "last_refreshed_at", "last_memory_seen_at"):
            require(before.get(field) == after.get(field), "Dry run changed persisted model state")
        require("obsidian" in preview.get("preview_content", "").lower(),
                "Model preview did not preserve synthetic keepsake evidence")
        submitted = await self.request("POST", path + "/refresh")
        operation_id = submitted.get("operation_id")
        require(isinstance(operation_id, str) and operation_id, "Model refresh returned no operation identity")
        await self.poll(operation_id)
        refreshed = await self.request("GET", path)
        require("obsidian" in (refreshed.get("content") or "").lower() and
                refreshed.get("last_refreshed_at"), "Completed refresh did not persist sourced model content")
        require(refreshed.get("reflect_response", {}).get("based_on"),
                "Refreshed model did not preserve reflection evidence")
        return {"model_id": self.model_id, "last_refreshed_at": refreshed["last_refreshed_at"],
                "usage": preview.get("usage")}

    async def deletion(self) -> dict:
        path = self.path + "/mental-models/" + self.model_id
        before = await self.request("GET", path)
        await self.request("DELETE", self.path + "/documents/synthetic-erased")
        await self.request("GET", self.path + "/documents/synthetic-erased", expected=404)
        recalled = await self.recall("Mira invented keepsake obsidian", tags=["phase0-forget"],
                                     tags_match="all_strict")
        require(not recalled.get("results"), "Deleted synthetic source remained recallable")
        model = await self.request("GET", path)
        stale = model.get("is_stale")
        retained_claim = "obsidian" in (model.get("content") or "").lower()
        cleared = await self.request("POST", path + "/clear")
        require(not cleared.get("content"), "Explicit invalidation did not clear the derived model")
        # Record the upstream behavior distinctly; do not count stale summaries as safe deletion.
        return {"source_recall_removed": True, "explicit_clear_verified": True,
                "stale_flag_before_deletion": before.get("is_stale"),
                "automatic_stale_flag": stale, "summary_retained_deleted_claim": retained_claim,
                **({"status": "limitation", "reason":
                    "Source deletion leaves derived content; explicit app invalidation remains required"}
                   if retained_claim or stale is not True else {})}

    async def concurrent_edit(self) -> dict:
        identifier = self.concurrent_model_id
        path = self.path + "/mental-models/" + identifier
        created = await self.request("POST", self.path + "/mental-models", json={
            "id": identifier, "name": "Initial synthetic notebook overview",
            "source_query": "Describe Mira's notebook using available evidence.",
            "tags": [SCOPE], "max_tokens": 512,
            "trigger": {"mode": "full", "fact_types": ["world", "experience"],
                        "exclude_mental_models": True, "refresh_after_consolidation": False}})
        require(created.get("mental_model_id") == identifier, "Concurrent model creation identity changed")
        creation_operation = created.get("operation_id")
        require(isinstance(creation_operation, str) and creation_operation,
                "Concurrent model creation lacks an operation ID")
        await self.poll(creation_operation)
        submitted = await self.request("POST", path + "/refresh")
        operation_id = submitted.get("operation_id")
        require(isinstance(operation_id, str) and operation_id, "Concurrent refresh lacks an operation ID")
        deadline = time.monotonic() + min(30, self.poll_timeout)
        while True:
            operation = await self.request("GET", self.path + "/operations/" + operation_id)
            status = operation.get("status")
            if status == "completed":
                return {"status": "not_run", "reason":
                        "Refresh finished before an edit could overlap; concurrent behavior is unverified"}
            require(status in {"pending", "processing"}, "Concurrent refresh failed before editing")
            if status == "processing":
                break
            if time.monotonic() >= deadline:
                return {"status": "not_run", "reason":
                        "No processing overlap observed before deadline; concurrent behavior is unverified"}
            await asyncio.sleep(0.1)
        patch = {"name": "Reviewed synthetic notebook overview",
                 "source_query": "Describe only the clasp on Mira's notebook from available evidence."}
        await self.request("PATCH", path, json=patch)
        await self.poll(operation_id)
        model = await self.request("GET", path)
        require(all(model.get(key) == value for key, value in patch.items()),
                "Concurrent refresh overwrote an accepted model edit")
        return {"status": "limitation", "overlap_observed": True,
                "accepted_metadata_edit_preserved": True,
                "reason": "Metadata edit survived one refresh overlap; public PATCH does not expose manual content editing",
                "scope": "metadata-edit preservation only; the reviewed-content edit race and atomic revision guarantees remain unverified"}

    async def scoped_admin(self) -> dict:
        if self.scoped_key is None:
            return {"status": "not_run", "reason":
                    "No independently configured tag-scoped credential; OpenAPI availability cannot prove authorization"}
        # Last probe: if the claimed scoped credential is actually unscoped, only our new
        # synthetic bank is affected, and the result is explicitly a failed scope check.
        response = await self.client.delete(self.path, headers={"Authorization": f"Bearer {self.scoped_key}"})
        require(response.status_code in {401, 403}, "Tag-scoped caller was not refused a whole-bank operation")
        require(response.status_code == 403, "Scoped credential was not authenticated; scope restriction remains unverified")
        await self.request("GET", self.path + "/stats")
        return {"http_status": response.status_code, "bank_preserved": True}

    async def run(self) -> dict:
        if not await self.check("deployed_contract_and_readiness", self.contract):
            return self.report
        if not await self.check("async_retain_idempotency_and_source_identity", self.seed):
            return self.report
        for name, callback in (("source_expansion", self.source_expansion),
                               ("tag_scoping_and_legacy_access", self.tags),
                               ("structured_reflection_and_evidence", self.reflection),
                               ("temporal_boundary_contract", self.temporal),
                               ("long_query_compatibility", self.long_query)):
            await self.check(name, callback)
        if await self.check("mental_model_preview_and_refresh", self.refresh_model):
            await self.check("deletion_invalidation", self.deletion)
        else:
            self.report["checks"].append({"name": "deletion_invalidation", "status": "not_run",
                                          "reason": "Model refresh failed; deletion safety is unverified"})
        await self.check("concurrent_model_edit_and_refresh", self.concurrent_edit)
        await self.check("scoped_admin_restriction", self.scoped_admin)
        self.report["completed_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
        return self.report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True,
                        help="Separate local service origin; live 9177 and 9191 are refused")
    parser.add_argument("--bank", required=True, help="New eidolon-phase0-synthetic- bank with a unique suffix")
    parser.add_argument("--state-dir", type=Path, required=True, help="Dedicated EIDOLON_HOME outside Git checkouts")
    parser.add_argument("--isolated-instance", action="store_true", help="Confirm endpoint uses an isolated database")
    parser.add_argument("--expected-version", help="Fail before seeding unless the API reports this version")
    parser.add_argument("--poll-timeout", type=float, default=300)
    parser.add_argument("--request-timeout", type=float, default=180)
    parser.add_argument("--api-key-env", help="Optional environment variable for the isolated service credential")
    parser.add_argument("--scoped-api-key-env", help="Optional separately configured tag-scoped credential")
    args = parser.parse_args()
    try:
        state_dir = validate_isolation(args.base_url, args.bank, args.state_dir, args.isolated_instance)
        require(args.poll_timeout > 0 and args.request_timeout > 0, "Timeouts must be positive")
        headers = {}
        if args.api_key_env:
            require(bool(os.environ.get(args.api_key_env)), "Isolated API credential environment variable is missing")
            headers["Authorization"] = "Bearer " + os.environ[args.api_key_env]
        scoped_key = None
        if args.scoped_api_key_env:
            require(bool(os.environ.get(args.scoped_api_key_env)), "Scoped credential environment variable is missing")
            scoped_key = os.environ[args.scoped_api_key_env]
    except (ProbeError, ValueError) as error:
        parser.error(str(error))

    async def execute() -> dict:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=args.request_timeout,
                                     headers=headers, trust_env=False) as client:
            return await CapabilityRunner(client, args.bank, state_dir, poll_timeout=args.poll_timeout,
                                          expected_version=args.expected_version, scoped_key=scoped_key).run()
    report = asyncio.run(execute())
    print(f"Report: {state_dir / (args.bank + '-capabilities.json')}")
    return int(any(check["status"] == "failed" for check in report["checks"]))


if __name__ == "__main__":
    raise SystemExit(main())
