# Memory technical reference

[Back to Eidolon](../README.md)

Start with [optional memory setup and use](MEMORY.md). This reference covers
the current adapter, local service operations, and maintenance of an existing
runtime. All paths below refer to the selected private Eidolon state directory,
normally `~/Library/Application Support/Eidolon/`.

## Durable delivery

The conversation engine captures recall and saving choices before provider work.
The storage transaction commits the completed clean user/assistant exchange,
its saving eligibility, and an optional outbox entry together. Repeating a
completed generation cannot change that eligibility or substitute different
text. Prompt snapshots, system instructions, credentials, and cosmetic avatar
cues are not copied into the retention payload. Message text itself can still
contain private information and is sent when saving is enabled.

New documents use `eidolon-room:exchange:<generation-id>`. The operation ID is
UUIDv5 with `uuid.NAMESPACE_URL` and that complete document ID. Payloads contain
both speakers' recorded names, timestamps, source context, and session metadata.
The adapter submits with `update_mode: replace`, `async: true`, and the saved
operation ID. Persisted historical namespaces and operation identities are
preserved; a rename is not a reason to recreate them or re-ingest conversations.

| Outbox state | Interpretation |
| --- | --- |
| `queued` | Local completed exchange awaiting submission. |
| `submitted` | Trackable server operation accepted or still pending/processing. |
| `failed` | Last attempt failed; payload, identity, and retry time remain saved. |
| `completed` | Server confirmed completion, or returned synchronous `success: true` with `async: false`. |

`outbox` status also exposes `held`, `eligible`, `rejected`, `review_polling`,
`delivery_paused`, and a safe error. `pending` includes unfinished held work;
rejected entries are excluded from pending status counts. These aggregate counts
do not identify the completion of a particular new exchange.

The dispatcher requires `retain_enabled` and `verified`; it does not require
recall or new saving to remain enabled. Approved eligible rows are delivered in
bounded passes (up to 20 ready entries). Previously attempted rows are polled
first. Completed operations finish locally; pending/processing operations stay
submitted; failed/cancelled operations use the server's retry endpoint with the
same UUID. Only an explicit operation 404 is treated as absence permitting
resubmission of the frozen payload. Unknown states or mismatched operation IDs
are failures, not confirmation.

Failures persist exponential cooldowns capped at one hour. Capacity responses
can lengthen that delay using a bounded `Retry-After`; the backlog and new
entries inherit the durable cooldown. HTTP 429 and 503 are busy
responses, not reasons to restart a healthy service. Transport errors and HTTP
502/504 may use the bounded managed-runtime recovery path. Uncertain retain
submissions are not immediately replayed. Restarting Eidolon preserves the queue
and its retry timing.

Pause acknowledgement shares the submission lock, including during adapter
replacement. Acknowledgement waits for an already running submission; it cannot
cancel a server operation already accepted. Polling uncertain held operations
may still occur while delivery is paused, without authorizing new submissions.

## Local service operations

These are operator procedures for the current local Unix-socket API. They are
not CLI subcommands or buttons in `/memory`. Run them from the checkout root
against a running Eidolon service. `load_config()` selects the normal private
configuration or the current `EIDOLON_HOME`. If you normally use `--config`, use
`load_config(Path("/private/path/to/config.yaml"))` instead, importing `Path`
from `pathlib`. These snippets do not start a stopped service.

### Inspect readiness and delivery

```sh
uv run python - <<'PY'
import asyncio
import json
from eidolon.config import load_config
from eidolon.service.client import RoomClient

async def main():
    client = RoomClient(load_config().socket_path)
    state = await client.request("memory_state")
    print(json.dumps({key: state[key] for key in ("controls", "readiness", "outbox")}, indent=2))

asyncio.run(main())
PY
```

`readiness.verified` and `readiness.retain_enabled` are saved operator settings;
`readiness.status` is connection/error information. Reading `memory_state` does
not trigger recall, retention, or summary inference. Run `/check` or `doctor` to
refresh connection diagnostics. A configured bank is checked even when recall
is off by the running service; with no bank selected, it skips the probe.

### Pause or resume pending delivery

```sh
uv run python - <<'PY'
import asyncio
import json
from eidolon.config import load_config
from eidolon.service.client import RoomClient

async def main():
    client = RoomClient(load_config().socket_path)
    state = await client.request("memory_delivery", pause=True)
    print(json.dumps(state["outbox"], indent=2))

asyncio.run(main())
PY
```

Confirm `delivery_paused: true`. This choice survives restart. Change only
`pause=True` to `pause=False` to resume; previously approved work becomes
deliverable when readiness permits. Turning saving off is a different operation.

### Review held legacy exchanges

Unfinished older outbox entries with unknown eligibility are held during schema
upgrade. Their payloads and IDs are not rewritten. Pause delivery first, then
inspect the held previews locally:

```sh
uv run python - <<'PY'
import asyncio
import json
from eidolon.config import load_config
from eidolon.service.client import RoomClient

async def main():
    client = RoomClient(load_config().socket_path)
    state = await client.request("memory_state")
    print(json.dumps(state["legacy_pending"], indent=2, ensure_ascii=False))
    print(json.dumps(state.get("inspection_omitted", {}), indent=2))

asyncio.run(main())
PY
```

This output contains conversation content; keep it private. Previews are limited
to 4,000 characters and mark `source_truncated`. The list is limited to 100 and
can be further reduced by the socket byte allowance. Do not approve unseen or
truncated content without inspecting the original local exchange.

For an explicitly reviewed selection, replace the synthetic ID below with IDs
from that preview. The API accepts 1–100 IDs; it never approves the whole backlog
by omission.

```sh
uv run python - <<'PY'
import asyncio
import json
from eidolon.config import load_config
from eidolon.service.client import RoomClient

async def main():
    client = RoomClient(load_config().socket_path)
    state = await client.request(
        "memory_review_legacy",
        approve=False,
        document_ids=["example-reviewed-document-id"],
    )
    print(json.dumps(state["outbox"], indent=2))

asyncio.run(main())
PY
```

`approve=False` excludes selected eligible-for-review rows from future delivery;
use `approve=True` to include them. Review decisions are one-way. Before deciding,
the dispatcher polls uncertain submitted/failed legacy operations. Still-running
operations stay held, completed operations finish, and a failed poll defers work.
Consequently a request may leave selected entries held; inspect again later.
Queued review decisions update unknown eligibility only and cannot revise a new
turn's saved choice. Repeat inspection/review for remaining pages. Exclusion
does not undo an earlier external submission. Resume only when you intend the
approved backlog to deliver.

### Private context and evidence

`memory_state(session_id=...)` lists up to 100 saved attempts for that room,
current notes, revisions, and maintenance status within a bounded UTF-8 response.
`memory_manifest(manifest_id=...)` loads a complete saved memory manifest;
`memory_continuity_item(item_id=...)` loads a note and source evidence;
`memory_continuity_history(history_id=...)` loads a historical note revision.
Use the IDs returned by the corresponding inspection response. These are
read-only service requests through the same `RoomClient.request` interface.
`memory_continuity_run(task_id=...)` provides content comparisons for run history,
without a raw source inventory. `maintenance_attempts(task_id=...)` provides
attempt provenance. These diagnostic records are private.

The content browser intentionally shows prose instead of backend identifiers.
Full manifests retain records, dates, IDs, provenance, omission reasons, frozen
controls, budget allocation, and actual supplied text. Tool evidence counts as
supplied only when a later provider invocation received it. Failed/cancelled
attempts may have manifests without becoming learning inputs. See
[compact rendering](MEMORY_CONTEXT_OPTIMIZATION.md) for attribution limits.

## Current-life scheduling and persistence

The service's maintenance loop runs roughly every ten seconds, checking memory,
reloading saved connections when foreground generation is idle, ticking scheduled
jobs, dispatching the outbox, and considering a current-life pass. This is not
an exact execution-time guarantee. Current-life work is bounded to one task and
is cancelled before foreground generation acquires priority. It uses the
configured auxiliary route and never calls tools.

Automatic summary maintenance defaults off. Once enabled, it coalesces eligible
completed chat exchanges until the batch threshold (default eight) or idle time
since the latest completion (default 600 seconds). Smaller model input allowances
can reduce a batch. Manual **Refresh current life** requests one bounded pass
without enabling automatic updates or reply inclusion. It bypasses coalescing and
retry delay and starts a fresh bounded recovery pass for pending work. It preserves
lifetime history and usage, source eligibility, locks, capacity, and revision checks.

Inputs are oldest-first completed chat exchanges with explicit saving eligibility
and matching scope (`real_life` for the worker). Partial, opted-out, unknown
eligibility, scheduled-job, and other-scope exchanges are excluded. Hindsight
delivery/indexing need not finish first. Automatic notes are model-written
narratives with complete cited original-message snapshots; no raw exchange bridge
is added to current-life reply context. Manual notes do not create synthetic
exchanges or external retention entries.

SQLite stores scope/control revisions, source hashes, immutable batch identities,
stage status, attempts, note revisions, the input high-water mark, and a separate
processed/unresolved evidence ledger. Commit checks evidence and revisions
atomically. Stale edits, malformed proposals, cancellation,
and incomplete model streams leave the batch pending without partial note updates
or watermark advancement. Retry backoff grows from 30 seconds to one hour;
foreground interruption permits immediate retry. Retries preserve the original
task/evidence identity across restart while capturing current configuration and
participant names for the new attempt. Each original batch defaults to six dispatched
requests per pass (`memory.continuity_recovery_calls`, 1–100), including correction,
rejected, interrupted, and stale requests. Planning or credential failures before
dispatch do not count. The frozen pass allowance survives restart. Explicit Refresh
starts a new user-requested pass; paced backlog recovery can start a bounded
automatic pass. Settings changes do not replenish an existing pass.

Recognized configuration/authentication and input-limit failures pause unchanged
settings instead of repeatedly dispatching. A relevant configuration change makes
the batch eligible gradually under the shared daily allowance; explicit Refresh
also releases the pause. Exhaustion sets the evidence aside and remains visible in Run history
and the main Life indicator. Complete evidence stays retained while newer eligible
evidence continues under its own normal budget. The high-water mark never proves that older gaps completed.
The backlog shares six automatic retry calls per UTC day by default
(`memory.continuity_daily_retry_calls`, 0–100; 0 pauses automatic backlog retries),
with one opportunity per ten minutes and new evidence first. Families take turns.
Usage, the accounting period, and pacing survive restart; daily limit edits after
the first opportunity apply next period. Unchanged configuration failures receive
no daily probes. Run history shows remaining calls, next eligibility, and whether
each attempt was automatic or user-requested.
Open a retained run and choose Retry batch to request a separate bounded pass;
a later successful run does not erase unresolved gaps or their warnings. See
[bounded recovery](AUXILIARY_MODEL.md#bounded-recovery-and-configuration-pauses) for
settings, actions, diagnostics, and the limits of this policy.

Completed work is not automatically dispatched again. Legacy narrative conversion
and over-limit capacity review can run without new exchanges; explicit refresh
can reconsider a completed no-change conversion/review. The
[current-life guide](CURRENT_LIFE_SUMMARIES.md) details note protection, expiry,
replacement, conversion, and safe failure messages.

## Existing managed runtime

This section applies only to an installation that already has private
`memory-service/settings.json`, an independently installed official Hindsight/pg0
runtime, preserved PostgreSQL storage/instance metadata, and working credential
references. Fresh setup creates none of these. Do not construct an empty
replacement database to repair an unavailable existing bank.

The [operations guide](OPERATIONS.md#existing-managed-memory-runtime) owns the
supervisor's prerequisites, preserved storage/instance checks, watchdog behavior,
LaunchAgent inspection/restart/stop controls, and diagnostic locations. It also
explains the separate application service and why local automatic recovery can
restore a deliberately unloaded memory agent. Stopping the memory API leaves
PostgreSQL running. External endpoints remain under their operator's service
manager.

Safe reads receive at most one transport-recovery retry within their deadline.
Uncertain writes keep their outbox identities for later polling. Restoring or
upgrading a backend is a separate procedure from restarting the preserved runtime;
see the backup and recovery boundaries below.

## Compatibility and synthetic verification

The adapter supports bank inventory, bounded recall, structured reflection,
async retention, and operation polling/retry. The repository contract fixture is
`tests/fixtures/hindsight_openapi_0_10_2.json`; it is a compatibility baseline,
not a declaration of the version installed on your machine. Added optional API
fields may be compatible; changed required shapes must be reviewed. A health
response, matching version string, or successful recall does not verify retention
idempotency, generated evidence fidelity, or safe backend schema rollback.

`scripts/memory_capabilities.py` is a developer verification harness that **creates
a new synthetic bank** and exercises mutations on a separately prepared local
service/database. It does not activate the app or configure its production bank.
Never use the ordinary memory endpoint for this harness. With an isolated API
already running on the synthetic example port:

```sh
export EIDOLON_HOME="/private/tmp/eidolon-memory-verification-example"
uv run python scripts/memory_capabilities.py \
  --base-url http://127.0.0.1:9183 \
  --bank eidolon-phase0-synthetic-verification-example \
  --state-dir "$EIDOLON_HOME" \
  --isolated-instance
```

Use a fresh bank ID for another run and a dedicated state directory outside Git
and existing app state. `--isolated-instance` is an operator assertion that the
database is isolated; a different port alone cannot ensure isolation. The harness
rejects remote/credential-bearing URLs, reused banks, app-state directories, and
reserved ports 9177/9191, but those guards do not recognize every possible live
installation. For authenticated test services, `--api-key-env NAME` selects an
environment credential without putting its value in the command.

Inspect the resulting `<bank>-capabilities.json` and individual check outcomes.
The harness checks caller operation-ID preservation across repeated submission,
completion polling, and document counts, alongside other API capabilities. A
reported limitation is not proof of evidence/correction/deletion safety. It does
not remove its synthetic bank or set `verified`/`retain_enabled` for you.

### Retrieval and prompt benchmarks

For an opt-in Current Life evaluation through the production maintenance worker
and local storage, see [First-attempt Current Life baseline](MAINTENANCE_BASELINE.md).
The [reliability assessment](MAINTENANCE_ASSESSMENT.md) pairs its frozen workload
with revised first attempts and reports unattended recovery separately, including
failed attempts, retained evidence, usage coverage and terminal review limits.
It provides offline scripted checks and explicitly enabled, synthetic real-model
trials with recovery disabled, usage measurements, and human quality review.

[`scripts/memory_baseline.py`](../scripts/memory_baseline.py) measures the current
engine's retrieval and actual prompt assembly against the repository's
[synthetic fixture](../tests/fixtures/memory_baseline_companion.json). It copies
generic prompt defaults without reading private app configuration, prompts,
skills, or conversations. Optional provider replies use an explicitly supplied
private provider-only configuration. Run the harness only against separately
prepared, isolated memory storage; a different API port is insufficient.

The fixture covers room changes, restart, dates, changing preferences, fiction,
attribution, contradictions, multilingual text, and missing evidence. For a
matched baseline/candidate comparison, both APIs must use the same isolated
database. Start with a fresh synthetic bank and an empty dedicated state
directory outside Git and live app state:

```sh
export EIDOLON_HOME="$(mktemp -d /tmp/eidolon-memory-benchmark.XXXXXX)"
uv run python scripts/memory_baseline.py \
  --url http://127.0.0.1:9183 \
  --bank eidolon-phase0-synthetic-companion-example \
  --label baseline

uv run python scripts/memory_baseline.py \
  --url http://127.0.0.1:9184 \
  --bank eidolon-phase0-synthetic-companion-example \
  --label candidate --reuse-bank

uv run python scripts/memory_baseline_compare.py \
  "$EIDOLON_HOME/runs/eidolon-phase0-synthetic-companion-example/baseline.json" \
  "$EIDOLON_HOME/runs/eidolon-phase0-synthetic-companion-example/candidate.json"
```

Seeding creates the synthetic bank, submits fixture exchanges through the
durable outbox, and waits for completion. The runner refuses remote URLs,
reserved ports 9177/9191, an existing bank during seeding, and state without a
matching synthetic marker. Reuse checks both the completed seed manifest and
the bank's ID/name marker. Stable document and operation identities include the
bank and fixture hash; changing the bank is not a way to reuse an operation ID.
The harness deletes no bank. Its guards cannot recognize every live installation.

For final replies, append the same options to both baseline commands:

```sh
--provider-config /private/path/to/provider-only-config.yaml --reply-concurrency 4
```

The file uses ordinary `provider` and `providers` fields; only the selected
provider profile is read. Credentials resolve through its references. The harness
forces plain replies, disables tools, and uses the fixture's token context window.
It records the selected parameters, source fingerprints, and concurrency for
comparison. Retrieval/prompt preparation is sequential; reply concurrency is
bounded from 1–8. For timing without completions, retain the same provider file,
add `--no-replies --reuse-bank`, and choose fresh labels. Existing labels cannot
be overwritten. Reports, captured requests/replies, and the source-only SQLite
transcript remain private under `$EIDOLON_HOME/runs/<bank>/`.

Every source is indexed before historical replay. Probe timestamps steer recall
without strictly excluding future sources; local history uses the probe clock
and room boundary. Evidence-atom matches measure substring coverage, not semantic
correctness. Supplied coverage excludes the question itself, and missing-evidence
probes have no retrieval coverage score. Review final replies against the fixture
rubric for attribution, current preferences, dates, fiction, and uncertainty.
Provider-reported usage is distinct from character/byte counts. Partial or failed
runs cannot be compared as completed baselines; old latency and quality results
are not current guarantees.

For offline packing comparisons using existing explicitly synthetic captures:

```sh
uv run python scripts/memory_phase1_replay.py \
  --report /private/path/to/baseline.json \
  --report /private/path/to/candidate.json \
  --output /private/tmp/eidolon-memory-replay-example.json --repetitions 100
```

The [replay tool](../scripts/memory_phase1_replay.py) makes no provider or memory
requests. It checks whole-record selection, evidence coverage, provenance labels,
and local assembly time. Substituting rendered text in a captured request estimates
character allocation; it does not measure new token usage, backend latency, or
reply quality. See the [baseline tests](../tests/test_memory_baseline.py) and
[replay tests](../tests/test_memory_phase1_replay.py) for controlled regressions.

### Installed runtime source contract

[`scripts/memory_runtime_contract.py`](../scripts/memory_runtime_contract.py)
is specifically for independently installed Hindsight 0.10.2 and 1.0.0 runtimes:

```sh
uv run python scripts/memory_runtime_contract.py \
  --old-python /absolute/path/to/hindsight-0-10-2/bin/python \
  --candidate-python /absolute/path/to/hindsight-1-0-0/bin/python
```

It uses clean subprocess environments and offline Alembic SQL to check those
exact releases' migration ledger behavior and installed scoped-deletion guards
without database access. It is not a general upgrade validator and does not
exercise HTTP authentication, API restart, persisted migrations, or rollback.
Other versions require their own contract review. Use each harness's `--help`
for current arguments.

### Upgrade and recovery boundaries

Before changing an existing runtime or schema, preserve consistent database and
application backups plus configuration, dependency versions, and saved operation
identities. Restore into a separate database for testing. Do not restore older
SQLite over newer conversations, use a dispatcher that ignores held/rejected
eligibility, or replay history under newly generated IDs. Backend provisioning,
schema upgrades/rollback, and live installation migration require their own
verified operational procedure.

See [Backup and recovery](RECOVERY.md) for consistent SQLite snapshots,
PostgreSQL backup and isolated restoration, and schema rollback limits.
[State migration](STATE_MIGRATION.md) covers installation copies while preserving
saved work and managed storage identity; [Privacy](PRIVACY.md) explains data
destinations and safe handling of diagnostics.

## Implementation and regression references

- [Configuration](../src/eidolon/config.py) and [CLI setup/doctor](../src/eidolon/cli.py)
  define defaults, credential references, and the limits of interactive setup.
- [Conversation engine](../src/eidolon/engine/conversation.py),
  [store](../src/eidolon/storage/store.py), and
  [adapter/dispatcher](../src/eidolon/memory/hindsight.py) implement frozen
  eligibility, durable IDs, polling, delivery pause, and review.
- [Service runtime](../src/eidolon/service/runtime.py),
  [maintenance worker](../src/eidolon/memory/maintenance.py), and
  [continuity validation](../src/eidolon/memory/continuity.py) own scheduling and
  atomic source/revision checks.
- [Memory browser](../src/eidolon/ui/memory.py) and
  [its tests](../tests/test_ui_memory.py) define the current content-only controls.
- [Delivery tests](../tests/test_memory_delivery.py),
  [adapter tests](../tests/test_memory.py),
  [service/socket tests](../tests/test_service.py),
  [narrative tests](../tests/test_continuity_narrative.py), and
  [supervisor/recovery tests](../tests/test_memory_runtime.py) exercise controlled
  services and synthetic state. They verify implementation contracts without
  establishing the semantic fidelity of a selected model or the health of a live
  installation.
