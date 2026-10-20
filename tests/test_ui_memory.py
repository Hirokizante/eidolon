import asyncio
import copy

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TextArea
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.memory.continuity import THINKING_TIMEOUT_NOTICE
from eidolon.memory.coordinator import CONTEXT_PREFIX, CONTEXT_SUFFIX, CONTINUITY_PREFIX
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.commands import CommandMenu, find_command, matching_commands
from eidolon.ui.memory import CATEGORIES, MemoryScreen, memory_content, reply_details, run_details
from eidolon.ui.memory_notes import NoteDeleteScreen, NoteEditorScreen
from eidolon.ui.preferences import PreferencesScreen
from test_ui import FakeClient


DIAGNOSTIC = "internal-diagnostic-value"
MEMORY_TEXT = "雨 🌸 café [literal]"
NOTE_TEXT = "Finish the synthetic rainy-day sketch 雨 🌸 café"
OLD_NOTE_TEXT = "The synthetic rainy-day sketch still needs a first draft."


def memory_snapshot():
    return {
        "controls": {"use_learned_memories": True, "save_new_memories": False,
                     "pending_delivery_paused": False},
        "readiness": {"verified": False, "retain_enabled": True, "status": DIAGNOSTIC},
        "outbox": {"counts": {"pending": 1, "held": 1}, "pending": 1, "held": 1},
        "manifests": [{"id": 42, "created_at": "2026-10-14T04:00:00+00:00",
                       "session_id": DIAGNOSTIC, "generation_id": DIAGNOSTIC,
                       "attempt_id": DIAGNOSTIC, "manifest": {
                           "status": "ready", "preview": MEMORY_TEXT}}],
        "legacy_pending": [{"document_id": DIAGNOSTIC, "status": "pending",
                            "source_preview": DIAGNOSTIC, "source_truncated": False}],
        "continuity": {"scope": "real_life", "revision": 2, "control_revision": 0,
                       "watermark": 8, "items": [{
                           "id": "thread-1", "revision": 2, "category": "thread", "state": "open",
                           "scope": "real_life", "content_format": "narrative",
                           "topic": "Rainy-day sketch", "importance": "moderate",
                           "content": NOTE_TEXT, "origin": "derived",
                           "sources": [{"exchange_id": 8, "role": "user",
                                        "document_id": DIAGNOSTIC, "excerpt": DIAGNOSTIC,
                                        "timestamp": "2026-10-14T19:00:00+00:00"}],
                           "event_at": "2026-10-16", "derived": True,
                       }], "history": [{"id": 9, "item_id": "thread-1", "revision": 1,
                                          "preview": DIAGNOSTIC}],
                       "maintenance": {
                           "automatic": False, "use_in_replies": False,
                           "running": False, "pending": 1, "error": None,
                           "stages": [{"stage": "continuity", "status": "completed",
                                       "task_id": "synthetic-task", "model_calls": 3, "change_count": 1,
                                       "updated_at": "2026-10-14T04:00:00+00:00",
                                       "input_watermark": 8, "error": DIAGNOSTIC,
                                       "committed_execution": {"model": DIAGNOSTIC,
                                                               "fingerprint": DIAGNOSTIC}}],
                       }},
    }


class MemoryClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.memory = memory_snapshot()
        self.manifest = {**self.memory["manifests"][0], "manifest": {
            "status": "ready", "text": CONTEXT_PREFIX + MEMORY_TEXT + CONTEXT_SUFFIX,
            "entries": [{"id": DIAGNOSTIC, "text": MEMORY_TEXT, "origin": "user",
                         "source": DIAGNOSTIC, "document_id": DIAGNOSTIC,
                         "occurred_start": "2026-10-13", "selection_reason": DIAGNOSTIC,
                         "metadata": {"origin": "user", "diagnostic": DIAGNOSTIC}}],
            "budget": {"used_chars": 1234}, "query": DIAGNOSTIC,
            "control_revision": DIAGNOSTIC,
        }}
        self.memory_error = False
        note = self.memory["continuity"]["items"][0]
        self.run_changes = {"synthetic-task": [{
            "after": copy.deepcopy(note),
            "before": {**copy.deepcopy(note), "content": OLD_NOTE_TEXT}, "created": False,
        }]}
        self.memory_gate = None
        self.manifest_gate = None
        self.run_gate = None
        self.note_gate = None
        self.memory_started = asyncio.Event()

    async def request(self, method, **params):
        if not method.startswith("memory_"):
            return await super().request(method, **params)
        self.calls.append((method, params))
        if method == "memory_state":
            self.memory_started.set()
            if self.memory_gate is not None:
                await self.memory_gate.wait()
        if method == "memory_manifest" and self.manifest_gate is not None:
            await self.manifest_gate.wait()
        if method == "memory_continuity_run" and self.run_gate is not None:
            await self.run_gate.wait()
        if method in {"memory_create_note", "memory_edit_note", "memory_delete_note"}:
            if self.note_gate is not None:
                await self.note_gate.wait()
        if self.memory_error:
            raise RuntimeError(DIAGNOSTIC)
        if method == "memory_manifest":
            return copy.deepcopy(self.manifest)
        if method == "memory_continuity_run":
            maintenance = self.memory["continuity"]["maintenance"]
            stage = next(row for row in maintenance["stages"]
                         if row["task_id"] == params["task_id"])
            latest = maintenance.get("latest_attempt") or {}
            running = (latest.get("task_id") == params["task_id"]
                       and latest.get("outcome") == "running" and maintenance.get("running"))
            return {"status": "running" if running else stage["status"],
                    "updated_at": latest["started_at"] if running else stage["updated_at"],
                    "changes": copy.deepcopy(self.run_changes.get(params["task_id"], []))}
        if method == "memory_refresh":
            self.memory["continuity"]["maintenance"]["running"] = True
            return {**copy.deepcopy(self.memory), "refresh_queued": True}
        note_result = None
        if method == "memory_create_note":
            note_result = {"id": "user-1", "revision": 1, "state": "open", "scope": "real_life",
                           "category": params["category"], "content": params["content"],
                           "topic": params["topic"], "importance": params["importance"],
                           "origin": "user_note", "user_created": True, "locked": True}
            self.memory["continuity"]["items"].append(note_result)
        elif method in {"memory_edit_note", "memory_delete_note"}:
            note_result = next(item for item in self.memory["continuity"]["items"]
                               if item["id"] == params["item_id"])
            if note_result["revision"] != params["expected_revision"]:
                raise RuntimeError("Continuity item revision changed")
            if method == "memory_edit_note":
                note_result.update(content=params["content"], category=params["category"],
                                   topic=params["topic"], importance=params["importance"],
                                   state=params["state"],
                                   origin="user_note", revision=note_result["revision"] + 1)
            else:
                self.memory["continuity"]["items"].remove(note_result)
                note_result = None
        data = copy.deepcopy(self.memory)
        for item in data["continuity"]["items"]:
            item["source_count"] = len(item.pop("sources", []))
            item["detail_available"] = True
        if method in {"memory_create_note", "memory_edit_note", "memory_delete_note"}:
            return {"item": copy.deepcopy(note_result), "snapshot": data}
        return data


def detail_text(screen):
    return str(screen.query_one("#memory-text", Static).render())


def status_text(screen):
    return str(screen.query_one(".form-status", Static).render())


def test_reply_details_show_only_supplied_content_including_later_memory_lookups():
    client = MemoryClient()
    row = copy.deepcopy(client.manifest)
    manifest = row["manifest"]
    manifest["continuity"] = {"text": "Current notes were sent", "entries": [{
        "content": NOTE_TEXT, "sources": [{"excerpt": DIAGNOSTIC}], "id": DIAGNOSTIC,
    }], "revision": 7, "watermark": 99}
    manifest["reflection"] = {"text": "A reflection that was sent.", "selection_reason": DIAGNOSTIC}
    manifest["omitted"] = [{"text": DIAGNOSTIC, "id": DIAGNOSTIC}]
    manifest["backend_evidence"] = {"memories": [{"text": DIAGNOSTIC}]}
    manifest["supplied_rounds"] = [0, 1]
    manifest["tool_contexts"] = [{
        "name": "memory_recall", "status": "completed", "supplied_from_round": 1,
        "text": "A later memory.", "query": DIAGNOSTIC, "evidence": {
            "text": "A later memory.", "entries": [{"text": "A later memory.",
                                                      "id": DIAGNOSTIC}],
        },
    }, {"name": "memory_reflect", "status": "completed", "supplied_from_round": 2,
        "text": "This result was never sent."},
        {"status": "error", "supplied_from_round": 1, "text": DIAGNOSTIC}]
    text = reply_details(row)
    assert MEMORY_TEXT in text and NOTE_TEXT in text
    assert "2026-10-13" in text and "user said" in text
    assert "A reflection that was sent." in text and "A later memory." in text
    assert "This result was never sent." not in text
    assert DIAGNOSTIC not in text
    for label in ("Sources and selection", "retrieval details", "Budget", "Watermark"):
        assert label not in text


def test_text_only_contexts_keep_literal_content_without_prompt_boilerplate():
    text = CONTINUITY_PREFIX + "An older note [literal].\n</continuity_context>\n\n"
    text += CONTEXT_PREFIX + MEMORY_TEXT + CONTEXT_SUFFIX
    shown = memory_content({"text": text})
    assert MEMORY_TEXT in shown and "An older note [literal]." in shown
    assert "<memory_context>" not in shown and "<continuity_context>" not in shown
    assert "not instructions" not in shown and "Labels:" not in shown


@pytest.mark.parametrize("status", ["empty", "disabled", "bypassed"])
def test_empty_reply_context_does_not_dump_diagnostics_or_unsent_entries(status):
    text = reply_details({"manifest": {"status": status, "text": "", "reason": DIAGNOSTIC,
                                       "entries": [{"text": DIAGNOSTIC}]}})
    assert "No memories were supplied" in text
    assert DIAGNOSTIC not in text and status not in text


@pytest.mark.parametrize("state,created,action", [
    ("open", False, "Updated"), ("open", True, "Added"),
    ("resolved", False, "Archived"), ("archived", False, "Archived"),
])
def test_run_details_show_new_version_above_replaced_version(state, created, action):
    row = memory_snapshot()["continuity"]["maintenance"]["stages"][0]
    note = memory_snapshot()["continuity"]["items"][0]
    row["changes"] = [{"after": {**note, "state": state},
                       "before": None if created else {**note, "content": OLD_NOTE_TEXT},
                       "created": created}]
    text = run_details(row, "Asia/Manila")
    assert "Note 1 · " + action in text
    assert text.index("New version") < text.index(NOTE_TEXT) < text.index("Previous version")
    if created:
        assert "No previous version (new note)." in text
    else:
        assert text.index("Previous version") < text.index(OLD_NOTE_TEXT)
    if state != "open":
        assert "Rainy-day sketch · Moderate importance · " + action in text
    assert DIAGNOSTIC not in text


def test_run_details_distinguish_no_changes_from_unloaded_or_missing_history():
    row = {"status": "completed", "updated_at": "2026-10-14T04:00:00+00:00", "change_count": 0}
    assert "No notes were changed" not in run_details(row, "Asia/Manila")
    text = run_details({**row, "changes": []}, "Asia/Manila")
    assert "No notes were changed by this run." in text
    assert "New version" not in text
    text = run_details({**row, "changes": [{"after": {"content": NOTE_TEXT},
                                           "before": None, "created": False}]}, "Asia/Manila")
    assert "The previous version is unavailable." in text
    assert "new note" not in text


@pytest.mark.parametrize("size", [(140, 40), (80, 24), (60, 18), (48, 12)])
async def test_memory_keyboard_detail_and_close_preserve_composer(size):
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent message 雨 🌸 café")
        composer.selection = Selection((0, 1), (0, 7))
        selection = composer.selection
        app.execute_command("/memory")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MemoryScreen)
        dialog = screen.query_one(".form-dialog")
        assert dialog.region.bottom == composer.region.y
        assert dialog.region.y > app.query_one("#conversation").region.y
        table = screen.query_one("#memory-table", DataTable)
        assert table.row_count == 1 and table.region.height >= 1
        assert table.region.bottom <= screen.query_one("#memory-list").content_region.bottom
        assert "Oct 14 12:00" in table.get_row("manifest:42")[0].plain
        assert MEMORY_TEXT in table.get_row("manifest:42")[0].plain
        assert not screen.query_one("#memory-life-controls").display
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "Recalled memories" in detail_text(screen)
        assert MEMORY_TEXT in detail_text(screen) and "2026-10-13" in detail_text(screen)
        assert DIAGNOSTIC not in detail_text(screen)
        assert ("memory_manifest", {"manifest_id": 42}) in client.calls
        assert app.focused is screen.query_one("#memory-detail-scroll")
        await pilot.press("x", "enter")
        assert composer.text == "Unsent message 雨 🌸 café"
        await pilot.press("escape")
        assert app.screen is screen and not screen.detail_key
        await pilot.press("escape")
        assert app.focused is composer and composer.selection == selection
        assert composer.text == "Unsent message 雨 🌸 café"


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_current_life_has_separate_notes_and_run_history_with_direct_refresh(size):
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent current life question 雨")
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        table = screen.query_one(DataTable)
        assert list(screen.rows) == ["continuity:thread-1"]
        assert screen.query_one("#memory-notes").has_class("selected")
        refresh = screen.query_one("#memory-refresh", Button)
        toolbar = screen.query_one("#memory-life-controls")
        assert toolbar.display and refresh.visible and not refresh.disabled
        assert table.region.height >= 1
        assert refresh.region.right <= screen.query_one(".form-dialog").content_region.right
        assert refresh.region.bottom <= screen.query_one(".form-dialog").content_region.bottom
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert NOTE_TEXT in detail_text(screen) and "Oct 16, 2026" in detail_text(screen)
        assert DIAGNOSTIC not in detail_text(screen)
        assert not any(method == "memory_continuity_item" for method, _ in client.calls)
        await pilot.press("escape")
        table.focus()
        await pilot.press("shift+tab", "shift+tab", "shift+tab")
        assert app.focused is screen.query_one("#memory-history")
        await pilot.press("enter")
        await pilot.pause()
        assert list(screen.rows) == ["maintenance:synthetic-task"]
        assert screen.query_one("#memory-history").has_class("selected")
        assert "create" not in str(screen.query_one(".form-hint", Static).render())
        assert "Oct 14 12:00 · Finished" in table.get_row("maintenance:synthetic-task")[0].plain
        assert "1 note updated" in table.get_row("maintenance:synthetic-task")[0].plain
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        if size[1] >= 18:
            assert "1 note updated" in detail_text(screen)
        text = detail_text(screen)
        assert text.index("New version") < text.index(NOTE_TEXT)
        assert text.index(NOTE_TEXT) < text.index("Previous version") < text.index(OLD_NOTE_TEXT)
        assert ("memory_continuity_run", {"task_id": "synthetic-task"}) in client.calls
        assert DIAGNOSTIC not in detail_text(screen) and "calls" not in detail_text(screen)
        assert not any(method == "maintenance_attempts" for method, _ in client.calls)
        await pilot.press("escape")
        refresh.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert ("memory_refresh", {"session_id": app.session_id}) in client.calls
        assert "queued" in status_text(screen)
        assert refresh.disabled and screen.detail_key is None
        assert client.memory["readiness"]["status"] == DIAGNOSTIC
        await pilot.press("escape")
        assert app.focused is composer and composer.text == "Unsent current life question 雨"


async def test_notes_show_archived_and_expired_topics_below_active_notes():
    client = MemoryClient()
    note = client.memory["continuity"]["items"][0]
    client.memory["continuity"]["items"].extend([
        {**note, "id": "resolved", "state": "resolved"},
        {**note, "id": "expired", "expires_at": "2000-01-01T00:00:00+00:00"},
        {**note, "id": "invalidated", "invalidated": True},
    ])
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        assert list(screen.rows) == ["continuity:thread-1", "continuity:resolved", "continuity:expired"]
        assert screen.rows["continuity:thread-1"][0] == "Rainy-day sketch · Moderate"
        assert screen.rows["continuity:resolved"][0].startswith("Archived · Rainy-day sketch")


@pytest.mark.parametrize("size", [(140, 40), (80, 24), (60, 18), (48, 12)])
async def test_notes_create_edit_and_delete_with_keyboard_and_preserve_chat(size, monkeypatch):
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft 雨")
        app.open_memory()
        await pilot.pause()
        memory = app.screen
        memory.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        create = memory.query_one("#memory-create", Button)
        assert create.region.right <= memory.query_one(".form-dialog").content_region.right
        assert memory.query_one(DataTable).region.height >= 1
        await pilot.press("ctrl+n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, NoteEditorScreen)
        assert editor.query_one(".form-dialog").region.bottom == composer.region.y
        assert app.focused is editor.query_one("#note-topic")
        await pilot.press(*"Rainy-day feeling", "tab", "tab", "enter", "down", "down", "down",
                          "enter", "tab")
        await pilot.pause()
        assert editor.query_one("#note-category", Select).value == "emotion"
        text = editor.query_one("#note-content", TextArea)
        assert app.focused is text
        assert text.region.intersection(editor.query_one(".form-body").region).height >= 1
        if size[0] >= 80 and size[1] >= 24:
            label = editor.query_one("#note-category")
            assert label.region.y >= editor.query_one(".form-body").content_region.y
            assert label.region.bottom <= editor.query_one(".form-body").content_region.bottom
        assert editor.query_one("#note-save").region.bottom <= editor.query_one(
            ".form-dialog").content_region.bottom
        text.load_text("A user-created rainy-day note 雨 🌸 café [literal].")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is memory
        assert "User-created" in detail_text(memory)
        assert "Feeling" in detail_text(memory)
        assert "rainy-day note" in detail_text(memory)
        assert memory.query_one("#memory-edit").region.bottom <= memory.query_one(
            ".form-dialog").content_region.bottom
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert isinstance(app.screen, NoteEditorScreen)
        app.screen.query_one("#note-content", TextArea).load_text("An edited user-created note 雨.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "An edited user-created note 雨." in detail_text(memory)
        await pilot.press("escape")
        await pilot.pause()
        table = memory.query_one(DataTable)
        assert not table.show_horizontal_scrollbar
        label = table.get_row("continuity:user-1")[0].plain
        assert label == "Rainy-day feeling · Moderate · User-created"
        assert "User-created" not in table.get_row("continuity:thread-1")[0].plain
        table.move_cursor(row=table.get_row_index("continuity:user-1"))
        table.focus()
        await pilot.press("enter", "ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, NoteDeleteScreen)
        assert app.focused is app.screen.query_one("#note-keep")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is memory
        assert not any(method == "memory_delete_note" for method, _ in client.calls)
        await pilot.press("ctrl+d")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert app.screen is memory and memory.detail_key is None
        assert "continuity:user-1" not in memory.rows
        assert "continuity:thread-1" in memory.rows
        await pilot.press("escape")
        assert app.focused is composer and composer.text == "Unsent draft 雨"


async def test_note_editor_preserves_drafts_on_cancel_and_failure_and_validates_content():
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        memory = app.screen
        memory.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.press("ctrl+n", "ctrl+s")
        await pilot.pause()
        editor = app.screen
        assert "Write a note" in str(editor.query_one("#note-content-error", Static).render())
        assert not any(method == "memory_create_note" for method, _ in client.calls)
        editor.query_one(TextArea).load_text("x" * 4001)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "4,000" in str(editor.query_one("#note-content-error", Static).render())
        editor.query_one(TextArea).load_text("Keep my note draft 雨.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "one topic" in str(editor.query_one("#note-topic-error", Static).render())
        editor.query_one("#note-topic", Input).value = "Rainy-day sketch"
        editor.query_one("#note-category", Select).value = "relationship"
        editor.query_one("#note-importance", Select).value = "high"
        await pilot.resize_terminal(47, 11)
        await pilot.pause()
        assert editor.has_class("too-small")
        assert editor.query_one(".form-too-small").display
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not editor.has_class("too-small")
        assert editor.query_one(TextArea).text == "Keep my note draft 雨."
        await pilot.press("escape", "escape")
        app.open_memory()
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        editor = app.screen
        assert editor.query_one(TextArea).text == "Keep my note draft 雨."
        assert editor.query_one("#note-category", Select).value == "relationship"
        assert editor.query_one("#note-topic", Input).value == "Rainy-day sketch"
        assert editor.query_one("#note-importance", Select).value == "high"
        client.memory_error = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is editor and "draft is kept" in status_text(editor)
        assert DIAGNOSTIC not in status_text(editor)
        client.memory_error = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MemoryScreen)
        assert "new" not in app.memory_state.note_drafts


async def test_note_editor_rejects_concurrent_edits_and_keeps_the_draft():
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        memory = app.screen
        memory.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        memory.query_one(DataTable).focus()
        await pilot.press("enter", "ctrl+e")
        await pilot.pause()
        editor = app.screen
        editor.query_one(TextArea).load_text("My edited rainy-day sketch draft.")
        note = client.memory["continuity"]["items"][0]
        note.update(revision=3, content="A maintenance update to the sketch.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is editor and "This note changed" in status_text(editor)
        assert editor.query_one(TextArea).text == "My edited rainy-day sketch draft."
        assert note["content"] == "A maintenance update to the sketch."
        await pilot.press("escape")
        assert "thread-1" in app.memory_state.note_drafts


async def test_repeated_save_during_creation_commits_once():
    client = MemoryClient()
    client.note_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        app.screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        editor = app.screen
        editor.query_one(TextArea).load_text("One new note.")
        editor.query_one("#note-topic", Input).value = "One new subject"
        await pilot.press("ctrl+s", "ctrl+s", "escape")
        await pilot.pause()
        assert app.screen is editor and editor.committing
        assert sum(method == "memory_create_note" for method, _ in client.calls) == 1
        client.note_gate.set()
        await pilot.pause()
        assert isinstance(app.screen, MemoryScreen)
        assert sum(note.get("user_created", False)
                   for note in client.memory["continuity"]["items"]) == 1


async def test_run_history_orders_recent_activity_and_shows_an_active_refresh():
    client = MemoryClient()
    maintenance = client.memory["continuity"]["maintenance"]
    maintenance.update(running=True, latest_attempt={
        "task_id": "retry-task", "outcome": "running",
        "started_at": "2026-10-15T04:00:00+00:00", "provenance": DIAGNOSTIC,
    })
    maintenance["stages"].append({
        "stage": "continuity", "task_id": "retry-task", "status": "failed",
        "updated_at": "2026-10-13T04:00:00+00:00", "error": DIAGNOSTIC,
    })
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.click("#memory-history")
        await pilot.pause()
        assert list(screen.rows) == ["maintenance:retry-task", "maintenance:synthetic-task"]
        assert "Oct 15 12:00 · Refreshing" in screen.rows["maintenance:retry-task"][0]
        screen.read_selection("maintenance:retry-task")
        await pilot.pause()
        assert "in the background" in detail_text(screen)
        assert DIAGNOSTIC not in detail_text(screen)
        await pilot.press("escape", "escape")
        client.memory_error = True
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        assert screen.state.life_section == "history"
        screen.read_selection("maintenance:retry-task")
        await pilot.pause()
        assert "in the background" in detail_text(screen)


async def test_run_history_no_change_entry_and_cached_versions_survive_an_outage():
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.click("#memory-history")
        await pilot.pause()
        screen.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        assert OLD_NOTE_TEXT in detail_text(screen)
        await pilot.press("escape", "escape")
        client.memory_error = True
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        assert NOTE_TEXT in detail_text(screen) and OLD_NOTE_TEXT in detail_text(screen)
        assert "cached" in status_text(screen) and DIAGNOSTIC not in status_text(screen)
        client.memory_error = False
        client.run_changes["synthetic-task"] = []
        client.memory["continuity"]["maintenance"]["stages"][0].update(
            change_count=0, updated_at="2026-10-15T04:00:00+00:00",
        )
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert "No notes were changed by this run." in detail_text(screen)
        await pilot.press("escape")
        assert "No changes" in screen.rows["maintenance:synthetic-task"][0]


async def test_run_finishing_refreshes_note_versions_without_moving_focus_or_scroll():
    client = MemoryClient()
    changes = client.run_changes["synthetic-task"]
    client.run_changes["synthetic-task"] = []
    maintenance = client.memory["continuity"]["maintenance"]
    stage = maintenance["stages"][0]
    stage.update(status="pending", change_count=0)
    maintenance.update(running=True, latest_attempt={
        "task_id": "synthetic-task", "outcome": "running", "started_at": stage["updated_at"],
    })
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.click("#memory-history")
        await pilot.pause()
        screen.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        assert "No note updates have been saved" in detail_text(screen)
        scroll = screen.query_one("#memory-detail-scroll", VerticalScroll)
        scroll.scroll_to(y=1, animate=False, immediate=True)
        position, focus = scroll.scroll_y, app.focused
        maintenance["running"] = False
        maintenance["latest_attempt"]["outcome"] = "committed"
        stage.update(status="completed", change_count=1, updated_at="2026-10-15T04:00:00+00:00")
        client.run_changes["synthetic-task"] = changes
        calls = len(client.calls)
        screen._poll_maintenance()
        await pilot.pause()
        assert NOTE_TEXT in detail_text(screen) and OLD_NOTE_TEXT in detail_text(screen)
        assert app.focused is focus and scroll.scroll_y == position
        assert [method for method, _ in client.calls[calls:]] == [
            "memory_state", "memory_continuity_run",
        ]


async def test_run_comparison_resize_keeps_versions_and_prioritizes_wording_at_the_floor():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=MemoryClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.click("#memory-history")
        await pilot.pause()
        screen.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        assert detail_text(screen).startswith("Oct 14")
        await pilot.resize_terminal(48, 12)
        await pilot.pause()
        assert detail_text(screen).startswith("New version")
        assert NOTE_TEXT in detail_text(screen) and OLD_NOTE_TEXT in detail_text(screen)
        assert screen.query_one("#memory-detail-scroll").region.height >= 2
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert detail_text(screen).startswith("Oct 14")
        assert OLD_NOTE_TEXT in detail_text(screen)


async def test_failed_run_detail_load_retries_and_late_reads_do_not_replace_another_note():
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        await pilot.click("#memory-history")
        await pilot.pause()
        client.memory_error = True
        screen.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        assert "couldn't be loaded" in detail_text(screen)
        assert DIAGNOSTIC not in detail_text(screen) + status_text(screen)
        client.memory_error = False
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert OLD_NOTE_TEXT in detail_text(screen)
        await pilot.press("escape")
        client.run_gate = asyncio.Event()
        screen.read_selection("maintenance:synthetic-task")
        await pilot.pause()
        await pilot.press("escape")
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.click("#memory-notes")
        await pilot.pause()
        screen.read_selection("continuity:thread-1")
        await pilot.pause()
        client.run_gate.set()
        await pilot.pause()
        assert screen.detail_key == "continuity:thread-1"
        assert NOTE_TEXT in detail_text(screen) and "Previous version" not in detail_text(screen)


async def test_view_limits_only_describe_the_visible_collection():
    client = MemoryClient()
    client.memory["inspection_omitted"] = {"legacy_pending": 20, "continuity_history": 50}
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        assert "inspection-limit" not in screen.rows
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        assert "inspection-limit" not in screen.rows
        client.memory["inspection_omitted"]["continuity_items"] = 10
        await pilot.press("ctrl+l")
        await pilot.pause()
        screen.read_selection("inspection-limit")
        await pilot.pause()
        assert detail_text(screen) == "More entries are saved than this view can show."


@pytest.mark.parametrize("old_category", ["controls", "legacy"])
async def test_removed_categories_and_actions_are_absent_even_with_old_saved_state(old_category):
    assert CATEGORIES == (("Reply context", "replies"), ("Current life", "continuity"))
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    app.memory_state.category = old_category
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#memory-category", Select).value == "replies"
        assert {button.id for button in screen.query(Button)} == {
            "memory-notes", "memory-history", "memory-refresh",
            "memory-create", "memory-edit", "memory-delete",
        }
        assert not any(method in {"memory_delivery", "memory_review_legacy"}
                       for method, _ in client.calls)


def test_memory_discovery_describes_the_content_browser():
    assert find_command("memory").name == "memory"
    assert "current life notes" in find_command("memory").description
    assert matching_commands("memories sent")[0].name == "memory"
    assert "pending delivery" not in find_command("memory").description


async def test_memory_opens_from_palette_and_search_only_matches_visible_content():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=MemoryClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandMenu)
        app.screen.query_one(Input).value = "/memory"
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MemoryScreen)
        screen.read_selection("manifest:42")
        await pilot.pause()
        await pilot.press("escape", "ctrl+f")
        assert app.focused is screen.query_one("#memory-filter")
        screen.query_one("#memory-filter", Input).value = "雨"
        await pilot.pause()
        assert screen.query_one(DataTable).row_count == 1
        screen.query_one("#memory-filter", Input).value = DIAGNOSTIC
        await pilot.pause()
        assert screen.query_one(DataTable).row_count == 0
        assert "No entries match" in str(screen.query_one("#memory-empty", Static).render())
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        assert screen.query_one(DataTable).row_count == 0
        screen.query_one("#memory-filter", Input).value = "rainy-day"
        await pilot.pause()
        assert screen.query_one(DataTable).row_count == 1


async def test_memory_loading_outage_cached_reading_and_retry_remain_responsive():
    client = MemoryClient()
    client.memory_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await client.memory_started.wait()
        screen = app.screen
        assert "Loading" in str(screen.query_one("#memory-empty", Static).render())
        await pilot.press("escape")
        assert app.focused is app.query_one(Composer)
        client.memory_gate.set()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one(DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert MEMORY_TEXT in detail_text(screen)
        await pilot.press("escape", "escape")
        client.memory_error = True
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        assert screen.query_one(".form-status").has_class("problem")
        assert "cached" in status_text(screen) and DIAGNOSTIC not in status_text(screen)
        screen.query_one(DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert MEMORY_TEXT in detail_text(screen)
        client.memory_error = False
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.connected and not screen.query_one(".form-status").has_class("problem")


async def test_failed_detail_load_can_retry_in_place_without_exposing_exception_text():
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        client.memory_error = True
        screen.read_selection("manifest:42")
        await pilot.pause()
        assert "couldn't be loaded" in detail_text(screen)
        assert DIAGNOSTIC not in detail_text(screen) + status_text(screen)
        client.memory_error = False
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert MEMORY_TEXT in detail_text(screen)
        assert screen.detail_key == "manifest:42"


async def test_slow_detail_load_does_not_replace_a_note_after_navigation():
    client = MemoryClient()
    client.manifest_gate = asyncio.Event()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.read_selection("manifest:42")
        await pilot.pause()
        await pilot.press("escape")
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        screen.read_selection("continuity:thread-1")
        await pilot.pause()
        client.manifest_gate.set()
        await pilot.pause()
        assert NOTE_TEXT in detail_text(screen) and "Recalled memories" not in detail_text(screen)


async def test_memory_resize_monochrome_ascii_and_empty_sections(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = MemoryClient()
    client.memory["manifests"] = []
    client.memory["continuity"]["items"] = []
    client.memory["continuity"]["maintenance"]["stages"] = []
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        assert screen.has_class("monochrome") and screen.has_class("ascii")
        assert "Send a message first" in str(screen.query_one("#memory-empty", Static).render())
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        assert "No current life notes" in str(screen.query_one("#memory-empty", Static).render())
        await pilot.click("#memory-history")
        await pilot.pause()
        assert "No refresh runs" in str(screen.query_one("#memory-empty", Static).render())
        screen.query_one("#memory-filter", Input).value = "kept query 雨"
        for size in [(60, 18), (48, 12), (47, 11), (80, 24)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert screen.query_one(".form-too-small").display == (size[0] < 48)
            assert screen.query_one(".form-dialog").display == (size[0] >= 48)
        assert screen.query_one("#memory-filter", Input).value == "kept query 雨"
        assert screen.state.life_section == "history"


async def test_memory_preferences_keep_recall_and_saving_independent():
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PreferencesScreen)
        screen.query_one("#preference-category", Select).value = "memory"
        await pilot.pause()
        recall = screen.query_one("#pref-memory_enabled", Checkbox)
        saving = screen.query_one("#pref-memory_save_enabled", Checkbox)
        assert str(recall.label) == "Use learned memories"
        assert str(saving.label) == "Save new memories"
        recall.focus()
        await pilot.press("space", "ctrl+s")
        await pilot.pause()
        assert not app.config.memory.enabled and app.config.memory.save_enabled
        assert ("save_preferences", {"preferences": {"memory_enabled": False}}) in client.calls


async def test_current_life_preferences_remain_independent_on_small_terminal():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=MemoryClient())
    async with app.run_test(size=(48, 12)) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#preference-category", Select).value = "continuity"
        await pilot.pause()
        automatic = screen.query_one("#pref-continuity_enabled", Checkbox)
        use = screen.query_one("#pref-continuity_use_enabled", Checkbox)
        assert not automatic.value and not use.value
        maximum = screen.query_one("#pref-continuity_max_notes", Input)
        assert maximum.value == "12"
        maximum.value = "7"
        automatic.focus()
        await pilot.press("space", "ctrl+s")
        await pilot.pause()
        assert app.config.memory.continuity_enabled
        assert not app.config.memory.continuity_use_enabled
        assert app.config.memory.enabled and app.config.memory.save_enabled
        assert app.config.memory.continuity_max_notes == 7


@pytest.mark.parametrize("size", [(60, 18), (48, 12)])
async def test_current_life_fields_remain_identifiable_and_archiving_is_reachable(size):
    client = MemoryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        preferences = app.screen
        preferences.query_one("#preference-category", Select).value = "continuity"
        maximum = preferences.query_one("#pref-continuity_max_notes", Input)
        maximum.focus()
        await pilot.pause()
        assert "Maximum active notes" in preferences.query_one(".form-dialog").border_title
        assert maximum.region.overlaps(preferences.query_one("#preference-continuity").region)
        await pilot.press("escape")
        app.open_memory()
        await pilot.pause()
        memory = app.screen
        memory.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        memory.read_selection("continuity:thread-1")
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        editor = app.screen
        for selector, label in (("note-importance", "Importance"), ("note-state", "Status"),
                                ("note-content", "Note")):
            control = editor.query_one("#" + selector)
            control.focus()
            await pilot.pause()
            assert label in editor.query_one(".form-dialog").border_title
            assert control.region.overlaps(editor.query_one(".form-body").region)
        editor.query_one("#note-state", Select).value = "archived"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is memory and "Archived" in detail_text(memory)
        await pilot.press("escape")
        await pilot.pause()
        assert memory.query_one(DataTable).get_row("continuity:thread-1")[0].plain.startswith("Archived")


async def test_memory_first_load_error_can_retry_without_losing_the_overlay():
    client = MemoryClient()
    client.memory_error = True
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        assert "unavailable" in str(screen.query_one("#memory-empty", Static).render())
        assert DIAGNOSTIC not in status_text(screen)
        client.memory_error = False
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.screen is screen and screen.query_one(DataTable).row_count == 1


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_running_refresh_polls_without_dispatching_work_or_stealing_focus(size):
    client = MemoryClient()
    client.memory["continuity"]["maintenance"].update(running=True, error=None)
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        assert screen.query_one("#memory-refresh", Button).disabled
        calls = len(client.calls)
        screen.loading = True
        screen._poll_maintenance()
        await pilot.pause()
        assert len(client.calls) == calls
        screen.loading = False
        screen.query_one("#memory-table").focus()
        await pilot.pause()
        focus = app.focused
        client.memory["continuity"]["maintenance"]["running"] = False
        screen._poll_maintenance()
        await pilot.pause()
        assert not screen.state.snapshot["continuity"]["maintenance"]["running"]
        assert not screen.query_one("#memory-refresh", Button).disabled
        assert app.focused is focus
        assert [method for method, _ in client.calls[calls:]] == ["memory_state"]
        assert "finished" in status_text(screen)
        calls = len(client.calls)
        screen._poll_maintenance()
        await pilot.pause()
        assert len(client.calls) == calls


@pytest.mark.parametrize("size", [(80, 24), (48, 12)])
async def test_current_life_errors_are_actionable_without_raw_diagnostics(size):
    client = MemoryClient()
    client.memory["continuity"]["maintenance"]["error"] = DIAGNOSTIC
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.open_memory()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#memory-category", Select).value = "continuity"
        await pilot.pause()
        assert "retry" in status_text(screen) and DIAGNOSTIC not in status_text(screen)
        assert screen.query_one(DataTable).region.height >= 1
        assert not screen.query_one("#memory-refresh", Button).disabled
        client.memory["continuity"]["maintenance"]["error"] = THINKING_TIMEOUT_NOTICE
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert "thinking" in status_text(screen) and "/preferences" in status_text(screen)
        if size == (48, 12):
            assert "Check /preferences" in screen.query_one(".form-dialog").border_title
        client.memory["continuity"]["maintenance"]["auxiliary"] = {
            "mode": "disabled", "notice": DIAGNOSTIC,
        }
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert "/preferences" in status_text(screen) and DIAGNOSTIC not in status_text(screen)


async def test_memory_dismissal_preserves_the_conversation_reading_position():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=MemoryClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one("#conversation", VerticalScroll)
        await conversation.mount(Static("Earlier conversation\n" * 80, markup=False))
        await pilot.pause()
        conversation.scroll_to(y=10, animate=False, immediate=True)
        position = conversation.scroll_y
        app.open_memory()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert conversation.scroll_y == position
