"""Paired assessment cannot turn missing work or mixed trials into improvement."""
import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "maintenance_compare", Path(__file__).parents[1] / "scripts/maintenance_compare.py")
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def report(rows):
    return {"mode": "scripted", "workload": "synthetic-v1", "workload_sha256": "same",
            "recovery_enabled": False, "complete": True, "planned_samples": len(rows),
            "rows": [{"sample_id": f"{case}:1", "case_id": case, "category": "updates",
                      "outcome": outcome, "expectation_match": match, "requests": 1,
                      "review": None, "usage": None, "latency_seconds": 0.1}
                     for case, outcome, match in rows]}


def test_comparison_requires_fewer_failures_and_preserves_useful_changes():
    old = report([("update", "accepted_change", True),
                  ("closure", "validation_rejection", False)])
    new = report([("update", "accepted_no_change", False),
                  ("closure", "accepted_change", True)])
    result = compare.compare_reports(old, new)
    assert result["baseline"]["failed_attempts"] == 1
    assert result["candidate"]["failed_attempts"] == 0
    assert result["candidate"]["unjustified_no_change"] == 1
    assert result["application_improvement_demonstrated"] is False
    assert result["model_quality_improvement_demonstrated"] is None
    new["rows"][0].update(outcome="accepted_change", expectation_match=True)
    assert compare.compare_reports(old, new)["application_improvement_demonstrated"] is True


@pytest.mark.parametrize("mutation", ["hash", "recovery", "incomplete", "duplicate", "missing", "mixed"])
def test_incompatible_comparisons_are_refused(mutation):
    old = report([("update", "accepted_change", True), ("closure", "validation_rejection", False)])
    new = copy.deepcopy(old)
    if mutation == "hash":
        new["workload_sha256"] = "changed"
    elif mutation == "recovery":
        new["recovery_enabled"] = True
    elif mutation == "incomplete":
        new["complete"] = False
    elif mutation == "duplicate":
        new["rows"][1] = copy.deepcopy(new["rows"][0])
    elif mutation == "missing":
        new["rows"].pop()
    else:
        new["mode"] = "live"
    with pytest.raises(ValueError):
        compare.compare_reports(old, new)


def test_live_quality_requires_review_and_identical_route():
    old = report([("closure", "validation_rejection", False)])
    new = report([("closure", "accepted_change", True)])
    for item in (old, new):
        item.update(mode="live", identity={"endpoint": "https://synthetic.invalid/v1",
                    "model": "synthetic-small", "transport": "chat-completions", "mode": "separate"})
    assert compare.compare_reports(old, new)["model_quality_improvement_demonstrated"] is None
    old["rows"][0]["review"] = {"verdict": "unresolved", "rationale": "Rejected closure."}
    new["rows"][0]["review"] = {"verdict": "useful_faithful", "rationale": "Supported closure."}
    assert compare.compare_reports(old, new)["model_quality_improvement_demonstrated"] is True
    new["rows"][0]["review"]["verdict"] = "unsafe"
    assert compare.compare_reports(old, new)["model_quality_improvement_demonstrated"] is False
    new["identity"]["model"] = "other"
    with pytest.raises(ValueError):
        compare.compare_reports(old, new)


@pytest.fixture
def recovery_junit(tmp_path):
    import json
    import xml.etree.ElementTree as ET

    suite = ET.Element("testsuite", tests="1", failures="0", errors="0")
    case = ET.SubElement(suite, "testcase", name="test_recovery")
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name="maintenance_assessment", value=json.dumps({
        "scenario": "malformed", "mode": "scripted", "calls": 2, "samples": 1,
        "attempt_outcomes": {"failed": 1, "committed": 1}, "recovered_failure": True,
        "justified_no_change": False, "evidence": {"processed": 1, "unresolved": 0},
        "useful_notes": 1, "latency_seconds": 0.2,
        "usage_kind": "fabricated", "usage": {"prompt_tokens": 84},
        "attempts": [{"outcome": "failed"}, {"outcome": "committed"}],
    }))
    path = tmp_path / "results.xml"
    ET.ElementTree(suite).write(path)
    return path


def test_recovery_summary_keeps_failed_attempts_and_refuses_failed_suites(recovery_junit):
    import xml.etree.ElementTree as ET

    result = compare.recovery_summary(recovery_junit, expected_scenarios={"malformed"})
    assert result["failed_attempts"] == 1 and result["recovered_scenarios"] == 1
    assert result["calls"] == 2 and result["unresolved_evidence"] == 0
    assert result["committed_attempts"] == 1
    assert result["attempt_outcomes"] == {"failed": 1, "committed": 1}
    suite = ET.parse(recovery_junit).getroot()
    case = suite.find("testcase")
    ET.SubElement(case, "failure", message="synthetic failure")
    ET.ElementTree(suite).write(recovery_junit)
    with pytest.raises(ValueError):
        compare.recovery_summary(recovery_junit, expected_scenarios={"malformed"})


@pytest.mark.parametrize("outcomes", [
    {"failed": 2},
    {"committed": 2},
    {"failed": 2, "committed": 1},
    {"failed": 1},
    {"failed": 1, "committed": 1, "configuration_error": 1},
])
def test_recovery_summary_refuses_mismatched_outcome_counts(recovery_junit, outcomes):
    import json
    import xml.etree.ElementTree as ET

    suite = ET.parse(recovery_junit).getroot()
    prop = suite.find(".//property")
    row = json.loads(prop.attrib["value"])
    row["attempt_outcomes"] = outcomes
    prop.set("value", json.dumps(row))
    ET.ElementTree(suite).write(recovery_junit)
    with pytest.raises(ValueError, match="Invalid synthetic attempt accounting"):
        compare.recovery_summary(recovery_junit, expected_scenarios={"malformed"})


@pytest.mark.parametrize("mutation", ["extra_dispatch", "invalid_review", "false_quality"])
def test_mislabelled_first_attempt_or_quality_is_rejected(mutation):
    old = report([("closure", "validation_rejection", False)])
    new = report([("closure", "accepted_change", True)])
    if mutation == "extra_dispatch":
        new["rows"][0]["requests"] = 2
    else:
        for item in (old, new):
            item.update(mode="live", identity={"endpoint": "https://synthetic.invalid/v1",
                        "model": "small", "transport": "chat", "mode": "separate"})
        new["rows"][0]["review"] = {"verdict": "useful_faithful", "rationale": "Reviewed."}
        if mutation == "invalid_review":
            new["rows"][0]["review"] = {"verdict": "invented", "rationale": ""}
        else:
            new["rows"][0].update(outcome="empty", expectation_match=False, committed=False)
    with pytest.raises(ValueError):
        compare.compare_reports(old, new)


@pytest.mark.parametrize("also_resolves_useful_task", [False, True])
def test_accepting_incorrect_changes_cannot_earn_an_improvement_claim(also_resolves_useful_task):
    old = report([("closure", "validation_rejection", False)])
    new = report([("closure", "accepted_change", False)])
    if also_resolves_useful_task:
        old = report([("closure", "validation_rejection", False),
                      ("update", "validation_rejection", False)])
        new = report([("closure", "accepted_change", False),
                      ("update", "accepted_change", True)])
    result = compare.compare_reports(old, new)
    assert result["candidate"]["failed_attempts"] == 0
    assert result["candidate"]["unresolved_expectations"] == 1
    assert result["application_improvement_demonstrated"] is False
