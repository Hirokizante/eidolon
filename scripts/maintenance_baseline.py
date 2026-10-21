"""Opt-in first-attempt Current Life evaluation using only synthetic evidence.

No default configuration, service, memory server, or recovery loop is started.
See docs/MAINTENANCE_BASELINE.md for execution and human review.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import aclosing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import yaml

from eidolon.config import AppConfig, AuxiliaryModelConfig, IdentityConfig, atomic_write
from eidolon.memory.continuity import validate_operations
from eidolon.memory.maintenance import ContinuityWorker, numeric_usage
from eidolon.providers.openai import (
    OpenAIProvider, ProviderCapabilityError, ProviderError, ProviderOutputLimitError,
)
from eidolon.providers.routing import configured_route_fingerprint, resolve_auxiliary
from eidolon.storage import Store

from memory_baseline import ROOT, isolated_home, load_fixture, parse_time


WORKLOAD = ROOT / "tests/fixtures/maintenance_baseline_v1.json"
OUTCOMES = ("accepted_change", "accepted_no_change", "malformed", "empty", "truncated",
            "provider_rejection", "provider_failure", "validation_rejection", "unresolved")
VERDICTS = {"useful_faithful", "unsafe", "unresolved"}


def load_workload() -> tuple[dict, dict[str, dict], str]:
    companion, companion_hash = load_fixture()
    raw = WORKLOAD.read_bytes()
    workload = json.loads(raw)
    if workload["version"] != 1 or workload["synthetic"] is not True:
        raise ValueError("Only the versioned synthetic workload is accepted")
    if workload["companion_sha256"] != companion_hash:
        raise ValueError("Companion evidence changed; version the workload and expectations")
    sources = {source["id"]: source for source in companion["sources"]}
    for source in workload["extra_sources"]:
        if source["id"] in sources:
            raise ValueError("Duplicate source ID")
        parse_time(source["timestamp"])
        sources[source["id"]] = source
    ids = [case["id"] for case in workload["cases"]]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Case IDs must be nonempty and unique")
    for case in workload["cases"]:
        for source_id in [*case["inputs"], *(n["source"] for n in case["notes"]
                                             if "source" in n)]:
            if source_id not in sources:
                raise ValueError("Case references missing evidence")
        if not case["rubric"]:
            raise ValueError("Every case needs an independent review rubric")
    return workload, sources, hashlib.sha256(raw + companion_hash.encode()).hexdigest()


def read_profile(path: Path | None, *, live: bool) -> AppConfig:
    if not live:
        if path is not None:
            raise ValueError("Provider configuration is only read in explicitly enabled live mode")
        return AppConfig(auxiliary=AuxiliaryModelConfig(context_window=32768))
    if path is None:
        raise ValueError("Live trials require an explicit provider configuration")
    resolved = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in resolved.parents):
        raise ValueError("Provider configuration must be outside Git checkouts")
    # Deliberately avoid load_config, prompts, paths, personal identity and memory settings.
    data = yaml.safe_load(resolved.read_text())
    config = AppConfig(providers=data["providers"], provider=data["provider"],
                       auxiliary=data["auxiliary"])
    if config.auxiliary.mode == "disabled":
        raise ValueError("Select an enabled auxiliary route for a live trial")
    profile = resolve_auxiliary(config, revision=1).profile()
    endpoint = urlsplit(profile.endpoint)
    if endpoint.query or endpoint.fragment:
        raise ValueError("Evaluation endpoints must not contain queries or fragments")
    return config


def add_source(store: Store, source: dict, case_id: str) -> dict:
    timestamp = parse_time(source["timestamp"])
    session = store.session_for(timestamp, "Asia/Manila", 4)
    generation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, case_id + ":" + source["id"]))
    with patch("eidolon.storage.store.utc_now", return_value=source["timestamp"]):
        store.complete_turn(session["id"], source["user"], source["assistant"],
                            generation_id=generation_id, retain=True,
                            metadata={"memory_scope": source.get("scope", "real_life")})
    return store.recent_continuity_inputs(limit=1)[0]


def upsert(note: dict, source: dict) -> dict:
    return {"op": "upsert", "expected_revision": 0, "topic": note["topic"],
            "importance": note.get("importance", "moderate"), "category": "thread",
            "content": note["content"], "content_format": "narrative", "origin": "derived",
            "scope": "real_life", "sources": [{"exchange_id": source["exchange_id"],
                "document_id": source["document_id"], "role": "user", "origin": "user_statement"}]}


def prepare_case(store: Store, case: dict, sources: dict[str, dict]) -> dict[str, dict]:
    """Seed supported learned notes through validation/commit; ownership stays real."""
    originals = {}
    for note in case["notes"]:
        if note.get("protected"):
            saved = store.create_continuity_item(note["content"], "thread", topic=note["topic"],
                                                importance=note.get("importance", "moderate"))
        else:
            source = add_source(store, sources[note["source"]], case["id"])
            originals[note["source"]] = source
            state = store.continuity_state()
            operation = upsert(note, source)
            if note.get("legacy"):
                operation.pop("content_format")
                operation["origin"] = "user_statement"
                operation["sources"][0]["excerpt"] = source["user"]
            checked = validate_operations({"operations": [operation]}, inputs=[source], state=state)
            task = store.create_maintenance_task([source], expected_revision=state["revision"],
                                                 control_revision=state["control_revision"])
            store.commit_continuity(task["id"], checked, expected_revision=state["revision"],
                                    control_revision=state["control_revision"])
            saved = next(item for item in store.continuity_state()["items"]
                         if item["topic"] == note["topic"])
            if note.get("locked"):
                saved = store.edit_continuity_item(saved["id"], {"locked": True},
                                                   expected_revision=saved["revision"])
        originals[note["key"]] = saved
    for source_id in case["inputs"]:
        originals[source_id] = add_source(store, sources[source_id], case["id"])
    return originals


def expand(value: object, refs: dict[str, dict], revision: int) -> object:
    """Bind fixture-local references without depending on generated database IDs."""
    if isinstance(value, list):
        return [expand(item, refs, revision) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, refs, revision) for key, item in value.items()}
    if value == "$revision":
        return revision
    if isinstance(value, str) and value.startswith("$"):
        ref, field = value[1:].split(".", 1)
        return refs[ref][field]
    return value


class ScriptedProvider:
    """A fixture answer, never an inference or a quality result."""

    def __init__(self, profile, response: object):
        self.config, self.response = profile, response

    async def stream(self, messages: list[dict], **kwargs):
        yield {"type": "request", "endpoint": "scripted://offline"}
        yield {"type": "token", "text": json.dumps(self.response)}
        yield {"type": "finish", "reason": "stop"}


class SingleDispatchTransport(httpx.AsyncBaseTransport):
    """Count actual HTTP sends and forbid a second dispatch, including retries."""

    def __init__(self, transport: httpx.AsyncBaseTransport):
        self.transport = transport
        self.requests = 0
        self.status_code: int | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.requests:
            raise RuntimeError("Recovery is disabled in first-attempt evaluation")
        self.requests += 1
        response = await self.transport.handle_async_request(request)
        self.status_code = response.status_code
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


class ObservedProvider:
    def __init__(self, provider):
        self.provider, self.config = provider, provider.config
        self.calls = 0
        self.requests = 0
        self.text = ""
        self.finish: str | None = None
        self.failure: str | None = None
        self.messages: list[dict] = []
        self.endpoint: str | None = None
        self.usage: dict = {}

    async def stream(self, messages: list[dict], **kwargs):
        if self.calls:
            raise RuntimeError("Recovery is disabled in first-attempt evaluation")
        self.calls += 1
        self.messages = messages
        try:
            async with aclosing(self.provider.stream(messages, **kwargs)) as stream:
                async for event in stream:
                    if event["type"] == "token" and isinstance(event.get("text"), str):
                        self.text += event["text"]
                    elif event["type"] == "request":
                        self.requests += 1
                        self.endpoint = event.get("endpoint")
                    elif event["type"] == "finish":
                        self.finish = event.get("reason")
                    elif event["type"] == "usage":
                        self.usage.update(numeric_usage(event.get("usage")))
                    yield event
        except ProviderOutputLimitError:
            self.failure = "truncated"
            raise
        except ProviderCapabilityError:
            self.failure = "provider_rejection"
            raise
        except ProviderError:
            self.failure = "provider_failure"
            raise


def expectation_checks(case: dict, before: list[dict], after: list[dict]) -> dict[str, bool]:
    changed = [item for item in after if item not in before]
    checks = {"change_count": len(changed) == len(case["expected"])}
    for index, expected in enumerate(case["expected"]):
        matches = [item for item in changed if item["topic"] == expected["topic"]]
        prefix = str(index) + ":"
        checks[prefix + "topic"] = len(matches) == 1
        if len(matches) != 1:
            continue
        item = matches[0]
        checks[prefix + "fields"] = all(item.get(key) == value for key, value in
            expected.items() if key not in {"atoms", "forbidden", "source_roles"})
        checks[prefix + "atoms"] = all(any(atom.casefold() in item["content"].casefold()
            for atom in group) for group in expected.get("atoms", []))
        checks[prefix + "forbidden"] = not any(atom.casefold() in item["content"].casefold()
            for atom in expected.get("forbidden", []))
        checks[prefix + "source_roles"] = set(expected.get("source_roles", [])) <= {
            source["role"] for source in item.get("sources", [])}
    checks["protected"] = all(item in after for item in before if
                                item.get("locked") or item.get("user_created"))
    return checks


def outcome(provider: ObservedProvider, committed: bool, changes: bool,
            status_code: int | None = None) -> str:
    if status_code is not None and status_code >= 400:
        return "provider_rejection"
    if provider.failure:
        return provider.failure
    if committed:
        return "accepted_change" if changes else "accepted_no_change"
    if not provider.calls:
        return "unresolved"
    if provider.finish not in {None, "stop"}:
        return "truncated"
    if not provider.text.strip():
        return "empty" if provider.finish == "stop" else "unresolved"
    if provider.finish is None:
        return "truncated"
    try:
        proposal = json.loads(provider.text)
    except ValueError:
        return "malformed"
    return "validation_rejection" if isinstance(proposal, dict) else "malformed"


async def evaluate_case(case: dict, sources: dict[str, dict], directory: Path,
                        config: AppConfig, *, live: bool = False,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict:
    directory.mkdir(mode=0o700)
    store = Store(directory / "room.sqlite3")
    worker = None
    try:
        refs = prepare_case(store, case, sources)
        before = store.continuity_state()["items"]
        route = store.reconcile_auxiliary_route(configured_route_fingerprint(config))
        execution = resolve_auxiliary(config, revision=route["revision"])
        profile = execution.profile()
        response = expand(case["scripted"], refs, store.continuity_state()["revision"])
        dispatch = SingleDispatchTransport(transport or httpx.AsyncHTTPTransport()) if live else None
        async with httpx.AsyncClient(transport=dispatch, timeout=execution.timeout_seconds,
                                     follow_redirects=False) as client:
            base = OpenAIProvider(profile, client=client, stream_usage=execution.stream_usage) if (
                live) else ScriptedProvider(profile, response)
            observed = ObservedProvider(base)
            worker = ContinuityWorker(store, observed, timezone="Asia/Manila",
                max_notes=case.get("max_notes", 12), timeout=execution.timeout_seconds,
                max_input_chars=execution.context_chars, execution=execution,
                identity=IdentityConfig(user_name="Aster", companion_name="Vale"))
            started = time.perf_counter()
            # Exactly one worker pass. No service scheduler, corrections, splits or retry.
            committed = await worker.run_once(force=True)
            elapsed = time.perf_counter() - started
        after = store.continuity_state()["items"]
        checks = expectation_checks(case, before, after)
        result = outcome(observed, committed, before != after,
                         dispatch.status_code if dispatch else None)
        return {"case_id": case["id"], "category": case["category"], "rubric": case["rubric"],
            "outcome": result, "committed": committed, "checks": checks,
            "expectation_match": committed and all(checks.values()),
            "review": None, "requests": dispatch.requests if dispatch else observed.requests,
            "provider_invocations": observed.calls, "latency_seconds": elapsed,
            "usage": observed.usage or None, "cost": None,
            "cost_note": "No price schedule supplied; token usage is not monetary cost.",
            "request_metrics": [row["metrics"] for row in store.list_request_metrics()],
            "before": before, "after": after, "messages": observed.messages,
            "response": observed.text, "finish_reason": observed.finish,
            "request_endpoint": observed.endpoint,
            "status_code": dispatch.status_code if dispatch else None,
            "attempts": store.maintenance_status()["stages"]}
    finally:
        if worker:
            await worker.close()
        store.close()


def summarize(rows: list[dict]) -> dict:
    reviewed = [row for row in rows if row["review"] is not None]
    counts = Counter(row["outcome"] for row in rows)
    verdicts = Counter(row["review"]["verdict"] for row in reviewed)
    usage = {}
    for key in sorted({key for row in rows for key in row.get("usage") or {}}):
        values = [row["usage"][key] for row in rows if row.get("usage") and key in row["usage"]]
        usage[key] = {"known_samples": len(values), "sum_known": sum(values),
                      "total": sum(values) if len(values) == len(rows) else None}
    return {"samples": len(rows), "requests": sum(row["requests"] for row in rows),
        "outcomes": {name: counts[name] for name in OUTCOMES},
        "expectation_matches": sum(row["expectation_match"] for row in rows),
        "reviewed_samples": len(reviewed), "unreviewed_samples": len(rows) - len(reviewed),
        "reviewed_verdicts": {name: verdicts[name] for name in sorted(VERDICTS)},
        "useful_faithful_rate": verdicts["useful_faithful"] / len(rows) if rows and
            len(reviewed) == len(rows) else None,
        "unsafe_rate": verdicts["unsafe"] / len(rows) if rows and len(reviewed) == len(rows)
            else None,
        "latency_seconds": {"count": len(rows), "mean": sum(r["latency_seconds"] for r in rows)
            / len(rows) if rows else None,
            "p95": sorted(r["latency_seconds"] for r in rows)[max(0, (len(rows)*95+99)//100-1)]
            if rows else None},
        "usage": usage, "cost": None,
        "estimated_cost_usd": {
            "known_samples": sum(r.get("estimated_cost_usd") is not None for r in rows),
            "sum_known": sum(r.get("estimated_cost_usd") or 0 for r in rows),
            "total": sum(r["estimated_cost_usd"] for r in rows) if rows and all(
                r.get("estimated_cost_usd") is not None for r in rows) else None},
        "by_category": {category: {"samples": sum(r["category"] == category for r in rows),
            "expectation_matches": sum(r["expectation_match"] for r in rows
                                       if r["category"] == category)}
            for category in sorted({r["category"] for r in rows})}}


def apply_reviews(report: dict, reviews: dict) -> dict:
    """Review binds to a particular report hash in main, never to mutable case IDs alone."""
    if report["mode"] != "live":
        raise ValueError("Scripted checks cannot be promoted to model quality results")
    if not report.get("complete"):
        raise ValueError("Incomplete runs cannot be promoted to completed quality results")
    rows = {row["sample_id"]: row for row in report["rows"]}
    if set(reviews) - rows.keys():
        raise ValueError("Review contains an unknown sample ID")
    for sample_id, review in reviews.items():
        if (set(review) != {"verdict", "rationale"} or review["verdict"] not in VERDICTS
                or not isinstance(review["rationale"], str) or not review["rationale"].strip()):
            raise ValueError("Reviews require a verdict and written rationale")
        if review["verdict"] == "useful_faithful" and not rows[sample_id]["committed"]:
            raise ValueError("Failed or unresolved attempts cannot be useful faithful outcomes")
        rows[sample_id]["review"] = review
    report["summary"] = summarize(report["rows"])
    return report


def provenance() -> dict:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()
    paths = [*sorted((ROOT / "src/eidolon/memory").glob("*.py")),
             ROOT / "src/eidolon/storage/store.py", ROOT / "src/eidolon/providers/openai.py",
             ROOT / "src/eidolon/providers/routing.py", ROOT / "src/eidolon/providers/context.py",
             ROOT / "src/eidolon/providers/metrics.py", ROOT / "src/eidolon/config.py",
             ROOT / "uv.lock", Path(__file__).resolve()]
    return {"revision": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in paths}}


async def run(args: argparse.Namespace) -> Path:
    live = args.mode == "live"
    if live and not args.allow_real_model:
        raise ValueError("Live trials require --allow-real-model and may incur provider charges")
    if not live and args.allow_real_model:
        raise ValueError("--allow-real-model requires --mode live")
    rates = [getattr(args, field, None) for field in
             ("input_usd_per_million", "output_usd_per_million")]
    if any(rate is not None for rate in rates) and (
            not live or any(rate is None or not math.isfinite(rate) or rate < 0 for rate in rates)):
        raise ValueError("Live cost estimates require two finite nonnegative token prices")
    workload, sources, digest = load_workload()
    config = read_profile(args.provider_config, live=live)
    home = isolated_home(os.environ.get("EIDOLON_HOME"), digest)
    run_id = "maintenance-" + uuid.uuid4().hex
    directory = home / "runs" / run_id
    directory.mkdir(mode=0o700)
    execution = resolve_auxiliary(config, revision=1)
    profile = execution.profile()
    report = {"schema_version": 1, "workload": workload["id"], "workload_sha256": digest,
        "mode": args.mode, "application": provenance(), "recovery_enabled": False,
        "complete": False, "planned_samples": len(workload["cases"]) * args.repetitions,
        "pricing": {"input_usd_per_million": rates[0], "output_usd_per_million": rates[1],
                    "kind": "user-supplied estimate; excludes discounts, fees and cache pricing"},
        "repetitions": args.repetitions, "identity": {
            "endpoint": profile.endpoint if live else "scripted://offline",
            "model": profile.model if live else "scripted-fixture-v1",
            "transport": "openai-compatible-chat-completions" if live else "scripted",
            "output_mode": "prompt-only-json", "schema_enforcement": False,
            **execution.provenance()}, "rows": []}
    path = directory / "report.json"
    atomic_write(directory / "workload.json", json.dumps(workload, indent=2))
    atomic_write(directory / "sources.json", json.dumps(sources, indent=2))
    for repetition in range(args.repetitions):
        for case in workload["cases"]:
            sample_id = f"{case['id']}:{repetition + 1}"
            row = await evaluate_case(case, sources, directory / sample_id.replace(":", "-"),
                                      config, live=live)
            metrics = row["request_metrics"]
            if (live and rates[0] is not None and len(metrics) == 1 and all(
                    metrics[0].get(key) is not None for key in ("input_tokens", "output_tokens"))):
                row["estimated_cost_usd"] = (metrics[0]["input_tokens"] * rates[0]
                    + metrics[0]["output_tokens"] * rates[1]) / 1_000_000
            else:
                row["estimated_cost_usd"] = None
            report["rows"].append({**row, "sample_id": sample_id})
            report["summary"] = summarize(report["rows"])
            atomic_write(path, json.dumps(report, indent=2, ensure_ascii=False))
    report["complete"] = True
    atomic_write(path, json.dumps(report, indent=2, ensure_ascii=False))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    trial = commands.add_parser("run", help="Run independent synthetic first attempts")
    trial.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    trial.add_argument("--allow-real-model", action="store_true")
    trial.add_argument("--provider-config", type=Path)
    trial.add_argument("--input-usd-per-million", type=float)
    trial.add_argument("--output-usd-per-million", type=float)
    trial.add_argument("--repetitions", type=int, choices=range(1, 101), default=1)
    review = commands.add_parser("review", help="Apply human review and print aggregate counts")
    review.add_argument("report", type=Path)
    review.add_argument("reviews", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "run":
            path = asyncio.run(run(args))
            report = json.loads(path.read_text())
            print(json.dumps(report["summary"], indent=2))
            print("Private synthetic report: " + str(path))
        else:
            raw = args.report.read_bytes()
            reviews = json.loads(args.reviews.read_text())
            if reviews["report_sha256"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("Review does not match the exact report bytes")
            report = apply_reviews(json.loads(raw), reviews["samples"])
            # Keep the original evidence immutable; review writes a separate artifact.
            atomic_write(args.report.with_name("reviewed-" + args.report.name),
                         json.dumps(report, indent=2, ensure_ascii=False))
            print(json.dumps(report["summary"], indent=2))
    except Exception:
        print("Evaluation stopped. Check explicit options, synthetic state isolation, "
              "workload and review inputs. No provider details are printed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
