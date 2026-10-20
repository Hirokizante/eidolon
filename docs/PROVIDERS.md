# Providers and models

[Back to Eidolon](../README.md)

Eidolon uses streaming **OpenAI-compatible Chat Completions**. Configure a running
server or gateway and its exact model ID. Compatibility describes the request
protocol; it does not guarantee tools, vision, reasoning controls, structured
output, usage counters, or prompt caching. First-time endpoint setup belongs in
[Installation](INSTALLATION.md#configure-a-model).

## Add or edit a connection

1. Open `/connect`. Choose an existing connection or a new one.
2. Enter **Base URL (required)**, such as `http://127.0.0.1:1234/v1` for a local
   server you have already started. Use your gateway's final URL; redirects are
   disabled. Embedded URL credentials are rejected; use credential references for authentication.
3. Enter an **API key** if required. Blank keeps an existing key; local servers
   can have no key. An optional **Display name** makes the picker easier to read.
4. Choose **Fetch models** (Ctrl+R) to query the inventory. If discovery is
   unavailable, enter **Fallback model ID** manually. A discovered ID is not a
   capability test. Save needs a model ID, supplied manually or from discovery.
5. Expand **Advanced: headers, tools and context** only as needed. It contains
   the saved connection ID, custom headers as JSON, a context token override,
   and **Enable tool calling**. Choose **Save** (Ctrl+S).
6. Open `/models` to select the dialogue provider/model and **Apply**. Saving a
   connection alone does not select a different dialogue provider.

Connection and model drafts survive closing/reopening their menus during this
terminal session. Fetching models does not save a connection. Discovery failures
allow manual IDs; check the exact ID with the endpoint operator. Changing a saved
base URL requires reentering its key/reference and clearing or reentering secret
header references, so credentials are not silently sent to another endpoint.

## Select the dialogue model and limits

In `/models`, choose **Provider**, filter the inventory, use arrows and Enter to
select, or type an exact **Model ID**. **Refresh** (Ctrl+R) updates discovery.
**Connect** opens the connection editor. Adjust these fields, then **Apply**:

| Field | Meaning |
| --- | --- |
| Reasoning effort | Default omits the parameter; listed options include none, minimal, low, medium, high, xhigh, and max. Use only values your endpoint supports. A saved custom value remains visible. |
| Context Size (tokens) | Positive manual serving capacity; blank uses discovered capacity. Switching model clears the previous manual override. |
| Max Response Length (tokens) | Positive output-token limit. The form starts at 32,768 if none is configured; opening or closing it alone does not save that value. |

Apply saves to private `config.yaml` and affects the next response at a safe
boundary. An active response keeps its provider snapshot. Existing output-field
names are preserved; if none exists, Apply uses `max_completion_tokens` and removes
conflicting output-limit fields. Some compatible endpoints require `max_tokens`;
set that field in private configuration first. Unrelated generation parameters
are preserved.

A larger output reserve leaves less room for input. If a small model reports
that the request cannot fit, reduce Max Response Length, shorten prompts/tool
output, or choose a model with more capacity. Correct an inaccurate manual Context
Size rather than inflating it to bypass the serving limit. Unknown capacity stays
unknown and leaves enforcement to the provider. The status bar's `~` marks a
preflight estimate. Discovery precedence, token estimates, and usage normalization
are documented in [Model metadata](MODEL_METADATA.md).

## Credentials and private configuration

Keys typed into the TUI are stored in macOS Keychain under service `eidolon`;
configuration stores `keyring:NAME` references. `env:NAME` reads the environment
of the service process. For service-environment setup, see
[state and credentials](INSTALLATION.md#configuration-state-and-credentials).
Do not put literal credentials in `api_key_ref` or sensitive headers. Secret-bearing
headers require references too; ordinary nonsecret header strings are allowed.

This synthetic fragment shows one dialogue connection. Replace the example URL,
model, and reference with your own private values; merge it into your existing
configuration instead of replacing unrelated settings:

```yaml
provider: gateway
providers:
  - name: gateway
    endpoint: https://api.example.com/v1
    model: example-chat-model
    api_key_ref: env:EIDOLON_MODEL_API_KEY
    headers:
      X-Workspace: example-workspace
      X-Access-Token: env:EIDOLON_GATEWAY_TOKEN
    tools: true
    presentation: auto
    structured_verified: false
    vision: null
    post_history: system
    context_window: null
    parameters:
      max_completion_tokens: 4096
```

`parameters` supplies endpoint-specific generation options. Reserved overrides
(`model`, `messages`, `stream`, `tools`, `tool_choice`, `response_format`) are
rejected. Configure only supported options; the app does not silently remove
rejected semantic parameters or choose another model. Streamed usage is requested
by default; an endpoint that rejects it can use
`parameters: {stream_options: {include_usage: false}}`. Missing counters remain
unknown. The full generic field inventory is in [config.example.yaml](../config.example.yaml).

## Match capabilities to the endpoint

**Tools:** `tools: true` enables tool requests; it is not evidence that the model
supports them. Turn it off in `/connect` for text-only endpoints. Agent mode
requires tool calling. Skills use a direct-instruction fallback when tools are off.

**Presentation:** `plain` is ordinary text. `prefix` uses cosmetic cue instructions
with a normal streamed reply. `auto` (default) chooses `prefix` unless
`structured_verified: true`, which selects strict JSON presentation. Explicit
`json` requires that verification flag and a working strict `json_schema` endpoint.
These are private configuration options, not model-picker capability probes. Do
not mark structured output verified merely because `/models` lists the model.

**Post-history instructions:** `post_history: system` (default) sends
`POST_HISTORY.md` as a trailing system message; `user` appends it to the current
user content. Choose according to your endpoint's role support. See
[Prompt assembly and caching](PROMPT_CACHING.md) for exact ordering.

**Metadata:** Discovery can supply context size and explicit image-input support.
It does not establish accepted reasoning values, token-limit fields, tools, or
strict structured-output behavior. Model names alone do not establish capability.

## Use images with a model

By default, `/preferences` → **Vision model** → **Use dialogue model vision**
(`vision.mode: disabled`) sends attached images directly to the dialogue model
when it supports images. Explicit metadata or a configured `providers[].vision`
boolean supplies that capability. If metadata is inconclusive, set `vision: true`
only after verifying the actual model/endpoint; false disables direct vision.
Selecting a different dialogue model clears that model-specific override.

For a separate image analyst:

1. Save its connection through `/connect`.
2. Open `/preferences` → **Vision model**, choose **Choose a separate vision model**.
3. Use **Model settings** to choose the connection, exact vision model ID, reasoning
   effort, Context Size, and Max Response Length. Apply returns to Preferences.
4. Review **Advanced request options** if your endpoint needs a different timeout,
   thinking mode, or output-token field. **Test** checks the draft using a synthetic
   image; it does not save it. Choose Preferences **Apply** to persist the route.

Separate vision gives the tool-capable dialogue model `vision_analyze`, which it
can call with its own image question. It does not send the pixels to the dialogue
model. The analyst gets a plain request without the conversation's persona or tool
definitions. It needs image support and its own valid budgets. The defaults are
60 seconds and 4,096 output tokens; bounds are 1–120 seconds and 128–8,192 tokens.
Switching target clears the role's manual capacity. A failed or truncated analysis
does not silently fall back to another route. Test success checks compatibility,
not visual accuracy; inspect results for your task.

Clipboard attachment controls and limits are in [Everyday use](EVERYDAY_USE.md).
The background model for current-life maintenance is a different role; configure
it through [Auxiliary model](AUXILIARY_MODEL.md). Scheduled jobs keep their own
override or dialogue target. Hindsight server inference is configured separately
from all of these application model roles.

## Troubleshoot a route

| Symptom | Next step |
| --- | --- |
| Discovery is empty or unavailable | Check the final base URL, running server, key access, and inventory support; enter a known exact ID manually |
| HTTP 401/403 or unavailable credential | Check the saved Keychain entry or the environment visible to the service |
| HTTP 429 | Check the provider's quota/rate limit and retry later |
| Request rejects a parameter | Review output-field name, reasoning/thinking, streamed usage, tool support, and structured presentation; change the explicit configuration |
| Context overflow | Check discovered serving capacity and output reserve in [Model metadata](MODEL_METADATA.md) |
| Unknown cache/speed values | The endpoint may omit valid counters; missing is not zero |
| Image input refused | Verify direct vision metadata/override or the separate analyst's capability and tool prerequisites |

Use `/inspect` to review the exact dispatched JSON locally and `/check` or
`uv run eidolon doctor` for connection diagnostics. A successful inventory probe
does not replace a real conversation or a feature-specific test. Review private
request exports before sharing them.
