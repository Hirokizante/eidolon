"""App-owned configuration and credential references. No migration-source reads here."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator,
)


PROMPT_NAMES = ("SYSTEM", "SOUL", "POST_HISTORY", "USER")
OUTPUT_TOKEN_FIELDS = ("max_completion_tokens", "max_tokens", "max_output_tokens")
DEFAULT_MAX_RESPONSE_TOKENS = 32_768

# Successful Keychain reads are shared by every adapter in this process. Hold
# the lock across a first read so concurrent discovery/chat/lifecycle requests
# cannot each open an authorization dialog. Nothing here is persisted to disk.
_keychain_values: dict[str, str] = {}
_keychain_lock = threading.RLock()


def validate_secret_reference(value: str) -> str:
    if value.startswith("env:") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value[4:]):
        return value
    if (value.startswith("keyring:") and value[8:].strip()
            and value[8:] == value[8:].strip() and not any(c in value for c in "\r\n\0")):
        return value
    raise ValueError("Credentials must use env:NAME or keyring:NAME references")


SecretReference = Annotated[str, AfterValidator(validate_secret_reference)]


def validate_settings_credentials(values: dict[str, str]) -> dict[str, str]:
    for name, value in values.items():
        sensitive = re.search(
            r"(?:^|[_-])(?:authorization|cookie|key|token|secret|password|passwd|credential)s?(?:$|[_-])",
            name, re.IGNORECASE,
        )
        if sensitive or value.startswith(("env:", "keyring:")):
            validate_secret_reference(value)
    return values


def validate_endpoint(value: str) -> str:
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("endpoint must be an HTTP(S) URL without embedded credentials")
    return value.rstrip("/")


def default_state_dir() -> Path:
    override = os.environ.get("EIDOLON_HOME")
    return Path(override).expanduser() if override else Path.home() / "Library/Application Support/Eidolon"


def system_timezone() -> str:
    """Resolve an IANA zone without treating an ambiguous abbreviation as a zone."""
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        localtime = str(Path("/etc/localtime").resolve(strict=True))
        if "/zoneinfo/" in localtime:
            candidates.append(localtime.split("/zoneinfo/", 1)[1])
    except (OSError, RuntimeError):
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except (OSError, UnicodeError):
        pass
    candidates.append(getattr(datetime.now().astimezone().tzinfo, "key", ""))
    for candidate in candidates:
        if not candidate or candidate.startswith("/"):
            continue
        try:
            ZoneInfo(candidate)
        except (KeyError, ValueError):
            continue
        return candidate
    return "UTC"


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, hide_input_in_errors=True)


class IdentityConfig(SettingsModel):
    user_name: str = Field(default="You", strict=True)
    companion_name: str = Field(default="Companion", strict=True)

    @field_validator("user_name", "companion_name")
    @classmethod
    def valid_display_name(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Name must be valid UTF-8") from None
        # Check the original input so stripping cannot hide forbidden controls.
        if any(
            unicodedata.category(char) in {"Cc", "Zl", "Zp"}
            or "\u202a" <= char <= "\u202e" or "\u2066" <= char <= "\u2069"
            for char in value
        ):
            raise ValueError("Name must not contain line breaks, control characters, or bidi controls")
        value = value.strip()
        if not 1 <= len(value) <= 80:
            raise ValueError("Name must contain 1–80 Unicode code points after trimming spaces")
        return value


class ModelMetadata(SettingsModel):
    context_window: int | None = Field(default=None, gt=0)
    vision: bool | None = Field(default=None, strict=True)
    source: str
    updated_at: float = Field(ge=0, allow_inf_nan=False)


class ProviderConfig(SettingsModel):
    name: str = "local"
    display_name: str | None = None
    endpoint: str = "http://127.0.0.1:1234/v1"
    model: str = "configure-model"
    models: list[str] = Field(default_factory=list)
    api_key_ref: SecretReference | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    tools: bool = True
    presentation: Literal["plain", "prefix", "json", "auto"] = "auto"
    structured_verified: bool = False
    # Explicit override for compatible endpoints without capability metadata.
    vision: bool | None = Field(default=None, strict=True)
    post_history: Literal["system", "user"] = "system"
    context_window: int | None = Field(default=None, gt=0)
    model_metadata: dict[str, ModelMetadata] = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def retire_character_budget(cls, values: Any) -> Any:
        # Old files and older connection forms may carry this independent cap.
        # Do not convert it into a token override: discovery must remain active.
        if isinstance(values, dict) and "context_chars" in values:
            values = {key: value for key, value in values.items() if key != "context_chars"}
        return values

    _validate_headers = field_validator("headers")(validate_settings_credentials)

    _validate_endpoint = field_validator("endpoint")(validate_endpoint)

    def context_window_for(self, model: str | None = None) -> int | None:
        model = model or self.model
        if model == self.model and self.context_window is not None:
            return self.context_window
        metadata = self.model_metadata.get(model)
        return metadata.context_window if metadata else None

    def output_token_parameter(self) -> str:
        return next((key for key in OUTPUT_TOKEN_FIELDS if key in self.parameters),
                    "max_completion_tokens")

    @field_validator("display_name")
    @classmethod
    def optional_display_name(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator("models")
    @classmethod
    def unique_models(cls, value: list[str]) -> list[str]:
        return sorted({model for model in value if model.strip()})


class MemoryConfig(SettingsModel):
    enabled: bool = False
    save_enabled: bool = False
    continuity_enabled: bool = False
    continuity_use_enabled: bool = False
    continuity_max_notes: int = Field(default=12, ge=1, le=64, strict=True)
    continuity_batch_exchanges: int = Field(default=8, ge=1, le=32)
    continuity_idle_seconds: int = Field(default=600, ge=1, le=86400)
    url: str = "http://127.0.0.1:8888"
    bank: str = ""
    api_key_ref: SecretReference | None = None
    retain_enabled: bool = False
    verified: bool = False
    timeout: float = Field(default=20, gt=0, le=120)
    recall_budget: str = "low"
    max_memories: int = Field(default=12, ge=1, le=50)

    _validate_endpoint = field_validator("url")(validate_endpoint)

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_opt_out(cls, value: Any) -> Any:
        # The old switch disabled both recall and delivery. Do not start learning
        # for users who disabled it before saving became an independent choice.
        if isinstance(value, dict) and "save_enabled" not in value:
            enabled = value.get("enabled", True)
            # Match the false boolean spellings accepted by Pydantic as well
            # as YAML/JSON's native booleans.
            if enabled is False or enabled == 0 or (
                isinstance(enabled, str) and enabled.lower() in {"false", "off", "no", "0", "f", "n"}
            ):
                return {**value, "save_enabled": False}
        return value


class AuxiliaryModelConfig(SettingsModel):
    """App-owned inference; connection credentials stay in providers[]."""

    mode: Literal["inherit", "separate", "disabled"] = "inherit"
    provider: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=500)
    context_chars: int | None = Field(default=None, ge=8_000, le=1_000_000)
    context_window: int | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(default=60.0, ge=1, le=120, allow_inf_nan=False)
    max_output_tokens: int = Field(default=4096, ge=128, le=8192)
    output_token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_effort: str | None = Field(default=None, max_length=80)
    thinking_mode: Literal["adaptive", "disabled"] | None = None

    @field_validator("provider", "model", "reasoning_effort")
    @classmethod
    def bounded_identifier(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("Enter a nonempty identifier without control characters")
        return value

    # Binding completeness and provider references are checked by the resolver
    # and save action, so an externally broken auxiliary route cannot stop chat.


class VisionModelConfig(SettingsModel):
    """Optional image analyst; credentials belong to the saved provider connection."""

    mode: Literal["disabled", "separate"] = "disabled"
    provider: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=500)
    context_window: int | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(default=60.0, ge=1, le=120, allow_inf_nan=False)
    max_output_tokens: int = Field(default=4096, ge=128, le=8192)
    output_token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_effort: str | None = Field(default=None, max_length=80)
    thinking_mode: Literal["adaptive", "disabled"] | None = None

    _bounded_identifier = field_validator("provider", "model", "reasoning_effort")(
        AuxiliaryModelConfig.bounded_identifier.__func__
    )


class SearchProviderConfig(SettingsModel):
    api_key_ref: SecretReference | None = None
    max_results: int = Field(default=5, ge=1, le=20)
    timeout_seconds: float = Field(default=15, ge=1, le=120, allow_inf_nan=False)
    max_content_chars: int = Field(default=3000, ge=100, le=20_000)


def validate_search_list(values: list[str]) -> list[str]:
    if any(not value.strip() or any(c in value for c in "\r\n\0") for value in values):
        raise ValueError("Enter comma-separated values without empty items or control characters")
    return list(dict.fromkeys(value.strip() for value in values))


def validate_search_date(value: str | None) -> str | None:
    if value is not None:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("Enter a date as YYYY-MM-DD") from None
    return value


SearchDate = Annotated[str, AfterValidator(validate_search_date)]
SearchList = Annotated[list[str], AfterValidator(validate_search_list)]


class DatedSearchConfig(SearchProviderConfig):
    start_date: SearchDate | None = None
    end_date: SearchDate | None = None

    @field_validator("end_date")
    @classmethod
    def ordered_dates(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value and info.data.get("start_date") and value < info.data["start_date"]:
            raise ValueError("End date must be on or after the start date")
        return value


class TavilyConfig(DatedSearchConfig):
    depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic"
    topic: Literal["general", "news", "finance"] = "general"
    time_range: Literal["day", "week", "month", "year"] | None = None
    include_domains: SearchList = Field(default_factory=list, max_length=300)
    exclude_domains: SearchList = Field(default_factory=list, max_length=150)
    chunks_per_source: int = Field(default=3, ge=1, le=3)
    include_published_date: bool = True
    exact_match: bool = False
    safe_search: bool = False

    @model_validator(mode="after")
    def supported_safe_search(self):
        if self.safe_search and self.depth in {"fast", "ultra-fast"}:
            raise ValueError("Tavily safe search requires basic or advanced depth")
        return self


class BraveSearchConfig(SearchProviderConfig):
    country: str = Field(default="ALL", pattern=r"^(?:[A-Za-z]{2}|ALL)$")
    search_lang: str = Field(default="en", min_length=2, max_length=20,
                             pattern=r"^[A-Za-z-]+$")
    safesearch: Literal["off", "moderate", "strict"] = "moderate"
    freshness: str | None = None
    extra_snippets: bool = True
    goggles: SearchList = Field(default_factory=list, max_length=10)

    @field_validator("freshness")
    @classmethod
    def valid_freshness(cls, value: str | None) -> str | None:
        if value is None or value in {"pd", "pw", "pm", "py"}:
            return value
        dates = value.split("to")
        if len(dates) != 2:
            raise ValueError("Use pd, pw, pm, py, or YYYY-MM-DDtoYYYY-MM-DD")
        start, end = (validate_search_date(part) for part in dates)
        if start > end:
            raise ValueError("Freshness end date must be on or after its start date")
        return value


class ExaSearchConfig(DatedSearchConfig):
    search_type: Literal["auto", "instant", "fast", "deep-lite", "deep", "deep-reasoning"] = "auto"
    category: Literal["news", "publication", "company", "people", "personal site",
                      "financial report"] | None = None
    include_domains: SearchList = Field(default_factory=list, max_length=1200)
    exclude_domains: SearchList = Field(default_factory=list, max_length=1200)
    content_mode: Literal["highlights", "text"] = "highlights"

    @model_validator(mode="after")
    def supported_filters(self):
        if self.category in {"company", "people"} and (self.start_date or self.end_date):
            raise ValueError("Exa company and people searches do not support publication dates")
        if self.category == "people" and self.exclude_domains:
            raise ValueError("Exa people searches do not support excluded domains")
        return self


class PerplexitySearchConfig(DatedSearchConfig):
    search_type: Literal["web", "fast"] = "web"
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    language_filter: SearchList = Field(default_factory=list, max_length=20)
    domain_filter: SearchList = Field(default_factory=list, max_length=20)
    recency: Literal["hour", "day", "week", "month", "year"] | None = None
    updated_after: SearchDate | None = None
    updated_before: SearchDate | None = None
    max_tokens: int = Field(default=5000, ge=1, le=1_000_000)
    max_tokens_per_page: int = Field(default=1024, ge=1, le=1_000_000)

    @field_validator("language_filter")
    @classmethod
    def language_codes(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z]{2}", value) for value in values):
            raise ValueError("Use two-letter language codes, for example en, fr")
        return values

    @field_validator("domain_filter")
    @classmethod
    def consistent_domains(cls, values: list[str]) -> list[str]:
        if any(len(value) > 253 for value in values):
            raise ValueError("Each domain must contain at most 253 characters")
        if values and any(value.startswith("-") for value in values) and not all(
            value.startswith("-") for value in values
        ):
            raise ValueError("Use either included domains or excluded domains prefixed with -")
        return values

    @field_validator("updated_before")
    @classmethod
    def ordered_updates(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value and info.data.get("updated_after") and value < info.data["updated_after"]:
            raise ValueError("Updated-before date must be on or after updated-after date")
        return value


class SearXNGConfig(SearchProviderConfig):
    base_url: str = "http://127.0.0.1:8080"
    engines: SearchList = Field(default_factory=list, max_length=100)
    categories: SearchList = Field(default_factory=list, max_length=50)
    language: str | None = Field(default=None, max_length=30, pattern=r"^[A-Za-z_-]+$")
    time_range: Literal["day", "month", "year"] | None = None
    safesearch: Literal[0, 1, 2] = 0

    @field_validator("base_url")
    @classmethod
    def instance_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        value = validate_endpoint(value)
        parsed = urlsplit(value)
        # Accessing .port validates numeric ports and their range.
        parsed.port
        if any(character.isspace() for character in value):
            raise ValueError("Use an instance base URL without whitespace")
        if parsed.query or parsed.fragment:
            raise ValueError("Use an instance base URL without a query or fragment")
        return value


SearchProvider = Literal["tavily", "brave", "exa", "perplexity", "searxng"]
SEARCH_PROVIDERS = {"tavily": "Tavily", "brave": "Brave Search", "exa": "Exa",
                    "perplexity": "Perplexity", "searxng": "SearXNG"}


class WebSearchConfig(SettingsModel):
    provider: SearchProvider = "tavily"
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    brave: BraveSearchConfig = Field(default_factory=BraveSearchConfig)
    exa: ExaSearchConfig = Field(default_factory=ExaSearchConfig)
    perplexity: PerplexitySearchConfig = Field(default_factory=PerplexitySearchConfig)
    searxng: SearXNGConfig = Field(default_factory=SearXNGConfig)

    def selected(self) -> SearchProviderConfig:
        return getattr(self, self.provider)


class MCPServerConfig(SettingsModel):
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    enabled: bool = True
    timeout: float = Field(default=10, gt=0, le=120)

    _validate_credentials = field_validator("env", "headers")(validate_settings_credentials)

    @field_validator("url")
    @classmethod
    def endpoint_url(cls, value: str | None) -> str | None:
        return validate_endpoint(value) if value is not None else None


class AvatarConfig(SettingsModel):
    enabled: bool = False
    server: str = "avatar"
    profile: str = Field(default="companion", strict=True)
    lifecycle_url: str = "http://127.0.0.1:8766/events"
    signing_key_ref: SecretReference | None = None
    catalog_ttl: int = Field(default=300, ge=5)

    _validate_endpoint = field_validator("lifecycle_url")(validate_endpoint)

    @field_validator("profile")
    @classmethod
    def valid_profile(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Avatar profile must be valid UTF-8") from None
        if not value.strip() or any(
            unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value
        ):
            raise ValueError("Avatar profile must be nonempty single-line text without controls")
        return value


class ThemeConfig(SettingsModel):
    name: str = "tokyo-night"
    colors: dict[str, str] = Field(default_factory=dict)
    custom_base: str | None = None
    custom_colors: dict[str, str] = Field(default_factory=dict)


StatusComponent = Literal["model", "context", "cache", "speed", "connection", "memory",
                          "avatar"]


def default_status_components() -> list[StatusComponent]:
    return ["model", "context", "cache", "speed"]


def validate_status_components(value: list[StatusComponent]) -> list[StatusComponent]:
    if len(value) != len(set(value)):
        raise ValueError("Choose each status component only once")
    return value


class InterfacePreferences(SettingsModel):
    follow_replies: bool = True
    show_service_status: bool = True
    status_components: list[StatusComponent] = Field(default_factory=default_status_components)

    _status_components = field_validator("status_components")(validate_status_components)


class AppConfig(SettingsModel):
    state_dir: Path = Field(default_factory=default_state_dir)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    prompts: dict[str, str] = Field(default_factory=dict)
    provider: str = "local"
    providers: list[ProviderConfig] = Field(default_factory=lambda: [ProviderConfig()])
    timezone: str = Field(default_factory=system_timezone)
    reset_hour: int = Field(default=4, ge=0, le=23)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    auxiliary: AuxiliaryModelConfig = Field(default_factory=AuxiliaryModelConfig)
    vision: VisionModelConfig = Field(default_factory=VisionModelConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    allowed_roots: list[str] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    jobs_file: Path | None = None
    theme: ThemeConfig = Field(default_factory=ThemeConfig)
    bindings: dict[str, str] = Field(default_factory=lambda: {"send": "enter", "newline": "shift+enter"})
    preferences: InterfacePreferences = Field(default_factory=InterfacePreferences)

    @model_validator(mode="before")
    @classmethod
    def migrate_tavily(cls, values: Any) -> Any:
        if isinstance(values, dict) and "tavily" in values:
            values = values.copy()
            tavily = values.pop("tavily")
            search = values.get("web_search", {})
            if isinstance(search, dict):
                values["web_search"] = {"tavily": tavily, **search}
        return values

    @property
    def tavily(self) -> TavilyConfig:
        """Compatibility for callers editing the original Tavily settings."""
        return self.web_search.tavily

    @field_validator("prompts")
    @classmethod
    def known_prompts(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - set(PROMPT_NAMES) or any(not path.strip() for path in value.values()):
            raise ValueError("Prompt paths must use SYSTEM, SOUL, POST_HISTORY, or USER")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError):
            raise ValueError("Enter a valid IANA timezone, such as Europe/Paris") from None
        return value

    @model_validator(mode="after")
    def selected_provider_exists(self):
        names = [p.name for p in self.providers]
        if len(names) != len(set(names)) or self.provider not in names:
            raise ValueError("providers must have unique names and include the selected provider")
        return self

    def selected_provider(self) -> ProviderConfig:
        return next(p for p in self.providers if p.name == self.provider)

    @property
    def socket_path(self) -> Path:
        # macOS sockaddr_un limits paths to 104 bytes; use a private short directory.
        import hashlib
        identity = hashlib.sha256(str(self.state_dir.resolve()).encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"eidolon-{os.getuid()}-{identity}" / "room.sock"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "room.sqlite3"


def resolve_secret(reference: str | None) -> str | None:
    if not reference:
        return None
    validate_secret_reference(reference)
    if reference.startswith("env:"):
        value = os.environ.get(reference[4:])
        if not value:
            raise ValueError(f"Missing environment credential {reference[4:]}")
        return value
    if reference.startswith("keyring:"):
        import keyring
        with _keychain_lock:
            if reference not in _keychain_values:
                value = keyring.get_password("eidolon", reference[8:])
                if not value:
                    raise ValueError(f"Missing Keychain credential {reference[8:]}")
                _keychain_values[reference] = value
            return _keychain_values[reference]
    raise ValueError("Credentials must use env:NAME or keyring:NAME references")


def store_secret(name: str, value: str) -> str:
    reference = validate_secret_reference(f"keyring:{name}")
    if not value:
        raise ValueError("Credential cannot be empty")
    import keyring
    with _keychain_lock:
        _keychain_values.pop(reference, None)
        keyring.set_password("eidolon", name, value)
        _keychain_values[reference] = value
    return reference


def delete_secret(name: str) -> str:
    reference = validate_secret_reference(f"keyring:{name}")
    import keyring
    with _keychain_lock:
        _keychain_values.pop(reference, None)
        keyring.delete_password("eidolon", name)
    return reference


def clear_secret_cache(reference: str) -> None:
    """Forget one credential after a CLI write in another process."""
    validate_secret_reference(reference)
    with _keychain_lock:
        _keychain_values.pop(reference, None)


def config_path() -> Path:
    return default_state_dir() / "config.yaml"


def _require_private_location(path: Path) -> None:
    # A worktree uses a .git file, while a normal checkout uses a directory.
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Runtime configuration and app state must live outside Git checkouts; set EIDOLON_HOME to a private directory")


def _configuration_target(path: Path) -> Path:
    target = path.expanduser().resolve()
    if ".example." in path.name or ".example." in target.name:
        raise ValueError("Example configuration is a template; use a private config.yaml instead")
    _require_private_location(target.parent)
    return target


def load_config(path: Path | None = None) -> AppConfig:
    target = _configuration_target(path or config_path())
    handoff = target.parent / "migration/rebrand.json"
    if handoff.is_file():
        try:
            verified = json.loads(handoff.read_text()).get("status") == "verified"
        except (ValueError, AttributeError):
            verified = False
        if not verified:
            raise ValueError("Installation migration is incomplete; inspect the private migration report before starting")
    if not target.exists():
        legacy = target.with_name("config.json")
        if target.name == "config.yaml" and legacy.is_file():
            config = _read_config(_configuration_target(legacy))
            save_config(config, target)
            legacy.chmod(0o600)
            return config
        raise FileNotFoundError(f"Room is not configured. Run `eidolon setup` ({target}).")
    config = _read_config(target)
    # Upgrade legacy source-tree prompts once, preserving their exact contents.
    if _privatize_prompts(config):
        save_config(config, target)
    else:
        target.chmod(0o600)
    return config


def _read_config(target: Path) -> AppConfig:
    content = target.read_text(encoding="utf-8")
    try:
        values = json.loads(content) if target.suffix == ".json" else yaml.safe_load(content)
    except (json.JSONDecodeError, yaml.YAMLError):
        raise ValueError("Configuration could not be parsed; check the file syntax") from None
    # Discard retired file-output settings when opening an older configuration.
    # Keep normal settings validation strict for unrelated unknown keys.
    if isinstance(values, dict):
        values.pop("drawer_root", None)
        preferences = values.get("preferences")
        if isinstance(preferences, dict) and isinstance(preferences.get("status_components"), list):
            preferences["status_components"] = [
                name for name in preferences["status_components"] if name != "notes"
            ]
    return AppConfig.model_validate(values)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = _configuration_target(path or config.state_dir / "config.yaml")
    # Revalidate mutable dictionaries before any private data reaches disk.
    AppConfig.model_validate(config.model_dump())
    _privatize_prompts(config)
    private_directory(target.parent)
    values = config.model_dump(mode="json")
    data = (json.dumps(values, indent=2) + "\n" if target.suffix == ".json"
            else yaml.safe_dump(values, sort_keys=False, allow_unicode=True))
    atomic_write(target, data, mode=0o600)
    return target


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".eidolon-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _privatize_prompts(config: AppConfig) -> bool:
    """Copy editable prompts into app state; package defaults are only seed data.

    All sources and conflicts are checked before writing. A missing configured
    prompt is an error, never a silent reset of an existing personality.
    """
    state = config.state_dir.expanduser().resolve()
    _require_private_location(state)
    directory = state / "prompts"
    if directory.is_symlink():
        raise ValueError("The private prompts directory must not be a symbolic link")
    prompts, copies = {}, {}
    for name in PROMPT_NAMES:
        destination = directory / f"{name}.md"
        if destination.is_symlink():
            raise ValueError(f"The private {name} prompt must not be a symbolic link")
        source = Path(config.prompts[name]).expanduser().resolve() if name in config.prompts else None
        if source is not None:
            if not source.is_file():
                raise ValueError(f"Missing prompt source for {name}; restore it before continuing")
            if source != destination:
                content = source.read_bytes()
                if destination.exists() and destination.read_bytes() != content:
                    raise ValueError(f"Private {name} prompt conflicts with its configured source; review both files before migrating")
                copies[destination] = content
        elif not destination.exists():
            copies[destination] = files("eidolon").joinpath("defaults", f"{name}.md").read_bytes()
        prompts[name] = str(destination)

    changed = config.prompts != prompts or config.state_dir != state
    prepared = {destination: content.decode("utf-8") for destination, content in copies.items()}
    private_directory(state)
    private_directory(directory)
    for destination, content in prepared.items():
        # Preserve CRLF and other source bytes when migrating custom prompts.
        atomic_write(destination, content)
    for destination in prompts.values():
        Path(destination).chmod(0o600)
    config.state_dir = state
    config.prompts = prompts
    return changed


def initialize_config(project: Path | None = None, *, state_dir: Path | None = None,
                      path: Path | None = None) -> AppConfig:
    state = (state_dir or default_state_dir()).expanduser().resolve()
    target = _configuration_target(path or state / "config.yaml")
    if target.exists() or (target.name == "config.yaml" and target.with_suffix(".json").is_file()):
        return load_config(target)
    prompts = {}
    if project is not None:
        project = project.expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The prompt import directory does not exist")
        for name in PROMPT_NAMES:
            source = project / f"{name}.md"
            if name == "POST_HISTORY" and not source.is_file():
                source = project / "POST HISTORY.md"
            if source.is_file():
                prompts[name] = str(source)
    config = AppConfig(state_dir=state, prompts=prompts, jobs_file=state / "jobs.json")
    save_config(config, target)
    if not config.jobs_file.exists():
        atomic_write(config.jobs_file, "[]\n")
    return config
