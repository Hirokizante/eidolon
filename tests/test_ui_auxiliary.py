import pytest
from textual.widgets import Button, Checkbox, Collapsible, ContentSwitcher, Input, Select, Static
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.models import ModelScreen
from eidolon.ui.preferences import PreferencesScreen
from test_ui import FakeClient


class AuxiliaryClient(FakeClient):
    test_result = {"ok": True, "notice": "Contract passed (prompt JSON). "
                   "Quality and enforcement untested."}

    async def request(self, method, **params):
        if method == "test_auxiliary_model":
            self.calls.append((method, params))
            return self.test_result
        if method == "list_models":
            self.calls.append((method, params))
            return {"models": [], "error": "Offline; use a manual model ID."}
        return await super().request(method, **params)


async def open_picker(app, pilot) -> ModelScreen:
    app.screen.query_one("#aux-choose", Button).focus()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, ModelScreen)
    return app.screen


@pytest.mark.parametrize("size", [(140, 40), (80, 24), (60, 18), (48, 12)])
async def test_auxiliary_keyboard_drilldown_manual_model_and_draft_restore(size, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = AuxiliaryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("A draft 🌸 漢字 é")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#pref-follow_replies", Checkbox).value = False
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        aux = prefs
        assert app.screen is prefs and isinstance(prefs, PreferencesScreen)
        assert not prefs.query("#preferences-auxiliary")
        await pilot.press("tab")
        assert app.focused is aux.query_one("#aux-mode", Select)
        assert aux.has_class("ascii") and aux.has_class("monochrome")
        assert aux.query_one("#aux-test", Button).display
        aux.query_one("#aux-mode", Select).value = "separate"
        await pilot.pause()
        aux.query_one("#aux-choose", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, ModelScreen) and picker.role == "auxiliary"
        assert not prefs.query_one(".form-dialog").display
        picker.query_one("#model-id", Input).value = "synthetic/manual-aux"
        await pilot.pause()
        picker.query_one("#model-effort", Select).value = "medium"
        picker.query_one("#model-effort", Select).focus()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is picker.query_one("#model-context-size", Input)
        picker.query_one("#model-context-size", Input).value = "262144"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is picker.query_one("#model-max-response", Input)
        picker.query_one("#model-max-response", Input).value = "8192"
        if picker.has_class("short"):
            assert picker.query_one(".form-dialog").border_title == "Max Response Length (tokens)"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is aux
        assert "synthetic/manual-aux" in str(aux.query_one("#aux-target", Static).render())
        assert not any(method == "select_model" for method, _ in client.calls)
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer and composer.selection == selection
        assert app.config.auxiliary.mode == "inherit"
        assert not any(method.startswith("save_") for method, _ in client.calls)
        app.open_preferences()
        await pilot.pause()
        aux = app.screen
        assert aux.query_one("#aux-mode", Select).value == "separate"
        assert not aux.query_one("#pref-follow_replies", Checkbox).value
        assert "synthetic/manual-aux" in str(aux.query_one("#aux-target", Static).render())
        for button in aux.query(".form-actions Button"):
            assert button.region.bottom <= aux.query_one(".form-dialog").content_region.bottom
            assert button.region.right <= aux.query_one(".form-dialog").content_region.right
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert app.config.auxiliary.model == "synthetic/manual-aux"
        assert app.config.auxiliary.context_window == 262144
        assert app.config.auxiliary.max_output_tokens == 8192
        assert app.config.auxiliary.reasoning_effort == "medium"
        assert not app.config.preferences.follow_replies
        assert app.config.selected_provider().model == "configure-model"
        assert app.focused is composer and composer.selection == selection
        assert composer.text == "A draft 🌸 漢字 é"
        saves = [(method, params) for method, params in client.calls if method.startswith("save_")]
        assert saves == [("save_preferences_with_auxiliary", {
            "preferences": {"follow_replies": False},
            "auxiliary": {"mode": "separate", "provider": "local", "model": "synthetic/manual-aux",
                          "context_window": 262144, "max_output_tokens": 8192,
                          "reasoning_effort": "medium"},
        })]


@pytest.mark.parametrize("dialogue_effort", [None, "high", "null"])
async def test_inherited_auxiliary_picker_edits_limits_without_changing_binding(dialogue_effort):
    client = AuxiliaryClient()
    profile = client.configuration["providers"][0]
    profile["model"] = "synthetic/dialogue"
    if dialogue_effort:
        profile["parameters"]["reasoning_effort"] = None if dialogue_effort == "null" else dialogue_effort
    client.configuration["auxiliary"].update(
        provider="local", model="synthetic/saved-auxiliary", reasoning_effort="medium",
        context_window=131072, max_output_tokens=8192,
    )
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        assert not prefs.query("#aux-context_window, #aux-max_output_tokens, #aux-reasoning_effort")
        for selector in ("#aux-context_chars", "#aux-timeout_seconds", "#aux-thinking_mode",
                         "#aux-output_token_field"):
            assert prefs.query(selector)
        picker = await open_picker(app, pilot)
        assert picker.query_one("#model-id", Input).value == "synthetic/dialogue"
        assert picker.query_one("#model-context-size", Input).value == "131072"
        assert picker.query_one("#model-context-size", Input).placeholder == "Blank follows dialogue context window"
        assert picker.query_one("#model-max-response", Input).value == "8192"
        for selector in ("#model-provider", "#model-filter", "#model-options", "#model-id",
                         "#model-effort", "#model-connect"):
            assert picker.query_one(selector).disabled
        assert picker.query_one("#model-effort", Select).value == "medium"
        picker.query_one("#model-context-size", Input).value = ""
        picker.query_one("#model-max-response", Input).value = "4096"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is prefs
        assert prefs.query_one("#aux-mode", Select).value == "inherit"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.auxiliary.context_window is None
        assert app.config.auxiliary.max_output_tokens == 4096
        assert app.config.auxiliary.model == "synthetic/saved-auxiliary"
        assert app.config.auxiliary.reasoning_effort == "medium"
        assert app.config.selected_provider().model == "synthetic/dialogue"
        assert app.config.selected_provider().parameters == profile["parameters"]
        assert not any(method == "select_model" for method, _ in client.calls)


async def test_auxiliary_picker_cancel_restores_unsaved_model_settings():
    client = AuxiliaryClient()
    client.configuration["auxiliary"].update(
        mode="separate", provider="local", model="synthetic/auxiliary",
        context_window=65536, max_output_tokens=2048,
    )
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        picker = await open_picker(app, pilot)
        picker.query_one("#model-context-size", Input).value = "131072"
        picker.query_one("#model-max-response", Input).value = "8192"
        picker.query_one("#model-effort", Select).value = "high"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is prefs
        assert prefs.auxiliary.read_values()["max_output_tokens"] == 2048
        picker = await open_picker(app, pilot)
        assert picker.query_one("#model-context-size", Input).value == "131072"
        assert picker.query_one("#model-max-response", Input).value == "8192"
        assert picker.query_one("#model-effort", Select).value == "high"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert prefs.auxiliary.read_values()["max_output_tokens"] == 8192
        assert app.config.auxiliary.max_output_tokens == 2048
        assert not any(method.startswith("save_") for method, _ in client.calls)


async def test_new_auxiliary_target_keeps_reentered_context_size():
    client = AuxiliaryClient()
    client.configuration["auxiliary"].update(
        mode="separate", provider="local", model="synthetic/old-auxiliary", context_window=131072,
    )
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        picker = await open_picker(app, pilot)
        picker.query_one("#model-id", Input).value = "synthetic/new-auxiliary"
        await pilot.pause()
        assert picker.query_one("#model-context-size", Input).value == ""
        picker.query_one("#model-context-size", Input).value = "131072"
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        saves = [(method, params) for method, params in client.calls if method.startswith("save_")]
        assert saves == [("save_preferences_with_auxiliary", {
            "preferences": {},
            "auxiliary": {"model": "synthetic/new-auxiliary", "context_window": 131072},
        })]
        assert app.config.auxiliary.context_window == 131072
        assert app.config.auxiliary.model == "synthetic/new-auxiliary"


async def test_blank_auxiliary_input_allowance_clears_saved_limit():
    client = AuxiliaryClient()
    client.configuration["auxiliary"]["context_chars"] = 32_000
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        app.screen.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        allowance = app.screen.query_one("#aux-context_chars", Input)
        assert allowance.value == "32000"
        app.screen.query_one("#aux-advanced", Collapsible).collapsed = False
        allowance.focus()
        allowance.value = ""
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert not isinstance(app.screen, PreferencesScreen)
        assert app.config.auxiliary.context_chars is None
        saves = [(method, params) for method, params in client.calls if method.startswith("save_")]
        assert saves == [("save_preferences_with_auxiliary", {
            "preferences": {}, "auxiliary": {"context_chars": None},
        })]
        app.open_preferences()
        await pilot.pause()
        allowance = app.screen.query_one("#aux-context_chars", Input)
        assert allowance.value == ""
        assert allowance.placeholder == "Blank: unlimited"


async def test_auxiliary_resize_floor_validation_and_synthetic_test():
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=AuxiliaryClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        app.screen.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        aux = app.screen
        aux.query_one("#aux-context_chars", Input).value = "7"
        aux.query_one("#preference-category", Select).value = "conversation"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert aux.query_one("#aux-context_chars-error").has_class("problem")
        assert aux.query_one("#preference-pages", ContentSwitcher).current == "preference-auxiliary"
        assert not aux.query_one("#aux-advanced", Collapsible).collapsed
        assert app.focused is aux.query_one("#aux-context_chars", Input)
        assert aux.query_one("#aux-context_chars-error").region.overlaps(
            aux.query_one("#preference-auxiliary").region)
        assert not any(method.startswith("save_") for method, _ in app.client.calls)
        aux.query_one("#aux-context_chars", Input).value = "32000"
        aux.query_one("#aux-context_chars", Input).focus()
        for size in [(60, 18), (48, 12), (40, 10), (80, 24)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert aux.query_one(".form-too-small").display == (size[0] < 48)
        assert aux.query_one("#aux-context_chars", Input).value == "32000"
        picker = await open_picker(app, pilot)
        picker.query_one("#model-max-response", Input).value = "8192"
        await pilot.press("ctrl+s")
        await pilot.pause()
        aux.query_one("#aux-thinking_mode", Input).value = "disabled"
        aux.query_one("#aux-test", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "passed" in str(aux.query_one(".form-status", Static).render())
        assert aux.query_one(".form-status", Static).display
        assert app.client.calls[-1][1]["auxiliary"]["max_output_tokens"] == 8192
        assert app.client.calls[-1][1]["auxiliary"]["thinking_mode"] == "disabled"
        assert not any(method.startswith("save_") for method, _ in app.client.calls)
        assert app.screen is aux
        assert app.config.auxiliary.max_output_tokens == 4096


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
@pytest.mark.parametrize("connected", [True, False])
async def test_contract_feedback_preserves_preference_and_composer_drafts(size, connected, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = AuxiliaryClient()
    notice = ("Connected; contract failed. Check model options and output limits."
              if connected else "No provider response. Check connection, model options and capacity.")
    client.test_result = {"ok": False, "connected": connected,
                          "contract_compatible": False, "notice": notice, "output_mode": "prompt"}
    app = RoomApp(AppConfig(), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Draft 漢字 🌸 é")
        composer.selection = Selection((0, 1), (0, 4))
        selection = composer.selection
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        prefs.query_one("#aux-timeout_seconds", Input).value = "45"
        prefs.query_one("#aux-test", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        status = prefs.query_one(".form-status", Static)
        assert notice in str(status.render()) and status.has_class("problem")
        dialog = prefs.query_one(".form-dialog")
        assert status.display or notice in str(dialog.border_subtitle)
        for button in prefs.query(".form-actions Button"):
            if button.display:
                assert button.region.bottom <= dialog.content_region.bottom
        assert app.screen is prefs and app.focused is prefs.query_one("#aux-test", Button)
        assert prefs.query_one("#aux-timeout_seconds", Input).value == "45"
        assert not any(method.startswith("save_") for method, _ in client.calls)
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer and composer.text == "Draft 漢字 🌸 é"
        assert composer.selection == selection
        app.open_preferences()
        await pilot.pause()
        assert float(app.screen.query_one("#aux-timeout_seconds", Input).value) == 45


async def test_discard_restores_auxiliary_and_other_preferences_from_saved_configuration():
    client = AuxiliaryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#pref-follow_replies", Checkbox).value = False
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        await pilot.pause()
        picker = await open_picker(app, pilot)
        picker.query_one("#model-max-response", Input).value = "8192"
        await pilot.press("ctrl+s")
        await pilot.pause()
        prefs.query_one("#aux-mode", Select).value = "disabled"
        await pilot.pause()
        assert prefs.query_one("#aux-test", Button).disabled
        assert "Unsaved changes" in str(prefs.query_one(".form-status", Static).render())

        client.configuration["auxiliary"].update(
            mode="separate", provider="local", model="synthetic/changed-elsewhere",
            max_output_tokens=2048,
        )
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert prefs.query_one("#pref-follow_replies", Checkbox).value
        assert prefs.query_one("#aux-mode", Select).value == "separate"
        assert prefs.auxiliary.read_values()["max_output_tokens"] == 2048
        assert "synthetic/changed-elsewhere" in str(prefs.query_one("#aux-target", Static).render())
        assert not prefs.query_one("#aux-test", Button).disabled
        assert app.preferences_state.draft is None and app.preferences_state.auxiliary.draft is None
        prefs.query_one("#preference-category", Select).value = "conversation"
        await pilot.pause()
        assert not prefs.query_one("#aux-test", Button).display
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert not any(method.startswith("save_") for method, _ in client.calls)


async def test_auxiliary_draft_preserves_settings_changed_elsewhere():
    client = AuxiliaryClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        prefs.query_one("#aux-timeout_seconds", Input).value = "90"
        await pilot.press("escape")
        client.configuration["auxiliary"].update(
            mode="separate", provider="local", model="synthetic/changed-elsewhere",
            max_output_tokens=2048,
        )
        app.refresh_data()
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        assert prefs.query_one("#aux-mode", Select).value == "separate"
        assert prefs.auxiliary.read_values()["max_output_tokens"] == 2048
        assert float(prefs.query_one("#aux-timeout_seconds", Input).value) == 90
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.auxiliary.model == "synthetic/changed-elsewhere"
        assert app.config.auxiliary.max_output_tokens == 2048
        assert app.config.auxiliary.timeout_seconds == 90
        saves = [(method, params) for method, params in client.calls if method.startswith("save_")]
        assert saves == [("save_preferences_with_auxiliary", {
            "preferences": {}, "auxiliary": {"timeout_seconds": 90.0},
        })]


async def test_unsupported_combined_save_retains_both_drafts_for_retry():
    class FailingClient(AuxiliaryClient):
        fail = True

        async def request(self, method, **params):
            if method == "save_preferences_with_auxiliary" and self.fail:
                self.calls.append((method, params))
                raise RuntimeError("Unknown service request")
            return await super().request(method, **params)

    client = FailingClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_preferences()
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#pref-follow_replies", Checkbox).value = False
        prefs.query_one("#preference-category", Select).value = "auxiliary"
        prefs.query_one("#aux-mode", Select).value = "disabled"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is prefs
        assert prefs.query_one(".form-status").has_class("problem")
        assert "service needs a restart" in str(prefs.query_one(".form-status", Static).render())
        assert not prefs.query_one("#preferences-apply", Button).disabled
        assert prefs.query_one("#aux-test", Button).disabled
        assert client.configuration["preferences"]["follow_replies"]
        assert client.configuration["auxiliary"]["mode"] == "inherit"
        assert not any(method == "save_preferences" for method, _ in client.calls)

        await pilot.press("escape", "alt+6")
        await pilot.pause()
        prefs = app.screen
        assert not prefs.query_one("#pref-follow_replies", Checkbox).value
        assert prefs.query_one("#aux-mode", Select).value == "disabled"
        client.fail = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert not app.config.preferences.follow_replies
        assert app.config.auxiliary.mode == "disabled"
        assert app.preferences_state.draft is None and app.preferences_state.auxiliary.draft is None
