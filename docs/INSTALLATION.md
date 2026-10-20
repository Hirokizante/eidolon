# Installation and first conversation

[Back to Eidolon](../README.md)

This guide takes a new macOS user from a source checkout to a conversation with a
configured model. Eidolon is a Python 3.12+ terminal app built with Textual. It
starts with generic prompts and the display names `You` and `Companion`; you can
customize both without supplying a personal profile.

## Prepare your terminal and model

You need macOS, a terminal, and a source checkout containing `pyproject.toml`,
`uv.lock`, and `src/eidolon/`. Obtain the source from the repository you are
reading, then open Terminal in its root folder. The commands below install and
run that checkout.

Install uv using its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
Verify that your shell can find it:

```sh
uv --version
```

If the command is unavailable after installation, follow the installer's PATH
instructions or open a new terminal. Eidolon requires Python 3.12 or newer. uv
can download a compatible Python if one is missing; see uv's
[project guide](https://docs.astral.sh/uv/guides/projects/).

Before configuring Eidolon, obtain these details from your model service:

| Detail | What to supply |
| --- | --- |
| Endpoint | The service's OpenAI-compatible API base URL, including its API prefix such as `/v1` where required. |
| Model | An exact model ID available to your account or local server. |
| Credential | An API key if required by that endpoint; keyless servers can leave it blank. |

The service must support streaming Chat Completions. Compatibility does not
establish tool calling, image input, structured output, or other capabilities.
If you use a local model server, install and start it separately and load a model
before checking the connection. This guide does not provision a model service.

## Install dependencies

From the checkout root:

```sh
uv sync --locked --group dev
```

This installs the project and its development dependency group in the checkout's
`.venv` using `uv.lock`. Use `uv run eidolon …` from this folder for the remaining
steps; activating `.venv` manually is unnecessary. Dependency installation may
need network access. Python dependencies in `.venv` are separate from Eidolon's
private runtime state.

## Configure a model

Run interactive setup:

```sh
uv run eidolon setup --interactive
```

Plain `uv run eidolon setup` only initializes configuration and bundled prompts
on a fresh install. Its selected provider is named `local`, its endpoint is
`http://127.0.0.1:1234/v1`, and its model is `configure-model`. That model string
is a placeholder. Setup does not discover a running server or choose a real
model for you, so accepting every default will not produce a usable conversation.

Interactive setup asks for the following values in order. Pressing Enter keeps
the current value; on a fresh install, follow the guidance here:

| Prompt | First-conversation guidance |
| --- | --- |
| Your name | Keep `You` or enter a display name. |
| Companion's name | Keep `Companion` or enter a display name. |
| Provider endpoint | Enter your service's API base URL. The default is useful only if you already run a compatible server at that address. |
| Model | Replace `configure-model` with your actual model ID. |
| Provider API key | Enter the key if required. Input is hidden and saved to macOS Keychain. Leave blank for a keyless endpoint. |
| Tavily API key | Optional web search; leave blank. |
| IANA timezone | Keep the detected zone or enter a zone such as `Europe/Paris` or `UTC`. |
| Existing Hindsight URL | Optional external memory; leave blank to keep the default URL without connecting it. |
| Existing bank | Leave blank to keep external memory unconfigured. |
| Permitted read-only folders | Optional file access; leave blank to keep the initial empty list. |
| Standalone Vessel bridge directory | Optional avatar bridge; leave blank. |

The bridge prompt uses the external integration's name. No bridge, memory bank,
search key, or permitted folder is required for an ordinary conversation.
For an existing bridge, see [optional avatar setup](AVATAR.md); other tool servers
use the [MCP configuration guide](INTEGRATIONS.md#mcp-prerequisites-and-permissions).
Recording a Hindsight URL and bank does not itself enable recall, saving, or
retention verification. On a fresh install, external recall and saving are off,
no bank is selected, and the avatar is disabled. Local chat history is stored
independently in SQLite. When you have an existing endpoint and bank, follow
[optional memory setup](MEMORY.md) to activate and verify each control.

Setup detects and saves an IANA timezone from the system, using UTC if detection
fails. It prints the saved zone when finished. Run interactive setup again to
change it. Conversation days use that timezone and a default reset hour of 04:00.

Setup can be repeated: ordinary setup preserves saved settings and custom prompt
contents. Interactive setup shows saved values and changes the answers you
supply. A blank API key preserves an existing credential reference; it does not
clear it. Blank optional integration answers also preserve existing settings,
so they skip integrations on a fresh install rather than disabling them on an
existing one.

### Display names

You can also update names without answering every setup prompt:

```sh
uv run eidolon setup --user-name "Aster" --companion-name "Nova"
```

These names are synthetic examples. Omitting either flag preserves that saved
name. Names must contain 1–80 Unicode code points after trimming spaces; blank
names, line breaks, terminal controls, bidi override/isolate controls, and invalid
UTF-8 are rejected. The app also provides **Preferences → Identity** for editing
names. Configured names take precedence over conflicting names in custom prompts,
while the prompt contents are preserved. Names imply no gender or relationship.

Each response or scheduled run keeps the names captured when it starts. New
exchanges record that identity for later display; older exchanges without a
snapshot use current configured labels. Renaming does not rewrite conversation
text or change integration routes.

For data destinations, diagnostic handling, and repository checks, read
[Privacy and private state](PRIVACY.md). Existing installations should use
[State migration](STATE_MIGRATION.md) for a verified move and
[Backup and recovery](RECOVERY.md) before schema or runtime changes.

## Check the configuration

Run:

```sh
uv run eidolon doctor
```

Read the JSON output, including the `provider` message. The command can finish
successfully while reporting an unreachable provider or an HTTP error; its exit
status alone is not a readiness check.

With the service stopped, doctor requests the selected endpoint's `/models`
inventory, reports its HTTP status or a connection/credential failure, checks
prompt file presence, and reports service, memory, avatar, and auxiliary-model
configuration. On a fresh install, memory and avatar being disabled is expected.
It does not start the application service.

With a service already running, doctor asks it for diagnostics, including model
discovery, memory checks, auxiliary state, MCP/avatar status, and pending memory
deliveries. The stopped-service check validates memory only when recall is
enabled; the running service checks a selected bank independently of recall and
saving. Neither check writes conversation data to that bank.

**An inventory response is not a chat test.** Even HTTP 200 or a list of model IDs
does not prove that the selected model works, streams responses, calls tools,
accepts post-history instructions, handles images, or supports structured output.
An endpoint without model discovery may still accept a manually supplied model
ID. Confirm basic conversation next, and verify additional capabilities before
relying on them.

## Start your first conversation

From the checkout root:

```sh
uv run eidolon
```

Launching Eidolon starts or connects to its background service, then opens the
terminal UI in the current conversation day. No LaunchAgent installation is
required for this first run. Enter a simple message such as `Hello. What can you
help me with?` and confirm that a reply streams and completes.

| Action | Default control |
| --- | --- |
| Send | Enter; Ctrl+Enter is an alternate send key. |
| Insert a newline | Shift+Enter |
| Cancel an active response | Ctrl+C |
| Help | F1 or `/help` |
| Inspect connection diagnostics | `/check` |
| Edit or add a provider | `/connect` |
| Select a model | `/models` |
| Browse local conversation history | `/history` or Alt+2 |
| Close the terminal UI | Ctrl+Q or `/quit` |

Closing the UI leaves the service running. To stop it after use:

```sh
uv run eidolon service stop
```

The next launch starts it again. Login startup through a macOS LaunchAgent is an
optional later configuration step. See
[Background operation and scheduled jobs](OPERATIONS.md) for foreground service
diagnostics, login-agent installation and removal, lifecycle controls, and job
management.

## Configuration, state, and credentials

The default private directory is `~/Library/Application Support/Eidolon/`:

| Location inside that directory | Purpose |
| --- | --- |
| `config.yaml` | Provider profiles, credential references, identity, timezone, and integration settings. |
| `prompts/` | Editable `SYSTEM.md`, `SOUL.md`, `USER.md`, and `POST_HISTORY.md`, initially copied from generic bundled defaults. |
| `room.sqlite3` | Local conversations and application records; created by the runtime. |
| `jobs.json` | Scheduled jobs; a fresh install starts with an empty list. |
| `service.log` | Background service output. |

Treat these files, exports, and custom prompts as private. Keep runtime state
outside every Git checkout and worktree; Eidolon rejects runtime configuration
and state paths inside them. Do not run the tracked
[configuration example](../config.example.yaml) as your working configuration.
Edit the private file created by setup instead. Your messages and context are
sent to the configured model endpoint when you converse.

For an isolated installation, set `EIDOLON_HOME` before setup and keep it set for
all commands that use that installation. This synthetic example checks setup in
a separate private directory outside the checkout:

```sh
export EIDOLON_HOME="$HOME/Library/Application Support/Eidolon-Sandbox"
uv run eidolon setup --interactive
uv run eidolon doctor
```

Choose a fresh directory for a separate installation. Changing this environment
variable selects a location; it does not move existing state. Run any service
stop command while the override is still set. Future shells need the same
override to use the same installation. An installed LaunchAgent retains its saved
configuration path; changing the shell override does not reconfigure that agent.
Account for an existing LaunchAgent before launching a second installation.
The global `--config` option can instead select an explicit private file, for
example:

```sh
uv run eidolon --config "$HOME/Library/Application Support/Eidolon-Sandbox/config.yaml" doctor
```

Configuration stores credential references, never inline API keys. Interactive
setup stores a provider key in the Keychain service `eidolon` and saves a
reference such as `keyring:provider-local`. A manually configured `env:NAME`
reference reads the named environment variable. For example, the selected
provider's `api_key_ref` can be `env:EIDOLON_PROVIDER_KEY` if that variable is
supplied privately to the service process. Leave `api_key_ref: null` for a keyless
endpoint. Avoid putting secret values in shell commands or tracked files.

Environment credentials must reach the background service, not just the UI.
An already running service keeps its original environment, and an installed
LaunchAgent does not automatically inherit your interactive shell's variables.
For first setup, Keychain avoids that environment handoff. Do not share private
configuration, logs, or request exports without reviewing their contents.

## Troubleshoot the first run

| Symptom | Next step |
| --- | --- |
| `uv` is not found | Finish uv installation and PATH setup, then reopen Terminal. |
| `Room is not configured` | Run interactive setup with the same `EIDOLON_HOME` or `--config` used to launch. |
| Runtime state path is rejected | Choose a private directory outside Git checkouts and worktrees. |
| Provider unreachable | Start your model service, check the base URL/API prefix, and check connectivity. |
| HTTP 401/403 or unavailable credential | Check your API key and permissions; blank setup input preserves the old key. |
| Model missing or `configure-model` still selected | Enter an exact available model ID in interactive setup or `/models`. |
| Inventory succeeds but chat fails | Check streaming Chat Completions support and the selected model. Inventory does not establish capability support. |
| Tools or structured output fail | Review the provider profile in private `config.yaml`; set `tools: false` for a model without tool calling, and use `presentation: plain` if needed. Only set `structured_verified: true` after explicit compatibility testing. |
| Service does not start | Inspect `service.log` in this installation's private state directory. |

The service reads saved connection settings before the next conversation turn.
For a clean service restart after manual edits, close the UI, run
`uv run eidolon service stop`, then launch again with the same state selection.
