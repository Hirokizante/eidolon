"""Provider-specific web search settings embedded in Preferences."""

from dataclasses import dataclass, field

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Checkbox, Collapsible, Input, Select, Static

from eidolon.config import SEARCH_PROVIDERS, AppConfig
from eidolon.preferences import SearchCredentialError, merge_web_search
from eidolon.ui.forms import FormBody, FormCheckbox


@dataclass
class WebSearchState:
    draft: dict | None = None
    keys: dict[str, str] = field(default_factory=dict, repr=False)
    removed: set[str] = field(default_factory=set)

    def clear(self) -> None:
        self.draft = None
        self.keys.clear()
        self.removed.clear()


@dataclass(frozen=True)
class SearchField:
    name: str
    label: str
    kind: str = "text"
    options: tuple = ()
    optional: bool = False
    help: str = ""
    advanced: bool = True


COMMON = (
    SearchField("max_results", "Maximum results (1–20)", "integer", advanced=False),
    SearchField("timeout_seconds", "Timeout in seconds (1–120)", "number"),
    SearchField("max_content_chars", "Excerpt characters per result (100–20000)", "integer"),
)
DATES = (
    SearchField("start_date", "Published after (YYYY-MM-DD)", optional=True),
    SearchField("end_date", "Published before (YYYY-MM-DD)", optional=True),
)
TIME_RANGES = (
    ("Any time", ""),
    ("Past day", "day"),
    ("Past week", "week"),
    ("Past month", "month"),
    ("Past year", "year"),
)
FIELDS = {
    "tavily": (
        SearchField(
            "depth",
            "Search depth",
            "select",
            (
                ("Basic", "basic"),
                ("Advanced", "advanced"),
                ("Fast", "fast"),
                ("Ultra-fast", "ultra-fast"),
            ),
            advanced=False,
        ),
        SearchField(
            "topic",
            "Topic",
            "select",
            (("General", "general"), ("News", "news"), ("Finance", "finance")),
        ),
        SearchField("time_range", "Recency", "select", TIME_RANGES, optional=True),
        *DATES,
        SearchField(
            "include_domains",
            "Include domains",
            "list",
            help="Comma-separated; blank searches all domains.",
        ),
        SearchField(
            "exclude_domains",
            "Exclude domains",
            "list",
            help="Comma-separated; blank excludes none.",
        ),
        SearchField("chunks_per_source", "Snippets per source (1–3)", "integer"),
        SearchField("include_published_date", "Include publication dates", "boolean"),
        SearchField("exact_match", "Match quoted phrases exactly", "boolean"),
        SearchField("safe_search", "Safe search (basic / advanced only)", "boolean"),
    ),
    "brave": (
        SearchField("country", "Country code (or ALL)", advanced=False),
        SearchField("search_lang", "Search language code", help="For example en, fr, or zh-hans."),
        SearchField(
            "safesearch",
            "Safe search",
            "select",
            (("Off", "off"), ("Moderate", "moderate"), ("Strict", "strict")),
        ),
        SearchField(
            "freshness",
            "Freshness (optional)",
            optional=True,
            help="pd / pw / pm / py, or YYYY-MM-DDtoYYYY-MM-DD.",
        ),
        SearchField("extra_snippets", "Include additional snippets", "boolean"),
        SearchField(
            "goggles",
            "Goggles (optional)",
            "list",
            help="Comma-separated Goggle URLs for custom ranking.",
        ),
    ),
    "exa": (
        SearchField(
            "search_type",
            "Search mode",
            "select",
            (
                ("Auto", "auto"),
                ("Instant", "instant"),
                ("Fast", "fast"),
                ("Deep lite", "deep-lite"),
                ("Deep", "deep"),
                ("Deep reasoning", "deep-reasoning"),
            ),
            advanced=False,
        ),
        SearchField(
            "content_mode",
            "Returned content",
            "select",
            (("Relevant highlights", "highlights"), ("Page text (bounded)", "text")),
        ),
        SearchField(
            "category",
            "Category",
            "select",
            (
                ("All categories", ""),
                ("News", "news"),
                ("Scholarly publications", "publication"),
                ("Companies", "company"),
                ("People", "people"),
                ("Personal sites", "personal site"),
                ("Financial reports", "financial report"),
            ),
            optional=True,
            help="Company / people searches cannot use publication-date filters.",
        ),
        SearchField(
            "include_domains",
            "Include domains or paths",
            "list",
            help="Comma-separated; blank searches all domains.",
        ),
        SearchField(
            "exclude_domains",
            "Exclude domains or paths",
            "list",
            help="Comma-separated; unavailable for people searches.",
        ),
        *DATES,
    ),
    "perplexity": (
        SearchField(
            "search_type",
            "Search mode",
            "select",
            (("Standard web", "web"), ("Fast web", "fast")),
            advanced=False,
        ),
        SearchField(
            "country",
            "Country code (optional)",
            optional=True,
            help="Two-letter code, for example PH or US.",
        ),
        SearchField(
            "language_filter",
            "Language codes (optional)",
            "list",
            help="Up to 20 comma-separated two-letter codes, for example en, fr.",
        ),
        SearchField(
            "domain_filter",
            "Domain filter (optional)",
            "list",
            help="Up to 20 domains. Prefix every domain with - to exclude instead.",
        ),
        SearchField(
            "recency",
            "Publication recency",
            "select",
            (("Any time", ""), ("Past hour", "hour"), *TIME_RANGES[1:]),
            optional=True,
        ),
        *DATES,
        SearchField("updated_after", "Updated after (YYYY-MM-DD)", optional=True),
        SearchField("updated_before", "Updated before (YYYY-MM-DD)", optional=True),
        SearchField("max_tokens", "Total content token limit (1–1000000)", "integer"),
        SearchField("max_tokens_per_page", "Content tokens per page (1–1000000)", "integer"),
    ),
    "searxng": (
        SearchField(
            "base_url",
            "Instance base URL",
            advanced=False,
            help="Your instance must enable the json search format.",
        ),
        SearchField(
            "engines",
            "Engines (optional)",
            "list",
            help="Comma-separated engine names; blank uses the instance defaults.",
        ),
        SearchField(
            "categories",
            "Categories (optional)",
            "list",
            help="Comma-separated categories; blank uses the instance defaults.",
        ),
        SearchField(
            "language",
            "Language (optional)",
            optional=True,
            help="For example en or en-US; blank uses the instance default.",
        ),
        SearchField(
            "time_range",
            "Recency",
            "select",
            (("Any time", ""), ("Past day", "day"), ("Past month", "month"), ("Past year", "year")),
            optional=True,
        ),
        SearchField(
            "safesearch", "Safe search", "select", (("Off", 0), ("Moderate", 1), ("Strict", 2))
        ),
    ),
}


def provider_fields(provider: str) -> tuple[SearchField, ...]:
    return (*COMMON, *FIELDS[provider])


class WebSearchForm(FormBody):
    DEFAULT_CSS = """
    WebSearchForm .web-provider-fields { height: auto; }
    WebSearchForm Collapsible { border: none; padding: 0; margin-top: 1; }
    WebSearchForm Collapsible > Contents { padding: 0 0 0 1; }
    WebSearchForm .web-credential { height: auto; color: $text-muted; }
    PreferencesScreen.narrow WebSearchForm Collapsible { margin-top: 0; }
    """

    def __init__(self, config: AppConfig, state: WebSearchState):
        super().__init__(id="preference-web", classes="form-body")
        self.config, self.state = config.model_copy(deep=True), state
        self.saved = config.web_search.model_dump()
        self.values = self.saved.copy()
        for name, value in (state.draft or {}).items():
            self.values[name] = self.saved[name] | value if name in SEARCH_PROVIDERS else value

    def compose(self) -> ComposeResult:
        yield Select(
            [(label, name) for name, label in SEARCH_PROVIDERS.items()],
            value=self.values["provider"],
            allow_blank=False,
            compact=True,
            id="web-provider",
        )
        yield Static("", id="web-provider-error", classes="field-error", markup=False)
        for provider in SEARCH_PROVIDERS:
            with Vertical(id=f"web-{provider}-fields", classes="web-provider-fields"):
                optional = " (optional)" if provider == "searxng" else ""
                yield Static("API key" + optional, classes="field-label")
                yield Input(
                    self.state.keys.get(provider, ""),
                    password=True,
                    placeholder="Blank keeps saved key; or env:NAME / keyring:NAME",
                    id=f"web-{provider}-key",
                )
                yield Static(
                    "", id=f"web-{provider}-key-error", classes="field-error", markup=False
                )
                yield Static(
                    self.credential_label(provider),
                    id=f"web-{provider}-credential",
                    classes="web-credential",
                    markup=False,
                )
                yield FormCheckbox(
                    "Remove saved credential",
                    value=provider in self.state.removed,
                    id=f"web-{provider}-remove-key",
                )
                fields = provider_fields(provider)
                for spec in fields:
                    if not spec.advanced:
                        yield from self.compose_field(provider, spec)
                with Collapsible(
                    title="Search filters and limits",
                    collapsed_symbol="+",
                    expanded_symbol="-",
                    id=f"web-{provider}-advanced",
                ):
                    for spec in fields:
                        if spec.advanced:
                            yield from self.compose_field(provider, spec)

    def compose_field(self, provider: str, spec: SearchField) -> ComposeResult:
        value = self.values[provider][spec.name]
        control_id = f"web-{provider}-{spec.name}"
        if spec.kind == "boolean":
            yield FormCheckbox(spec.label, value=value, id=control_id)
        else:
            yield Static(spec.label, classes="field-label", markup=False)
            if spec.kind == "select":
                yield Select(
                    list(spec.options),
                    value="" if value is None else value,
                    allow_blank=False,
                    compact=True,
                    id=control_id,
                )
            else:
                text = (
                    ", ".join(value) if spec.kind == "list" else "" if value is None else str(value)
                )
                yield Input(text, id=control_id)
        yield Static("", id=control_id + "-error", classes="field-error", markup=False)
        if spec.help:
            yield Static(spec.help, classes="field-help", markup=False)

    def credential_label(self, provider: str) -> str:
        reference = self.saved[provider]["api_key_ref"]
        return (
            f"Saved: {reference}" if reference else "No credential saved"
        ) + " · New keys use Keychain."

    def on_mount(self) -> None:
        self.show_provider(self.values["provider"])

    def show_provider(self, provider: str) -> None:
        for name in SEARCH_PROVIDERS:
            self.query_one(f"#web-{name}-fields").display = name == provider

    def focus_title(self) -> str:
        provider = self.query_one("#web-provider", Select).value
        label = SEARCH_PROVIDERS[provider]
        focused = self.screen.focused
        control_id = focused.id if focused is not None else None
        prefix = f"web-{provider}-"
        if control_id and control_id.startswith(prefix):
            name = control_id[len(prefix) :]
            field = next(
                (spec.label for spec in provider_fields(provider) if spec.name == name), None
            )
            field = {"key": "API key", "remove-key": "Remove saved credential"}.get(name, field)
            if field:
                return label + " · " + field.split(" (", 1)[0]
        return "Web search · " + label

    @on(Select.Changed, "#web-provider")
    def provider_changed(self, event: Select.Changed) -> None:
        if self.is_mounted and event.value in SEARCH_PROVIDERS:
            self.show_provider(event.value)

    def read_values(self) -> dict:
        values = {"provider": self.query_one("#web-provider", Select).value}
        for provider in SEARCH_PROVIDERS:
            values[provider] = {}
            for spec in provider_fields(provider):
                control = self.query_one(f"#web-{provider}-{spec.name}")
                value = control.value
                if isinstance(control, Input):
                    value = value.strip()
                    if spec.kind == "list":
                        value = [item.strip() for item in value.split(",")] if value else []
                    elif spec.kind in {"integer", "number"}:
                        try:
                            value = int(value) if spec.kind == "integer" else float(value)
                        except ValueError:
                            pass
                if spec.optional and value == "":
                    value = None
                values[provider][spec.name] = value
        return values

    def changes(self) -> dict:
        values = self.read_values()
        changes = {}
        if values["provider"] != self.saved["provider"]:
            changes["provider"] = values["provider"]
        for provider in SEARCH_PROVIDERS:
            patch = {
                name: value
                for name, value in values[provider].items()
                if value != self.saved[provider][name]
            }
            if patch:
                changes[provider] = patch
        return changes

    def credentials(self) -> dict:
        keys = {}
        for provider in SEARCH_PROVIDERS:
            key = self.query_one(f"#web-{provider}-key", Input).value.strip()
            remove = self.query_one(f"#web-{provider}-remove-key", Checkbox).value
            if key and remove:
                raise SearchCredentialError(
                    provider, "Enter a replacement key or remove the saved credential"
                )
            if key or remove:
                keys[provider] = key if key else None
        return keys

    def is_dirty(self) -> bool:
        return bool(self.changes()) or any(
            self.query_one(f"#web-{name}-key", Input).value.strip()
            or self.query_one(f"#web-{name}-remove-key", Checkbox).value
            for name in SEARCH_PROVIDERS
        )

    def remember_draft(self) -> None:
        self.state.draft = self.changes() or None
        self.state.keys = {
            name: self.query_one(f"#web-{name}-key", Input).value
            for name in SEARCH_PROVIDERS
            if self.query_one(f"#web-{name}-key", Input).value
        }
        self.state.removed = {
            name
            for name in SEARCH_PROVIDERS
            if self.query_one(f"#web-{name}-remove-key", Checkbox).value
        }

    def validated(self) -> None:
        merge_web_search(self.config, self.changes(), self.credentials())

    def error_field(self, provider: str, name: str | None) -> str:
        self.query_one("#web-provider", Select).value = provider
        self.show_provider(provider)
        if name == "api_key_ref":
            name = "key"
        if name is None:
            name = (
                "category"
                if provider == "exa"
                else "depth"
                if provider == "tavily"
                else "max_results"
            )
        return f"web-{provider}-{name}"

    def restore(self, config: AppConfig) -> None:
        self.config = config.model_copy(deep=True)
        self.saved = config.web_search.model_dump()
        self.values = self.saved.copy()
        self.state.clear()
        self.query_one("#web-provider", Select).value = self.saved["provider"]
        self.show_provider(self.saved["provider"])
        for provider in SEARCH_PROVIDERS:
            self.query_one(f"#web-{provider}-key", Input).value = ""
            self.query_one(f"#web-{provider}-remove-key", Checkbox).value = False
            self.query_one(f"#web-{provider}-credential", Static).update(
                self.credential_label(provider)
            )
            for spec in provider_fields(provider):
                control = self.query_one(f"#web-{provider}-{spec.name}")
                value = self.saved[provider][spec.name]
                if isinstance(control, Input):
                    value = (
                        ", ".join(value)
                        if spec.kind == "list"
                        else ""
                        if value is None
                        else str(value)
                    )
                elif isinstance(control, Select) and value is None:
                    value = ""
                control.value = value
