"""Editable app behavior, independent of provider and theme configuration."""

from pydantic import Field, field_validator

from eidolon.config import (
    SEARCH_PROVIDERS, AppConfig, IdentityConfig, InterfacePreferences, StatusComponent,
    VisionModelConfig, WebSearchConfig,
    default_status_components, system_timezone, validate_status_components,
)


class UserPreferences(IdentityConfig):
    timezone: str = Field(default_factory=system_timezone)
    reset_hour: int = Field(default=4, ge=0, le=23)
    bindings: dict[str, str] = Field(default_factory=lambda: {
        "send": "enter", "newline": "shift+enter",
    })
    follow_replies: bool = True
    show_service_status: bool = True
    status_components: list[StatusComponent] = Field(default_factory=default_status_components)
    memory_enabled: bool = False
    memory_save_enabled: bool = False
    continuity_enabled: bool = False
    continuity_use_enabled: bool = False
    continuity_max_notes: int = Field(default=12, ge=1, le=64, strict=True)
    continuity_batch_exchanges: int = Field(default=8, ge=1, le=32)
    continuity_idle_seconds: int = Field(default=600, ge=1, le=86400)
    continuity_recovery_calls: int = Field(default=6, ge=1, le=100, strict=True)
    recall_budget: str = "low"
    max_memories: int = Field(default=12, ge=1, le=50)
    avatar_enabled: bool = False
    search_depth: str = "basic"
    search_results: int = Field(default=5, ge=1, le=20)

    _timezone = field_validator("timezone")(AppConfig.valid_timezone.__func__)
    _status_components = field_validator("status_components")(validate_status_components)

    @field_validator("bindings")
    @classmethod
    def valid_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != {"send", "newline"} or any(not key.strip() for key in value.values()):
            raise ValueError("Send and newline both need a keyboard binding")
        if value["send"] == value["newline"]:
            raise ValueError("Send and newline must use different keys")
        return value

    @field_validator("recall_budget")
    @classmethod
    def valid_budget(cls, value: str) -> str:
        if value not in {"low", "mid", "high"}:
            raise ValueError("Choose low, mid, or high recall effort")
        return value

    @field_validator("search_depth")
    @classmethod
    def valid_depth(cls, value: str) -> str:
        if value not in {"basic", "advanced", "fast", "ultra-fast"}:
            raise ValueError("Choose basic, advanced, fast, or ultra-fast search")
        return value

    @classmethod
    def from_config(cls, config: AppConfig) -> "UserPreferences":
        # Project typed configuration without touching integrations.
        return cls.model_construct(
            **config.identity.model_dump(),
            timezone=config.timezone, reset_hour=config.reset_hour,
            bindings={name: config.bindings.get(name, default) for name, default in (
                ("send", "enter"), ("newline", "shift+enter"),
            )},
            **config.preferences.model_dump(),
            memory_enabled=config.memory.enabled, memory_save_enabled=config.memory.save_enabled,
            continuity_enabled=config.memory.continuity_enabled,
            continuity_use_enabled=config.memory.continuity_use_enabled,
            continuity_max_notes=config.memory.continuity_max_notes,
            continuity_batch_exchanges=config.memory.continuity_batch_exchanges,
            continuity_idle_seconds=config.memory.continuity_idle_seconds,
            continuity_recovery_calls=config.memory.continuity_recovery_calls,
            recall_budget=config.memory.recall_budget,
            max_memories=config.memory.max_memories, avatar_enabled=config.avatar.enabled,
            search_depth=config.tavily.depth, search_results=config.tavily.max_results,
        )

    def apply_to(self, config: AppConfig) -> AppConfig:
        """Merge only preferences into the latest saved configuration."""
        candidate = config.model_copy(deep=True)
        candidate.identity = IdentityConfig(
            user_name=self.user_name, companion_name=self.companion_name,
        )
        candidate.timezone, candidate.reset_hour = self.timezone, self.reset_hour
        candidate.bindings.update(self.bindings)
        candidate.preferences = InterfacePreferences(
            follow_replies=self.follow_replies, show_service_status=self.show_service_status,
            status_components=self.status_components,
        )
        candidate.memory.enabled = self.memory_enabled
        candidate.memory.save_enabled = self.memory_save_enabled
        candidate.memory.continuity_enabled = self.continuity_enabled
        candidate.memory.continuity_use_enabled = self.continuity_use_enabled
        candidate.memory.continuity_max_notes = self.continuity_max_notes
        candidate.memory.continuity_batch_exchanges = self.continuity_batch_exchanges
        candidate.memory.continuity_idle_seconds = self.continuity_idle_seconds
        candidate.memory.continuity_recovery_calls = self.continuity_recovery_calls
        candidate.memory.recall_budget = self.recall_budget
        candidate.memory.max_memories = self.max_memories
        candidate.avatar.enabled = self.avatar_enabled
        candidate.tavily.depth = self.search_depth
        candidate.tavily.max_results = self.search_results
        return candidate


def merge_preferences(config: AppConfig, changes: dict) -> AppConfig:
    preferences = UserPreferences.model_validate({
        **UserPreferences.from_config(config).model_dump(), **changes,
    })
    return preferences.apply_to(config)


def merge_vision_model(config: AppConfig, changes: dict) -> AppConfig:
    """Validate a role binding against saved connections without changing dialogue."""
    vision = VisionModelConfig.model_validate({**config.vision.model_dump(), **changes})
    if vision.mode == "separate":
        if not vision.provider or not vision.model:
            raise ValueError("Choose a vision connection and model ID in Preferences.")
        if not any(profile.name == vision.provider for profile in config.providers):
            raise ValueError("Vision connection is missing; choose a saved connection in Preferences.")
    if (vision.mode, vision.provider, vision.model) != (
        config.vision.mode, config.vision.provider, config.vision.model,
    ) and "context_window" not in changes:
        vision.context_window = None
    candidate = config.model_copy(deep=True)
    candidate.vision = vision
    return candidate


class SearchCredentialError(ValueError):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


def merge_web_search(config: AppConfig, changes: dict,
                     api_keys: dict[str, str | None] | None = None) -> tuple[AppConfig, dict[str, str]]:
    """Validate settings and stage literal keys separately from persisted configuration."""
    if not isinstance(changes, dict) or (api_keys is not None and not isinstance(api_keys, dict)):
        raise ValueError("Choose web search settings and credentials")
    values = config.web_search.model_dump()
    for name, patch in changes.items():
        values[name] = values[name] | patch if name in SEARCH_PROVIDERS and isinstance(patch, dict) else patch
    values = WebSearchConfig.model_validate(values).model_dump()
    literal_keys = {}
    credential_edits = set()
    for provider, raw in (api_keys or {}).items():
        if provider not in SEARCH_PROVIDERS:
            raise ValueError("Choose a supported web search provider")
        if raw is not None and (not isinstance(raw, str) or len(raw) > 8192
                                or any(c in raw for c in "\r\n\0")):
            raise SearchCredentialError(provider, "API key must be text or an env:NAME / keyring:NAME reference")
        key = raw.strip() if raw is not None else None
        if key == "":
            continue
        credential_edits.add(provider)
        if raw is None:
            values[provider]["api_key_ref"] = None
        elif key.startswith(("env:", "keyring:")):
            values[provider]["api_key_ref"] = key
        else:
            literal_keys[provider] = key
    if (values["searxng"]["base_url"] != config.web_search.searxng.base_url
            and config.web_search.searxng.api_key_ref and "searxng" not in credential_edits):
        raise SearchCredentialError(
            "searxng", "Reenter or remove the credential before changing the SearXNG instance URL",
        )
    candidate = config.model_copy(deep=True)
    candidate.web_search = WebSearchConfig.model_validate(values)
    return candidate, literal_keys
