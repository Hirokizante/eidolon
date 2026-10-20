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
Use **Refresh current life** to retry eligible work after correcting settings.
The memory UI does not expose the full attempt diagnostics.

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

[Routing](../src/eidolon/providers/routing.py) resolves immutable execution snapshots.
Changes to the configured route advance a persisted monotonic revision, close old
work, and preserve original task/evidence identity. A→B→A still rejects results from
the first A execution. Configuration and evidence revisions are checked before
transactional commit. Metadata refresh affects later request assembly without
invalidating a valid in-flight result solely because metadata changed.

Only one auxiliary request runs at a time, and foreground conversations retain
priority even across different endpoints. Closing a stream does not prove the
server has released compute resources.

Attempt details are bounded to the latest 100 per task/stage; the internal service
RPC `maintenance_attempts` exposes that history. Diagnostics retain hashes,
provider/model identifiers, safe request options, and numeric counters without
copying transcripts, raw responses, endpoint credentials, headers, or reference
contents. Aggregate call/usage counts and committed provenance survive pruning.
The separate numeric request log is described in [Prompt caching](PROMPT_CACHING.md).

[Routing tests](../tests/test_model_routing.py),
[service tests](../tests/test_auxiliary_service.py), and
[preference UI tests](../tests/test_ui_auxiliary.py) use synthetic evidence and
mocked transports. They exercise routing, payloads, interruption, retries,
capacity holds, and stale-result rejection; they cannot establish live model
quality or actual gateway cache/billing behavior.
