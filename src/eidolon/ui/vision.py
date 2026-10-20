"""Optional vision analyst fields embedded in Preferences."""
from dataclasses import dataclass, field

from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, Collapsible, Input, Select, Static

from eidolon.config import AppConfig, VisionModelConfig
from eidolon.preferences import merge_vision_model
from eidolon.ui.forms import FormBody
from eidolon.ui.models import ProviderMenuState


@dataclass
class VisionState:
    draft: dict | None = None
    picker: ProviderMenuState = field(default_factory=ProviderMenuState)
    context_confirmed: bool = False


ADVANCED = (
    ("timeout_seconds", "Timeout (seconds, 1–120)", "60"),
    ("thinking_mode", "Thinking mode (optional)", "Blank uses model default; disabled / adaptive"),
)


class VisionForm(FormBody):
    DEFAULT_CSS = """
    VisionForm #vision-advanced { border: none; padding: 0; margin-top: 1; }
    VisionForm #vision-advanced > Contents { padding: 0 0 0 1; }
    VisionForm #vision-target { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    PreferencesScreen.narrow VisionForm #vision-advanced { margin-top: 0; }
    """

    def __init__(self, config: AppConfig, state: VisionState):
        super().__init__(id="preference-vision", classes="form-body")
        self.config, self.state = config.model_copy(deep=True), state
        self.saved = config.vision.model_dump()
        self.values = self.saved | (state.draft or {})
        self._picker_mode = self.values["mode"]

    def compose(self) -> ComposeResult:
        yield Select([("Use dialogue model vision", "disabled"),
                      ("Choose a separate vision model", "separate")],
                     value=self.values["mode"], allow_blank=False, compact=True, id="vision-mode")
        yield Static("", id="vision-target", markup=False)
        yield Button("Model settings", id="vision-choose", flat=True, compact=True)
        with Collapsible(title="Advanced request options", collapsed_symbol="+",
                         expanded_symbol="-", id="vision-advanced"):
            for name, label, placeholder in ADVANCED:
                yield Static(label, classes="field-label", markup=False)
                value = self.values[name]
                yield Input("" if value is None else str(value), id="vision-" + name,
                            placeholder=placeholder)
                yield Static("", id="vision-" + name + "-error", classes="field-error", markup=False)
            yield Static("Output-token field", classes="field-label")
            yield Select([("max_tokens", "max_tokens"),
                          ("max_completion_tokens", "max_completion_tokens")],
                         value=self.values["output_token_field"], allow_blank=False,
                         compact=True, id="vision-output_token_field")

    def on_mount(self) -> None:
        self.update_target()

    def update_target(self) -> None:
        separate = self.query_one("#vision-mode", Select).value == "separate"
        text = (f"Selected: {self.values['provider'] or '(choose connection)'} / "
                f"{self.values['model'] or '(choose model)'}" if separate
                else "Images go to the dialogue model when it supports vision.")
        self.query_one("#vision-target", Static).update(text)
        self.query_one("#vision-choose", Button).display = separate

    @on(Select.Changed, "#vision-mode")
    def mode_changed(self) -> None:
        if self.is_mounted:
            mode = self.query_one("#vision-mode", Select).value
            if mode != self._picker_mode:
                self.state.picker.models.clear()
                self.state.picker.model_provider = None
                self.state.context_confirmed = False
                self.values["context_window"] = None
                self._picker_mode = mode
            self.update_target()

    def read_values(self) -> dict:
        values = self.values | {"mode": self.query_one("#vision-mode", Select).value}
        for name, _, _ in ADVANCED:
            value = self.query_one("#vision-" + name, Input).value.strip()
            if value and name == "timeout_seconds":
                try:
                    value = float(value)
                except ValueError:
                    pass
            values[name] = value if value != "" else None
        values["output_token_field"] = self.query_one("#vision-output_token_field", Select).value
        return values

    def changes(self) -> dict:
        values = self.read_values()
        changes = {name: value for name, value in values.items() if value != self.saved[name]}
        if self.state.context_confirmed and any(key in changes for key in ("mode", "provider", "model")):
            changes["context_window"] = values["context_window"]
        return changes

    def remember_draft(self) -> None:
        self.state.draft = self.changes() or None

    def validated(self) -> VisionModelConfig:
        return merge_vision_model(self.config, self.read_values()).vision

    def select_model(self, result: dict) -> None:
        fields = ["provider", "model", "context_window", "max_output_tokens", "reasoning_effort"]
        self.values.update({key: result[key] for key in fields})
        self.state.context_confirmed = result["context_window"] is not None
        self.update_target()

    def restore(self, config: AppConfig) -> None:
        self.config = config.model_copy(deep=True)
        self.saved = config.vision.model_dump()
        self.values = self.saved.copy()
        self._picker_mode = self.values["mode"]
        self.state.draft = None
        self.state.context_confirmed = False
        self.state.picker.models.clear()
        self.state.picker.model_provider = None
        self.query_one("#vision-mode", Select).value = self.values["mode"]
        for name, _, _ in ADVANCED:
            value = self.values[name]
            self.query_one("#vision-" + name, Input).value = "" if value is None else str(value)
        self.query_one("#vision-output_token_field", Select).value = self.values["output_token_field"]
        self.update_target()
