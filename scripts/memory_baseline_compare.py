"""Compare private synthetic reports without copying memory or reply text to stdout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from scripts.memory_baseline import atom_score, load_fixture, summary
except ModuleNotFoundError:
    from memory_baseline import atom_score, load_fixture, summary


def rescore(report: dict) -> dict:
    """Exclude question overlap even for reports captured by the first runner revision."""
    for row in report["rows"]:
        messages = row.get("messages", [])
        recalled = row.get("recalled_context", "")
        digest = ""
        header = "Earlier conversation excerpts (the complete transcript remains in local history):\n"
        for message in messages:
            if message["role"] != "system":
                continue
            context = message["content"]
            if header in context:
                digest = context[context.index(header):]
        suffix = 2 if messages and messages[-1]["role"] == "system" else 1
        history = [m for m in messages[:-suffix] if m["role"] in {"user", "assistant"}]
        supplied = "\n".join([recalled, digest, *(m["content"] for m in history)])
        row["recall_evidence"] = atom_score(recalled, row["expected_atoms"])
        row["supplied_evidence"] = atom_score(supplied, row["expected_atoms"])
        row["question_atom_overlap"] = atom_score(row["question"], row["expected_atoms"])
    report["summary"] = summary(report["rows"])
    return report


def compare_reports(baseline: dict, candidate: dict) -> dict:
    fixture, digest = load_fixture()
    for report in (baseline, candidate):
        if (report.get("synthetic") is not True or report.get("fixture_sha256") != digest
                or len(report["rows"]) != len(fixture["probes"])):
            raise ValueError("Both reports must be complete captures of the current synthetic fixture")
        rescore(report)
    if baseline["bank"] != candidate["bank"]:
        raise ValueError("Comparison requires the same seeded synthetic bank")
    provider_fields = {"model", "context_window", "context_chars", "endpoint_sha256", "parameters_sha256",
                       "replies_enabled", "reply_concurrency"}
    differences = [field for field in sorted(provider_fields)
                   if baseline["provider"].get(field) != candidate["provider"].get(field)]
    if differences:
        raise ValueError("Provider comparison settings differ: " + ", ".join(differences))
    rows = []
    for old, new in zip(baseline["rows"], candidate["rows"], strict=True):
        for field in ("id", "question", "timestamp", "query", "expected_document_ids"):
            if old[field] != new[field]:
                raise ValueError("Probe identity or query differs: " + old["id"])
        old_score, new_score = old["recall_evidence"]["coverage"], new["recall_evidence"]["coverage"]
        rows.append({"id": old["id"], "category": old["category"],
                     "baseline_recall_coverage": old_score, "candidate_recall_coverage": new_score,
                     "recall_coverage_delta": new_score - old_score
                     if old_score is not None and new_score is not None else None,
                     "baseline_error": bool(old.get("error")),
                     "candidate_error": bool(new.get("error"))})
    return {
        "synthetic": True, "fixture_sha256": digest,
        "baseline_version": baseline["hindsight_version"],
        "candidate_version": candidate["hindsight_version"],
        "baseline_summary": baseline["summary"], "candidate_summary": candidate["summary"],
        "source_changed_during_run": bool(baseline.get("source_changed_during_run")
                                          or candidate.get("source_changed_during_run")),
        "application_source_differences": [name for name, old in baseline.get("source_sha256", {}).items()
                                           if candidate.get("source_sha256", {}).get(name) != old],
        "rows": rows,
        "scoring_limit": "Substring coverage measures evidence availability. Human rubric review "
                         "is required for attribution, dates, contradictions, fiction and abstention.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    result = compare_reports(json.loads(args.baseline.read_text()),
                             json.loads(args.candidate.read_text()))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
