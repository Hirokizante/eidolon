# Personalize your companion

[Back to Eidolon](../README.md)

Use `/preferences` for participant names and `/prompts` for personality and
instructions. These are separate controls: changing a display name does not
rewrite your prompts or invent a biography. For initial setup, see
[Installation](INSTALLATION.md); for appearance and reusable workflows, see
[Themes](THEMES.md) and [Skills](SKILLS.md).

## Set participant names

1. Open `/preferences` (Alt+6) and choose **Identity**.
2. Edit **Your name** and **Companion's name**.
3. Choose **Apply** or press Ctrl+S. Esc or **Close** retains an unsaved draft
   for this terminal app session; **Discard edits** reloads saved preferences.

Fresh installations store `You` and `Companion`. Both fields require strings
containing 1–80 Unicode code points after surrounding whitespace is trimmed.
Unicode names, spaces, punctuation, and identical participant names are allowed.
Invalid UTF-8, control characters, line/paragraph separators, and directional
embedding/isolate controls are rejected. Markup-like names display literally;
they are not formatting or prompt instructions.

Names persist in the private `config.yaml`:

```yaml
identity:
  user_name: Alex
  companion_name: Cedar
```

Apply affects subsequent operations without restarting the service. A reply,
scheduled execution, or maintenance attempt already running keeps the names it
captured at its start, including its later tool rounds. Newly recorded messages
retain that attribution. Older messages without recorded names display the
current configured labels. Renaming does not rewrite message bodies, memory
records, queued delivery payloads, captured requests, or saved evidence.

The application supplies the names to the model as display-name data and tells
it to prefer them over conflicting names inside custom prompts. Other personality,
pronoun, and relationship instructions in those prompts remain intact. Review
your own prompt text for consistency; this precedence rule does not guarantee
that a model will always follow it. Display names do not change avatar routes,
memory banks, credentials, or storage identifiers.

## Edit personality and instructions

Open `/prompts` (Alt+5), select a file, and edit its Markdown. You can open a
file directly with `/prompts SOUL`; names are case-insensitive.

| File | Purpose | Generic starting content |
| --- | --- | --- |
| `SYSTEM.md` | Base behavior and tool/credential rules | Clear, honest help; no invented personal facts or shared history |
| `SOUL.md` | Personality, tone, and conversational style | Warm, thoughtful, curious, candid, and adaptable |
| `USER.md` | Reviewed background about the user | No configured personal profile or assumed relationship |
| `POST_HISTORY.md` | Instructions placed after history or appended to the current user message | Respond naturally to the latest request and distinguish facts from uncertainty |

Ctrl+S saves the selected file; Ctrl+L reloads its saved contents, replacing the
editor draft. Unsaved edits stay in the editor while you navigate the running
terminal app; save before quitting. The service reads a snapshot of all four
files at generation start. Saved edits affect the next response, while a response
already running keeps its original prompt snapshot.

For example, a private `SOUL.md` could say:

```markdown
Be calm, curious, and direct. Prefer short answers unless I ask for detail.
When helping me plan, ask about my available time before suggesting a schedule.
Do not invent shared experiences or assume facts I have not provided.
```

Put stable, reviewed facts in `USER.md` only when you want them supplied as
background. These files are sent to the configured dialogue model. They are
private local files, but their contents become model input; keep credentials out
of them. Request inspection can also expose their contents locally.

## Find and preserve your private prompts

The normal location is `~/Library/Application Support/Eidolon/prompts/`, with a
different private state location possible through `EIDOLON_HOME` or explicit
configuration. Keep state outside Git checkouts. See
[state and credentials](INSTALLATION.md#configuration-state-and-credentials).

Package files under `src/eidolon/defaults/` are seed data. Setup/load copies
prompts into private state and saves their paths in `config.yaml`. Existing private
prompts are retained. Importing configured custom prompts preserves their contents,
including original line endings; a conflicting private destination requires review
instead of an overwrite. A missing configured prompt is an error, not permission
to reset personality to the generic defaults. Prompt destinations and their
directory must not be symlinks. Private prompt files use permissions `0600` and
their directory uses `0700`.

If a prompt is missing, restore its configured source or private copy before
continuing. If migration reports a conflict, compare both files and deliberately
resolve which content to keep. Renaming in Identity never performs a search and
replace inside prompts. Editing private Markdown directly also works; save it
before the next generation, and reload any open editor before editing further.

For the request ordering and provider-specific post-history setting, see
[Prompt assembly and caching](PROMPT_CACHING.md).
