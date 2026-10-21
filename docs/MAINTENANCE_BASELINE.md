# First-attempt Current Life baseline

[Back to memory reference](MEMORY_REFERENCE.md) · [Issue #2](https://github.com/Hirokizante/eidolon/issues/2)

[`scripts/maintenance_baseline.py`](../scripts/maintenance_baseline.py) measures one
Current Life maintenance attempt per independent synthetic case. It uses the
production auxiliary route resolver, `ContinuityWorker.run_once()`, local
validator, and real temporary SQLite storage. It never starts the application
service, recovery scheduler, Hindsight, dialogue generation, or memory delivery.
This evaluation is opt-in; ordinary tests use scripts or mocked HTTP exclusively.

## Offline execution

Run from the repository root with dependencies from `uv.lock`:

```sh
export EIDOLON_HOME="$(mktemp -d /tmp/eidolon-maintenance.XXXXXX)"
uv run python scripts/maintenance_baseline.py run
```

The explicit home must be empty or marked for this exact synthetic workload,
outside every Git checkout/worktree and outside the live Eidolon state tree.
The evaluator reuses the existing companion baseline's isolation guard. Keep
this directory separate from the companion recall baseline because their
workload markers differ. Every invocation creates a new `runs/maintenance-*`
directory. Each sample gets a fresh database; failed work stays unresolved in
that database and is never retried. Source exchanges are memory-eligible, but no
outbox dispatcher is instantiated or contacted.

The command prints aggregate counts and the private report location. `report.json`
contains case IDs, rubrics, expectation checks, before/after notes, synthetic
request messages and responses, attempt records, HTTP status when available,
request counts, latency, available numeric token usage, and route provenance.
`sources.json` and `workload.json` preserve the exact evaluation inputs. Reports
are checkpointed after each sample and marked complete only after all planned
samples finish. A partial report is not a completed baseline.

Scripted responses are deterministic fixture answers. Their acceptance rate
tests the harness and application contract, not model comprehension, cost, or
reliability. Their token usage and monetary cost remain unknown.

## Workload and reproducibility

The [versioned workload](../tests/fixtures/maintenance_baseline_v1.json) pins the
SHA-256 of the [companion source fixture](../tests/fixtures/memory_baseline_companion.json)
and reuses its dated, entirely invented exchanges. It adds synthetic maintenance
cases for admissions, updates, explicit and paraphrased closures, justified
no-change, authored and locked notes, full capacity, atomic replacement, lowered
capacity, legacy conversion, optional date/promise metadata, uncertainty,
negation, attribution, fiction, and paraphrased developments. Each case includes
a written rubric and desired state checks independent of the scripted answer.
Fixture-local references bind to real source and note identities at runtime.
For ordinary maintenance, the scripted adapter translates those historical
fixture identities into the current request-local proposal references. It keeps
the fixture's substantive judgments and expectations unchanged. Live responses
receive no such translation. Capacity reviews and legacy conversions retain
their existing contract, and reports record each actual task's prompt/schema hashes.

The report records workload hashes, the tested Git revision, dirty status,
hashes of relevant application/evaluator files and `uv.lock`, exact model and
endpoint, auxiliary mode (`inherit` or `separate`), safe effective request
options, prompt/schema hashes, and transport/output mode. The current production
contract is prompt-only JSON over OpenAI-compatible Chat Completions; the report
does not claim JSON schema enforcement. Each sample also records the actual
request endpoint when dispatched. Headers and resolved credentials are excluded.

The [pre-change scripted reference](../tests/fixtures/maintenance_baseline_prechange_v1.json)
records application revision `6fac2fadf1097f4b32405da379cfdcdf75aefd35`, relevant
source hashes, all 17 observed case outcomes, and expectation matches. It has
10 accepted changes, 6 accepted no-changes, and 1 validation rejection. The
rejected paraphrased closure deliberately retains its desired closure expectation.
There was no live-model trial, and no safety/usefulness rate is inferred from
this reference. It excludes machine-specific latency and private paths.

The original baseline issue added no production maintenance changes or frozen prompts.
To reproduce a historical trial, use a separate checkout of the report's clean
application revision, sync its lockfile, and use a new synthetic home. Preserve
the original report and inputs. A dirty run additionally requires the exact
recorded source files; a Git revision alone is insufficient. Use the commit
introducing this evaluator to reproduce the pre-change production behavior;
its production source hashes match the reference above. Future improvements
should run the same versioned cases and compare per-case outcomes and reviewed
verdicts. Change workload/expectation versions deliberately and report differences;
do not weaken desired outcomes to conceal failures or pin tests to old prompts.

## Explicit real-model trials

Live trials send only the synthetic case inputs but incur requests and possibly
charges on the selected endpoint. They require both `--mode live` and
`--allow-real-model`, plus an explicit private provider configuration outside Git.
The evaluator reads only `provider`, `providers`, and `auxiliary` from that file.
It does not load personal prompts, identity, histories, memory settings, or default
app configuration. Both separate and inherited routing use the current production
resolver, including its existing limitations and inherited request settings.
Use a dedicated minimal profile to make those settings reproducible. For example:

```yaml
provider: synthetic-trial
providers:
  - name: synthetic-trial
    endpoint: https://provider.example/v1
    model: exact-model-id
    api_key_ref: env:EIDOLON_TRIAL_API_KEY
auxiliary:
  mode: separate
  provider: synthetic-trial
  model: exact-model-id
  context_window: 32768
  max_output_tokens: 4096
  output_token_field: max_tokens
  timeout_seconds: 60
```

Replace the example endpoint/model with the intended trial route and supply its
credential through the named environment variable or a `keyring:` reference.
Do not embed credentials in URLs. Endpoint queries/fragments are refused. Keep
profiles and outputs private even when all evidence is synthetic.

```sh
uv run python scripts/maintenance_baseline.py run \
  --mode live --allow-real-model \
  --provider-config /private/path/to/trial.yaml \
  --repetitions 3
```

This runs 51 independent first attempts in fixed case order. It may dispatch fewer
requests if input limits or configuration prevent dispatch. Recovery is disabled:
there is one worker pass, and the HTTP transport forbids a second dispatch for
the sample. Corrections, model/endpoint switching, split retries, and provider
fallbacks cannot improve its first-attempt score. Provider-rejected requests count
as dispatched; work rejected before dispatch counts as zero requests. Failures
and accepted no-change remain visible separately from expectation matches.

Optional `--input-usd-per-million N --output-usd-per-million N` record explicit
user-supplied prices and estimate USD from reported input/output tokens. Both
prices are required together and must be finite and nonnegative. Estimates exclude
cache discounts, fees, reasoning-specific prices, and provider billing rules;
actual cost remains unknown. Without both token counts, a sample estimate stays
unknown. Summary usage includes known sample counts and partial sums, and only
supplies a total when every sample reports that counter. Never substitute zero
for missing usage, estimate tokens from text length, or report partial costs as
the full run's cost. HTTP status and output exhaustion are retained where available;
arbitrary provider error bodies are never copied into diagnostic fields.

## Review and aggregate reporting

Automatic expectation checks inspect changed-note count, desired fields, evidence
roles, selected prose atoms, and protected-note preservation. They are deliberately
limited: substring matches cannot establish fidelity, semantic correctness, or
absence of invented facts. An accepted no-change that omits a warranted admission
does not match that case's expectation. A higher no-change rate alone is not an
improvement. Raw outcomes distinguish accepted changes/no-change, malformed or
empty output, truncation, provider rejection/failure, validation rejection, and
unresolved work. Rejected unsafe proposals and unsafe persisted changes must both
be identified during review, with the persistence distinction in the rationale.

Review each live sample against its rubric, complete source exchanges in
`sources.json`, request state, proposal, and resulting notes. Assign one verdict:

- `useful_faithful`: all warranted useful changes (or justified no-change) were
  committed, faithfully preserving uncertainty, speaker ownership, scope, source
  chronology, metadata, protections, and allocation decisions.
- `unsafe`: a proposal or committed result invents or misattributes facts, violates
  protections, prematurely closes a topic, or otherwise violates the safety rubric.
  State whether the application rejected it or persisted it.
- `unresolved`: incomplete/failed work, an omitted warranted change, unhelpful
  no-change, or insufficient evidence to judge usefulness and fidelity.

Store written decisions outside Git. Bind them to the exact report bytes with
`shasum -a 256 /private/path/to/report.json`:

```json
{
  "report_sha256": "<SHA-256 of the original report bytes>",
  "samples": {
    "admission:1": {
      "verdict": "useful_faithful",
      "rationale": "The interview and requested encouragement were admitted faithfully."
    }
  }
}
```

```sh
uv run python scripts/maintenance_baseline.py review \
  /private/path/to/report.json /private/path/to/reviews.json
```

This writes a separate `reviewed-report.json` and prints aggregate counts. Partial
human reviews show reviewed/unreviewed sample counts, but usefulness and unsafe
rates remain unknown until every sample is reviewed. Failed attempts cannot be
reviewed as useful faithful, incomplete runs cannot be promoted, and scripted
checks cannot be promoted into live-model quality results. The original report
stays intact. Review rationales themselves may contain sensitive material.

Before sharing, review the printed aggregate summary under the
[privacy guide](PRIVACY.md#sharing-diagnostics). Share sample sizes, case/workload
version, tested application revision, reviewed outcome counts, reported usage
coverage, latency, and sanitized route/mode identity sufficient to describe the
trial. Keep private URLs, profiles, full reports, requests, responses, database
files, and local paths private. One small fixed-order trial does not establish
long-running reliability, statistical significance, provider feature enforcement,
or live macOS service behavior. Compare matched endpoint/model/settings and
repetitions, include unresolved and unsafe results, and report human review limits.
