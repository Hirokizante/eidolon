"""Slash commands shared by the composer, palette, and help."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    key: str = ""
    arguments: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"/{self.name}" + (f" {self.arguments}" if self.arguments else "")

    @property
    def shortcut(self) -> str:
        return "+".join(
            {"ctrl": "Ctrl", "alt": "Alt"}.get(k, k.upper()) for k in self.key.split("+")
        )


COMMANDS = (
    Command("history", "Browse past rooms; Enter to read", "alt+2"),
    Command("skills", "Create, edit, and turn companion skills on or off", "alt+3"),
    Command("activity", "Read tool results and response activity", "alt+4"),
    Command("memory", "View memories sent with replies and current life notes"),
    Command("agent", "Toggle agent mode for file editing and commands", arguments="[on|off]"),
    Command("prompts", "Edit a prompt file", "alt+5", "[name]"),
    Command("preferences", "Change how the app behaves", "alt+6"),
    Command("jobs", "Create, edit, and manage cron jobs and agent tasks", "alt+7"),
    Command(
        "theme", "Preview, choose, or customize a theme", "alt+8", "[name|custom]", ("themes",)
    ),
    Command("connect", "Add or edit an OpenAI-compatible provider"),
    Command("models", "Choose dialogue provider, model, reasoning effort, and token limits"),
    Command("paste", "Paste an image or text from the macOS clipboard"),
    Command("clear-attachments", "Remove images from drafts and waiting messages"),
    Command("inspect", "View the previous exact model request as formatted JSON", "ctrl+r"),
    Command("reload", "Reload the current view from the service", "ctrl+l"),
    Command("check", "Check provider, memory, and avatar connections"),
    Command("today", "Return to today's room"),
    Command(
        "retry", "Retry a waiting message; without-memory skips failed recall for this reply",
        arguments="[without-memory]",
    ),
    Command("cancel", "Cancel the active response", "ctrl+c"),
    Command("help", "All commands and keyboard shortcuts", "f1"),
    Command("suspend", "Pause app for shell; fg brings it back", "alt+z"),
    Command("quit", "Close the room; leave the service running", "ctrl+q", aliases=("exit",)),
)


def find_command(name: str) -> Command | None:
    return next((c for c in COMMANDS if name in (c.name, *c.aliases)), None)


def matching_commands(query: str) -> list[Command]:
    query = query.strip().removeprefix("/").casefold()
    if not query:
        return list(COMMANDS)
    # Names rank ahead of description matches.
    names = [c for c in COMMANDS if any(n.startswith(query) for n in (c.name, *c.aliases))]
    return names + [
        c
        for c in COMMANDS
        if c not in names
        and all(
            word in f"{c.name} {' '.join(c.aliases)} {c.description}".casefold()
            for word in query.split()
        )
    ]


def command_option(command: Command, *, compact: bool = False) -> Option:
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(width=12, no_wrap=True, overflow="ellipsis")
    row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    values = [Text("/" + command.name, style="bold"), Text(command.description)]
    if not compact:
        row.add_column(width=6, justify="right", no_wrap=True)
        values.append(Text(command.shortcut, style="dim"))
    row.add_row(*values)
    return Option(row, id=command.name)


class CommandMenu(ModalScreen[str | None]):
    """A keyboard-first menu which also accepts full slash commands."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("tab", "complete", show=False, priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="command-dialog"):
            yield Static("Room commands", classes="dialog-title")
            yield Input(placeholder="Search or type a /command", id="command-input")
            yield OptionList(id="command-options", compact=True)
            yield Static(
                "↑↓ choose · Tab complete · Enter run · Esc close",
                id="command-hint",
                classes="hint",
            )

    def on_mount(self):
        self.filter_commands("")
        self.query_one(Input).focus()

    def on_resize(self, event):
        if self.is_mounted:
            self.filter_commands(self.query_one(Input).value, event.size.width)

    def filter_commands(self, query: str, width: int | None = None):
        width = width or self.size.width
        options = self.query_one(OptionList)
        options.clear_options()
        matches = matching_commands(query.split(" ", 1)[0] if query.startswith("/") else query)
        options.add_options(command_option(c, compact=width < 80) for c in matches)
        options.highlighted = 0 if matches else None
        self.query_one("#command-hint", Static).update(
            (
                "↑↓ move · Tab fill · Enter · Esc"
                if width < 60
                else "↑↓ choose · Tab complete · Enter run · Esc close"
            )
            if matches
            else "No matching command. Try history, theme, or help."
        )

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed):
        self.filter_commands(event.value)

    @on(Input.Submitted)
    def run_command(self, event: Input.Submitted):
        text = event.value.strip().removeprefix("/")
        name = text.split(" ", 1)[0]
        if find_command(name):
            self.dismiss("/" + text)
        else:
            options = self.query_one(OptionList)
            if options.highlighted is not None:
                self.dismiss("/" + str(options.get_option_at_index(options.highlighted).id))

    @on(OptionList.OptionSelected)
    def option_selected(self, event: OptionList.OptionSelected):
        self.dismiss("/" + str(event.option.id))

    def action_move(self, direction: int):
        options = self.query_one(OptionList)
        (options.action_cursor_down if direction > 0 else options.action_cursor_up)()

    def action_complete(self):
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            command = find_command(str(options.get_option_at_index(options.highlighted).id))
            field = self.query_one(Input)
            field.value = f"/{command.name}" + (" " if command.arguments else "")
            field.cursor_position = len(field.value)

    def action_close(self):
        self.dismiss(None)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,f1", "close", "Close", priority=True)]

    def __init__(self, send: str, newline: str):
        super().__init__()
        self.send_key = send
        self.newline_key = newline

    def compose(self) -> ComposeResult:
        rows = "\n".join(
            f"- **{c.usage}**" + (f" · {c.shortcut}" if c.key else "") + f" — {c.description}"
            for c in COMMANDS
        )
        text = (
            "## Make yourself at home\n\n"
            "Type `/` in your message to find commands. **Ctrl+P** opens the same menu "
            "from any view and keeps your message draft.\n\n"
            f"**{self.send_key}** sends · **{self.newline_key}** adds a line · "
            "**Ctrl+Enter** also sends. Start a message with `//` to send a literal slash.\n\n"
            "**Cmd+V** or `/paste` attaches an image from the macOS clipboard. "
            "Paste up to four images, add an optional message, and send. "
            "`/clear-attachments` removes images from your draft or a waiting message. "
            "If your terminal does not forward image paste, use `/paste`.\n\n"
            "**Tab / Shift+Tab** move focus. **Enter** reads a selected room, note, or activity. "
            "**Esc** returns to the list, then the conversation. **Ctrl+F** focuses a list filter. "
            "**Ctrl+Z** undoes text edits. **Alt+Z** pauses this UI process for the shell; "
            "`fg` restores it. **Ctrl+Q** closes the UI and leaves the background service "
            "running.\n\n"
            "In History, **Enter** reads a room and **Ctrl+Enter** continues it in chat. "
            "Use `/today` to return to today's conversation. In prompt editors, "
            "**Ctrl+S** saves and **Ctrl+L** reloads the saved file.\n\n"
            "If memory recall fails, `/retry` tries again. "
            "`/retry without-memory` sends the waiting message without recalled memories "
            "for that reply only.\n\n"
            "`/memory` shows the memories sent with each request. Current life has separate "
            "Notes and Run history sections, with Refresh current life on the main screen. "
            "In Notes, **Ctrl+N** creates a note with your chosen label. Open a note, then "
            "**Ctrl+E** edits or **Ctrl+D** deletes with confirmation. **Ctrl+S** saves; "
            "**Esc** keeps an unsaved draft. User-created notes are marked and protected "
            "from maintenance; editing an automatic note still allows maintenance updates. "
            "Use `/preferences` for recall and saving controls.\n\n"
            "### Scheduled jobs\n\n"
            "`/jobs` opens the job manager. **Ctrl+N** creates, **Enter** edits, "
            "**Ctrl+T** enables or pauses, **Ctrl+D** deletes with confirmation, "
            "**Ctrl+R** opens runs, **Ctrl+F** filters, and **Ctrl+L** refreshes the list. "
            "**Ctrl+S** saves a job; **Esc** keeps its draft and returns to the list. "
            "**Ctrl+L** in the editor replaces the draft with the saved job. "
            "In runs, **Enter** opens details and **Ctrl+Y** retries a failed run. "
            "Choose **Run in agent mode** in each job's form to enable file editing and "
            "commands for that job independently of chat.\n\n"
            "### Commands\n\n" + rows
        )
        with Vertical(id="help-dialog"):
            with VerticalScroll():
                yield Markdown(text)
            yield Static("Esc close · Ctrl+P commands", classes="hint")

    def action_close(self):
        self.dismiss()
