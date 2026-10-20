# Model metadata and request budgets

[Back to Eidolon](../README.md)

This is developer reference for Eidolon's implemented discovery, budgeting, and
usage normalization. For connection and model-selection tasks, use
[Providers](PROVIDERS.md). Capacity is metadata about a serving route; usage is a
measurement from a dispatched request. A catalog cannot report this conversation's
cache hit rate or generation speed.

## Capacity precedence and discovery

The effective dialogue context window comes from a positive manual
`providers[].context_window` override for the selected model, otherwise that model's
cached metadata. Discovery fills `model_metadata` using this precedence:

1. Local runtime capacity: LM Studio loaded-instance configuration, Ollama running
   model context, or llama.cpp default generation settings.
2. Provider `/models` fields: `loaded_context_length`, `context_length`,
   `context_window`, `max_model_len`, `max_context_length`, then OpenRouter's
   `top_provider.context_length`, taking the first valid positive limit.
3. For an unloaded local model without a higher-priority limit, LM Studio's declared
   maximum or Ollama's `num_ctx` Modelfile override / architecture context metadata.
4. For cloud routes still missing capacity, a matching provider/model entry in the
   cached models.dev catalog.
5. Unknown; the app does not invent a model-family capacity.

The [resolver](../src/eidolon/providers/metadata.py) matches model IDs and serving
routes. Catalog selection uses endpoint hosts/paths, with an explicit provider
name as a proxy fallback, rather than a model-family substring. Local routes never
inherit a cloud catalog capacity. LM Studio uses a named loaded instance's limit,
or the smallest loaded limit when a model key addresses several instances.
llama.cpp `/props` is applied only to its model alias or an inventory containing
that single model. Ollama also recognizes the explicit `:latest` alias.

Native probes use `/api/v1/models`, `/api/ps`, `/api/show`, and `/props` as
applicable. They preserve a gateway base path and reuse its authentication.
They query metadata, not model generation/loading. Public catalog requests are
separate and receive neither provider headers nor client-default credentials.
Redirects are disabled. Catalog cache data lives in private `cache/models-dev.json`
with a one-day freshness period; a cached catalog can remain useful offline.
Failures do not prevent selecting an exact model ID or having a conversation.

Per-model records contain `context_window`, optional `vision`, `source`, and
`updated_at`. Connection identity changes clear stale cached metadata. Selecting
a different model clears the prior manual capacity and explicit vision override,
then uses that model's metadata. Late discovery cannot overwrite a changed
connection's metadata. Metadata refresh does not restart integrations or interrupt
a running response.

Image support uses explicit booleans, capabilities, or input modalities from
inventory/native/catalog records. Unknown stays unknown. Direct dialogue vision
requires affirmative support (or an explicit `vision: true` override). A separately
selected analyst permits an unknown custom model but rejects explicit negative
metadata. Neither model names nor successful inventory discovery establish tools,
reasoning values, structured output, or the correct output-token field.

## Full-request token budgeting

[ContextBudget and TokenCounter](../src/eidolon/providers/context.py) are shared
by prompt assembly and the status meter. Dialogue has no independent character
cap: legacy `context_chars` is discarded without creating a token override.

For a known token window:

```text
usable input = window - output reserve - safety reserve
safety reserve = max(64, ceil(window / 50))
```

The output reserve is the largest positive configured `max_tokens`,
`max_completion_tokens`, or `max_output_tokens`. Without one, it is the smaller
of 4,096 and one quarter of the window (at least one token). For unknown capacity,
the default reserve is 4,096 and the app cannot enforce a window. This internal
fallback does not send a new output-limit parameter. It is separate from the
model-picker form's 32,768 starting value, which is persisted only on Apply.

Preflight counts cover messages, tools, structured-response schema, and framing.
Text uses tiktoken's model encoding when recognized, otherwise `o200k_base`; if
unavailable offline, UTF-8 byte counts provide a conservative fallback. Image
transport base64 is not counted as text: each image receives an estimated 4,096
image-token allowance plus message framing. Actual model/gateway tokenization
and image costs can differ.

Prompt preview and every dialogue/tool round fit recent complete exchanges and
bounded earlier excerpts into the available input. Fixed prompts, schemas, the
latest message, and current tool results must still fit. If they cannot, the app
fails before dispatch and reports the same window/reserve used by the meter.
Reduce output reserve or input, correct a wrong capacity, or use a larger model.
See [history checkpoints](PROMPT_CACHING.md#stable-prefix-and-fresh-context).

Preflight status uses `~`; valid provider-reported usage replaces the estimate.
That reported context count reflects actual input/output rather than a future
reserved response. Unknown capacity leaves enforcement to the endpoint.

## Response usage normalization

[request_metrics](../src/eidolon/providers/metrics.py) accepts compatible usage
variants without adding native Anthropic or Responses API transports:

| Compatible response fields | Interpretation |
| --- | --- |
| `prompt_tokens`, `completion_tokens` | Input and output counts |
| `prompt_tokens_details.cached_tokens` or `input_tokens_details.cached_tokens` | Cache-read count |
| `prompt_cache_hit_tokens`, `cached_tokens` | Cache-read fallbacks |
| `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | When Chat-Completions-style prompt count is absent, add cache reads/writes to this input count |
| `input_tokens`, `output_tokens` | Alternate input/output counts; input details can provide cache reads |
| `timings.prompt_n`, `timings.cache_n` | When standard input usage is absent, processed plus reused tokens; reused is the cache-read fallback |
| `prompt_tokens_details.cache_write_tokens` / `input_tokens_details.cache_write_tokens`, `cache_creation_input_tokens`, `cache_write_tokens` | Cache-write count, in precedence order |
| `timings.predicted_n` | Output count fallback |
| `timings.predicted_per_second`, `stats.tokens_per_second` | Reported output rate, preferred over calculation |

Otherwise speed is output tokens divided by elapsed request duration, not a pure
decode benchmark. Cache hit rate is cache reads divided by input only when input
is positive and reads do not exceed it. Context use is input plus output when both
are known, otherwise a valid total/input fallback. Negative, boolean, nonfinite,
or missing values are not valid measurements. Zero is shown only when reported;
stream fragments are never used to infer token counts.

The status bar describes the current dialogue request/tool round, not aggregate
conversation cost. Per-request numeric logs and their privacy boundaries are
covered in [Prompt caching](PROMPT_CACHING.md#numeric-request-diagnostics).

## Other model roles and validation

Separate auxiliary and vision routes resolve metadata for their own model IDs,
with role-owned manual context overrides. They do not copy a different dialogue
model's manual capacity. Auxiliary additionally permits a character input allowance;
that is a work bound, not a capacity measurement. Metadata refresh changes later
assembly without advancing the configured auxiliary routing revision. See
[Auxiliary model](AUXILIARY_MODEL.md) and [vision selection](PROVIDERS.md#use-images-with-a-model).

[Metadata tests](../tests/test_model_metadata.py),
[budget tests](../tests/test_context_budget.py), and
[provider tests](../tests/test_provider.py) check these contracts with synthetic
inventories and mock transports. They do not guarantee that a real endpoint
reports accurate capacity/counters or accepts every generation option.
