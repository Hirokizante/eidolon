# Choose an auxiliary model

[Back to Eidolon](../README.md)

The auxiliary model runs app-owned background inference for current-life notes,
including narrative summaries and conversion of legacy extractive notes. It does
not generate ordinary replies. A separate target can use another model on the
same saved connection or a different connection. Start with [Providers](PROVIDERS.md)
for connection setup; memory activation and evidence policy remain separate.

## Select and save a target

1. Open `/preferences` (Alt+6) and choose **Auxiliary model**.
2. Choose a mode:

   | Label | Configuration | Behavior |
   | --- | --- | --- |
   | Follow dialogue model | `inherit` (default) | Uses the dialogue connection/model, bounded auxiliary output, and explicit reasoning/thinking controls |
   | Choose a separate model | `separate` | Uses a saved connection and an independent exact model ID with its own request options |
   | Off | `disabled` | Stops automatic and manual auxiliary inference; preserves existing notes, pending inputs, and attempt history |

3. Open **Model settings**. For a separate route, choose a connection and model
   or enter an exact ID when discovery is unavailable. The picker shows the
   auxiliary role, with **Reasoning effort**, **Context Size**, and **Max Response
   Length** in that order. In Follow dialogue mode, connection, model, and effective
   reasoning effort are read-only; context and response budgets remain editable.
4. **Apply** in the picker returns settings to the Preferences draft. Review the
   selected target, optionally **Test**, then **Apply** in Preferences (Ctrl+S)
   to save. Esc retains a draft for the terminal session; **Discard edits** reloads
   saved preferences and clears unfinished picker drafts.

**Connect** uses the saved connection editor and Keychain flow without selecting
another dialogue model. Saving a connection is a separate persistence action;
editing a shared endpoint or credential affects every role using that connection.
Selecting a separate auxiliary model does not change the dialogue model.

The optional Test sends one fixed, wholly synthetic creative-project exchange
through production maintenance request assembly, output controls, stream handling,
and the local proposal validator. It reports provider connectivity separately
from maintenance-contract compatibility and shows the effective output mode.
An `OK` reply, empty output, refusal, incomplete stream, or invalid proposal fails
the contract check even when the endpoint responds successfully. Valid no-change
is permitted by the real contract; passing does not establish a useful admission,
note quality, reliability across tasks, schema enforcement, speed, or cost.
Test reads no personal evidence or configured names and writes no conversation,
memory, request metrics, or maintenance task. Run it while foreground generation is idle; otherwise
it asks you to retry. It cancels existing maintenance before testing and can itself
be interrupted for foreground work. Test does not save the draft.

## Set request budgets and compatible options

The default auxiliary response limit is 4,096 tokens; the allowed range is
128–8,192. Context Size is an optional positive token capacity. Changing the
selected mode/model/connection clears the previous manual capacity; enter a new
override for the new target when needed. Separate routes never copy the dialogue
model's manual capacity. Unknown serving capacity remains unknown.

**Advanced request options** contains:

| Field | Default and limits |
| --- | --- |
| Input allowance (characters) | Blank/null/omitted means no character cap; an explicit limit is 8,000–1,000,000 characters |
| Timeout | 60 seconds; range 1–120 |
| Thinking mode | Blank omits the role override; optional `disabled` or `adaptive` sends `thinking: {type: ...}` |
| Output-token field | `max_tokens` by default; alternatively `max_completion_tokens` |

The character allowance is an additional work limit, not a token-capacity
measurement. Known token capacity, output reserve, and safety reserve still apply.
Maintenance counts its effective task instructions, response schema when used,
complete topic/protection index, selected note details and complete exchanges.
The schema also counts toward an explicit character allowance. Nothing is clipped
to manufacture a fit. An unknown context window remains unknown; removing a
character cap does not establish the provider's actual capacity.

The response cap also bounds work: smaller allowances select fewer whole
exchanges, fewer operations and shorter prose. The request carries per-field and
total prose limits, which are checked locally. Planning leaves 10% (at least 32
tokens) for response estimation and reserves JSON/reference bookkeeping before
allocating prose. Ordinary, capacity and legacy tasks use their own overheads and
operation ceilings. For example, 512 output tokens permits one ordinary operation
and one complete exchange per new batch; the default 4096 allows up to eight.
One exchange can still require more work than estimated; it remains pending if
the model cannot express the useful changes within the plan.

Reasoning/thinking can consume the output allowance. If output is incomplete,
reduce supported reasoning effort or increase Max Response Length within the bounds.
An explicit positive thinking `budget_tokens` is subtracted when planning answer
space, while the full output allowance remains reserved in the context window.
Adaptive or otherwise unknown reasoning consumption cannot be predicted. The app
never increases the configured token/time cap or disables thinking to make work fit.
Very small output settings can be valid configuration values but unusable for a
maintenance task's minimum planned work. Use **Test auxiliary** to check the draft
contract before saving; it reports a preflight limit without making a provider call.
Only configure thinking controls on endpoints that accept them; a reasoning effort
such as `none` does not itself guarantee that a gateway disables thinking.

A separate request uses the maintenance output contract with no tools. It takes credentials, headers,
and transport from the saved connection, plus only its own generation options
and the connection's explicit `stream_options` override. It does not copy dialogue
temperature, stops, personality, or presentation settings. Null reasoning effort
omits that parameter. Exactly one output-token field is sent.

Follow dialogue mode caps output at the auxiliary limit and preserves only the
output-token field, reasoning effort, thinking controls, and explicit stream-usage
options. Dialogue stops, temperature, top-p, and unrelated vendor parameters are
excluded. It does not force temperature to zero or disable reasoning. Explicit
auxiliary reasoning/thinking settings override the inherited controls independently.
The picker shows the effective read-only reasoning effort. Reserved protocol overrides, multiple
output-limit fields, or inherited `max_output_tokens` cause a configuration error.
The app does not silently remove rejected semantic options or switch models.

Both modes request streamed usage by default. A saved connection override such
as `parameters: {stream_options: {include_usage: false}}` is honored. Missing
usage remains unknown, including after interruption. See
[Prompt assembly and diagnostics](PROMPT_CACHING.md).

## Output constraints and route-specific guarantees

Maintenance output is independent of dialogue/avatar presentation and its
`structured_verified` flag. Selection prefers a verified enforced schema, then
supported JSON-object mode, then the compact prompt-only JSON contract. The local
validator and atomic evidence checks run in every mode. JSON mode constrains JSON
syntax; it does not enforce the maintenance schema or semantic correctness.

Unknown routes remain prompt-only. Model names, inventories, successful HTTP
responses, and a passing Test never establish enforcement. There are no automatic
paid capability probes. An operator can record **independently verified** support
in private configuration on the saved connection, for example:

```yaml
maintenance_output_support:
  - endpoint: https://api.example.com/v1
    model: example-summary-model
    transport: chat_completions_stream
    reasoning_effort: null
    thinking_mode: null
    json_schema: enforced  # unknown | enforced | unsupported | ignored
    json_object: true     # true | false | null (unknown)
```

This field belongs under the relevant `providers` entry, alongside `parameters`.
It is an operator assertion, reported as `configured_verification`; the app does
not claim to have measured it. Keep the default empty list until documentation and
independent route verification justify stronger controls. Endpoint, exact model ID,
streaming Chat Completions transport, and effective reasoning/thinking mode must
match. A changed endpoint/model/mode or conflicting records use prompt-only output.
Set `json_schema: unsupported` or `ignored` to retain known negative evidence across
service restarts. The optional Test never edits these records or saves its draft.

The schema uses a conservative provider subset: closed objects, required nullable
optional fields, enums and nested `anyOf` variants. Length, count, reference,
evidence, protection and transition rules remain local. Null optional fields mean
omission; missing required substantive fields are never repaired. Token planning
includes the effective response-format schema overhead.

Anthropic's native OpenAI compatibility endpoint uses prompt-only output because
it documents ignored `response_format` controls. OpenRouter constrained requests
require upstream parameter support via `provider.require_parameters: true`.
Other gateways require their own exact-route verification; a model's native API
support does not imply gateway support. No endpoint, model, beta transport, or user
resource limit is changed to obtain constrained output. See the primary references:
[OpenAI's supported schema subset](https://developers.openai.com/api/docs/guides/structured-outputs),
[DeepSeek JSON output](https://api-docs.deepseek.com/guides/json_mode/),
[Anthropic compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk),
and [OpenRouter structured output](https://openrouter.ai/docs/guides/features/structured-outputs)
(checked 2026-10-21).

An explicit machine-readable unsupported-format error is remembered across
auxiliary adapter replacements and Tests during the current service lifetime
(bounded to 128 route configurations). It permits a lower supported output mode
on a later attempt, with `provider_rejected` provenance, and never triggers an
immediate hidden retry. Generic rejected options, schema errors, refusals, and
successful responses do not alter capability evidence. Semantic options such as
reasoning are never silently removed to rescue a rejected request. Restart clears
this temporary negative cache; update the private support record for durable
unsupported/ignored settings.

Service status and new attempt details report `output_mode` (`json_schema`,
`json_object`, or `prompt`), contract version, and capability source. Private
attempt provenance also hashes the effective provider response format separately
from the full local schema. Earlier attempts without these fields stay unknown.

This private configuration fragment assumes a saved connection named `local`:

```yaml
auxiliary:
  mode: separate
  provider: local
  model: example-summary-model
  context_chars: null
  context_window: null
  timeout_seconds: 60
  max_output_tokens: 4096
  output_token_field: max_tokens
  reasoning_effort: null
  thinking_mode: null
```

Choosing a target does not enable recall, saving, automatic current-life updates,
or inclusion of notes in replies. Off does not prevent already valid notes from
being used when the independent memory/current-life controls permit them.
Scheduled conversations retain their job-specific override or dialogue target.
See [current-life notes](CURRENT_LIFE_SUMMARIES.md) for update/inclusion controls
and [optional memory](MEMORY.md) for external retention readiness.
Hindsight's server inference and credentials are independent. Extractive history
digests are deterministic; auxiliary inference is not added to the foreground
reply path. For the separate image analyst, see [Providers](PROVIDERS.md#use-images-with-a-model).

## Diagnose and recover

`/memory` → **Current life** shows refresh status and actionable notices. Choose
**Run history**, then Enter on a run, to review its outcome and saved note changes.
`/runs` opens Run history directly, and Ctrl+P lists the same command. A quiet
**Life: recovering**, **Life: needs retry**, or **Life: check settings** indicator
near the composer appears while maintenance has unresolved trouble. It remains
visible even when optional service-status components are hidden. Automatic work
never adds retry messages to chat or moves focus.

Run details show each retained attempt's safe cause, effective model, start/end
time, duration, and numeric usage when the provider supplies it. They also show
aggregate outcomes, recorded usage across attempts, the next action or earliest
automatic retry time, last successful maintenance, and existing before/after note
comparisons. Unknown usage stays unavailable; recorded totals exclude unknown
usage and should not be treated as complete billing totals. Foreground work can
delay a scheduled retry beyond the displayed time. With automatic updates off,
use **Refresh current life** after correcting settings.

A successful retry preserves earlier failed attempts. Warnings represent all
unresolved conditions, so a newer success clears only the condition it resolves.
Runs with unresolved conditions appear first in Run history, even when their
continuity update finished. An unrelated older failure remains visible. Connection/configuration warnings
are distinct from Hindsight readiness. This diagnostics slice preserves the
existing retry policy: it does not introduce recovery budgets, set-aside batches,
or progress past the oldest unresolved evidence.

`uv run eidolon doctor` reports auxiliary routing separately from Hindsight
connectivity. With the service running, it also includes maintenance diagnostics:
task state, latest attempt, last successful execution, elapsed time, and available
numeric usage. With the service stopped, it reports configured routing and the
stopped state without that running maintenance view.

Failures keep existing notes and processing watermarks intact and use bounded
retry backoff. If an immutable pending batch cannot fit a smaller model, choose
a larger-capacity target or increase an overly restrictive input allowance, then
refresh again. The app does not truncate that batch or skip to newer evidence.
Preflight notices distinguish instructions/schema/output-reserve overhead,
required topic index or target details, an oversized complete source, and an
insufficient response allowance. These appear as settings attention in status
and Run history, including runs where no request was dispatched. Such runs have
no paid attempt and do not increment model-call counts. A single oversized
exchange remains retained and unresolved. Lowering the response cap also holds
an existing larger immutable batch instead of sending it with a smaller work plan.
A missing externally edited auxiliary connection is isolated from dialogue startup;
repair it in Preferences or deliberately choose Follow dialogue model.

To suspend inference, choose Off; to return to dialogue targeting, choose Follow
dialogue model. Keep the updated app/schema. An older build may reject the config
or database; do not replace newer conversations with an older database backup to
change a model target. A synthetic connection test does not establish summary
quality, speed, cost, or foreground responsiveness. Review generated notes and
compare candidates with synthetic evidence before relying on them.

## Developer reference: routing and provenance

Ordinary evidence maintenance uses the versioned `current-life-ordinary-v1`
proposal contract in [proposals.py](../src/eidolon/memory/proposals.py).
The model chooses substantive changes in one request: topic, importance,
category, prose, supporting sources, and closure/replacement decisions.
It returns `{"operations":[...]}` with 1–24 explicit operations, further bounded
by the request's `response_limits`. A new note is:

```json
{"operations":[{"op":"upsert","topic":"Interview preparation","importance":"moderate","category":"thread","content":"The user's interview is approaching; practice remains undecided.","sources":["s1"]}]}
```

Updates add `note_ref`, keeping the existing topic. Closures use `op: archive`,
`note_ref`, `archive_reason`, `decision_reason`, supported prose in `content`,
and new evidence in `sources`. A replacement pairs an archive with reason
`replaced` and empty sources (no prose) with a new upsert's `replaces_ref` and
tradeoff reason. Legitimate no-change is exactly
`{"operations":[{"op":"no_change"}]}`. It means no useful supported change was
warranted, rather than a fallback for an invalid operation.

Every request includes `topic_index`, with stable local note references, topics,
importance, protection and detail availability for every active note. Selected
`items` contain full note details; their saved background appears as complete
`supporting_exchanges`, separate from the new `exchanges`. Index-only notes are
visible for duplicate/protection decisions but cannot be changed. Legacy targets
must fit with their required details before dispatch. Capacity-only review needs
the saved note details for policy decisions and preserves its evidence locally.

When useful work needs an index-only note's details, return sole
`{"operations":[{"op":"defer","reason":"context_required","note_ref":"n1"}]}`.
When the useful changes cannot fit the response plan, return sole
`{"operations":[{"op":"defer","reason":"response_budget"}]}`. Deferral retains
the evidence and reports the limiting condition; it is not a successful no-change
and does not complete a partial set of operations. Increase the relevant allowance
and refresh to reconsider the retained work.

Note references (`n1`, etc.) and source references (`s1`, etc.) are bounded and
local to the displayed request; reusing them from another request is unsupported.
The compiler captures independent copies of the displayed reference tables,
source exchanges, and note snapshot before awaiting the model. It supplies item
IDs, expected revisions, scope, narrative format, derived origin, original source
identity, complete excerpts, and source-based timestamps. Attribution remains
attached to the original evidence; current display names never relabel it.
Unknown references, incomplete operations, protected-note changes, unsupported
transitions, invalid replacements, and stale transactional snapshots reject the
entire proposal. Invalid operations are never dropped to produce a success.

The app offers verified date-literal and promise-kind candidates from complete
originals. Candidates establish eligibility, not relevance: the model chooses
`event_at`/`follow_up_at` or a cited `promise_source` when it belongs to that note.
The compiler supplies the promise speaker and kind; it does not schedule actions.
When metadata is unavailable or uncertain, supported details remain in prose.
Emotion expiry remains application policy. Only whitespace and one complete
outer JSON Markdown fence are tolerated locally. Duplicate keys, commentary,
partial JSON, and substantive repairs are rejected; streams must finish cleanly.

Capacity-only reviews use `current-life-capacity-v1`, with at most 24 operations:

```json
{"operations":[{"op":"archive","note_ref":"n1","decision_reason":"This topic has less continuing relevance than the others."}]}
```

The compiler supplies `archive_reason: capacity` and preserves the saved note's
prose, evidence, attribution, category, scope and metadata. A pass can only reduce
the existing excess, across bounded passes if needed. Protected notes still count
toward the cap. Admissions, classification, rewrites, replacements and claimed
topic completion are rejected in this task.

Legacy classification/conversion uses `current-life-legacy-v1`. Its `target_refs`
names at most four eligible notes whose complete original exchanges fit the batch.
Other displayed notes are read-only context. Conversion looks like:

```json
{"operations":[{"op":"upsert","note_ref":"n1","topic":"Interview preparation","importance":"moderate","content":"The user's interview is approaching; practice remains undecided."}]}
```

For a narrative note awaiting topic/importance classification, omit `content`;
the compiler retains its exact existing wording. For an extractive note, narrative
`content` is required. The app supplies **all** saved sources, category, scope,
revision, narrative format and derived origin. Saved date, expiry and promise
metadata remains unchanged, including emotion expiry. New notes, unnamed or
protected targets, dropped sources, metadata changes and terminal transitions
are rejected. Both specialized tasks permit sole `no_change`, use the same frozen
snapshot compiler and atomic commit checks, and have concise task-specific examples
and safe correction feedback.

Ordinary requests omit specialized task switches and classification instructions,
and require new batch evidence for substantive changes. Capacity review precedes
legacy conversion when no new evidence is pending. Legacy conversion is not a
required extra stage of every ordinary success. Completed specialized work is not
redispatched automatically, including after restart. An explicit refresh may
reconsider a completed no-change pass; unfinished retries retain their task identity.
Attempt provenance hashes the effective instructions and versioned schema for
the actual task, plus its effective output controls as described above. The
existing streaming Chat Completions transport is retained for every mode.

[Routing](../src/eidolon/providers/routing.py) resolves immutable execution snapshots.
Changes to the configured route advance a persisted monotonic revision, close old
work, and preserve original task/evidence identity. A→B→A still rejects results from
the first A execution. Configuration and evidence revisions are checked before
transactional commit. Metadata refresh affects later request assembly without
invalidating a valid in-flight result solely because metadata changed.

Only one auxiliary request runs at a time, and foreground conversations retain
priority even across different endpoints. Closing a stream does not prove the
server has released compute resources.

Attempt details are bounded to the latest 100 per task/stage. Schema v10 adds a
versioned application-owned reason (category and safe explanation) to attempts
and unresolved stages. Older history keeps missing reasons unavailable; migration
does not infer a cause from its last error string. Lifetime outcome counts are
seeded from retained old attempts, so already-pruned historical outcomes remain
unknown. Total call counts and available usage remain intact. Migration takes a
private SQLite snapshot as described in [Recovery](RECOVERY.md#automatic-sqlite-snapshots).

The service `status` and `memory_state` contracts expose aggregate `unresolved`
counts independent of the bounded recent-stage list, plus `last_successful_at`.
`memory_continuity_run` returns attempts, outcome counts, next action/time,
unresolved conditions, and note comparisons; `maintenance_attempts` returns the
same safe attempt representation. Unknown reasons and usage are null. Attempt
reasons survive restarts. Aggregate outcome and usage counts survive pruning;
individual attempt details do not.
The `set_aside` count is zero under the current retry policy.

Raw prompts, proposals, responses, arbitrary exception text, credentials, URLs,
and full provenance are excluded from failure details. Reasons come from a
versioned allowlist, including typed authentication, rate/credit, configuration,
output-limit, timeout, and validation categories. Models are shown as bounded
identifiers; malformed or URL-shaped labels are unavailable. Private internal
storage still retains route/execution hashes and safe request options.
Aggregate call/usage/outcome counts and committed provenance survive pruning.
The separate numeric request log is described in [Prompt caching](PROMPT_CACHING.md).

[Routing tests](../tests/test_model_routing.py),
[service tests](../tests/test_auxiliary_service.py), and
[preference UI tests](../tests/test_ui_auxiliary.py) use synthetic evidence and
mocked transports. They exercise routing, payloads, interruption, retries,
capacity holds, and stale-result rejection; they cannot establish live model
quality or actual gateway cache/billing behavior.
