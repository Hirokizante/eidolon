# UI Design Guide

Eidolon should feel like a quiet place to spend time with your configured companion. This is the intended design; existing screens may still need to migrate. Consult the `tui-design` skill for implementation details.

## Immersion: Chat Is Home

- Give the conversation most of the terminal. Anchor the composer at the bottom; avoid persistent navigation rails, tab bars, and dashboard panels.
- Open secondary menus and spaces as modals over chat wherever practical, including History, Skills, Activity, settings, and help. Anchor dialogs immediately above the composer; grow upward and scroll internally without moving the conversation.
- Keep chat and composer visible with gentle background dimming. The modal owns input; the underlying composer cannot accept typing or send messages.
- Frame modal dialogs, including `/models` and `/connect`, with a thin, single-cell border in the theme's primary color to distinguish them from surrounding content. Use the foreground color in monochrome and ASCII borders in ASCII mode; keep controls inside the dialog flat and unboxed.
- Preserve the draft, cursor, selection, and scroll position. Dismissal restores prior focus; background responses never steal it.
- Use larger overlays for reading and editing. Reserve dedicated views for tasks that cannot remain usable in an overlay, with a clear return path and preserved chat state.

## Keyboard Navigation First

- Make every action keyboard-reachable. Use slash commands and `Ctrl+P` for discovery, `F1` or `/help` for complete help, and one compact line of contextual shortcuts.
- Use arrows for lists, `Enter` to select, and `Tab` / `Shift+Tab` in visual order. Trap focus inside modals. `Esc` backs out or dismisses without silently saving.
- Printable shortcuts yield to text fields. Preserve normal editing, configurable send/newline bindings, `Ctrl+C` cancellation, `Ctrl+Q` exit, and documented suspend and undo behavior.
- Show unmistakable focus and selection, including in monochrome. Retain editor drafts across dismissal or ask before discarding meaningful edits. Validate on blur or submit, with errors beside fields.

## Flat, Compact Controls

Only flat buttons may be used: text on a simple surface, without raised edges, bevels, shadows, or decorative borders. Fit labels with minimal padding; remove inherited minimum sizes that add empty space. Never stretch buttons across a row. Prefer commands when buttons add no value. Make focus, disabled, and destructive states readable.

## Calm Visual Language

- Use existing semantic theme tokens: readable text, muted metadata, subtle surfaces, and a restrained accent. Reserve warning/error emphasis for actionable problems.
- Frame each message with a thin, rounded border matching its speaker title: the user name in the accent color and the companion name in the primary color (defaults: "You" / "Companion"). Use the exchange's recorded names when available; older messages use current configured labels without changing their bodies. Keep padding modest and avoid nested borders, repeated icons, and redundant labels.
- Show each message's local date and time in its lower-right border, using muted text without adding a content row. Preserve the original send time for user messages and the completion time for assistant messages.
- Keep tool details in Activity and concise progress/errors near the composer. Show a small spinner beside progress during an active turn; stop it when work ends and honor disabled animations. Avoid blinking, decorative animation, and routine success toasts.
- Keep typing and cancellation responsive. Follow streaming output only when already at the bottom; never pull readers away from earlier messages.
- Support `NO_COLOR`, monochrome, and `EIDOLON_ASCII=1`. Color alone must not carry meaning; special icon fonts must not be required. Wrap prose using terminal cell widths, including emoji, CJK, and combining marks.

## Identity Editing & Labels

`/preferences` has an **Identity** category with **Your name** and **Companion's name**.
Use flat text fields and shared validation: 1–80 Unicode code points after trimming
surrounding spaces, valid UTF-8, no line breaks, terminal controls, Unicode
line/paragraph separators, or bidi override/isolate controls. Equal names are allowed;
roles remain authoritative. Show errors beside fields; short layouts put the
focused field's label and error in the dialog border so they stay visible while scrolling.

Ctrl+S or Apply saves edited fields; Esc retains an unsaved draft, and Discard edits
reloads saved values. Preserve the underlying composer draft, cursor, selection,
focus, and scroll intent. A save failure keeps active labels and the editable draft.
Use the configured companion name or neutral language in supporting copy. Active
operations retain their captured names; a rename applies to later operations.

Render display names literally, including Rich/Markdown-like punctuation. Wrap or
shorten labels by terminal cell width without clipping essential controls. Keep
full names in settings and provider inputs. Review long names, CJK, combining marks,
emoji, markup-like names, and mixed recorded/current attribution in color and
monochrome/ASCII. Identity labels are independent of product branding and avatar routing.

## Small Terminals & Review

At 80×24, preserve chat, composer, and modal controls. At 60 columns, use single-column overlays, drill-down details instead of side previews, and reduced margins. Below 48×12, show the resize message and preserve state. Never clip essential controls.

Review keyboard-only use, focus restoration, drafts, resize, and empty/loading/error/disconnected states at these sizes and in monochrome/ASCII. Remove chrome that does not help reading, navigation, or understanding state.
