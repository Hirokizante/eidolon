# Themes

[Back to Eidolon](../README.md)

Eidolon includes ten palettes and one saved Custom slot. No theme installation
or network connection is required. For other settings, see
[Everyday use](EVERYDAY_USE.md#change-everyday-preferences).

`/theme` (Alt+8) opens a modal immediately above the composer. Up/Down and the mouse
wheel preview palettes throughout the room without saving. Enter or Ctrl+S applies
the highlighted theme; Esc restores the saved theme. Chat, composer drafts, selection,
and focus survive the menu.

Customize (Ctrl+E) starts with the highlighted palette. Each semantic color has a
labeled field; use `#RRGGBB`, a Textual color name, or a blank field to inherit the base.
Valid edits preview immediately. Invalid colors show errors beside their fields and
cannot be saved. Ctrl+S stores the Custom palette. Esc goes back to the list, then
closes; Close dismisses directly. Unsaved custom drafts survive dismissal until saved
or discarded with Ctrl+L. There is one saved Custom slot, which can be edited again.
Choosing a bundled theme keeps the saved Custom slot available. `/theme custom`
reapplies it; `/theme nord` and other IDs below select bundled palettes directly.

The active palette, overrides, and saved Custom slot live in the app's private
`config.yaml`. They survive app and service restarts. Runtime theme selection does
not fetch anything from the internet.

## Bundled palettes

Colors are adapted from [TerminalColors](https://terminalcolors.com/)' Alacritty
downloads. Background and foreground are preserved; ANSI blue/cyan/magenta and
green/yellow/red provide primary/secondary/accent and success/warning/error.
Gruvbox uses its bright ANSI set for readable dark-theme accents. Surface and panel
blend the source background toward its foreground by 4% and 8%, respectively.
This maps terminal colors onto the app's existing semantic tokens.

| Palette | ID | Source |
| --- | --- | --- |
| Catppuccin Mocha | `catppuccin-mocha` | [TerminalColors](https://terminalcolors.com/themes/catppuccin/mocha/) |
| Dracula | `dracula` | [TerminalColors](https://terminalcolors.com/themes/dracula/default/) |
| Nord | `nord` | [TerminalColors](https://terminalcolors.com/themes/nord/default/) |
| Gruvbox Dark | `gruvbox-dark` | [TerminalColors](https://terminalcolors.com/themes/gruvbox/dark/) |
| Tokyo Night | `tokyo-night` | [TerminalColors](https://terminalcolors.com/themes/tokyo-night/default/) |
| Rosé Pine | `rose-pine` | [TerminalColors](https://terminalcolors.com/themes/rose-pine/default/) |
| Everforest Dark | `everforest-dark` | [TerminalColors](https://terminalcolors.com/themes/everforest/dark/) |
| One Dark | `one-dark` | [TerminalColors](https://terminalcolors.com/themes/one/dark/) |
| Solarized Light | `solarized-light` | [TerminalColors](https://terminalcolors.com/themes/solarized/light/) |
| GitHub Light | `github-light` | [TerminalColors](https://terminalcolors.com/themes/github/light/) |

Tokyo Night is the default. Saved legacy Moon, Dawn, and Ink configurations map to
Tokyo Night, Solarized Light, and Nord, keeping overrides. Existing custom overrides
are retained in the Custom slot when a new theme is saved.

## Troubleshoot colors and small terminals

Invalid color values show an error and cannot be saved. Correct the field or
leave it blank to inherit the base palette. If saving fails, the live preview
can remain visible without becoming the saved theme; retry saving or close the
picker to restore the saved appearance. Ctrl+L discards the custom draft.

The picker and custom fields scroll at smaller terminal sizes; use Tab to bring
the focused field into view. Forms require at least 48×12 and retain state while
a resize message is shown below that size. `EIDOLON_ASCII=1` selects ASCII borders
and hints; monochrome mode follows `NO_COLOR`. These affect rendering, not the
saved palette. A terminal's own theme does not replace Eidolon's configured one.

Bundled IDs, colors, and source attribution live in
[themes.py](../src/eidolon/themes.py). Preview/cancel, custom persistence, failure,
and resize behavior are covered in [theme UI tests](../tests/test_ui_themes.py).
