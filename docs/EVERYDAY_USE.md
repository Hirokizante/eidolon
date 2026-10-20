# Everyday conversations

[Back to Eidolon](../README.md)

After [installation and your first conversation](INSTALLATION.md), launch Eidolon
with `uv run eidolon` from the checkout root. Today's room opens with the composer
focused. The terminal UI talks to the background service, which owns saved history
and running work.

## Write and navigate

| Action | Control |
| --- | --- |
| Send a message | Enter by default; Ctrl+Enter also sends |
| Add a line | Shift+Enter by default |
| Find commands | Type `/` in the composer, or Ctrl+P from any view |
| Send text beginning with a literal slash | Begin with `//` |
| See all current commands and keys | `/help` or F1 |
| Move focus | Tab / Shift+Tab |
| Undo a text edit | Ctrl+Z in text editors |
| Return from a detail or menu | Esc; repeated Esc returns toward chat |
| Cancel the active response | `/cancel` or Ctrl+C |
| Refresh the current view | `/reload` or Ctrl+L; in editors this reloads saved text |
| Suspend the terminal UI to the shell | `/suspend` or Alt+Z; run `fg` to return |
| Close the terminal UI | `/quit`, `/exit`, or Ctrl+Q |

Ctrl+P preserves the message draft. Menus also retain their selection and focus
where applicable. Shortcuts depend on the terminal forwarding the key combination;
use the slash command when a terminal intercepts an Alt key or image-paste shortcut.
Forms support terminals down to 48×12. Below that size, resize to recover the form
and its retained draft.

Closing the UI leaves the service running, including enabled scheduled jobs.
Cancelling a response stops the client operation; it does not guarantee that a
remote server has stopped computing. For service setup and stopping the service,
use [Background operation and scheduled jobs](OPERATIONS.md).

## Read and continue earlier rooms

Open `/history` (Alt+2). Ctrl+F filters the list, Enter reads the selected room,
and Esc returns to the list. Reading does not change the active conversation.
Ctrl+Enter explicitly continues the selected room in chat. Use `/today` to return
to today's conversation. Ctrl+L refreshes the history list.

Local conversation history remains available when external memory is disabled.
Completed exchanges are stored in the private SQLite database. Names recorded
with messages remain attached to them; see [Personalization](PERSONALIZATION.md)
for how older messages and later renames display.

## Understand a reply or recover a waiting message

Open `/activity` (Alt+4) to review tool inputs, sources, results, errors, and timing.
Enter reads an operation; Esc goes back. `/inspect` (Ctrl+R) opens the latest
captured exact model request as formatted JSON, including its tool round and model.
Inside the inspector, Ctrl+R refreshes, Ctrl+Y copies raw JSON, and Ctrl+S exports
it to private state. It captures a dispatched request rather than a prediction of
the next request. Prompt bodies, user text, tool results, and image content can be
present; review an export before sharing it.

`/check` checks provider, memory, and avatar connections. An inventory check does
not establish that every model feature works; see [Providers](PROVIDERS.md).
When enabled memory recall fails, the message waits for an explicit choice:
`/retry` retries recall, while `/retry without-memory` sends that waiting message
without recalled memories for this reply only. `/clear-attachments` removes
images from the composer draft and any waiting message. Use `/memory` to review
memory supplied to replies and current-life notes; its controls are separate from
local chat history. See [optional memory](MEMORY.md) for activation, delivery
readiness, queued saves, and disabling, and
[current-life notes](CURRENT_LIFE_SUMMARIES.md) for summaries and manual notes.

## Change everyday preferences

Open `/preferences` (Alt+6), choose a category, edit, and use **Apply** or Ctrl+S.
Changes persist in private `config.yaml`. Esc/**Close** retains a draft only for
the current terminal app session; **Discard edits** reloads saved settings across
categories. Opening a form or running its optional **Test** does not save edits.

| Category | Controls and defaults |
| --- | --- |
| Identity | **Your name** and **Companion's name**; see [Personalization](PERSONALIZATION.md) |
| Conversation | **Send a message** chooses Enter-send/Shift+Enter-newline or Ctrl+Enter-send/Enter-newline; **Follow streaming replies** and **Show status bar** default on |
| Status bar | Choose ordered components once each, or **Hidden** for unused positions |
| Daily room | **Timezone** is a valid IANA zone; **New day starts at** is a local hour from 0–23, default 4 |
| Memory / Current life | Independent recall, saving, automatic-update, and reply-inclusion controls; new installations leave these off |
| Auxiliary model | Background current-life inference; see [Auxiliary model](AUXILIARY_MODEL.md) |
| Vision model | Direct dialogue vision or a separate image analyst; see [Providers](PROVIDERS.md#use-images-with-a-model) |
| Avatar | **Use the avatar** requires the optional integration to be configured |
| Web search | Search provider, credentials, limits, and provider-specific filters |

The status bar starts with **Model and reasoning**, **Context window**, **Cache hit
rate**, and **Tokens per second**. **Provider connection**, **Memory status**, and
**Avatar status** are additional choices. Following replies scrolls with streaming
text while you are at the bottom. Scroll up to read earlier text without following.
Usage can be unavailable when a server omits it. A `~` marks a context estimate;
see [model limits and usage](MODEL_METADATA.md).

Setup detects the machine's IANA timezone, falling back to UTC. The daily boundary
uses that configured zone and reset hour. Changes to daily settings are reconciled
when the service is idle; active work finishes before the service switches rooms. In general, saved model/settings changes apply
to subsequent work at a safe boundary rather than replacing adapters used by an
active response. Identity and prompt snapshots have their own behavior described
in [Personalization](PERSONALIZATION.md).

## Use images, tools, and reusable workflows

Cmd+V or `/paste` reads the macOS clipboard. Images attach to the draft; text
pastes as text. Attach up to four PNG images (10 MiB each), add an optional question,
and send. If image paste is intercepted, use `/paste`. Image support and separate
vision selection are described in [Providers](PROVIDERS.md#use-images-with-a-model).

`/agent on` enables file editing and command tools; `/agent off` revokes them and
stops managed commands. `/agent` toggles the mode. The selected provider must have
tool calling enabled. Skills do not grant permissions or enable this mode; see
[Skills](SKILLS.md). Agent mode is runtime state, not a saved preference, and jobs
have an independent **Run in agent mode** choice.

Use `/skills` (Alt+3) for reusable instructions, `/theme` (Alt+8) for colors, and
`/prompts` (Alt+5) for personality. `/jobs` (Alt+7) opens scheduled jobs and run
history; use `/help` for its editor, enable/pause, retry, and deletion keys. These
menus preserve the conversation draft while you work.
