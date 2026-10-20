"""Readable activity summaries; transport JSON stays out of the conversation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Markdown, Static

from eidolon.ui.forms import FormModal


LABELS = {
    "memory_recall": "Recall memories",
    "memory_reflect": "Reflect on memories",
    "web_search": "Search the web",
    "fetch_webpage": "Read a webpage",
    "list_files": "Browse files",
    "read_file": "Read a file",
    "search_files": "Search files",
    "read_skill": "Read a skill",
    "write_file": "Write a file",
    "edit_file": "Edit a file",
    "run_command": "Run a command",
    "read_command_output": "Read command output",
    "write_command_input": "Send command input",
    "stop_command": "Stop a command",
    "list_commands": "List commands",
    "avatar": "Avatar cue",
    "presentation": "Reply format",
}
STATUSES = {
    "started": "Working",
    "completed": "Done",
    "error": "Failed",
    "bypassed": "Skipped",
    "queued": "Queued",
    "skipped": "Skipped",
    "unavailable": "Unavailable",
    "fallback": "Fallback",
}


def readable_name(value: str) -> str:
    if value.lower() in {"url", "id", "mcp"}:
        return value.upper()
    return value.replace("_", " ").replace("-", " ").strip().capitalize()


def unpack_result(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def brief(value, limit: int = 120) -> str:
    """Bound content before measuring it in the actual terminal renderer."""
    if isinstance(value, dict):
        value = value.get("error") or value.get("title") or value.get("message") or ""
    elif isinstance(value, list):
        return f"{len(value)} items"
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def activity_summary(event: dict) -> str:
    result = unpack_result(event.get("result", ""))
    if event.get("status") == "error":
        return brief(result) or brief(event.get("message"))
    inputs = event.get("inputs") or event.get("cue") or {}
    if event.get("message"):
        return brief(event["message"])
    if event.get("name") == "avatar":
        return brief(inputs.get("name"))
    if isinstance(result, dict):
        if result.get("command_id"):
            state = "Running" if result.get("status") == "running" else (
                "Timed out" if result.get("timed_out") else f"Exit {result.get('exit_code')}"
            )
            return state + " · " + brief(inputs.get("command") or result["command_id"])
        for key, singular, plural in (
            ("results", "source", "sources"),
            ("matches", "match", "matches"),
            ("entries", "entry", "entries"),
        ):
            if isinstance(result.get(key), list):
                count = len(result[key])
                return f"{count} {singular if count == 1 else plural}" + (
                    " · " + brief(inputs.get("query")) if inputs.get("query") else ""
                )
        if result.get("path"):
            return brief(result["path"])
    return brief(
        inputs.get("query") or inputs.get("url") or inputs.get("path") or inputs.get("title")
    )


def readable_value(value, depth: int = 0) -> str:
    """Turn nested tool results into prose, preserving source links and text."""
    if depth >= 6:
        return "Further nested details omitted."
    if isinstance(value, dict):
        if value.get("url") and value.get("title"):
            return f"**{value['title']}**\n\n{value['url']}\n\n{value.get('content', '')}"
        parts = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            label = readable_name(str(key))
            body = readable_value(item, depth + 1)
            parts.append(
                f"**{label}**\n\n{body}"
                if isinstance(item, (dict, list))
                else f"**{label}:** {body}"
            )
        return "\n\n".join(parts) or "No additional details."
    if isinstance(value, list):
        return (
            "\n\n".join(readable_value(item, depth + 1) for item in value[:50])
            + (f"\n\n{len(value) - 50} more items omitted." if len(value) > 50 else "")
            or "No results."
        )
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value) if value is not None else "—"


@dataclass
class Activity:
    key: int
    event: dict

    @property
    def label(self) -> str:
        name = self.event.get("name", "activity")
        return LABELS.get(name, readable_name(name))

    @property
    def status(self) -> str:
        value = self.event.get("status", "completed")
        return STATUSES.get(value, readable_name(value))

    @property
    def duration(self) -> str:
        value = self.event.get("duration")
        return f"{value:.1f}s" if isinstance(value, (float, int)) else "—"

    def status_text(self) -> Text:
        state = self.event.get("status")
        style = "bold" if state in {"error", "unavailable"} else ""
        return Text(self.status, style=style)

    def detail(self) -> str:
        parts = [
            f"## {self.label}",
            f"{self.status}" + (f" · {self.duration}" if self.duration != "—" else ""),
        ]
        for key, title in (
            ("inputs", "Inputs"),
            ("cue", "Cue"),
            ("result", "Result"),
            ("message", "Details"),
        ):
            if self.event.get(key):
                value = unpack_result(self.event[key])
                if (
                    key == "result"
                    and isinstance(value, dict)
                    and isinstance(value.get("results"), list)
                ):
                    title, value = "Sources", value["results"]
                parts.append(f"### {title}\n\n" + readable_value(value))
        if self.event.get("diagnostic_path"):
            parts.append("### Saved diagnostic\n\n" + str(self.event["diagnostic_path"]))
        return "\n\n".join(parts)


@dataclass
class ActivityLog:
    entries: list[Activity] = field(default_factory=list)
    sequence: int = 0
    limit: int = 80

    def record(self, event: dict) -> Activity:
        if event.get("status") != "started":
            for entry in reversed(self.entries):
                if (
                    entry.event.get("name") == event.get("name")
                    and entry.event.get("status") == "started"
                ):
                    entry.event = dict(event)
                    return entry
        self.sequence += 1
        entry = Activity(self.sequence, dict(event))
        self.entries.append(entry)
        del self.entries[: -self.limit]
        return entry


class ActivityScreen(FormModal):
    """Read the current reply's operations without leaving the conversation."""

    DEFAULT_CSS = """
    ActivityScreen #activity-dialog { max-width: 110; }
    ActivityScreen #activity-browser, ActivityScreen #activity-list { height: 1fr; }
    ActivityScreen #activity-count { height: auto; max-height: 2; color: $text-muted; }
    ActivityScreen.short #activity-list.has-activity #activity-count { display: none; }
    ActivityScreen #activity-table { height: 1fr; background: $surface; }
    ActivityScreen #activity-preview {
        display: none; height: 1fr; scrollbar-size: 1 1;
    }
    ActivityScreen #activity-browser.detail #activity-list { display: none; }
    ActivityScreen #activity-browser.detail #activity-preview { display: block; }
    ActivityScreen #activity-text { background: $surface; padding: 0; }
    ActivityScreen #activity-text MarkdownH2 {
        margin: 0; color: $text; text-style: bold;
    }
    ActivityScreen #activity-text MarkdownH3 {
        margin: 0; color: $text-muted; text-style: bold;
    }
    ActivityScreen DataTable:focus > .datatable--cursor { text-style: bold reverse; }
    """
    BINDINGS = [Binding("escape", "close", "Back", priority=True)]

    def __init__(self) -> None:
        super().__init__()
        self.activity_log: ActivityLog | None = None
        self.detail_key: int | None = None
        self.detail_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="activity-dialog", classes="form-dialog"):
            yield Static("Activity", classes="form-title", markup=False)
            with Vertical(id="activity-browser"):
                with Vertical(id="activity-list"):
                    yield Static("", id="activity-count", markup=False)
                    yield DataTable(id="activity-table", cursor_type="row")
                with VerticalScroll(id="activity-preview"):
                    yield Markdown("", id="activity-text")
            yield Static("", id="activity-hint", classes="form-hint", markup=False)
        yield Static(
            "Terminal too small — need 48×12. Resize or press Esc to close.",
            classes="form-too-small",
        )

    def on_mount(self) -> None:
        self.resize_state()
        self.query_one(DataTable).focus()

    def remember_draft(self) -> None:
        pass

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        return action != "save"

    def responsive_copy(self, width: int) -> None:
        self.refresh_activity()

    def update_hint(self) -> None:
        hint = ("Esc back · Ctrl+R inspect" if self.detail_key is not None else
                "Enter details · Ctrl+R inspect · Esc close")
        self.query_one("#activity-hint", Static).update(
            hint.replace("·", "|") if self.app.ascii else hint
        )

    def cell_text(self, value: str, width: int, *, style: str | Style = "") -> Text:
        text = Text(" ".join(value.split()), style=style, no_wrap=True, overflow="crop")
        if text.cell_len > width:
            ellipsis = "..." if self.app.ascii else "…"
            text.truncate(max(0, width - len(ellipsis)), overflow="crop")
            text.append(ellipsis)
        return text

    def refresh_activity(self) -> None:
        table = self.query_one(DataTable)
        selected = (table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                    if table.row_count else None)
        scroll_y = table.scroll_y
        if self.activity_log is not self.app.activity:
            self.activity_log = self.app.activity
            selected, scroll_y = None, 0
            self.show_list()
        table.clear(columns=True)
        table.show_header = not self.has_class("short")
        dialog_width = min(110, int(self.app.size.width * (
            1 if self.has_class("narrow") else .96 if self.has_class("compact") else .92
        )))
        padding = 2 if self.has_class("compact") else 4
        # Three columns' cell padding, the dialog border, and a scrollbar all count.
        operation_width = max(8, dialog_width - padding - 26)
        table.add_column("Operation", width=operation_width)
        table.add_column("Status", width=11)
        table.add_column("Time", width=6)
        separator = " | " if self.app.ascii else " · "
        for item in self.activity_log.entries:
            summary = activity_summary(item.event)
            title = item.label + (separator + summary if summary else "")
            table.add_row(
                self.cell_text(title, operation_width),
                self.cell_text(item.status, 11, style=item.status_text().style),
                Text(item.duration.replace("—", "-") if self.app.ascii else item.duration,
                     justify="right", no_wrap=True, overflow="ellipsis"),
                key=str(item.key),
            )
        count = len(self.activity_log.entries)
        table.display = bool(count)
        self.query_one("#activity-list").set_class(bool(count), "has-activity")
        message = (f"{count} operation{'s' if count != 1 else ''}{separator}current reply"
                   if count else "Waiting for activity from this reply…" if self.app.busy else
                   "No activity yet. Tools will appear during a reply.")
        self.query_one("#activity-count", Static).update(
            message.replace("…", "...") if self.app.ascii else message
        )
        if selected and selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected), scroll=False)
        table.scroll_to(y=scroll_y, animate=False, force=True)
        if self.detail_key is not None:
            entry = next((item for item in self.activity_log.entries
                          if item.key == self.detail_key), None)
            if entry is None:
                self.show_list()
            else:
                self.update_detail(entry.detail())
        self.update_hint()
        if (self.app.screen is self and self.focused is None
                and not self.has_class("too-small")):
            self.query_one(
                "#activity-preview" if self.detail_key is not None else "#activity-table"
            ).focus()

    def update_detail(self, text: str) -> None:
        if text == self.detail_text:
            return
        self.detail_text = text
        self.render_detail(text)

    @work(exclusive=True, group="activity-detail", exit_on_error=False)
    async def render_detail(self, text: str) -> None:
        preview = self.query_one("#activity-preview", VerticalScroll)
        scroll_y = preview.scroll_y
        await self.query_one(Markdown).update(text)
        preview.scroll_to(y=scroll_y, animate=False, force=True)

    @on(DataTable.RowSelected, "#activity-table")
    def selected_row(self, event: DataTable.RowSelected) -> None:
        event.stop()
        entry = next((item for item in self.app.activity.entries
                      if str(item.key) == event.row_key.value), None)
        if entry is None:
            return
        self.detail_key = entry.key
        self.query_one("#activity-browser").add_class("detail")
        preview = self.query_one("#activity-preview", VerticalScroll)
        preview.scroll_home(animate=False)
        self.update_detail(entry.detail())
        preview.focus()
        self.update_hint()

    def show_list(self) -> None:
        was_reading = self.detail_key is not None
        self.detail_key = None
        self.query_one("#activity-browser").remove_class("detail")
        if was_reading and self.app.screen is self:
            self.query_one(DataTable).focus()
        self.update_hint()

    def action_close(self) -> None:
        if self.detail_key is not None:
            self.show_list()
        else:
            self.dismiss(None)
