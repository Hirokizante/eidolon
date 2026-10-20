import pytest
from textual.widgets import Button, Checkbox, Collapsible, ContentSwitcher, Input, Select, Static
from textual.widgets.text_area import Selection

from eidolon.config import AppConfig, AuxiliaryModelConfig
from eidolon.preferences import merge_preferences, merge_vision_model
from eidolon.ui.app import Composer, RoomApp
from eidolon.ui.models import ModelScreen
from eidolon.ui.preferences import PreferencesScreen
from test_ui import FakeClient


class VisionPreferencesClient(FakeClient):
    fail_save = False

    async def request(self, method, **params):
        if method == "save_preferences_with_vision":
            self.calls.append((method, params))
            if self.fail_save:
                raise RuntimeError("Unknown service request")
            config = merge_preferences(AppConfig.model_validate(self.configuration),
                                       params["preferences"])
            config = merge_vision_model(config, params["vision"])
            if "auxiliary" in params:
                config.auxiliary = AuxiliaryModelConfig.model_validate({
                    **config.auxiliary.model_dump(), **params["auxiliary"],
                })
            self.configuration = config.model_dump(mode="json")
            return self.configuration
        if method == "test_vision_model":
            self.calls.append((method, params))
            return {"ok": True, "notice": "Synthetic vision test passed."}
        if method == "list_models":
            self.calls.append((method, params))
            return {"models": [], "error": "Offline; use a manual model ID."}
        return await super().request(method, **params)


async def open_vision(app, pilot):
    app.open_preferences()
    await pilot.pause()
    app.screen.query_one("#preference-category", Select).value = "vision"
    await pilot.pause()
    return app.screen


@pytest.mark.parametrize("size", [(80, 24), (60, 18), (48, 12)])
async def test_vision_preferences_keyboard_picker_drafts_and_atomic_save(size, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("EIDOLON_ASCII", "1")
    client = VisionPreferencesClient()
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Draft 🌸 漢字 é")
        composer.selection = Selection((0, 2), (0, 5))
        selection = composer.selection
        prefs = await open_vision(app, pilot)
        await pilot.press("tab")
        assert app.focused is prefs.query_one("#vision-mode", Select)
        assert prefs.has_class("ascii") and prefs.has_class("monochrome")
        assert prefs.query_one("#vision-test", Button).disabled
        assert not prefs.query_one("#vision-choose", Button).display
        prefs.query_one("#vision-mode", Select).value = "separate"
        await pilot.pause()
        prefs.query_one("#vision-choose", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, ModelScreen) and picker.role == "vision"
        assert not prefs.query_one(".form-dialog").display
        assert picker.query_one("#model-max-response", Input).value == "4096"
        picker.query_one("#model-id", Input).value = "synthetic/vision"
        await pilot.pause()
        picker.query_one("#model-context-size", Input).value = "131072"
        picker.query_one("#model-max-response", Input).value = "2048"
        picker.query_one("#model-effort", Select).value = "low"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is prefs
        assert "synthetic/vision" in str(prefs.query_one("#vision-target", Static).render())
        assert not any(method == "select_model" for method, _ in client.calls)
        prefs.query_one("#pref-follow_replies", Checkbox).value = False
        prefs.query_one("#aux-mode", Select).value = "disabled"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer and composer.selection == selection
        assert app.config.vision.mode == "disabled"
        prefs = await open_vision(app, pilot)
        assert prefs.query_one("#vision-mode", Select).value == "separate"
        assert "synthetic/vision" in str(prefs.query_one("#vision-target", Static).render())
        for button in prefs.query(".form-actions Button"):
            if button.display:
                assert button.region.bottom <= prefs.query_one(".form-dialog").content_region.bottom
                assert button.region.right <= prefs.query_one(".form-dialog").content_region.right
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, PreferencesScreen)
        assert app.config.vision.model == "synthetic/vision"
        assert app.config.vision.context_window == 131072
        assert app.config.vision.max_output_tokens == 2048
        assert app.config.vision.reasoning_effort == "low"
        assert not app.config.preferences.follow_replies
        assert app.config.auxiliary.mode == "disabled"
        assert app.config.selected_provider().model == "configure-model"
        assert composer.text == "Draft 🌸 漢字 é" and composer.selection == selection
        saves = [(method, params) for method, params in client.calls if method.startswith("save_")]
        assert saves == [("save_preferences_with_vision", {
            "preferences": {"follow_replies": False}, "auxiliary": {"mode": "disabled"},
            "vision": {"mode": "separate", "provider": "local", "model": "synthetic/vision",
                       "context_window": 131072, "max_output_tokens": 2048,
                       "reasoning_effort": "low"},
        })]


async def test_vision_preferences_validation_test_discard_and_resize():
    client = VisionPreferencesClient()
    client.configuration["vision"].update(
        mode="separate", provider="local", model="synthetic/vision",
    )
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        prefs = await open_vision(app, pilot)
        prefs.query_one("#vision-timeout_seconds", Input).value = "0"
        prefs.query_one("#preference-category", Select).value = "conversation"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert prefs.query_one("#preference-pages", ContentSwitcher).current == "preference-vision"
        assert prefs.query_one("#vision-timeout_seconds-error").has_class("problem")
        assert not prefs.query_one("#vision-advanced", Collapsible).collapsed
        assert app.focused is prefs.query_one("#vision-timeout_seconds", Input)
        prefs.query_one("#vision-timeout_seconds", Input).value = "90"
        for size in [(60, 18), (48, 12), (40, 10), (80, 24)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert prefs.query_one(".form-too-small").display == (size[0] < 48)
        prefs.query_one("#vision-test", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "passed" in str(prefs.query_one(".form-status", Static).render())
        assert client.calls[-1][0] == "test_vision_model"
        assert client.calls[-1][1]["vision"]["timeout_seconds"] == 90
        assert not any(method.startswith("save_") for method, _ in client.calls)
        client.configuration["vision"].update(model="synthetic/changed", max_output_tokens=2048)
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert "synthetic/changed" in str(prefs.query_one("#vision-target", Static).render())
        assert prefs.vision.read_values()["timeout_seconds"] == 60
        assert prefs.vision.read_values()["max_output_tokens"] == 2048
        assert app.preferences_state.vision.draft is None


async def test_vision_save_failure_preserves_drafts_for_retry():
    client = VisionPreferencesClient()
    client.configuration["vision"].update(
        mode="separate", provider="local", model="synthetic/vision",
    )
    client.fail_save = True
    app = RoomApp(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        prefs = await open_vision(app, pilot)
        prefs.query_one("#pref-follow_replies", Checkbox).value = False
        prefs.query_one("#vision-timeout_seconds", Input).value = "90"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is prefs
        assert prefs.query_one(".form-status").has_class("problem")
        assert client.configuration["preferences"]["follow_replies"]
        await pilot.press("escape")
        await pilot.pause()
        prefs = await open_vision(app, pilot)
        assert not prefs.query_one("#pref-follow_replies", Checkbox).value
        assert prefs.vision.read_values()["timeout_seconds"] == 90
        client.fail_save = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.config.vision.timeout_seconds == 90
        assert not app.config.preferences.follow_replies
        assert app.preferences_state.vision.draft is None
