# Optional memory

[Back to Eidolon](../README.md)

Eidolon keeps local conversation history whether or not you use external memory.
Hindsight is an optional service for recalling older information and saving new
completed exchanges. Current-life notes are a separate local summary of ongoing
topics; they use the [auxiliary model](AUXILIARY_MODEL.md), not Hindsight inference.
You can use manual current-life notes without an external memory service.

## Before connecting Hindsight

You need an already running Hindsight API, an **existing bank ID**, and a
credential with access to that bank if the API requires authentication. Obtain
these from the service operator. The adapter checks the bank inventory before
recall or retention and refuses a missing bank instead of creating one.

Eidolon setup does not provision Hindsight, PostgreSQL, a managed runtime, or a
bank. Installing Eidolon does not install those services. For an existing
Eidolon-managed runtime, see the separate
[runtime maintenance reference](MEMORY_REFERENCE.md#existing-managed-runtime).

New installations use these defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `memory.url` | `http://127.0.0.1:8888` | Placeholder service origin; setup does not start a server there. |
| `memory.bank` | Empty | No external bank selected. |
| `memory.enabled` | `false` | Recall and use of learned context are off. |
| `memory.save_enabled` | `false` | New completed exchanges are not eligible for learning. |
| `memory.retain_enabled` | `false` | External delivery is not authorized. |
| `memory.verified` | `false` | Retention compatibility has not been acknowledged. |
| `memory.continuity_enabled` | `false` | Automatic current-life updates are off. |
| `memory.continuity_use_enabled` | `false` | Current-life notes are not included in replies. |

Interactive `uv run eidolon setup --interactive` records an existing endpoint and
bank. It does **not** enable any of these switches or verify retention. Blank
input preserves an existing value; on a fresh installation, leaving the bank
blank keeps external memory unconfigured. There is no memory activation wizard.

## Configure and activate recall

Run commands from the checkout root. Use the same `EIDOLON_HOME` or global
`--config` selection throughout; see
[private state and credentials](INSTALLATION.md#configuration-state-and-credentials).
Edit the private `config.yaml` created by setup, not the repository example.
Preserve other settings and any existing outbox when editing an installation.

1. Record the existing URL and bank through interactive setup, or edit their
   fields in the private file. This synthetic example initially enables recall
   only:

   ```yaml
   memory:
     url: http://127.0.0.1:8888
     bank: example-existing-bank
     api_key_ref: null
     enabled: true
     save_enabled: false
     retain_enabled: false
     verified: false
   ```

2. For an authenticated endpoint, run `uv run eidolon secret set hindsight` and
   enter the credential at its hidden prompt, then set
   `memory.api_key_ref: keyring:hindsight`. An `env:NAME` reference also works if
   that variable reaches the background service. URLs must not contain embedded
   credentials. Setup's provider-key prompt does not configure a Hindsight key.
3. Close the UI and run `uv run eidolon service stop`, then `uv run eidolon` with
   the same state selection to load the edited configuration cleanly. Saved
   connection changes also reload before the next conversation turn. In
   `/preferences` → **Memory**, **Use learned memories** controls
   `memory.enabled`; Ctrl+S saves preferences.
4. Run `/check` in the app or `uv run eidolon doctor` from Terminal. With the
   service running, memory should report a connection to the existing bank.
   A stopped-service `doctor` only probes memory when recall is enabled;
   “disabled by configuration” is not a connectivity test. Neither check writes
   a memory or changes readiness flags.
5. Send a query appropriate to information already in your bank. Open `/memory`
   → **Reply context** and select the reply to see the memory actually sent to
   the model. An empty result is valid and does not prove retrieval is broken.
   `/inspect` shows the actual model request. Recall transmits a bounded query
   derived from the current message and eligible recent history to Hindsight,
   even when saving is off.

Recall does not require either retention-readiness flag. Keeping saving off
still preserves local chat history. Re-enabling saving later does not make
exchanges accepted while it was off eligible for learning.

## Activate saving and verify delivery

Saving has three distinct parts: eligibility for new exchanges, authorization to
submit eligible exchanges, and confirmation that the external operation finished.

1. Verify your service's retain and operation-tracking compatibility before
   authorizing delivery. A connection check proves only that the bank is
   available. The required behavior includes preserving caller-supplied document
   and operation identities, reporting asynchronous completion, and safely
   retrying the same operation. Use an isolated service/database and synthetic
   content for compatibility checks; the
   [verification reference](MEMORY_REFERENCE.md#compatibility-and-synthetic-verification)
   describes the repository harness. A successful test against a separate service
   does not by itself establish your production service's configuration.
2. After those checks, manually set **both** `memory.verified: true` and
   `memory.retain_enabled: true` in private configuration. These are operator
   acknowledgements, not automatically discovered health results. Neither setup,
   `/check`, `doctor`, nor preferences sets them. Reload as above. Existing
   approved queued work can now deliver even with recall and new saving off;
   [pause delivery](MEMORY_REFERENCE.md#pause-or-resume-pending-delivery) first if
   you need to review a backlog.
3. In `/preferences` → **Memory**, turn on **Save new memories** and save with
   Ctrl+S, or set `memory.save_enabled: true` in private configuration. You can
   leave **Use learned memories** off to save without recalling. The choice is
   captured when a generation starts, so finish or cancel an active turn before
   changing it for the next turn.
4. Complete a harmless synthetic exchange, then run
   `uv run eidolon service status`. Inspect `outbox.counts` and `outbox.error`.
   `queued` means locally saved for delivery; `submitted` means accepted for
   external processing; only `completed` records confirmed success. Counts are
   installation-wide. Use your service's operation/document inspection when
   you need to confirm a particular exchange. A reply arriving or a queue count
   increasing does not prove persistence or immediate search visibility.

Enabling **Save new memories** while readiness is false is supported: completed
eligible exchanges queue locally and survive restart. Status explains that
retention awaits verified configuration. Cancelled, partial, or failed
generations do not create a completed transcript/outbox exchange. Failed model
attempts may still have saved request contexts for inspection.

## Queues, outages, and older installations

Turning off recall or saving does not revoke previously accepted deliveries.
The dispatcher keeps frozen payloads and operation identities, polls uncertain
submissions before replay, and waits through persisted retry delays. An endpoint
outage leaves eligible work queued locally. A busy service can defer the entire
backlog, including newly queued exchanges. See
[delivery states and retry rules](MEMORY_REFERENCE.md#durable-delivery).

Older outbox entries whose original eligibility is unknown may be **held** for
review. They are not silently approved when you enable saving. Previously
submitted work may already finish externally. Reviewing or rejecting local work
does not remove externally saved content. The current `/memory` screen has no
delivery-pause or held-review controls, despite older help text describing them.
Use the documented [local service operations](MEMORY_REFERENCE.md#local-service-operations)
to pause delivery or review selected held entries.

If foreground recall fails, the waiting message requires a choice: `/retry`
tries recall again; `/retry without-memory` sends it without recalled or
current-life context for that reply. This does not change future settings or
the turn's saving choice. Saving off does not hide the current query from recall.

For connection failures, check the URL, bank ID, credential access, and service
health. A “bank not found” error calls for correcting the selected bank or
restoring the intended service, not making a replacement bank. Readiness flags
do not repair failed credentials, unavailable inference, or incompatible APIs.
Current-life update failures are separate from Hindsight readiness and do not
block ordinary replies.

## Inspect context and current life

Open `/memory` directly or through Ctrl+P. **Reply context** lists saved attempts
for the current room and shows recalled memories, current-life context, and any
additional memory-tool results actually sent on later provider rounds. It is
evidence of supplied context, not proof that a memory caused particular words.

Choose **Current life** for **Notes**, **Run history**, **Refresh current life**,
and **Create note**. `/runs` opens Run history directly with safe per-attempt
causes, model, timing, available numeric usage, next action, and note comparisons.
A quiet Life indicator near the composer links to `/runs` while any maintenance
condition remains unresolved; later success does not erase earlier attempt history.
Enter opens details, Ctrl+F searches visible content, Ctrl+L
reloads the view, and Esc returns to the list and then the conversation. Loaded
details remain readable during a service outage. Lists are bounded and report
when more entries exist than the view can show. Source IDs and full diagnostic
provenance are retained privately through the service; the content browser does
not display a raw provenance dump or let you edit the external bank.

The [current-life guide](CURRENT_LIFE_SUMMARIES.md) covers the independent update
and inclusion controls, manual notes, narrative summaries, archival, and retry
behavior. The [rendering reference](MEMORY_CONTEXT_OPTIMIZATION.md) explains
dates, attribution, evidence limits, and compact context.

## Disable the integration

To stop using learned context, turn off **Use learned memories**. To stop new
learning eligibility, also turn off **Save new memories**. Existing local history
remains available. Earlier eligible exchanges remain deliverable.

To stop pending submissions as well, use the
[persisted delivery pause](MEMORY_REFERENCE.md#pause-or-resume-pending-delivery).
For a fully inactive external connection, close the UI, stop the service, and
edit private configuration: set `enabled`, `save_enabled`, `retain_enabled`, and
`verified` to `false`, and clear `bank` to `''`. Clearing the bank prevents routine
startup/background bank checks. Keep the intended endpoint/bank recorded
privately before disconnecting: pending payloads use the adapter's selected bank
when later delivered. Do not point an existing backlog at another bank casually.
Start Eidolon again if you want to continue using local chat.

If you also want no automatic current-life inference, turn off **Update current
life automatically**; **Auxiliary model → Off** disables its model calls.
Turn off **Use current life in replies** for its independent inclusion choice.
Manual notes remain local. Disabling controls, pausing delivery, or clearing the
bank deletes neither local history nor externally retained memories; content
already submitted can finish at the server.
