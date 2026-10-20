"""Create, edit, and remove current-life notes in anchored forms."""

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Select, Static, TextArea

from eidolon.memory.continuity import TOPIC_MAX_CHARS, valid_topic, with_note_metadata
from eidolon.ui.forms import FormBody, FormModal


NOTE_CATEGORIES = {
    "situation": "Situation", "thread": "Open topic", "relationship": "Relationship",
    "emotion": "Feeling",
}
NOTE_IMPORTANCE = {"low": "Low", "moderate": "Moderate", "high": "High"}


def note_error(error: Exception, action: str) -> str:
    """Translate known service failures without exposing arbitrary diagnostics."""
    message = str(error)
    if "Continuity item revision changed" in message:
        return "This note changed. Return to Notes, refresh, and reopen it."
    if "Continuity item does not exist:" in message:
        return "This note was removed. Return to Notes and refresh."
    if "Current life is full" in message:
        return "Current life is full. Archive an active note or raise the limit in /preferences."
    if "already covers this topic" in message:
        return "An active note already covers this topic. Edit that note or choose another topic."
    return f"Couldn't {action} the note. Try again or return to Notes and refresh."


class NoteEditorScreen(FormModal):
    DEFAULT_CSS = """
    NoteEditorScreen #note-content {
        height: 1fr; min-height: 2; max-height: 6;
        border: none; padding: 0 1; background: $background;
    }
    NoteEditorScreen #note-content:focus { background: $panel; }
    NoteEditorScreen Select { height: 1; }
    NoteEditorScreen SelectCurrent { height: 1; border: none; padding: 0 1; }
    NoteEditorScreen.compact .field-label { margin-top: 0; }
    NoteEditorScreen.short .form-hint { display: none; }
    """

    def __init__(self, client, drafts: dict[str, dict], *, note: dict | None = None,
                 session_id: str | None = None) -> None:
        super().__init__()
        self.client, self.drafts, self.note, self.session_id = client, drafts, note, session_id
        self.draft_key = str(note["id"]) if note else "new"
        self.values = {"category": "situation", "content": "", "topic": "",
                       "importance": "moderate", "state": "open",
                       **(with_note_metadata(note) if note else {}),
                       **drafts.get(self.draft_key, {})}
        self.committing = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Edit note" if self.note else "Create note", classes="form-title",
                         markup=False)
            yield Static(
                "Maintenance can update, archive, or resolve this edited note."
                if self.note and not self.note.get("user_created") else
                "User-created notes stay unchanged during maintenance.",
                classes="form-subtitle", markup=False,
            )
            with FormBody(classes="form-body"):
                yield Static("Topic", classes="field-label", markup=False)
                yield Input(self.values["topic"], id="note-topic")
                yield Static("", id="note-topic-error", classes="field-error", markup=False)
                yield Static("Importance to your companion and relationship", classes="field-label",
                             markup=False)
                yield Select([(label, value) for value, label in NOTE_IMPORTANCE.items()],
                             value=self.values["importance"], allow_blank=False, compact=True,
                             id="note-importance")
                yield Static("Label", classes="field-label", markup=False)
                yield Select([(label, value) for value, label in NOTE_CATEGORIES.items()],
                             value=self.values["category"], allow_blank=False, compact=True,
                             id="note-category")
                if self.note:
                    yield Static("Status", classes="field-label", markup=False)
                    yield Select([("Active", "open"), ("Archived", "archived")],
                                 value="open" if self.values["state"] == "open" else "archived",
                                 allow_blank=False, compact=True, id="note-state")
                yield Static("Note", classes="field-label", markup=False)
                yield TextArea(self.values["content"], soft_wrap=True, tab_behavior="focus",
                               id="note-content")
                yield Static("", id="note-content-error", classes="field-error", markup=False)
            yield Static("", classes="form-status", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("Save", id="note-save", flat=True, compact=True)
                yield Button("Back (keep draft)", id="note-back", flat=True, compact=True)
            yield Static("Ctrl+S save | Esc back, keeping draft", classes="form-hint", markup=False)
        yield Static("Terminal too small — need 48×12. Resize or press Esc to go back.",
                     classes="form-too-small", markup=False)

    def on_mount(self) -> None:
        self.resize_state()
        self.query_one("#note-topic").focus()

    def responsive_copy(self, width: int) -> None:
        self.update_focus_title()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        super().on_descendant_focus(event)
        self.update_focus_title()

    def update_focus_title(self) -> None:
        if not (self.has_class("short") or self.has_class("narrow")):
            return
        label = {"note-topic": "Topic", "note-importance": "Importance",
                 "note-category": "Label", "note-state": "Status",
                 "note-content": "Note"}.get(getattr(self.focused, "id", None))
        title = "Edit note" if self.note else "Create note"
        self.query_one(".form-dialog").border_title = title + (" · " + label if label else "")

    def remember_draft(self) -> None:
        self.drafts[self.draft_key] = {
            "category": self.query_one("#note-category", Select).value,
            "topic": self.query_one("#note-topic", Input).value,
            "importance": self.query_one("#note-importance", Select).value,
            "content": self.query_one("#note-content", TextArea).text,
        }
        if self.note:
            self.drafts[self.draft_key]["state"] = self.query_one("#note-state", Select).value

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "note-save":
            self.action_save()
        elif event.button.id == "note-back":
            self.action_close()

    @work(group="note-save", exit_on_error=False)
    async def action_save(self) -> None:
        if self.committing:
            return
        self.clear_errors()
        self.remember_draft()
        values = self.drafts[self.draft_key]
        if not values["content"].strip():
            self.field_error("note-content", "Write a note before saving.")
            return
        if len(values["content"]) > 4000:
            self.field_error("note-content", "Keep the note within 4,000 characters.")
            return
        if not valid_topic(values["topic"]):
            self.field_error("note-topic", f"Give this note one topic, within {TOPIC_MAX_CHARS} characters.")
            return
        self.committing = True
        self.query_one("#note-save", Button).disabled = True
        self.status("Saving note…")
        try:
            params = {**values, "session_id": self.session_id}
            if self.note:
                params.update(item_id=self.note["id"], expected_revision=self.note["revision"])
            result = await self.client.request(
                "memory_edit_note" if self.note else "memory_create_note", **params,
            )
            self.drafts.pop(self.draft_key, None)
            self._committed = True
            self.dismiss(result)
        except Exception as error:
            self.status(note_error(error, "save") + " Your draft is kept.", problem=True)
        finally:
            self.committing = False
            self.query_one("#note-save", Button).disabled = False


class NoteDeleteScreen(FormModal):
    def __init__(self, client, note: dict, session_id: str | None = None) -> None:
        super().__init__()
        self.client, self.note, self.session_id = client, note, session_id
        self.committing = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Delete note?", classes="form-title", markup=False)
            with FormBody(classes="form-body"):
                yield Static("Remove this note from Current life?\n\n" + self.note["content"],
                             markup=False)
            yield Static("", classes="form-status", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("Keep note", id="note-keep", flat=True, compact=True)
                yield Button("Delete note", id="note-delete-confirm", flat=True, compact=True)
            yield Static("Tab choose | Enter confirm | Esc keep note", classes="form-hint",
                         markup=False)
        yield Static("Terminal too small — need 48×12. Resize or press Esc to keep the note.",
                     classes="form-too-small", markup=False)

    def on_mount(self) -> None:
        self.resize_state()
        self.query_one("#note-keep").focus()

    def remember_draft(self) -> None:
        pass

    def check_action(self, action, parameters):
        return action != "save"

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "note-delete-confirm":
            self.delete_note()
        elif event.button.id == "note-keep":
            self.action_close()

    @work(group="note-delete", exit_on_error=False)
    async def delete_note(self) -> None:
        if self.committing:
            return
        self.committing = True
        self.query_one("#note-delete-confirm", Button).disabled = True
        self.status("Deleting note…")
        try:
            result = await self.client.request(
                "memory_delete_note", item_id=self.note["id"],
                expected_revision=self.note["revision"], session_id=self.session_id,
            )
            self._committed = True
            self.dismiss(result)
        except Exception as error:
            self.status(note_error(error, "delete"), problem=True)
        finally:
            self.committing = False
            self.query_one("#note-delete-confirm", Button).disabled = False
