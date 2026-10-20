# Memory context rendering and provenance

[Back to Eidolon](../README.md)

See [optional memory](MEMORY.md) for setup and
[the technical reference](MEMORY_REFERENCE.md) for delivery and maintenance.
This page describes the current compact renderer, not an earlier rollout's
performance measurements.

## What the response model receives

The renderer packs complete facts and their useful qualifications into a data
envelope. It does not summarize or shorten the original fact text and adds no
model call or tokenizer dependency. A fact that cannot fit is omitted whole with
an omission reason in the private manifest.

- Memory, document, chunk, and supporting-fact IDs, backend fact-type labels,
  repeated unknown fields, and relevance scores stay out of injected bullets.
  Memory tools accept queries rather than these identifiers.
- Dates use `YYYY-MM-DD`; same-day ranges collapse while distinct dates remain
  explicit. Mention dates are labelled `mentioned`, not presented as event dates.
  Malformed dates are not turned into precise occurrences.
- Useful statement attribution such as `user said`, `user confirmed`,
  `assistant said`, or `derived` is retained. Unknown speakers share a default in
  the envelope. Backend types such as world/experience do not establish speaker
  identity or truth. Private speaker identifiers are not displayed as names.
- Meaningful source context is retained. Only recognized generic conversation
  boilerplate is omitted; qualifications and literal memory wording survive.

A synthetic example:

```text
- [2026-10-14; user said] Aster prefers peppermint tea now; jasmine was an earlier preference.
  Context: A stated preference, not a fictional scene.
```

Automatic context has an 8,000-character allowance including its framing. When
current life is included, its envelope and complete notes receive at most 3,000
characters from that allowance; historical recall gets the remainder. The
adapter also limits returned records using `memory.max_memories` (default 12,
range 1–50) and `memory.recall_budget` (`low`, `mid`, or `high`). Character budgets
are separate from the provider's token-based overall request budget.

Recall queries prioritize the current message and at most six recent eligible
messages within an 800-byte UTF-8 limit. The adapter recognizes the server's
specific query-token-limit validation failure and retries once with a smaller
byte allowance. It does not restart a daemon for HTTP 400. Local history keeps
the full messages. A query timestamp steers retrieval; it is not a strict cutoff
that excludes every later source.

Explicit recall tools use the same compact fact renderer. Reflection is generated
synthesis: its prose is labelled derived and its supporting records are packed
inside its allowance. Typed reflection validates requested structured output
locally and refuses remote schema references. Returned support and evidence
actually supplied to the dialogue model are recorded separately. Generated
source IDs alone do not establish verified citations.

## What remains inspectable

Format-v2 manifests retain original IDs, ISO timestamps, source references,
backend types, metadata, exact supplied text, and omission reasons. Controls are
captured per attempt. Tool context is counted as supplied only if a later
provider round received it. `/memory` → **Reply context** presents that supplied
prose; `/inspect` exposes the actual provider request. Full private provenance
is available through the [local service API](MEMORY_REFERENCE.md#private-context-and-evidence).

Current-life summaries are a separate model-written transformation of original
exchanges. Their evidence checks constrain provenance and metadata, not the
semantic truth of a paraphrase. See
[current-life summaries](CURRENT_LIFE_SUMMARIES.md) for source snapshots,
participant names, protected notes, and conversion of older extractive notes.

Changing the renderer does not rewrite older saved requests or manifests. Seeing
a memory in a saved request establishes that it was supplied, not that it caused
a response or was factually correct. Offline replay can check packing and evidence
preservation; it does not measure a current provider's billing, latency, or reply
quality.
