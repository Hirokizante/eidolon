"""Replay captured synthetic recall responses offline; never contact a service.

This measures packing and evidence preservation, not recall, reply quality, or
backend latency. Inputs must be explicitly marked synthetic and contain the raw
successful recall responses captured by memory_baseline.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path

from eidolon.config import ProviderConfig, atomic_write
from eidolon.providers.context import ContextBudget, TokenCounter
from eidolon.memory.coordinator import assemble_context
from eidolon.memory.models import MemoryRecord, RecallResult


def stats(values: list[float | int]) -> dict:
    ordered = sorted(values)
    return {"count": len(ordered), "mean": statistics.mean(ordered) if ordered else None,
            "p50": statistics.median(ordered) if ordered else None,
            "p95": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
            "max": ordered[-1] if ordered else None}


def atom_matches(text: str, groups: list[list[str]]) -> list[bool]:
    folded = text.casefold()
    return [any(atom.casefold() in folded for atom in group) for group in groups]


def validate_report(report: dict) -> None:
    if report.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic reports are accepted")
    rows = report.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("A report must contain captured probe rows")
    provider = report.get("provider", {})
    if not any(isinstance(provider.get(key), int) for key in ("context_window", "context_chars")):
        raise ValueError("A report must declare its provider context allowance")
    for row in rows:
        raw = row.get("raw_recall_responses")
        if (not isinstance(raw, list) or not raw or not isinstance(raw[-1], dict)
                or not isinstance(raw[-1].get("results"), list)):
            raise ValueError("Every probe must contain an existing raw successful recall response")
        if not isinstance(row.get("recalled_context"), str) or not isinstance(row.get("messages"), list):
            raise ValueError("Every probe must contain its baseline rendered context and messages")
        groups = row.get("expected_atoms")
        if not isinstance(groups, list) or any(
            not isinstance(group, list) or not group
            or any(not isinstance(atom, str) or not atom for atom in group) for group in groups
        ):
            raise ValueError("Expected evidence must be nonempty atom-alternative groups")


def typed_result(row: dict, max_memories: int) -> RecallResult:
    # The adapter uses the final successful response after any query fallback.
    raw = row["raw_recall_responses"][-1]
    return RecallResult(
        records=tuple(MemoryRecord.model_validate(item) for item in raw["results"][:max_memories]),
        query=row.get("query", ""), effort="low", usage=raw.get("usage"),
        chunks=raw.get("chunks"), source_facts=raw.get("source_facts"),
        source_facts_truncated=raw.get("source_facts_truncated"), trace=raw.get("trace"),
    )


def replay_report(report: dict, *, max_chars: int = 8000, max_memories: int = 12,
                  repetitions: int = 20) -> dict:
    validate_report(report)
    if max_chars < 0 or not 1 <= max_memories <= 50 or repetitions < 1:
        raise ValueError("Use a nonnegative budget, 1–50 memories and positive repetitions")
    rows, assembly_ms, typed_assembly_ms = [], [], []
    provenance = Counter()
    for row in report["rows"]:
        result = typed_result(row, max_memories)
        context = assemble_context(result, max_chars=max_chars)
        # Warm each response before timing local work. Disk reads and report
        # validation are excluded, as are all historical transport/model times.
        for _ in range(repetitions):
            started = time.perf_counter_ns()
            assemble_context(result, max_chars=max_chars)
            assembly_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            started = time.perf_counter_ns()
            assemble_context(typed_result(row, max_memories), max_chars=max_chars)
            typed_assembly_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        entries = context.manifest["entries"]
        for entry in entries:
            provenance["selected_records"] += 1
            for label in ("date", "type", "source", "origin"):
                available = entry[label] != "unknown"
                provenance[label + ("_known" if available else "_unknown")] += 1
            # Provenance stays in the private manifest. The compact prompt
            # intentionally excludes backend identifiers and diagnostic labels.
            identifiers = (entry.get("id"), entry.get("document_id"), entry.get("chunk_id"),
                           *(entry.get("source_fact_ids") or []))
            provenance["backend_identifiers_in_prompt"] += sum(
                bool(identifier and identifier in entry["rendered"])
                for identifier in identifiers
            )
            provenance["whole_text_preserved"] += int(entry["text"] in entry["rendered"])
        baseline = row["recalled_context"]
        before, after = [atom_matches(text, row["expected_atoms"])
                         for text in (baseline, context.text)]
        messages = []
        replacements = 0
        for message in row["messages"]:
            message = dict(message)
            content = message.get("content")
            if baseline and isinstance(content, str) and baseline in content:
                replacements += content.count(baseline)
                message["content"] = content.replace(baseline, context.text)
            messages.append(message)
        if baseline and replacements != 1:
            raise ValueError("Baseline recall context must occur exactly once in captured messages")
        prompt_chars = len("\n".join(json.dumps(message, ensure_ascii=False) for message in messages))
        provider = report["provider"]
        limit = provider.get("context_chars")  # Historical character-budget captures remain readable.
        token_window = provider.get("context_window")
        estimated_tokens = TokenCounter(provider.get("model", "")).request(messages) if token_window else None
        budget = ContextBudget.for_profile(ProviderConfig(
            context_window=token_window, parameters=provider.get("parameters", {})))
        rows.append({
            "id": row["id"], "category": row.get("category", "unknown"),
            "baseline_memory_chars": len(baseline), "phase1_memory_chars": len(context.text),
            "raw_records": len(row["raw_recall_responses"][-1]["results"]),
            "candidate_records": len(result.records), "selected_records": len(entries),
            "omitted": context.manifest["omitted"],
            "baseline_atom_matches": before, "phase1_atom_matches": after,
            "atom_groups": len(before), "atom_groups_lost": sum(a and not b for a, b in zip(before, after)),
            "atom_groups_gained": sum(b and not a for a, b in zip(before, after)),
            "phase1_prompt_chars_estimate": prompt_chars,
            "provider_context_allowance_chars": limit,
            "provider_context_window_tokens": token_window,
            "phase1_prompt_tokens_estimate": estimated_tokens,
            "estimated_prompt_fits": (budget.fits(estimated_tokens) if token_window
                                      else prompt_chars <= limit),
        })

    def coverage(items: list[dict], key: str) -> dict:
        scored = [row for row in items if row["atom_groups"]]
        total = sum(row["atom_groups"] for row in scored)
        return {"scored_probes": len(scored), "groups": total,
                "matched_groups": sum(sum(row[key]) for row in scored),
                "mean_probe_coverage": statistics.mean(
                    sum(row[key]) / row["atom_groups"] for row in scored
                ) if scored else None}

    return {
        "label": report.get("label"), "synthetic": True, "probes": len(rows),
        "method": {
            "max_chars": max_chars, "max_memories": max_memories, "effort": "low",
            "raw_response": "last successful captured response",
            "timing_repetitions_per_probe": repetitions,
            "timing_scope": "Local in-process validation/assembly only; no backend/model latency measured",
            "prompt_estimate": "Replace baseline recall text in captured messages; other context unchanged",
            "limits": "Frozen retrieved results; atom coverage is not reply quality or temporal correctness",
        },
        "baseline_atom_coverage": coverage(rows, "baseline_atom_matches"),
        "phase1_atom_coverage": coverage(rows, "phase1_atom_matches"),
        "atom_groups_lost": sum(row["atom_groups_lost"] for row in rows),
        "atom_groups_gained": sum(row["atom_groups_gained"] for row in rows),
        "probes_with_loss": sum(bool(row["atom_groups_lost"]) for row in rows),
        "probes_with_gain": sum(bool(row["atom_groups_gained"]) for row in rows),
        "memory_chars_baseline": stats([row["baseline_memory_chars"] for row in rows]),
        "memory_chars_phase1": stats([row["phase1_memory_chars"] for row in rows]),
        "phase1_prompt_chars_estimate": stats([row["phase1_prompt_chars_estimate"] for row in rows]),
        "prompts_estimated_over_allowance": sum(not row["estimated_prompt_fits"] for row in rows),
        "records_selected": sum(row["selected_records"] for row in rows),
        "records_omitted": sum(len(row["omitted"]) for row in rows),
        "omission_reasons": dict(Counter(omitted["reason"] for row in rows for omitted in row["omitted"])),
        "provenance": dict(provenance),
        "assembly_ms": stats(assembly_ms), "typed_validation_and_assembly_ms": stats(typed_assembly_ms),
        "by_category": {category: {
            "baseline": coverage([r for r in rows if r["category"] == category], "baseline_atom_matches"),
            "phase1": coverage([r for r in rows if r["category"] == category], "phase1_atom_matches"),
        } for category in sorted({row["category"] for row in rows})},
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if any((parent / ".git").exists() for parent in output.parents):
        parser.error("Replay artifacts must remain outside Git checkouts")
    if output.exists():
        parser.error("Refusing to overwrite an existing artifact")
    reports = []
    for source in args.report:
        source = source.expanduser().resolve()
        raw = source.read_bytes()
        report = replay_report(json.loads(raw), repetitions=args.repetitions)
        reports.append({"source": str(source), "source_sha256": hashlib.sha256(raw).hexdigest(),
                        **report})
    source_root = Path(__file__).resolve().parents[1]
    source_paths = [Path(__file__), source_root / "src/eidolon/memory/models.py",
                    source_root / "src/eidolon/memory/coordinator.py"]
    artifact = {"synthetic": True, "offline": True, "reports": reports,
                "source_sha256": {str(path.relative_to(source_root)): hashlib.sha256(
                    path.read_bytes()).hexdigest() for path in source_paths}}
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(output, json.dumps(artifact, ensure_ascii=False, indent=2))
    print(str(output))


if __name__ == "__main__":
    main()
