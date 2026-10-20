"""Offline replay must preserve evidence without consulting services or app state."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "memory_phase1_replay", Path(__file__).parents[1] / "scripts/memory_phase1_replay.py",
)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def report_fixture() -> dict:
    baseline = "[fact] A violet notebook."
    return {
        "synthetic": True, "provider": {"context_chars": 32000}, "rows": [{
            "id": "probe", "category": "synthetic", "query": "notebook",
            "expected_atoms": [["violet"], ["notebook"]], "recalled_context": baseline,
            "messages": [{"role": "system", "content": "Context\n" + baseline}],
            "raw_recall_responses": [{"results": [{
                "id": "fact", "text": "A violet notebook.", "type": "world",
                "occurred_start": "2026-01-01", "document_id": "source",
                "metadata": {"origin": "user_statement"},
            }]}],
        }],
    }


def test_replay_preserves_atoms_and_provenance_without_mutating_inputs():
    report = report_fixture()
    original = copy.deepcopy(report)
    result = replay.replay_report(report, repetitions=1)
    assert report == original
    assert result["baseline_atom_coverage"]["matched_groups"] == 2
    assert result["phase1_atom_coverage"]["matched_groups"] == 2
    assert result["atom_groups_lost"] == 0 and result["records_omitted"] == 0
    assert result["provenance"]["origin_known"] == 1
    assert result["provenance"]["date_known"] == 1
    assert result["provenance"]["backend_identifiers_in_prompt"] == 0
    assert result["prompts_estimated_over_allowance"] == 0
    assert result["assembly_ms"]["count"] == 1


def test_replay_accepts_token_windows_and_accounts_for_response_reserve():
    report = report_fixture()
    report["provider"] = {"model": "test-model", "context_window": 1000}
    result = replay.replay_report(report, repetitions=1)
    row = result["rows"][0]
    assert row["provider_context_allowance_chars"] is None
    assert row["provider_context_window_tokens"] == 1000
    assert row["phase1_prompt_tokens_estimate"] > 0
    assert row["estimated_prompt_fits"]
    report["provider"]["parameters"] = {"max_tokens": 1000}
    result = replay.replay_report(report, repetitions=1)
    assert not result["rows"][0]["estimated_prompt_fits"]


def test_replay_applies_adapter_record_limit_and_detects_budget_evidence_loss():
    report = report_fixture()
    report["rows"][0]["raw_recall_responses"][0]["results"].append({"text": "Later evidence"})
    result = replay.replay_report(report, max_chars=0, max_memories=1, repetitions=1)
    assert result["rows"][0]["raw_records"] == 2
    assert result["rows"][0]["candidate_records"] == 1
    assert result["atom_groups_lost"] == 2 and result["records_omitted"] == 1
    assert result["memory_chars_phase1"]["max"] == 0


def test_replay_uses_last_capture_and_does_not_score_question_as_retrieved_evidence():
    report = report_fixture()
    row = report["rows"][0]
    row["raw_recall_responses"].insert(0, {"results": [{"text": "Earlier fallback result"}]})
    row["expected_atoms"].append(["unretrieved detail"])
    row["messages"].append({"role": "user", "content": "Ask about unretrieved detail"})
    result = replay.replay_report(report, repetitions=1)
    assert result["phase1_atom_coverage"]["matched_groups"] == 2
    assert result["phase1_atom_coverage"]["groups"] == 3


@pytest.mark.parametrize("invalid", [
    {"synthetic": False}, {"rows": []}, {"provider": {}},
    {"rows": [{"raw_recall_responses": []}]},
])
def test_replay_requires_explicit_synthetic_existing_capture(invalid):
    with pytest.raises(ValueError):
        replay.replay_report(report_fixture() | invalid, repetitions=1)
