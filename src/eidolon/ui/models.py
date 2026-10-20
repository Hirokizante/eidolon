"""Connection forms and model selection without leaving the conversation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    OptionList,
    Select,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from eidolon.config import (
    DEFAULT_MAX_RESPONSE_TOKENS, AppConfig, AuxiliaryModelConfig, ProviderConfig, VisionModelConfig,
)
from eidolon.ui.forms import FormBody, FormCheckbox, FormModal


NEW_PROVIDER = "__new_provider__"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
RESTART_NOTICE = (
    "The background service needs a restart. Run eidolon service stop, then eidolon service start."
)


@dataclass
class ModelDraft:
    model: str = ""
    effort: str = ""
    query: str = ""
    context_window: str = ""
    max_response_tokens: str = str(DEFAULT_MAX_RESPONSE_TOKENS)


@dataclass
class ProviderMenuState:
    """Keep uncommitted menu drafts in memory across dismissal."""

    connections: dict[str, dict] = dataclass_field(default_factory=dict)
    models: dict[str, ModelDraft] = dataclass_field(default_factory=dict)
    connection: str | None = None
    model_provider: str | None = None



def provider_label(provider: ProviderConfig) -> str:
    display = getattr(provider, "display_name", None)
    return f"{display} ({provider.name})" if display and display != provider.name else provider.name


def available_models(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value and value != "configure-model"))


def safe_notice(value: Any, *secrets: str) -> str:
    """Keep backend diagnostics bounded and mask a draft credential defensively."""
    message = str(value)
    if message == "Unknown service request":
        return RESTART_NOTICE
    for secret in secrets:
        if secret and not secret.startswith(("env:", "keyring:")):
            message = message.replace(secret, "[hidden]")
    message = re.sub(r"\bBearer\s+[^\s,;]+", "Bearer [hidden]", message, flags=re.IGNORECASE)
    return message[:350]


class ConnectScreen(FormModal):
    """Create or edit an OpenAI-compatible provider, with optional discovery."""

    BINDINGS = [Binding("ctrl+r", "refresh", "Refresh", priority=True)]

    DEFAULT_CSS = """
    ConnectScreen #connect-advanced { padding: 0; margin-top: 1; border: none; }
    ConnectScreen #connect-headers { height: 4; border: none; background: $background; }
    ConnectScreen #connect-headers:focus { background: $panel; }
    ConnectScreen Checkbox { height: auto; border: none; padding: 0; margin-top: 1; }
    ConnectScreen #connect-model-options { height: 4; border: none; background: $surface; }
    ConnectScreen #connect-model-options:focus { background: $panel; }
    ConnectScreen.short #connect-provider-label { display: none; }
    """

    def __init__(
        self,
        client,
        config: AppConfig,
        *,
        state: ProviderMenuState | None = None,
        provider_name: str | None = None,
        connection_only: bool = False,
    ) -> None:
        super().__init__()
        self.client = client
        self.connection_only = connection_only
        self.config = config.model_copy(deep=True)
        self.state = state if state is not None else ProviderMenuState()
        editing = provider_name or self.state.connection or config.provider
        self.editing = (
            editing
            if editing == NEW_PROVIDER or any(p.name == editing for p in config.providers)
            else config.provider
        )
        self.drafts = self.state.connections
        self.models: list[str] = []
        self.busy = False
        self.committing = False
        self._loading_draft = False
        self._headers_touched = False
        self._original_headers = "{}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Static("Auxiliary connection" if self.connection_only else "Connect a provider",
                         classes="form-title", markup=False)
            yield Static(
                "OpenAI-compatible Chat Completions and local servers.",
                classes="form-subtitle",
            )
            with FormBody(classes="form-body", id="connect-body"):
                yield Static("Connection", id="connect-provider-label", classes="field-label")
                yield Select(
                    [(provider_label(p), p.name) for p in self.config.providers]
                    + [("+ Add provider", NEW_PROVIDER)],
                    value=self.editing,
                    allow_blank=False,
                    compact=True,
                    id="connect-provider",
                )
                yield Static("Base URL (required)", classes="field-label")
                yield Input(id="connect-url", placeholder="https://api.example.com/v1")
                yield Static("", id="connect-url-error", classes="field-error", markup=False)
                yield Static("API key (optional for local servers)", classes="field-label")
                yield Input(
                    id="connect-key",
                    password=True,
                    placeholder="Blank keeps saved key; env:NAME / keyring:NAME",
                )
                yield Static("", id="connect-key-error", classes="field-error", markup=False)
                yield Static("Display name (optional)", classes="field-label")
                yield Input(id="connect-display", placeholder="A friendly name for the picker")
                yield Static("Fallback model ID (optional)", classes="field-label")
                yield Input(
                    id="connect-model", placeholder="Use when model discovery is unavailable"
                )
                yield OptionList(id="connect-model-options", compact=True)
                with Collapsible(
                    title="Advanced: headers, tools and context",
                    id="connect-advanced",
                    collapsed_symbol="+",
                    expanded_symbol="-",
                ):
                    yield Static(
                        "Connection ID (optional for new connections)", classes="field-label"
                    )
                    yield Input(id="connect-name", placeholder="Defaults to the URL hostname")
                    yield Static("", id="connect-name-error", classes="field-error", markup=False)
                    yield Static("Custom headers (JSON object)", classes="field-label")
                    yield TextArea("{}", id="connect-headers", soft_wrap=True, compact=True)
                    yield Static("", id="connect-headers-error", classes="field-error", markup=False)
                    yield Static(
                        "Sensitive header values must be env:NAME or keyring:NAME references.",
                        classes="field-help",
                        markup=False,
                    )
                    yield Static("Context window override (tokens)", classes="field-label")
                    yield Input(id="connect-context", placeholder="Blank uses automatic discovery")
                    yield Static("", id="connect-context-error", classes="field-error", markup=False)
                    yield Static("Token limit used for conversation history and the context meter.",
                                 classes="field-help", markup=False)
                    yield FormCheckbox(
                        "Enable tool calling", value=True, id="connect-tools", compact=True
                    )
                    yield FormCheckbox(
                        "Authorize credential headers for changed URL",
                        value=False,
                        id="connect-authorize-headers",
                        compact=True,
                    )
            yield Static(
                "Save fetches models; manual IDs also work.",
                id="connect-status",
                classes="form-status",
                markup=False,
            )
            with Horizontal(classes="form-actions"):
                yield Button("Save", id="connect-save", flat=True, compact=True)
                yield Button("Fetch models", id="connect-fetch", flat=True, compact=True)
                yield Button("Close", id="connect-cancel", flat=True, compact=True)
            yield Static(
                "Tab | Ctrl+S save | Ctrl+R fetch | Esc keeps draft", classes="form-hint"
            )
        yield Static(
            "Terminal too small — need 48×12. Resize or press Esc to close.",
            classes="form-too-small",
            markup=False,
        )

    def on_mount(self):
        self.resize_state()
        self.load_draft(self.editing)
        self.query_one("#connect-provider", Select).focus()

    def responsive_copy(self, width: int):
        self.query_one(".form-hint", Static).update(
            "Tab | ^S save | ^R fetch | Esc keeps draft"
            if width < 70
            else "Tab | Ctrl+S save | Ctrl+R fetch | Esc keeps draft"
        )

    def profile(self, name: str | None = None) -> ProviderConfig | None:
        return next((p for p in self.config.providers if p.name == (name or self.editing)), None)

    def draft(self) -> dict:
        return {
            "name": self.query_one("#connect-name", Input).value,
            "display_name": self.query_one("#connect-display", Input).value,
            "endpoint": self.query_one("#connect-url", Input).value,
            "api_key": self.query_one("#connect-key", Input).value,
            "model": self.query_one("#connect-model", Input).value,
            "context_window": self.query_one("#connect-context", Input).value,
            "headers": self.query_one("#connect-headers", TextArea).text,
            "tools": self.query_one("#connect-tools", Checkbox).value,
            "headers_touched": self._headers_touched,
            "authorize_headers": self.query_one("#connect-authorize-headers", Checkbox).value,
        }

    def remember_draft(self) -> None:
        self.drafts[self.editing] = self.draft()
        self.state.connection = self.editing

    def load_draft(self, name: str):
        self._loading_draft = True
        self.editing = name
        self.clear_errors()
        profile = self.profile()
        data = self.drafts.get(name) or {
            "name": profile.name if profile else "",
            "display_name": getattr(profile, "display_name", None) or "",
            "endpoint": profile.endpoint if profile else "",
            "api_key": "",
            "model": profile.model if profile and profile.model != "configure-model" else "",
            "context_window": str(profile.context_window) if profile and profile.context_window
                              else "",
            "headers": json.dumps(profile.headers if profile else {}, indent=2),
            "tools": profile.tools if profile else True,
            "headers_touched": False,
            "authorize_headers": False,
        }
        for field, key in (
            ("name", "name"),
            ("display", "display_name"),
            ("url", "endpoint"),
            ("key", "api_key"),
            ("model", "model"),
            ("context", "context_window"),
        ):
            self.query_one(f"#connect-{field}", Input).value = data[key]
        self.query_one("#connect-name", Input).disabled = profile is not None
        if self.connection_only and profile is not None:
            for control in ("#connect-model", "#connect-context", "#connect-tools"):
                self.query_one(control).disabled = True
        self.query_one("#connect-headers", TextArea).load_text(data["headers"])
        self._original_headers = json.dumps(profile.headers if profile else {}, indent=2)
        self._headers_touched = data["headers_touched"]
        self.query_one("#connect-tools", Checkbox).value = data["tools"]
        self.query_one("#connect-authorize-headers", Checkbox).value = data["authorize_headers"]
        self.query_one("#connect-key", Input).placeholder = (
            "Blank keeps saved key; env:NAME / keyring:NAME"
            if profile and profile.api_key_ref
            else "Optional for local APIs; env:NAME / keyring:NAME"
        )
        self.models = []
        self.query_one("#connect-model-options", OptionList).display = False
        self.status("Save fetches models; manual IDs also work.")
        self._loading_draft = False

    @on(Select.Changed, "#connect-provider")
    def change_connection(self, event: Select.Changed):
        if self._loading_draft or not isinstance(event.value, str) or event.value == self.editing:
            return
        self.remember_draft()
        self.load_draft(event.value)
        if event.value == NEW_PROVIDER:
            self.query_one("#connect-url", Input).focus()

    @on(TextArea.Changed, "#connect-headers")
    def headers_changed(self, event: TextArea.Changed):
        if not self._loading_draft and event.text_area.text != self._original_headers:
            self._headers_touched = True

    def payload(self) -> dict | None:
        self.clear_errors()
        data = self.draft()
        name, endpoint, key = (
            data["name"].strip(),
            data["endpoint"].strip(),
            data["api_key"].strip(),
        )
        if not name:
            try:
                hostname = urlsplit(endpoint).hostname or "provider"
            except ValueError:
                hostname = "provider"
            base = re.sub(r"[^a-z0-9]+", "-", hostname.casefold()).strip("-") or "provider"
            name, suffix = base, 2
            existing = {p.name for p in self.config.providers}
            while name in existing:
                name, suffix = f"{base}-{suffix}", suffix + 1
        if self.profile() is None and any(p.name == name for p in self.config.providers):
            self.field_error(
                "connect-name", "That provider ID already exists. Choose its connection to edit it."
            )
            return None
        if not endpoint:
            self.field_error(
                "connect-url", "Enter the API base URL."
            )
            return None
        profile = self.profile()
        if (
            profile
            and profile.api_key_ref
            and profile.endpoint.rstrip("/") != endpoint.rstrip("/")
            and not key
        ):
            self.field_error(
                "connect-key",
                "The URL changed. Re-enter the key or its reference to authorize this endpoint.",
            )
            return None
        try:
            headers = json.loads(data["headers"])
            if not isinstance(headers, dict) or any(
                not isinstance(v, str) for v in headers.values()
            ):
                raise ValueError("Custom headers must be a JSON object with string values.")
            changed_url = profile and profile.endpoint.rstrip("/") != endpoint.rstrip("/")
            has_saved_headers = profile and any(
                value.startswith(("env:", "keyring:")) for value in profile.headers.values()
            )
            has_credential_headers = any(
                value.startswith(("env:", "keyring:")) for value in headers.values()
            )
            confirm_headers = data["headers_touched"] and data["authorize_headers"]
            if changed_url and has_saved_headers and has_credential_headers and not confirm_headers:
                self.field_error(
                    "connect-headers",
                    "The URL changed. Clear credential headers, or reenter and authorize them in Advanced.",
                )
                return None
            settings = profile.model_dump() if profile else ProviderConfig().model_dump()
            settings.update(
                name=name,
                endpoint=endpoint,
                model=data["model"].strip(),
                headers=headers,
                tools=data["tools"],
                context_window=data["context_window"].strip() or None,
            )
            if "display_name" in ProviderConfig.model_fields:
                settings["display_name"] = data["display_name"].strip() or None
            if key.startswith(("env:", "keyring:")):
                settings["api_key_ref"] = key
            provider = ProviderConfig.model_validate(settings)
        except json.JSONDecodeError:
            self.field_error(
                "connect-headers", 'Custom headers must be valid JSON, for example {"X-App": "Eidolon"}.'
            )
            return None
        except ValidationError as exc:
            errors = exc.errors(include_input=False)
            fields = {"endpoint": "url", "headers": "headers", "api_key_ref": "key", "name": "name",
                      "context_window": "context"}
            for error in errors:
                field = fields.get(error["loc"][0])
                message = safe_notice(error["msg"], key)
                if field:
                    self.field_error(f"connect-{field}", message)
                else:
                    self.status(message, problem=True)
            return None
        except ValueError as exc:
            self.field_error("connect-headers", safe_notice(exc, key))
            return None
        result = {"provider": provider.model_dump(mode="json")}
        if self.connection_only:
            result["connection_only"] = True
        if key:
            result["api_key"] = key
        if confirm_headers:
            result["confirm_header_credentials"] = True
        return result

    def set_busy(self, busy: bool):
        self.busy = busy
        for widget in self.query("Input, Select, TextArea, Checkbox, OptionList"):
            widget.disabled = busy
        if not busy:
            self.query_one("#connect-name", Input).disabled = self.profile() is not None
            if self.connection_only and self.profile() is not None:
                for control in ("#connect-model", "#connect-context", "#connect-tools"):
                    self.query_one(control).disabled = True
        self.query_one("#connect-save", Button).disabled = busy
        self.query_one("#connect-fetch", Button).disabled = busy
        self.query_one("#connect-cancel", Button).disabled = self.committing

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "connect-save":
            self.action_save()
        elif event.button.id == "connect-fetch":
            self.action_refresh()
        elif event.button.id == "connect-cancel":
            self.action_close()

    @on(OptionList.OptionSelected, "#connect-model-options")
    def choose_discovered(self, event: OptionList.OptionSelected):
        self.query_one("#connect-model", Input).value = str(event.option.id)

    def action_save(self):
        if not self.busy and (payload := self.payload()):
            self.committing = True
            self.set_busy(True)
            self.save_connection(payload)

    def action_refresh(self):
        if not self.busy and (payload := self.payload()):
            self.set_busy(True)
            self.fetch_models(payload)

    @on(Input.Submitted)
    def next_field(self, event: Input.Submitted):
        event.stop()
        self.focus_next()

    @work(exclusive=True, group="connection-operation", exit_on_error=False)
    async def fetch_models(self, payload: dict):
        self.set_busy(True)
        self.status("Fetching available models… Esc cancels.")
        try:
            data = await self.client.request("discover_models", **payload)
            self.models = available_models(data.get("models", []))
            choices = self.query_one("#connect-model-options", OptionList)
            choices.clear_options()
            choices.add_options(
                Option(Text(model, no_wrap=True, overflow="ellipsis"), id=model)
                for model in self.models
            )
            choices.display = bool(self.models)
            if self.models:
                choices.highlighted = 0
                field = self.query_one("#connect-model", Input)
                if not field.value.strip():
                    field.value = self.models[0]
                self.status(
                    f"Found {len(self.models)} models. Choose one below the fallback model field."
                )
            elif data.get("error"):
                self.status(
                    "Enter a fallback model ID to continue. "
                    + safe_notice(data["error"], payload.get("api_key", "")),
                    problem=True,
                )
            else:
                self.status("No models were listed. Enter a fallback model ID to continue.")
        except Exception as exc:
            self.status(
                "You can still enter a fallback model ID. "
                + safe_notice(exc, payload.get("api_key", "")),
                problem=True,
            )
        finally:
            if self.is_mounted:
                self.set_busy(False)

    @work(exclusive=True, group="connection-operation", exit_on_error=False)
    async def save_connection(self, payload: dict):
        self.committing = True
        self.set_busy(True)
        self.status("Saving connection and fetching models…")
        try:
            data = await self.client.request("save_provider", **payload)
            name = payload["provider"]["name"]
            notice = f"Saved {payload['provider'].get('display_name') or name}."
            if data.get("warning"):
                notice += " " + safe_notice(data["warning"], payload.get("api_key", ""))
            elif data.get("models"):
                notice += f" Found {len(data['models'])} models."
            self.drafts.pop(self.editing, None)
            self.state.connection = name
            self._committed = True
            self.dismiss(
                {
                    "config": data["config"],
                    "notice": notice,
                    "provider": name,
                    "warning": safe_notice(data["warning"], payload.get("api_key", ""))
                    if data.get("warning") else None,
                }
            )
        except Exception as exc:
            self.status(safe_notice(exc, payload.get("api_key", "")), problem=True)
        finally:
            self.committing = False
            if self.is_mounted:
                self.set_busy(False)


class ModelScreen(FormModal):
    """A searchable model picker with dialogue reasoning and token settings."""

    BINDINGS = [Binding("ctrl+r", "refresh", "Refresh", priority=True)]

    DEFAULT_CSS = """
    ModelScreen #model-options { height: 7; min-height: 3; border: none; background: $surface; }
    ModelScreen #model-options:focus { background: $panel; }
    ModelScreen #model-filter { margin-top: 1; }
    ModelScreen #model-help { margin-top: 1; height: auto; }
    ModelScreen.compact #model-options { height: 5; }
    ModelScreen.compact .field-label { margin-top: 0; }
    """

    def __init__(
        self,
        client,
        config: AppConfig,
        provider_name: str | None = None,
        *,
        state: ProviderMenuState | None = None,
        role: str = "dialogue",
        model: str | None = None,
        effort: str | None = None,
        context_window: int | None = None,
        max_response_tokens: int | None = None,
        follow_dialogue: bool = False,
    ) -> None:
        super().__init__()
        self.client = client
        self.role = role
        self.separate_role = role in {"auxiliary", "vision"}
        self.follow_dialogue = role == "auxiliary" and follow_dialogue
        self._role_effort = effort
        self._role_response = (max_response_tokens if max_response_tokens is not None
                               else getattr(config, role).max_output_tokens
                               if self.separate_role else DEFAULT_MAX_RESPONSE_TOKENS)
        self.config = config.model_copy(deep=True)
        self.state = state if state is not None else ProviderMenuState()
        self.provider_name = provider_name or self.state.model_provider or config.provider
        if not any(p.name == self.provider_name for p in self.config.providers):
            self.provider_name = config.provider
        self.models: list[str] = []
        self.busy = False
        self.committing = False
        self.drafts = self.state.models
        self._loading_provider = False
        self._context_model = ""
        if self.separate_role:
            self.drafts.setdefault(self.provider_name, ModelDraft(
                model=model or "", effort=effort or "",
                context_window=str(context_window) if context_window is not None else "",
                max_response_tokens=str(self._role_response),
            ))

    def profile(self) -> ProviderConfig:
        return next(p for p in self.config.providers if p.name == self.provider_name)

    def compose(self) -> ComposeResult:
        active = self.config.selected_provider()
        with Vertical(classes="form-dialog"):
            yield Static(f"{self.role.capitalize()} model settings", classes="form-title",
                         markup=False)
            yield Static(
                ("Image analysis · selection returns to vision settings" if self.role == "vision"
                 else "Background tasks · selection returns to auxiliary settings")
                if self.separate_role else f"Current: {provider_label(active)} / {active.model}",
                classes="form-subtitle",
                markup=False,
            )
            with FormBody(classes="form-body", id="model-body"):
                yield Static("Provider", classes="field-label")
                yield Select(
                    [(provider_label(p), p.name) for p in self.config.providers],
                    value=self.provider_name,
                    allow_blank=False,
                    compact=True,
                    id="model-provider",
                )
                yield Input(
                    placeholder="Search models (arrows choose, Enter selects)", id="model-filter"
                )
                yield OptionList(id="model-options", compact=True)
                yield Static("Model ID (required)", classes="field-label")
                yield Input(id="model-id", placeholder="Exact model ID from your provider")
                yield Static("", id="model-id-error", classes="field-error", markup=False)
                yield Static("Reasoning effort", classes="field-label")
                yield Select(
                    self.effort_options(),
                    value="",
                    allow_blank=False,
                    compact=True,
                    id="model-effort",
                )
                yield Static(
                    "Follows dialogue; uses low effort when configured."
                    if self.follow_dialogue else "API support varies. Default is safest (no override).",
                    classes="field-help",
                    id="model-help",
                    markup=False,
                )
                yield Static("Context Size (tokens)", classes="field-label")
                yield Input(id="model-context-size", placeholder=(
                    "Blank follows dialogue context window" if self.follow_dialogue
                    else "Blank uses discovered context window"
                ))
                yield Static("", id="model-context-size-error", classes="field-error", markup=False)
                yield Static("Max Response Length (tokens)", classes="field-label")
                yield Input(id="model-max-response", placeholder=str(
                    self._role_response,
                ))
                yield Static("", id="model-max-response-error", classes="field-error", markup=False)
            yield Static(
                "Loading available models…",
                id="model-status",
                classes="form-status",
                markup=False,
            )
            with Horizontal(classes="form-actions"):
                yield Button("Apply", id="model-apply", flat=True, compact=True)
                yield Button("Refresh", id="model-refresh", flat=True, compact=True)
                yield Button("Connect", id="model-connect", flat=True, compact=True)
                yield Button("Close", id="model-cancel", flat=True, compact=True)
            yield Static(
                "Tab | Ctrl+S apply | Ctrl+R refresh | Esc keeps draft", classes="form-hint"
            )
        yield Static(
            "Terminal too small — need 48×12. Resize or press Esc to close.",
            classes="form-too-small",
            markup=False,
        )

    def on_mount(self):
        self.resize_state()
        self.load_provider(self.provider_name)
        if self.follow_dialogue:
            self.query("#model-provider, #model-filter, #model-options, #model-id, "
                       "#model-effort, #model-connect").set(disabled=True)
        self.query_one("#model-context-size" if self.follow_dialogue else "#model-filter", Input).focus()

    def responsive_copy(self, width: int):
        self.query_one(".form-hint", Static).update(
            "Tab | ^S apply | ^R refresh | Esc keeps draft"
            if width < 70
            else "Tab | Ctrl+S apply | Ctrl+R refresh | Esc keeps draft"
        )

    def reveal_focus(self) -> None:
        super().reveal_focus()
        if self.has_class("short"):
            fallback = f"{self.role.capitalize()} model settings"
            title = {"model-context-size": "Context Size (tokens)",
                     "model-max-response": "Max Response Length (tokens)"}.get(
                         self.focused.id if self.focused else None, fallback,
                     )
            for dialog in self.query(".form-dialog"):
                dialog.border_title = title

    def effort_options(self) -> list[tuple[str, str]]:
        effort = self.profile().parameters.get("reasoning_effort")
        options = [("Default (provider / model decides)", "")] + [
            (e.capitalize(), e) for e in EFFORTS
        ]
        if isinstance(effort, str) and effort and effort not in EFFORTS:
            options.append((f"Saved custom effort: {effort}", effort))
        return options

    def load_provider(self, name: str):
        self._loading_provider = True
        self.provider_name = name
        self.clear_errors()
        profile = self.profile()
        limit = profile.parameters.get(profile.output_token_parameter())
        separate = self.separate_role
        default_effort = (self._role_effort or "") if separate else profile.parameters.get("reasoning_effort") or ""
        default_context = str(profile.context_window) if not separate and profile.context_window is not None else ""
        default_response = self._role_response if separate else (
            limit if type(limit) is int and limit > 0 else DEFAULT_MAX_RESPONSE_TOKENS
        )
        draft = self.drafts.get(
            name,
            ModelDraft(
                model=profile.model if profile.model != "configure-model" else "",
                effort=default_effort, context_window=default_context,
                max_response_tokens=str(default_response),
            ),
        )
        self._context_model = draft.model.strip()
        self.query_one("#model-id", Input).value = draft.model
        selector = self.query_one("#model-effort", Select)
        options = self.effort_options()
        if draft.effort and draft.effort not in {value for _, value in options}:
            options.append((f"Saved custom effort: {draft.effort}", draft.effort))
        selector.set_options(options)
        if self.follow_dialogue:
            selector.value = "low" if "reasoning_effort" in profile.parameters else ""
        else:
            selector.value = draft.effort
        self.query_one("#model-context-size", Input).value = draft.context_window
        self.query_one("#model-max-response", Input).value = draft.max_response_tokens
        self.query_one("#model-filter", Input).value = draft.query
        self.models = available_models(getattr(profile, "models", []))
        self.filter_models(draft.query)
        self._loading_provider = False
        self.refresh_models(name)

    def remember_draft(self) -> None:
        effort = self.query_one("#model-effort", Select).value
        self.drafts[self.provider_name] = ModelDraft(
            model=self.query_one("#model-id", Input).value,
            effort=effort if isinstance(effort, str) else "",
            query=self.query_one("#model-filter", Input).value,
            context_window=self.query_one("#model-context-size", Input).value,
            max_response_tokens=self.query_one("#model-max-response", Input).value,
        )
        self.state.model_provider = self.provider_name

    @on(Select.Changed, "#model-provider")
    def provider_changed(self, event: Select.Changed):
        if (
            self._loading_provider
            or not isinstance(event.value, str)
            or event.value == self.provider_name
        ):
            return
        self.remember_draft()
        self.load_provider(event.value)

    def filter_models(self, query: str):
        saved = self.profile().model
        models = self.models.copy()
        if saved and saved != "configure-model" and saved not in models:
            models.insert(0, saved)
        words = query.casefold().split()
        matches = [model for model in models if all(w in model.casefold() for w in words)]
        options = self.query_one("#model-options", OptionList)
        highlighted = (
            options.get_option_at_index(options.highlighted).id
            if options.highlighted is not None and options.option_count
            else None
        )
        options.clear_options()
        options.styles.height = max(3, min(5 if self.has_class("compact") else 7, len(matches)))
        options.add_options(
            Option(
                Text(
                    model + (" (saved)" if model == saved else ""),
                    no_wrap=True,
                    overflow="ellipsis",
                ),
                id=model,
            )
            for model in matches
        )
        selected = highlighted or self.query_one("#model-id", Input).value
        options.highlighted = (
            matches.index(selected) if selected in matches else 0 if matches else None
        )
        if not matches:
            options.add_option(Option("No matching models. Enter a model ID below.", disabled=True))

    @on(Input.Changed, "#model-filter")
    def filter_changed(self, event: Input.Changed):
        if not self._loading_provider:
            self.filter_models(event.value)

    @on(Input.Submitted, "#model-filter")
    def choose_filtered(self):
        options = self.query_one("#model-options", OptionList)
        if options.highlighted is not None:
            option = options.get_option_at_index(options.highlighted)
            if not option.disabled and option.id:
                self.choose_model(str(option.id))

    @on(Input.Submitted, "#model-id")
    def review_effort(self, event: Input.Submitted):
        event.stop()
        self.query_one("#model-effort", Select).focus()

    @on(Input.Changed, "#model-id")
    def model_id_changed(self, event: Input.Changed) -> None:
        if (self._loading_provider
                or event.value != self.query_one("#model-id", Input).value):
            return
        if event.value.strip() != self._context_model:
            self._context_model = event.value.strip()
            self.query_one("#model-context-size", Input).value = ""

    @on(OptionList.OptionSelected, "#model-options")
    def model_chosen(self, event: OptionList.OptionSelected):
        self.choose_model(str(event.option.id))

    def choose_model(self, model: str):
        self.query_one("#model-id", Input).value = model
        self.query_one("#model-effort", Select).focus()

    def on_key(self, event: Key):
        if self.focused is self.query_one("#model-filter", Input) and event.key in {"up", "down"}:
            options = self.query_one("#model-options", OptionList)
            (options.action_cursor_down if event.key == "down" else options.action_cursor_up)()
            event.stop()
            event.prevent_default()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "model-apply":
            self.action_save()
        elif event.button.id == "model-refresh":
            self.action_refresh()
        elif event.button.id == "model-connect":
            self.connect_provider()
        elif event.button.id == "model-cancel":
            self.action_close()

    def action_refresh(self):
        if not self.busy:
            self.refresh_models(self.provider_name)

    @work(exclusive=True, group="model-discovery", exit_on_error=False)
    async def refresh_models(self, name: str):
        # Widget.loading would cover the entire modal with a LoadingIndicator.
        # Keep cached choices and controls visible while discovery is pending.
        self.status("Refreshing the followed dialogue model…" if self.follow_dialogue
                    else "Fetching available models… You can still enter a model ID.")
        try:
            params = {"name": name}
            if self.separate_role and self.query_one("#model-id", Input).value.strip():
                params["model"] = self.query_one("#model-id", Input).value.strip()
            data = await self.client.request("list_models", **params)
            if name != self.provider_name:
                return
            self.models = available_models(data.get("models", []))
            self.filter_models(self.query_one("#model-filter", Input).value)
            if data.get("error"):
                self.status(
                    ("Following dialogue model. " if self.follow_dialogue
                     else "Use a saved or manual model ID. ") + safe_notice(data["error"]), problem=True
                )
            elif self.follow_dialogue:
                self.status("Following dialogue model. Edit auxiliary limits, then Apply.")
            elif self.models:
                self.status(f"{len(self.models)} models available. Choose a model, then Apply.")
            else:
                self.status("No models were listed. Use a saved or manual model ID.")
        except Exception as exc:
            self.status(("Following dialogue model. " if self.follow_dialogue
                         else "Use a saved or manual model ID. ") + safe_notice(exc), problem=True)

    def action_save(self):
        if self.busy:
            return
        self.clear_errors()
        model = self.query_one("#model-id", Input).value.strip()
        if not model:
            self.field_error("model-id", "Choose or enter an exact model ID.")
            return
        effort = self.query_one("#model-effort", Select).value
        limits = {}
        for field, key in (("model-context-size", "context_window"),
                           ("model-max-response", "max_response_tokens")):
            value = self.query_one("#" + field, Input).value.strip()
            if not value and key == "context_window":
                limits[key] = None
                continue
            try:
                tokens = int(value)
            except ValueError:
                tokens = 0
            if tokens <= 0:
                self.field_error(field, "Enter a positive token count.")
                return
            limits[key] = tokens
        if self.separate_role:
            try:
                model_config = VisionModelConfig if self.role == "vision" else AuxiliaryModelConfig
                role = model_config(
                    model=model, context_window=limits["context_window"],
                    max_output_tokens=limits["max_response_tokens"],
                    reasoning_effort=self._role_effort if self.follow_dialogue else effort or None,
                )
            except ValidationError as exc:
                error = exc.errors(include_input=False, include_url=False)[0]
                field = {"max_output_tokens": "model-max-response", "context_window": "model-context-size",
                         "model": "model-id"}.get(error["loc"][0])
                if field:
                    self.field_error(field, error["msg"])
                else:
                    self.status(error["msg"], problem=True)
                return
            self.drafts.pop(self.provider_name, None)
            self._committed = True
            self.dismiss({"provider": self.provider_name, "model": model,
                          "reasoning_effort": role.reasoning_effort,
                          "context_window": role.context_window,
                          "max_output_tokens": role.max_output_tokens,
                          "config": self.config.model_dump()})
            return
        self.busy = True
        self.committing = True
        self.apply_model(
            self.provider_name, model, effort if isinstance(effort, str) and effort else None,
            limits["context_window"], limits["max_response_tokens"],
        )

    @work(exclusive=True, group="model-selection", exit_on_error=False)
    async def apply_model(self, name: str, model: str, effort: str | None,
                          context_window: int | None, max_response_tokens: int) -> None:
        self.busy = True
        self.committing = True
        self.workers.cancel_group(self, "model-discovery")
        self.status("Applying model settings…")
        for widget in self.query("Input, Select, Button, OptionList"):
            widget.disabled = True
        try:
            data = await self.client.request(
                "select_model", name=name, model=model, reasoning_effort=effort,
                context_window=context_window, max_response_tokens=max_response_tokens,
            )
            self.drafts.pop(name, None)
            self.state.model_provider = name
            self._committed = True
            self.dismiss(
                {
                    "config": data,
                    "notice": f"Saved {model} · reasoning: {effort or 'default'} · applies to next response.",
                }
            )
        except Exception as exc:
            self.status(safe_notice(exc), problem=True)
        finally:
            self.busy = False
            self.committing = False
            if self.is_mounted:
                for widget in self.query("Input, Select, Button, OptionList"):
                    widget.disabled = False

    @work(exclusive=True, group="model-connect", exit_on_error=False)
    async def connect_provider(self):
        if self.busy:
            return
        self.remember_draft()
        connection_config = self.config.model_copy(deep=True)
        connection_config.provider = self.provider_name
        self.query_one(".form-dialog").display = False
        try:
            result = await self.app.push_screen_wait(
                ConnectScreen(
                    self.client, connection_config, state=self.state, provider_name=self.provider_name,
                    connection_only=self.separate_role,
                )
            )
        finally:
            self.query_one(".form-dialog").display = True
            self.resize_state()
        if result is None:
            return
        self.config = AppConfig.model_validate(result["config"])
        selector = self.query_one("#model-provider", Select)
        self._loading_provider = True
        selector.set_options([(provider_label(p), p.name) for p in self.config.providers])
        selector.value = result.get("provider", self.provider_name)
        self._loading_provider = False
        self.load_provider(str(selector.value))
        self.status(result["notice"])
