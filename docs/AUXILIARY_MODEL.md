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
   | Follow dialogue model | `inherit` (default) | Uses the dialogue connection/model and compatible generation options, with bounded auxiliary output and conditional low reasoning effort |
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

The optional Test sends a fixed synthetic request asking for `OK`. It checks
connection/request compatibility, not note quality, and writes no conversation,
memory, or maintenance task. Run it while foreground generation is idle; otherwise
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
Reasoning/thinking can consume the output allowance. If output is incomplete,
reduce supported reasoning effort or increase Max Response Length within the bounds.
Only configure thinking controls on endpoints that accept them; a reasoning effort
such as `none` does not itself guarantee that a gateway disables thinking.

A separate request uses plain text with no tools. It takes credentials, headers,
and transport from the saved connection, plus only its own generation options
and the connection's explicit `stream_options` override. It does not copy dialogue
temperature, stops, personality, or presentation settings. Null reasoning effort
omits that parameter. Exactly one output-token field is sent.

Follow dialogue mode preserves compatible dialogue parameters, caps its output
at the auxiliary limit, and uses `low` effort only when the dialogue profile had
reasoning effort configured. A configured auxiliary thinking mode overrides the
inherited thinking option independently. Reserved protocol overrides, multiple
output-limit fields, or inherited `max_output_tokens` cause a configuration error.
The app does not silently remove rejected semantic options or switch models.

Both modes request streamed usage by default. A saved connection override such
as `parameters: {stream_options: {include_usage: false}}` is honored. Missing
usage remains unknown, including after interruption. See
[Prompt assembly and diagnostics](PROMPT_CACHING.md).

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
It returns `{"operations":[...]}` with 1–24 explicit operations. A new note is:

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

Capacity-only reviews and legacy classification/conversion keep their existing
full operation contract and specialized scheduling. Ordinary requests omit their
task switches and classification instructions, and require new batch evidence
for substantive changes. Legacy conversion can follow after ordinary evidence
has been processed; it is not a required extra stage of every ordinary success.
Attempt provenance hashes the effective instructions and versioned schema for
the actual task. Requests still use prompt-only JSON over the existing plain
provider stream; this contract does not claim provider-enforced JSON or schemas.

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
