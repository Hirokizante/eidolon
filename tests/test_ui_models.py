import asyncio
import copy

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Checkbox, Collapsible, Input, OptionList, Select, Static, TextArea

from eidolon.config import AppConfig, ProviderConfig
from eidolon.ui.models import ConnectScreen, ModelScreen, NEW_PROVIDER, ProviderMenuState


class ModelClient:
    def __init__(self, config):
        self.config = config.model_dump(mode="json")
        self.calls = []
        self.models = ["alpha", "beta", "reasoner"]
        self.error = None
        self.failure = None
        self.gate = None
        self.started = asyncio.Event()

    async def request(self, method, **params):
        self.calls.append((method, params))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            raise RuntimeError(self.failure)
        if method in {"list_models", "discover_models"}:
            return {"models": self.models, "error": self.error}
        if method == "save_provider":
            provider = copy.deepcopy(params["provider"])
            if params.get("api_key"):
                provider["api_key_ref"] = (
                    params["api_key"]
                    if params["api_key"].startswith(("env:", "keyring:"))
                    else f"keyring:provider/{provider['name']}"
                )
            if not provider["model"] and self.models:
                provider["model"] = self.models[0]
            self.config["providers"] = [
                p for p in self.config["providers"] if p["name"] != provider["name"]
            ]
            self.config["providers"].append(provider)
            return {
                "config": copy.deepcopy(self.config),
                "models": self.models,
                "warning": self.error,
            }
        if method == "select_model":
            self.config["provider"] = params["name"]
            provider = next(p for p in self.config["providers"] if p["name"] == params["name"])
            provider["model"] = params["model"]
            if "context_window" in params:
                provider["context_window"] = params["context_window"]
            if "max_response_tokens" in params:
                field = ProviderConfig.model_validate(provider).output_token_parameter()
                provider["parameters"][field] = params["max_response_tokens"]
            if params["reasoning_effort"] is None:
                provider["parameters"].pop("reasoning_effort", None)
            else:
                provider["parameters"]["reasoning_effort"] = params["reasoning_effort"]
            return copy.deepcopy(self.config)
        raise AssertionError(f"Unexpected request {method}")


class ModalHarness(App):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.results = []

    def compose(self) -> ComposeResult:
        yield Static("Conversation draft stays here", id="draft")

    def on_mount(self):
        self.push_screen(self.dialog, self.results.append)


def configuration(**provider_fields):
    return AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, providers=[ProviderConfig(model="alpha", **provider_fields)])


def notice(screen):
    return str(screen.query_one(".form-status", Static).render())


@pytest.mark.parametrize("size", [(80, 24), (60, 24), (48, 12)])
async def test_model_picker_search_arrows_effort_and_apply(size):
    config = configuration(parameters={"temperature": 0.7, "reasoning_effort": "high"})
    client = ModelClient(config)
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert screen.query_one("#model-id", Input).value == "alpha"
        assert screen.query_one("#model-filter", Input).content_size.height == 1
        assert screen.query_one("#model-effort", Select).value == "high"
        assert screen.query_one("#model-context-size", Input).value == ""
        assert screen.query_one("#model-max-response", Input).value == "32768"
        assert "3 models available" in notice(screen)
        screen.query_one("#model-filter", Input).value = "reAson"
        await pilot.pause()
        options = screen.query_one("#model-options", OptionList)
        assert options.option_count == 1
        await pilot.press("enter")
        assert screen.query_one("#model-id", Input).value == "reasoner"
        screen.query_one("#model-effort", Select).value = "medium"
        await pilot.press("ctrl+s")
        await pilot.pause()
        result = app.results[0]
        provider = result["config"]["providers"][0]
        assert provider["model"] == "reasoner"
        assert provider["parameters"] == {"temperature": 0.7, "reasoning_effort": "medium",
                                          "max_completion_tokens": 32768}


async def test_filter_arrows_select_and_empty_match_has_manual_fallback():
    config = configuration()
    screen = ModelScreen(ModelClient(config), config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down", "enter")
        assert screen.query_one("#model-id", Input).value == "beta"
        screen.query_one("#model-filter", Input).value = "missing"
        await pilot.pause()
        options = screen.query_one("#model-options", OptionList)
        assert options.option_count == 1 and options.get_option_at_index(0).disabled
        screen.query_one("#model-id", Input).value = "manual/model"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.results[0]["config"]["providers"][0]["model"] == "manual/model"


@pytest.mark.parametrize("error", [None, "Model listing is unavailable"])
async def test_empty_inventory_keeps_saved_model_and_can_clear_effort(error):
    config = configuration(parameters={"reasoning_effort": "adaptive", "temperature": 0.2})
    client = ModelClient(config)
    client.models = []
    client.error = error
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.query_one("#model-options", OptionList).get_option_at_index(0).id == "alpha"
        assert screen.query_one("#model-effort", Select).value == "adaptive"
        assert "manual model ID" in notice(screen)
        screen.query_one("#model-effort", Select).value = ""
        await pilot.press("ctrl+s")
        await pilot.pause()
        provider = app.results[0]["config"]["providers"][0]
        assert provider["parameters"] == {"temperature": 0.2, "max_completion_tokens": 32768}
        assert client.calls[-1][1]["reasoning_effort"] is None


async def test_loading_is_nonblocking_cancel_restores_underlying_draft():
    config = configuration()
    client = ModelClient(config)
    client.gate = asyncio.Event()
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await client.started.wait()
        await pilot.pause()
        assert not screen.loading
        field = screen.query_one("#model-filter", Input)
        assert screen.get_widget_at(*field.region.center)[0] is field
        assert "Fetching available models" in notice(screen)
        await pilot.press("m", "a", "n", "u", "a", "l")
        assert field.value == "manual"
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert "Conversation draft stays here" in str(app.query_one("#draft", Static).render())
        assert not any(method == "select_model" for method, _ in client.calls)


async def test_discovery_error_and_refresh_preserve_manual_draft():
    config = configuration()
    client = ModelClient(config)
    client.failure = "Unknown service request"
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "eidolon service stop" in notice(screen)
        screen.query_one("#model-id", Input).value = "my/manual"
        client.failure = None
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert "3 models available" in notice(screen)
        assert screen.query_one("#model-id", Input).value == "my/manual"


async def test_switching_provider_restores_model_and_effort_drafts():
    config = configuration(parameters={"reasoning_effort": "high"})
    config.providers.append(ProviderConfig(name="other", model="other-model"))
    client = ModelClient(config)
    screen = ModelScreen(client, config, provider_name="other")
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.query_one("#model-id", Input).value == "other-model"
        screen.query_one("#model-id", Input).value = "draft-other"
        screen.query_one("#model-effort", Select).value = "max"
        await pilot.pause()
        screen.query_one("#model-context-size", Input).value = "262144"
        screen.query_one("#model-max-response", Input).value = "65536"
        screen.query_one("#model-provider", Select).value = "local"
        await pilot.pause()
        assert screen.query_one("#model-effort", Select).value == "high"
        screen.query_one("#model-provider", Select).value = "other"
        await pilot.pause()
        assert screen.query_one("#model-id", Input).value == "draft-other"
        assert screen.query_one("#model-effort", Select).value == "max"
        assert screen.query_one("#model-context-size", Input).value == "262144"
        assert screen.query_one("#model-max-response", Input).value == "65536"


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_model_token_settings_keyboard_order_and_save(size, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    config = configuration(context_window=131072, parameters={"max_tokens": 16384})
    client = ModelClient(config)
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        context = screen.query_one("#model-context-size", Input)
        response = screen.query_one("#model-max-response", Input)
        assert context.value == "131072" and response.value == "16384"
        screen.query_one("#model-effort", Select).focus()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is context
        assert context.region.overlaps(screen.query_one("#model-body").region)
        if screen.has_class("short"):
            assert screen.query_one(".form-dialog").border_title == "Context Size (tokens)"
        context.value = "262144"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is response
        assert response.region.overlaps(screen.query_one("#model-body").region)
        if screen.has_class("short"):
            assert screen.query_one(".form-dialog").border_title == "Max Response Length (tokens)"
        response.value = "65536"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.calls[-1] == ("select_model", {
            "name": "local", "model": "alpha", "reasoning_effort": None,
            "context_window": 262144, "max_response_tokens": 65536,
        })
        saved = app.results[0]["config"]["providers"][0]
        assert saved["context_window"] == 262144
        assert saved["parameters"] == {"max_tokens": 65536}


async def test_model_change_clears_context_override_and_cancel_keeps_token_draft():
    config = configuration(context_window=131072)
    client = ModelClient(config)
    state = ProviderMenuState()
    screen = ModelScreen(client, config, state=state)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen.choose_model("beta")
        await pilot.pause()
        assert screen.query_one("#model-context-size", Input).value == ""
        screen.query_one("#model-context-size", Input).value = "65536"
        screen.query_one("#model-max-response", Input).value = "8192"
        await pilot.press("escape")
        await pilot.pause()
        reopened = ModelScreen(client, config, state=state)
        app.push_screen(reopened)
        await pilot.pause()
        assert reopened.query_one("#model-id", Input).value == "beta"
        assert reopened.query_one("#model-context-size", Input).value == "65536"
        assert reopened.query_one("#model-max-response", Input).value == "8192"
        reopened.query_one("#model-context-size", Input).value = ""
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.calls[-1][1]["context_window"] is None
        assert client.calls[-1][1]["max_response_tokens"] == 8192


@pytest.mark.parametrize("field,value", [
    ("model-context-size", "0"), ("model-context-size", "-1"), ("model-context-size", "many"),
    ("model-max-response", "0"), ("model-max-response", "-1"),
    ("model-max-response", "many"), ("model-max-response", ""),
])
async def test_invalid_model_token_settings_show_inline_error(field, value):
    config = configuration()
    client = ModelClient(config)
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=(48, 12)) as pilot:
        await pilot.pause()
        screen.query_one("#" + field, Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.focused is screen.query_one("#" + field, Input)
        assert screen.query_one("#" + field + "-error").has_class("problem")
        assert not any(method == "select_model" for method, _ in client.calls)


async def test_auxiliary_picker_returns_role_settings_without_dialogue_save():
    config = configuration(context_window=1_000_000, parameters={"max_tokens": 65536})
    client = ModelClient(config)
    screen = ModelScreen(client, config, role="auxiliary", model="alpha")
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.query_one("#model-context-size", Input).value == ""
        assert screen.query_one("#model-max-response", Input).value == "4096"
        screen.query_one("#model-context-size", Input).value = "131072"
        screen.query_one("#model-max-response", Input).value = "8192"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.results[0]["model"] == "alpha"
        assert app.results[0]["context_window"] == 131072
        assert app.results[0]["max_output_tokens"] == 8192
        assert not any(method == "select_model" for method, _ in client.calls)


@pytest.mark.parametrize("value", ["127", "8193"])
async def test_auxiliary_picker_validates_role_response_bounds(value):
    config = configuration()
    client = ModelClient(config)
    screen = ModelScreen(client, config, role="auxiliary", model="alpha")
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen.query_one("#model-max-response", Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.results == []
        assert screen.query_one("#model-max-response-error").has_class("problem")
        assert app.focused is screen.query_one("#model-max-response", Input)


@pytest.mark.parametrize("key", ["secret-for-test", "env:REMOTE_API_KEY", "keyring:remote"])
async def test_connect_new_provider_discovers_with_blank_model_and_masked_key(key):
    config = configuration()
    client = ModelClient(config)
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        screen.query_one("#connect-provider", Select).value = NEW_PROVIDER
        await pilot.pause()
        screen.query_one("#connect-name", Input).value = "remote"
        screen.query_one("#connect-display", Input).value = "My API"
        screen.query_one("#connect-url", Input).value = "https://example.com/v1"
        field = screen.query_one("#connect-key", Input)
        field.value = key
        assert field.password
        await pilot.press("ctrl+s")
        await pilot.pause()
        method, params = client.calls[-1]
        assert method == "save_provider"
        assert params["provider"]["model"] == ""
        assert params["api_key"] == key
        result = app.results[0]
        assert result["provider"] == "remote"
        assert result["config"]["provider"] == "local"
        provider = next(p for p in result["config"]["providers"] if p["name"] == "remote")
        assert provider["display_name"] == "My API"
        assert provider["model"] == "alpha"
        assert key not in result["notice"]


async def test_connect_existing_preserves_profile_and_key_reference():
    config = configuration(
        api_key_ref="env:LOCAL_KEY",
        presentation="prefix",
        post_history="user",
        context_window=1_000_000,
        structured_verified=True,
        parameters={"temperature": 0.3},
        headers={"X-App": "Eidolon"},
    )
    client = ModelClient(config)
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.query_one("#connect-key", Input).value == ""
        assert screen.query_one("#connect-name", Input).disabled
        await pilot.press("ctrl+s")
        await pilot.pause()
        params = client.calls[-1][1]
        assert "api_key" not in params
        assert params["provider"] == config.providers[0].model_dump(mode="json")


@pytest.mark.parametrize("value", ["0", "unknown"])
async def test_connect_context_capacity_validates_then_saves(value):
    config = configuration()
    client = ModelClient(config)
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        screen.query_one("#connect-context", Input).value = value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert screen.query_one("#connect-context-error").has_class("problem")
        assert not screen.query_one("#connect-advanced", Collapsible).collapsed
        assert not app.results
        screen.query_one("#connect-context", Input).value = "1000000"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.calls[-1][1]["provider"]["context_window"] == 1_000_000


async def test_connect_url_derived_id_is_unique_and_fetch_chooses_first_model():
    config = configuration()
    config.providers.append(ProviderConfig(name="example-com", endpoint="https://example.com/v1"))
    client = ModelClient(config)
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen.query_one("#connect-provider", Select).value = NEW_PROVIDER
        await pilot.pause()
        assert app.focused is screen.query_one("#connect-url", Input)
        assert screen.query_one("#connect-url", Input).content_size.height == 1
        screen.query_one("#connect-url", Input).value = "https://example.com/v1"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert client.calls[-1][1]["provider"]["name"] == "example-com-2"
        assert screen.query_one("#connect-model", Input).value == "alpha"
        assert screen.query_one("#connect-model-options", OptionList).option_count == 3
        assert "Found 3 models" in notice(screen)


async def test_models_can_connect_new_provider_then_apply_without_losing_config():
    config = configuration()
    client = ModelClient(config)
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#model-connect")
        await pilot.pause()
        connection = app.screen
        assert isinstance(connection, ConnectScreen)
        connection.query_one("#connect-provider", Select).value = NEW_PROVIDER
        await pilot.pause()
        connection.query_one("#connect-url", Input).value = "https://remote.example/v1"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen
        assert screen.provider_name == "remote-example"
        assert screen.query_one("#model-provider", Select).value == "remote-example"
        assert screen.query_one("#model-id", Input).value == "alpha"
        assert len(screen.config.providers) == 2
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.results[0]["config"]["provider"] == "remote-example"


async def test_cached_inventory_is_visible_during_refresh_and_empty_model_is_rejected():
    config = configuration(models=["cached-model"])
    client = ModelClient(config)
    client.gate = asyncio.Event()
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await client.started.wait()
        options = screen.query_one("#model-options", OptionList)
        assert any(
            options.get_option_at_index(i).id == "cached-model" for i in range(options.option_count)
        )
        screen.query_one("#model-id", Input).value = ""
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "exact model ID" in notice(screen)
        assert not any(method == "select_model" for method, _ in client.calls)
        await pilot.press("escape")


async def test_unconfigured_failed_inventory_does_not_offer_placeholder_model():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"})
    client = ModelClient(config)
    client.models = ["configure-model"]
    client.error = "Cannot list models"
    screen = ModelScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.query_one("#model-id", Input).value == ""
        options = screen.query_one("#model-options", OptionList)
        assert options.option_count == 1
        assert options.get_option_at_index(0).disabled
        assert options.get_option_at_index(0).id is None
        await pilot.press("enter", "ctrl+s")
        await pilot.pause()
        assert app.results == []
        assert not any(method == "select_model" for method, _ in client.calls)


async def test_connect_fetch_fallback_and_failure_keep_secret_draft_masked():
    config = configuration()
    client = ModelClient(config)
    client.models = []
    client.error = "Unauthorized: secret-for-test"
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen.query_one("#connect-key", Input).value = "secret-for-test"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert "fallback model ID" in notice(screen)
        assert "secret-for-test" not in notice(screen)
        screen.query_one("#connect-model", Input).value = "fallback"
        client.failure = "Could not save secret-for-test"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.results == []
        assert screen.query_one("#connect-key", Input).value == "secret-for-test"
        assert screen.query_one("#connect-key", Input).password
        assert screen.query_one("#connect-model", Input).value == "fallback"
        assert "secret-for-test" not in notice(screen)
        assert not screen.busy


async def test_changed_endpoint_needs_reentered_key_and_authorized_headers():
    config = configuration(api_key_ref="env:SAVED_KEY", headers={"X-Token": "env:SAVED_TOKEN"})
    client = ModelClient(config)
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen.query_one("#connect-url", Input).value = "https://new.example/v1"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert client.calls == []
        assert "Re-enter the key" in notice(screen)
        screen.query_one("#connect-key", Input).value = "env:NEW_KEY"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert client.calls == []
        assert "credential headers" in notice(screen)
        screen.query_one("#connect-headers", TextArea).load_text('{"X-Token": "env:NEW_TOKEN"}')
        screen.query_one("#connect-authorize-headers", Checkbox).value = True
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        params = client.calls[-1][1]
        assert params["confirm_header_credentials"] is True
        assert params["api_key"] == "env:NEW_KEY"


async def test_advanced_headers_validate_without_showing_literal_secret():
    config = configuration()
    client = ModelClient(config)
    screen = ConnectScreen(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen.query_one("#connect-headers", TextArea).load_text(
            '{"Authorization": "literal-secret"}'
        )
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert client.calls == []
        assert "Credentials must use" in notice(screen)
        assert "literal-secret" not in notice(screen)


@pytest.mark.parametrize("kind,field_id", [
    (ConnectScreen, "connect-url"), (ModelScreen, "model-id"),
])
async def test_required_field_errors_appear_beside_the_field(kind, field_id):
    config = configuration()
    client = ModelClient(config)
    screen = kind(client, config)
    app = ModalHarness(screen)
    async with app.run_test(size=(48, 12)) as pilot:
        await pilot.pause()
        control = screen.query_one("#" + field_id, Input)
        control.value = ""
        await pilot.press("ctrl+s")
        await pilot.pause()
        error = screen.query_one(f"#{field_id}-error", Static)
        assert error.has_class("problem")
        assert str(error.render()).startswith("Error:")
        assert error.region.overlaps(screen.query_one(".form-body").region)
        assert not screen.query_one(".form-status").display
        assert app.focused is control
        assert not any(method in {"save_provider", "select_model"} for method, _ in client.calls)


async def test_advanced_validation_opens_and_focuses_headers_then_clears_after_correction():
    config = configuration()
    screen = ConnectScreen(ModelClient(config), config)
    app = ModalHarness(screen)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        headers = screen.query_one("#connect-headers", TextArea)
        headers.load_text("invalid JSON")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not screen.query_one("#connect-advanced", Collapsible).collapsed
        assert app.focused is headers
        assert screen.query_one("#connect-headers-error").display
        headers.load_text("{}")
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert not screen.query_one("#connect-headers-error").display
        assert screen.query_one(".form-status").display


@pytest.mark.parametrize("kind,select_id", [
    (ConnectScreen, "connect-provider"), (ModelScreen, "model-effort"),
])
async def test_escape_collapses_dropdown_before_closing_menu(kind, select_id):
    config = configuration()
    screen = kind(ModelClient(config), config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        selector = screen.query_one("#" + select_id, Select)
        selector.focus()
        await pilot.press("enter")
        assert selector.expanded
        await pilot.press("escape")
        assert not selector.expanded
        assert app.results == []
        assert app.focused is selector
        await pilot.press("escape")
        assert app.results == [None]


@pytest.mark.parametrize("kind", [ConnectScreen, ModelScreen])
async def test_too_small_resize_and_cancel(kind):
    config = configuration()
    screen = kind(ModelClient(config), config)
    app = ModalHarness(screen)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        assert screen.has_class("too-small")
        assert screen.query_one(".form-too-small", Static).display
        assert not screen.query_one(".form-dialog").display
        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        assert not screen.has_class("too-small")
        assert screen.query_one(".form-dialog").display
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]


@pytest.mark.parametrize("kind,action", [(ConnectScreen, "ctrl+s"), (ModelScreen, "ctrl+s")])
async def test_committing_cannot_claim_cancelled(kind, action):
    config = configuration()
    client = ModelClient(config)
    screen = kind(client, config)
    app = ModalHarness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        client.started.clear()
        client.gate = asyncio.Event()
        await pilot.press(action)
        await client.started.wait()
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == []
        assert "Saving is in progress" in notice(screen)
        cancel_id = "#connect-cancel" if kind is ConnectScreen else "#model-cancel"
        assert screen.query_one(cancel_id, Button).disabled
        client.gate.set()
        await pilot.pause()
        assert len(app.results) == 1 and app.results[0] is not None
