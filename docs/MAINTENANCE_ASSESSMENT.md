# Current Life reliability assessment

[Parent specification #1](https://github.com/Hirokizante/eidolon/issues/1) ·
[Assessment issue #14](https://github.com/Hirokizante/eidolon/issues/14) ·
[Machine-readable reviewed aggregate](validation/maintenance-reliability-v1.json)

The 2026-10-25 offline assessment demonstrates a narrower application improvement:
one previously rejected useful paraphrased closure now commits, with every other
expected outcome preserved. It also exercises bounded unattended recovery through
the existing service and real temporary SQLite storage. No paid endpoint/model
trial was enabled. These results do not establish model comprehension, endpoint
compatibility, production reliability, or statistical significance.

## Paired first attempts

Both sides use `eidolon-maintenance-first-attempt-v1`, with combined workload hash
`6bb0d0abcdfc8da426867651a38d590b372a54887a49433b0382aa0f41a8751a`.
There is one sample per case and one request per sample; recovery is disabled.
The [frozen baseline](../tests/fixtures/maintenance_baseline_prechange_v1.json)
pins production revision `6fac2fadf1097f4b32405da379cfdcdf75aefd35` and source
hashes. The candidate aggregate records the base revision, dirty status and exact
source hashes; reproduce from this contribution's final files rather than the base
revision alone. The fixture's original rubric was restored after a later
unversioned prose edit changed its hash; sources, proposals and expectations were
not changed. A regression test now binds it to the frozen reference.

| Measurement | Baseline | Revised |
| --- | ---: | ---: |
| Independent samples / dispatched requests | 17 / 17 | 17 / 17 |
| Expected changes accepted | 10 | 11 |
| Justified no-change accepted | 6 | 6 |
| Failed first attempts | 1 | 0 |
| Unresolved expected outcomes | 1 | 0 |
| Unjustified no-change | 0 | 0 |
| Reported model token usage / monetary cost | Unknown | Unknown |
| Live reviewed useful/faithful and unsafe rates | Unmeasured | Unmeasured |

The changed case is `paraphrased-closure:1`: ordinary-language completion now
archives the existing interview topic. Admissions, date corrections, explicit
closures, protected/locked notes, replacement, capacity review, legacy conversion,
promise ownership, uncertainty, negation, fiction and paraphrased updates retain
their expected results. Review checked the synthetic resulting notes against the
case rubrics; automatic checks alone are not a general semantic fidelity judge.
Archived notes preserve their earlier prose, while state and archive reason record
completion; an archived note is not an active upcoming event.

The comparison requires fewer failed first attempts, preservation of each
previously satisfied expectation, no decrease in useful expected changes, and no
increase in unjustified no-change. Each failed-to-accepted transition must satisfy
that task's expectation; accepting an incorrect change cannot earn improvement,
even when another task improves. It refuses partial runs, changed workload
hashes, duplicate/missing samples, recovery-enabled reports and multiple dispatched
requests per first attempt. It sets model-quality improvement to unknown for
scripted or incompletely reviewed runs. Fully reviewed live comparisons additionally
require preserved useful/faithful results and no increase in unsafe outcomes.
No numeric reliability threshold has been introduced.

Candidate latency is recorded in the JSON aggregate as local harness time.
Historical baseline latency was not retained, so no speedup is claimed. The
scripted provider supplies no token counters; missing usage and money stay unknown.

## Unattended recovery and retained evidence

Ten additional independent synthetic scenarios exercise the real service boundary.
They dispatch 29 scripted calls: 9 committed attempts and **20 unsuccessful
attempts**, comprising 19 failed validations/provider attempts and one configuration
error. Six scenarios recover after failure. Three deliberately retain one
unresolved evidence gap each. Successful newer work does not erase those gaps.

| Scenario | Calls | Unsuccessful attempts | Result |
| --- | ---: | ---: | --- |
| Malformed operations | 2 | 1 | Useful note committed after correction |
| Empty response | 2 | 1 | Useful note committed after correction |
| Truncated response | 2 | 1 | Useful note committed after correction |
| Temporary provider trouble | 2 | 1 | Useful note committed after delayed recovery |
| Restart after failure | 2 | 1 | Same allowance/history retained; useful note committed |
| Split batch | 4 | 2 | Two complete-exchange children committed atomically |
| Configuration failure | 1 | 1 | Paused; one unresolved gap; no repeated automatic dispatch |
| Exhausted older batch, then newer evidence | 7 | 6 | New useful note committed; older gap remains visible |
| Chronology-unsafe old preparation | 6 | 6 | Newer note preserved; old evidence retained |
| Small talk | 1 | 0 | Justified no-change |

The JSON retains every attempt's outcome in recorded execution order, safe reason
code, local elapsed time, and available counters. The provider fabricates 42 input
and 7 output tokens per call solely to check durable usage accounting. These are
**not measured model usage or prices**. Scenario wall time excludes waiting through real backoff:
controlled time advances eligibility, while stored attempt duration uses the real
storage clock. The recovery suite does not estimate production latency.

The broader focused tests cover initial six-call exhaustion, interruption and
stale dispatched work, split lineage and duplicate execution, child commit/restart
boundaries, atomic replacement, failed child retention, later-batch progress,
shared daily allowance, UTC boundaries, fairness behind new work, explicit Retry,
configuration-change pacing and pauses, migration, and original-source chronology.
Those tests establish invariants beyond the ten measured scenarios; their test
counts are not additional independent model-quality samples.

## Reproduce offline

Use the locked environment and keep all runtime artifacts outside every checkout:

```sh
export EIDOLON_HOME="$(mktemp -d /tmp/eidolon-maintenance-assessment.XXXXXX)"
uv run python scripts/maintenance_baseline.py run
uv run pytest -n 4 --dist worksteal \
  tests/test_maintenance_assessment.py \
  -o junit_family=xunit1 --junitxml=/tmp/eidolon-maintenance-recovery.xml
uv run python scripts/maintenance_compare.py \
  tests/fixtures/maintenance_baseline_prechange_v1.json \
  "$EIDOLON_HOME/runs/<printed-run-id>/report.json" \
  --recovery-junit /tmp/eidolon-maintenance-recovery.xml
```

Replace `<printed-run-id>` with the run directory printed by the evaluator. The
comparison prints only aggregates; recovery JUnit properties contain synthetic
counts and safe categories. It refuses failing JUnit reports and incomplete or
duplicate recovery scenario sets, and per-scenario outcome counts that disagree
with the recorded attempts. Raw first-attempt reports remain private even
when their workload is synthetic. Review generated output under
[the privacy guide](PRIVACY.md#sharing-diagnostics) before sharing it.

For broad boundary verification, run:

```sh
uv run pytest -n 4 --dist worksteal \
  tests/test_maintenance_baseline.py tests/test_maintenance_compare.py \
  tests/test_maintenance_assessment.py tests/test_maintenance_recovery.py \
  tests/test_maintenance_splitting.py tests/test_maintenance_daily.py \
  tests/test_maintenance_chronology.py tests/test_maintenance_budget.py \
  tests/test_maintenance_output.py tests/test_maintenance_semantics.py \
  tests/test_ordinary_proposals.py tests/test_ui_memory.py \
  tests/test_ui_auxiliary.py tests/test_status.py
```

The initial focused run passed 407 tests in 44.50 seconds. After assessment/reporting
regressions were added, the final focused run passed 47 tests in 5.00 seconds.
The final complete suite passed **2,852 tests, zero failures or skips**, in 166.35 seconds,
using `uv run pytest -n 4 --dist worksteal` with a temporary JUnit report.
Ruff, source/evaluator compilation, `uv build`, staged/all-file privacy and
secret checks, manual privacy/history checks, dependency auditing and diff
whitespace checks passed. No type checker is configured in this repository;
compilation is reported separately from behavioral validation.

The review follow-up passed 52 tests across `test_maintenance_compare.py`,
`test_maintenance_assessment.py`, and `test_maintenance_baseline.py` in 2.68 seconds
with four workers and work stealing. This run refreshed the recovery aggregate
after adding per-scenario count validation and restoring recorded execution order.
The broader validation results above are from the original assessment.

## Terminal review

Synthetic Textual pilots cover the persistent main-surface warning, `/runs`
discovery through the command palette, attempt reasons, budgets and retry timing,
selected-batch Retry, and warnings surviving newer successful work. They verify
keyboard access, focus/cursor/selection/draft preservation, scroll preservation,
loading, disconnected/cached reading, safe error states and resize behavior.

Reviewed synthetic screenshots at 80×24, 60×18, 48×12 and below-minimum 40×10 in
color and monochrome/ASCII show the quiet main warning and scrollable Run history.
At 48×12 the detail header and scroll/back instructions remain visible; additional
history is reached by scrolling. At 40×10 the resize notice replaces the layout;
resizing back preserves the draft and detail. Diagnostic canaries are absent from
the captures and tested rendered failure details. Captures remain outside Git.
This is a headless Textual review; no live terminal emulator, launchd, Keychain,
personal profile, Hindsight server or paid model was exercised.

## Exact-model trials and maintained behavior

Follow the [explicit live-trial procedure](MAINTENANCE_BASELINE.md#explicit-real-model-trials)
on both revisions with separate fresh synthetic homes, the same workload,
endpoint, model, transport and auxiliary mode, and the same repetition count.
Use `--mode live --allow-real-model --provider-config /private/path/trial.yaml`
only when explicitly authorized. Review both complete reports with hash-bound
rubrics, then compare their `reviewed-report.json` files. Output contract/options
may intentionally differ across revisions; preserve their provenance and inspect
each attempt's effective mode. A successful HTTP response never establishes schema
enforcement. No exact-model trial was enabled for this assessment, including the
inexpensive models motivating the parent issue; no model-specific claim follows.

The maintained [auxiliary guide](AUXILIARY_MODEL.md) describes task-specific compact
proposals, captured bookkeeping, complete source/context and output planning,
verified schema/JSON/prompt capability limits, auxiliary-only options, both recovery
budgets, set-aside gaps, chronological replay and safe failure categories.
[Memory reference](MEMORY_REFERENCE.md#current-life-scheduling-and-persistence) describes refresh/retry and
user-visible controls. The generic [configuration](../config.example.yaml) exposes
the six-call initial and six-call shared daily defaults; zero daily calls pauses
automatic backlog work. Neither recovery nor this assessment enables memory or
auxiliary inference when disabled. Parent acceptance remains tracked separately;
this ticket does not close or modify the parent specification.
