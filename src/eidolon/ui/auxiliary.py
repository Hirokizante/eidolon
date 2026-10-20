"""Auxiliary model fields embedded in Preferences."""
from dataclasses import dataclass, field

from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, Collapsible, Input, Select, Static

from eidolon.config import AppConfig, AuxiliaryModelConfig
from eidolon.providers.routing import resolve_auxiliary
from eidolon.ui.forms import FormBody
from eidolon.ui.models import ProviderMenuState


@dataclass
class AuxiliaryState:
    draft: dict | None = None
    picker: ProviderMenuState = field(default_factory=ProviderMenuState)
    context_confirmed: bool = False


ADVANCED = (
    ("context_chars", "Input allowance (characters)", "Blank: unlimited"),
    ("timeout_seconds", "Timeout (seconds, 1–120)", "60"),
    ("thinking_mode", "Thinking mode (optional)", "Blank uses model default; disabled / adaptive"),
)


class AuxiliaryForm(FormBody):
    DEFAULT_CSS = """
    AuxiliaryForm #aux-advanced { border: none; padding: 0; margin-top: 1; }
    AuxiliaryForm #aux-advanced > Contents { padding: 0 0 0 1; }
    AuxiliaryForm #aux-target { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    PreferencesScreen.narrow AuxiliaryForm #aux-advanced { margin-top: 0; }
    """

    def __init__(self, config: AppConfig, state: AuxiliaryState):
        super().__init__(id="preference-auxiliary", classes="form-body")
        self.config, self.state = config.model_copy(deep=True), state
        self.saved = config.auxiliary.model_dump()
        self.values = self.saved | (state.draft or {})
        self._picker_mode = self.values["mode"]

    def compose(self) -> ComposeResult:
        yield Select([("Follow dialogue model", "inherit"),
                      ("Choose a separate model", "separate"), ("Off", "disabled")],
                     value=self.values["mode"], allow_blank=False, compact=True, id="aux-mode")
        yield Static("", id="aux-target", markup=False)
        yield Button("Model settings", id="aux-choose", flat=True, compact=True)
        with Collapsible(title="Advanced request options", collapsed_symbol="+",
                         expanded_symbol="-", id="aux-advanced"):
            for name, label, placeholder in ADVANCED:
                yield Static(label, classes="field-label", markup=False)
                value = self.values[name]
                yield Input("" if value is None else str(value), id="aux-" + name,
                            placeholder=placeholder)
                yield Static("", id="aux-" + name + "-error", classes="field-error", markup=False)
            yield Static("Output-token field", classes="field-label")
            yield Select([("max_tokens", "max_tokens"),
                          ("max_completion_tokens", "max_completion_tokens")],
                         value=self.values["output_token_field"], allow_blank=False,
                         compact=True, id="aux-output_token_field")

    def on_mount(self) -> None:
        self.update_target()

    def update_target(self) -> None:
        mode = self.query_one("#aux-mode", Select).value
        if mode == "inherit":
            profile = self.config.selected_provider()
            text = f"Following dialogue: {profile.name} / {profile.model}"
        elif mode == "disabled":
            text = "Off · valid current-life notes and pending work are kept."
        else:
            text = f"Selected: {self.values['provider'] or '(choose connection)'} / {self.values['model'] or '(choose model)'}"
        self.query_one("#aux-target", Static).update(text)
        self.query_one("#aux-choose", Button).display = mode != "disabled"

    @on(Select.Changed, "#aux-mode")
    def mode_changed(self) -> None:
        if self.is_mounted:
            mode = self.query_one("#aux-mode", Select).value
            if mode != self._picker_mode:
                self.state.picker.models.clear()
                self.state.picker.model_provider = None
                self.state.context_confirmed = False
                self.values["context_window"] = None
                self._picker_mode = mode
            self.update_target()

    def read_values(self) -> dict:
        values = self.values | {"mode": self.query_one("#aux-mode", Select).value}
        for name, _, _ in ADVANCED:
            value = self.query_one("#aux-" + name, Input).value.strip()
            if value and name in {"context_chars", "timeout_seconds"}:
                try:
                    value = float(value) if name == "timeout_seconds" else int(value)
                except ValueError:
                    pass
            values[name] = value if value != "" else None
        values["output_token_field"] = self.query_one("#aux-output_token_field", Select).value
        return values

    def changes(self) -> dict:
        values = self.read_values()
        changes = {name: value for name, value in values.items() if value != self.saved[name]}
        # A new target may use the same number; preserve a re-entered override.
        if self.state.context_confirmed and any(key in changes for key in ("mode", "provider", "model")):
            changes["context_window"] = values["context_window"]
        return changes

    def remember_draft(self) -> None:
        self.state.draft = self.changes() or None

    def validated(self) -> AuxiliaryModelConfig:
        auxiliary = AuxiliaryModelConfig.model_validate(self.read_values())
        candidate = self.config.model_copy(deep=True)
        candidate.auxiliary = auxiliary
        resolve_auxiliary(candidate)
        return auxiliary

    def select_model(self, result: dict) -> None:
        fields = ["context_window", "max_output_tokens", "reasoning_effort"]
        if self.query_one("#aux-mode", Select).value == "separate":
            fields += ["provider", "model"]
        self.values.update({key: result[key] for key in fields})
        self.state.context_confirmed = result["context_window"] is not None
        self.update_target()

    def restore(self, config: AppConfig) -> None:
        self.config = config.model_copy(deep=True)
        self.saved = config.auxiliary.model_dump()
        self.values = self.saved.copy()
        self._picker_mode = self.values["mode"]
        self.state.draft = None
        self.state.context_confirmed = False
        self.state.picker.models.clear()
        self.state.picker.model_provider = None
        self.query_one("#aux-mode", Select).value = self.values["mode"]
        for name, _, _ in ADVANCED:
            value = self.values[name]
            self.query_one("#aux-" + name, Input).value = "" if value is None else str(value)
        self.query_one("#aux-output_token_field", Select).value = self.values["output_token_field"]
        self.update_target()
