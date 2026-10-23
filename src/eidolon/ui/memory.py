"""Read memories supplied to replies and manage current-life notes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static

from eidolon.memory.continuity import THINKING_TIMEOUT_NOTICE, active_items, with_note_metadata
from eidolon.memory.coordinator import (
    CONTEXT_PREFIX, CONTEXT_SUFFIX, CONTINUITY_PREFIX, render_record,
)
from eidolon.memory.models import MemoryRecord
from eidolon.memory.diagnostics import model_label, read_reason, safe_usage
from eidolon.ui.forms import FormModal
from eidolon.ui.memory_notes import (
    NOTE_CATEGORIES, NOTE_IMPORTANCE, NoteDeleteScreen, NoteEditorScreen,
)


CATEGORIES = (("Reply context", "replies"), ("Current life", "continuity"))
RUN_STATES = {
    "pending": "Waiting", "running": "Refreshing", "completed": "Finished",
    "failed": "Needs retry", "interrupted": "Paused", "stale": "Waiting",
}


def local_time(value: str, timezone_name: str) -> str:
    try:
        created = datetime.fromisoformat(value)
        return created.astimezone(ZoneInfo(timezone_name)).strftime("%b %d %H:%M")
    except (TypeError, ValueError):
        return "Date unavailable"


def reply_label(row: dict, timezone_name: str) -> str:
    date = local_time(row.get("created_at", ""), timezone_name)
    preview = " ".join(str(row.get("manifest", {}).get("preview") or "").split())
    return f"{date} · {preview}" if preview else "Reply · " + date


def memory_content(manifest: dict) -> str:
    """Present supplied prose, using an allowlist rather than dumping provenance."""
    text = manifest.get("text")
    if not text:
        return ""
    parts = []
    notes = [note_details(item) for item in manifest.get("continuity", {}).get("entries", [])
             if item.get("content")]
    if notes:
        parts.extend(["Current life", "\n\n".join(notes)])
    reflection = manifest.get("reflection", {}).get("text")
    if reflection:
        parts.extend(["Memory reflection", str(reflection)])
    records = [render_record(MemoryRecord.model_validate(entry))[0]
               for entry in manifest.get("entries", []) if entry.get("text")]
    if records:
        parts.extend(["Recalled memories", "\n\n".join(records)])
    if parts:
        return "\n\n".join(parts)
    # Older saved contexts may only contain text. Remove the app-owned framing,
    # preserving the memory's literal wording, brackets, and line breaks.
    return (str(text).replace(CONTEXT_PREFIX, "").replace(CONTEXT_SUFFIX, "")
            .replace(CONTINUITY_PREFIX, "").replace("\n</continuity_context>", "").strip())


def reply_details(row: dict) -> str:
    manifest = row.get("manifest", {})
    parts = []
    content = memory_content(manifest)
    if content:
        parts.append(content)
    elif "text" not in manifest:
        parts.append("Loading saved memory content…" if manifest.get("preview") else
                     "Saved memory content is not available yet.")
    else:
        parts.append("No memories were supplied with the initial request.")
    for context in manifest.get("tool_contexts", []):
        # A tool result is only supplied if the next provider request occurred.
        if ("supplied_rounds" in manifest and context.get("supplied_from_round")
                not in manifest["supplied_rounds"]):
            continue
        if context.get("status") != "completed":
            continue
        content = memory_content(context.get("evidence") or {"text": context.get("text")})
        if content:
            parts.extend(["Additional memories sent during this reply", content])
    return "\n\n".join(parts)


def note_details(row: dict) -> str:
    row = with_note_metadata(row)
    heading = NOTE_CATEGORIES.get(row.get("category"), "Current life note")
    heading += " · " + NOTE_IMPORTANCE[row["importance"]] + " importance"
    if row.get("user_created"):
        heading += " · User-created"
    if not active_items([row], scope=row.get("scope", "real_life")):
        heading += " · Archived"
    parts = [row["topic"], heading, str(row.get("content", ""))]
    if row.get("metadata_pending"):
        parts.append("Topic and importance await classification during maintenance.")
    if row.get("decision_reason"):
        parts.append("Decision: " + row["decision_reason"])
    event = row.get("event_at") or row.get("event_date")
    if event:
        try:
            parts.append("Event: " + datetime.fromisoformat(event).strftime("%b %d, %Y"))
        except (TypeError, ValueError):
            pass
    return "\n\n".join(parts)


def run_state(row: dict) -> str:
    return RUN_STATES.get(row.get("status"), "Unavailable")


def run_summary(row: dict) -> str:
    summary = run_state(row)
    count = row.get("change_count")
    if row.get("status") == "completed" and type(count) is int:
        summary += (" · No changes" if count == 0 else
                    f" · {count} {'note' if count == 1 else 'notes'} updated")
    if row.get("status") == "completed" and row.get("unresolved_count"):
        summary += " · Needs attention"
    return summary


def note_version_details(row: dict, timezone_name: str, *, compact: bool = False) -> str:
    """Show note wording and meaningful state, without revision/source metadata."""
    row = with_note_metadata(row)
    heading = row["topic"] + " · " + NOTE_IMPORTANCE[row["importance"]] + " importance"
    states = {"open": "Active", "resolved": "Archived", "archived": "Archived"}
    if row.get("state") in states:
        heading += " · " + states[row["state"]]
    content = str(row.get("content", ""))
    parts = [content, heading] if compact else [heading, content]
    event = row.get("event_at") or row.get("event_date")
    if event:
        try:
            parts.append("Event: " + datetime.fromisoformat(event).strftime("%b %d, %Y"))
        except (TypeError, ValueError):
            pass
    for key, label in (("observed_at", "Last mentioned"), ("expires_at", "Valid until"),
                       ("follow_up_at", "Follow-up")):
        if row.get(key):
            parts.append(label + ": " + local_time(row[key], timezone_name))
    owner = {"user": "User", "assistant": "Companion"}.get(row.get("promise_owner"))
    if owner:
        parts.append("Commitment: " + owner + (
            " (reported as scheduled)" if row.get("promise_kind") == "scheduled" else ""
        ))
    if row.get("decision_reason"):
        parts.append("Decision: " + row["decision_reason"])
    return "\n".join(parts)


def run_details(row: dict, timezone_name: str, *, compact: bool = False) -> str:
    descriptions = {
        "pending": "This refresh is waiting to continue.",
        "running": "Current life notes are being refreshed in the background.",
        "failed": "This refresh couldn't finish. Existing notes are preserved.",
        "interrupted": "This refresh was paused so the conversation could continue.",
    }
    parts = ([] if compact else
             [local_time(row.get("updated_at", ""), timezone_name) + " · " + run_summary(row)])
    reason = read_reason(row.get("reason"))
    if row.get("status") in descriptions and not reason:
        parts.append(descriptions[row["status"]])
    if reason:
        parts.append(reason["explanation"])
    budget = row.get("budget") or {}
    if budget:
        parts.append(f"Recovery pass {budget['pass']}: {budget['used']}/{budget['limit']} requests "
                     f"used; {budget['remaining']} remaining.")
        if budget.get("exhausted") and row.get("status") != "completed":
            parts.append("Budget exhausted. Complete evidence is retained; newer evidence waits.")
    if row.get("paused"):
        parts.append("Requests paused until relevant settings change or an explicit Refresh.")
    if "attempts" in row and isinstance(row["attempts"], list):
        action = {
            "complete": "Complete; no retry needed.",
            "running": "Refreshing now; foreground work has priority.",
            "configure": "Check the auxiliary model in /preferences, then refresh current life.",
            "retry_required": "Use Refresh current life to start a new bounded recovery pass.",
            "retry_scheduled": "Automatic retry after " + local_time(
                row.get("next_retry_at"), timezone_name) + "; foreground work has priority.",
            "automatic_check": "Waiting for the next automatic maintenance check.",
            "refresh": "Use Refresh current life to retry.",
        }.get(row.get("next_action"), "Next action unavailable.")
        parts.append("Next: " + action)
        parts.append("Last successful maintenance: " + (
            local_time(row["last_successful_at"], timezone_name) if row.get("last_successful_at")
            else "unavailable"))
        total = row.get("model_calls", len(row["attempts"]))
        counts = row.get("attempt_counts") or {}
        totals = ", ".join(f"{counts[key]} {label}" for key, label in (
            ("failed", "failed"), ("committed", "succeeded"), ("interrupted", "interrupted"),
            ("stale", "outdated"), ("configuration_error", "configuration rejected"),
        ) if counts.get(key))
        parts.append(f"Attempt history · {total} requests · newest first"
                     + (" · " + totals if totals else ""))
        totals_usage = safe_usage(row.get("usage_totals"))
        if totals_usage:
            parts.append("Recorded usage across attempts (unknown usage excluded): " +
                         ", ".join(f"{key}: {value}" for key, value in totals_usage.items()))
        if total > len(row["attempts"]):
            parts.append("Only the latest 100 attempts are retained; aggregate counts include "
                         "older requests. Older outcomes may be unavailable after an upgrade.")
        for index, attempt in enumerate(row["attempts"], 1):
            outcome = {"committed": "Succeeded", "failed": "Failed", "interrupted": "Interrupted",
                       "configuration_error": "Configuration rejected", "stale": "Outdated",
                       "running": "Running"}.get(attempt.get("outcome"), "Unavailable")
            elapsed = attempt.get("elapsed_seconds")
            duration = f"{elapsed:.1f}s" if type(elapsed) in {int, float} else "Timing unavailable"
            date = local_time(attempt.get("started_at"), timezone_name)
            detail = [f"{index}. {outcome} · " + date,
                      "Model: " + model_label(attempt.get("model")) + " · " + duration]
            if attempt.get("ended_at"):
                detail.append("Ended: " + local_time(attempt["ended_at"], timezone_name))
            cause = read_reason(attempt.get("reason"))
            if cause:
                detail.append(cause["explanation"])
            elif attempt.get("outcome") not in {"committed", "running"}:
                detail.append("No safe cause was recorded for this attempt.")
            usage = safe_usage(attempt.get("usage"))
            detail.append("Usage: " + (", ".join(f"{key}: {value}" for key, value in usage.items())
                                      if usage else "unavailable"))
            parts.append("\n".join(detail))
        if not row["attempts"]:
            parts.append("No per-attempt details were recorded for this run.")
    for condition in row.get("unresolved_conditions", []):
        if condition.get("stage") == "continuity":
            continue  # The run's primary cause is already shown above.
        label = {"summary": "Summary", "reflection": "Reflection"}.get(condition.get("stage"),
                                                                         "Maintenance")
        cause = read_reason(condition.get("reason"))
        parts.append(label + " still needs attention: " + (
            cause["explanation"] if cause else "No safe cause was recorded."))
    if "changes" not in row:
        parts.append("Note updates have not been loaded yet.")
    elif not row["changes"]:
        parts.append("No notes were changed by this run." if row.get("status") == "completed"
                     else "No note updates have been saved by this run.")
    for index, change in enumerate(row.get("changes", []), 1):
        after, before = change["after"], change.get("before")
        action = ("Added" if change.get("created") else
                  "Archived" if after.get("state") in {"resolved", "archived"} else "Updated")
        new_label = f"New version · Note {index} · {action}\n" if compact else (
            f"Note {index} · {action}\nNew version · "
        )
        previous = (note_version_details(before, timezone_name, compact=compact)
                    if before is not None else
                    "No previous version (new note)." if change.get("created")
                    else "The previous version is unavailable.")
        parts.extend([
            new_label + note_version_details(after, timezone_name, compact=compact),
            ("Previous version\n" if compact else "Previous version · ") + previous,
        ])
    return "\n\n".join(parts)


@dataclass
class MemoryState:
    category: str = "replies"
    life_section: str = "notes"
    query: str = ""
    selected: str | None = None
    snapshot: dict | None = None
    session_id: str | None = None
    details: dict[str, dict] = field(default_factory=dict)
    note_drafts: dict[str, dict] = field(default_factory=dict)


class MemoryScreen(FormModal):
    DEFAULT_CSS = """
    MemoryScreen #memory-browser, MemoryScreen #memory-list { height: 1fr; }
    MemoryScreen #memory-controls { height: 2; }
    MemoryScreen #memory-category { margin: 0; }
    MemoryScreen #memory-life-controls { height: 1; }
    MemoryScreen #memory-life-controls Button.selected { text-style: bold underline; }
    MemoryScreen #memory-life-controls Button:focus { text-style: bold reverse; }
    MemoryScreen #memory-life-controls Button.selected:focus {
        text-style: bold underline reverse;
    }
    MemoryScreen #memory-table {
        height: 1fr; border: none; padding: 0; background: $surface;
        scrollbar-size: 1 1;
    }
    MemoryScreen #memory-empty { height: 1fr; color: $text-muted; }
    MemoryScreen #memory-detail { display: none; height: 1fr; }
    MemoryScreen #memory-detail-scroll { height: 1fr; scrollbar-size: 1 1; }
    MemoryScreen #memory-text { height: auto; }
    MemoryScreen #memory-note-actions { height: 1; }
    MemoryScreen #memory-browser.detail #memory-list { display: none; }
    MemoryScreen #memory-browser.detail #memory-detail { display: block; }
    MemoryScreen .form-status {
        height: 1; max-height: 1; margin: 0; text-wrap: nowrap; text-overflow: ellipsis;
    }
    MemoryScreen.reading .form-status { display: none; }
    MemoryScreen.reading .form-status.problem { display: block; }
    MemoryScreen.short #memory-controls { layout: horizontal; height: 1; }
    MemoryScreen.short #memory-category { width: 50%; }
    MemoryScreen.short #memory-filter { width: 1fr; }
    MemoryScreen.short.problem .form-hint { display: none; }
    MemoryScreen.short.current-life .form-hint { display: none; }
    MemoryScreen.short.current-life .form-status.problem { display: none; }
    MemoryScreen.short.reading .form-hint { display: block; }
    MemoryScreen DataTable:focus > .datatable--cursor { text-style: bold reverse; }
    """
    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("ctrl+f", "filter", "Search", priority=True),
        Binding("ctrl+l", "reload", "Refresh", priority=True),
        Binding("ctrl+n", "new_note", "Create note", priority=True),
        Binding("ctrl+e", "edit_note", "Edit note", priority=True),
        Binding("ctrl+d", "delete_note", "Delete note", priority=True),
    ]

    def __init__(self, client, state: MemoryState, session_id: str | None = None):
        super().__init__()
        self.client, self.state, self.session_id = client, state, session_id
        if state.category not in {value for _, value in CATEGORIES}:
            state.category, state.selected = "replies", None
        if state.session_id != session_id:
            state.snapshot, state.details = None, {}
            state.selected = None
        state.session_id = session_id
        self.loading = False
        self.connected = False
        self.committing = False
        self.detail_key: str | None = None
        self.detail_value: dict | None = None
        self._detail_compact = False
        self.rows: dict[str, tuple[str, dict]] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Memory", classes="form-title", markup=False)
            yield Static("Loading memory…", classes="form-status", markup=False)
            with Vertical(id="memory-browser"):
                with Vertical(id="memory-list"):
                    with Vertical(id="memory-controls"):
                        yield Select(CATEGORIES, value=self.state.category, allow_blank=False,
                                     compact=True, id="memory-category")
                        yield Input(self.state.query, placeholder="Search this category…",
                                    id="memory-filter")
                    with Horizontal(id="memory-life-controls"):
                        yield Button("Notes", id="memory-notes", flat=True, compact=True)
                        yield Button("Run history", id="memory-history", flat=True, compact=True)
                        yield Button("Refresh current life", id="memory-refresh", flat=True,
                                     compact=True)
                        yield Button("Create note", id="memory-create", flat=True, compact=True)
                    yield Static("Loading memory…", id="memory-empty", markup=False)
                    yield DataTable(id="memory-table", cursor_type="row", show_header=False)
                with Vertical(id="memory-detail"):
                    with VerticalScroll(id="memory-detail-scroll"):
                        yield Static("", id="memory-text", markup=False)
                    with Horizontal(id="memory-note-actions"):
                        yield Button("Edit note", id="memory-edit", flat=True, compact=True)
                        yield Button("Delete note", id="memory-delete", flat=True, compact=True)
            yield Static("Enter details | ^F search | ^L refresh | Esc", classes="form-hint",
                         markup=False)
        yield Static("Terminal too small — need 48×12. Resize or press Esc to close.",
                     classes="form-too-small", markup=False)

    def on_mount(self) -> None:
        self.resize_state()
        self.render_list()
        self.query_one("#memory-table" if self.rows else "#memory-filter").focus()
        self.action_reload()
        self.set_interval(2, self._poll_maintenance)

    def _poll_maintenance(self) -> None:
        if (self.is_mounted and self.app.screen is self and self.connected
                and not self.loading and not self.committing
                and (self.state.snapshot or {}).get("continuity", {}).get(
                    "maintenance", {}).get("running")):
            self.action_reload(quiet=True)

    def remember_draft(self) -> None:
        self.state.query = self.query_one("#memory-filter", Input).value
        self.state.category = self.query_one("#memory-category", Select).value
        self.state.selected = self.selected_key()

    def responsive_copy(self, width: int) -> None:
        detail = self.query_one("#memory-browser").has_class("detail")
        self.query_one(".form-hint", Static).update(
            "^E edit | ^D delete | Esc back" if detail and self.detail_key.startswith("continuity:")
            else
            "Up/Down scroll | Esc back" if detail else
            "^N create | Enter read | ^F find | Esc" if self.state.category == "continuity"
            and self.state.life_section == "notes" else
            "Enter read | ^F find | ^L reload | Esc" if width < 60 else
            "Enter details | ^F search | ^L refresh | Esc" if width < 70 else
            "Enter details | Ctrl+F search | Ctrl+L refresh | Esc close"
        )
        self.query_one("#memory-refresh", Button).label = (
            "Refresh" if width < 70 else "Refresh current life"
        )
        self.query_one("#memory-create", Button).label = "New note" if width < 70 else "Create note"
        self.render_list()
        if (self.detail_key and self.detail_key.startswith("maintenance:")
                and self.detail_value is not None
                and self._detail_compact != self.has_class("short")):
            self._detail_compact = self.has_class("short")
            self.query_one("#memory-text", Static).update(
                self.row_text(self.detail_key, self.detail_value),
            )

    def status(self, message: str, *, problem: bool = False) -> None:
        self.set_class(problem, "problem")
        super().status(message, problem=problem)
        self.update_border_title()

    def update_border_title(self) -> None:
        title = "Memory" if self.has_class("short") else ""
        if self.has_class("short") and self.has_class("current-life") and self.has_class("problem"):
            maintenance = (self.state.snapshot or {}).get("continuity", {}).get("maintenance", {})
            auxiliary = maintenance.get("auxiliary", {})
            title += (" · Offline · Ctrl+L retries" if not self.connected else
                      " · Check /preferences" if auxiliary.get("notice")
                      or auxiliary.get("configuration_error") or auxiliary.get("mode") == "disabled"
                      or maintenance.get("error") == THINKING_TIMEOUT_NOTICE
                      else " · Refresh needs retry")
        self.query_one(".form-dialog").border_title = title

    def selected_key(self) -> str | None:
        table = self.query_one("#memory-table", DataTable)
        if table.row_count:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.state.selected

    def build_rows(self) -> dict[str, tuple[str, dict]]:
        data = self.state.snapshot
        if data is None:
            return {}
        if self.state.category == "continuity":
            continuity = data.get("continuity", {})
            if self.state.life_section == "history":
                maintenance = continuity.get("maintenance", {})
                latest = maintenance.get("latest_attempt") or {}
                stages = []
                for stage in maintenance.get("stages", []):
                    if (stage.get("task_id") == latest.get("task_id")
                            and stage.get("status") != "completed" and maintenance.get("running")
                            and latest.get("outcome") == "running"):
                        stage = {**stage, "status": "running",
                                 "updated_at": latest.get("started_at") or stage.get("updated_at")}
                    stages.append(stage)
                return self.inspection_rows({
                    "maintenance:" + stage["task_id"]: (
                        local_time(stage.get("updated_at", ""), self.app.config.timezone)
                        + " · " + run_summary(stage), stage,
                    ) for stage in sorted(stages, key=lambda row: (
                        bool(row.get("unresolved_count") or row.get("status") == "failed"),
                        row.get("updated_at") or "",
                    ), reverse=True)
                    if stage.get("task_id") and stage.get("stage") == "continuity"
                }, "maintenance_stages")
            rows = {}
            items = continuity.get("items", [])
            current = active_items(items, now=datetime.now(timezone.utc),
                                   scope=continuity.get("scope", "real_life"))
            inactive = [item for item in items if item not in current and (
                item.get("state") in {"archived", "resolved"} or item.get("expires_at"))]
            for original in [*current, *inactive]:
                item = with_note_metadata(original)
                label = item["topic"] + " · " + NOTE_IMPORTANCE[item["importance"]]
                if item.get("user_created"):
                    label += " · User-created"
                if original not in current:
                    label = "Archived · " + label
                rows["continuity:" + str(item["id"])] = (label, item)
            return self.inspection_rows(rows, "continuity_items")
        return self.inspection_rows({
            f"manifest:{row['id']}": (reply_label(row, self.app.config.timezone), row)
            for row in data.get("manifests", [])
        }, "reply_context")

    def inspection_rows(self, rows: dict, collection: str) -> dict:
        omitted = (self.state.snapshot or {}).get("inspection_omitted", {})
        maintenance_omitted = (collection == "maintenance_stages" and
            (self.state.snapshot or {}).get("continuity", {}).get("maintenance", {}).get(
                "stages_omitted"))
        if omitted.get(collection) or maintenance_omitted:
            rows["inspection-limit"] = ("More entries are saved than this view can show.", {})
        return rows

    def render_list(self) -> None:
        current_life = self.state.category == "continuity"
        self.set_class(current_life, "current-life")
        self.set_class(self.detail_key is not None, "reading")
        self.update_border_title()
        self.query_one("#memory-life-controls").display = current_life
        self.query_one("#memory-create").display = self.state.life_section == "notes"
        self.query_one("#memory-note-actions").display = bool(
            self.detail_key and self.detail_key.startswith("continuity:")
        )
        for selector in ("#memory-create", "#memory-edit", "#memory-delete"):
            self.query_one(selector, Button).disabled = (
                self.loading or self.committing or not self.connected
            )
        self.query_one("#memory-notes").set_class(self.state.life_section == "notes", "selected")
        self.query_one("#memory-history").set_class(
            self.state.life_section == "history", "selected",
        )
        maintenance = (self.state.snapshot or {}).get("continuity", {}).get("maintenance", {})
        self.query_one("#memory-refresh", Button).disabled = (
            self.loading or self.committing or not self.connected
            or bool(maintenance.get("running"))
        )
        table = self.query_one("#memory-table", DataTable)
        selected = self.selected_key()
        table.clear(columns=True)
        dialog_width = min(84, int(self.app.size.width * (
            1 if self.app.size.width < 70 else .96 if self.has_class("compact") else .92
        )))
        padding = 2 if self.has_class("compact") else 4
        table.add_column("Memory", width=max(12, dialog_width - padding - 5))
        self.rows = self.build_rows()
        query = self.query_one("#memory-filter", Input).value.casefold()
        for key, (label, row) in self.rows.items():
            if (not query
                    or query in (label + " " + self.row_text(key, self.detail_row(key))).casefold()):
                table.add_row(Text(label, no_wrap=True, overflow="ellipsis"), key=key)
        if selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected))
        table.display = bool(table.row_count)
        empty = self.query_one("#memory-empty", Static)
        empty.display = not table.row_count
        empty.update(
            "Loading memory…" if self.loading and self.state.snapshot is None else
            "Memory view unavailable. Ctrl+L retries." if self.state.snapshot is None else
            "No entries match your search." if query else
            "No refresh runs yet." if current_life and self.state.life_section == "history" else
            "No current life notes yet." if current_life else
            "No reply memories yet. Send a message first."
        )

    @on(Select.Changed, "#memory-category")
    def category_changed(self, event: Select.Changed) -> None:
        if event.value in dict((value, label) for label, value in CATEGORIES):
            self.state.category, self.state.selected = event.value, None
            self.responsive_copy(self.app.size.width)
            self.update_status()

    @on(Input.Changed, "#memory-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.state.query = event.value
        self.render_list()

    @on(Input.Submitted, "#memory-filter")
    def filter_submitted(self) -> None:
        if self.query_one("#memory-table", DataTable).row_count:
            self.query_one("#memory-table").focus()

    @on(DataTable.RowSelected, "#memory-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.read_selection(event.row_key.value)

    def row_text(self, key: str, row: dict) -> str:
        if key.startswith("manifest:"):
            return reply_details(row)
        if key.startswith("continuity:"):
            return note_details(row)
        if key.startswith("maintenance:"):
            return run_details(row, self.app.config.timezone, compact=self.has_class("short"))
        return "More entries are saved than this view can show."

    def show_detail(self, key: str, row: dict) -> None:
        self.detail_key = key
        self.detail_value = row
        self._detail_compact = self.has_class("short")
        self.query_one("#memory-browser").add_class("detail")
        self.query_one("#memory-text", Static).update(self.row_text(key, row))
        self.responsive_copy(self.app.size.width)

    def detail_row(self, key: str) -> dict:
        summary = self.rows[key][1]
        cached = (self.state.details.get(key)
                  if key.startswith(("manifest:", "maintenance:")) else None)
        if (cached is not None and key.startswith("maintenance:")
                and cached.get("_summary_version") != self.run_version(summary)):
            cached = None
        return cached if cached is not None else summary

    @staticmethod
    def run_version(row: dict) -> tuple:
        return (row.get("status"), row.get("updated_at"), row.get("change_count"),
                row.get("model_calls"), row.get("next_action"), row.get("next_retry_at"),
                row.get("unresolved_count"))

    @work(exclusive=True, group="memory-read", exit_on_error=False)
    async def read_selection(self, key: str, *, reset_scroll: bool = True) -> None:
        if key not in self.rows:
            return
        self.state.selected = key
        row = self.detail_row(key)
        # Keep the existing content in place during a background reload. A
        # shorter loading summary would clamp the reader's scroll position.
        if reset_scroll or key not in self.state.details:
            self.show_detail(key, row)
        scroll = self.query_one("#memory-detail-scroll", VerticalScroll)
        if reset_scroll:
            scroll.scroll_home(animate=False)
            scroll.focus()
        if not (key.startswith(("manifest:", "maintenance:")) and self.connected):
            return
        summary = self.rows[key][1]
        try:
            if key.startswith("maintenance:"):
                detail = await self.client.request(
                    "memory_continuity_run", task_id=summary["task_id"],
                )
                valid = (isinstance(detail, dict) and isinstance(detail.get("changes"), list)
                         and all(isinstance(change, dict) and isinstance(change.get("after"), dict)
                                 and (change.get("before") is None
                                      or isinstance(change["before"], dict))
                                 for change in detail["changes"]))
                if valid:
                    row = {**summary, **detail, "change_count": len(detail["changes"]),
                           "_summary_version": self.run_version(summary)}
            else:
                row = await self.client.request("memory_manifest", manifest_id=row["id"])
                valid = isinstance(row, dict) and isinstance(row.get("manifest"), dict)
            if not valid:
                raise ValueError("This memory detail is no longer available.")
            self.state.details[key] = row
            if self.detail_key == key:
                self.show_detail(key, row)
        except Exception:
            if self.detail_key == key:
                cached = self.detail_row(key) is self.state.details.get(key)
                label = "Note updates" if key.startswith("maintenance:") else "Memory content"
                if not cached:
                    self.query_one("#memory-text", Static).update(
                        label + " couldn't be loaded. Ctrl+L retries."
                    )
                self.status("Showing cached note updates. Ctrl+L retries."
                            if cached and key.startswith("maintenance:") else
                            "Showing cached memories. Ctrl+L retries." if cached else
                            "Note updates are unavailable. Ctrl+L retries."
                            if key.startswith("maintenance:") else
                            "Memory content is unavailable. Ctrl+L retries.", problem=True)

    def update_status(self, *, finished: bool = False) -> None:
        if not self.connected:
            if not self.loading:
                self.status("Disconnected · showing cached memories. Ctrl+L retries."
                            if self.state.snapshot is not None else
                            "Memory is unavailable. Ctrl+L retries.", problem=True)
            return
        if self.state.category != "continuity":
            self.status("Memories sent with each request.")
            return
        maintenance = (self.state.snapshot or {}).get("continuity", {}).get("maintenance", {})
        auxiliary = maintenance.get("auxiliary", {})
        unresolved = maintenance.get("unresolved") or {}
        if unresolved.get("total"):
            message = ("Current life needs configuration attention. Check /preferences." if
                       unresolved.get("configuration_attention") else
                       "Current life budget exhausted. Newer evidence waits. Open Run history." if
                       unresolved.get("set_aside") else
                       "Current life is recovering. Open Run history for causes and next steps.")
            self.status(message, problem=True)
        elif maintenance.get("running"):
            self.status("Refreshing current life in the background…")
        elif auxiliary.get("mode") == "disabled":
            self.status("Choose an auxiliary model in /preferences to refresh.", problem=True)
        elif auxiliary.get("configuration_error") or auxiliary.get("notice"):
            self.status("Check the auxiliary model in /preferences, then retry.", problem=True)
        elif maintenance.get("error") == THINKING_TIMEOUT_NOTICE:
            self.status("Auxiliary model timed out while thinking. Check /preferences, then retry.",
                        problem=True)
        elif maintenance.get("error") or maintenance.get("runtime_error"):
            self.status("Last refresh couldn't finish. Refresh current life to retry.", problem=True)
        else:
            continuity = (self.state.snapshot or {}).get("continuity", {})
            count = continuity.get("active_count", len(active_items(
                continuity.get("items", []), scope=continuity.get("scope", "real_life"))))
            limit = continuity.get("max_notes", self.app.config.memory.continuity_max_notes)
            self.status("Current life refresh finished." if finished else
                        "Recent current life refreshes." if self.state.life_section == "history"
                        else f"{count}/{limit} active notes. Archived topics appear below.")

    @work(exclusive=True, group="memory-reload", exit_on_error=False)
    async def action_reload(self, *, quiet: bool = False) -> None:
        if self.committing:
            return
        self.loading = True
        previous = self.state.snapshot
        changed = not quiet
        if not quiet:
            self.status("Refreshing memories…" if previous else "Loading memory…")
            self.render_list()
        try:
            data = await self.client.request("memory_state", session_id=self.session_id)
            if quiet and (self.committing or self.state.snapshot is not previous):
                return
            changed = changed or data != previous
            self.state.snapshot = data
            self.connected = True
            self.update_status(finished=quiet)
        except Exception:
            changed = True
            self.connected = False
            self.loading = False
            self.update_status()
        finally:
            self.loading = False
            if changed:
                focus = self.app.focused if quiet else None
                self.render_list()
                if self.detail_key and self.detail_key in self.rows:
                    row = self.detail_row(self.detail_key)
                    refreshing_run = (self.connected and self.detail_key.startswith("maintenance:")
                                      and "changes" not in row)
                    if not (refreshing_run and self.detail_key in self.state.details):
                        self.show_detail(self.detail_key, row)
                    if self.connected and (
                            not quiet and self.detail_key.startswith(("manifest:", "maintenance:"))
                            or self.detail_key.startswith("maintenance:") and "changes" not in row):
                        self.read_selection(self.detail_key, reset_scroll=False)
                elif self.detail_key:
                    self.close_detail()
                if focus is not None and focus.is_mounted and not focus.disabled:
                    self.call_after_refresh(focus.focus)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id in {"memory-notes", "memory-history"}:
            self.state.life_section = "history" if event.button.id == "memory-history" else "notes"
            self.state.selected = None
            self.responsive_copy(self.app.size.width)
            self.update_status()
        elif event.button.id == "memory-refresh":
            self.refresh_current_life()
        elif event.button.id == "memory-create":
            self.action_new_note()
        elif event.button.id == "memory-edit":
            self.action_edit_note()
        elif event.button.id == "memory-delete":
            self.action_delete_note()

    def action_new_note(self) -> None:
        if (self.connected and not self.loading and not self.committing
                and self.state.category == "continuity" and self.state.life_section == "notes"
                and self.detail_key is None):
            self.app.push_screen(NoteEditorScreen(
                self.client, self.state.note_drafts, session_id=self.session_id,
            ), self.note_changed)

    def action_edit_note(self) -> None:
        if (self.connected and not self.loading and not self.committing
                and self.detail_key and self.detail_key.startswith("continuity:")
                and self.detail_value):
            self.app.push_screen(NoteEditorScreen(
                self.client, self.state.note_drafts, note=self.detail_value,
                session_id=self.session_id,
            ), self.note_changed)

    def action_delete_note(self) -> None:
        if (self.connected and not self.loading and not self.committing
                and self.detail_key and self.detail_key.startswith("continuity:")
                and self.detail_value):
            note = self.detail_value
            self.app.push_screen(NoteDeleteScreen(self.client, note, self.session_id),
                                 lambda result: self.note_changed(result, deleted=note["id"]))

    def note_changed(self, result: dict | None, *, deleted: str | None = None) -> None:
        if result is None:
            return
        self.state.snapshot = result["snapshot"]
        self.connected = True
        if deleted:
            self.state.note_drafts.pop(deleted, None)
            self.state.selected = None
            self.close_detail()
        else:
            note = result["item"]
            self.state.selected = "continuity:" + note["id"]
            self.query_one("#memory-filter", Input).value = ""
            self.show_detail(self.state.selected, note)
            self.query_one("#memory-detail-scroll").focus()
        self.update_status()
        self.render_list()
        table = self.query_one("#memory-table", DataTable)
        if self.state.selected in table.rows:
            table.move_cursor(row=table.get_row_index(self.state.selected))

    @work(exclusive=True, group="memory-refresh", exit_on_error=False)
    async def refresh_current_life(self) -> None:
        maintenance = (self.state.snapshot or {}).get("continuity", {}).get("maintenance", {})
        if self.committing or self.loading or not self.connected or maintenance.get("running"):
            return
        self.committing = True
        self.render_list()
        self.status("Queueing current life refresh…")
        try:
            self.state.snapshot = await self.client.request(
                "memory_refresh", session_id=self.session_id,
            )
            self.update_status()
            if self.state.snapshot.get("refresh_queued"):
                self.status("Current life refresh queued.")
            elif not self.has_class("problem"):
                self.status("Current life is busy. Try refresh again shortly.")
        except Exception:
            self.connected = False
            self.status("Couldn't refresh current life. Ctrl+L reconnects.", problem=True)
        finally:
            self.committing = False
            self.render_list()
            # A disabled refresh button must not leave keyboard focus stranded.
            if self.app.focused is None:
                self.query_one("#memory-table" if self.rows else "#memory-filter").focus()

    def check_action(self, action, parameters):
        if action == "save":
            return False
        if action == "filter":
            return self.detail_key is None
        return True

    def action_filter(self) -> None:
        self.query_one("#memory-filter").focus()

    def close_detail(self) -> None:
        self.workers.cancel_group(self, "memory-read")
        self.detail_key = None
        self.detail_value = None
        self.query_one("#memory-browser").remove_class("detail")
        self.query_one("#memory-table" if self.rows else "#memory-filter").focus()
        self.responsive_copy(self.app.size.width)

    def action_close(self) -> None:
        selector = self.query_one("#memory-category", Select)
        if selector.expanded:
            selector.expanded = False
            selector.focus()
        elif self.committing:
            self.status("Applying your request. Please wait.")
        elif self.detail_key is not None:
            self.close_detail()
        else:
            self.remember_draft()
            self.dismiss(None)
