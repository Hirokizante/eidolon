# Optional avatar integration

[Back to Eidolon](../README.md) · [Tools and MCP](INTEGRATIONS.md)

Eidolon can send expressions, gestures, and response lifecycle events to a
user-supplied avatar bridge and renderer. The avatar is independent and optional;
ordinary text conversation works with `avatar.enabled: false`, the default.
Eidolon does not bundle, install, or update the bridge, renderer, or avatar assets.
Obtain and configure them separately using their own documentation.

## Prepare a compatible bridge

The setup prompt calls this the **Standalone Vessel bridge directory**. It
expects an existing Python MCP bridge project selected explicitly by the user.
Before registering it, prepare:

- The actual bridge directory containing `server.py` and `pyproject.toml`, rather
  than a parent checkout. Keep those files available for later service launches.
- `uv` available on `PATH` when setup runs. Setup records the executable it finds
  and uses `uv run --directory` to launch the bridge in its own project.
- A separately running renderer and whatever connection settings, assets, and
  authentication that bridge requires. Installing Eidolon's Python dependencies
  does not establish a working renderer connection.
- If credentials are needed, either explicit app-owned credential references or
  a private readable UTF-8 source file and the exact fields to import. Setup does
  not search home folders or discover a bridge's `.env` file.
- The renderer's lifecycle URL, expected profile, and matching signing secret if
  you want lifecycle events. These are independent of MCP transport credentials.

Setup checks the selected entry-point files, `uv` availability, profile validity,
and explicitly selected credential fields. These checks register configuration;
they do not validate bridge dependencies, MCP capabilities, renderer reachability,
or authentication end to end. Opening the bridge can cause `uv` to resolve its
own dependencies; prepare that project according to its documentation first.

### Required MCP capabilities

The default route is the MCP server named `avatar`, selected by `avatar.server`.
For the supported avatar integration, the bridge must advertise these exact tools
and compatible schemas:

| Tool | Eidolon usage and bridge contract |
| --- | --- |
| `list_emotes` | Called with `{}` to discover available expressions and gestures. Return `expressions` and `emotes`, each a list of names or a dictionary keyed by names. |
| `get_body_state` | Read-only state inspection through the discovered MCP schema. Its details are bridge-specific. |
| `set_expression` | Cosmetic dispatch supplies `{"name": "<discovered expression>"}`. Extra required arguments would make automatic cues incompatible. |
| `play_emote` | Cosmetic dispatch supplies `{"emote": "<discovered gesture>"}`. Extra required arguments would make automatic cues incompatible. |

For catalog results, Eidolon accepts an MCP structured object (including a nested
`result` object), or JSON in text content. For example, the following is a
synthetic catalog payload, not a promise that a renderer has these assets:

```json
{"expressions": ["smile"], "emotes": {"wave": {}}}
```

Use the catalog names your bridge actually reports. The renderer remains
responsible for validating names, admitting actions, busy state, cooldowns, and
animation blending. An acknowledgement is not proof of visible animation or its
completion. No voice capability is provided by Eidolon's avatar coordinator.

## Register the bridge

Run commands from the Eidolon checkout root after [initial setup](INSTALLATION.md).
The paths and field names below are placeholders; replace them with explicit
private inputs for your installation. Use the same state/configuration selection
for every command.

If the bridge needs no new credential imports:

```sh
uv run eidolon setup --avatar-bridge /absolute/path/to/avatar-bridge
```

To copy selected existing credentials into Eidolon's Keychain storage:

```sh
uv run eidolon setup \
  --avatar-bridge /absolute/path/to/avatar-bridge \
  --avatar-credentials /absolute/path/to/private/bridge.env \
  --avatar-token-env STAGE_BODY_TOKEN \
  --avatar-signing-env STAGE_SIGNING_SECRET \
  --avatar-profile stage-42
```

`STAGE_BODY_TOKEN`, `STAGE_SIGNING_SECRET`, and `stage-42` are synthetic examples.
Use the exact environment field names and lifecycle profile your external service
expects. Do not rename its protocol settings to match Eidolon's branding.
All other `--avatar-*` setup flags require `--avatar-bridge`; `--project` instead
selects prompt assets and does not select the bridge.

Token and signing fields are optional and selected independently. Selecting
either requires an explicit credential file. Names must start with a letter or
underscore and contain only letters, digits, and underscores. Selected values
must be present and nonempty. The file parser accepts `NAME=value`, quoted
values, `export` prefixes, and trailing comments. It parses data without running
a shell or expanding variables. Unselected fields are ignored; with neither
field selected, setup never reads the source file, even if a path is supplied.

A selected token is stored as `keyring:vessel-body-token` in the MCP `env` mapping
under the exact selected field name. A selected signing value becomes
`avatar.signing_key_ref: keyring:vessel-signing`. These are existing credential
entry identifiers. The values must already match the external service; setup
does not configure the renderer's credentials. Raw values and the source file
path are not saved in Eidolon's configuration. Runtime reads credential references
rather than returning to the import file; later edits to that file require a new
explicit import or a credential update.

Registration enables `avatar.enabled` and creates/replaces the selected MCP
server with stdio transport, the four tools above, and a five-second timeout. It
preserves other servers, existing environment references for that route, and the
saved lifecycle URL. Omitted signing/profile flags preserve saved values;
`avatar.server` remains the saved route or `avatar` on a fresh installation.
Repeating registration replaces that route's launch settings and tool allowlist,
so review custom launch settings before repeating it.

Alternatively, `uv run eidolon setup --interactive` prompts for the bridge,
optional credential source and fields, and lifecycle profile. Blank answers keep
saved settings or skip imports; a blank bridge does not disable an existing
avatar. Combining `--interactive` with `--avatar-bridge` uses the explicit avatar
flags and skips those avatar prompts.

## Review private configuration and signing

Setup has no lifecycle-URL flag. Edit the saved private `config.yaml` if your
renderer uses another endpoint. This synthetic example shows the registration
shape after selecting both credential fields:

```yaml
mcp_servers:
  - name: avatar
    transport: stdio
    command: /absolute/path/to/uv
    args:
      - run
      - --directory
      - /absolute/path/to/avatar-bridge
      - python
      - /absolute/path/to/avatar-bridge/server.py
    env:
      STAGE_BODY_TOKEN: keyring:vessel-body-token
    enabled_tools: [list_emotes, get_body_state, set_expression, play_emote]
    enabled: true
    timeout: 5
avatar:
  enabled: true
  server: avatar
  profile: stage-42
  lifecycle_url: http://127.0.0.1:8766/events
  signing_key_ref: keyring:vessel-signing
  catalog_ttl: 300
```

Merge the relevant settings into your private file, preserving other configured
servers. Keep configuration and credential sources outside Git checkouts.
[Credential guidance](INTEGRATIONS.md#credential-references) also covers manually
configured `env:NAME` references and complete HTTP authorization header values.
A compatible HTTP MCP bridge can instead use the
[Streamable HTTP configuration](INTEGRATIONS.md#streamable-http-server); the
registration helper specifically generates a local stdio launch.

`avatar.server` must match the chosen MCP server name. `avatar.profile` selects
the external lifecycle profile and defaults to `companion`, including when
loading older configuration without this field. It must be nonempty single-line
UTF-8 text without controls and is sent literally. Participant display names do
not select or change either route.

The default `lifecycle_url` is `http://127.0.0.1:8766/events`; use the endpoint
actually supplied by your renderer. It accepts HTTP(S) without embedded URL
credentials. `avatar.signing_key_ref` must resolve to the same secret used by the
receiver to authenticate events. Without a signing reference, Eidolon records
lifecycle events as `unconfigured` and sends no lifecycle HTTP request; MCP
expressions and gestures have their own credential path. A body token alone does
not enable signed lifecycle delivery.

For compatibility, lifecycle requests use the existing header
`X-Hermes-Signature-256: sha256=<hex digest>`, where the digest is HMAC-SHA256 over
the exact UTF-8 JSON request bytes. This external wire identifier is preserved;
it does not introduce a dependency on another companion application. Events carry
`hook_event_name` (`pre_llm_call`, `post_llm_call`, or `on_session_end`), the
configured `profile`, `session_id`, `extra.turn_id`, a unique `delivery_id`, and a
UTC `timestamp`. They carry lifecycle metadata rather than conversation text.
Protocol implementers should use the maintained
[coordinator source](../src/eidolon/avatar/coordinator.py) and
[raw-byte signature tests](../tests/test_avatar.py) for serialization and delivery
behavior. Authentication enforcement on the receiving endpoint belongs to the
external integration.

## Activate and check

1. Start the renderer using its own instructions and confirm the bridge's
   required connection/authentication settings are available.
2. Finish registration and private configuration review. Close Eidolon's UI,
   then stop its service using the same state/configuration selection:

   ```sh
   uv run eidolon service stop
   uv run eidolon service status
   ```

   Wait until status reports stopped before starting again; launchd unloading
   can take time. Then open the saved MCP route:

   ```sh
   uv run eidolon service start
   uv run eidolon doctor
   uv run eidolon
   ```

   See [service controls](OPERATIONS.md#install-and-control-the-login-agent) for
   login-agent ownership and restart details.

3. Read running-service diagnostics through `doctor` or `/check`. `mcp.avatar`
   (or your selected route) reports discovered tools and its last error.
   `avatar` reports `enabled`, a catalog, and recent `activity`. A nonempty
   catalog establishes discovery, not successful lifecycle authentication or a
   rendered gesture. With the service stopped, doctor reports only avatar
   configuration and does not connect to the bridge.
4. Try an ordinary interactive response. Cues are optional, so a reply need not
   animate. Check activity for queued cues and diagnostics for dispatch results;
   confirm visible behavior in your own renderer. If lifecycle signing is
   configured, look for `lifecycle` entries with `delivered` and inspect the
   renderer's state. That status means HTTP success, not a visual assertion.

The terminal's avatar status says `connected` when the catalog has expressions or
gestures. It does not independently test the signing secret. Recent diagnostic
activity is bounded; `avatar.activity` includes the latest ten entries. Check
soon after the action you are investigating. No live bridge or renderer validation
is claimed by this guide; the repository tests use controlled MCP services,
synthetic credentials, fake body adapters, and mocked lifecycle HTTP responses.

## Response behavior and recovery

Eidolon discovers the catalog at connection/reconnection and refreshes it in the
background (`catalog_ttl` defaults to 300 seconds, minimum 5). An unavailable
catalog clears authorized cue names and is retried with backoff. Stale names do
not authorize actions after reconnecting.

[Provider presentation modes](PROVIDERS.md#match-capabilities-to-the-endpoint)
control how model output carries cues. Prefix or verified JSON modes separate
cosmetic cues from visible reply text, so cue acknowledgements do not add a model
continuation. Plain mode can use explicitly enabled cosmetic tools through the
normal tool loop. Clean reply text goes to the transcript; hidden cue controls
are kept out of ordinary conversation text.

Expressions and gestures have independent bounded queues, a three-second cue
lifetime, and at most one accepted cue of each kind per turn, shared with explicit
cosmetic calls and retries. Unsent cues are dropped on cancellation or a
superseded turn. Lifecycle start/end is deduplicated per turn, with cleanup on
completion, cancellation, or failure; session rollover sends a session-end event.
Scheduled jobs do not animate this avatar or drive its thinking lifecycle.

The renderer owns action admission and cooldowns. A gesture rejection does not
suppress an expression. When a command outcome is uncertain, Eidolon records
`unknown_outcome` without automatically retrying it. Reconnection and catalog
refresh do not replay stale actions. Lifecycle delivery failures are recorded
without automatic redelivery.

| Symptom | Check or recovery |
| --- | --- |
| Setup rejects the bridge | Select the directory containing both required files and make `uv` available on PATH. Setup does not infer a subdirectory. |
| Setup rejects credential selection | Check the explicit UTF-8 source file, exact field names, quoting, and nonempty values. Check macOS Keychain access if storing fails. |
| Avatar stays `connecting or unavailable` | Check `avatar.server` matches an enabled MCP route, all four tools are allowlisted/advertised, the renderer is running, and bridge dependencies/credentials work. Read the MCP error and bridge logs. |
| Catalog unavailable | Check `list_emotes` returns `expressions` and `emotes` with compatible types. Allow background recovery after repairing the bridge. |
| Lifecycle `unconfigured` | Set `signing_key_ref` to a credential matching the receiver if thinking/session events are wanted. |
| Lifecycle `unavailable` | Check URL/port, receiver availability, signature secret, expected profile, and receiver authentication logs. MCP success does not verify this endpoint. |
| Cue queued but no animation | `queued` is acceptance into Eidolon's queue. Check `sent`/`acknowledged`/`expired`/`unknown_outcome` entries and the bridge result; then check renderer admission and assets. |
| Unknown-name or cooldown rejection | Let catalog refresh authorize later turns; respect renderer cooldowns. Avoid forcing repeated actions. |
| Malformed presentation or no visible reply | Use `presentation: plain` in the provider profile and retry a conversation. Private `diagnostics/presentation/` may contain raw model output; review it before sharing. |

To update a copied credential, run `uv run eidolon secret set vessel-body-token`
or `uv run eidolon secret set vessel-signing` and enter the new matching value
privately. If you use another reference, update that entry instead. Restart the
service to reopen bridge connections after rotation. Inspect the renderer's state
before deliberately repeating any action whose result was uncertain.

To disable cues and lifecycle events, open `/preferences`, choose **Avatar**, turn
off **Use the avatar**, and save with Ctrl+S; or set `avatar.enabled: false` in
private YAML. The MCP server has its own enable switch: to stop the bridge
connection and its tool exposure too, set that server's `enabled: false`. Close
the UI and restart the Eidolon service after manual edits. Retain configuration
and credential references for later reactivation. Disabling Eidolon does not
close an independently running renderer or remote server; manage those separately.

Implementation references: [bridge registration](../src/eidolon/avatar/setup.py),
[configuration defaults and validation](../src/eidolon/config.py),
[cosmetic tool dispatch](../src/eidolon/tools/registry.py), and
[setup CLI tests](../tests/test_cli.py).
