"""Past rooms in a quiet, composer-anchored overlay."""

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Markdown, Static

from eidolon.config import IdentityConfig
from eidolon.ui.forms import FormModal
from eidolon.ui.identity import markdown_speaker


def session_date(session: dict, timezone: str, *, ascii: bool = False) -> str:
    """Keep chat day boundaries; job dates use the original run's start time."""
    if session.get("day_key"):
        return session["day_key"]
    try:
        started = datetime.fromisoformat(session["created_at"])
        if started.tzinfo is not None:
            return started.astimezone(ZoneInfo(timezone)).date().isoformat()
    except (KeyError, TypeError, ValueError):
        pass
    return "-" if ascii else "—"


class HistoryScreen(FormModal):
    DEFAULT_CSS = """
    HistoryScreen #history-dialog { max-width: 110; }
    HistoryScreen #history-browser { height: 1fr; }
    HistoryScreen #history-list { height: 1fr; }
    HistoryScreen #history-filter { margin: 0; }
    HistoryScreen #history-count {
        height: auto; max-height: 2; color: $text-muted;
    }
    HistoryScreen.short #history-list.has-rooms #history-count { display: none; }
    HistoryScreen #history-table { height: 1fr; background: $surface; }
    HistoryScreen #history-preview {
        display: none; height: 1fr; scrollbar-size: 1 1;
    }
    HistoryScreen #history-heading { height: auto; max-height: 2; color: $text-muted; }
    HistoryScreen.short #history-heading {
        height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    HistoryScreen #history-browser.detail #history-list { display: none; }
    HistoryScreen #history-browser.detail #history-preview { display: block; }
    HistoryScreen #history-text { background: $surface; }
    HistoryScreen DataTable:focus > .datatable--cursor { text-style: bold reverse; }
    """
    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("ctrl+f", "filter", "Filter", priority=True),
        Binding("ctrl+l", "reload", "Reload", priority=True),
        Binding("ctrl+enter", "continue_room", "Continue room", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="history-dialog", classes="form-dialog"):
            yield Static("History", classes="form-title", markup=False)
            with Vertical(id="history-browser"):
                with Vertical(id="history-list"):
                    yield Input(placeholder="Filter rooms…", id="history-filter")
                    yield Static("Loading rooms…", id="history-count", markup=False)
                    yield DataTable(id="history-table", cursor_type="row")
                with VerticalScroll(id="history-preview"):
                    yield Static("", id="history-heading", markup=False)
                    yield Markdown("", id="history-text")
            yield Static("", id="history-hint", classes="form-hint", markup=False)
        yield Static(
            "Terminal too small — need 48×12. Resize or press Esc to close.",
            classes="form-too-small",
        )

    def on_mount(self) -> None:
        self.messages: list[dict] | None = None
        self.rendered_text: str | None = None
        self.resize_state()
        self.render_sessions()
        target = "#history-table" if self.app.overview.get("sessions") else "#history-filter"
        self.query_one(target).focus()
        if not self.app.overview:
            self.app.refresh_data()

    def remember_draft(self) -> None:
        pass

    def responsive_copy(self, width: int) -> None:
        detail = self.query_one("#history-browser").has_class("detail")
        hint = (
            "Ctrl+Enter continue · Esc back"
            if detail else "Enter read · Ctrl+Enter continue · Esc close"
        )
        self.query_one("#history-hint", Static).update(hint)
        self.render_sessions()

    @property
    def selected_session(self) -> str | None:
        if self.query_one("#history-browser").has_class("detail"):
            return self.app.history_id
        table = self.query_one(DataTable)
        if table.row_count:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return None

    def render_sessions(self) -> None:
        if not self.app.overview:
            return
        table = self.query_one(DataTable)
        table.show_header = not self.has_class("short")
        selected = self.selected_session
        table.clear(columns=True)
        # Use the same width calculation before and after the first layout pass.
        dialog_width = min(110, int(self.app.size.width * (
            1 if self.app.size.width < 70 else .96 if self.has_class("compact") else .92
        )))
        padding = 2 if self.has_class("compact") else 4
        table.add_column("Date", width=10)
        table.add_column("Room", width=max(12, dialog_width - padding - 18))
        query = self.query_one(Input).value.casefold()
        sessions = self.app.overview["sessions"]
        rows = []
        for session in sessions:
            date = session_date(session, self.app.config.timezone, ascii=self.app.ascii)
            if query not in f"{date} {session['title']} {session['kind']}".casefold():
                continue
            rows.append(session)
            title = session["title"]
            if session["kind"] != "chat":
                title += f" · {session['kind']}"
            table.add_row(date, Text(title, overflow="ellipsis", no_wrap=True), key=session["id"])
        table.display = bool(rows)
        self.query_one("#history-list").set_class(bool(rows), "has-rooms")
        count = f"{len(rows)} of {len(sessions)}" if query else str(len(sessions))
        self.query_one("#history-count", Static).update(
            f"{count} room{'s' if len(rows) != 1 else ''}"
            if rows else "No rooms match your filter." if query else
            "Your conversations will appear here."
        )
        if selected and selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected))

    @on(Input.Changed, "#history-filter")
    def filter_changed(self) -> None:
        self.render_sessions()

    @on(Input.Submitted, "#history-filter")
    def filter_submitted(self) -> None:
        if self.query_one(DataTable).row_count:
            self.query_one(DataTable).focus()

    @on(DataTable.RowSelected, "#history-table")
    def selected_row(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.read_selection(event.row_key.value)

    @work(exclusive=True, group="history-read", exit_on_error=False)
    async def read_selection(self, key: str) -> None:
        self.messages = None
        self.rendered_text = None
        self.workers.cancel_group(self, "history-labels")
        browser = self.query_one("#history-browser")
        browser.add_class("detail")
        self.app.history_id = key
        self.query_one("#history-heading", Static).update("Loading room…")
        await self.query_one(Markdown).update("")
        self.query_one(VerticalScroll).focus()
        self.responsive_copy(self.app.size.width)
        try:
            data = await self.app.client.request("history", session_id=key)
            session = data["session"]
            date = session_date(session, self.app.config.timezone, ascii=self.app.ascii)
            self.query_one("#history-heading", Static).update(f"{session['title']} · {date}")
            self.messages = data["messages"]
            self.refresh_identity(reset_scroll=True)
        except Exception as exc:
            self.query_one("#history-heading", Static).update(f"Could not load room: {exc}")

    @work(exclusive=True, group="history-labels", exit_on_error=False)
    async def refresh_identity(self, *, reset_scroll: bool = False) -> None:
        if self.messages is None:
            return
        messages = []
        for message in self.messages:
            identity = (IdentityConfig.model_validate(message["identity"])
                        if "identity" in message else self.app.config.identity)
            label = markdown_speaker(message["role"], identity)
            messages.append(f"**{label}**\n\n{message['content']}")
        text = "\n\n".join(messages) or "No exchanges in this room yet."
        preview = self.query_one(Markdown)
        if self.rendered_text == text and not reset_scroll:
            return
        scroller = self.query_one(VerticalScroll)
        position = (0, 0) if reset_scroll else scroller.scroll_offset
        self.rendered_text = None
        await preview.update(text)
        self.rendered_text = text
        self.call_after_refresh(scroller.scroll_to, *position, animate=False)

    def check_action(self, action, parameters):
        if action == "save":
            return False
        if action == "filter":
            return not self.query_one("#history-browser").has_class("detail")
        if action == "continue_room":
            return self.selected_session is not None and not self.app.busy
        return True

    def action_filter(self) -> None:
        self.query_one(Input).focus()

    def action_reload(self) -> None:
        self.app.refresh_data()

    @work(group="history-continue", exit_on_error=False)
    async def action_continue_room(self) -> None:
        session_id = self.selected_session
        if session_id is not None:
            try:
                await self.app.change_room(session_id)
            except Exception as exc:
                self.app.notify(str(exc), severity="error", timeout=8)

    def action_close(self) -> None:
        browser = self.query_one("#history-browser")
        if browser.has_class("detail"):
            self.workers.cancel_group(self, "history-read")
            browser.remove_class("detail")
            self.query_one(DataTable if self.query_one(DataTable).row_count else Input).focus()
            self.responsive_copy(self.app.size.width)
        elif self.query_one(Input).value:
            self.query_one(Input).value = ""
            self.query_one(DataTable).focus()
        else:
            self.dismiss(None)
