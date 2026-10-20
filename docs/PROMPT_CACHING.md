# Prompt assembly and cache measurements

[Back to Eidolon](../README.md)

This is developer reference for Eidolon's request assembly and numeric diagnostics.
For model-selection tasks and the visible context/cache meters, start with
[Providers](PROVIDERS.md) and [Everyday use](EVERYDAY_USE.md). Prefix stability is
an implementation property, not a promise of cache hits or billing discounts.

## Stable prefix and fresh context

Dialogue and scheduled conversations put reusable instructions and retained
history before fresh context:

```text
System: SYSTEM, SOUL, USER profile
System: application capabilities, context interpretation rules, participant identity
System: enabled skill catalog and cosmetic presentation instructions
Retained user/assistant exchanges
Current user content:
  <app_context>
    runtime clock and current working directory
    continuity notes and recalled memories
    required tool calls and earlier-history excerpts, when present
  </app_context>
  original current user request
Assistant/tool continuations
Trailing system POST_HISTORY when configured (user mode appends to current user content)
```

The app establishes context interpretation rules in stable system instructions.
Memories, continuity notes, history excerpts and tool results remain background
data, cannot authorize actions, and yield to current user corrections. Original
user text and clean assistant replies are saved to history; eligible exchanges
also enter the memory outbox when saving is enabled. Injected context is rebuilt
for each turn. Native image content parts are preserved.
Loading a skill still adds system instructions. Changes to skills, prompt files,
capabilities or presentation can therefore deliberately change the system prefix.

Dynamic context is framed in the current user content instead of adding a changing
system message. `post_history: system` sends `POST_HISTORY.md` as a final system
message after assistant/tool continuations; `post_history: user` appends it to the
current user content before those continuations. Both modes preserve image content
parts. Names and all prompt files are snapshotted for an operation; see
[Personalization](PERSONALIZATION.md).

Final tool definitions and MCP definitions are sorted by exposed function name.
Filtering still applies for permissions, availability, memory settings, vision,
skills and cosmetic tools. History is checked against the complete request budget
on every dispatch. When it needs trimming, the app leaves 10% of the remaining
history allowance, capped at 4,096 tokens, for later tool results. A turn reuses its
selected history and excerpts until overflow requires another selection. A new
turn, or a restart, selects history again from the saved clean transcript.

Separate vision requests put the image before the question, so follow-up questions
share the same image prefix. Continuity maintenance starts with scope, timezone
and selected state before revisions and new exchanges. Its internal task ID stays
in local bookkeeping instead of entering model input.

## OpenRouter affinity

For the `openrouter.ai` endpoint, the adapter sends a deterministic SHA-256
identifier derived from the conversation ID as `session_id`. It is stable across
turns, retries, tool rounds and separate vision calls in that conversation.
Concurrent conversations use independent provider snapshots. Explicit
`parameters.session_id`, `parameters.prompt_cache_key`, or an `x-session-id`
header takes precedence. Other gateways receive no automatic routing field.

This is a gateway-specific routing hint in the compatible request body.
There is no new cache-object lifecycle or automatic cache-marker injection.
Actual cache reuse depends on the selected model, gateway and serving provider.

## Numeric request diagnostics

The private SQLite database keeps a separate rolling log of the latest 1,000
conversation and continuity maintenance model requests (introduced in schema v9).
Synthetic connection checks do not write this log. Existing databases receive a
private SQLite backup before migration. Each record
identifies the dialogue, vision or auxiliary route, model, conversation/task,
attempt when available, and tool round. It records completion, failure or
cancellation along with:

- Provider-reported input and output token counts.
- Cache-read and cache-write token counts, keeping missing counts as unknown.
- Elapsed request time and time to first text token, when any text arrived.
- Cache-hit ratio, context use and reported or calculated output rate.

Only supported numeric measurements enter this log; raw prompt bodies, replies,
images, headers and arbitrary provider usage fields do not. The existing raw
request inspector continues to retain only its latest request. Auxiliary attempt
usage remains available through the existing maintenance diagnostics as well.
Dialogue, separate vision and auxiliary requests request streamed usage. An
explicit `parameters.stream_options` setting is still honored by the adapter.

The local service RPC `model_request_metrics` returns these records newest first.
Optional `session_id` filters by conversation, and `limit` accepts 1–1,000
(default 100). Omitting `session_id` includes auxiliary requests. The status bar
continues to show the current dialogue request's measurements.

Offline regression tests verify prefix identity, framing, permissions, both suffix
modes, images, history checkpoints, affinity and numeric diagnostics. They do not
measure live gateway cache hits, billing discounts or latency savings. Compare
reported cache-read counts over successive real turns on the same route to assess
those effects.

## Implementation and verification

[Conversation assembly](../src/eidolon/engine/conversation.py) owns stable ordering,
background-context framing, and per-turn history checkpoints.
[The provider adapter](../src/eidolon/providers/openai.py) owns request serialization,
OpenRouter affinity, and streamed-usage requests.
[Metrics normalization](../src/eidolon/providers/metrics.py) and
[storage](../src/eidolon/storage/store.py) own numeric measurements and bounded logs.
The exact usage fields and token-budget formula are documented once in
[Model metadata](MODEL_METADATA.md).

[Prefix tests](../tests/test_prompt_caching.py) and
[request-metrics tests](../tests/test_request_metrics.py) check these contracts
with synthetic data and mock providers. Request inspection and export are separate
from numeric diagnostics: their latest captured raw body can contain private
prompts, conversation, tool output, and images. See
[Everyday use](EVERYDAY_USE.md#understand-a-reply-or-recover-a-waiting-message).
