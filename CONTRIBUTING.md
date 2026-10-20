# Contributing to Eidolon

Eidolon is a macOS terminal companion built with Python 3.12+ and Textual.
Start with a focused bug fix, test, documentation repair, or feature with a clear
expected behavior. Keep unrelated cleanup in separate changes so reviewers can
assess the behavior and its validation together.

## Issues and pull requests

Use [GitHub Issues](https://github.com/Hirokizante/eidolon/issues) and pull requests
for contribution planning and review. For a bug,
describe the command or interaction, expected and actual results, relevant
versions, and a small synthetic reproduction. For a larger change, describe the
problem and proposed behavior in an issue before implementing it. Review anything
you share for private content using the [privacy guide](docs/PRIVACY.md#sharing-diagnostics).

Keep specifications, acceptance criteria, decisions, and validation updates in
the relevant GitHub issue. Follow the [issue conventions](docs/agents/issue-tracker.md)
and [triage roles](docs/agents/triage-labels.md) when maintaining engineering work.

## Development setup

Use macOS for running the application and exercising Keychain, launchd, clipboard,
and other platform integrations. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and use Python 3.12 or newer. From a source checkout, run commands at the
repository root:

```sh
uv sync --locked --group dev
uv run pre-commit install --install-hooks
```

The lockfile supplies application and development dependencies. The `dev` group
includes pytest, pytest-asyncio, pytest-xdist, Ruff, pre-commit, and pip-audit. Hook
installation installs both commit and push hooks according to
[`.pre-commit-config.yaml`](.pre-commit-config.yaml). No formatter hook is configured.

Automated tests use synthetic configuration and fake integrations; they do not
require setting up a personal companion or supplying real provider credentials.
For manual application work, create a separate private development installation
outside all checkouts and worktrees. For example, in the shell used to run Eidolon:

```sh
export EIDOLON_HOME="$(mktemp -d /tmp/eidolon-dev.XXXXXX)"
uv run eidolon setup --interactive
uv run eidolon doctor
uv run eidolon
```

Use the [installation walkthrough](docs/INSTALLATION.md) to configure a working
provider and model. Plain `uv run eidolon setup` initializes configuration with a
placeholder model; it does not establish a working conversation. `doctor` checks
configuration and integrations and can contact configured services. Launching
Eidolon starts or connects to its background service; closing the UI leaves that
service running. See [service controls](docs/OPERATIONS.md#choose-how-the-application-service-runs)
before stopping or cleaning up a development installation. Keep the same
`EIDOLON_HOME` when issuing its service commands. A temporary directory is suitable
for disposable work, not durable personal state.

## Finding the code and guidance

| Path | Responsibility |
| --- | --- |
| `src/eidolon/cli.py`, `config.py`, `preferences.py` | CLI, configuration, and saved preferences |
| `src/eidolon/ui/`, `ui/app.tcss` | Textual screens, commands, forms, and layout |
| `src/eidolon/engine/` | Conversation orchestration and presentation |
| `src/eidolon/providers/`, `memory/` | Model requests, context, external memory, and continuity |
| `src/eidolon/tools/`, `mcp/`, `avatar/` | Built-in tools and optional external integrations |
| `src/eidolon/storage/`, `scheduler/`, `service/` | SQLite, scheduled jobs, background runtime, and launchd |
| `src/eidolon/defaults/`, `config.example.yaml` | Generic bundled prompts and public configuration example |
| `tests/`, `scripts/` | Regression tests, privacy guards, and focused validation utilities |
| `docs/`, `docs/agents/` | User and technical guides, plus agent workflow conventions |

Read [AGENTS.md](AGENTS.md) for repository instructions and
[DESIGN.md](DESIGN.md) before changing terminal interactions or layout. The design
guide defines intended behavior; some existing screens still need migration.
Keep chat and composer central, actions keyboard-reachable, buttons flat, and
modal focus and drafts intact. Review small terminals, monochrome/ASCII,
resizing, and loading/error/disconnected states as specified there.

Follow the [domain documentation conventions](docs/agents/domain.md) when
exploring terminology or architectural decisions. Shared vocabulary belongs in
root `GLOSSARY.md`; decisions belong in `docs/adr/NNNN-<decision-slug>.md`.
These are created when needed and may be absent. Use existing terms and identify
conflicts with relevant ADRs when proposing a change.

Use the [README guide index](README.md#guides-and-reference) to find user-facing
behavior and detailed integration references. Update the relevant maintained
guide, CLI help, or generic example alongside behavior changes. Check documented
commands, defaults, and links against current implementation rather than copying
old reports. Keep detailed setup procedures in their authoritative guide and
link to them from other pages.

## Implementation and test practices

Follow nearby code, with four-space indentation, `snake_case` functions/modules,
`PascalCase` classes, and type annotations on new interfaces. Ruff targets Python
3.12 with a configured 100-character line length; its selected lint rules are in
[`pyproject.toml`](pyproject.toml). Keep integration logic in its owning module
instead of expanding UI handlers.

Add regression tests for behavior changes. Name files `test_<module>.py` and
functions `test_<behavior>`. pytest-asyncio uses automatic mode, and there is no
configured numeric coverage threshold.

Isolation is established by individual tests and fixtures, not a suite-wide
private-state sandbox. Use `tmp_path` for configuration, databases, prompts, and
jobs outside the checkout. Patch `EIDOLON_HOME` and home-directory resolution
when testing code that uses default locations, as in
[`tests/test_cli.py`](tests/test_cli.py). Fake providers and memory services, use
`httpx.MockTransport` for HTTP, and replace Keychain and process/launchd adapters
when testing those boundaries. Service tests use temporary Unix sockets; give
parallel tests independent paths and clean up async tasks, connections, and
servers. Do not use a real personal profile, live credentials, or installed login
agents as test fixtures.

Exercise Textual interactions with `app.run_test()` and its pilot, using fake
clients as in [`tests/test_ui.py`](tests/test_ui.py). Assert observable behavior,
including focus restoration, preserved drafts, and keyboard access where relevant.
Synthetic adapter tests do not establish that a live Keychain transfer, launchd
replacement, or external integration has been validated; distinguish any manual
integration checks in the change description.

## Validation commands

Always use four workers and work stealing for full and focused pytest runs:

```sh
uv run pytest -n 4 --dist worksteal
uv run pytest -n 4 --dist worksteal tests/test_provider.py
uv run pytest -n 4 --dist worksteal tests/test_provider.py -k reasoning_stream
```

Append paths, node IDs, or `-k` filters to the same base command. This supersedes
serial pytest examples in older reports and specs. Use `-n 0` only for a specific
debugging need that requires serial execution, and explain the reason in the
validation notes.

Run the checks appropriate to the change and report their actual results:

| Command | What it checks or produces |
| --- | --- |
| `uv run ruff check .` | Configured Python lint rules; this does not format files or run a type checker |
| `uv run python -m compileall -q src` | Source compilation; this does not exercise application behavior |
| `uv build` | Source distribution and wheel under `dist/` using Hatchling; this does not publish them |
| `uv run pre-commit run --all-files --hook-stage manual` | Configured manual private-file, private-file history, and secret-history checks |
| `uv run pip-audit --local --skip-editable` | Known vulnerabilities in the synced environment, excluding the editable application |
| `git diff --check` | Whitespace errors in the diff |

For application behavior changes, run relevant regression tests and the full
suite before handoff. For documentation-only work, verify changed commands,
examples, local links, and privacy checks; explain any test scope limits. Packaging
changes should also be checked with `uv build`. Hook commands using `--all-files`
select tracked files; include new files explicitly with `--files <paths>` when
checking them before staging.

The [GitHub Actions workflow](.github/workflows/security.yml) runs on pushes,
pull requests, manual dispatch, and a weekly schedule. Ubuntu jobs check full
history with the privacy hooks and audit locked dependencies with pip-audit.
A macOS job runs Ruff, source compilation, the four-worker test suite, and the
package build. Live Keychain, launchd, provider, and memory-service checks still
require separate validation. Include local validation and any integration limits
in the PR description.

## Privacy and preparing a contribution

Keep credentials, custom prompts, personal configuration, conversations,
databases, exports, logs, and diagnostic captures out of Git. Runtime state
normally lives in `~/Library/Application Support/Eidolon/`; overrides and backups
also belong outside checkouts. Use `env:NAME` or `keyring:NAME` credential
references, generic defaults, and synthetic examples. Consult the
[privacy guide](docs/PRIVACY.md) before sharing diagnostics or touching state.

The commit hooks check private-file paths and recognizable secrets. Push hooks
check private paths in reachable history and run Gitleaks across all refs with
redacted findings. The manual command above includes history checks. Review
[the guard](scripts/check_private_files.py),
[hook configuration](.pre-commit-config.yaml), and the
[privacy/history explanation](docs/PRIVACY.md#repository-privacy-checks-and-history)
for their scope. These checks cannot detect all personal prose or every secret
and do not guarantee public-release safety. Review new files and the diff yourself;
ignore rules alone do not sanitize content, and deleting a file does not remove
it from history. Handle any history remediation separately.

Use imperative, sentence-case commit subjects, for example “Preserve composer
drafts when closing preferences.” Keep the PR focused. Explain the problem and
resulting behavior, link the relevant issue, and list validation commands,
results, and limits. For terminal layout changes, include reviewed screenshots
made with synthetic content and describe keyboard/resize checks required by
`DESIGN.md`. Documentation-only changes do not need screenshots. Review
`git diff` and `git diff --cached` before committing to confirm the intended scope
and absence of private material.
