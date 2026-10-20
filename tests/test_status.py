import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual.widgets import Select, Static

from eidolon.config import AppConfig, InterfacePreferences, ProviderConfig
from eidolon.engine.metrics import request_metrics
from eidolon.ui.app import RoomApp
from eidolon.ui.status import status_line
from test_ui import FakeClient


def profile_and_status(model="test-model"):
    profile = ProviderConfig(model=model, context_window=1_000_000,
                             parameters={"reasoning_effort": "high"})
    status = {"metrics": {"provider": "local", "model": model, "reasoning_effort": "high",
                          "context_tokens": 5100, "cache_hit_rate": 0.8,
                          "tokens_per_second": 42.5}}
    return profile, status


def test_default_status_has_model_effort_meter_counts_cache_and_speed():
    profile, status = profile_and_status()
    line = status_line(profile, InterfacePreferences().status_components, status, width=100).plain
    model, context, cache, speed = line.split(" | ")
    assert model == "test-model high"
    assert context == "░░░░░░░░ 5.1k/1M"
    assert cache == "↺ 80%" and speed == "⚡ 42.5"
    status["metrics"]["context_tokens"] = 500_000
    assert "████░░░░ 500k/1M" in status_line(profile, ["context"], status, width=100).plain


@pytest.mark.parametrize("width", [76, 58, 46])
@pytest.mark.parametrize("ascii", [False, True])
def test_narrow_status_keeps_default_metrics_and_measures_terminal_cells(width, ascii):
    profile, status = profile_and_status("模型e\u0301-" * 20)
    line = status_line(profile, InterfacePreferences().status_components, status,
                       width=width, ascii=ascii).plain
    assert cell_len(line) <= width
    assert len(line.split(" | ")) == 4
    assert "high |" in line and "5.1k/1M" in line
    assert ("C 80%" if ascii else "↺ 80%") in line
    assert line.endswith("> 42.5" if ascii else "⚡ 42.5")
    if ascii:
        assert "█" not in line and "░" not in line and "…" not in line
        assert "↺" not in line and "⚡" not in line


def test_unknown_metrics_and_model_switch_do_not_show_old_or_fabricated_values():
    profile, status = profile_and_status()
    profile.model = "other-model"
    line = status_line(profile, InterfacePreferences().status_components, status, width=100).plain
    assert "░░░░░░░░ —/1M" in line and "↺ — | ⚡ —" in line
    profile.context_window = None
    assert "—/—" in status_line(profile, ["context"], status, width=100).plain
    assert status_line(profile, [], status, width=100).plain == ""


def test_custom_status_order_optional_components_and_context_overflow():
    profile, status = profile_and_status()
    status.update(memory="connected", outbox={"pending": 2}, avatar="offline")
    assert status_line(profile, ["speed", "memory", "avatar"], status, width=100).plain == (
        "⚡ 42.5 | ◐ Memory | ○ Avatar"
    )
    status["metrics"]["context_tokens"] = 1_200_000
    assert status_line(profile, ["context"], status, width=100).plain == "████████ 1.2M/1M"


@pytest.mark.parametrize("ascii", [False, True])
@pytest.mark.parametrize("current,maximum", [(None, None), (None, 1_000_000), (5100, None),
                                             (0, 1_000_000), (5100, 0)])
def test_unknown_or_zero_context_renders_an_empty_unbracketed_meter(ascii, current, maximum):
    profile, status = profile_and_status()
    status["metrics"].update(context_tokens=current, context_window=maximum)
    line = status_line(profile, ["context"], status, width=100, ascii=ascii).plain
    assert line.startswith(("........" if ascii else "░░░░░░░░") + " ")
    assert not any(character in line for character in "[]?")


@pytest.mark.parametrize("rate,expected", [(None, None), (0, None), (0.001, None),
                                         (0.01, "red"), (0.24, "red"), (0.25, "dark_orange"),
                                         (0.74, "dark_orange"), (0.75, "green"), (1, "green")])
@pytest.mark.parametrize("ascii", [False, True])
def test_cache_icon_and_percentage_share_color_without_coloring_neighbors(rate, expected, ascii):
    profile, status = profile_and_status()
    status["metrics"]["cache_hit_rate"] = rate
    line = status_line(profile, ["model", "cache", "speed"], status, width=60, ascii=ascii)
    cache = line.plain.split(" | ")[1]
    start = line.plain.index(cache)
    console = Console()
    expected_color = Style(color=expected).color
    for offset in range(len(line)):
        assert line.get_style_at_offset(console, offset).color == (
            expected_color if start <= offset < start + len(cache) else None
        )


@pytest.mark.parametrize("component", ["memory", "avatar"])
@pytest.mark.parametrize("value,symbol,color", [
    ("connected", "●", "green"),
    ("connected to existing bank 'example-bank'", "●", "green"),
    ("connected · 5 expressions, 3 gestures", "●", "green"),
    ("connecting or unavailable", "◐", "dark_orange"),
    ("offline", "○", "red"),
    ("Could not connect", "○", "red"),
    ("disabled", "◌", None),
    ("not checked", "◌", None),
    (None, "◌", None),
])
def test_integrations_show_only_a_light_and_label(component, value, symbol, color):
    profile, status = profile_and_status()
    status[component] = value
    line = status_line(profile, [component], status, width=20)
    assert line.plain == f"{symbol} {component.title()}"
    console = Console()
    assert line.get_style_at_offset(console, 0).color == Style(color=color).color
    assert line.get_style_at_offset(console, 2).color is None


@pytest.mark.parametrize("avatar,symbol,color", [
    ({"enabled": False}, "◌", None),
    ({"enabled": True, "catalog": {}}, "◐", "dark_orange"),
    ({"enabled": True, "catalog": {"expressions": ["smile"]}}, "●", "green"),
    ({"enabled": True, "catalog": {"gestures": ["wave"]}}, "●", "green"),
])
def test_avatar_catalog_status_uses_a_light_without_counts(avatar, symbol, color):
    profile, status = profile_and_status()
    status["avatar"] = avatar
    line = status_line(profile, ["avatar"], status, width=20)
    assert line.plain == f"{symbol} Avatar"
    assert line.get_style_at_offset(Console(), 0).color == Style(color=color).color


@pytest.mark.parametrize("pending,error,memory,symbol,color", [
    (0, None, "connected", "●", "green"),
    (2, None, "connected", "◐", "dark_orange"),
    (2, "Retention failed", "connected", "○", "red"),
    (2, None, "offline", "○", "red"),
])
def test_memory_queue_updates_the_light_without_extra_text(pending, error, memory, symbol, color):
    profile, status = profile_and_status()
    status.update(memory=memory, outbox={"pending": pending, "error": error})
    line = status_line(profile, ["memory"], status, width=20)
    assert line.plain == f"{symbol} Memory"
    assert line.get_style_at_offset(Console(), 0).color == Style(color=color).color


@pytest.mark.parametrize("ascii", [False, True])
@pytest.mark.parametrize("memory,pending,symbol,ascii_symbol", [
    ("connected", 2, "◐", "~"), ("disabled", 0, "◌", "-"),
])
def test_no_color_keeps_indicators_distinct_and_retains_agent_emphasis(ascii, memory, pending,
                                                                     symbol, ascii_symbol):
    profile, status = profile_and_status()
    status.update(agent_mode=True, memory=memory, outbox={"pending": pending}, avatar="offline")
    line = status_line(profile, ["cache", "memory", "avatar"], status,
                       width=80, ascii=ascii, no_color=True)
    assert line.plain == (
        f"Agent mode | C 80% | {ascii_symbol} Memory | ! Avatar" if ascii else
        f"Agent mode | ↺ 80% | {symbol} Memory | ○ Avatar"
    )
    console = Console()
    assert line.get_style_at_offset(console, 0).bold
    assert all(line.get_style_at_offset(console, i).color is None for i in range(len(line)))


def test_cache_color_survives_model_truncation_and_custom_component_order():
    profile, status = profile_and_status("模型e\u0301-" * 20)
    line = status_line(profile, ["speed", "model", "cache"], status, width=30,
                       colors={"success": "#9ece6a"})
    assert line.cell_len <= 30 and "…" in line.plain
    assert line.plain.endswith(" | ↺ 80%")
    console = Console()
    assert line.get_style_at_offset(console, -1).color == Style(color="#9ece6a").color
    assert line.get_style_at_offset(console, 0).color is None


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
@pytest.mark.parametrize("ascii,no_color", [(False, False), (False, True), (True, True)])
async def test_compact_status_fits_and_uses_theme_colors_or_monochrome(size, ascii, no_color,
                                                                     monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("EIDOLON_ASCII", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    if ascii:
        monkeypatch.setenv("EIDOLON_ASCII", "1")
    profile, status = profile_and_status()
    status["metrics"]["cache_hit_rate"] = 0.5
    status.update(memory="connected to existing bank 'example-bank'",
                  avatar="connected · 5 expressions, 3 gestures")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[profile], preferences=InterfacePreferences(
        status_components=["cache", "memory", "avatar", "speed"],
    ))
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.status.update(status)
        app.render_status()
        await pilot.pause()
        widget = app.query_one("#service-status", Static)
        line = widget.content
        assert isinstance(line, Text)
        assert line.plain == (
            "C 50% | o Memory | o Avatar | > 42.5" if ascii else
            "↺ 50% | ● Memory | ● Avatar | ⚡ 42.5"
        )
        assert line.cell_len <= widget.content_region.width and widget.size.height == 1
        console = Console()
        if no_color:
            assert all(line.get_style_at_offset(console, i).color is None for i in range(len(line)))
        else:
            assert line.get_style_at_offset(console, 0).color == Style(
                color=app.current_theme.warning,
            ).color
            start = line.plain.index("● Memory")
            assert line.get_style_at_offset(console, start).color == Style(
                color=app.current_theme.success,
            ).color


async def test_status_colors_refresh_immediately_after_theme_changes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    profile, status = profile_and_status()
    status.update(memory="connecting", avatar="offline")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[profile], preferences=InterfacePreferences(
        status_components=["cache", "memory", "avatar"],
    ))
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.status.update(status)
        app.render_status()
        for theme, colors in (
            ("one-dark", {}),
            ("tokyo-night", {"success": "#12ab34", "warning": "#cd8901", "error": "#ef5678"}),
            ("tokyo-night", {"success": "#56bc78", "warning": "#ab6702", "error": "#de1234"}),
        ):
            app.apply_theme(theme, colors)
            await pilot.pause()
            line = app.query_one("#service-status", Static).content
            console = Console()
            for symbol, role in (("↺", "success"), ("◐", "warning"), ("○", "error")):
                assert line.get_style_at_offset(console, line.plain.index(symbol)).color == Style(
                    color=getattr(app.current_theme, role),
                ).color
        app.theme = "tokyo-night"
        await pilot.pause()
        line = app.query_one("#service-status", Static).content
        assert line.get_style_at_offset(Console(), 0).color == Style(
            color=app.current_theme.success,
        ).color


def test_reported_usage_metrics_handle_missing_invalid_and_zero_counts():
    assert request_metrics({"prompt_tokens": 5000, "completion_tokens": 100,
                            "prompt_tokens_details": {"cached_tokens": 4000}}, 2) == {
        "context_tokens": 5100, "cache_hit_rate": 0.8, "tokens_per_second": 50,
    }
    assert request_metrics({"prompt_tokens": 10, "completion_tokens": 0,
                            "prompt_tokens_details": {"cached_tokens": 0}}, 2) == {
        "context_tokens": 10, "cache_hit_rate": 0, "tokens_per_second": 0,
    }
    assert request_metrics({}, 0) == {
        "context_tokens": None, "cache_hit_rate": None, "tokens_per_second": None,
    }
    result = request_metrics({"prompt_tokens": True, "completion_tokens": -5,
                              "prompt_tokens_details": None}, float("nan"))
    assert all(value is None for value in result.values())


@pytest.mark.parametrize("field", ["prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens"])
def test_compatible_cache_field_variants_use_total_prompt_tokens(field):
    result = request_metrics({"prompt_tokens": 5000, "completion_tokens": 100, field: 4000}, 2)
    assert result == {"context_tokens": 5100, "cache_hit_rate": 0.8, "tokens_per_second": 50}


def test_anthropic_usage_adds_cache_reads_and_writes_once():
    result = request_metrics({"input_tokens": 1000, "output_tokens": 100,
                              "cache_read_input_tokens": 3500,
                              "cache_creation_input_tokens": 500}, 2)
    assert result == {"context_tokens": 5100, "cache_hit_rate": 0.7, "tokens_per_second": 50}
    result = request_metrics({"input_tokens": 5000, "output_tokens": 100,
                              "input_tokens_details": {"cached_tokens": 4000}}, 2)
    assert result["context_tokens"] == 5100 and result["cache_hit_rate"] == 0.8


def test_server_timings_supply_cache_context_and_decode_rate_without_standard_usage():
    result = request_metrics({}, 10, timings={
        "cache_n": 4000, "prompt_n": 1000, "predicted_n": 100,
        "predicted_per_second": 42.5,
    })
    assert result == {"context_tokens": 5100, "cache_hit_rate": 0.8, "tokens_per_second": 42.5}
    assert request_metrics({"completion_tokens": 100}, 10,
        stats={"tokens_per_second": 0})["tokens_per_second"] == 0
    assert request_metrics({"completion_tokens": 100}, 10,
        stats={"tokens_per_second": float("nan")})["tokens_per_second"] == 10


def test_discovered_maximum_updates_status_and_respects_manual_override():
    profile, status = profile_and_status()
    profile.context_window = None
    status["model_context"] = {"provider": "local", "model": "test-model", "context_window": 1000000}
    assert "5.1k/1M" in status_line(profile, ["context"], status, width=100).plain
    profile.context_window = 10000
    assert "5.1k/10k" in status_line(profile, ["context"], status, width=100).plain
    profile.context_window = None
    profile.model = "new-model"
    assert "—/—" in status_line(profile, ["context"], status, width=100).plain


@pytest.mark.parametrize("width", [76, 58, 46])
@pytest.mark.parametrize("ascii", [False, True])
def test_prepared_request_estimate_uses_dispatch_window_and_marks_overflow(width, ascii):
    profile, status = profile_and_status("模型e\u0301-" * 20)
    status["metrics"].update(context_tokens=800_000, context_window=1_000_000,
                             context_estimated=True)
    # Metadata discovery can update during a turn; use its actual dispatch budget.
    status["model_context"] = {"provider": "local", "model": profile.model, "context_window": 2_000_000}
    line = status_line(profile, InterfacePreferences().status_components, status,
                       width=width, ascii=ascii).plain
    assert cell_len(line) <= width
    assert "~800k/1M" in line and ("C 80%" if ascii else "↺ 80%") in line
    status["metrics"]["context_tokens"] = 1_050_000
    line = status_line(profile, ["context"], status, width=width, ascii=ascii).plain
    assert "~1.1M/1M" in line and ("########" if ascii else "████████") in line
    status["metrics"]["context_estimated"] = False
    assert "~" not in status_line(profile, ["context"], status, width=width, ascii=ascii).plain


@pytest.mark.parametrize("size", [(80, 24), (60, 24)])
async def test_prepared_context_meter_renders_before_usage_and_keeps_overflow_visible(size):
    profile, status = profile_and_status()
    status["metrics"].update(context_tokens=800_000, context_window=1_000_000,
                             context_estimated=True, cache_hit_rate=None, tokens_per_second=None)
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[profile])
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.status.update(status)
        app.render_status()
        assert "~800k/1M" in str(app.query_one("#service-status", Static).render())
        app.status["metrics"]["context_tokens"] = 1_050_000
        app.render_status()
        assert "~1.1M/1M" in str(app.query_one("#service-status", Static).render())


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_preferences_reorder_hide_restore_and_apply_status_layout(size):
    profile, status = profile_and_status()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[profile])
    client = FakeClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.status.update(status)
        app.render_status()
        app.execute_command("/preferences")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#preference-category", Select).value = "status"
        await pilot.pause()
        first = screen.query_one("#pref-status_components", Select)
        assert first.region.overlaps(screen.query_one("#preference-status").region)
        first.focus()
        await pilot.press("enter", "end", "enter")  # Choose avatar status by keyboard.
        await pilot.pause()
        assert first.value == "avatar"
        screen.query_one("#pref-status_components-1", Select).value = "model"
        screen.query_one("#pref-status_components-2", Select).value = "hidden"
        await pilot.press("escape", "alt+6")
        await pilot.pause()
        screen = app.screen
        assert screen.read_values()["status_components"] == ["avatar", "model", "speed"]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.preferences.status_components == ["avatar", "model", "speed"]
        assert ("save_preferences", {"preferences": {
            "status_components": ["avatar", "model", "speed"],
        }}) in client.calls
        line = str(app.query_one("#service-status", Static).render())
        assert line.startswith("○ Avatar | test-model high |")


async def test_duplicate_layout_validation_discard_and_empty_layout():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=FakeClient())
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#pref-status_components", Select).value = "cache"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen
        assert screen.query_one("#preference-category", Select).value == "status"
        assert screen.query_one("#pref-status_components-error").has_class("problem")
        assert app.focused is screen.query_one("#pref-status_components", Select)
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.read_values()["status_components"] == ["model", "context", "cache", "speed"]
        for selector in screen.query("#preference-status Select"):
            selector.value = "hidden"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.preferences.status_components == []
        assert not app.query_one("#service-status").display


async def test_streamed_usage_updates_status_while_reply_is_active():
    import asyncio

    profile, metrics_status = profile_and_status()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[profile])
    gate = asyncio.Event()

    class MetricsClient(FakeClient):
        metrics = {}

        async def request(self, method, **params):
            result = await super().request(method, **params)
            if method == "status":
                result["metrics"] = self.metrics
            return result

        async def stream(self, method, **params):
            self.metrics = metrics_status["metrics"]
            yield {"type": "metrics", "metrics": self.metrics}
            yield {"type": "token", "text": "Hello"}
            await gate.wait()
            yield {"type": "completed"}

    client = MetricsClient()
    client.configuration = config.model_dump(mode="json")
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.generate("Hello")
        await pilot.pause()
        assert app.busy
        line = str(app.query_one("#service-status", Static).render())
        assert "5.1k/1M | ↺ 80% | ⚡ 42.5" in line
        gate.set()
        await pilot.pause()
        assert not app.busy
        assert str(app.query_one("#service-status", Static).render()) == line
