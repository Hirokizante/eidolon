# State migration

[Back to Eidolon](../README.md)

Use `migrate-state` to copy an existing installation into a new private state
directory. It preserves the source for recovery and verifies the copy before
replacing explicitly identified source services. It is an installation handoff,
not a backend schema upgrade or a general backup command. Keep an independent
[backup](RECOVERY.md) and review the [privacy boundaries](PRIVACY.md) first.

All paths, source service labels, credential names, and substitution inputs below
are synthetic. Replace them privately with an inventory of your own installation;
do not publish that inventory or manifest. Run `uv run` commands from the source
checkout with synced dependencies. No real Keychain transfer, launchd replacement,
or live installation migration was performed for this documentation overhaul.

## Choose the correct workflow

| Command | Purpose and prerequisite |
| --- | --- |
| `migrate --source ...` | One-time import from an external profile into an already initialized Eidolon state. Reads selected profile settings, `.env`, memory/profile files, and schedules; it does not copy an Eidolon SQLite installation. |
| `migrate-jobs --activate --verification ...` | Separate, journaled transfer of imported schedules after isolated scheduled-job verification. Requires import provenance, timezone evidence, and an installed application agent. See [operations](OPERATIONS.md#diagnostics-and-recovery). |
| `--config ... migrate-state --manifest ...` | Copy an installation from the manifest into an absent destination, validate it, transfer explicit credentials, and replace listed agents. Destination setup must **not** run first. |

Changing `EIDOLON_HOME`, display names, or a configuration path does not perform
either import or installation migration.

## External-profile import

After configuring a destination installation, an explicit profile import is:

```sh
uv run eidolon --config /private/tmp/eidolon-import-example/config.yaml migrate \
  --source /private/tmp/example-external-profile \
  --without-credentials
```

The importer reads `config.yaml`, `.env`, `memories/USER.md`,
`memories/MEMORY.md`, and `cron/jobs.json` when present. Private review copies and
the import report go under `migration/`; an existing `migration/manifest.json`
blocks another import. Authored `USER.md` is preserved; an empty or exact bundled
starter profile can be replaced. Supplemental memory notes are review material,
not automatically retained runtime instructions. Source files remain intact.

Omit `--without-credentials` only when credential import is intended. The
importer selects the provider's endpoint-specific source key with its supported
fallback and can import search credentials. An avatar signing credential requires
an explicitly selected field, for example
`--avatar-secret-env EXAMPLE_AVATAR_SIGNING_KEY`; there is no inferred field.
Selecting that field also redacts its value in copied material when credential
import is skipped. This targeted redaction is not a guarantee that arbitrary
source files contain no other secrets.

Imported provider capabilities start unverified, imported jobs remain disabled,
and memory retention is disabled pending verification. The existing bank is
preserved. Inspect private copies and configure capabilities before use; schedule
activation is the separate `migrate-jobs` workflow. This importer has no installation
handoff rollback transaction: back up the destination before importing.

## Prepare the installation copy

1. Inventory the source configuration, state directory, prompt paths, jobs file,
   credentials, memory settings, and any application/memory LaunchAgents. Record
   the source runtime/dependency versions and keep them available for recovery.
2. Choose disjoint private source and destination directories: neither may contain
   the other. The destination is the parent of the global `--config` argument and
   must not exist, even as an empty directory. Keep the manifest and both locations
   outside all Git checkouts/worktrees. Prefer durable private storage for an
   actual handoff; `/private/tmp` here is only an example.
3. The source directory must contain the selected configuration filename, and
   its configured `state_dir` must resolve to that directory. Arrange for all
   configured prompts and the configured jobs file to live inside source state
   before migrating. Resolve any symlink entries explicitly: the migrator rejects
   symlinks anywhere within the source tree, including its lock, rather than
   following them. Other unsupported special files are rejected; sockets are skipped.
4. Stop UI clients and prevent concurrent edits, job execution, or other writers.
   Closing the UI alone leaves the app service running. List every source agent
   that needs handing off; the migrator stops only those explicit labels. If the
   manifest has no service entries, you must stop the source app yourself using
   its original runtime/service controls before running the command. With entries,
   also stop any unlisted process that can write source state. Do not remove locks
   or let login/restart restart the source while copying.
5. Back up the stopped source and any separately hosted memory database. Check
   destination agent conflicts before handoff: existing destination plists are
   refused. A service replacement can fail after the copy has been published.

## Manifest and command

Create a private JSON file, for example
`/private/tmp/eidolon-transfer-example/installation-manifest.json`, with restricted
permissions. This complete example shows all supported fields:

```json
{
  "source": "/private/tmp/example-companion-source",
  "configuration": "config.yaml",
  "source_keychain_service": "example.previous-companion",
  "config_values": {
    "example-retired-theme": "nord",
    "example-old-avatar-route": "avatar"
  },
  "credential_names": ["example-extra-key"],
  "services": [
    {
      "kind": "app",
      "label": "local.example.previous-app",
      "plist": "/private/tmp/eidolon-transfer-example/source-app.plist"
    },
    {
      "kind": "memory",
      "label": "local.example.previous-memory",
      "plist": "/private/tmp/eidolon-transfer-example/source-memory.plist"
    }
  ]
}
```

| Field | Contract |
| --- | --- |
| `source` | Required explicit installation directory. |
| `configuration` | A filename within source state, default `config.yaml`; not a path to another directory. Use a YAML configuration for this copy workflow. |
| `source_keychain_service` | Required if any Keychain entries will be transferred; must differ from destination service `eidolon`. |
| `config_values` | Optional exact string-value substitutions; default `{}`. Include only values that actually need replacing. |
| `credential_names` | Optional additional Keychain entry names; default `[]`. Config/settings references are discovered automatically. Every named source entry must exist. |
| `services` | Optional list, default `[]`, at most one `app` and one `memory`. Each private plist must exist and its `Label` must match the explicit label. Source labels cannot equal the fixed Eidolon destination labels. Memory replacement requires copied `memory-service/settings.json`. |

For a copy without agent replacement, use `"services": []` and stop the source
app first. Omit unneeded credential fields if there are no Keychain references
or additional credentials. These are explicit inputs, not discovery hints.

Run the command **without initializing or creating the destination directory**:

```sh
uv run eidolon --config /private/tmp/example-companion-destination/config.yaml \
  migrate-state \
  --manifest /private/tmp/eidolon-transfer-example/installation-manifest.json
```

The global `--config` precedes `migrate-state`. To request immediate replacement
agent activation after verification, append `--activate-services` to this command.
That flag requires service entries. Without it, supplied source agents are still
stopped and replaced, and replacement plists are written without bootstrapping.
Their normal launchd login behavior still applies; this is not a permanent
disabled-services mode. No service entries means no agent installation or
activation. After a successful handoff, deliberately start the chosen replacement
using [operations](OPERATIONS.md); keep the source stopped.

## Credentials and substitutions

The migrator collects `keyring:` references from rewritten configuration and
managed-memory settings plus `credential_names`. It reads each from the explicit
source Keychain service and verifies the value under `eidolon` with the same entry
name. An equal existing destination value is accepted. A different value is never
overwritten; migration fails. Resolve that conflict privately after checking
which installation needs each entry. These Keychain entries are shared by the
macOS user, not isolated by `EIDOLON_HOME`. Source entries are retained.

`env:` references do not transfer shell environment values. Private source agent
environment strings are preserved in replacement agents, subject to the same
configuration substitutions and source-path remapping. Review them for obsolete
launch environment choices. Credential lookup or verification can fail even when
the source files copy correctly.

Paths equal to the resolved source directory or beginning with that directory
plus `/` are remapped in configuration/settings to destination paths. Substitutions
in `config_values` match **whole string values**, recursively, not substrings or
dictionary keys; exact substitutions take precedence over path remapping. They
also apply to copied agent environment settings. This is not a general search and
replace across files. Personal prompts, transcripts, job prompt contents, queued
payloads, and persisted external identifiers are not rewritten by these rules.

## Verification, preservation, and service handoff

After stopping supplied agents, migration takes the application's nonblocking
exclusive source `service.lock` before copying. If a source app still owns it,
the operation fails. It rechecks source configuration and symlinks under the
lock. The lock does not coordinate arbitrary external file editors or PostgreSQL
clients, so stopping those writers remains part of preparation.

Copying uses a private staging directory beside the destination. SQLite files
are detected by their header and copied through SQLite's backup API, including
committed WAL contents. Loose `-wal`/`-shm` files and `service.lock` are not copied.
Each database gets integrity, foreign-key, and logical dump-digest checks.
Configuration is validated; configured prompt files must exist in staging and
the internal jobs file is loaded/validated. Credential transfer is verified
before the staging directory is renamed to the destination.

The copy preserves conversations, configured identity and timezone, timestamps
in stored records, custom prompt bytes, memory configuration, pending outbox
payloads/document IDs/operation UUIDs, saved maintenance task IDs and evidence,
and scheduler state. File modification times are not a preservation guarantee.
Pending maintenance resumes using saved identities; completed evidence is not
recreated just because the product namespace changed. An older combined memory
switch supplies saving's value when `save_enabled` was absent.

Managed memory's original `database` settings object is retained unchanged so its
PostgreSQL instance identity and storage remain attached in place. The operation
does not dump, relocate, rename, or upgrade that external database or bank. Keep
the preserved storage available even if its path lies under source state; do not
delete the source tree after a successful copy. Private environment values from
listed agents survive in their replacements. No guarantee is made about external
tools whose own files or identifiers still point to the source.

For supplied services, migration retires each source plist to the same path with
suffix `.plist.pre-eidolon` and refuses an existing backup at that name. Replacement
agents use `local.eidolon.service` and `local.eidolon.hindsight` under
`~/Library/LaunchAgents/`. Memory is installed before the application; activation
is requested only with `--activate-services`. The new application agent records
the current interpreter and destination configuration. Review readiness through
[service status and diagnostics](OPERATIONS.md), not just a successful bootstrap.

Success prints a generic completion line plus a report; the private copy is
`migration/rebrand.json` with status `verified`. Report fields include credential
names and whether service activation was requested. They do not prove endpoint
health or semantic memory fidelity. Inspect identity, prompt bytes, conversations,
job state, pending work and managed storage before ordinary use. Avoid synthetic
retention probes on a live bank.

## Failure and original-state recovery

Existing state is never silently merged or overwritten. Before publication,
failed staging copies are removed. Newly added destination credentials are
deleted on failure; equal preexisting entries and source entries are retained.
Rollback attempts to unload/remove replacement agents, restore retired plists,
and restart source agents that were running before handoff. These are best-effort
operations and can themselves fail, including credential deletion.

If publication already happened, the destination remains for private inspection
with report status `failed; inspect before retrying`. Ordinary configuration
loading refuses missing/invalid verification status in that report. **Do not edit
or remove the report to force startup.** A generic CLI failure deliberately omits
private exception text; inspect preserved state and agent files privately.

Before starting either installation, establish which processes and agents still
own work. If a replacement cannot be unloaded, recovery intentionally leaves the
source stopped to prevent concurrent schedulers. Inspect the replacement's loaded
state and preserved `.plist.pre-eidolon` files, resolve the unload problem, and
restore the source agent only when it is safe. Unlisted source processes and
agents that were already stopped are not automatically restarted. A source with
no supplied services needs manual restart through its original controls.

For another attempt, keep the incomplete copy for inspection outside the new
destination and choose a fresh absent destination; review the manifest paths and
any remaining credentials/agents first. Do not merge it into the source or run
both copies. Once either installation has accepted new conversations or external
work, returning to the other is a separate recovery decision: external writes
and post-copy history are not undone by restoring agent files. Follow
[recovery and rollback limits](RECOVERY.md#restore-and-rollback-limits).

## Verification scope

The CLI and manifest above were checked against
[the CLI](../src/eidolon/cli.py), [installation migration](../src/eidolon/rebrand.py),
[external-profile import](../src/eidolon/migration.py), and controlled
[installation](../tests/test_rebrand.py), [profile](../tests/test_migration.py),
and [schedule handoff](../tests/test_schedule_migration.py) tests. They exercise
synthetic state, fake credentials and service runners, committed WAL data,
overlap/symlink/active-source rejection, credential conflicts, preserved identities,
prepublication failures, failed handoff recovery, and incomplete-copy startup refusal.
Real Keychain transfer, launchd replacement, and live installation migration have
not been performed by this overhaul. Backend upgrades and live restore validation
remain separate deliberate operations.
