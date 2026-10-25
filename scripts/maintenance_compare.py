"""Compare paired first attempts; print only aggregate synthetic assessment data.

Accepts complete maintenance_baseline reports or the frozen scripted reference.
Never contacts a provider or reads default application configuration.
"""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


ACCEPTED = {"accepted_change", "accepted_no_change"}
OUTCOMES = ACCEPTED | {"malformed", "empty", "truncated", "provider_rejection",
                       "provider_failure", "validation_rejection", "unresolved"}
RECOVERY_SCENARIOS = {"malformed", "empty", "truncated", "transient", "configuration", "restart",
                      "split", "retained_gap", "chronology", "justified_no_change"}


def samples(report: dict) -> dict[str, dict]:
    frozen = "rows" not in report and report.get("synthetic") is True
    if report.get("recovery_enabled") is not False or report.get("mode") not in {"scripted", "live"}:
        raise ValueError("Only first-attempt scripted or live reports can be compared")
    if frozen:
        if report["mode"] != "scripted" or not report.get("production_source_sha256"):
            raise ValueError("Frozen references require scripted source provenance")
        rows = [{**row, "sample_id": row["case_id"] + ":1"} for row in report["cases"]]
        planned = report["samples"]
    else:
        if report.get("complete") is not True:
            raise ValueError("Incomplete reports cannot be compared")
        rows, planned = report["rows"], report["planned_samples"]
    indexed = {row["sample_id"]: row for row in rows}
    if not rows or len(rows) != planned or len(indexed) != len(rows):
        raise ValueError("Missing or duplicate paired samples")
    if any(row["outcome"] not in OUTCOMES or type(row["expectation_match"]) is not bool
           or (row["expectation_match"] and row["outcome"] not in ACCEPTED) for row in rows):
        raise ValueError("Invalid outcome or expectation result")
    for row in rows:
        if not frozen and (type(row["requests"]) is not int or row["requests"] not in {0, 1}):
            raise ValueError("Each first attempt permits at most one dispatch")
        review = row.get("review")
        if review is not None:
            if (set(review) != {"verdict", "rationale"}
                    or review["verdict"] not in {"useful_faithful", "unsafe", "unresolved"}
                    or not isinstance(review["rationale"], str) or not review["rationale"].strip()):
                raise ValueError("Invalid human review")
            if review["verdict"] == "useful_faithful" and row["outcome"] not in ACCEPTED:
                raise ValueError("Failed attempts cannot establish useful faithful outcomes")
    return indexed


def aggregate(report: dict, rows: dict[str, dict]) -> dict:
    values = list(rows.values())
    counts = Counter(row["outcome"] for row in values)
    reviews = [row.get("review") for row in values]
    fully_reviewed = report["mode"] == "live" and all(reviews)
    verdicts = Counter(review["verdict"] for review in reviews if review)
    def matched(outcome: str) -> int:
        return sum(row["outcome"] == outcome and row["expectation_match"] for row in values)
    latencies = [row["latency_seconds"] for row in values if "latency_seconds" in row]
    usage = {}
    for key in sorted({key for row in values for key in row.get("usage") or {}}):
        known = [row["usage"][key] for row in values if key in (row.get("usage") or {})]
        usage[key] = {"known_samples": len(known), "sum_known": sum(known),
                      "total": sum(known) if len(known) == len(values) else None}
    return {"samples": len(values), "outcomes": dict(sorted(counts.items())),
            "failed_attempts": sum(row["outcome"] not in ACCEPTED for row in values),
            "expected_changes": matched("accepted_change"),
            "justified_no_change": matched("accepted_no_change"),
            "unjustified_no_change": counts["accepted_no_change"] - matched("accepted_no_change"),
            "unresolved_expectations": sum(not row["expectation_match"] for row in values),
            "requests": report.get("requests") if "rows" not in report else
                        sum(row["requests"] for row in values),
            "usage": usage or None, "monetary_cost": None,
            "latency_seconds": {"known_samples": len(latencies),
                                "sum_known": sum(latencies) if latencies else None},
            "reviewed_samples": sum(bool(review) for review in reviews),
            "useful_faithful": verdicts["useful_faithful"] if fully_reviewed else None,
            "unsafe": verdicts["unsafe"] if fully_reviewed else None}


def compare_reports(baseline: dict, candidate: dict) -> dict:
    old, new = samples(baseline), samples(candidate)
    for key in ("workload", "workload_sha256", "mode"):
        if not baseline.get(key) or baseline[key] != candidate.get(key):
            raise ValueError("Reports require the same workload hash and execution mode")
    if old.keys() != new.keys() or any(old[key]["case_id"] != new[key]["case_id"] or
            old[key]["category"] != new[key]["category"] for key in old):
        raise ValueError("Reports require identical paired samples")
    if baseline["mode"] == "live":
        for key in ("endpoint", "model", "transport", "mode"):
            before, after = baseline.get("identity", {}).get(key), candidate.get("identity", {}).get(key)
            if not before or before != after:
                raise ValueError("Live comparisons require the same endpoint/model/transport/route mode")
    before, after = aggregate(baseline, old), aggregate(candidate, new)
    preserved = all(not row["expectation_match"] or new[key]["expectation_match"] for key, row in old.items())
    resolved_failures_are_useful = all(new[key]["expectation_match"] for key, row in old.items()
                                      if row["outcome"] not in ACCEPTED
                                      and new[key]["outcome"] in ACCEPTED)
    improved = (after["failed_attempts"] < before["failed_attempts"] and preserved
                and resolved_failures_are_useful
                and after["expected_changes"] >= before["expected_changes"]
                and after["unjustified_no_change"] <= before["unjustified_no_change"])
    quality = None
    if before["unsafe"] is not None and after["unsafe"] is not None:
        quality = (improved and after["unsafe"] <= before["unsafe"]
                   and after["useful_faithful"] >= before["useful_faithful"]
                   and all(row["review"]["verdict"] != "useful_faithful" or
                           new[key]["review"]["verdict"] == "useful_faithful"
                           for key, row in old.items()))
    return {"workload": baseline["workload"], "workload_sha256": baseline["workload_sha256"],
            "mode": baseline["mode"], "recovery_enabled": False,
            "baseline": before, "candidate": after,
            "application_improvement_demonstrated": improved,
            "model_quality_improvement_demonstrated": quality,
            "changed_samples": [{"sample_id": key, "before": old[key]["outcome"],
                                 "after": new[key]["outcome"],
                                 "expectation_match": new[key]["expectation_match"]}
                                for key in old if (old[key]["outcome"], old[key]["expectation_match"]) !=
                                (new[key]["outcome"], new[key]["expectation_match"])],
            "limits": "Scripted outcomes establish application behavior only. Unknown usage, "
                      "cost, historical latency and unreviewed safety remain unknown. "
                      "No numeric reliability threshold is assumed."}


def recovery_summary(path: Path, *, expected_scenarios: set[str] = RECOVERY_SCENARIOS) -> dict:
    """Read aggregate properties emitted by the service-boundary assessment tests."""
    root = ET.parse(path).getroot()
    if root.find(".//failure") is not None or root.find(".//error") is not None:
        raise ValueError("Failed validation cannot establish a completed assessment")
    rows = [json.loads(prop.attrib["value"]) for prop in root.iter("property")
            if prop.get("name") == "maintenance_assessment"]
    if len(rows) != len(expected_scenarios) or {row["scenario"] for row in rows} != expected_scenarios:
        raise ValueError("Missing or duplicate recovery scenarios")
    if any(row["mode"] != "scripted" or row["calls"] != len(row["attempts"]) for row in rows):
        raise ValueError("Invalid synthetic attempt accounting")
    return {"mode": "scripted", "samples": len(rows),
            "calls": sum(row["calls"] for row in rows),
            "failed_attempts": sum(count for row in rows for outcome, count in
                                   row["attempt_outcomes"].items() if outcome != "committed"),
            "attempt_outcomes": dict(sum((Counter(row["attempt_outcomes"]) for row in rows), Counter())),
            "committed_attempts": sum(row["attempt_outcomes"].get("committed", 0) for row in rows),
            "recovered_scenarios": sum(row["recovered_failure"] for row in rows),
            "justified_no_change": sum(row["justified_no_change"] for row in rows),
            "unresolved_evidence": sum(row["evidence"]["unresolved"] for row in rows),
            "latency_seconds": sum(row["latency_seconds"] for row in rows),
            "usage_kind": "Fabricated counters test accounting only; live token cost is unknown.",
            "scenarios": sorted(rows, key=lambda row: row["scenario"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--recovery-junit", type=Path,
                        help="Include measured synthetic service recovery from a passing JUnit run")
    args = parser.parse_args()
    try:
        result = compare_reports(json.loads(args.baseline.read_text()),
                                 json.loads(args.candidate.read_text()))
        if args.recovery_junit:
            result["recovery"] = recovery_summary(args.recovery_junit)
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError):
        parser.exit(1, "Comparison refused: provide complete, compatible first-attempt reports.\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
