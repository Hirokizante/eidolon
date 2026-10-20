# Companion skills

[Back to Eidolon](../README.md)

Eidolon ships with no installed skills. `/skills` (Alt+3) opens a private,
single-column library over chat. Enabled skills provide reusable instructions for
conversations and scheduled tasks. For personality that should always apply, use
[private prompts](PERSONALIZATION.md#edit-personality-and-instructions). For tool
capabilities, see [Providers](PROVIDERS.md#match-capabilities-to-the-endpoint).

## Create and manage

- Ctrl+N or **New** creates a skill. Give it a name using lowercase letters,
  numbers, and single hyphens (up to 64 characters), a description of when to use it (up to 1,024 characters),
  and Markdown instructions (up to 12,000 characters). New skills start enabled;
  uncheck **Enabled** before saving to keep one off.
- Enter or **Edit** opens the selection. Ctrl+S or **Save** writes the skill.
  Names stay fixed after creation; descriptions and instructions remain editable.
- Ctrl+T or **Turn on** / **Turn off** saves a switch immediately. Disabled skills
  remain editable but are excluded from the companion's discovery context and skill reads.
- Ctrl+F filters names and descriptions. Ctrl+L refreshes the library or discards
  edits in the current editor. Esc returns to the list, clears a filter, then closes.
  Unsaved skill drafts remain available during this terminal app session.

Changes apply to subsequent responses without restarting the background service.
Already-loaded instructions can remain in a response that is in progress.
The composer draft, selection, and prior focus survive the menu.
At 80×24 and 60 columns the list and editor share one pane. At 48×12,
the button row hides, the shortcuts remain visible, and fields scroll into view
as Tab moves focus. Below that size, the menu asks for a resize and retains state.

## Files and companion use

Files live under `~/Library/Application Support/Eidolon/`, or an isolated
`EIDOLON_HOME` outside any Git checkout:

```text
skills/
  daily-plan/
    SKILL.md
    references/          # optional supporting text
skills.json              # per-skill true/false settings
```

An example `SKILL.md`:

```markdown
---
name: daily-plan
description: Help organize a day. Use when the user asks to plan tasks or priorities.
---

Ask which tasks matter most and how much time is available.
Suggest a short plan, with breaks and space for unexpected work.
Check that the plan fits the user's energy and constraints.
```

You can copy a skill directory into the private `skills/` folder, refresh the menu,
and enable it. Imported files start off. The frontmatter name must match the
directory name. Optional frontmatter is preserved when editing through the menu.
Invalid skills show an **Invalid** state and stay unavailable to the companion;
fix their file and reload. Saving an editor opened before an external file change
fails with a reload instruction instead of overwriting the newer file.

With tool calling enabled, the companion initially receives only enabled skill
names and descriptions. It chooses relevant workflows and calls `read_skill` to
load their instructions. The engine places those instructions in the next model
request's active context. `read_skill` can also read supporting text using a path
relative to that skill's directory. With a text-only provider, enabled instructions
are included directly because the model cannot request them through a tool.
Skill context counts against the model token window along with prompts, history,
and tools. Large instructions can leave less room for conversation or cause an
overflow; see [Model metadata](MODEL_METADATA.md#full-request-token-budgeting).
Scheduled jobs with tool allowlists need `read_skill` in that allowlist to discover
skills through tool calls.

Skills provide instructions for the app's existing tools and do not enable agent
mode or grant new file/tool access. In companion mode they cannot execute shell
commands, run bundled scripts, or install dependencies. When you explicitly enable
`/agent`, the companion can use the agent tools to carry out those workflows.
Supporting reads reject traversal, symlinks, special files, and binary data.
Files are bounded to 32,000 bytes and supporting text to 12,000 characters.
Created files use mode `0600` and directories `0700`. Skill bodies stay out of
transcripts and the memory outbox; private request inspection can show the context
sent to the model. Switching a skill off does not erase earlier conversation text.

## Troubleshoot and understand the limits

| Symptom | What to check |
| --- | --- |
| Imported skill stays off | Imports default off; refresh the library and use Ctrl+T to enable it |
| Invalid skill | Check YAML frontmatter, matching directory/name, nonempty description/body, and size limits; fix the file and reload |
| Cannot save after an external edit | Preserve any draft text you need, then Ctrl+L to reload the newer file before editing again |
| Companion does not use an enabled skill | Make its description specific about when it applies; tool-capable models choose when to call `read_skill` |
| Scheduled job cannot discover skills | Its tool allowlist must include `read_skill` when tool calling is enabled |
| Skill tries to run an installer or script | This library loads instructions and bounded text resources; it is not an executable skill installer |
| Request exceeds context capacity | Shorten instructions/references or adjust the actual model/output budget; text-only profiles include all enabled bodies |

Only reviewed instructions should be enabled: a loaded skill becomes model
instructions and can affect responses and tool choices. Optional frontmatter is
preserved, but client-specific hooks, dependency installers, and permission
features from other applications do not run here. Supporting reads require
nonhidden relative paths inside the skill folder and reject traversal and symlinks.
Turning a skill off removes it from later discovery; it does not retract context
already loaded into an active response or erase older conversation text.

The authoritative implementation is [SkillLibrary](../src/eidolon/skills.py) and
[engine skill loading](../src/eidolon/engine/conversation.py), with regression
coverage in [library tests](../tests/test_skills.py) and
[UI tests](../tests/test_ui_skills.py). The local library starts empty; no external
skill registry or network download is needed to create one.
