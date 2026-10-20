from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from eidolon.ui.forms import FormModal


class RequestInspector(FormModal):
    """View the latest captured provider request above the conversation."""

    DEFAULT_CSS = """
    RequestInspector #request-dialog { max-width: 120; }
    RequestInspector #request-details {
        height: auto; max-height: 2; margin: 1 0;
        color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    RequestInspector #request-formatted { height: 1fr; border: none; }
    RequestInspector #request-actions { height: auto; color: $text-muted; }
    RequestInspector.compact #request-details { margin: 0; }
    RequestInspector.short #request-details { height: 1; max-height: 1; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+r", "refresh", "Refresh", priority=True),
        Binding("ctrl+y", "copy_request", "Copy raw", priority=True),
        Binding("ctrl+s", "save_request", "Save JSON", priority=True),
    ]

    def __init__(self, client, timezone: str):
        super().__init__()
        self.client = client
        self.timezone = ZoneInfo(timezone)
        self.request: dict | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="request-dialog", classes="form-dialog"):
            yield Static("Inspect · latest request", classes="form-title", markup=False)
            yield Static("Loading latest request…", id="request-details", markup=False)
            yield TextArea(id="request-formatted", read_only=True, soft_wrap=True)
            yield Static(
                "Ctrl+Y copy raw · Ctrl+S save JSON · Ctrl+R refresh · Esc close",
                id="request-actions",
            )
        yield Static(
            "Terminal too small — need 48×12. Resize or press Esc to close.",
            classes="form-too-small",
        )

    def on_mount(self) -> None:
        self.resize_state()
        self.query_one(TextArea).focus()
        self.refresh_requests()

    def responsive_copy(self, width: int) -> None:
        self.query_one("#request-actions", Static).update(
            "Ctrl+Y copy · Ctrl+S save · Ctrl+R · Esc" if width < 60 else
            "Ctrl+Y copy · Ctrl+S save · Ctrl+R refresh · Esc close"
            if width < 80 else
            "Ctrl+Y copy raw · Ctrl+S save JSON · Ctrl+R refresh · Esc close"
        )

    def remember_draft(self) -> None:
        pass

    def clear_request(self, message: str) -> None:
        self.request = None
        self.query_one("#request-details", Static).update(message)
        self.query_one(TextArea).load_text("")
        self.refresh_bindings()

    @work(exclusive=True, group="request-refresh", exit_on_error=False)
    async def refresh_requests(self) -> None:
        self.clear_request("Loading latest request…")
        try:
            data = await self.client.request("latest_model_request")
            request = data["request"]
            if request is None:
                self.clear_request("No request captured yet. Send a message to capture one.")
                return
            formatted = json.dumps(json.loads(request["body"]), ensure_ascii=False, indent=2)
            self.request = request
            timestamp = datetime.fromisoformat(request["created_at"]).astimezone(
                self.timezone
            ).strftime("%Y-%m-%d %H:%M:%S")
            self.query_one("#request-details", Static).update(
                f"{request['method']} {request['endpoint']}\n"
                f"{request['provider']} · {request['model']} · "
                f"round {request['round_index'] + 1} · {timestamp}"
            )
            self.query_one(TextArea).load_text(formatted)
            self.refresh_bindings()
        except Exception as exc:
            if str(exc) == "Unknown service request":
                self.clear_request(
                    "The background service needs a restart to inspect requests.\n"
                    "Run eidolon service stop, then eidolon service start."
                )
            else:
                self.clear_request(str(exc))

    def check_action(self, action, parameters):
        if action in {"copy_request", "save_request"}:
            return self.request is not None
        return True

    def action_refresh(self) -> None:
        self.refresh_requests()

    def action_copy_request(self) -> None:
        if self.request:
            self.app.copy_to_clipboard(self.request["body"])
            self.notify("Raw request copied.")

    @work(exit_on_error=False)
    async def action_save_request(self) -> None:
        try:
            if self.request:
                result = await self.client.request(
                    "export_model_request", request_id=self.request["id"]
                )
                self.notify("Saved raw JSON to " + result["path"], timeout=12)
        except Exception as exc:
            self.notify(str(exc), severity="error")
