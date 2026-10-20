# Privacy and private state

[Back to Eidolon](../README.md)

Eidolon keeps local conversation history and editable configuration on your Mac.
It sends data to the model endpoints and integrations you configure and use.
Local storage does not make a configured remote model or tool private, and
Eidolon does not control those services' storage, retention, or training policies.

## What to keep private

The default state directory is `~/Library/Application Support/Eidolon/`.
`EIDOLON_HOME` selects another private installation; global
`--config /private/path/config.yaml` selects a configuration file. Neither option
moves existing files. Keep runtime state, backups, exports, diagnostic captures,
and isolated state overrides outside Git checkouts and worktrees. Configuration
and state validation reject locations beneath a `.git` directory or file.

| Material | Location or handling |
| --- | --- |
| Configuration and identity | Private `config.yaml`, including participant names, provider endpoints, integration settings, permitted folders, and credential references. The repository's `config.example.yaml` is a public template. |
| Authored instructions | Private `prompts/` holds `SYSTEM.md`, `SOUL.md`, `USER.md`, and `POST_HISTORY.md`; installed skills and their supporting files also belong in private state. Only generic bundled defaults belong in Git. |
| Conversations and work | `room.sqlite3` stores messages, generation metadata, exact model request bodies, request metrics, saved identities, summaries, pending memory payloads, maintenance tasks, and scheduled-run state. SQLite may also have `-wal` and `-shm` files. |
| Schedules | `jobs.json` normally holds job definitions and prompts; configuration can select another private jobs file. Results and errors may contain conversation or tool output. |
| Diagnostics and exports | `service.log`, `diagnostics/`, request exports, presentation-failure captures, and any copied output can contain prompts, conversations, images, paths, or identifiers. Request export writes `diagnostics/requests/latest.json`. |
| Migration and backups | `migration/` reports, source copies, schedule journals, SQLite snapshots under `backups/`, and memory database dumps remain private. Reports may contain credential names even when they omit values. |
| Managed memory | Private `memory-service/settings.json`, status and logs describe the separate runtime. PostgreSQL data can live outside application state; copying the app directory does not back up that live database. |

The application creates private configuration and prompt files with mode `0600`
and private state/prompt directories with mode `0700`. Installation migration
also restricts copied files. These permissions are not encryption, an access
boundary against your own processes, or a guarantee about every file produced by
an external integration. Include private state in your own backup/access policy.
See [backup and recovery](RECOVERY.md) before copying a database.

## Credential references

Secret fields accept references such as `keyring:example-model-key` or
`env:EXAMPLE_MODEL_KEY`, rather than literal keys. Eidolon's Keychain service name
is `eidolon`; the part after `keyring:` is the entry name. Set a credential through
the hidden-input prompt:

```sh
uv run eidolon secret set example-model-key
```

Do not put its value in the command line, example configuration, issue text, or
Git. Ordinary nonsecret headers can be literal strings; sensitive headers require
references. Provider endpoints reject embedded username/password credentials.
Keep URLs free of secret query parameters too.

`env:` resolves in the process actually doing the work. An already running
service does not inherit changes in a new shell, and a login agent has its own
environment. Successful Keychain lookups are cached in process memory; secret
commands notify a reachable service to clear the affected cache. A state backup
does not itself export Keychain entries or shell environment values. Plan their
recovery separately; an installation migration uses the explicit transfer rules
in [State migration](STATE_MIGRATION.md#credentials-and-substitutions).

## Where data goes

| Destination | Data and boundary |
| --- | --- |
| Selected dialogue provider | Requests include the configured prompt instructions and names, selected skills, relevant history, current input, recalled/local continuity context, available tool schemas, and tool results. Attached images go to a vision-capable dialogue model or the configured separate vision model. Authentication resolves at the provider connection. |
| Auxiliary provider | When enabled, background continuity work sends selected source exchanges and task instructions to its resolved provider/model. It may inherit dialogue targeting or use a separate connection. |
| Hindsight endpoint and bank | Recall/reflect sends query text and options; returned records can enter model context. Eligible saving sends completed clean exchanges with speaker labels, timestamps, document IDs and saved operation IDs through the durable outbox. Prompt snapshots and cosmetic cues are excluded from that exchange payload. The backend may use its own model providers. New installs have recall and saving off and no bank selected. |
| Search and fetched websites | Web search sends the tool's query and filters to the selected Tavily, Brave, Exa, Perplexity, or SearXNG service. Page fetches contact the requested site; returned text can be sent to the model. |
| MCP servers | Eidolon connects configured stdio processes or Streamable HTTP endpoints for discovery. Enabled tools receive their arguments and return results that can enter model context. Stdio servers run locally with process permissions; remote servers have their own data handling. |
| Avatar bridge | When enabled, the bridge receives catalog/tool calls and signed lifecycle events with session/turn identifiers and configured external profile information. Signing authenticates the lifecycle payload; it does not establish the bridge's storage policy. |
| Local file and command tools | Companion-mode reads are bounded by `allowed_roots` and symlink checks. Agent mode permits broader local reads, writes, and shell commands under your OS permissions, including following symlinks; `allowed_roots` does not restrict it. Tool results may enter provider requests. Scheduled agent work requires the job's own saved agent flag and tool choices. |

See [providers](PROVIDERS.md), [auxiliary inference](AUXILIARY_MODEL.md),
[memory controls](MEMORY.md), [tools and permissions](INTEGRATIONS.md), and
[avatar setup](AVATAR.md) for activation and configuration. Disabling a connection
or saving does not retract requests already sent, erase external records, or
necessarily cancel accepted work. Pending memory delivery has separate pause and
review controls. Removing an application service preserves local state.

## Sharing diagnostics

Start with the command, observed behavior, app/dependency versions, and a
synthetic reproduction. Review a copy of any diagnostic material before sharing.
Remove credential values, authorization headers, cookies, private URLs/query
parameters, local paths, participant names, bank/profile IDs, prompt contents,
conversation text, images, job prompts/results, and external record identifiers.
Keep originals privately when needed for recovery.

`/inspect` and request exports show the actual model request body. They can expose
all prompt and conversation content in that request even when authentication
headers are absent. Logs, exceptions from integrations, `doctor`, service status,
and migration reports are not safe-to-publish bundles. `doctor` performs network
checks and may invoke managed-memory recovery; it is not an offline redaction
tool. The migration failure CLI deliberately emits a generic recovery message,
but that does not make other diagnostic output anonymous.

## Repository privacy checks and history

Install hooks after dependency setup:

```sh
uv run pre-commit install --install-hooks
uv run pre-commit run --all-files --hook-stage manual
```

The [hook configuration](../.pre-commit-config.yaml) runs two kinds of checks:

- The [private-file guard](../scripts/check_private_files.py) checks paths against
  ignore rules, including force-added ignored files. The history variant walks
  reachable commit trees and direct tree snapshot refs, retaining historical
  private-path rules and explicit public-default exceptions. It prints paths,
  not file contents.
- Gitleaks scans for recognizable secrets; its history hook scans all refs with
  redacted findings. Commit checks and push/history checks run at their configured
  stages; the manual command above invokes the history checks too.

These checks do not understand all personal prose or detect every secret. Review
the diff and intended files as well. `.gitignore` is not a sanitizer or an access
control. Deleting a file does not remove prior commits, snapshot refs,
clones, or copies held elsewhere. If private material was
committed, stop distributing it, rotate exposed credentials as appropriate, and
plan any history remediation separately; deleting the current file is insufficient.

The storage and destination descriptions above were checked against
[configuration](../src/eidolon/config.py), the
[conversation engine](../src/eidolon/engine/conversation.py),
[tool registry](../src/eidolon/tools/registry.py),
[service request export](../src/eidolon/service/runtime.py), and
[privacy guard tests](../tests/test_repository_privacy.py).
