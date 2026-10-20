# Background operation and scheduled jobs

[Back to Eidolon](../README.md)

Eidolon's terminal UI connects to an application service that owns conversations,
SQLite history, scheduled jobs, integration connections, pending memory delivery,
and current-life maintenance. Closing the UI with Ctrl+Q or `/quit` leaves that
service running. The optional existing-bank memory supervisor is a separate
process with separate storage and lifecycle controls.

Run the `uv run` commands below from the source checkout after following
[Installation](INSTALLATION.md). They use the default private installation at
`~/Library/Application Support/Eidolon/`. For another installation, keep the same
`EIDOLON_HOME` or global `--config /private/path/config.yaml` selection on every
command, including stop and status. Runtime state belongs outside Git checkouts.

## Choose how the application service runs

| Mode | Start | Lifetime and ownership |
| --- | --- | --- |
| Ordinary launch | `uv run eidolon` | Connects to an existing service or starts one, then opens the UI. Without an installed agent, the service is a detached process logging to `service.log`. It can run without an open terminal UI, but has no launchd restart or login-start guarantee. |
| Background only | `uv run eidolon service start` | Uses the same startup logic without opening the UI. If a service already answers, returns without restarting it. |
| Foreground service | `uv run eidolon service run` | Runs the service in the current terminal, without the UI. Useful for diagnostics; output goes to that terminal. Ctrl+C or SIGTERM requests shutdown. |
| macOS login agent | Install, then start as below | launchd runs the service in the signed-in user's GUI domain, starts it at login, and keeps it alive. The UI remains optional. |

Only one service can own a state directory. Ownership is locked before SQLite is
opened or interrupted jobs are recovered. Do not delete `service.lock` to bypass
an ownership error: the presence of the file alone does not mean a service is
running. Use status and the supported stop command for the selected installation.
Chat and scheduled generation share a lock, so active work can delay other work.

## Install and control the login agent

This requires macOS, a signed-in GUI user session, initialized private
configuration, and a working Python environment with Eidolon installed. The
agent records the current Python interpreter and the selected configuration path.
Keep that environment and configuration available after logout and login. It
does not run `uv sync`, start a model server, or provision optional integrations.

```sh
uv run eidolon service install
uv run eidolon service start
uv run eidolon service status
uv run eidolon doctor
```

`setup --install-service` also writes the application agent during setup. Neither
installation command bootstraps or restarts it immediately. `service start`
bootstraps/kickstarts an installed agent when the selected service is stopped,
then waits up to about ten seconds for its socket to answer. If the service
already answers, start leaves it running. After installing over an existing
detached process, stop it and start again to use launchd supervision now.

The agent is `local.eidolon.service`, stored at
`~/Library/LaunchAgents/local.eidolon.service.plist`. Its `RunAtLoad` and
`KeepAlive` settings request login startup and restart after exit; launchd
throttles restarts by ten seconds. This is a per-user login agent, so do not rely
on it to run while logged out, while the Mac is asleep, or while required
provider services are unavailable.

The fixed agent label and plist are shared by installations for the same macOS
user. Installing from another state selection replaces that file. Start/stop can
also act on that fixed label even when a different configuration is selected.
Use one designated installation for the login agent. For isolated work, account
for the existing agent before starting; a shell's `EIDOLON_HOME` does not retarget
an already installed agent.

| Operation | Effect |
| --- | --- |
| `uv run eidolon service status` | Requests JSON status from the selected installation's socket, or reports stopped. It does not start the service or prove that launchd owns it. |
| `uv run eidolon doctor` | Requests integration diagnostics from a running service; with the service stopped, performs a smaller standalone check. See [doctor's limits](INSTALLATION.md#check-the-configuration). |
| `uv run eidolon service stop` | Boots out the application agent if its plist exists, then requests shutdown through the selected socket. Leaves its plist, history, job definitions, run history, and separate memory service intact. A later login can load the retained agent again; starting Eidolon can also start it again. |
| `uv run eidolon service uninstall` | Stops as above, then removes the application plist. Preserves private application state and the separate memory runtime. |

For a restart, close the UI, stop the service, check status until it reports
stopped, then start again. Shutdown and launchd unloading can take time; the stop
command requests shutdown rather than waiting for every process to exit. A rapid
stop/start can race unloading. Stop can interrupt in-progress generation; saved
job recovery is described below.

For read-only launchd inspection:

```sh
launchctl print "gui/$(id -u)/local.eidolon.service"
```

This complements socket status: launchd can have an agent loaded even when its
application process cannot start. Inspect `service.log` in the private state
directory for startup errors. Reinstall after moving or recreating the Python
environment, then perform a stop/start to use the new interpreter path.

An installed application agent supplies a small explicit environment (including
a standard executable PATH); it does not copy the interactive shell environment.
Prefer the [credential-reference setup](INSTALLATION.md#configuration-state-and-credentials)
for credentials. An `env:NAME` reference must resolve in the service process;
exporting it in a UI terminal does not update an already running service or an
installed agent. Keep any required custom agent environment private. Reinstalling
with `service install` rewrites the plist and its environment.

## Create and manage scheduled jobs

Fresh setup creates an empty `jobs.json`; nothing is scheduled automatically.
There is no general job-creation CLI subcommand. Open `/jobs` or Alt+7 in the UI:

1. Press Ctrl+N for a new job. Enter a name and a prompt, for example
   **Evening note** and **Write a short planning checklist for tomorrow.**
2. Choose a preset or enter a five-field cron expression. The default is
   `0 22 * * *` (daily at 22:00); `30 8 * * 1-5` means weekdays at 08:30.
   Fields are minute, hour, day of month, month, and day of week.
3. Check the IANA time zone, such as `Europe/Paris` or `UTC`. A new UI job uses
   the application's configured zone. Each saved job keeps its own zone.
4. Review **Enabled** and **Run in agent mode**. New UI jobs are enabled by
   default; turn Enabled off before saving if you only want to prepare a draft.
   Agent mode starts off.
5. Expand **Advanced** if needed, then press Ctrl+S to save. An enabled job can
   become eligible on the next scheduler check, including a recent missed time.

Advanced fields select a configured provider (default: current chat provider),
comma-separated allowed and required tool names, and the grace period in seconds.
The default grace is 21,600 seconds (six hours); accepted values are 0 through
604,800 (seven days). An explicit provider is a saved profile name, not a model
ID. Configure its model and capabilities in [Providers](PROVIDERS.md).

Companion mode uses the permitted information tools. An empty allowed-tool list
grants no tools in this mode. Agent mode permits file editing and shell commands,
requires tool calling enabled on the chosen provider, and runs without interactive
approval prompts. With no explicit allowed tools, agent mode uses its default
agent tools plus `read_skill`. Use explicit tool lists when you need a narrower
task. Required tools must also be allowed; unavailable or unsuccessfully used
requirements fail the run. See [Tools and MCP](INTEGRATIONS.md) for permissions.

| Jobs control | Action |
| --- | --- |
| Enter on a job | Edit its saved definition. |
| Ctrl+T | Pause or enable the selected schedule. |
| Ctrl+D | Open deletion confirmation; confirm to remove the schedule. Run history stays. |
| Ctrl+R | Open recent runs (up to the latest 100 across jobs). |
| Enter on a run | Inspect its result or error, scheduled time, and status. |
| Ctrl+Y on a failed run | Queue the same run for retry. Its job must still exist and be enabled. |
| Ctrl+F / Ctrl+L | Filter jobs / reload saved data. Reloading an existing editor replaces its unsaved values on success. |
| Esc | Go back; editor drafts survive closing/reopening the overlay during this UI session. Save to persist them across UI exit. |

Save/toggle/delete detect stale job edits and competing file updates. On an
error, keep the draft, reload the saved definition, and reconcile before saving.
Pausing or deleting a schedule does not provide cancellation of a run already
executing. There is no general **run now** action; retry applies to failed runs.

## Execution, persistence, and failures

The service checks background work on a roughly ten-second loop; checks can take
longer when integrations are slow or generation is busy. Jobs run only with the
service active. launchd does not wake a sleeping Mac for these Python schedules.
The displayed next time is a schedule prediction, not a promise of exact start.

On startup or wake, a new job considers only its most recent scheduled occurrence.
For an existing job, persisted cursors identify missed occurrences: normally only
the latest within its grace period is queued and older occurrences are skipped.
Outside grace, even the latest is skipped. Thus enabling a daily 22:00 job at
23:00 can run that evening's occurrence immediately. A backlog of more than
10,000 occurrences raises an error for schedule review rather than replaying it
all. The persisted cursor advances even when occurrences are skipped.

Definitions live in the private configured `jobs_file` (normally `jobs.json`).
SQLite stores cursors, stable occurrence/run IDs, statuses, isolated job sessions,
results, and errors. Results live in run history, rather than exported Markdown
folders or the active chat day. Keep both job definitions and SQLite in a
consistent backup. Prefer the UI for edits; malformed JSON, invalid cron/zones,
duplicate IDs, or invalid fields can prevent startup or interrupt maintenance.
The underlying job-file model defaults `enabled` to false if omitted; the UI's
new-job default is intentionally enabled.

Runs move through `queued`, `running`, and `completed`, or end as `failed` or
`skipped`. A failed run remains failed until retried; later scheduled occurrences
can still run. Fix the reported cause (provider, memory, required tool, or task
error), then use Ctrl+Y. Retry preserves the occurrence ID and session without
changing the cron schedule, and uses the current job definition when it executes.

On service restart, interrupted running jobs return to the queue. If a result or
completed assistant exchange was saved, recovery reuses it without another model
generation. Otherwise generation can be attempted again. This does not guarantee
exactly-once external tool side effects: inspect what a task already changed
before retrying work that edits files, runs commands, or calls external tools.
Runs do not overlap for the same job; model generation is also serialized with
chat. Participant names are captured when execution begins.

## Existing managed-memory runtime

This applies only to an already configured private installation with
`memory-service/settings.json`, a separately installed official Hindsight API and
pg0 Python runtime, working credential references, and preserved PostgreSQL
storage plus instance metadata. Ordinary setup, `service install`, and
`service start` do not create those prerequisites, install Hindsight, or create
a bank. For selecting an existing endpoint/bank and enabling recall or saving,
follow [Optional memory](MEMORY.md).

The separate `local.eidolon.hindsight` LaunchAgent runs
`eidolon.memory.runtime --settings` with its private settings path. Those settings
select `runtime_python`, `api_binary`, `api_port`, database instance/data
directory/port/user/database, `password_ref`, and optional `credential_refs` and
`environment`. They belong to the preserved installation; this is not a template
for provisioning a fresh service.

The supervisor resolves credential references and connects the API through an
explicit local PostgreSQL TCP URL. It enables the API worker and disables schema
migrations on startup. The pg0 manager verifies the existing instance's data
directory and port before reuse/start. It requires `PG_VERSION` inside the data
directory and `instance.json` in its parent. Missing or mismatched storage/instance
identity causes failure rather than creation of replacement storage. Preserve
existing bank names and instance identity through migration.

The supervisor checks every ten seconds, allows a two-minute API startup grace,
restarts after any API exit, and replaces an unresponsive API after three failed
liveness probes following that grace. Database verification/readiness failures
alone do not tear down an API still serving requests. Manager and API launch
failures are retried. Stopping the supervisor stops its API but leaves PostgreSQL
running, including storage used by other clients.

For an already installed managed-memory agent, these are inspection and deliberate
control commands (the path assumes the standard existing plist location):

```sh
launchctl print "gui/$(id -u)/local.eidolon.hindsight"
launchctl kickstart -k "gui/$(id -u)/local.eidolon.hindsight"
launchctl bootout "gui/$(id -u)/local.eidolon.hindsight"
```

`kickstart -k` forces a supervisor restart. `bootout` unloads it for the current
login session; its retained plist can load again at login. Before a deliberate
stop, stop the application service too: its automatic local recovery can otherwise
restore the agent. Do not launch the app or run memory diagnostics that invoke
recovery during that maintenance window. `service uninstall` removes only the
application agent; there is no memory uninstall subcommand or database removal
operation. Keep the existing memory plist, settings, and storage for recovery.

Application recovery is limited to macOS and a plain local HTTP origin whose
host/port matches the saved settings. It does not manage arbitrary remote URLs.
It serializes recovery, throttles attempts to at least 15 seconds apart, and
bootstraps/kickstarts without forcing an active supervisor restart. A cold API can
take longer than the bounded recovery wait; later checks can retry. See
[memory failure and delivery behavior](MEMORY_REFERENCE.md) for safe-read retry
and uncertain-write handling.

## Diagnostics and recovery

| Symptom | Inspection and supported next step |
| --- | --- |
| Service stopped or startup timed out | Check the selected config/state, `service.log`, and launchd inspection. Verify the recorded interpreter still exists. Fix the cause and start again; allow unloading to finish after a stop. |
| State-directory ownership error | Find the service using that state; stop it through its original configuration. Do not remove the lock or start a second owner. |
| launchd agent loaded but socket unavailable | Inspect the plist's config/interpreter paths and `service.log`. A loaded job does not establish successful app startup. |
| Credential works in a shell but fails in the service | Check its credential reference and the service environment. Use private Keychain references or supply the required environment to the actual service process, then restart. |
| Job missed, queued, or skipped | Check Enabled, the job's time zone/cron/grace, service uptime, active generation, and run details. Sleep, logout, downtime, and generation contention delay checks. |
| Failed run | Read its error in `/jobs`; fix provider/integration/tool configuration and retry the failed run. A skipped run has no retry control. |
| Background work needs attention | Inspect service status's `error` and `service.log`; validate the jobs file and integrations. Maintenance retries on later checks after the cause is corrected. |
| Managed memory unavailable | Inspect private `memory-service/status.json`, `supervisor.log`, `supervisor-error.log`, and `api.log`; verify the separate interpreter/API binary and credentials. Distinguish API liveness from bank/readiness or schema problems. |
| Preserved database verification failed | Verify original storage, instance metadata, port, and credentials. Restore verified state using a separate recovery procedure; do not create an empty replacement or change identity to bypass checks. |

Status, logs, prompts, job results, settings, exports, and migration reports can
contain private data. Review them before sharing. Socket status includes a `busy`
flag and background `error`; it is not a full process inventory. The Unix socket
uses a short private temporary directory derived from the state path, rather
than residing next to the database.

For an installation move or rebrand, use the explicit `migrate-state` workflow
rather than changing `EIDOLON_HOME` and assuming files moved. A failed operation
preserves the source; inspect the destination's private `migration/rebrand.json`
and preserved agents before starting either installation. An incomplete published
copy is blocked from ordinary use. Do not delete the report to bypass that guard,
merge copies, overwrite newer history, or activate both schedulers. Follow
[State migration](STATE_MIGRATION.md) for the manifest, verification, service
handoff, and original-state recovery procedure, and
[Backup and recovery](RECOVERY.md) for snapshots and rollback limits.

Imported external-profile schedules remain disabled until an explicit verified
handoff. `migrate-jobs` is separate from `migrate-state`: it requires the original
private import manifest, timezone evidence, an installed application agent, and
isolated scheduled-job verification. Its journal disables selected originals
before enabling replacements and retains backups. After an interrupted handoff,
review the private checkpoint and resume with the same verification inputs; do
not manually enable both copies or overwrite changed schedules. See
[the handoff contract](../src/eidolon/scheduler/migration.py) and
`uv run eidolon migrate-jobs --help`.

Before backend/schema changes, preserve consistent application and database
backups and dependency versions. Test restoration in separate storage, retain
saved operation identities, and avoid replaying retained history under new IDs.
The [memory recovery guidance](MEMORY_REFERENCE.md#compatibility-and-synthetic-verification)
explains those limits. Restarting the preserved runtime does not upgrade or
restore its schema.

## Verification scope

The commands and lifecycle descriptions were checked against the
[CLI](../src/eidolon/cli.py), [LaunchAgent implementation](../src/eidolon/service/launchd.py),
[service runtime](../src/eidolon/service/runtime.py),
[scheduler](../src/eidolon/scheduler/jobs.py), and controlled tests. Tests use
temporary state, fake providers, and mocked launchctl/process runners, including
activation/removal, interrupted jobs, missed-time handling, and memory watchdog
recovery. They do not establish live launchd login/restart behavior, Keychain
availability, external API health, model quality, or backend schema compatibility.
No live agents or private services were installed, bootstrapped, replaced, or
stopped for this documentation work.
