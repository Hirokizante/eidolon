"""Create, edit, and switch companion skills without leaving the conversation."""

from dataclasses import dataclass, field

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Static, TextArea

from eidolon.skills import SkillError, validate_skill_fields
from eidolon.ui.forms import FormBody, FormCheckbox, FormModal


@dataclass
class SkillsState:
    drafts: dict[str, dict] = field(default_factory=dict)
    selected: str | None = None
    query: str = ""


class SkillsScreen(FormModal):
    DEFAULT_CSS = """
    SkillsScreen #skills-pages, SkillsScreen #skills-list { height: 1fr; }
    SkillsScreen #skills-count { height: auto; max-height: 2; color: $text-muted; }
    SkillsScreen #skills-table { height: 1fr; background: $surface; }
    SkillsScreen DataTable:focus > .datatable--cursor { text-style: bold reverse; }
    SkillsScreen #skill-instructions {
        height: 6; min-height: 1; border: none; padding: 0 1; background: $background;
    }
    SkillsScreen #skill-instructions:focus { background: $panel; }
    SkillsScreen Checkbox { height: 1; border: none; padding: 0; margin-top: 1; }
    SkillsScreen #skills-toggle { width: 10; }
    SkillsScreen.short #skills-list.has-skills #skills-count { display: none; }
    SkillsScreen.short .form-actions { display: none; }
    """
    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("ctrl+n", "new_skill", "New skill", priority=True),
        Binding("ctrl+t", "toggle_skill", "Turn on/off", priority=True),
        Binding("ctrl+f", "filter", "Filter", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+l", "reload", "Reload", priority=True),
    ]

    def __init__(self, client, state: SkillsState):
        super().__init__()
        self.client, self.state = client, state
        self.entries: list[dict] = []
        self.loaded = False
        self.loading = False
        self.committing = False
        self.editing_name: str | None = None
        self.saved: dict = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Skills", classes="form-title", markup=False)
            with ContentSwitcher(initial="skills-list", id="skills-pages"):
                with Vertical(id="skills-list"):
                    yield Input(value=self.state.query, placeholder="Filter skills…", id="skills-filter")
                    yield Static("Loading skills…", id="skills-count", markup=False)
                    yield DataTable(id="skills-table", cursor_type="row")
                with FormBody(id="skill-editor", classes="form-body"):
                    yield Static("Name · lowercase, with hyphens", classes="field-label")
                    yield Input(placeholder="my-skill", id="skill-name")
                    yield Static("", id="skill-name-error", classes="field-error", markup=False)
                    yield Static("When should this skill be used?", classes="field-label")
                    yield Input(placeholder="Use when…", id="skill-description")
                    yield Static("", id="skill-description-error", classes="field-error", markup=False)
                    yield Static("Instructions · Markdown", classes="field-label")
                    yield TextArea(soft_wrap=True, id="skill-instructions")
                    yield Static("", id="skill-instructions-error", classes="field-error", markup=False)
                    yield FormCheckbox("Enabled", value=True, id="skill-enabled")
            yield Static("", classes="form-status", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("New", id="skills-new", flat=True)
                yield Button("Edit", id="skills-edit", flat=True)
                yield Button("Turn on", id="skills-toggle", flat=True)
                yield Button("Save", id="skills-save", flat=True)
                yield Button("Back", id="skills-back", flat=True)
            yield Static("", id="skills-hint", classes="form-hint", markup=False)
        yield Static(
            "Terminal too small — need 48×12. Resize or press Esc to close.",
            classes="form-too-small",
        )

    @property
    def editing(self) -> bool:
        return self.query_one("#skills-pages", ContentSwitcher).current == "skill-editor"

    @property
    def selected(self) -> dict | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        name = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((item for item in self.entries if item["name"] == name), None)

    def on_mount(self) -> None:
        self.resize_state()
        self.update_controls()
        self.query_one("#skills-filter").focus()
        self.refresh_skills()

    def responsive_copy(self, width: int) -> None:
        hint = ("Ctrl+S save · Esc back" if self.editing else
                "Enter edit · Ctrl+N new · Ctrl+T on/off · Esc close")
        if width < 60 and not self.editing:
            hint = "Enter edit · ^N new · ^T on/off · Esc close"
        if self.app.ascii:
            hint = hint.replace("·", "|")
        self.query_one("#skills-hint", Static).update(hint)
        if self.loaded:
            self.render_skills()

    def position_dialog(self) -> None:
        super().position_dialog()
        height = int(self.query_one(".form-dialog").styles.height.value)
        self.query_one(TextArea).styles.height = max(1, min(6, height - 5))

    def status(self, message: str, *, problem: bool = False) -> None:
        super().status(message, problem=problem)
        self.query_one(".form-status").display = bool(message)

    def check_action(self, action, parameters):
        if action == "close":
            return True
        if self.committing or self.loading:
            return False
        if action == "save":
            return self.editing
        if action == "filter":
            return not self.editing
        if action == "new_skill":
            return not self.editing
        if action == "toggle_skill":
            return not self.editing and self.selected is not None
        return True

    def update_controls(self) -> None:
        editing = self.editing
        self.query_one("#skill-editor").disabled = self.committing
        for key in ("new", "edit", "toggle"):
            self.query_one(f"#skills-{key}", Button).display = not editing
        for key in ("save", "back"):
            self.query_one(f"#skills-{key}", Button).display = editing
        selected = self.selected
        toggle = self.query_one("#skills-toggle", Button)
        toggle.label = "Turn off" if selected and selected["enabled"] else "Turn on"
        for button in self.query(Button):
            button.disabled = self.committing or self.loading or (
                button.id in {"skills-edit", "skills-toggle"} and selected is None
            )
        self.refresh_bindings()

    def render_skills(self) -> None:
        table = self.query_one(DataTable)
        selected = self.selected
        name = selected["name"] if selected else self.state.selected
        table.clear(columns=True)
        table.show_header = self.app.size.height >= 24
        dialog_width = min(84, int(self.app.size.width * (
            1 if self.has_class("narrow") else .96 if self.has_class("compact") else .92
        )))
        table.add_column("State", width=7)
        table.add_column("Skill", width=max(12, dialog_width - (16 if self.has_class("compact") else 18)))
        query = self.query_one("#skills-filter", Input).value.casefold()
        rows = [item for item in self.entries if query in
                f"{item['name']} {item['description']}".casefold()]
        for item in rows:
            state = "Invalid" if item["error"] else "On" if item["enabled"] else "Off"
            table.add_row(state, Text(item["name"], overflow="ellipsis", no_wrap=True),
                          key=item["name"])
        table.display = bool(rows)
        self.query_one("#skills-list").set_class(bool(rows), "has-skills")
        text = (f"{len(rows)} of {len(self.entries)} skills" if query and rows else
                f"{len(rows)} skill{'s' if len(rows) != 1 else ''}" if rows else
                "No skills match. Clear the filter or create a skill." if query else
                "No skills installed. Ctrl+N to create your first skill.")
        if self.has_class("short") and not rows:
            text = "No matches. Ctrl+F to filter." if query else "No skills. Ctrl+N to create."
        self.query_one("#skills-count", Static).update(text)
        if name and name in table.rows:
            table.move_cursor(row=table.get_row_index(name))
        self.update_controls()

    @work(exclusive=True, group="skills-load", exit_on_error=False)
    async def refresh_skills(self) -> None:
        self.loading = True
        self.update_controls()
        try:
            self.entries = (await self.client.request("skills"))["skills"]
            self.loaded = True
            self.status("")
            self.render_skills()
            if self.app.screen is self and not self.editing:
                self.query_one(DataTable if self.selected else "#skills-filter").focus()
        except Exception as exc:
            self.status(f"Could not load skills: {exc}. Ctrl+L to retry.", problem=True)
            self.query_one("#skills-count", Static).update("Skills unavailable. Ctrl+L to retry.")
        finally:
            self.loading = False
            self.update_controls()

    @on(Input.Changed, "#skills-filter")
    def filter_changed(self) -> None:
        self.state.query = self.query_one("#skills-filter", Input).value
        if self.loaded:
            self.render_skills()

    @on(Input.Submitted, "#skills-filter")
    def filter_submitted(self) -> None:
        if self.selected:
            self.query_one(DataTable).focus()

    @on(DataTable.RowHighlighted, "#skills-table")
    def row_highlighted(self) -> None:
        if self.selected:
            self.state.selected = self.selected["name"]
        self.update_controls()

    @on(DataTable.RowSelected, "#skills-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if not self.committing and not self.loading:
            self.edit_skill(event.row_key.value)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.committing or self.loading:
            return
        if event.button.id == "skills-new":
            self.action_new_skill()
        elif event.button.id == "skills-edit" and self.selected:
            self.edit_skill(self.selected["name"])
        elif event.button.id == "skills-toggle":
            self.action_toggle_skill()
        elif event.button.id == "skills-save":
            self.action_save()
        elif event.button.id == "skills-back":
            self.action_close()

    def open_editor(self, saved: dict, *, create: bool) -> None:
        self.saved = saved
        self.editing_name = None if create else saved["name"]
        values = self.state.drafts.get(self.editing_name or "", saved)
        self.query_one("#skill-name", Input).value = values["name"]
        self.query_one("#skill-name", Input).disabled = not create
        self.query_one("#skill-description", Input).value = values["description"]
        self.query_one(TextArea).load_text(values["instructions"])
        self.query_one(FormCheckbox).value = values["enabled"]
        self.query_one("#skills-pages", ContentSwitcher).current = "skill-editor"
        self.query_one(".form-title", Static).update("New skill" if create else "Edit skill")
        self.status("Unsaved draft restored." if values != saved else "")
        self.clear_errors()
        self.update_controls()
        self.resize_state()
        self.query_one("#skill-name" if create else "#skill-description").focus()

    def action_new_skill(self) -> None:
        self.open_editor({"name": "", "description": "", "instructions": "", "enabled": True},
                         create=True)

    @work(exclusive=True, group="skills-load", exit_on_error=False)
    async def edit_skill(self, name: str) -> None:
        self.loading = True
        self.update_controls()
        try:
            saved = await self.client.request("get_skill", name=name)
            self.open_editor(saved, create=False)
        except Exception as exc:
            self.status(f"Could not read skill: {exc}. Check its SKILL.md and reload.", problem=True)
        finally:
            self.loading = False
            self.update_controls()

    def editor_values(self) -> dict:
        return {"name": self.query_one("#skill-name", Input).value,
                "description": self.query_one("#skill-description", Input).value,
                "instructions": self.query_one(TextArea).text,
                "enabled": self.query_one(FormCheckbox).value}

    def remember_draft(self) -> None:
        if self.editing:
            values = self.editor_values()
            key = self.editing_name or ""
            if any(values[field].strip() for field in ("name", "description", "instructions")):
                self.state.drafts[key] = values

    @work(group="skills-save", exit_on_error=False)
    async def action_save(self) -> None:
        if not self.editing or self.committing:
            return
        self.clear_errors()
        values = self.editor_values()
        try:
            validate_skill_fields(values["name"], values["description"], values["instructions"])
        except SkillError as exc:
            self.field_error("skill-" + exc.field, str(exc))
            return
        previous_focus = self.focused
        self.committing = True
        self.status("Saving skill…")
        self.update_controls()
        try:
            await self.client.request("save_skill", **values, create=self.editing_name is None,
                                      expected_hash=self.saved.get("hash"))
            self.state.drafts.pop(self.editing_name or "", None)
            self.state.selected = values["name"]
            self.show_list()
            self.refresh_skills()
        except Exception as exc:
            self.status(f"Could not save skill: {exc}", problem=True)
        finally:
            self.committing = False
            self.update_controls()
            if self.editing and self.app.screen is self and previous_focus is not None:
                self.call_after_refresh(previous_focus.focus)

    @work(group="skills-save", exit_on_error=False)
    async def action_toggle_skill(self) -> None:
        selected = self.selected
        if self.editing or not selected or self.committing:
            return
        self.committing = True
        self.update_controls()
        try:
            await self.client.request("toggle_skill", name=selected["name"],
                                      enabled=not selected["enabled"])
            selected["enabled"] = not selected["enabled"]
            self.status("")
            self.render_skills()
        except Exception as exc:
            self.status(f"Could not change skill: {exc}", problem=True)
        finally:
            self.committing = False
            self.update_controls()

    def show_list(self) -> None:
        self.query_one("#skills-pages", ContentSwitcher).current = "skills-list"
        self.query_one(".form-title", Static).update("Skills")
        self.status("")
        self.clear_errors()
        self.update_controls()
        self.resize_state()
        self.query_one(DataTable if self.selected else "#skills-filter").focus()

    def action_filter(self) -> None:
        self.query_one("#skills-filter").focus()

    def action_reload(self) -> None:
        if self.editing:
            self.state.drafts.pop(self.editing_name or "", None)
            if self.editing_name:
                self.edit_skill(self.editing_name)
            else:
                self.action_new_skill()
        else:
            self.refresh_skills()

    def action_close(self) -> None:
        if self.committing:
            self.status("Saving skill…")
        elif self.editing:
            self.remember_draft()
            self.show_list()
        elif self.query_one("#skills-filter", Input).value:
            self.query_one("#skills-filter", Input).value = ""
        else:
            self.dismiss(None)
