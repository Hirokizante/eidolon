import pytest
from textual.widgets import Button, Checkbox, Collapsible, ContentSwitcher, Input, Select, Static
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig, AuxiliaryModelConfig, SEARCH_PROVIDERS
from eidolon.preferences import merge_preferences, merge_vision_model, merge_web_search
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.preferences import GROUPS, PreferencesScreen
from test_ui import FakeClient


class SearchPreferencesClient(FakeClient):
    fail_save = False

    async def request(self, method, **params):
        if method == "save_preferences_with_web_search":
            self.calls.append((method, params))
            if self.fail_save:
                raise RuntimeError("Service unavailable; retry saving.")
            config = merge_preferences(
                AppConfig.model_validate(self.configuration), params["preferences"]
            )
            if "auxiliary" in params:
                config.auxiliary = AuxiliaryModelConfig.model_validate(
                    {
                        **config.auxiliary.model_dump(),
                        **params["auxiliary"],
                    }
                )
            if "vision" in params:
                config = merge_vision_model(config, params["vision"])
            config, keys = merge_web_search(
                config, params["web_search"], params.get("web_search_api_keys")
            )
            for name in keys:
                getattr(config.web_search, name).api_key_ref = "keyring:test-search-" + name
            self.configuration = config.model_dump(mode="json")
            return self.configuration
        if method == "test_web_search":
            self.calls.append((method, params))
            return {"provider": params["web_search"]["provider"], "results": 5}
        return await super().request(method, **params)


async def open_web(app, pilot, provider="tavily"):
    app.open_preferences()
    await pilot.pause()
    screen = app.screen
    screen.query_one("#preference-category", Select).value = "web"
    screen.query_one("#web-provider", Select).value = provider
    await pilot.pause()
    return screen


@pytest.mark.parametrize(
    "provider,field,value",
    [
        ("tavily", "depth", "advanced"),
        ("brave", "freshness", "pw"),
        ("exa", "search_type", "fast"),
        ("perplexity", "max_tokens", "8000"),
        ("searxng", "base_url", "http://127.0.0.1:9090/search-instance"),
    ],
)
async def test_preferences_select_provider_save_key_and_settings_and_keep_chat_draft(
    provider, field, value
):
    client = SearchPreferencesClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Draft 🌸 漢字 é")
        composer.selection = Selection((0, 1), (0, 4))
        selection = composer.selection
        screen = await open_web(app, pilot, provider)
        assert GROUPS["avatar"] == "Avatar" and GROUPS["web"] == "Web search"
        assert screen.query_one("#pref-avatar_enabled") in screen.query_one(
            "#preference-avatar"
        ).query(Checkbox)
        key = screen.query_one(f"#web-{provider}-key", Input)
        key.value = "synthetic-test-value"
        assert key.password
        control = screen.query_one(f"#web-{provider}-{field}")
        control.value = value
        screen.query_one(f"#web-{provider}-max_results", Input).value = "9"
        screen.query_one(f"#web-{provider}-timeout_seconds", Input).value = "45"
        screen.query_one(f"#web-{provider}-max_content_chars", Input).value = "2400"
        screen.query_one("#pref-follow_replies", Checkbox).value = False
        key.focus()
        await pilot.pause()
        assert "synthetic-test-value" not in app.export_screenshot()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert app.config.web_search.provider == provider
        settings = app.config.web_search.selected()
        assert getattr(settings, field) == (int(value) if field == "max_tokens" else value)
        assert settings.api_key_ref == "keyring:test-search-" + provider
        assert settings.max_results == 9 and settings.timeout_seconds == 45
        assert settings.max_content_chars == 2400 and not app.config.preferences.follow_replies
        assert app.focused is composer and composer.text == "Draft 🌸 漢字 é"
        assert composer.selection == selection
        assert app.preferences_state.web.draft is None and not app.preferences_state.web.keys
        assert "synthetic-test-value" not in repr(client.configuration)
        saves = [(method, params) for method, params in client.calls if method.startswith("save_")]
        assert len(saves) == 1 and saves[0][0] == "save_preferences_with_web_search"
        assert saves[0][1]["preferences"] == {"follow_replies": False}
        assert saves[0][1]["web_search_api_keys"] == {provider: "synthetic-test-value"}


async def test_switching_provider_and_closing_keeps_each_draft_and_discard_clears_keys():
    client = SearchPreferencesClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = await open_web(app, pilot, "brave")
        screen.query_one("#web-brave-key", Input).value = "synthetic-brave-value"
        screen.query_one("#web-brave-country", Input).value = "PH"
        screen.query_one("#web-provider", Select).value = "exa"
        screen.query_one("#web-exa-key", Input).value = "env:EXA_TEST_KEY"
        screen.query_one("#web-exa-search_type", Select).value = "fast"
        await pilot.press("escape")
        await pilot.pause()
        assert not any(method.startswith("save_") for method, _ in client.calls)
        assert "synthetic-brave-value" not in repr(app.preferences_state.web)
        app.open_preferences()
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#web-provider", Select).value == "exa"
        assert screen.query_one("#web-exa-key", Input).value == "env:EXA_TEST_KEY"
        screen.query_one("#web-provider", Select).value = "brave"
        await pilot.pause()
        assert screen.query_one("#web-brave-key", Input).value == "synthetic-brave-value"
        assert screen.query_one("#web-brave-country", Input).value == "PH"
        client.configuration["web_search"]["provider"] = "searxng"
        client.configuration["web_search"]["searxng"]["base_url"] = "http://localhost:9000"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert screen.query_one("#web-provider", Select).value == "searxng"
        assert screen.query_one("#web-searxng-base_url", Input).value == "http://localhost:9000"
        assert all(
            not screen.query_one(f"#web-{name}-key", Input).value for name in SEARCH_PROVIDERS
        )
        assert not app.preferences_state.web.keys and app.preferences_state.web.draft is None
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert not any(method.startswith("save_") for method, _ in client.calls)


@pytest.mark.parametrize(
    "provider,field,value",
    [
        ("tavily", "max_results", "0"),
        ("brave", "timeout_seconds", "0"),
        ("brave", "freshness", "yesterday"),
        ("exa", "start_date", "2026-02-30"),
        ("perplexity", "language_filter", "english"),
        ("searxng", "base_url", "file:///tmp/search"),
    ],
)
async def test_invalid_web_settings_reveal_provider_and_field_even_from_another_category(
    provider, field, value
):
    client = SearchPreferencesClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        screen = await open_web(app, pilot)
        screen.query_one(f"#web-{provider}-{field}", Input).value = value
        screen.query_one("#preference-category", Select).value = "conversation"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen
        assert screen.query_one("#preference-pages", ContentSwitcher).current == "preference-web"
        assert screen.query_one("#web-provider", Select).value == provider
        assert screen.query_one(f"#web-{provider}-{field}-error").has_class("problem")
        assert app.focused is screen.query_one(f"#web-{provider}-{field}")
        assert not any(method.startswith("save_") for method, _ in client.calls)
        if field in {"timeout_seconds", "freshness", "start_date", "language_filter"}:
            assert not screen.query_one(f"#web-{provider}-advanced", Collapsible).collapsed


async def test_key_removal_reference_entry_and_connection_test_do_not_save_unrelated_edits():
    client = SearchPreferencesClient()
    client.configuration["web_search"]["brave"]["api_key_ref"] = "env:OLD_TEST_KEY"
    config = AppConfig.model_validate(client.configuration)
    app = RoomApp(config, client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = await open_web(app, pilot, "brave")
        assert screen.query_one("#web-brave-key", Input).value == ""
        screen.query_one("#web-brave-remove-key", Checkbox).value = True
        screen.query_one("#web-brave-key", Input).value = "env:NEW_TEST_KEY"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert screen.query_one("#web-brave-key-error").has_class("problem")
        screen.query_one("#web-brave-remove-key", Checkbox).value = False
        screen.query_one("#pref-follow_replies", Checkbox).value = False
        screen.query_one("#web-test", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "passed" in str(screen.query_one(".form-status", Static).render())
        assert "unsaved" in str(screen.query_one(".form-status", Static).render())
        assert not any(method.startswith("save_") for method, _ in client.calls)
        test = next(params for method, params in client.calls if method == "test_web_search")
        assert test["web_search"] == {"provider": "brave"}
        assert test["web_search_api_keys"] == {"brave": "env:NEW_TEST_KEY"}
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.web_search.brave.api_key_ref == "env:NEW_TEST_KEY"
        assert not app.config.preferences.follow_replies
        screen = await open_web(app, pilot, "brave")
        screen.query_one("#web-brave-remove-key", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.web_search.brave.api_key_ref is None


async def test_search_save_failure_preserves_key_draft_for_retry():
    client = SearchPreferencesClient()
    client.fail_save = True
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = await open_web(app, pilot, "exa")
        screen.query_one("#web-exa-key", Input).value = "synthetic-test-value"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is screen and screen.query_one(".form-status").has_class("problem")
        await pilot.press("escape")
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        assert app.screen.query_one("#web-exa-key", Input).value == "synthetic-test-value"
        client.fail_save = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.web_search.provider == "exa"
        assert not app.preferences_state.web.keys


async def test_web_search_applies_together_with_other_preference_sections():
    client = SearchPreferencesClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = await open_web(app, pilot, "perplexity")
        screen.query_one("#web-perplexity-key", Input).value = "env:PERPLEXITY_TEST_KEY"
        screen.query_one("#aux-mode", Select).value = "disabled"
        screen.query_one("#vision-timeout_seconds", Input).value = "90"
        screen.query_one("#pref-avatar_enabled", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert app.config.web_search.provider == "perplexity"
        assert app.config.auxiliary.mode == "disabled" and app.config.vision.timeout_seconds == 90
        assert app.config.avatar.enabled
        saves = [params for method, params in client.calls if method.startswith("save_")]
        assert len(saves) == 1
        assert saves[0]["auxiliary"] == {"mode": "disabled"}
        assert saves[0]["vision"] == {"timeout_seconds": 90}


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_web_preferences_keyboard_resize_and_monochrome(size, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=SearchPreferencesClient())
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = await open_web(app, pilot, "searxng")
        screen.query_one("#web-provider", Select).focus()
        await pilot.press("tab")
        assert app.focused is screen.query_one("#web-searxng-key", Input)
        await pilot.press("tab", "tab")
        assert app.focused is screen.query_one("#web-searxng-max_results", Input)
        await pilot.press("tab")
        assert app.focused is screen.query_one("#web-searxng-base_url", Input)
        await pilot.press("tab", "enter")
        assert not screen.query_one("#web-searxng-advanced", Collapsible).collapsed
        await pilot.press("tab")
        assert app.focused is screen.query_one("#web-searxng-timeout_seconds", Input)
        for _ in range(3):
            screen.reveal_focus()
        await pilot.pause()
        assert screen.query_one("#web-searxng-timeout_seconds").region.overlaps(
            screen.query_one("#preference-web").region
        )
        if size[1] < 18:
            dialog_title = str(screen.query_one(".form-dialog").border_title)
            assert "Timeout in seconds" in dialog_title and "SearXNG" in dialog_title
        assert screen.has_class("monochrome") and screen.has_class("ascii")
        dialog = screen.query_one(".form-dialog")
        for button in screen.query(".form-actions Button"):
            if button.display:
                assert button.region.bottom <= dialog.content_region.bottom
                assert button.region.right <= dialog.content_region.right
        await pilot.resize_terminal(40, 10)
        await pilot.pause()
        assert screen.query_one(".form-too-small").display
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert app.focused is screen.query_one("#web-searxng-timeout_seconds", Input)
        assert screen.query_one("#web-searxng-timeout_seconds").region.overlaps(
            screen.query_one("#preference-web").region
        )
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.focused, Composer)
