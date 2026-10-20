"""Manage scheduled tasks and their runs without leaving the conversation."""

from dataclasses import dataclass, field
from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import ValidationError
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button, Collapsible, ContentSwitcher, DataTable, Input, Select, Static, TextArea,
)

from eidolon.config import AppConfig
from eidolon.scheduler import Job
from eidolon.ui.forms import FormBody, FormCheckbox, FormModal


SCHEDULES = (
    ("Every day at 22:00", "0 22 * * *"),
    ("Weekdays at 09:00", "0 9 * * 1-5"),
    ("Every hour", "0 * * * *"),
    ("Sundays at 18:00", "0 18 * * 0"),
    ("Custom cron", "custom"),
)
TEXT_FIELDS = ("name", "cron", "timezone",
               "permitted_tools", "required_tools", "grace_seconds")


def comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def next_run(job: Job) -> str:
    try:
        occurrence = croniter(job.cron, datetime.now(ZoneInfo(job.timezone))).get_next(datetime)
        return occurrence.strftime("%b %d %H:%M")
    except (ValueError, OverflowError):
        return "No upcoming occurrence"


@dataclass
class JobsState:
    drafts: dict[str, dict] = field(default_factory=dict)
    selected: str | None = None
    query: str = ""


class JobsScreen(FormModal):
    DEFAULT_CSS = """
    JobsScreen #jobs-pages, JobsScreen #jobs-list, JobsScreen #job-runs { height: 1fr; }
    JobsScreen #jobs-table, JobsScreen #job-runs-table { height: 1fr; background: $surface; }
    JobsScreen DataTable:focus > .datatable--cursor { text-style: bold reverse; }
    JobsScreen #jobs-count { height: auto; max-height: 2; color: $text-muted; }
    JobsScreen #jobs-summary {
        height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    JobsScreen #job-prompt {
        height: 5; min-height: 1; border: none; padding: 0 1; background: $background;
    }
    JobsScreen #job-prompt:focus { background: $panel; }
    JobsScreen Select { height: 1; }
    JobsScreen SelectCurrent { height: 1; border: none; padding: 0 1; }
    JobsScreen Checkbox { height: 1; border: none; padding: 0; margin-top: 1; }
    JobsScreen Checkbox:focus { border: none; }
    JobsScreen #job-advanced { padding: 0; margin-top: 1; border: none; }
    JobsScreen #job-advanced > Contents { padding: 0; }
    JobsScreen #job-advanced > CollapsibleTitle { padding: 0; height: 1; }
    JobsScreen #job-advanced > CollapsibleTitle:focus { text-style: bold reverse; }
    JobsScreen #jobs-toggle { width: 8; }
    JobsScreen #job-runs-empty { height: auto; color: $text-muted; }
    JobsScreen #job-run-text, JobsScreen #job-delete-text { height: auto; }
    JobsScreen.compact #jobs-list.has-jobs #jobs-count,
    JobsScreen.short #jobs-summary, JobsScreen.short .form-actions { display: none; }
    JobsScreen.low #jobs-summary { display: none; }
    JobsScreen.compact .field-label { margin-top: 0; }
    JobsScreen.compact Checkbox { margin-top: 0; }
    """
    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("ctrl+n", "new_job", "New job", priority=True),
        Binding("ctrl+t", "toggle_job", "Enable/pause", priority=True),
        Binding("ctrl+d", "delete_job", "Delete", priority=True),
        Binding("ctrl+r", "runs", "Runs", priority=True),
        Binding("ctrl+y", "retry", "Retry run", priority=True),
        Binding("ctrl+f", "filter", "Filter", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+l", "reload", "Reload", priority=True),
    ]

    def __init__(self, client, state: JobsState, config: AppConfig) -> None:
        super().__init__()
        self.client, self.state, self.config = client, state, config
        self.entries: list[Job] = []
        self.runs: list[dict] = []
        self.loaded = False
        self.loading = False
        self.committing = False
        self.editing_id: str | None = None
        self.saved: Job | None = None
        self.baseline: dict = {}
        self.run_id: str | None = None
        self.deleting: Job | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Jobs", classes="form-title", markup=False)
            with ContentSwitcher(initial="jobs-list", id="jobs-pages"):
                with Vertical(id="jobs-list"):
                    yield Input(value=self.state.query, placeholder="Filter jobs...",
                                id="jobs-filter")
                    yield Static("Loading jobs...", id="jobs-count", markup=False)
                    yield DataTable(id="jobs-table", cursor_type="row")
                    yield Static("", id="jobs-summary", markup=False)
                with FormBody(id="job-editor", classes="form-body"):
                    yield Static("Name", classes="field-label")
                    yield Input(placeholder="Daily reflection", id="job-name")
                    yield Static("", id="job-name-error", classes="field-error", markup=False)
                    yield Static("Schedule (job time zone)", classes="field-label")
                    yield Select(SCHEDULES, value="0 22 * * *", allow_blank=False,
                                 id="job-schedule")
                    yield Input(placeholder="0 22 * * *", id="job-cron")
                    yield Static("", id="job-cron-error", classes="field-error", markup=False)
                    yield Static("minute hour day-of-month month day-of-week", classes="field-help")
                    yield Static("Time zone", classes="field-label")
                    yield Input(placeholder="Europe/Paris", id="job-timezone")
                    yield Static("", id="job-timezone-error", classes="field-error", markup=False)
                    yield FormCheckbox("Enabled", value=True, id="job-enabled")
                    yield FormCheckbox("Run in agent mode", id="job-agent-mode")
                    yield Static("Use file editing and shell commands for this job.",
                                 classes="field-help")
                    yield Static("Prompt (what should happen?)", classes="field-label")
                    yield TextArea(soft_wrap=True, id="job-prompt")
                    yield Static("", id="job-prompt-error", classes="field-error", markup=False)
                    with Collapsible(title="Advanced", id="job-advanced",
                                     collapsed_symbol="+", expanded_symbol="-"):
                        yield Static("Provider", classes="field-label")
                        yield Select(
                            [("Current chat provider", ""), *[
                                (profile.display_name or profile.name, profile.name)
                                for profile in self.config.providers
                            ]], value="", allow_blank=False, id="job-provider",
                        )
                        yield Static("", id="job-provider-error", classes="field-error",
                                     markup=False)
                        yield Static("Allowed tools (comma-separated)", classes="field-label")
                        yield Input(id="job-permitted_tools")
                        yield Static("", id="job-permitted_tools-error", classes="field-error",
                                     markup=False)
                        yield Static(
                            "Blank: no tools in companion mode; file/command tools and skills "
                            "in agent mode. "
                            "A list restricts the job to those tools.", classes="field-help",
                        )
                        yield Static("Required tools (must also be allowed)", classes="field-label")
                        yield Input(id="job-required_tools")
                        yield Static("", id="job-required_tools-error", classes="field-error",
                                     markup=False)
                        yield Static("Catch-up window (seconds, 0-604800)", classes="field-label")
                        yield Input(id="job-grace_seconds")
                        yield Static("", id="job-grace_seconds-error", classes="field-error",
                                     markup=False)
                with Vertical(id="job-runs"):
                    yield Static("No runs yet.", id="job-runs-empty", markup=False)
                    yield DataTable(id="job-runs-table", cursor_type="row")
                with FormBody(id="job-run-detail", classes="form-body"):
                    yield Static("", id="job-run-text", markup=False)
                with FormBody(id="job-delete", classes="form-body"):
                    yield Static("", id="job-delete-text", markup=False)
            yield Static("", classes="form-status", markup=False)
            with Horizontal(classes="form-actions"):
                for key, label in (("new", "New"), ("edit", "Edit"), ("toggle", "Enable"),
                                   ("delete", "Delete"), ("runs", "Runs"), ("save", "Save"),
                                   ("retry", "Retry"), ("confirm-delete", "Delete job"),
                                   ("back", "Back")):
                    yield Button(label, id="jobs-" + key, flat=True)
            yield Static("", id="jobs-hint", classes="form-hint", markup=False)
        yield Static("Terminal too small — need 48×12. Resize or press Esc to close.",
                     classes="form-too-small")

    @property
    def page(self) -> str:
        return self.query_one("#jobs-pages", ContentSwitcher).current or "jobs-list"

    @property
    def editing(self) -> bool:
        return self.page == "job-editor"

    @property
    def selected(self) -> Job | None:
        table = self.query_one("#jobs-table", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((job for job in self.entries if job.id == key), None)

    @property
    def selected_run(self) -> dict | None:
        table = self.query_one("#job-runs-table", DataTable)
        if self.page == "job-run-detail":
            key = self.run_id
        elif table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        else:
            return None
        return next((run for run in self.runs if run["id"] == key), None)

    def on_mount(self) -> None:
        self.resize_state()
        self.update_controls()
        self.query_one("#jobs-filter").focus()
        self.refresh_jobs()

    def status(self, message: str, *, problem: bool = False) -> None:
        super().status(message, problem=problem)
        self.query_one(".form-status").display = bool(message)

    def responsive_copy(self, width: int) -> None:
        self.set_class(self.app.size.height < 24, "low")
        if self.editing:
            hint = "Ctrl+S save · Esc back (keeps draft)"
        elif self.page == "job-delete":
            hint = "Ctrl+D confirm delete · Esc cancel"
        elif self.page in {"job-runs", "job-run-detail"}:
            hint = "Enter details · Ctrl+Y retry failed · Esc back"
        else:
            hint = "Enter edit · ^N new · ^T on/off · ^R runs · Esc close"
            if width < 60:
                hint = "Enter edit · ^N new · ^T on/off · Esc close"
        if self.app.ascii:
            hint = hint.replace("·", "|")
        self.query_one("#jobs-hint", Static).update(hint)
        if self.loaded:
            self.render_jobs()
            self.render_runs()

    def position_dialog(self) -> None:
        super().position_dialog()
        height = int(self.query_one(".form-dialog").styles.height.value)
        self.query_one("#job-prompt").styles.height = max(1, min(5, height - 5))
        if self.loaded:
            self.render_jobs()
            self.render_runs()

    def check_action(self, action, parameters):
        if action == "close":
            return True
        if self.committing or self.loading:
            return False
        if action == "save":
            return self.editing
        if action in {"new_job", "filter", "runs"}:
            return self.page == "jobs-list"
        if action == "toggle_job":
            return self.page == "jobs-list" and self.selected is not None
        if action == "delete_job":
            return self.page == "job-delete" or (
                self.page == "jobs-list" and self.selected is not None
            )
        if action == "retry":
            run = self.selected_run
            return (self.page in {"job-runs", "job-run-detail"} and run is not None
                    and run["status"] == "failed")
        return True

    def update_controls(self) -> None:
        selected = self.selected
        self.query_one("#job-editor").disabled = self.committing or self.loading
        visible = ({"new", "edit", "toggle", "delete", "runs"} if self.page == "jobs-list" else
                   {"save", "back"} if self.editing else
                   {"confirm-delete", "back"} if self.page == "job-delete" else {"retry", "back"})
        self.query_one("#jobs-toggle", Button).label = (
            "Pause" if selected and selected.enabled else "Enable"
        )
        for button in self.query(Button):
            key = button.id.removeprefix("jobs-")
            button.display = key in visible
            button.disabled = self.committing or self.loading or (
                key in {"edit", "toggle", "delete"} and selected is None
            ) or (key == "retry" and not self.check_action("retry", ()))
        self.refresh_bindings()

    def table_width(self) -> int:
        return max(30, self.query_one(".form-dialog").content_size.width)

    def render_jobs(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        selected = self.selected
        key = selected.id if selected else self.state.selected
        query = self.query_one("#jobs-filter", Input).value.casefold()
        rows = [job for job in self.entries if query in (
            f"{job.name} {job.id} {job.cron} {job.timezone} "
            f"{'Agent' if job.agent_mode else 'Companion'} {'On' if job.enabled else 'Paused'}"
        ).casefold()]
        table.clear(columns=True)
        table.show_header = self.app.size.height >= 24
        table.add_column("State", width=6)
        table.add_column("Job", width=max(8, self.table_width() - 23))
        table.add_column("Mode", width=9)
        for job in rows:
            table.add_row("On" if job.enabled else "Paused",
                          Text(job.name, overflow="ellipsis", no_wrap=True),
                          "Agent" if job.agent_mode else "Companion", key=job.id)
        table.display = bool(rows)
        self.query_one("#jobs-list").set_class(bool(rows), "has-jobs")
        count = (f"{len(rows)} of {len(self.entries)} jobs" if query and rows else
                 f"{len(rows)} job{'s' if len(rows) != 1 else ''}" if rows else
                 "No jobs match. Clear the filter or create a job." if query else
                 "No jobs yet. Ctrl+N to create your first scheduled task.")
        if self.has_class("short") and not rows:
            count = "No matches. Ctrl+F to filter." if query else "No jobs. Ctrl+N to create."
        self.query_one("#jobs-count", Static).update(count)
        if key and key in table.rows:
            table.move_cursor(row=table.get_row_index(key))
        self.update_summary()
        self.update_controls()

    def update_summary(self) -> None:
        job = self.selected
        summary = ""
        if job:
            summary = f"{job.cron} · {job.timezone}"
            if job.enabled:
                summary += " · Next " + next_run(job)
        self.query_one("#jobs-summary", Static).update(
            summary.replace("·", "|") if self.app.ascii else summary
        )

    def render_runs(self) -> None:
        table = self.query_one("#job-runs-table", DataTable)
        selected = self.selected_run
        key = selected["id"] if selected else None
        table.clear(columns=True)
        table.show_header = self.app.size.height >= 24
        table.add_column("Scheduled", width=11)
        table.add_column("Job", width=max(8, self.table_width() - 28))
        table.add_column("Status", width=9)
        jobs = {job.id: job for job in self.entries}
        rows = self.runs
        for run in rows:
            job = jobs.get(run["job_id"])
            try:
                stamp = datetime.fromisoformat(run["scheduled_at"]).astimezone(
                    ZoneInfo(job.timezone if job else self.config.timezone)
                ).strftime("%m-%d %H:%M")
            except ValueError:
                stamp = run["scheduled_at"]
            table.add_row(Text(stamp, overflow="ellipsis", no_wrap=True),
                          Text(job.name if job else run["job_id"],
                               overflow="ellipsis", no_wrap=True),
                          run["status"], key=run["id"])
        if key and key in table.rows:
            table.move_cursor(row=table.get_row_index(key))
        table.display = bool(rows)
        self.query_one("#job-runs-empty").display = not rows
        self.query_one("#job-runs-empty", Static).update(
            "No runs yet. Enabled jobs run while the service is running."
        )

    @work(exclusive=True, group="jobs-load", exit_on_error=False)
    async def refresh_jobs(self, *, reload_editor: bool = False) -> None:
        editing_id = self.editing_id
        self.loading = True
        self.update_controls()
        try:
            data = await self.client.request("jobs")
            self.entries = [Job.model_validate(job) for job in data["jobs"]]
            self.runs = data["runs"]
            self.loaded = True
            self.status("")
            self.render_jobs()
            self.render_runs()
            if reload_editor and self.editing and self.editing_id == editing_id:
                job = next((job for job in self.entries if job.id == editing_id), None)
                if job is None:
                    raise ValueError(
                        "This job was removed. Your draft is kept; Esc returns to the list."
                    )
                self.state.drafts.pop(editing_id, None)
                self.open_editor(job)
            if self.app.screen is self and self.page == "jobs-list":
                self.query_one("#jobs-table" if self.selected else "#jobs-filter").focus()
        except Exception as exc:
            self.status(f"Could not load jobs: {exc}. Ctrl+L to retry.", problem=True)
            if not self.loaded:
                self.query_one("#jobs-count", Static).update("Jobs unavailable. Ctrl+L to retry.")
        finally:
            self.loading = False
            self.update_controls()

    @on(Input.Changed, "#jobs-filter")
    def filter_changed(self) -> None:
        self.state.query = self.query_one("#jobs-filter", Input).value
        if self.loaded:
            self.render_jobs()

    @on(Input.Submitted, "#jobs-filter")
    def filter_submitted(self) -> None:
        if self.selected:
            self.query_one("#jobs-table").focus()

    @on(DataTable.RowHighlighted, "#jobs-table")
    def job_highlighted(self) -> None:
        if self.selected:
            self.state.selected = self.selected.id
        self.update_summary()
        self.update_controls()

    @on(DataTable.RowHighlighted, "#job-runs-table")
    def run_highlighted(self) -> None:
        self.update_controls()

    @on(DataTable.RowSelected, "#jobs-table")
    def job_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.selected and not self.committing and not self.loading:
            self.open_editor(self.selected)

    @on(DataTable.RowSelected, "#job-runs-table")
    def run_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        run = self.selected_run
        if run and not self.committing and not self.loading:
            self.run_id = run["id"]
            self.show_page("job-run-detail", "Run details")
            self.render_run_detail(run)
            self.query_one("#job-run-detail").can_focus = True
            self.query_one("#job-run-detail").focus()

    def render_run_detail(self, run: dict) -> None:
        self.query_one("#job-run-text", Static).update(
            f"Job: {run['job_id']}\nScheduled: {run['scheduled_at']}\nStatus: {run['status']}\n\n"
            + ("Error: " + run["error"] if run.get("error")
               else run.get("result") or "No result yet.")
        )

    @on(Select.Changed, "#job-schedule")
    def schedule_changed(self, event: Select.Changed) -> None:
        if event.value != "custom" and isinstance(event.value, str):
            self.query_one("#job-cron", Input).value = event.value

    @on(Input.Changed, "#job-cron")
    def cron_changed(self) -> None:
        value = self.query_one("#job-cron", Input).value
        preset = value if value in {expression for _, expression in SCHEDULES} else "custom"
        self.query_one("#job-schedule", Select).value = preset

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.committing or self.loading:
            return
        key = event.button.id.removeprefix("jobs-")
        if key == "edit" and self.selected:
            self.open_editor(self.selected)
        else:
            action = {"new": self.action_new_job, "toggle": self.action_toggle_job,
                      "delete": self.action_delete_job, "runs": self.action_runs,
                      "save": self.action_save, "retry": self.action_retry,
                      "confirm-delete": self.confirm_delete, "back": self.action_close}.get(key)
            if action:
                action()

    def open_editor(self, job: Job | None) -> None:
        self.saved = job
        self.editing_id = job.id if job else None
        source = job or Job(id="job-" + uuid.uuid4().hex[:16], name="", prompt="",
                            timezone=self.config.timezone, enabled=True)
        baseline = source.model_dump()
        for key in ("permitted_tools", "required_tools"):
            baseline[key] = ", ".join(baseline[key])
        baseline["grace_seconds"] = str(baseline["grace_seconds"])
        baseline.pop("id")
        self.baseline = baseline
        values = self.state.drafts.get(self.editing_id or "", baseline)
        for key in TEXT_FIELDS:
            self.query_one("#job-" + key, Input).value = values[key]
        self.query_one("#job-prompt", TextArea).load_text(values["prompt"])
        for key in ("enabled", "agent_mode"):
            self.query_one("#job-" + key.replace("_", "-"), FormCheckbox).value = values[key]
        provider = values["provider"] or ""
        options = [("Current chat provider", ""), *[
            (profile.display_name or profile.name, profile.name)
            for profile in self.config.providers
        ]]
        if provider and provider not in {value for _, value in options}:
            options.append((provider + " (unavailable)", provider))
        self.query_one("#job-provider", Select).set_options(options)
        self.query_one("#job-provider", Select).value = provider
        self.show_page("job-editor", "Edit job" if job else "New job")
        self.status("Unsaved draft restored." if values != baseline else "")
        self.query_one("#job-name").focus()

    def editor_values(self) -> dict:
        return {**{key: self.query_one("#job-" + key, Input).value for key in TEXT_FIELDS},
                "prompt": self.query_one("#job-prompt", TextArea).text,
                "provider": self.query_one("#job-provider", Select).value or None,
                "enabled": self.query_one("#job-enabled", FormCheckbox).value,
                "agent_mode": self.query_one("#job-agent-mode", FormCheckbox).value}

    def remember_draft(self) -> None:
        if self.editing:
            values = self.editor_values()
            if values != self.baseline:
                self.state.drafts[self.editing_id or ""] = values
            else:
                self.state.drafts.pop(self.editing_id or "", None)

    def action_new_job(self) -> None:
        if self.page == "jobs-list" and not self.committing and not self.loading:
            self.open_editor(None)

    @work(group="jobs-save", exit_on_error=False)
    async def action_save(self) -> None:
        if not self.editing or self.committing or self.loading:
            return
        self.clear_errors()
        values = self.editor_values()
        for key in ("name", "prompt"):
            if not values[key].strip():
                self.field_error("job-" + key, "Enter a " + key + ".")
                return
        data = dict(values)
        data["name"] = data["name"].strip()
        for key in ("cron", "timezone"):
            data[key] = data[key].strip()
        for key in ("permitted_tools", "required_tools"):
            data[key] = comma_list(data[key])
        data["id"] = self.editing_id or "job-" + uuid.uuid4().hex[:16]
        try:
            job = Job.model_validate(data)
        except ValidationError as exc:
            error = exc.errors(include_url=False, include_input=False)[0]
            key = error["loc"][0] if error["loc"] else "required_tools"
            self.field_error("job-" + key, error["msg"].removeprefix("Value error, "))
            return
        profile = next((profile for profile in self.config.providers
                        if profile.name == (job.provider or self.config.provider)), None)
        if profile is None:
            self.field_error("job-provider", "Choose a configured provider for this job.")
            return
        if job.enabled and job.agent_mode and not profile.tools:
            self.field_error("job-provider",
                             "Enable tool calling for this provider to use agent mode.")
            return
        await self.commit("save_job", job=job.model_dump(), create=self.saved is None,
                          **({"expected_job": self.saved.model_dump()} if self.saved else {}))

    @work(group="jobs-save", exit_on_error=False)
    async def action_toggle_job(self) -> None:
        job = self.selected
        if self.page == "jobs-list" and job and not self.committing and not self.loading:
            await self.commit("toggle_job", id=job.id, enabled=not job.enabled,
                              expected_job=job.model_dump())

    def action_delete_job(self) -> None:
        if self.page == "job-delete":
            self.confirm_delete()
        elif (self.selected and self.page == "jobs-list"
              and not self.committing and not self.loading):
            self.deleting = self.selected
            self.show_page("job-delete", "Delete job?")
            self.query_one("#job-delete-text", Static).update(
                f"Delete {self.deleting.name}?\n\n"
                "This removes its schedule. Run history remains."
            )
            self.query_one("#jobs-back").focus()

    @work(group="jobs-save", exit_on_error=False)
    async def confirm_delete(self) -> None:
        if self.deleting and self.page == "job-delete" and not self.committing:
            await self.commit("delete_job", id=self.deleting.id,
                              expected_job=self.deleting.model_dump())

    async def commit(self, method: str, **params) -> None:
        previous_focus = self.focused
        self.committing = True
        self.status("Saving job...")
        self.update_controls()
        try:
            result = await self.client.request(method, **params)
            job = Job.model_validate(result["job"]) if result["job"] else None
            key = job.id if job else params["id"]
            index = next((i for i, item in enumerate(self.entries) if item.id == key),
                         len(self.entries))
            self.entries = [item for item in self.entries if item.id != key]
            if job:
                self.entries.insert(index, job)
                self.state.selected = job.id
            if method == "save_job":
                self.state.drafts.pop(self.editing_id or "", None)
                self.state.query = ""
                self.query_one("#jobs-filter", Input).value = ""
            elif method == "delete_job":
                self.state.drafts.pop(key, None)
            self.show_list()
            self.render_jobs()
            self.render_runs()
        except Exception as exc:
            self.status(f"Could not save job: {exc}", problem=True)
        finally:
            self.committing = False
            self.update_controls()
            if self.app.screen is self:
                if self.page == "jobs-list":
                    self.query_one("#jobs-table" if self.selected else "#jobs-filter").focus()
                elif previous_focus is not None:
                    self.call_after_refresh(previous_focus.focus)

    def action_runs(self) -> None:
        self.show_page("job-runs", "Recent runs")
        self.render_runs()
        table = self.query_one("#job-runs-table", DataTable)
        self.query_one("#job-runs-table" if table.row_count else "#jobs-back").focus()

    @work(group="jobs-save", exit_on_error=False)
    async def action_retry(self) -> None:
        run = self.selected_run
        if not self.check_action("retry", ()) or run is None:
            return
        job = next((job for job in self.entries if job.id == run["job_id"]), None)
        if job is None or not job.enabled:
            self.status("Enable this job before retrying its run.", problem=True)
            return
        self.committing = True
        self.update_controls()
        try:
            await self.client.request("retry_job", run_id=run["id"])
            run["status"] = "queued"
            run["error"] = None
            self.status("Run queued for retry.")
            self.render_runs()
            if self.page == "job-run-detail":
                self.render_run_detail(run)
        except Exception as exc:
            self.status(f"Could not retry run: {exc}", problem=True)
        finally:
            self.committing = False
            self.update_controls()

    def show_page(self, page: str, title: str) -> None:
        self.query_one("#jobs-pages", ContentSwitcher).current = page
        self.query_one(".form-title", Static).update(title)
        self.status("")
        self.clear_errors()
        self.update_controls()
        self.resize_state()

    def show_list(self) -> None:
        self.show_page("jobs-list", "Jobs")
        self.query_one("#jobs-table" if self.selected else "#jobs-filter").focus()

    def action_filter(self) -> None:
        self.query_one("#jobs-filter").focus()

    def action_reload(self) -> None:
        if self.editing:
            if self.editing_id is None:
                self.state.drafts.pop("", None)
                self.open_editor(None)
            else:
                self.refresh_jobs(reload_editor=True)
        else:
            self.refresh_jobs()

    def action_close(self) -> None:
        if self.committing:
            self.status("Saving is in progress. Wait for it to finish.")
        elif any(selector.expanded for selector in self.query(Select)):
            super().action_close()
        elif self.editing:
            self.remember_draft()
            self.show_list()
        elif self.page == "job-run-detail":
            self.show_page("job-runs", "Recent runs")
            self.query_one("#job-runs-table").focus()
        elif self.page != "jobs-list":
            self.show_list()
        elif self.query_one("#jobs-filter", Input).value:
            self.query_one("#jobs-filter", Input).value = ""
        else:
            self.dismiss(None)
