from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from rich.spinner import Spinner
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, RenderResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import (
    ContentSwitcher,
    Footer,
    Markdown,
    OptionList,
    Select,
    Static,
    TextArea,
)

from eidolon.config import AppConfig, IdentityConfig
from eidolon.clipboard import read_clipboard_image, read_clipboard_text
from eidolon.service.client import RoomClient
from eidolon.themes import PALETTES, resolve_theme, validate_theme
from eidolon.ui.activity import ActivityLog, ActivityScreen, activity_summary, readable_value
from eidolon.ui.commands import (
    COMMANDS,
    CommandMenu,
    HelpScreen,
    command_option,
    find_command,
    matching_commands,
)
from eidolon.ui.history import HistoryScreen
from eidolon.ui.identity import companion_copy, speaker_title
from eidolon.ui.jobs import JobsScreen, JobsState
from eidolon.ui.memory import MemoryScreen, MemoryState
from eidolon.ui.requests import RequestInspector
from eidolon.ui.models import ConnectScreen, ModelScreen, ProviderMenuState
from eidolon.ui.preferences import PreferencesScreen, PreferencesState
from eidolon.ui.skills import SkillsScreen, SkillsState
from eidolon.ui.status import status_line
from eidolon.ui.themes import ThemeDraft, ThemeScreen


PROMPTS = ("SYSTEM", "SOUL", "USER", "POST_HISTORY")
PAGES = {
    "room": "Room",
    "prompts": "Prompts",
    "connections": "Connections",
}


def display_error(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
    if isinstance(exc, ValidationError):
        return "\n".join(
            f"{'.'.join(map(str, e['loc'])) or 'Settings'}: {e['msg']}"
            for e in exc.errors(include_url=False, include_input=False)
        )
    return str(exc) or type(exc).__name__


def message_timestamp(created_at: str | datetime, timezone_name: str) -> str:
    """Use a stable local date and minute, including when reading an older room."""
    try:
        stamp = datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
        if stamp.tzinfo is not None:
            return stamp.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    return ""


class Composer(TextArea):
    class Submit(Message):
        pass

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True, id="composer_send"),
        Binding("shift+enter", "line_break", show=False, priority=True, id="composer_newline"),
        Binding("up", "command_move(-1)", show=False),
        Binding("down", "command_move(1)", show=False),
        Binding("tab", "complete_command", show=False),
        Binding("super+v", "paste_image", show=False, priority=True),
    ]

    def _on_paste(self, event: events.Paste) -> None:
        if not event.text:
            # Terminals may forward an empty bracketed paste for an image.
            event.prevent_default()
            self.app.paste_clipboard()
        event.stop()

    def action_paste_image(self) -> None:
        self.app.paste_clipboard()

    def check_action(self, action, parameters):
        if action in {"command_move", "complete_command"}:
            return self.app.slash_visible
        return True

    def action_submit(self) -> None:
        self.post_message(self.Submit())

    def action_line_break(self) -> None:
        self.insert("\n")

    def action_command_move(self, direction: int):
        options = self.app.query_one("#slash-suggestions", OptionList)
        (options.action_cursor_down if direction > 0 else options.action_cursor_up)()

    def action_complete_command(self):
        self.app.complete_slash()


class ThinkingSpinner(Static):
    """A compact spinner that only refreshes during an active turn."""

    def __init__(self, *, ascii_mode: bool = False) -> None:
        super().__init__(id="thinking-spinner")
        self.spinner = Spinner("line" if ascii_mode else "dots")

    def set_active(self, active: bool) -> None:
        self.display = active
        interval = 0.1 if active and self.app.animation_level != "none" else None
        if self.auto_refresh != interval:
            self.auto_refresh = interval

    def render(self) -> RenderResult:
        if self.app.animation_level == "none":
            return "*"
        return self.spinner


class MessageFrame(Vertical):
    """Retain captured attribution; only legacy frames follow current names."""

    def __init__(self, *children: Widget, role: str, identity: IdentityConfig | None) -> None:
        super().__init__(*children, classes="message-frame")
        self.role = role
        self.identity = identity.model_copy(deep=True) if identity is not None else None
        self.set_class(role == "assistant", "assistant-message")

    def refresh_title(self, fallback: IdentityConfig) -> None:
        self.border_title = speaker_title(self.role, self.identity or fallback)


class RoomApp(App):
    TITLE = "Eidolon"
    AUTO_FOCUS = "#composer"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+enter", "send", show=False, priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("ctrl+s", "save_prompt", "Save", priority=True),
        Binding("ctrl+z", "suspend_process", show=False),
        Binding("slash", "commands", show=False),
        *[
            Binding(
                c.key,
                f"command('{c.name}')",
                c.name.title(),
                show=c.name in {"cancel", "quit"},
                priority=True,
            )
            for c in COMMANDS
            if c.key
        ],
    ]

    def __init__(self, config: AppConfig, *, client=None):
        super().__init__()
        self.config = config
        self.client = client or RoomClient(config.socket_path)
        self.session_id: str | None = None
        self.history_id: str | None = None
        self.resuming_past = False
        self.busy = False
        self.awaiting_recall = False
        self.pending_text: str | None = None
        self.draft_images: list[dict] = []
        self.pending_images: list[dict] = []
        self.pasting_image = False
        self.clipboard_revision = 0
        self.reply_widget: Markdown | None = None
        self.reply_text = ""
        self.turn_identity: IdentityConfig | None = None
        self.turn_progress: str | None = None
        self.loaded_session: str | None = None
        self.room_title = "Today"
        self.status: dict = {}
        self.overview: dict = {}
        self.activity = ActivityLog()
        self.slash_visible = False
        self.saved_editors: dict[str, str] = {}
        self.provider_menu_state = ProviderMenuState()
        self.preferences_state = PreferencesState()
        self.memory_state = MemoryState()
        self.skills_state = SkillsState()
        self.jobs_state = JobsState()
        self.theme_draft: ThemeDraft | None = None
        self.ascii = bool(os.environ.get("EIDOLON_ASCII"))
        self.register_room_themes()
        self.apply_theme(resolve_theme(config.theme.name), config.theme.colors)

    def register_room_themes(self) -> None:
        for name, palette in PALETTES.items():
            self.register_theme(Theme(name=name, **palette))

    def apply_theme(self, name: str, colors: dict[str, str]) -> None:
        validate_theme(name, colors)
        if colors:
            self.register_theme(Theme(name="eidolon-custom", **{**PALETTES[name], **colors}))
            self.theme = "eidolon-custom"
        else:
            self.theme = name
        if self.is_running:
            self.call_after_refresh(self.render_status)

    def apply_custom_theme(self, colors: dict[str, str]) -> None:
        self.apply_theme(resolve_theme(self.config.theme.name), colors)

    def companion_copy(self, prefix: str, suffix: str, *, active: bool = False) -> str:
        return companion_copy(
            (self.turn_identity if active else None) or self.config.identity, prefix, suffix,
            width=self.size.width - (8 if self.size.width >= 80 else 6), ascii_mode=self.ascii,
        )

    def welcome_text(self) -> Text:
        return Text.assemble(
            (" /\\_/\\\n( o.o )  ", "bold"),
            ("A little place to be here.\n", "bold"),
            self.companion_copy("         Say something to ", ".") + "\n\n",
            ("/history", "bold"),
            " past rooms    ",
            ("/skills", "bold"),
            " learn together\n",
            ("/help", "bold"),
            " all commands    Ctrl+P opens the menu",
        )

    def empty_conversation(self) -> Static:
        return Static(self.welcome_text(), classes="empty-chat")

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("Eidolon", id="brand", markup=False)
            yield Static("Room", id="location", markup=False)
        with Vertical(id="workspace"):
            with ContentSwitcher(initial="room", id="pages"):
                with Vertical(id="room", classes="page"):
                    with VerticalScroll(id="conversation"):
                        yield self.empty_conversation()
                    with Horizontal(id="turn-status"):
                        yield ThinkingSpinner(ascii_mode=self.ascii)
                        yield Static("Connecting to the room…", id="notice", markup=False)
                    yield OptionList(id="slash-suggestions", compact=True)
                    yield Static("", id="attachments", markup=False)
                    yield Composer(
                        placeholder=Content(self.companion_copy(
                            "Say something to ", "…  / for commands"
                        )),
                        id="composer",
                    )
                with Vertical(id="prompts", classes="page"):
                    yield Static("The words behind the room", classes="page-title")
                    yield Select(
                        [(name, name) for name in PROMPTS],
                        value="SYSTEM",
                        allow_blank=False,
                        id="prompt-select",
                    )
                    with ContentSwitcher(initial="prompt-SYSTEM", id="prompt-pages"):
                        for name in PROMPTS:
                            with Vertical(id=f"prompt-{name}"):
                                yield Static(
                                    self.config.prompts.get(name, "Path not configured"),
                                    classes="hint",
                                    markup=False,
                                )
                                yield TextArea(id=f"edit-{name}", classes="editor", soft_wrap=True)
                with Vertical(id="connections", classes="page"):
                    with VerticalScroll():
                        yield Markdown("Checking connections…", id="connection-details")
            yield Static("Connecting…", id="service-status", markup=False)
        yield Static(
            "Terminal too small\nResize to at least 48 × 12.\nCtrl+Q closes the room.",
            id="too-small",
            markup=False,
        )
        yield Footer()

    @property
    def history_screen(self) -> HistoryScreen | None:
        return next((s for s in self.screen_stack if isinstance(s, HistoryScreen)), None)

    @property
    def activity_screen(self) -> ActivityScreen | None:
        return next((s for s in self.screen_stack if isinstance(s, ActivityScreen)), None)

    @property
    def current_page(self) -> str:
        return self.query_one("#pages", ContentSwitcher).current or "room"

    @property
    def active_editor(self) -> str | None:
        if self.current_page == "prompts":
            active = self.query_one("#prompt-pages", ContentSwitcher).current
            return "edit-" + active.removeprefix("prompt-")
        return None

    def check_action(self, action, parameters):
        if isinstance(self.screen, ModalScreen):
            if action in {"send", "filter", "back", "save_prompt"}:
                return False
        if action == "save_prompt":
            return self.active_editor is not None
        if isinstance(self.screen, (
            ConnectScreen, ModelScreen, PreferencesScreen, ThemeScreen, SkillsScreen, JobsScreen,
        )):
            if self.screen.committing:
                if action == "commands":
                    return False
                if action == "command" and parameters and parameters[0] not in {
                    "quit", "cancel", "suspend",
                }:
                    return False
            if action == "command" and parameters and parameters[0] in {
                "reload", "inspect",
            }:
                return False
        if action == "command" and parameters:
            name = parameters[0]
            if name == "reload":
                return self.active_editor is not None or not self.status
            if name == "cancel":
                return self.busy
        if action == "filter":
            return False
        if action == "back":
            return self.current_page != "room" or self.slash_visible
        return True

    async def on_mount(self):
        self.apply_interface_preferences()
        self.screen.set_class(self.no_color, "monochrome")
        self.screen.set_class(self.ascii, "ascii")
        self.resize_layout(self.size.width, self.size.height)
        self.set_interval(15, self.refresh_status)
        self.app_resume_signal.subscribe(self, self.refresh_after_resume)
        self.theme_changed_signal.subscribe(self, lambda _: self.render_status())
        self.refresh_data()
        self.refresh_status()
        self.query_one("#composer", Composer).focus()

    def resize_layout(self, width, height):
        base = self.screen_stack[0]
        was_too_small = base.has_class("too-small")
        was_at_end = self.query_one("#conversation", VerticalScroll).is_vertical_scroll_end
        base.set_class(width < 80, "narrow")
        base.set_class(height < 24, "short")
        base.set_class(width < 48 or height < 12, "too-small")
        if self.is_mounted:
            self.query_one(Footer).compact = width < 60
            self.call_after_refresh(self.refresh_identity_copy)
            self.render_status()
            if self.overview:
                self.render_tables(width)
            self.call_after_refresh(self.size_composer)
            if (
                was_too_small and not base.has_class("too-small")
                and self.current_page == "room" and not isinstance(self.screen, ModalScreen)
                and self.focused is None
            ):
                self.call_after_refresh(self.query_one("#composer", Composer).focus)
            if was_at_end:
                self.call_after_refresh(
                    self.query_one("#conversation", VerticalScroll).scroll_end, animate=False
                )

    def on_resize(self, event: events.Resize):
        if self.is_mounted:
            self.resize_layout(event.size.width, event.size.height)

    @work(exclusive=True, group="resume", exit_on_error=False)
    async def refresh_after_resume(self):
        self.refresh_data()
        self.refresh_status()
        if not self.busy and self.pending_text is None and self.session_id:
            try:
                await self.load_conversation(self.session_id)
            except Exception as exc:
                self.set_notice(display_error(exc), error=True)

    def render_header(self):
        label = PAGES[self.current_page]
        context = (
            ("Past room · " if self.resuming_past else "") + self.room_title
            if self.current_page == "room"
            else label
        )
        self.query_one("#location", Static).update(Text(context, overflow="ellipsis", no_wrap=True))

    def render_status(self):
        show_details = (self.config.preferences.show_service_status
                        and bool(self.config.preferences.status_components))
        self.query_one("#service-status", Static).display = show_details or bool(self.status.get("agent_mode"))
        if not self.status:
            return
        editor = self.active_editor
        unsaved = bool(
            editor
            and editor in self.saved_editors
            and self.query_one(f"#{editor}", TextArea).text != self.saved_editors[editor]
        )
        self.query_one("#service-status", Static).update(
            status_line(self.config.selected_provider(),
                        self.config.preferences.status_components if show_details else [],
                        self.status, width=self.size.width - (2 if self.size.width < 80 else 4),
                        ascii=self.ascii, unsaved=unsaved and show_details,
                        colors={name: getattr(self.current_theme, name)
                                for name in ("success", "warning", "error")},
                        no_color=self.no_color)
        )
        self.render_header()

    def set_notice(self, text: str, *, error: bool = False):
        self.turn_progress = None
        widget = self.query_one("#notice", Static)
        widget.update(text)
        widget.display = bool(text)
        widget.set_class(error, "problem")
        self.query_one("#turn-status").display = bool(text)
        self.query_one(ThinkingSpinner).set_active(self.busy and bool(text) and not error)

    def set_turn_progress(self, phase: str) -> None:
        self.set_notice(self.companion_copy(
            "", f" is {phase}… · Ctrl+C cancels", active=True
        ))
        self.turn_progress = phase

    def action_commands(self):
        if isinstance(self.screen, CommandMenu):
            self.screen.dismiss(None)
            return
        if isinstance(self.screen, (HelpScreen, RequestInspector)):
            self.pop_screen()
        if isinstance(self.screen, (ThemeScreen, SkillsScreen, JobsScreen)):
            self.screen.remember_draft()
            self.pop_screen()
        self.push_screen(
            CommandMenu(), lambda command: self.execute_command(command) if command else None
        )

    def action_command(self, name: str):
        if isinstance(self.screen, JobsScreen) and name == "retry":
            self.screen.action_retry()
            return
        if isinstance(self.screen, ModalScreen) and name not in {"quit", "cancel", "suspend"}:
            if not (isinstance(self.screen, (HistoryScreen, ActivityScreen)) and name == "inspect") and (
                name != "inspect" or not isinstance(self.screen, RequestInspector)
            ):
                if isinstance(self.screen, JobsScreen):
                    self.screen.remember_draft()
                self.pop_screen()
        self.execute_command("/" + name)

    def action_open_tab(self, pane: str):
        """Keep page changes and focus predictable, including palette navigation."""
        if pane == "jobs":
            self.action_open_tab("room")
            self.push_screen(JobsScreen(self.client, self.jobs_state, self.config))
            return
        if pane == "history":
            if self.history_screen is None:
                self.action_open_tab("room")
                self.push_screen(HistoryScreen())
            return
        if pane == "activity":
            if self.activity_screen is None:
                self.action_open_tab("room")
                self.push_screen(ActivityScreen())
            return
        if self.history_screen is not None or self.activity_screen is not None:
            while len(self.screen_stack) > 1:
                self.pop_screen()
        if pane == "themes":
            self.open_themes()
            return
        self.query_one("#pages", ContentSwitcher).current = pane
        targets = {
            "room": "composer",
            "prompts": "prompt-select",
            "connections": "connection-details",
        }
        target = self.query_one(f"#{targets[pane]}")
        target.focus()
        self.render_header()
        self.render_status()
        self.refresh_bindings()

    def action_back(self):
        if self.slash_visible:
            self.hide_slash()
        else:
            self.action_open_tab("room")
        self.refresh_bindings()

    def action_inspect_requests(self):
        if isinstance(self.screen, RequestInspector):
            self.screen.refresh_requests()
            return
        self.push_screen(RequestInspector(self.client, self.config.timezone))

    def open_model_settings(self, connect: bool = False) -> None:
        # refresh_data and save callbacks keep this snapshot current. Opening a
        # menu must not wait for service I/O; model discovery runs in its worker.
        if connect:
            self.push_screen(
                ConnectScreen(self.client, self.config, state=self.provider_menu_state),
                self.provider_connected,
            )
        else:
            self.push_screen(
                ModelScreen(self.client, self.config, state=self.provider_menu_state),
                self.model_settings_saved,
            )

    def model_settings_saved(self, result):
        if result:
            self.config = AppConfig.model_validate(result["config"])
            if result.get("warning"):
                self.set_notice(result["warning"], error=True)
            self.apply_interface_preferences()
        self.refresh_status()
        self.refresh_data()

    def open_themes(self) -> None:
        if isinstance(self.screen, ThemeScreen):
            return
        self.push_screen(
            ThemeScreen(self.client, self.config.theme, self.theme_draft), self.theme_saved,
        )

    def theme_saved(self, result: dict | None) -> None:
        if result:
            self.config = AppConfig.model_validate(result)
            self.apply_theme(resolve_theme(self.config.theme.name), self.config.theme.colors)
            self.apply_interface_preferences()
            self.refresh_status()

    def open_preferences(self) -> None:
        if isinstance(self.screen, PreferencesScreen):
            return
        self.push_screen(
            PreferencesScreen(self.client, self.config, self.preferences_state),
            self.preferences_saved,
        )

    def open_memory(self) -> None:
        if not isinstance(self.screen, MemoryScreen):
            self.action_open_tab("room")
            self.push_screen(MemoryScreen(self.client, self.memory_state, self.session_id))

    def preferences_saved(self, result: dict | None) -> None:
        if result:
            self.config = AppConfig.model_validate(result)
            self.apply_interface_preferences()
            self.render_tables()
            self.refresh_status()
            self.refresh_data()

    def apply_interface_preferences(self) -> None:
        self.set_keymap({
            "composer_send": self.config.bindings.get("send", "enter"),
            "composer_newline": self.config.bindings.get("newline", "shift+enter"),
        })
        self.refresh_identity_copy()
        self.render_status()

    def refresh_identity_copy(self) -> None:
        self.query_one("#composer", Composer).placeholder = Content(self.companion_copy(
            "Say something to ", "…  / for commands"
        ))
        for welcome in self.query(".empty-chat"):
            welcome.update(self.welcome_text())
        for frame in self.query(MessageFrame):
            frame.refresh_title(self.config.identity)
        if self.history_screen is not None:
            self.history_screen.refresh_identity()
        if self.turn_progress:
            self.set_turn_progress(self.turn_progress)

    def provider_connected(self, result):
        if result:
            self.model_settings_saved(result)
            self.push_screen(
                ModelScreen(
                    self.client, self.config, provider_name=result.get("provider"),
                    state=self.provider_menu_state,
                ),
                self.model_settings_saved,
            )

    def action_send(self):
        if self.current_page != "room":
            self.action_open_tab("room")
        else:
            self.submit()

    @on(Composer.Submit)
    def composer_submit(self):
        self.submit()

    @on(TextArea.Changed, "#composer")
    def composer_changed(self):
        composer = self.query_one("#composer", Composer)
        self.size_composer()
        text = composer.text
        if (
            text.startswith("/")
            and not text.startswith("//")
            and "\n" not in text
            and " " not in text
        ):
            options = self.query_one("#slash-suggestions", OptionList)
            matches = matching_commands(text)
            options.clear_options().add_options(command_option(c, compact=True) for c in matches)
            options.highlighted = 0 if matches else None
            options.display = bool(matches)
            self.slash_visible = bool(matches)
        else:
            self.hide_slash()
        self.refresh_bindings()

    def size_composer(self):
        composer = self.query_one("#composer", Composer)
        composer.styles.height = min(6, max(3, composer.wrapped_document.height + 2))

    def render_attachments(self) -> None:
        indicator = self.query_one("#attachments", Static)
        images = self.draft_images or (self.pending_images if not self.busy else [])
        indicator.display = bool(images)
        if not images:
            return
        count = len(images)
        label = f"{count} image{'s' if count != 1 else ''}"
        if self.draft_images:
            label += " attached · /clear-attachments"
        else:
            label += " waiting · /retry" + (" [without-memory]" if self.awaiting_recall else "")
        indicator.update(Text(label, no_wrap=True, overflow="ellipsis"))

    @work(group="clipboard", exit_on_error=False)
    async def paste_clipboard(self) -> None:
        if self.pasting_image:
            return
        if self.current_page != "room" or isinstance(self.screen, ModalScreen):
            return
        revision = self.clipboard_revision
        session_id = self.session_id
        composer = self.query_one("#composer", Composer)
        draft_text, draft_selection = composer.text, composer.selection
        self.pasting_image = True
        try:
            image = await read_clipboard_image()
            if revision != self.clipboard_revision or session_id != self.session_id:
                return
            if image is None:
                text = await read_clipboard_text()
                if revision != self.clipboard_revision or session_id != self.session_id:
                    return
                if (
                    self.current_page != "room" or isinstance(self.screen, ModalScreen)
                    or composer.text != draft_text or composer.selection != draft_selection
                ):
                    self.set_notice("Clipboard text wasn't pasted because the draft changed. /paste again.")
                    return
                if text:
                    result = composer.replace(text, *composer.selection, maintain_selection_offset=False)
                    composer.move_cursor(result.end_location)
                else:
                    self.set_notice("The clipboard has no image or text to paste.")
                return
            if len(self.draft_images) >= 4:
                raise ValueError("Attach up to 4 images per message. /clear-attachments removes them.")
            result = await self.client.request(
                "upload_image", data_base64=base64.b64encode(image).decode("ascii"),
                filename="Clipboard image.png", media_type="image/png",
            )
            if revision != self.clipboard_revision or session_id != self.session_id:
                return
            if not isinstance(result, dict) or not result.get("id"):
                raise ValueError("The image could not be attached. Try /paste again.")
            # Repeated terminal paste signals should not attach the same image twice.
            if not any(item["id"] == result["id"] for item in self.draft_images):
                self.draft_images.append(result)
            self.render_attachments()
        except Exception as exc:
            if revision == self.clipboard_revision and session_id == self.session_id:
                self.set_notice(display_error(exc), error=True)
        finally:
            if revision == self.clipboard_revision:
                self.pasting_image = False

    def cancel_clipboard_paste(self) -> None:
        self.clipboard_revision += 1
        self.workers.cancel_group(self, "clipboard")
        self.pasting_image = False

    def hide_slash(self):
        self.slash_visible = False
        self.query_one("#slash-suggestions").display = False

    def complete_slash(self):
        options = self.query_one("#slash-suggestions", OptionList)
        if options.highlighted is not None:
            command = find_command(str(options.get_option_at_index(options.highlighted).id))
            composer = self.query_one("#composer", Composer)
            composer.load_text("/" + command.name + (" " if command.arguments else ""))
            composer.move_cursor((0, len(composer.text)))

    @on(OptionList.OptionSelected, "#slash-suggestions")
    def slash_selected(self, event):
        self.query_one("#composer", Composer).load_text("")
        self.execute_command("/" + str(event.option.id))

    def submit(self):
        composer = self.query_one("#composer", Composer)
        text = composer.text.strip()
        if not text and not self.draft_images:
            return
        if text.startswith("/") and not text.startswith("//"):
            if self.slash_visible and " " not in text:
                options = self.query_one("#slash-suggestions", OptionList)
                if options.highlighted is not None:
                    text = "/" + str(options.get_option_at_index(options.highlighted).id)
            name = text[1:].split(" ", 1)[0].casefold()
            if not find_command(name):
                self.set_notice(
                    f"Unknown command /{name}. Use /help, or // to send a literal slash.",
                    error=True,
                )
                return
            composer.load_text("")
            self.hide_slash()
            self.execute_command(text)
            return
        if self.busy:
            self.set_notice("Still replying · Ctrl+C cancels. Your draft is kept.")
            return
        if self.pasting_image:
            self.set_notice("Attaching clipboard image… Your message draft is kept.")
            return
        if self.awaiting_recall:
            self.set_notice(
                "The previous message is waiting · /retry or /retry without-memory.",
                error=True,
            )
            return
        if not self.session_id or self.loaded_session != self.session_id:
            self.set_notice("The room is still connecting · /reload to try again.", error=True)
            return
        reload_transcript = self.pending_text is not None
        composer.load_text("")
        self.pending_text = text[1:] if text.startswith("//") else text
        self.pending_images, self.draft_images = self.draft_images, []
        self.busy = True
        self.render_attachments()
        self.refresh_bindings()
        self.generate(self.pending_text, reload=reload_transcript)

    @work(exclusive=True, group="status", exit_on_error=False)
    async def refresh_status(self):
        try:
            checked_session = self.session_id
            self.status = await self.client.request("status", session_id=checked_session)
            today = self.status["session"]
            self.render_status()
            if (
                not self.busy
                and self.pending_text is None
                and not self.resuming_past
                and self.session_id != today["id"]
            ):
                self.session_id = today["id"]
                await self.load_conversation(self.session_id)
                if checked_session and checked_session != self.session_id:
                    self.status = await self.client.request("status", session_id=self.session_id)
                    self.render_status()
                if not self.awaiting_recall:
                    self.set_notice("")
            error = self.status.get("outbox", {}).get("error")
            if error and not self.busy:
                self.set_notice("Memory retention queued · " + error, error=True)
        except Exception as exc:
            self.status = {"agent_mode": True} if self.status.get("agent_mode") else {}
            self.render_status()
            self.query_one("#service-status", Static).update(
                Text(("Agent mode | " if self.status.get("agent_mode") else "")
                     + "Service disconnected · /reload to reconnect", no_wrap=True, overflow="ellipsis")
            )
            if not self.busy:
                self.set_notice(display_error(exc), error=True)
            self.refresh_bindings()

    def load_editor(self, name: str, text: str, *, force: bool = False):
        editor = self.query_one(f"#{name}", TextArea)
        if force or name not in self.saved_editors or editor.text == self.saved_editors[name]:
            editor.load_text(text)
            self.saved_editors[name] = text

    @work(exclusive=True, group="data", exit_on_error=False)
    async def refresh_data(self, reload_page: str | None = None):
        try:
            self.overview = await self.client.request("overview")
            saved = self.overview["config"]
            self.config = AppConfig.model_validate({**saved, "theme": self.config.theme.model_dump()})
            self.apply_interface_preferences()
            self.render_tables()
            for name, content in {
                **{
                    f"edit-{name}": text
                    for name, text in self.overview["prompts"].items()
                    if name in PROMPTS
                },
            }.items():
                self.load_editor(name, content, force=reload_page == name)
        except Exception as exc:
            if not self.overview:
                if self.history_screen is not None:
                    self.history_screen.query_one("#history-count", Static).update(
                        "Could not load rooms · Ctrl+L to try again."
                    )
            self.notify(display_error(exc), severity="error", timeout=8)

    def render_tables(self, viewport_width: int | None = None):
        if not self.overview:
            return
        if self.history_screen is not None:
            self.history_screen.render_sessions()

    @work(exclusive=True, group="enter", exit_on_error=False)
    async def enter_room(self):
        self.action_open_tab("room")
        try:
            if not self.session_id:
                self.session_id = (await self.client.request("status"))["session"]["id"]
            await self.load_conversation(self.session_id)
            self.refresh_status()
        except Exception as exc:
            self.notify(display_error(exc), severity="error")

    async def load_conversation(self, session_id):
        data = await self.client.request("history", session_id=session_id)
        container = self.query_one("#conversation", VerticalScroll)
        await container.remove_children()
        for message in data["messages"]:
            await self.append_message(
                message["role"], message["content"], images=message.get("images"), scroll=False,
                created_at=message.get("created_at", ""),
                identity=(IdentityConfig.model_validate(message["identity"])
                          if "identity" in message else None),
            )
        if not data["messages"]:
            await container.mount(self.empty_conversation())
        self.loaded_session = session_id
        self.room_title = data["session"]["title"]
        self.render_header()
        container.scroll_end(animate=False)
        self.call_after_refresh(container.scroll_end, animate=False)

    async def append_message(
        self, role: str, text: str, *, images: list[dict] | None = None, scroll: bool = True,
        created_at: str | datetime | None = None,
        identity: IdentityConfig | None = None,
    ) -> Markdown:
        container = self.query_one("#conversation", VerticalScroll)
        for placeholder in list(container.query(".empty-chat")):
            await placeholder.remove()
        message = Markdown(text, classes="message")
        content: list[Markdown | Static] = [message]
        if images:
            count = len(images)
            icon = "" if self.ascii else "📷 "
            content.append(Static(
                f"[{icon}{count} image{'s' if count != 1 else ''} attached]",
                classes="message-attachments",
                markup=False,
            ))
        frame = MessageFrame(*content, role=role, identity=identity)
        frame.refresh_title(self.config.identity)
        frame.border_subtitle = message_timestamp(
            created_at if created_at is not None else datetime.now(timezone.utc), self.config.timezone
        )
        await container.mount(frame)
        if scroll:
            container.scroll_end(animate=False)
        return message

    def render_activity(self) -> None:
        screen = self.activity_screen
        if screen is not None and screen.is_mounted:
            screen.refresh_activity()

    def record_activity(self, event):
        entry = self.activity.record(event)
        self.render_activity()
        if event.get("status") == "started":
            self.set_notice(entry.label + "… · Ctrl+C cancels")
        elif event.get("status") in {"error", "unavailable"}:
            self.set_notice(
                entry.label + " · " + (activity_summary(event) or entry.status), error=True
            )

    @work(exclusive=True, group="generation", exit_on_error=False)
    async def generate(self, text, bypass=False, reload=False):
        self.busy = True
        self.turn_identity = self.config.identity.model_copy(deep=True)
        self.awaiting_recall = False
        self.render_attachments()
        self.activity = ActivityLog()
        self.render_activity()
        self.reply_text = ""
        self.reply_widget = None
        if self.config.memory.enabled and not bypass:
            self.set_notice("Reaching for memories… · Ctrl+C cancels")
        else:
            self.set_turn_progress("thinking")
        self.refresh_bindings()
        try:
            if reload:
                await self.load_conversation(self.session_id)
            user_widget = await self.append_message(
                "user", text, images=self.pending_images, identity=self.turn_identity
            )
            # Keep following through the frame's deferred layout changes; user scrolling
            # releases Textual's anchor until they return to the bottom.
            self.query_one("#conversation", VerticalScroll).anchor(
                self.config.preferences.follow_replies
            )
            async for event in self.client.stream(
                "generate",
                session_id=self.session_id,
                text=text,
                bypass_recall=bypass,
                resume=self.resuming_past,
                **({"images": [image["id"] for image in self.pending_images]}
                   if self.pending_images else {}),
            ):
                kind = event.get("type")
                if kind == "turn_started":
                    self.turn_identity = IdentityConfig.model_validate(event["identity"])
                    user_widget.parent.identity = self.turn_identity.model_copy(deep=True)
                    user_widget.parent.refresh_title(self.config.identity)
                    if self.turn_progress:
                        self.set_turn_progress(self.turn_progress)
                elif kind == "token":
                    conversation = self.query_one("#conversation", VerticalScroll)
                    follow = (
                        self.config.preferences.follow_replies
                        and conversation.is_vertical_scroll_end
                    )
                    if self.reply_widget is None:
                        self.reply_widget = await self.append_message(
                            "assistant", "", scroll=follow, identity=self.turn_identity
                        )
                    self.reply_text += event.get("text", "")
                    await self.reply_widget.update(self.reply_text)
                    if follow:
                        conversation.scroll_end(animate=False)
                    self.set_turn_progress("replying")
                elif kind == "model_request":
                    self.set_turn_progress("thinking")
                elif kind == "metrics":
                    self.status["metrics"] = event.get("metrics", {})
                    self.render_status()
                elif kind == "activity":
                    self.record_activity(event)
                elif kind == "recall_required":
                    self.awaiting_recall = True
                    self.set_notice(
                        "Memory unavailable · /retry to try recall again; "
                        "/retry without-memory to reply without memories\n"
                        + event.get("message", "Recall failed."),
                        error=True,
                    )
                elif kind == "error":
                    self.set_notice(
                        event.get("message", "Response failed") + " · /retry", error=True
                    )
                    if not self.query_one("#composer", Composer).text and not self.draft_images:
                        self.query_one("#composer", Composer).load_text(text)
                        self.draft_images = list(self.pending_images)
                elif kind == "completed":
                    user_stamp = event.get("metadata", {}).get("user_message_at")
                    if user_stamp:
                        user_widget.parent.border_subtitle = message_timestamp(
                            user_stamp, self.config.timezone
                        )
                    completed_at = event.get("turn", {}).get("created_at")
                    if self.reply_widget is None:
                        conversation = self.query_one("#conversation", VerticalScroll)
                        self.reply_widget = await self.append_message(
                            "assistant", event.get("text", ""), created_at=completed_at,
                            identity=self.turn_identity,
                            scroll=(conversation.is_vertical_scroll_end
                                    and self.config.preferences.follow_replies),
                        )
                    elif completed_at:
                        self.reply_widget.parent.border_subtitle = message_timestamp(
                            completed_at, self.config.timezone
                        )
                    self.pending_text = None
                    self.pending_images = []
                    self.set_notice("")
                    self.refresh_data()
        except Exception as exc:
            self.set_notice(display_error(exc) + " · /retry", error=True)
            if not self.query_one("#composer", Composer).text and not self.draft_images:
                self.query_one("#composer", Composer).load_text(text)
                self.draft_images = list(self.pending_images)
        finally:
            self.busy = False
            self.turn_identity = None
            self.turn_progress = None
            self.query_one(ThinkingSpinner).set_active(False)

            def release_scroll_anchor() -> None:
                if not self.busy:
                    self.query_one("#conversation", VerticalScroll).anchor(False)

            # Let the final message layout settle, unless a new turn has already started.
            self.call_after_refresh(release_scroll_anchor)
            self.render_activity()
            self.render_attachments()
            self.refresh_bindings()
            self.refresh_status()

    @work(exit_on_error=False)
    async def action_cancel_turn(self):
        if self.busy:
            try:
                self.set_notice("Cancelling the response…")
                await self.client.request("cancel")
            except Exception as exc:
                self.notify(display_error(exc), severity="error")

    @on(Select.Changed, "#prompt-select")
    def prompt_changed(self, event):
        if event.value in PROMPTS:
            self.query_one("#prompt-pages", ContentSwitcher).current = f"prompt-{event.value}"
            self.refresh_bindings()

    @on(TextArea.Changed, ".editor")
    def editor_changed(self):
        self.render_status()

    @work(group="commands", exit_on_error=False)
    async def execute_command(self, text: str):
        name, _, argument = text.strip().removeprefix("/").partition(" ")
        command = find_command(name.casefold())
        if not command:
            self.notify("Unknown command. Use /help.", severity="warning")
            return
        name, argument = command.name, argument.strip()
        try:
            if argument and not command.arguments:
                raise ValueError(f"/{name} doesn't take an argument.")
            if name in {"history", "activity", "jobs"}:
                self.action_open_tab(name)
            elif name == "skills":
                self.action_open_tab("room")
                self.push_screen(SkillsScreen(self.client, self.skills_state))
            elif name == "preferences":
                self.open_preferences()
            elif name == "paste":
                self.action_open_tab("room")
                self.paste_clipboard()
            elif name == "clear-attachments":
                self.cancel_clipboard_paste()
                self.draft_images = []
                if not self.busy:
                    self.pending_images = []
                    if self.pending_text == "":
                        self.pending_text, self.awaiting_recall = None, False
                        self.set_notice("")
                        self.enter_room()
                self.render_attachments()
            elif name == "memory":
                self.open_memory()
            elif name == "agent":
                if argument.casefold() not in {"", "on", "off"}:
                    raise ValueError("Use /agent to toggle, /agent on, or /agent off.")
                params = {"enabled": argument.casefold() == "on"} if argument else {}
                result = await self.client.request("agent_mode", **params)
                self.status["agent_mode"] = result["agent_mode"]
                self.render_status()
                self.set_notice("Agent mode enabled · file editing and commands are available."
                                if result["agent_mode"] else "Agent mode disabled.")
                self.refresh_status()
            elif name in {"connect", "models"}:
                self.open_model_settings(connect=name == "connect")
            elif name == "prompts":
                source = argument.upper()
                if source and source not in PROMPTS:
                    raise ValueError("Choose SYSTEM, SOUL, USER, or POST_HISTORY.")
                self.action_open_tab("prompts")
                if source:
                    self.query_one("#prompt-select", Select).value = source
                    self.query_one("#prompt-pages", ContentSwitcher).current = f"prompt-{source}"
                    self.query_one(f"#edit-{source}", TextArea).focus()
            elif name == "theme":
                if argument:
                    palette = argument.casefold().replace(" ", "-")
                    if palette == "custom":
                        if not self.config.theme.custom_base and not self.config.theme.colors:
                            raise ValueError("Create a custom theme in /theme first.")
                        palette = resolve_theme(
                            self.config.theme.custom_base or self.config.theme.name
                        )
                        colors = (self.config.theme.custom_colors if self.config.theme.custom_base
                                  else self.config.theme.colors)
                    else:
                        colors = {}
                    validate_theme(palette, colors)
                    data = await self.client.request("save_theme", name=palette, colors=colors)
                    self.theme_saved(data)
                else:
                    self.open_themes()
            elif name == "inspect":
                self.action_inspect_requests()
            elif name == "help":
                self.push_screen(
                    HelpScreen(
                        self.config.bindings.get("send", "enter"),
                        self.config.bindings.get("newline", "shift+enter"),
                    )
                )
            elif name == "quit":
                self.exit()
            elif name == "suspend":
                self.action_suspend_process()
            elif name == "cancel":
                self.action_cancel_turn()
            elif name == "reload":
                self.refresh_data(reload_page=self.active_editor)
                self.refresh_status()
                if self.current_page == "room" and not self.busy and self.session_id:
                    await self.load_conversation(self.session_id)
            elif name == "check":
                self.action_open_tab("connections")
                await self.query_one("#connection-details", Markdown).update(
                    "Checking connections…"
                )
                data = await self.client.request("doctor")
                await self.query_one("#connection-details", Markdown).update(
                    "## Connection check\n\n" + readable_value(data)
                )
                self.refresh_status()
            elif name == "today":
                await self.change_room()
            elif name == "retry":
                if argument not in {"", "without-memory"}:
                    raise ValueError("Use /retry, or /retry without-memory after memory recall fails.")
                if self.busy:
                    raise ValueError("The response is still running. Ctrl+C cancels it.")
                if isinstance(self.screen, JobsScreen) and not argument:
                    self.screen.action_retry()
                else:
                    if self.pending_text is None:
                        raise ValueError("No message is waiting for retry.")
                    if argument and not self.awaiting_recall:
                        raise ValueError("without-memory is only available after memory recall fails.")
                    self.action_open_tab("room")
                    composer = self.query_one("#composer", Composer)
                    if composer.text.strip() == self.pending_text:
                        composer.load_text("")
                        if self.draft_images == self.pending_images:
                            self.draft_images = []
                    self.busy = True
                    self.generate(self.pending_text, bypass=bool(argument), reload=True)
        except Exception as exc:
            if name == "check":
                await self.query_one("#connection-details", Markdown).update(
                    "## Connection check failed\n\n"
                    + display_error(exc)
                    + "\n\nUse /check to try again."
                )
            self.notify(display_error(exc), severity="error", timeout=8)

    async def change_room(self, session_id: str | None = None) -> None:
        if self.busy:
            raise ValueError("Finish or cancel the active response before changing rooms.")
        selected = session_id or (await self.client.request("status"))["session"]["id"]
        self.cancel_clipboard_paste()
        self.resuming_past = session_id is not None
        self.session_id = selected
        self.pending_text, self.awaiting_recall = None, False
        self.pending_images = []
        self.render_attachments()
        self.set_notice("")
        self.enter_room()

    @work(group="commands", exit_on_error=False)
    async def action_save_prompt(self) -> None:
        try:
            await self.save_editor()
        except Exception as exc:
            self.notify(display_error(exc), severity="error", timeout=8)

    async def save_editor(self):
        editor_id = self.active_editor
        if not editor_id:
            raise ValueError("Open a prompt editor first.")
        text = self.query_one(f"#{editor_id}", TextArea).text
        if self.current_page == "prompts":
            source = editor_id.removeprefix("edit-")
            await self.client.request("save_prompt", name=source, text=text)
        self.saved_editors[editor_id] = text
        self.render_status()
        self.notify(
            "Saved · applies to the next response."
            if self.current_page == "prompts"
            else "Saved."
        )
