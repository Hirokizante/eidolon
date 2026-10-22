import asyncio

import pytest
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Select, Static, TextArea
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.scheduler import Job
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import CommandMenu, find_command
from eidolon.ui.forms import FormCheckbox
from eidolon.ui.jobs import JobsScreen
from test_ui import FakeClient


class JobsClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []
        self.runs: list[dict] = []
        self.failure: str | None = None
        self.save_gate: asyncio.Event | None = None

    async def request(self, method, **params):
        if method not in {"jobs", "save_job", "toggle_job", "delete_job", "retry_job"}:
            return await super().request(method, **params)
        self.calls.append((method, params))
        if method == self.failure:
            raise ConnectionError("Service disconnected")
        if method == "jobs":
            return {"jobs": [job.model_dump() for job in self.jobs], "runs": self.runs}
        if self.save_gate:
            await self.save_gate.wait()
        if method == "retry_job":
            next(run for run in self.runs if run["id"] == params["run_id"])["status"] = "queued"
            return {"queued": True}
        if method == "save_job":
            job = Job.model_validate(params["job"])
            self.jobs = [item for item in self.jobs if item.id != job.id] + [job]
        else:
            job = next(job for job in self.jobs if job.id == params["id"])
            if method == "delete_job":
                self.jobs.remove(job)
                job = None
            else:
                job.enabled = params["enabled"]
        return {"job": job.model_dump() if job else None}


def install(client: JobsClient, **changes) -> Job:
    job = Job.model_validate({"id": "daily", "name": "Daily reflection", "prompt": "Write a note.",
                              "enabled": True, **changes})
    client.jobs.append(job)
    return job


# Layout boundaries in Unicode; ASCII appearance also exercised at the floor.
@pytest.mark.parametrize("size,ascii_mode", [
    *((size, False) for size in [(140, 40), (80, 24), (60, 24), (60, 18), (48, 12)]),
    ((48, 12), True),
])
async def test_jobs_overlay_fits_and_preserves_chat(size, ascii_mode, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    if ascii_mode:
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = JobsClient()
    install(client, name="每日计划 e\u0301 🌸 " * 12, agent_mode=True)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 🌸")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        await pilot.press("alt+7")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobsScreen)
        assert app.query_one(ContentSwitcher).current == "room"
        assert not app.query("#jobs-editor")
        dialog = screen.query_one(".form-dialog")
        table = screen.query_one("#jobs-table", DataTable)
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        assert dialog.styles.border.top[0] == ("ascii" if ascii_mode else "solid")
        assert dialog.styles.border.top[1].hex == app.theme_variables["foreground"]
        assert table.region.height >= (2 if table.show_header else 1)
        assert table.virtual_size.width <= table.size.width
        hint = screen.query_one("#jobs-hint")
        assert hint.region.bottom <= dialog.content_region.bottom
        assert hint.region.right <= dialog.content_region.right
        await pilot.press("ctrl+f", "x", "ctrl+enter")
        assert app.focused is screen.query_one("#jobs-filter")
        assert composer.text == "Unsent draft 🌸" and composer.selection == selection
        assert not app.busy
        await pilot.press("escape", "escape")
        assert app.focused is composer and composer.selection == selection


async def test_create_edit_pause_delete_and_draft_restore():
    client = JobsClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.execute_command("/jobs")
        await pilot.pause()
        screen = app.screen
        assert "Ctrl+N" in str(screen.query_one("#jobs-count", Static).render())
        await pilot.press("ctrl+n")
        screen.query_one("#job-name", Input).value = "Morning plan"
        screen.query_one("#job-prompt", TextArea).load_text("Review my tasks and update the plan.")
        screen.query_one("#job-cron", Input).value = "30 8 * * 1-5"
        screen.query_one("#job-agent-mode", FormCheckbox).value = True
        await pilot.press("escape", "escape", "alt+7", "ctrl+n")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#job-cron", Input).value == "30 8 * * 1-5"
        assert screen.query_one("#job-schedule", Select).value == "custom"
        assert screen.query_one("#job-agent-mode", FormCheckbox).value
        assert not client.jobs
        await pilot.press("ctrl+s")
        await pilot.pause()
        job = client.jobs[0]
        assert job.name == "Morning plan" and job.agent_mode and job.enabled
        assert not screen.editing and not app.jobs_state.drafts
        assert screen.query_one("#jobs-table", DataTable).get_row(job.id)[2] == "Agent"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert not job.enabled
        await pilot.press("enter")
        await pilot.pause()
        assert screen.editing
        screen.query_one("#job-agent-mode", FormCheckbox).value = False
        screen.query_one("#job-prompt", TextArea).load_text("Just write a note.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.jobs[0].id == job.id
        assert not client.jobs[0].agent_mode and not client.jobs[0].enabled
        await pilot.press("ctrl+d")
        assert screen.page == "job-delete" and app.focused is screen.query_one("#jobs-back")
        await pilot.press("escape")
        assert client.jobs and screen.page == "jobs-list"
        await pilot.press("ctrl+d", "ctrl+d")
        await pilot.pause()
        assert not client.jobs and screen.page == "jobs-list"


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_editor_fields_keyboard_reachable_and_resize_retains_draft(size):
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=JobsClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "ctrl+n")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#job-name", Input).value = "New job"
        for key in ("job-name", "job-schedule", "job-cron", "job-timezone",
                    "job-enabled", "job-agent-mode", "job-prompt"):
            assert app.focused is screen.query_one("#" + key)
            assert app.focused.content_region.intersection(
                screen.query_one(".form-body").content_region
            ).height >= 1
            if isinstance(app.focused, FormCheckbox):
                assert app.focused.region.height == 1
            await pilot.press("tab")
            await pilot.pause()
        await pilot.resize_terminal(40, 10)
        await pilot.pause()
        assert screen.has_class("too-small")
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert not screen.has_class("too-small") and screen.editing
        assert screen.query_one("#job-name", Input).value == "New job"


async def test_validation_and_save_failure_preserve_values():
    client = JobsClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "ctrl+n", "ctrl+s")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#job-name-error").has_class("problem")
        screen.query_one("#job-name", Input).value = "Test job"
        screen.query_one("#job-prompt", TextArea).load_text("Do a task.")
        for field, invalid, valid in (("cron", "nope", "0 9 * * *"),
                                      ("timezone", "Nowhere/Invalid", "UTC"),
                                      ("grace_seconds", "abc", "60"),
                                      ("required_tools", "read_file", "")):
            screen.query_one("#job-" + field, Input).value = invalid
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.query_one("#job-" + field + "-error").has_class("problem")
            assert not any(method == "save_job" for method, _ in client.calls)
            screen.query_one("#job-" + field, Input).value = valid
        client.failure = "save_job"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert screen.editing and screen.query_one("#job-prompt", TextArea).text == "Do a task."
        assert not screen.committing
        assert "Service disconnected" in str(screen.query_one(".form-status", Static).render())
        client.failure = None
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(client.jobs) == 1


async def test_existing_advanced_fields_survive_name_edit():
    client = JobsClient()
    job = install(client, provider="local", grace_seconds=1234,
                  permitted_tools=["read_file", "read_skill"], required_tools=["read_file"])
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "enter")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#job-name", Input).value = "Renamed"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.jobs[0].model_dump() == {**job.model_dump(), "name": "Renamed"}


async def test_schedule_presets_and_mode_are_keyboard_operable():
    client = JobsClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "ctrl+n", "tab", "enter", "down", "down", "enter")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#job-cron", Input).value == "0 * * * *"
        screen.query_one("#job-agent-mode").focus()
        await pilot.press("space")
        assert screen.query_one("#job-agent-mode", FormCheckbox).value
        screen.query_one("#job-name", Input).value = "Hourly task"
        screen.query_one("#job-prompt", TextArea).load_text("Check the task list.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.jobs[0].cron == "0 * * * *" and client.jobs[0].agent_mode


async def test_reload_fetches_saved_job_and_failure_keeps_editor_draft():
    client = JobsClient()
    install(client)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "enter")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#job-prompt", TextArea).load_text("Unsaved changes")
        client.failure = "jobs"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.editing and screen.query_one("#job-prompt", TextArea).text == "Unsaved changes"
        client.failure = None
        client.jobs[0].prompt = "Changed by another editor"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.query_one("#job-prompt", TextArea).text == "Changed by another editor"
        assert not app.jobs_state.drafts
        screen.query_one("#job-prompt", TextArea).load_text("A new draft")
        await pilot.press("f1", "escape", "alt+7", "enter")
        await pilot.pause()
        assert app.screen.query_one("#job-prompt", TextArea).text == "A new draft"


async def test_resize_keeps_selection_and_shows_rows_in_small_split():
    client = JobsClient()
    for index in range(5):
        install(client, id=f"job-{index}", name=f"Task {index}")
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "down", "down")
        await pilot.pause()
        screen = app.screen
        assert screen.selected.id == "job-2"
        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        table = screen.query_one("#jobs-table", DataTable)
        assert screen.selected.id == "job-2" and table.size.height >= 3
        assert table.virtual_size.width <= table.size.width
        await pilot.resize_terminal(48, 12)
        await pilot.pause()
        assert screen.selected.id == "job-2" and table.size.height >= 1
        assert table.virtual_size.width <= table.size.width


async def test_runs_show_failure_details_and_retry_without_changing_schedule():
    client = JobsClient()
    install(client)
    client.runs = [{"id": "run-one", "job_id": "daily", "status": "failed",
                    "scheduled_at": "2026-10-13T22:00:00+08:00",
                    "error": "Provider offline", "result": None}]
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "ctrl+r", "enter")
        await pilot.pause()
        screen = app.screen
        assert screen.page == "job-run-detail"
        assert "Provider offline" in str(screen.query_one("#job-run-text", Static).render())
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert ("retry_job", {"run_id": "run-one"}) in client.calls
        assert "Status: queued" in str(screen.query_one("#job-run-text", Static).render())
        assert client.jobs[0].enabled
        await pilot.press("escape", "escape", "ctrl+d", "ctrl+d")
        await pilot.pause()
        assert client.runs and not client.jobs


async def test_saving_blocks_double_submission_and_keeps_menu_open():
    client = JobsClient()
    install(client)
    client.save_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+7", "enter")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#job-name", Input).value = "Updated job"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert screen.committing and screen.query_one("#jobs-save", Button).disabled
        await pilot.press("ctrl+s", "escape", "ctrl+p", "alt+2")
        assert app.screen is screen
        assert len([method for method, _ in client.calls if method == "save_job"]) == 1
        client.save_gate.set()
        await pilot.pause()
        assert not screen.editing and client.jobs[0].name == "Updated job"


async def test_offline_reload_and_palette_keep_job_draft():
    assert "cron" in find_command("jobs").description
    client = JobsClient()
    client.failure = "jobs"
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+7")
        await pilot.pause()
        assert "unavailable" in str(app.screen.query_one("#jobs-count", Static).render())
        client.failure = None
        await pilot.press("ctrl+l", "ctrl+n")
        await pilot.pause()
        app.screen.query_one("#job-name", Input).value = "A draft"
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandMenu) and len(app.screen_stack) == 2
        app.screen.query_one(Input).value = "/jobs"
        await pilot.press("enter", "ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, JobsScreen) and len(app.screen_stack) == 2
        assert app.screen.query_one("#job-name", Input).value == "A draft"
