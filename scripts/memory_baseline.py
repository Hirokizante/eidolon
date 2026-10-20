"""Measure the current companion pipeline against explicitly synthetic memories.

This deliberately does not read default app configuration or private prompt files.
Run with --help; see docs/MEMORY_REFERENCE.md for isolation and scoring limits.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import yaml

from eidolon.config import AppConfig, MemoryConfig, ProviderConfig, atomic_write
from eidolon.engine.conversation import ConversationEngine
from eidolon.memory.hindsight import HindsightMemory, MemoryUnavailable, OutboxDispatcher
from eidolon.memory.coordinator import assemble_context
from eidolon.memory.query import compose_recall_query
from eidolon.providers import OpenAIProvider
from eidolon.storage.store import Store


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/memory_baseline_companion.json"
BANK_PREFIX = "eidolon-phase0-synthetic-"
BANK_NAME = "Eidolon Phase 0 synthetic companion fixture"
MARKER = "phase0-synthetic-state.json"


def load_fixture(path: Path = FIXTURE) -> tuple[dict, str]:
    raw = path.read_bytes()
    fixture = json.loads(raw)
    if fixture.get("schema_version") != 1 or fixture.get("synthetic") is not True:
        raise ValueError("Only the versioned synthetic baseline fixture is accepted")
    sources = fixture.get("sources", [])
    probes = fixture.get("probes", [])
    if len(probes) < 60 or not sources:
        raise ValueError("Baseline requires source exchanges and at least 60 probes")
    ids = [source["id"] for source in sources]
    if len(set(ids)) != len(ids) or len({p["id"] for p in probes}) != len(probes):
        raise ValueError("Fixture IDs must be unique")
    timestamps = {s["id"]: parse_time(s["timestamp"]) for s in sources}
    for probe in probes:
        timestamp = parse_time(probe["timestamp"])
        for source_id in probe["expected_source_ids"]:
            if source_id not in timestamps or timestamps[source_id] > timestamp:
                raise ValueError("Expected evidence must exist at the probe timestamp")
        if any(not group or any(not isinstance(atom, str) or not atom for atom in group)
               for group in probe["expected_atoms"]):
            raise ValueError("Evidence atoms must contain nonempty string alternatives")
    return fixture, hashlib.sha256(raw).hexdigest()


def parse_time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Fixture timestamps must include timezone offsets")
    return timestamp


def isolated_home(value: str | None, fixture_hash: str) -> Path:
    if not value:
        raise ValueError("Explicit EIDOLON_HOME is required for baseline state")
    supplied = Path(value).expanduser()
    home = supplied.resolve()
    live = (Path.home() / "Library/Application Support/Eidolon").resolve()
    if supplied.is_symlink() or home == live or live in home.parents or home == Path.home():
        raise ValueError("Use a dedicated synthetic state directory away from live app state")
    if any((parent / ".git").exists() for parent in (home, *home.parents)):
        raise ValueError("EIDOLON_HOME must be outside every Git checkout")
    marker = home / MARKER
    if home.exists() and any(home.iterdir()):
        if not marker.is_file() or marker.is_symlink():
            raise ValueError("Refusing existing state without a synthetic baseline marker")
        data = json.loads(marker.read_text())
        if data != {"synthetic": True, "fixture_sha256": fixture_hash}:
            raise ValueError("Synthetic state marker does not match the fixture")
        if any(path.name not in {MARKER, "runs"} for path in home.iterdir()):
            raise ValueError("Refusing state containing app configuration or unrelated files")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(marker, json.dumps({"synthetic": True, "fixture_sha256": fixture_hash}))
    (home / "runs").mkdir(mode=0o700, exist_ok=True)
    if (home / "runs").is_symlink():
        raise ValueError("Synthetic report directory cannot be a symbolic link")
    return home


def isolated_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.port in {None, 9177, 9191}):
        raise ValueError("Use an explicit loopback test port; live 9177/9191 APIs are refused")
    return url.rstrip("/")


def synthetic_bank(bank: str) -> str:
    if not re.fullmatch(re.escape(BANK_PREFIX) + r"[a-z0-9][a-z0-9-]{0,63}", bank):
        raise ValueError("Bank must use the eidolon-phase0-synthetic- prefix and a unique suffix")
    return bank


def synthetic_bank_name(bank: str) -> str:
    # Include identity in the marker rather than trusting a descriptive name alone.
    return BANK_NAME + " (" + synthetic_bank(bank) + ")"


async def prepare_bank(memory: HindsightMemory, *, reuse: bool):
    bank = synthetic_bank(memory.config.bank)
    if not reuse:
        inventory = await memory._request("GET", "/v1/default/banks")
        if any(b.get("bank_id") == bank for b in inventory.get("banks", [])):
            raise ValueError("Refusing to seed an existing bank; use a new synthetic ID")
        created = await memory._request("PUT", memory._bank_path,
                                        json={"name": synthetic_bank_name(bank),
                                              "background": "Entirely invented evaluation data."})
        if created.get("bank_id") != bank:
            raise ValueError("Synthetic bank creation returned a different or missing bank identity")
    known = await memory.validate_bank()
    if known.get("bank_id") != bank or known.get("name") != synthetic_bank_name(bank):
        raise ValueError("Synthetic bank identity or unique name marker is missing or changed")


def source_hashes() -> dict[str, str]:
    paths = [ROOT / "src/eidolon/memory/query.py", ROOT / "src/eidolon/memory/hindsight.py",
             ROOT / "src/eidolon/memory/models.py", ROOT / "src/eidolon/memory/coordinator.py",
             ROOT / "src/eidolon/engine/conversation.py"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def provider_profile(path: Path | None, fixture: dict) -> ProviderConfig:
    if path is None:
        return ProviderConfig(name="phase0", model="capture-only", tools=False,
                              presentation="plain", context_window=fixture["context_window"])
    # Read the provider section only. load_config would also migrate private prompts/state.
    resolved = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (resolved.parent, *resolved.parents)):
        raise ValueError("Provider configuration must be an explicit private file outside Git")
    data = yaml.safe_load(resolved.read_text())
    selected = next(p for p in data["providers"] if p["name"] == data["provider"])
    profile = ProviderConfig.model_validate(selected)
    # Baseline final replies use one ordinary completion with no side-effect tools.
    profile.tools = False
    profile.presentation = "plain"
    profile.context_window = fixture["context_window"]
    return profile


def fixed_clock(timestamp: datetime):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return timestamp.astimezone(tz) if tz else timestamp.replace(tzinfo=None)
    return Clock


class ProbeStore:
    """Read only local history at the replay time; probes never become evidence."""

    def __init__(self, store: Store, timestamp: datetime):
        self.store, self.timestamp = store, timestamp
        self.summary = ""

    def list_messages(self, session_id):
        return [m for m in self.store.list_messages(session_id)
                if parse_time(m["created_at"]) <= self.timestamp]

    def set_summary(self, session_id, summary):
        self.summary = summary

    def complete_turn(self, *args, **kwargs):
        return {"id": "read-only-baseline-capture"}


class CaptureProvider:
    presentation_mode = "plain"

    def __init__(self):
        self.messages = []

    async def stream(self, messages, **kwargs):
        self.messages = messages
        yield {"type": "token", "text": "Synthetic prompt captured."}
        yield {"type": "finish", "reason": "stop"}


class MeasuredMemory:
    def __init__(self, memory):
        self.memory = memory
        self.text = ""
        self.query = ""
        self.elapsed_ms = None

    async def recall(self, query, **kwargs):
        self.query = query
        started = time.perf_counter()
        try:
            self.text = await self.memory.recall(query, **kwargs)
            return self.text
        finally:
            self.elapsed_ms = (time.perf_counter() - started) * 1000

    async def recall_result(self, query, **kwargs):
        self.query = query
        started = time.perf_counter()
        try:
            result = await self.memory.recall_result(query, **kwargs)
            self.text = assemble_context(result, max_chars=kwargs.get("max_chars", 8000)).text
            return result
        finally:
            self.elapsed_ms = (time.perf_counter() - started) * 1000


def atom_score(text: str, groups: list[list[str]]) -> dict:
    matched = [any(atom.casefold() in text.casefold() for atom in group) for group in groups]
    return {"matched": matched, "coverage": sum(matched) / len(matched) if matched else None}


def summary(rows: list[dict]) -> dict:
    def stats(values):
        values = sorted(v for v in values if v is not None)
        if not values:
            return {"count": 0, "mean": None, "median": None, "p95": None}
        return {"count": len(values), "mean": sum(values) / len(values),
                "median": values[(len(values) - 1) // 2],
                "p95": values[max(0, (len(values) * 95 + 99) // 100 - 1)]}

    categories = sorted({row["category"] for row in rows})
    return {
        "probes": len(rows),
        "failed_probes": sum(bool(row.get("error")) for row in rows),
        "recall_latency_ms": stats([r.get("recall_latency_ms") for r in rows]),
        "preparation_latency_ms": stats([r.get("preparation_latency_ms") for r in rows]),
        "context_chars": stats([r.get("context_chars") for r in rows]),
        "context_utf8_bytes": stats([r.get("context_utf8_bytes") for r in rows]),
        "reply_latency_ms": stats([r["reply"].get("latency_ms")
                                   for r in rows if r.get("reply")]),
        "reported_prompt_tokens": stats([r["reply"].get("usage", {}).get("prompt_tokens")
                                         for r in rows if r.get("reply")]),
        "reported_completion_tokens": stats([r["reply"].get("usage", {}).get("completion_tokens")
                                             for r in rows if r.get("reply")]),
        "by_category": {
            category: {
                "probes": sum(r["category"] == category for r in rows),
                "recall_atom_coverage": stats([r.get("recall_evidence", {}).get("coverage")
                                              for r in rows if r["category"] == category]),
                "supplied_atom_coverage": stats([r.get("supplied_evidence", {}).get("coverage")
                                                for r in rows if r["category"] == category]),
            } for category in categories
        },
        "reply_evaluation": "Human rubric review required; atom coverage is not reply quality.",
    }


def source_identity(fixture: dict, source: dict, *, bank: str, fixture_hash: str) -> str:
    # Operation UUID ownership can be global across banks. The Store derives its
    # operation UUID from this document identity, so both must include bank scope.
    identity = ":".join([fixture["fixture_id"], fixture_hash, synthetic_bank(bank), source["id"]])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def seed_store(store: Store, fixture: dict, *, bank: str, fixture_hash: str) -> dict[str, str]:
    documents = {}
    for source in fixture["sources"]:
        timestamp = parse_time(source["timestamp"])
        session = store.session_for(timestamp, fixture["timezone"], fixture["reset_hour"])
        with patch("eidolon.storage.store.utc_now", return_value=source["timestamp"]):
            turn = store.complete_turn(session["id"], source["user"], source["assistant"],
                                       generation_id=source_identity(fixture, source, bank=bank,
                                                                     fixture_hash=fixture_hash))
        documents[source["id"]] = turn["document_id"]
    return documents


async def wait_for_index(dispatcher: OutboxDispatcher, *, deadline: float = 900):
    started = time.perf_counter()
    while True:
        status = await dispatcher.dispatch_once()
        counts = status["counts"]
        if counts.get("failed", 0):
            raise MemoryUnavailable(status["error"] or "Synthetic retention failed")
        if not status["pending"]:
            return (time.perf_counter() - started) * 1000
        if time.perf_counter() - started > deadline:
            raise MemoryUnavailable("Synthetic retention did not complete within the deadline")
        await asyncio.sleep(1)


async def final_reply(profile, messages) -> dict:
    provider = OpenAIProvider(profile)
    reply, usage, finish = [], {}, None
    started = time.perf_counter()
    async for event in provider.stream(messages, presentation="plain"):
        if event["type"] == "token":
            reply.append(event["text"])
        elif event["type"] == "usage":
            usage = event.get("usage", {})
        elif event["type"] == "finish":
            finish = event.get("reason")
    if finish is None:
        raise RuntimeError("Reply stream ended without a completion signal")
    return {"text": "".join(reply), "latency_ms": (time.perf_counter() - started) * 1000,
            "usage": usage, "finish_reason": finish, "human_score": None}


async def evaluate_probe(config, store, memory, probe, fixture, documents, *, replies=False):
    timestamp = parse_time(probe["timestamp"])
    session = store.session_for(timestamp, fixture["timezone"], fixture["reset_hour"])
    history_store = ProbeStore(store, timestamp)
    capture, measured = CaptureProvider(), MeasuredMemory(memory)
    engine = ConversationEngine(config, history_store, measured, provider=capture)
    errors = []
    started = time.perf_counter()
    # Only the evaluation clock is overridden; no query/context policy is changed.
    with patch("eidolon.engine.conversation.datetime", fixed_clock(timestamp)), \
            patch("eidolon.memory.hindsight.datetime", fixed_clock(timestamp)):
        async for event in engine.generate(session["id"], probe["question"], interactive=False):
            if event["type"] in {"error", "recall_required"}:
                errors.append(event["message"])
    elapsed = (time.perf_counter() - started) * 1000
    await engine.close()
    serialized = "\n".join(json.dumps(m, ensure_ascii=False) for m in capture.messages)
    # The current question and generic identity prompts are not retrieved evidence.
    suffix_count = 2 if config.selected_provider().post_history == "system" else 1
    history_messages = [m for m in capture.messages[:-suffix_count]
                        if m["role"] in {"user", "assistant"}]
    supplied = "\n".join([measured.text, history_store.summary,
                           *(m["content"] for m in history_messages)])
    row = {
        "id": probe["id"], "category": probe["category"], "timestamp": probe["timestamp"],
        "question": probe["question"], "session_id": session["id"],
        "restart": probe["restart"], "query": measured.query,
        "query_utf8_bytes": len(measured.query.encode()),
        "recall_latency_ms": measured.elapsed_ms, "preparation_latency_ms": elapsed,
        "memory_chars": len(measured.text), "memory_utf8_bytes": len(measured.text.encode()),
        "context_chars": len(serialized), "context_utf8_bytes": len(serialized.encode()),
        "context_tokens": None, "context_token_note": "Only provider-reported usage is tokens",
        "recalled_context": measured.text, "messages": capture.messages,
        "expected_atoms": probe["expected_atoms"],
        "expected_document_ids": [documents[s] for s in probe["expected_source_ids"]],
        "recall_evidence": atom_score(measured.text, probe["expected_atoms"]),
        "supplied_evidence": atom_score(supplied, probe["expected_atoms"]),
        "question_atom_overlap": atom_score(probe["question"], probe["expected_atoms"]),
        "reply_rubric": probe["reply_rubric"], "reply": None,
        "forbidden_reply_atoms": probe["forbidden_reply_atoms"],
    }
    assert not measured.query or measured.query == compose_recall_query(
        history_store.list_messages(session["id"]), probe["question"])
    if errors:
        row["error"] = "; ".join(errors)
    elif replies:
        try:
            row["reply"] = await final_reply(config.selected_provider(), capture.messages)
            text = row["reply"]["text"]
            row["reply"]["atom_checks"] = atom_score(text, probe["expected_atoms"])
            row["reply"]["forbidden_atom_hits"] = [atom for atom in probe["forbidden_reply_atoms"]
                                                    if atom.casefold() in text.casefold()]
        except Exception as error:
            row["error"] = "Final provider reply failed: " + type(error).__name__
    return row


async def evaluate_replies(rows, profile, *, concurrency=1, on_update=None):
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(row):
        if row.get("error"):
            return
        async with semaphore:
            try:
                row["reply"] = await final_reply(profile, row["messages"])
                text = row["reply"]["text"]
                row["reply"]["atom_checks"] = atom_score(text, row["expected_atoms"])
                row["reply"]["forbidden_atom_hits"] = [
                    atom for atom in row["forbidden_reply_atoms"]
                    if atom.casefold() in text.casefold()]
            except Exception as error:
                row["error"] = "Final provider reply failed: " + type(error).__name__
            if on_update:
                on_update(row)

    await asyncio.gather(*(evaluate(row) for row in rows))


async def run(args) -> dict:
    fixture, digest = load_fixture()
    home = isolated_home(os.environ.get("EIDOLON_HOME"), digest)
    endpoint, bank = isolated_endpoint(args.url), synthetic_bank(args.bank)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.label):
        raise ValueError("Report label must contain lowercase letters, digits and hyphens")
    if not 1 <= args.reply_concurrency <= 8:
        raise ValueError("Reply concurrency must be between 1 and 8")
    run_dir = home / "runs" / bank
    run_dir.mkdir(mode=0o700, exist_ok=True)
    if run_dir.is_symlink():
        raise ValueError("Run directory cannot be a symbolic link")
    manifest_path = run_dir / "seed-manifest.json"
    report_path = run_dir / (args.label + ".json")
    if report_path.exists():
        raise ValueError("Refusing to overwrite an existing baseline report")
    profile = provider_profile(args.provider_config, fixture)
    state = run_dir / args.label
    state.mkdir(mode=0o700)
    config = AppConfig(state_dir=state, provider=profile.name, providers=[profile],
                       timezone=fixture["timezone"], reset_hour=fixture["reset_hour"],
                       prompts={n: str(ROOT / "src/eidolon/defaults" / (n + ".md"))
                                for n in ("SYSTEM", "SOUL", "USER", "POST_HISTORY")},
                       memory=MemoryConfig(url=endpoint, bank=bank, retain_enabled=True,
                                           verified=True, timeout=120))
    store = Store(state / "synthetic.sqlite")
    raw_recalls = []

    async def observe(response):
        if response.request.url.path.endswith("/memories/recall") and response.is_success:
            await response.aread()
            raw_recalls.append(response.json())

    report = {"fixture_id": fixture["fixture_id"], "fixture_sha256": digest,
              "synthetic": True, "label": args.label, "bank": bank,
              "pipeline": "ConversationEngine.generate + MemoryCoordinator + HindsightMemory.recall_result",
              "corpus_mode": "All source exchanges indexed before read-only probes; local history "
                             "and query/context clocks replay each probe date. The current adapter "
                             "does not strictly exclude later sources; inspect temporal leakage.",
              "provider": {"model": profile.model, "context_window": profile.context_window_for(),
                           "reply_concurrency": args.reply_concurrency,
                           "endpoint_sha256": hashlib.sha256(profile.endpoint.encode()).hexdigest(),
                           "parameters_sha256": hashlib.sha256(json.dumps(
                               profile.parameters, sort_keys=True).encode()).hexdigest(),
                           "parameters": {k: v for k, v in profile.parameters.items()
                                          if k in {"temperature", "top_p", "seed", "max_tokens",
                                                   "max_completion_tokens", "reasoning_effort"}},
                           "replies_enabled": bool(args.provider_config) and not args.no_replies},
              "source_sha256": source_hashes(), "rows": []}
    try:
        async with httpx.AsyncClient(base_url=endpoint, timeout=120,
                                    event_hooks={"response": [observe]}) as client:
            response = await client.get("/openapi.json")
            response.raise_for_status()
            report["hindsight_version"] = response.json()["info"]["version"]
            memory = HindsightMemory(config.memory, client=client)
            documents = seed_store(store, fixture, bank=bank, fixture_hash=digest)
            if args.reuse_bank:
                if not manifest_path.is_file():
                    raise ValueError("Reuse requires this runner's completed seed manifest")
                manifest = json.loads(manifest_path.read_text())
                if (manifest.get("synthetic") is not True or manifest.get("bank") != bank
                        or manifest.get("fixture_sha256") != digest
                        or manifest.get("documents") != documents):
                    raise ValueError("Seed manifest does not match this synthetic fixture")
                for entry in store.pending_outbox():
                    store.update_outbox(entry["document_id"], status="completed")
            await prepare_bank(memory, reuse=args.reuse_bank)
            if not args.reuse_bank:
                report["retention_latency_ms"] = await wait_for_index(
                    OutboxDispatcher(memory, store), deadline=args.index_timeout)
                atomic_write(manifest_path, json.dumps({"synthetic": True, "bank": bank,
                                                       "fixture_sha256": digest,
                                                       "documents": documents}, indent=2))
            for index, probe in enumerate(fixture["probes"]):
                if probe["restart"]:
                    # Reopen the durable transcript and recreate the HTTP adapter/engine.
                    store.close()
                    store = Store(state / "synthetic.sqlite")
                    memory = HindsightMemory(config.memory, client=client)
                raw_recalls.clear()
                row = await evaluate_probe(config, store, memory, probe, fixture, documents,
                                           replies=False)
                row["raw_recall_responses"] = list(raw_recalls)
                raw_text = json.dumps(raw_recalls, ensure_ascii=False)
                row["expected_document_reference_hits"] = [
                    document for document in row["expected_document_ids"] if document in raw_text]
                row["future_document_reference_hits"] = [
                    documents[source["id"]] for source in fixture["sources"]
                    if parse_time(source["timestamp"]) > parse_time(probe["timestamp"])
                    and documents[source["id"]] in raw_text]
                report["rows"].append(row)
                report["summary"] = summary(report["rows"])
                atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2))
                print(f"{args.label}: {index + 1}/{len(fixture['probes'])} {probe['id']} "
                      f"recall={row['recall_evidence']['coverage']} "
                      f"context={row['context_chars']} chars", flush=True)
            if report["provider"]["replies_enabled"]:
                completed_replies = 0

                def persist_reply(row):
                    nonlocal completed_replies
                    completed_replies += 1
                    report["summary"] = summary(report["rows"])
                    atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2))
                    print(f"{args.label}: replies {completed_replies}/{len(report['rows'])} "
                          f"{row['id']}", flush=True)

                await evaluate_replies(report["rows"], profile,
                                       concurrency=args.reply_concurrency,
                                       on_update=persist_reply)
        report["application_revision"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
            check=True).stdout.strip()
        report["source_changed_during_run"] = source_hashes() != report["source_sha256"]
        atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Private synthetic report: {report_path}", flush=True)
        return report
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True,
                        help="Isolated loopback API; live 9177/9191 ports are refused")
    parser.add_argument("--bank", required=True, help="New eidolon-phase0-synthetic-* bank ID")
    parser.add_argument("--label", required=True, help="Unique private report label")
    parser.add_argument("--reuse-bank", action="store_true",
                        help="Read the exact previously seeded synthetic bank; never reseed")
    parser.add_argument("--provider-config", type=Path,
                        help="Explicit private YAML config; only selected provider is read")
    parser.add_argument("--no-replies", action="store_true",
                        help="Preserve selected provider settings for timing-only prompt capture")
    parser.add_argument("--reply-concurrency", type=int, default=1,
                        help="Independent final reply requests after sequential preparation, 1-8")
    parser.add_argument("--index-timeout", type=float, default=900,
                        help="Whole synthetic corpus indexing deadline, seconds")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        # Backend/validation exception bodies may contain sensitive runtime settings.
        message = str(error) if isinstance(error, (ValueError, MemoryUnavailable)) else type(error).__name__
        print("Synthetic baseline failed: " + message, file=sys.stderr)
        return 1
    return int(report["summary"]["failed_probes"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
