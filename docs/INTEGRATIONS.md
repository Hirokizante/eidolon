# Tools and MCP integrations

[Back to Eidolon](../README.md) · [Optional avatar](AVATAR.md)

Eidolon owns its configuration and credentials. Normal operation uses its private
configuration and credential references. In default companion mode, built-in
tools provide configurable web search, bounded readable webpages, restricted
folder access, and targeted Hindsight recall/reflection. `read_skill` loads
enabled user workflows. Companion mode provides no shell or general file-editing
tool.

## Agent mode

Use `/agent` to toggle agent mode, or `/agent on` and `/agent off` to choose it
explicitly. The selected provider must support tool calling. The mode starts off
when the background service starts, is not written to configuration, and remains
visible as **Agent mode** at the beginning of the status bar while enabled.
Hiding the usual status details does not hide this reminder.

Agent mode keeps companion tools and explicitly enabled MCP tools, replaces the
three restricted file-reading schemas with unrestricted local versions, and adds:

| Tool | Behavior |
| --- | --- |
| `list_files` | Browse any local directory, including symlink entries. |
| `read_file` | Read UTF-8 text with a 1-based `offset` and line `limit`; `next_offset` continues a bounded page. |
| `search_files` | Search literal text or a regular expression, optionally filtering filenames/relative paths with `glob`. |
| `write_file` | Create or replace a text file, creating parent directories and preserving existing permissions. |
| `edit_file` | Replace an exact snippet; ambiguous matches require more context or `replace_all`. |
| `run_command` | Run a shell command with optional `cwd`, `tty`, timeout, and initial wait. |
| `read_command_output` | Poll new stdout/stderr and the exit status using `command_id`. |
| `write_command_input` | Send input to stdin or the terminal, optionally sending EOF. |
| `stop_command` | Stop a managed command and its process group. |
| `list_commands` | Inspect managed command sessions. |

File paths and command `cwd` default to the user's home directory. Agent file
tools follow symlinks and are not restricted by `allowed_roots`; operating-system
permissions still apply. File edits are atomic. Reads and output are bounded to
fit the model's context; page reads and poll while `output_remaining` is true.
Text writes/edits support files up to 2 MB; commands can handle larger or binary
files, patch application, directory operations, Git, builds, tests, and networking.

Commands use the user's shell, default to a 600-second timeout, and return after
`yield_time_ms` (default 1,000; maximum 10,000). A running command retains its
`command_id` across successful conversation turns. Terminal sessions combine
stdout/stderr in `stdout`. Up to eight commands may run concurrently, and the
service retains up to 32 command records with bounded output buffers. Stopping
agent mode or the service stops managed commands; cancelling a response stops
commands started by that response. An interrupted response does not undo file
changes or completed command effects.

Interactive agent responses allow up to 50 tool rounds and 30 minutes; companion
responses retain their existing limits. Scheduled jobs never inherit chat's agent
mode. Each job has a saved `agent_mode` flag (off for existing jobs), editable in
`/jobs`. Agent jobs use a separate tool registry and the same agent turn limits;
their commands stop when the job finishes or fails. They do not animate the avatar.
With a blank `permitted_tools` list, agent jobs receive file/command tools and
`read_skill`; a nonempty list restricts them to those tools. Companion jobs retain
their original allowlist behavior, where a blank list permits no tools. The job's
provider must have tool calling enabled. Changing provider connections
preserves the mode but closes command sessions when components are rebuilt.

## Companion tool boundaries

Configure `allowed_roots` with folders the companion may read in companion mode. File tools refuse symlink
traversal, parent traversal, and paths outside these folders. Search scans at most
1,000 entries, 12 directory levels, and 100 matches; reads are bounded to 256 KB.
Listing and search skip descendant directories named `.git`, `.venv`,
`.hindsight-runtime`, `node_modules`, `__pycache__`, `.pytest_cache`, and
`.ruff_cache`, so dependency/state files do not consume the source-search
budget. Explicit permitted paths into these folders still work.

Webpage fetching accepts standard HTTP/HTTPS ports, checks every redirect, blocks
private/local/reserved addresses, and connects to the validated address while
preserving TLS hostname validation. Downloads are bounded to 1 MB and returned
text to 20,000 characters. Webpage content remains untrusted tool data.

Web search uses one `web_search` tool in companion mode, agent mode, and scheduled
jobs. Select **Web search** in `/preferences` to choose Tavily, Brave Search, Exa,
Perplexity, or SearXNG. **Avatar** is a separate preference category. Each search
provider retains its own credential, result limit, timeout, excerpt limit, and
filters when you switch providers. Expand **Search filters and limits** for all
remaining options. Provider-specific controls include:

- Tavily: depth, topic, recency, publication dates, included/excluded domains,
  snippets per source, publication metadata, exact matching, and safe search.
- Brave: country, language, safe search, freshness (including custom date ranges),
  additional snippets, and Goggles.
- Exa: search mode, highlights or bounded page text, category, domain/path filters,
  and publication dates. The form validates category/filter incompatibilities.
- Perplexity: standard or fast search, country, languages, domain inclusion or
  exclusion, publication/updated-date filters, recency, and content token limits.
- SearXNG: instance base URL, engines, categories, language, recency, and safe
  search. Enable `json` in the instance's `search.formats`; public instances often
  disable JSON. Localhost instances are supported independently of the public
  webpage fetcher's network restrictions. An optional API key is sent as Bearer
  authentication for protected instances.

Paste a new API key into the masked field, or enter an `env:NAME` / `keyring:NAME`
reference. Blank keeps the saved credential; **Remove saved credential** removes
its configuration reference. New keys are stored in Keychain by the service,
never as literal values in YAML or returned configuration. **Test** runs one
search using the draft settings without saving settings or storing new keys.
`Ctrl+S` applies changes, `Esc` keeps an in-memory draft, and `Ctrl+L` reloads the
saved settings and clears draft keys. Search changes take effect after any
in-flight response completes, without restarting avatar or MCP connections.

Configuration is under `web_search.provider` and `web_search.<provider>`, with
credentials in `api_key_ref`, such as `keyring:tavily` or
`env:EIDOLON_TAVILY_KEY`. Legacy top-level `tavily` settings load automatically
and are written in the new structure on the next save. Tavily remains the
default, with basic search, five results, and bounded source snippets. Results
contain the serving provider and normalized source titles, URLs, excerpts, and
publication dates when available. Provider-generated answer fields are excluded
from tool results.
Store secrets from the CLI using `eidolon secret set NAME`; the resulting reference
is `keyring:NAME`.

## MCP prerequisites and permissions

MCP connects Eidolon to independently provided tools. Install or obtain your MCP
server separately and follow its requirements. For a local server you need a
working executable, project dependencies, and any files or credentials it uses.
For a remote server you need a reachable Streamable HTTP endpoint and its required
authentication headers. Eidolon does not install servers or register accounts.
A conversational model must support tool calling and have `tools: true` in its
[provider profile](PROVIDERS.md) to use discovered tools.

Configure servers in `mcp_servers` in your private `config.yaml`. This starts as
an empty list. Each enabled server needs a unique `name`, independent of display
names, and an explicit `enabled_tools` list of the server's raw tool names. Only
allowlisted tools that the server actually advertises are exposed and callable.
An empty list permits no tools; it is not a wildcard. `enabled: false` skips the
server entirely. An allowlist grants the listed tools' capabilities; companion
file restrictions do not constrain what an independent MCP server can do.

### Local stdio server

With `transport: stdio`, Eidolon launches `command` directly with the `args` list
and keeps a persistent MCP session over stdin/stdout. There is no shell expansion
of `~`, `$HOME`, pipes, or quoted command strings. Use actual absolute paths to
executables and project files, particularly when running as a background service.

This synthetic example assumes a separately installed Python MCP project that
provides `lookup_note`:

```yaml
mcp_servers:
  - name: notes-local
    transport: stdio
    command: /absolute/path/to/uv
    args:
      - run
      - --directory
      - /absolute/path/to/notes-server
      - python
      - /absolute/path/to/notes-server/server.py
    env:
      NOTES_API_TOKEN: keyring:notes-api-token
    enabled_tools: [lookup_note]
    enabled: true
    timeout: 10
```

Replace paths and tool/environment names with your server's actual contract.
`uv run --directory` uses that project's dependencies. Other executable/argument
combinations are supported; `server.py` and `pyproject.toml` are requirements of
[avatar setup](AVATAR.md#register-the-bridge), not of every MCP server.

### Streamable HTTP server

Use `transport: http` with the complete MCP endpoint in `url`. This means
Streamable HTTP through the MCP Python SDK; it is not a generic REST URL or a
legacy SSE endpoint. Eidolon connects to the server rather than launching it.

```yaml
mcp_servers:
  - name: notes-remote
    transport: http
    url: https://mcp.example.invalid/mcp
    headers:
      Authorization: keyring:notes-authorization
    enabled_tools: [lookup_note]
    enabled: true
    timeout: 10
```

The reserved `.invalid` hostname is illustrative; supply your service's actual
URL. Omit `headers` for a server that requires none. URLs must use HTTP(S) and
cannot embed a username/password. `env` supplies the stdio subprocess;
`headers` supplies the HTTP connection. They do not configure one another.

### Credential references

Environment and header mappings accept `env:NAME` and `keyring:NAME` references.
Use these for all secrets. Nonsensitive literal settings, such as a locale, are
also accepted. Sensitive fields such as `Authorization` or `NOTES_API_TOKEN`
require a credential reference. Store a value through the masked prompt:

```sh
uv run eidolon secret set notes-api-token
uv run eidolon secret set notes-authorization
```

The first command creates `keyring:notes-api-token`; the second creates
`keyring:notes-authorization`, both in the Keychain service `eidolon`. Values are
substituted as stored: for an `Authorization` header that expects Bearer auth,
enter the complete `Bearer <token>` header value at the prompt. Eidolon does not
prepend `Bearer` to MCP headers. Do not write secret values into YAML or command
arguments.

An `env:NAME` reference reads from the Eidolon service process's environment.
Ensure it reaches that process; an existing service retains its old environment,
and a LaunchAgent does not automatically inherit terminal exports. Keychain is
usually simpler for a background service. See
[state and credentials](INSTALLATION.md#configuration-state-and-credentials).

### Activate, diagnose, and recover

After editing your private configuration, close the UI and stop the service
using the same `EIDOLON_HOME` or global `--config` selection as setup:

```sh
uv run eidolon service stop
uv run eidolon service status
```

Wait until status reports stopped before starting again; an immediate stop/start
can race launchd unloading. Then reconnect and check discovery:

```sh
uv run eidolon service start
uv run eidolon doctor
uv run eidolon
```

See [service controls](OPERATIONS.md#install-and-control-the-login-agent) for
login-agent ownership and restart details.

From the UI, `/check` also requests diagnostics. With the service running,
`doctor` includes an `mcp` mapping: each server has `tools` (discovered allowlisted
names) and `error` (the latest local error, or null). With the service stopped,
`doctor` does not open MCP connections. It checks configuration and other
integrations, so run the service before using it to assess MCP discovery. A
successful command exit or discovery alone does not prove that a tool action
works. Check an appropriate harmless operation with your server.

Connections are persistent; requests to a given server are serialized. `timeout`
is a positive number of seconds, at most 120, defaulting to 10. A disconnect or
timeout becomes a local tool error. Later requests may reconnect and rediscover
tools; the failed invocation is never automatically repeated because its outcome
may be unknown. Optional server failures do not prevent chat from opening.

| Symptom | Check or recovery |
| --- | --- |
| Server absent from `mcp` diagnostics | Check `enabled`, server name, active private configuration, and restart after edits. |
| Empty or missing `tools` | Check exact `enabled_tools` names against the server's advertised tools and inspect `error`. Empty allowlists grant nothing. |
| Stdio server unavailable | Check executable and argument paths, dependency installation, permissions, and the server's own diagnostics. Use `service.log` for Eidolon process errors. |
| HTTP server unavailable or unauthorized | Check endpoint/transport, reachability, and the full resolved header value. Confirm credentials are available to the service. |
| Tools discovered but unavailable in conversation | Check provider tool support and `tools: true`; jobs can impose an additional permitted-tools list. |
| Invocation times out or disconnects | Inspect the external service's state before choosing to repeat an action. A local error does not establish whether the remote operation ran. |

To stop all contact with a server, set its `enabled: false` and restart Eidolon.
Clearing `enabled_tools` removes tool permission but leaves connection/discovery
enabled. Disabling Eidolon's route does not shut down an independently running
HTTP service. Use that service's own shutdown procedure if needed.

For the avatar-specific tool contract, signed lifecycle endpoint, and setup
flags, continue with [Optional avatar integration](AVATAR.md). Implementation
references: [MCP configuration](../src/eidolon/config.py),
[persistent MCP client](../src/eidolon/mcp/client.py), and
[controlled MCP tests](../tests/test_mcp.py).
