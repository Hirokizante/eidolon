"""A focused, typed editor for app behavior."""

import asyncio
from dataclasses import dataclass, field as dataclass_field

from pydantic import ValidationError
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, ContentSwitcher, Input, Select, Static

from eidolon.config import SEARCH_PROVIDERS, AppConfig
from eidolon.preferences import SearchCredentialError, UserPreferences
from eidolon.ui.auxiliary import ADVANCED, AuxiliaryForm, AuxiliaryState
from eidolon.ui.forms import FormBody, FormCheckbox, FormModal
from eidolon.ui.models import ModelScreen, safe_notice
from eidolon.ui.status import COMPONENT_LABELS
from eidolon.ui.vision import ADVANCED as VISION_ADVANCED
from eidolon.ui.vision import VisionForm, VisionState
from eidolon.ui.web_search import WebSearchForm, WebSearchState


STATUS_OPTIONS = (("Hidden", "hidden"),
                  *((label, name) for name, label in COMPONENT_LABELS.items()))


@dataclass(frozen=True)
class PreferenceField:
    name: str
    label: str
    help: str
    group: str
    kind: str = "boolean"
    options: tuple[tuple[str, str], ...] = ()


FIELDS = (
    PreferenceField("user_name", "Your name", "1–80 characters; surrounding spaces are trimmed.",
                    "identity", "text"),
    PreferenceField("companion_name", "Companion's name", "Used for future conversations.",
                    "identity", "text"),
    PreferenceField("bindings", "Send a message", "The other key adds a new line.",
                    "conversation", "keys"),
    PreferenceField("follow_replies", "Follow streaming replies",
                    "Scroll with the reply while you are at the bottom.", "conversation"),
    PreferenceField("show_service_status", "Show status bar",
                    "Display model and usage details below the composer.", "conversation"),
    PreferenceField("status_components", "Status bar layout",
                    "Read left to right. Choose each component once; Hidden skips a position.",
                    "status", "layout"),
    PreferenceField("timezone", "Timezone", "IANA name, for example Europe/Paris.",
                    "daily", "text"),
    PreferenceField("reset_hour", "New day starts at", "Local hour, from 0 to 23.",
                    "daily", "integer"),
    PreferenceField("memory_enabled", "Use learned memories",
                    "Supply learned memories to replies. Saving is controlled separately.",
                    "memory"),
    PreferenceField("memory_save_enabled", "Save new memories",
                    "Local history is kept. Turns accepted while off are never learned later. "
                    "Earlier eligible exchanges still deliver; pause them in /memory.", "memory"),
    PreferenceField("recall_budget", "Recall effort", "Higher effort may take longer.",
                    "memory", "select", (("Low", "low"), ("Medium", "mid"), ("High", "high"))),
    PreferenceField("max_memories", "Maximum recalled memories", "From 1 to 50; default 12.",
                    "memory", "integer"),
    PreferenceField("continuity_enabled", "Update current life automatically",
                    "Learn current situations and open threads from eligible chat exchanges. "
                    "Manual refresh is available in /memory.", "continuity"),
    PreferenceField("continuity_use_enabled", "Use current life in replies",
                    "Supply the learned snapshot and recent eligible exchanges when learned "
                    "memories are enabled. Off by default while the candidate is reviewed.",
                    "continuity"),
    PreferenceField("continuity_max_notes", "Maximum active notes",
                    "From 1 to 64; default 12. Archived notes do not count. "
                    "At capacity, a new topic must replace a less important note.",
                    "continuity", "integer"),
    PreferenceField("continuity_batch_exchanges", "Completed exchanges per update",
                    "From 1 to 32; default 8. Larger backlogs are processed in bounded batches.",
                    "continuity", "integer"),
    PreferenceField("continuity_idle_seconds", "Idle time before update",
                    "Seconds from 1 to 86400; default 600 (ten minutes).",
                    "continuity", "integer"),
    PreferenceField("avatar_enabled", "Use the avatar",
                    "Enable expression and lifecycle events for the configured avatar.", "avatar"),
)
GROUPS = {"identity": "Identity", "conversation": "Conversation", "status": "Status bar",
          "daily": "Daily room",
          "memory": "Memory", "continuity": "Current life", "auxiliary": "Auxiliary model",
          "vision": "Vision model", "avatar": "Avatar", "web": "Web search"}
SEND_MODES = {
    "enter": {"send": "enter", "newline": "shift+enter"},
    "ctrl+enter": {"send": "ctrl+enter", "newline": "enter"},
}


@dataclass
class PreferencesState:
    draft: dict | None = None
    category: str = "conversation"
    auxiliary: AuxiliaryState = dataclass_field(default_factory=AuxiliaryState)
    vision: VisionState = dataclass_field(default_factory=VisionState)
    web: WebSearchState = dataclass_field(default_factory=WebSearchState)


class PreferencesScreen(FormModal):
    DEFAULT_CSS = """
    PreferencesScreen #preference-pages { height: 1fr; }
    PreferencesScreen Checkbox { height: 1; border: none; padding: 0; margin-top: 1; }
    PreferencesScreen Checkbox:focus { border: none; }
    PreferencesScreen .field-help { margin-bottom: 1; }
    PreferencesScreen #preference-category { margin-bottom: 1; }
    PreferencesScreen.short Checkbox { margin-top: 0; }
    PreferencesScreen.short .field-help { margin-bottom: 0; }
    PreferencesScreen.short #preference-category { margin-bottom: 0; }
    PreferencesScreen.identity.narrow .form-subtitle,
    PreferencesScreen.identity.narrow .form-status { display: none; }
    PreferencesScreen.identity.narrow .form-status.problem { display: block; }
    PreferencesScreen.identity.narrow .form-status.field-problem { display: none; }
    PreferencesScreen.identity.short .form-hint,
    PreferencesScreen.identity.short .field-error,
    PreferencesScreen.identity.short .form-status.problem { display: none; }
    PreferencesScreen.narrow Checkbox { margin-top: 0; }
    PreferencesScreen.narrow .field-help { margin-bottom: 0; }
    PreferencesScreen.narrow #preference-category { margin-bottom: 0; }
    PreferencesScreen.auxiliary.narrow .form-subtitle,
    PreferencesScreen.auxiliary.narrow .form-status,
    PreferencesScreen.vision.narrow .form-subtitle,
    PreferencesScreen.vision.narrow .form-status,
    PreferencesScreen.web.narrow .form-subtitle,
    PreferencesScreen.web.narrow .form-status { display: none; }
    PreferencesScreen.continuity.narrow .form-subtitle,
    PreferencesScreen.continuity.narrow .form-status { display: none; }
    PreferencesScreen.continuity.narrow .form-status.problem { display: block; }
    PreferencesScreen.continuity.narrow .form-status.field-problem { display: none; }
    PreferencesScreen.auxiliary.narrow .form-status.problem,
    PreferencesScreen.vision.narrow .form-status.problem,
    PreferencesScreen.web.narrow .form-status.problem { display: block; }
    PreferencesScreen.auxiliary.narrow .form-status.field-problem,
    PreferencesScreen.vision.narrow .form-status.field-problem,
    PreferencesScreen.web.narrow .form-status.field-problem { display: none; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+s", "save", "Apply", priority=True),
        Binding("ctrl+l", "reload", "Discard edits", priority=True),
    ]

    def __init__(self, client, config: AppConfig, state: PreferencesState):
        super().__init__()
        self.client, self.config, self.state = client, config, state
        self.saved = UserPreferences.from_config(config).model_dump()
        self.values = {**self.saved, **(state.draft or {})}
        self.committing = False
        self.reloading = False
        self.auxiliary = AuxiliaryForm(config, state.auxiliary)
        self.vision = VisionForm(config, state.vision)
        self.web = WebSearchForm(config, state.web)
        self._connection_result = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Preferences", classes="form-title")
            yield Static(self.category_help(self.state.category), classes="form-subtitle")
            yield Select([(label, group) for group, label in GROUPS.items()],
                         value=self.state.category, allow_blank=False, compact=True,
                         id="preference-category")
            with ContentSwitcher(initial="preference-" + self.state.category, id="preference-pages"):
                for group in GROUPS:
                    if group == "auxiliary":
                        yield self.auxiliary
                        continue
                    if group == "vision":
                        yield self.vision
                        continue
                    if group == "web":
                        yield self.web
                        continue
                    with FormBody(id="preference-" + group, classes="form-body"):
                        for field in FIELDS:
                            if field.group != group:
                                continue
                            value = self.values[field.name]
                            control_id = f"pref-{field.name}"
                            if field.kind == "boolean":
                                yield FormCheckbox(field.label, value=value, id=control_id)
                            else:
                                if field.kind != "layout":
                                    yield Static(field.label, classes="field-label")
                                if field.kind == "layout":
                                    for index in range(len(COMPONENT_LABELS)):
                                        yield Select(
                                            [(f"{index + 1}. {label}", name) for label, name in
                                             STATUS_OPTIONS],
                                            value=value[index] if index < len(value) else "hidden",
                                            allow_blank=False, compact=True,
                                            id=control_id if index == 0 else f"{control_id}-{index}",
                                        )
                                        if index == 0:
                                            yield Static("", id=control_id + "-error",
                                                         classes="field-error", markup=False)
                                elif field.kind in {"text", "integer"}:
                                    yield Input(str(value), id=control_id)
                                else:
                                    options = list(field.options)
                                    if field.kind == "keys":
                                        options = [("Enter sends / Shift+Enter newline", "enter"),
                                                   ("Ctrl+Enter sends / Enter newline", "ctrl+enter")]
                                        mode = next((key for key, keys in SEND_MODES.items()
                                                     if keys == value), "custom")
                                        if mode == "custom":
                                            options.append((f"Saved: {value['send']} sends / "
                                                            f"{value['newline']} newline", "custom"))
                                        value = mode
                                    elif value not in {value for _, value in options}:
                                        options.append((f"Saved: {value}", value))
                                    yield Select(options, value=value, allow_blank=False,
                                                 compact=True, id=control_id)
                            if field.kind != "layout":
                                yield Static("", id=control_id + "-error", classes="field-error",
                                             markup=False)
                            yield Static(field.help, classes="field-help")
            yield Static("Changes are applied only when saved.", classes="form-status",
                         markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("Apply", id="preferences-apply", flat=True, compact=True)
                yield Button("Test", id="aux-test", flat=True, compact=True)
                yield Button("Test", id="vision-test", flat=True, compact=True)
                yield Button("Test", id="web-test", flat=True, compact=True)
                yield Button("Discard edits", id="preferences-discard", flat=True, compact=True)
                yield Button("Close", id="preferences-close", flat=True, compact=True)
            yield Static("Tab fields | Ctrl+S apply | Esc keeps draft", classes="form-hint")
        yield Static("Terminal too small — need 48×12. Resize or press Esc to close.",
                     classes="form-too-small", markup=False)

    def on_mount(self):
        self.resize_state()
        self.update_auxiliary_actions()
        self.query_one("#preference-category", Select).focus()
        if any(draft is not None for draft in (
            self.state.draft, self.state.auxiliary.draft, self.state.vision.draft,
            self.state.web.draft,
        )) or self.state.web.keys or self.state.web.removed:
            self.status("Unsaved draft restored · Ctrl+S applies changes.")

    def responsive_copy(self, width: int):
        self.query_one(".form-hint", Static).update(
            "Tab fields | ^S apply | Esc keeps draft" if width < 70
            else "Tab fields | Ctrl+S apply | Esc keeps draft"
        )
        if self.has_class("short") and self.state.category in {"auxiliary", "vision"}:
            role = "aux" if self.state.category == "auxiliary" else "vision"
            self.query_one(".form-dialog").border_title = str(
                self.query_one(f"#{role}-target", Static).render()
            )
        if self.is_mounted and self.has_class("short") and self.state.category == "web":
            self.query_one(".form-dialog").border_title = self.web.focus_title()
        self.update_field_title()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if self.is_mounted and self.has_class("short") and self.state.category == "web":
            self.query_one(".form-dialog").border_title = self.web.focus_title()
        self.update_field_title()

    def update_field_title(self) -> None:
        dialog = self.query_one(".form-dialog")
        dialog.border_subtitle = ""
        identity_short = self.state.category == "identity" and self.has_class("short")
        if (self.state.category not in {"continuity", "identity"}
                or not (self.has_class("narrow") or identity_short)):
            return
        field = next((field for field in FIELDS
                      if getattr(self.focused, "id", None) == "pref-" + field.name), None)
        title = GROUPS[self.state.category] + (" · " + field.label if field else "")
        dialog.border_title = title
        if identity_short:
            status = self.query_one(".form-status", Static)
            if status.has_class("problem") and not status.has_class("field-problem"):
                dialog.border_subtitle = Text(str(status.render()))
            elif field:
                error = self.query_one(f"#pref-{field.name}-error", Static)
                if error.has_class("problem"):
                    dialog.border_subtitle = Text(str(error.render()))

    def status(self, message: str, *, problem: bool = False) -> None:
        super().status(message, problem=problem)
        self.update_field_title()

    def field_error(self, field_id: str, message: str) -> None:
        if field_id in {"pref-user_name", "pref-companion_name"}:
            message = message.removeprefix("Value error, ")
        super().field_error(field_id, message)
        self.update_field_title()

    def clear_errors(self) -> None:
        super().clear_errors()
        self.update_field_title()

    @on(Select.Changed, "#preference-category")
    def category_changed(self, event: Select.Changed):
        if event.value in GROUPS:
            self.query_one("#preference-pages", ContentSwitcher).current = "preference-" + event.value
            self.state.category = event.value
            self.query_one(".form-subtitle", Static).update(self.category_help(event.value))
            self.update_auxiliary_actions()

    def category_help(self, category: str) -> str:
        if category == "identity":
            return "Names for you and your companion · saved in config.yaml"
        if category == "status":
            return "Left to right | Hidden skips a position"
        if category == "auxiliary":
            return "Processes eligible conversation evidence for background tasks."
        if category == "vision":
            return "A separate model analyzes attached images when more detail is needed."
        if category == "web":
            return "Choose a search provider and configure its connection."
        return "Your room's behavior · saved in config.yaml"

    def update_auxiliary_actions(self) -> None:
        if self.has_class("narrow") and not self.has_class("short"):
            self.query_one(".form-dialog").border_title = ""
        self.set_class(self.state.category == "auxiliary", "auxiliary")
        self.set_class(self.state.category == "vision", "vision")
        self.set_class(self.state.category == "web", "web")
        self.set_class(self.state.category == "continuity", "continuity")
        self.set_class(self.state.category == "identity", "identity")
        test = self.query_one("#aux-test", Button)
        test.display = self.state.category == "auxiliary"
        test.disabled = (self.committing or self.reloading
                         or self.auxiliary.query_one("#aux-mode", Select).value == "disabled")
        vision_test = self.query_one("#vision-test", Button)
        vision_test.display = self.state.category == "vision"
        vision_test.disabled = (self.committing or self.reloading
                               or self.vision.query_one("#vision-mode", Select).value == "disabled")
        web_test = self.query_one("#web-test", Button)
        web_test.display = self.state.category == "web"
        web_test.disabled = self.committing or self.reloading
        provider = self.web.query_one("#web-provider", Select).value
        self.query_one(".form-title", Static).update(
            "Preferences · " + SEARCH_PROVIDERS[provider] if self.state.category == "web"
            else "Preferences"
        )
        if self.has_class("short"):
            role = "aux" if self.state.category == "auxiliary" else "vision"
            self.query_one(".form-dialog").border_title = (
                str(self.query_one(f"#{role}-target", Static).render())
                if self.state.category in {"auxiliary", "vision"} else "Preferences"
            )
            if self.state.category == "web":
                self.query_one(".form-dialog").border_title = self.web.focus_title()
        self.update_field_title()

    def set_controls_disabled(self, disabled: bool) -> None:
        self.query("Button, Input, Select, Checkbox").set(disabled=disabled)
        self.update_auxiliary_actions()

    def read_values(self) -> dict:
        # Legacy search preferences remain available to older service clients.
        # The Web search form edits the provider settings independently.
        values = self.saved.copy()
        for field in FIELDS:
            control = self.query_one(f"#pref-{field.name}")
            if field.kind == "layout":
                value = [self.query_one(
                    f"#pref-{field.name}" + (f"-{index}" if index else ""), Select,
                ).value for index in range(len(COMPONENT_LABELS))]
                value = [component for component in value if component != "hidden"]
            elif isinstance(control, Checkbox):
                value = control.value
            elif isinstance(control, Input):
                value = control.value
                if field.kind == "integer":
                    try:
                        value = int(value)
                    except ValueError:
                        pass
            else:
                value = control.value
                if field.kind == "keys":
                    value = SEND_MODES.get(value, self.values["bindings"]).copy()
            values[field.name] = value
        return values

    def remember_draft(self):
        if self._committed:
            return
        self.state.draft = {
            name: value for name, value in self.read_values().items() if value != self.saved[name]
        } or None
        self.auxiliary.remember_draft()
        self.vision.remember_draft()
        self.web.remember_draft()
        self.state.category = self.query_one("#preference-category", Select).value

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    def preference_changed(self, event):
        if self.is_mounted and not self.committing and not self.reloading:
            if getattr(event.control, "id", None) == "preference-category":
                return
            self.update_dirty_status()

    def update_dirty_status(self) -> None:
        dirty = (self.read_values() != self.saved or bool(self.auxiliary.changes())
                 or bool(self.vision.changes()) or self.web.is_dirty())
        self.status("Unsaved changes · Ctrl+S applies them." if dirty
                    else "Changes are applied only when saved.")
        self.update_auxiliary_actions()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "aux-choose":
            self.choose_auxiliary_model()
        elif event.button.id == "aux-test":
            self.test_auxiliary()
        elif event.button.id == "vision-choose":
            self.choose_vision_model()
        elif event.button.id == "vision-test":
            self.test_vision()
        elif event.button.id == "web-test":
            self.test_web_search()
        elif event.button.id == "preferences-apply":
            self.action_save()
        elif event.button.id == "preferences-discard":
            self.action_reload()
        elif event.button.id == "preferences-close":
            self.action_close()

    def dismiss(self, result=None):
        return super().dismiss(result if result is not None else self._connection_result)

    @work(group="preferences-auxiliary-picker", exclusive=True, exit_on_error=False)
    async def choose_auxiliary_model(self) -> None:
        if self.committing or self.reloading:
            return
        self.auxiliary.values = self.auxiliary.read_values()
        self.remember_draft()
        self.query_one(".form-dialog").display = False
        try:
            values = self.auxiliary.values
            follow = values["mode"] == "inherit"
            picker = ModelScreen(
                self.client, self.config, self.config.provider if follow else values["provider"],
                state=self.state.auxiliary.picker, role="auxiliary",
                model=self.config.selected_provider().model if follow else values["model"],
                effort=values["reasoning_effort"], context_window=values["context_window"],
                max_response_tokens=values["max_output_tokens"], follow_dialogue=follow,
            )
            result = await self.app.push_screen_wait(picker)
            # Saved connections survive dismissal of the model selection.
            if picker.config.providers != self.config.providers:
                self._connection_result = picker.config.model_dump(mode="json")
            self.config = self.auxiliary.config = self.vision.config = picker.config
            if result:
                self.auxiliary.select_model(result)
            self.update_dirty_status()
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.query_one(".form-dialog").display = True
            self.resize_state()
            self.auxiliary.update_target()
            self.query_one("#aux-choose", Button).focus()

    @work(group="preferences-vision-picker", exclusive=True, exit_on_error=False)
    async def choose_vision_model(self) -> None:
        if self.committing or self.reloading:
            return
        self.vision.values = self.vision.read_values()
        self.remember_draft()
        self.query_one(".form-dialog").display = False
        try:
            values = self.vision.values
            picker = ModelScreen(
                self.client, self.config, values["provider"],
                state=self.state.vision.picker, role="vision", model=values["model"],
                effort=values["reasoning_effort"], context_window=values["context_window"],
                max_response_tokens=values["max_output_tokens"],
            )
            result = await self.app.push_screen_wait(picker)
            if picker.config.providers != self.config.providers:
                self._connection_result = picker.config.model_dump(mode="json")
            self.config = self.auxiliary.config = self.vision.config = picker.config
            if result:
                self.vision.select_model(result)
            self.update_dirty_status()
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.query_one(".form-dialog").display = True
            self.resize_state()
            self.vision.update_target()
            self.query_one("#vision-choose", Button).focus()

    def validate_vision(self) -> dict | None:
        try:
            return self.vision.validated().model_dump()
        except (ValidationError, ValueError) as exc:
            self.committing = False
            self.set_controls_disabled(False)
            self.query_one("#preference-category", Select).value = "vision"
            self.query_one("#preference-pages", ContentSwitcher).current = "preference-vision"
            if isinstance(exc, ValidationError):
                error = exc.errors(include_input=False, include_url=False)[0]
                name = error["loc"][0]
                if name in {item[0] for item in VISION_ADVANCED}:
                    self.field_error("vision-" + name, error["msg"])
                    return None
            self.status(safe_notice(exc), problem=True)
            return None

    @work(group="preferences-vision-test", exit_on_error=False)
    async def test_vision(self) -> None:
        if self.committing or self.reloading:
            return
        self.clear_errors()
        vision = self.validate_vision()
        if vision is None:
            return
        self.committing = True
        self.set_controls_disabled(True)
        self.status("Testing with a synthetic image…")
        try:
            result = await self.client.request("test_vision_model", vision=vision)
            self.status(result["notice"], problem=not result["ok"])
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.committing = False
            self.set_controls_disabled(False)

    def validate_auxiliary(self) -> dict | None:
        try:
            return self.auxiliary.validated().model_dump()
        except (ValidationError, ValueError) as exc:
            self.committing = False
            self.set_controls_disabled(False)
            self.query_one("#preference-category", Select).value = "auxiliary"
            self.query_one("#preference-pages", ContentSwitcher).current = "preference-auxiliary"
            if isinstance(exc, ValidationError):
                error = exc.errors(include_input=False, include_url=False)[0]
                name = error["loc"][0]
                if name in {item[0] for item in ADVANCED}:
                    self.field_error("aux-" + name, error["msg"])
                    return None
            self.status(safe_notice(exc), problem=True)
            return None

    @work(group="preferences-auxiliary-test", exit_on_error=False)
    async def test_auxiliary(self) -> None:
        if self.committing or self.reloading:
            return
        self.clear_errors()
        auxiliary = self.validate_auxiliary()
        if auxiliary is None:
            return
        self.committing = True
        self.set_controls_disabled(True)
        self.status("Testing with synthetic text only…")
        try:
            result = await self.client.request("test_auxiliary_model", auxiliary=auxiliary)
            self.status(result["notice"], problem=not result["ok"])
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.committing = False
            self.set_controls_disabled(False)

    def validate_web_search(self) -> bool:
        try:
            self.web.validated()
            return True
        except (ValidationError, ValueError) as exc:
            self.committing = False
            self.set_controls_disabled(False)
            self.query_one("#preference-category", Select).value = "web"
            self.query_one("#preference-pages", ContentSwitcher).current = "preference-web"
            if isinstance(exc, SearchCredentialError):
                self.field_error(self.web.error_field(exc.provider, "key"), str(exc))
            elif isinstance(exc, ValidationError):
                error = exc.errors(include_input=False, include_url=False)[0]
                location = error["loc"]
                field_id = (self.web.error_field(location[0], location[1] if len(location) > 1 else None)
                            if location and location[0] in SEARCH_PROVIDERS else "web-provider")
                self.field_error(field_id, error["msg"])
            else:
                self.status(safe_notice(exc), problem=True)
            return False

    @work(group="preferences-web-test", exit_on_error=False)
    async def test_web_search(self) -> None:
        if self.committing or self.reloading:
            return
        self.clear_errors()
        if not self.validate_web_search():
            return
        self.committing = True
        self.set_controls_disabled(True)
        self.status("Testing with one web search…")
        try:
            result = await self.client.request(
                "test_web_search", web_search={**self.web.changes(),
                    "provider": self.web.query_one("#web-provider", Select).value},
                web_search_api_keys=self.web.credentials(),
            )
            self.status(f"{SEARCH_PROVIDERS[result['provider']]} connection passed · "
                        f"{result['results']} results returned. Settings are still unsaved.")
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.committing = False
            self.set_controls_disabled(False)

    @work(group="preferences-save", exit_on_error=False)
    async def action_save(self):
        if self.committing or self.reloading:
            return
        self.clear_errors()
        self.committing = True
        self.set_controls_disabled(True)
        try:
            candidate = (await asyncio.to_thread(
                UserPreferences.model_validate, self.read_values(),
            )).model_dump()
        except ValidationError as exc:
            self.committing = False
            self.set_controls_disabled(False)
            error = exc.errors(include_url=False, include_input=False)[0]
            name = error["loc"][0]
            group = next(field.group for field in FIELDS if field.name == name)
            self.query_one("#preference-category", Select).value = group
            self.query_one("#preference-pages", ContentSwitcher).current = "preference-" + group
            self.field_error(f"pref-{name}", error["msg"])
            return
        changes = {name: value for name, value in candidate.items() if value != self.saved[name]}
        auxiliary_changes = self.auxiliary.changes()
        if auxiliary_changes:
            auxiliary = self.validate_auxiliary()
            if auxiliary is None:
                return
            auxiliary_changes = {name: auxiliary[name] for name in auxiliary_changes}
        vision_changes = self.vision.changes()
        if vision_changes:
            vision = self.validate_vision()
            if vision is None:
                return
            vision_changes = {name: vision[name] for name in vision_changes}
        web_changed = self.web.is_dirty()
        if web_changed and not self.validate_web_search():
            return
        web_changes = self.web.changes()
        web_keys = self.web.credentials()
        if not changes and not auxiliary_changes and not vision_changes and not web_changed:
            self.state.draft = None
            self.state.auxiliary.draft = None
            self.state.auxiliary.context_confirmed = False
            self.state.vision.draft = None
            self.state.vision.context_confirmed = False
            self.state.web.clear()
            self._committed = True
            self.committing = False
            self.dismiss(None)
            return
        self.status("Saving preferences…")
        try:
            params = {"preferences": changes}
            method = "save_preferences"
            if auxiliary_changes:
                # Older services reject this action instead of silently ignoring auxiliary edits.
                method = "save_preferences_with_auxiliary"
                params["auxiliary"] = auxiliary_changes
            if vision_changes:
                method = "save_preferences_with_vision"
                params["vision"] = vision_changes
            if web_changed:
                method = "save_preferences_with_web_search"
                params["web_search"] = web_changes
                if web_keys:
                    params["web_search_api_keys"] = web_keys
            result = await self.client.request(method, **params)
            self.state.draft = None
            self.state.auxiliary.draft = None
            self.state.auxiliary.context_confirmed = False
            self.state.vision.draft = None
            self.state.vision.context_confirmed = False
            self.state.web.clear()
            for provider in SEARCH_PROVIDERS:
                self.web.query_one(f"#web-{provider}-key", Input).value = ""
            self._committed = True
            self.dismiss(result)
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.committing = False
            self.set_controls_disabled(False)

    @work(group="preferences-reload", exit_on_error=False)
    async def action_reload(self):
        if self.committing or self.reloading:
            return
        self.reloading = True
        self.set_controls_disabled(True)
        self.status("Loading saved preferences…")
        try:
            config = AppConfig.model_validate((await self.client.request("overview"))["config"])
            self.config = config
            self.auxiliary.restore(config)
            self.vision.restore(config)
            self.web.restore(config)
            if self._connection_result is not None:
                self._connection_result = config.model_dump(mode="json")
            self.saved = UserPreferences.from_config(config).model_dump()
            self.values = self.saved
            for field in FIELDS:
                control = self.query_one(f"#pref-{field.name}")
                value = self.saved[field.name]
                if field.kind == "layout":
                    for index in range(len(COMPONENT_LABELS)):
                        self.query_one(
                            f"#pref-{field.name}" + (f"-{index}" if index else ""), Select,
                        ).value = value[index] if index < len(value) else "hidden"
                elif isinstance(control, Input):
                    control.value = str(value)
                elif field.kind == "keys":
                    options = [("Enter sends / Shift+Enter newline", "enter"),
                               ("Ctrl+Enter sends / Enter newline", "ctrl+enter")]
                    mode = next((key for key, keys in SEND_MODES.items() if keys == value), "custom")
                    if mode == "custom":
                        options.append(("Saved custom bindings", "custom"))
                    control.set_options(options)
                    control.value = mode
                else:
                    if isinstance(control, Select) and value not in {
                        option_value for _, option_value in field.options
                    }:
                        control.set_options([*field.options, (f"Saved: {value}", value)])
                    control.value = value
            self.state.draft = None
            self.clear_errors()
            self.status("Saved preferences restored.")
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.reloading = False
            self.set_controls_disabled(False)
