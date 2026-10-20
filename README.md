# Eidolon

Eidolon is a configurable macOS terminal companion built with Python 3.12+ and
[Textual](https://textual.textualize.io/). Talk with a model in your terminal,
keep local conversation history, and customize the companion's name and
personality. New installations use the display names **You** and **Companion**
and generic prompts with no assumed personal facts or relationship.

Eidolon connects to a model endpoint you configure. External memory, web search,
avatar services, and other tool integrations are optional.

## Prerequisites

- macOS and a terminal.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12
  or newer; uv can obtain a compatible Python when syncing the project.
- A source checkout of this repository.
- A running OpenAI-compatible **Chat Completions** endpoint, an available model
  ID, and an API key if that endpoint requires one. Conversation responses use
  streaming; model and tool capabilities vary by endpoint.

## Quickstart from source

Open Terminal in the checkout root (the folder containing `pyproject.toml`), then
run these commands in order:

```sh
uv sync --locked --group dev
uv run eidolon setup --interactive
uv run eidolon doctor
uv run eidolon
```

During setup, enter your provider's base URL, actual model ID, and any required
API key. Keep the detected timezone or enter another IANA timezone. For a first
conversation, leave the optional search key, Hindsight endpoint/bank, permitted
folders, and avatar bridge prompts blank. See the
[setup prompt walkthrough](docs/INSTALLATION.md#configure-a-model) for details.

**Plain `setup` is not enough to chat:** it creates a profile pointing at
`http://127.0.0.1:1234/v1` with the placeholder model `configure-model`. Interactive
setup lets you configure a working endpoint and replace the placeholder model. A
successful `doctor` inventory check still needs a
[first conversation check](docs/INSTALLATION.md#start-your-first-conversation).

Once the app opens, type a message and press Enter. Shift+Enter inserts a newline;
F1 or `/help` shows controls. Ctrl+Q closes the terminal UI while its background
service remains running.

## Configuration and local state

Setup writes private configuration to
`~/Library/Application Support/Eidolon/config.yaml`. That directory also holds
editable prompts, SQLite conversation history, jobs, and service logs. Credentials
entered during setup are stored in macOS Keychain; configuration stores references
such as `keyring:provider-local`. `env:NAME` references are also supported.

Use `EIDOLON_HOME` to select a different private state directory. Keep all runtime
state outside Git checkouts and worktrees, including isolated test installations.
External memory starts disabled; local conversation history remains available.
See [state and credentials](docs/INSTALLATION.md#configuration-state-and-credentials)
before editing configuration or choosing an isolated installation.

## Guides and reference

- [Installation and first conversation](docs/INSTALLATION.md): dependency setup,
  prompt-by-prompt configuration, connection checks, and first-run troubleshooting.
- [Configuration example](config.example.yaml): generic fields and defaults;
  setup creates your private working configuration.
- [Everyday use](docs/EVERYDAY_USE.md): conversation controls, history, activity,
  images, settings, and waiting-message recovery.
- [Background operation and scheduled jobs](docs/OPERATIONS.md): application
  service and login-agent controls, job creation and run recovery, diagnostics,
  and the existing managed-memory runtime.
- [Privacy and private state](docs/PRIVACY.md): credential references, data sent
  to integrations, safe diagnostic sharing, and repository privacy checks.
- [State migration](docs/STATE_MIGRATION.md) and [backup and recovery](docs/RECOVERY.md):
  external-profile import versus installation handoff, synthetic manifests,
  verification, incomplete-copy recovery, and restore/rollback limits.
- [Personalization](docs/PERSONALIZATION.md): participant names, personality,
  private prompt files, and preservation behavior.
- [Providers and models](docs/PROVIDERS.md): connections, credentials, capabilities,
  token limits, and separate vision; [auxiliary model](docs/AUXILIARY_MODEL.md)
  covers background inference.
- [Optional memory](docs/MEMORY.md): existing Hindsight bank setup, independent
  recall and saving controls, delivery readiness, inspection, and disabling.
  [Current-life notes](docs/CURRENT_LIFE_SUMMARIES.md) cover local summaries;
  [memory reference](docs/MEMORY_REFERENCE.md) covers queues, runtime maintenance,
  and [synthetic verification and benchmarks](docs/MEMORY_REFERENCE.md#compatibility-and-synthetic-verification).
- [Themes](docs/THEMES.md) and [skills](docs/SKILLS.md): appearance and reusable
  companion instructions.
- [Tools and MCP](docs/INTEGRATIONS.md): tool permissions, stdio and Streamable
  HTTP servers, credential references, and connection recovery.
  [Optional avatar](docs/AVATAR.md) covers a user-supplied bridge, signed lifecycle
  events, activation, diagnostics, and disabling.
- [Model metadata](docs/MODEL_METADATA.md) and
  [prompt caching](docs/PROMPT_CACHING.md): detailed model and request behavior.
- [Contributing](CONTRIBUTING.md): development setup, test isolation, required
  checks, and issue/PR preparation; [repository instructions](AGENTS.md) and
  [UI design rules](DESIGN.md) cover engineering and terminal changes.

For the current CLI options, run `uv run eidolon --help` or
`uv run eidolon setup --help` from the checkout root.

## License

Eidolon is available under the [MIT License](LICENSE).
