# Backup and recovery

[Back to Eidolon](../README.md)

Back up the private application state and any external memory database separately.
A copy of configuration alone cannot restore conversations, pending work, or
external memories. An application-state copy does not export Keychain credentials
or snapshot a running PostgreSQL instance. Keep backups, verification evidence,
and all restore locations outside Git; they contain private data.

## Capture a recoverable backup

1. Record the application revision, dependency lockfile/runtime versions, selected
   configuration path, and state directory. Record the managed-memory runtime,
   database version/instance identity, bank identity, storage paths, and credential
   references privately. Keep a known working runtime available.
2. Close the UI and [stop the application service](OPERATIONS.md#install-and-control-the-login-agent)
   through its selected configuration. Wait for shutdown; closing the UI alone
   leaves the service and scheduler active. Account for login agents and other
   writers so they cannot restart during the backup. Do not remove `service.lock`.
3. Preserve the stopped state directory: configuration, exact prompt/skill files,
   jobs, SQLite, pending work, private migration manifests/journals, memory runtime
   settings and relevant logs. Include configured files outside that directory
   explicitly. Keep original WAL/SHM files with a raw database copy until a
   verified SQLite backup has been made; copying only a live `.sqlite3` can lose
   committed WAL contents. Do not copy a running PostgreSQL data directory as if
   it were an independent consistent backup.
4. Capture a SQLite backup through its backup API and check it, as below. Keep
   credential recovery separate: record entry names and required environment
   variables without printing values. Preserve private LaunchAgent configuration
   and any external integration settings needed to reconstruct the service.
5. Verify restoration into new isolated storage before relying on the backup.
   A checksum detects later file changes; it does not demonstrate restore success.

This standalone SQLite example uses synthetic paths. The source database must
already exist and the source app must be stopped. Choose a fresh private backup
directory; its exclusive creation prevents overwriting an earlier backup. Replace
the source path privately when following it for your own installation:

```python
import os
import sqlite3
from pathlib import Path

source = Path("/private/tmp/example-companion-source/room.sqlite3")
backup_dir = Path("/private/tmp/example-companion-backup")
if not source.is_file():
    raise FileNotFoundError("Expected an existing source database")
backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
target = backup_dir / "room.sqlite3"
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
    with sqlite3.connect(target) as copied:
        original.backup(copied)
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not copied.execute("PRAGMA foreign_key_check").fetchall()
        assert list(original.iterdump()) == list(copied.iterdump())
print("SQLite backup verified; configuration and external memory need separate backups.")
```

Treat any partial output from a failed backup as unverified. This example does
not obtain the application lock or back up other files. Stop the writers first;
SQLite's consistent database snapshot alone does not make concurrently edited
configuration and jobs part of the same snapshot.

## Automatic SQLite snapshots

The current [Store](../src/eidolon/storage/store.py) accepts schema versions up to
11 and refuses unknown newer versions. Before upgrading an existing nonempty
older database, it takes a SQLite backup including committed WAL data. The normal
path is `backups/room.sqlite3.pre-v11-<UTC timestamp>.sqlite3` beside the database.
Snapshots have mode `0600` in a `0700` directory. A database under a Git checkout
uses a private temporary backup directory instead; that fallback does not make
in-checkout runtime state acceptable. An already current database or an empty
new database does not trigger this migration snapshot.

These snapshots cover SQLite before schema/policy mutations. They do not replace
a full application/credential/backend backup. The store preserves transcript and
saved operation identities through supported upgrades; old pending work with
unknown saving eligibility may be held for review. Inspect held work through
[memory delivery controls](MEMORY.md) instead of forcing it into the queue.

## External memory backup and isolated restoration

For a PostgreSQL-backed memory service, use version-compatible PostgreSQL tools
and its existing authentication. `pg_dump --format=custom` produces a consistent
database snapshot; it does not back up cluster-wide roles. Preserve required
globals separately, excluding role passwords from diagnostic exports, and retain
private service configuration and dependency versions. See the official
[PostgreSQL dump guidance](https://www.postgresql.org/docs/18/backup-dump.html)
for the supported procedure and consult the documentation matching your server.

Restore into a **new isolated database/data directory**, with a separate port and
short Unix socket directory. Create the target database from `template0`, prepare
required roles, and use `pg_restore --exit-on-error --single-transaction` with an
explicit isolated connection. Check the
[restore options](https://www.postgresql.org/docs/18/app-pgrestore.html) for your
version. Keep live database names, ports, sockets, and storage paths out of restore
commands. A different API port alone does not isolate the underlying database.

Record and compare bank configuration/identity, original source document and
memory identifiers, source records, and saved operation identities. Test recall
and readiness, then synthetic asynchronous retention using stable document IDs
and operation UUIDs only on the isolated service. Verify completion polling,
repeat-submission behavior and absence of duplicate documents. Retain private
evidence and stop verification services afterward. The
[synthetic memory harness](MEMORY_REFERENCE.md#compatibility-and-synthetic-verification)
can exercise a separately prepared local service; its isolation flag is an
operator assertion, not proof that its database is separate.

## Restore and rollback limits

Keep originals intact and restore into fresh private locations. For a compatible
installation move, [migrate-state](STATE_MIGRATION.md) validates and remaps a
stopped source copy; it refuses an existing destination. For another restore,
review absolute `state_dir`, prompt, jobs, runtime and database paths before
starting any app. Do not let a restored configuration still point to live files.
Keep schedulers, pending delivery and managed-memory recovery from reaching live
services during inspection. Merely setting `EIDOLON_HOME` does not retarget an
existing LaunchAgent or nested absolute paths. Existing fixed Eidolon agent labels
are shared per macOS user; do not install an isolated test agent over a live one.

Opening an older restored SQLite database with current code can upgrade that
copy. Check integrity and logical records first using SQLite directly; preserve
the original backup. Verify messages, prompt bytes, identity/timezone, jobs/run
history, outbox payloads and IDs, maintenance task/evidence IDs, and memory
storage identity before activation. Keep unchanged records' identifiers stable.
Do not re-ingest history under new IDs to make a restore appear complete.

To suspend a feature operationally, use current configuration controls while
keeping the current application/schema: pause pending delivery, disable saving
or recall, disable current-life automatic refresh/reply use, or disable auxiliary
inference as appropriate. See [memory](MEMORY.md),
[current-life notes](CURRENT_LIFE_SUMMARIES.md), and
[auxiliary rollback](AUXILIARY_MODEL.md). Do not run an older dispatcher that
ignores held/rejected eligibility against current state. A schema snapshot is
not a supported downgrade command, and older builds can refuse newer databases.

Backend schema rollback is separate from restarting or changing the memory API
interpreter. The managed runtime attaches to preserved storage with startup schema
migrations disabled; restarting it neither upgrades nor restores that schema.
Validate a candidate runtime and its revision-ledger/startup behavior against
an isolated restored database before a live switch, including any older clients
sharing the database. An older API becoming ready is insufficient evidence of
write compatibility. Keep dependency versions and schema evidence with the backup.

Restoring an older SQLite or PostgreSQL snapshot over current data can discard
new conversations and decisions, replay external work, or revive content that
was excluded or forgotten. Local rollback does not undo already accepted remote
writes. Retain both copies and reconcile post-backup work deliberately before
choosing an active installation. Never silently merge copies or enable both
schedulers. For an incomplete installation handoff, follow
[original-state recovery](STATE_MIGRATION.md#failure-and-original-state-recovery)
and keep its report guard intact.

## Verification scope

SQLite snapshots and identity preservation are covered by controlled
[storage](../tests/test_storage.py),
[continuity storage](../tests/test_continuity_storage.py),
[request schema](../tests/test_request_metrics.py), and
[installation migration](../tests/test_rebrand.py) tests, including WAL and failure
paths. Examples use synthetic inputs. The overhaul did not perform real Keychain
transfer, launchd replacement, a live installation migration, a live PostgreSQL
dump/restore, or backend schema rollback. Recovery instructions preserve durable
procedures from older reports without treating historical installation results
as verification of your current environment.
