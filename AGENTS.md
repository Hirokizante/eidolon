# Repository Guidelines

Read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, validation scope,
test isolation, documentation maintenance, and contribution preparation.

## Project Structure & Module Organization

Eidolon is a standalone macOS terminal companion built with Python 3.12+ and Textual. Source lives in `src/eidolon/`: `ui/` contains screens, commands, and `app.tcss`; `engine/` orchestrates conversations; `providers/`, `memory/`, `mcp/`, and `avatar/` connect external services. `storage/` manages SQLite persistence, `scheduler/` runs jobs, and `service/` handles the background runtime and launchd integration.

Tests live in `tests/`. Generic prompt assets are in `src/eidolon/defaults/`; configuration examples are in `config.example.yaml`. Consult `docs/` for integration and migration details.

## Build, Test, and Development Commands

Run commands from the repository root:

- `uv sync --locked --group dev`: install dependencies from `uv.lock`.
- `uv run pre-commit install --install-hooks`: install the configured commit and push privacy guards.
- `uv run eidolon setup`: initialize private app configuration.
- `uv run eidolon doctor`: inspect configuration and integration health.
- `uv run eidolon`: launch the terminal app.
- `uv run pytest -n 4 --dist worksteal`: run the test suite with four workers.
- `uv run ruff check .`: run configured lint checks.
- `uv run python -m compileall -q src`: check source compilation.
- `uv build`: build distribution artifacts using Hatchling.
- `uv run pre-commit run --all-files --hook-stage manual`: run privacy and secret checks, including history scans.

## Coding Style & Naming Conventions

For UI changes, follow [DESIGN.md](DESIGN.md).

Use four-space indentation, `snake_case` for functions/modules, and `PascalCase` for classes. Follow nearby code and add type annotations to new interfaces. Ruff targets Python 3.12 with a configured 100-character line length; no formatter hook is configured. Keep integration logic in its existing module rather than expanding UI handlers.

## Testing Guidelines

Keep screenshots generated during testing in temporary directories outside Git checkouts and worktrees. Never stage or commit these captures, including screenshots made with synthetic content. Record UI validation results in the contribution notes.

Use pytest, pytest-asyncio, and pytest-xdist; async tests run in auto mode. Name files `test_<module>.py` and functions `test_<behavior>`. Use `tmp_path`, fake services, and `httpx.MockTransport` to isolate state and network calls. Exercise Textual interactions with `app.run_test()` and its pilot. Add regression tests for behavior changes; no numeric coverage threshold is configured.

Always use `uv run pytest -n 4 --dist worksteal` for full-suite and focused test runs. Append paths, node IDs, or `-k` filters to this command; for example, `uv run pytest -n 4 --dist worksteal tests/test_provider.py`. These commands supersede serial pytest examples in older reports and task specs. Use `-n 0` only when a specific debugging task requires serial execution, and explain the reason in validation notes.

## Commit & Pull Request Guidelines

Use imperative, sentence-case commit subjects such as “Harden memory recall, recovery, and presentation fallback.” Keep commits focused. PRs should explain changed behavior, link relevant issues, and list validation performed and its limits, including terminal layout checks. Update maintained guides and generic examples when behavior changes.

Use GitHub Issues and pull requests for planning and review. CI runs privacy/history hooks, dependency auditing, Ruff, source compilation, tests, and builds. Live macOS integrations require separate validation.

## Security & Configuration

Keep credentials, custom prompts, databases, and exports out of Git. Runtime state belongs in `~/Library/Application Support/Eidolon/`; an isolated `EIDOLON_HOME` must remain outside Git checkouts and worktrees. Use `env:NAME` or `keyring:NAME` credential references. Commit only generic defaults and explicit examples. Read [docs/PRIVACY.md](docs/PRIVACY.md) before sharing diagnostics or changing private-state handling. Review diffs and new files; privacy hooks cannot guarantee public-release safety or sanitize Git history.

## Agent skills

### Issue tracker

Use GitHub Issues for engineering tasks and specifications. Read
[docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) when creating,
fetching, or updating issues or specifications.

### Triage labels

Use the five default triage labels. Read
[docs/agents/triage-labels.md](docs/agents/triage-labels.md) when assigning
or changing GitHub issue triage labels.

### Domain docs

Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`. Read
[docs/agents/domain.md](docs/agents/domain.md) before exploring the codebase
with engineering skills. Domain files are created when needed and may be absent.
