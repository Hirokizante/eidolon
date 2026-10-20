"""Cell-aware, single-line status components."""

from collections.abc import Mapping

from rich.cells import cell_len
from rich.text import Text

from eidolon.config import ProviderConfig


COMPONENT_LABELS = {
    "model": "Model and reasoning", "context": "Context window", "cache": "Cache hit rate",
    "speed": "Tokens per second", "connection": "Provider connection", "memory": "Memory status",
    "avatar": "Avatar status",
}
STATUS_COLORS = {"success": "green", "warning": "dark_orange", "error": "red"}


def integration_state(value: str | dict | None) -> str | None:
    if isinstance(value, dict):
        if not value.get("enabled"):
            return None
        catalog = value.get("catalog") or {}
        return "success" if catalog.get("expressions") or catalog.get("gestures") else "warning"
    value = (value or "").strip().casefold()
    if value in {"", "not checked", "unknown", "disabled", "off", "not configured"}:
        return None
    if value in {"connected", "enabled", "online", "ready"} or value.startswith("connected "):
        return "success"
    if value.startswith(("connecting", "pending", "starting", "checking")):
        return "warning"
    return "error"


def compact_tokens(value: int | None, unknown: str) -> str:
    if value is None:
        return unknown
    for scale, suffix in ((1_000_000, "M"), (1_000, "k")):
        if value >= scale:
            return f"{value / scale:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def truncate(value: str, width: int, *, ascii: bool) -> str:
    if cell_len(value) <= width:
        return value
    marker = "..." if ascii and width >= 3 else "." if ascii else "…"
    text = Text(value)
    text.truncate(max(0, width - len(marker)), overflow="crop")
    return text.plain + marker if width > 0 else ""


def status_line(profile: ProviderConfig, components: list[str], status: dict, *,
                width: int, ascii: bool = False, unsaved: bool = False,
                colors: Mapping[str, str | None] | None = None, no_color: bool = False) -> Text:
    def color(state: str | None) -> str:
        if no_color or state is None:
            return ""
        return (colors or {}).get(state) or STATUS_COLORS[state]

    def indicator(label: str, state: str | None) -> Text:
        symbols = {
            "success": ("●", "o"), "warning": ("◐", "~"),
            "error": ("○", "!"), None: ("◌", "-"),
        }
        symbol, ascii_symbol = symbols[state]
        return Text.assemble((ascii_symbol if ascii else symbol, color(state)), " " + label)

    unknown = "-" if ascii else "—"
    metrics = status.get("metrics") or {}
    effort = profile.parameters.get("reasoning_effort")
    if (metrics.get("provider"), metrics.get("model"), metrics.get("reasoning_effort")) != (
        profile.name, profile.model, effort,
    ):
        metrics = {}
    current = metrics.get("context_tokens")
    maximum = profile.context_window_for()
    context_metadata = status.get("model_context") or {}
    if profile.context_window is None and (
        context_metadata.get("provider"), context_metadata.get("model"),
    ) == (profile.name, profile.model):
        maximum = context_metadata.get("context_window") or maximum
    if "context_window" in metrics:
        maximum = metrics["context_window"]
    estimate = "~" if current is not None and metrics.get("context_estimated") else ""
    count = f"{estimate}{compact_tokens(current, unknown)}/{compact_tokens(maximum, unknown)}"
    rate = metrics.get("cache_hit_rate")
    percentage = round(rate * 100) if rate is not None else None
    cache_value = f"{percentage}%" if percentage is not None else unknown
    cache_state = None
    if percentage:
        cache_state = "success" if percentage >= 75 else "warning" if percentage >= 25 else "error"
    speed = metrics.get("tokens_per_second")
    outbox = status.get("outbox") or {}
    memory_state = integration_state(status.get("memory"))
    if outbox.get("pending"):
        if outbox.get("error"):
            memory_state = "error"
        elif memory_state != "error":
            memory_state = "warning"
    cache_icon, speed_icon = ("C", ">") if ascii else ("↺", "⚡")
    fields = {
        "model": Text(f"{profile.model} {effort or 'default'}"),
        "cache": Text(f"{cache_icon} {cache_value}", style=color(cache_state)),
        "speed": Text(f"{speed_icon} {speed:.1f}" if speed is not None
                      else f"{speed_icon} {unknown}"),
        "connection": Text(status.get("provider", profile.display_name or profile.name)),
        "memory": indicator("Memory", memory_state),
        "avatar": indicator("Avatar", integration_state(status.get("avatar"))),
    }

    def context(cells: int) -> str:
        filled = 0
        if current is not None and maximum is not None and maximum > 0:
            filled = min(cells, max(0, round(current / maximum * cells)))
        bar = ("#" if ascii else "█") * filled + ("." if ascii else "░") * (cells - filled)
        return f"{bar} {count}"

    selected = [name for name in components if name in COMPONENT_LABELS]
    prefix = [Text("Agent mode", style="bold")] if status.get("agent_mode") else []
    if unsaved:
        prefix.append(Text("Unsaved (Ctrl+S)"))
    separator = Text(" | ", no_wrap=True, overflow="crop")
    for cells in (8, 4, 2):
        fields["context"] = Text(context(cells))
        values = prefix + [fields[name] for name in selected]
        if separator.join(values).cell_len <= width:
            break
    if "model" in selected:
        others = prefix + [fields[name] for name in selected if name != "model"]
        budget = max(1, width - separator.join(others).cell_len - 3 * bool(others))
        suffix = " " + str(effort or "default")
        model = truncate(profile.model, max(1, budget - cell_len(suffix)), ascii=ascii)
        fields["model"] = Text(model + suffix)
    text = separator.join(prefix + [fields[name] for name in selected])
    text.plain = truncate(text.plain, max(0, width), ascii=ascii)
    return text
