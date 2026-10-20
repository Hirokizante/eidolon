import copy

import pytest

from eidolon.config import AppConfig, ProviderConfig
from eidolon.preferences import UserPreferences, merge_preferences


def test_identity_preferences_default_and_partial_patches_preserve_the_other_name():
    preferences = UserPreferences()
    assert preferences.user_name == "You" and preferences.companion_name == "Companion"
    config = merge_preferences(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), {"user_name": "  Avery  "})
    assert config.identity.user_name == "Avery" and config.identity.companion_name == "Companion"
    updated = merge_preferences(config, {"companion_name": "  Orbit  ", "follow_replies": False})
    assert updated.identity.user_name == "Avery" and updated.identity.companion_name == "Orbit"
    assert not updated.preferences.follow_replies
    assert config.identity.companion_name == "Companion" and config.preferences.follow_replies
    assert merge_preferences(updated, {"max_memories": 9}).identity == updated.identity


@pytest.mark.parametrize("names", [
    {"user_name": "Avery", "companion_name": "Orbit"},
    {"user_name": "雪 👩‍🚀", "companion_name": "[bold]{{companion}}[/bold]"},
    {"user_name": "界" * 80, "companion_name": "界" * 80},
])
def test_identity_preferences_round_trip_without_changing_other_configuration(names):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=names)
    config.selected_provider().api_key_ref = "env:SYNTHETIC_MODEL_KEY"
    config.theme.colors = {"accent": "#123456"}
    config.avatar.enabled = True
    before = config.model_dump()
    preferences = UserPreferences.from_config(config)
    assert {key: getattr(preferences, key) for key in names} == names
    assert preferences.apply_to(config).model_dump() == before
    changed = merge_preferences(config, {"companion_name": "Nova"})
    expected = copy.deepcopy(before)
    expected["identity"]["companion_name"] = "Nova"
    assert changed.model_dump() == expected
    assert config.model_dump() == before


@pytest.mark.parametrize("field", ["user_name", "companion_name"])
@pytest.mark.parametrize("value", [
    None, 7, True, [], {}, "", "   ", "x" * 81,
    "PRIVATE\n", "PRIVATE\x1b", "PRIVATE\u2028", "PRIVATE\u2066", "PRIVATE\ud800",
])
def test_invalid_identity_preferences_use_shared_validation_without_echoing_input(field, value):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity={"user_name": "Avery", "companion_name": "Orbit"})
    before = config.model_dump()
    with pytest.raises(ValueError) as error:
        merge_preferences(config, {field: value})
    assert field in str(error.value) and "PRIVATE" not in str(error.value)
    assert config.model_dump() == before


def test_preferences_merge_preserves_provider_theme_and_integration_details():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"},
        providers=[ProviderConfig(model="custom", api_key_ref="env:MODEL_KEY")],
    )
    config.theme.colors = {"accent": "#123456"}
    config.memory.api_key_ref = "env:MEMORY_KEY"
    config.memory.verified = True
    config.memory.retain_enabled = True
    config.bindings["other"] = "alt+k"
    values = UserPreferences.from_config(config).model_dump()
    values.update(timezone="UTC", reset_hour=8, memory_enabled=False, memory_save_enabled=False,
                  recall_budget="high", search_results=9)
    changed = UserPreferences.model_validate(values).apply_to(config)
    assert changed.providers == config.providers and changed.theme == config.theme
    assert changed.memory.api_key_ref == "env:MEMORY_KEY"
    assert changed.memory.verified and changed.memory.retain_enabled
    assert not changed.memory.enabled and changed.memory.recall_budget == "high"
    assert not changed.memory.save_enabled
    assert not UserPreferences.from_config(changed).memory_save_enabled
    assert changed.bindings["other"] == "alt+k"
    assert changed.timezone == "UTC" and changed.reset_hour == 8
    assert changed.tavily.max_results == 9
    assert config.timezone == "Asia/Manila" and config.memory.enabled
    assert config.memory.save_enabled


@pytest.mark.parametrize("patch", [
    {"timezone": "Invalid/Timezone"}, {"reset_hour": 24},
    {"max_memories": 0}, {"recall_budget": "maximum"}, {"search_depth": "deep"},
    {"search_results": 21}, {"bindings": {"send": "enter", "newline": "enter"}},
    {"provider": "other"}, {"theme": {"name": "solarized-light"}},
    {"status_components": ["cache", "cache"]}, {"status_components": ["unknown"]},
    {"continuity_max_notes": 0}, {"continuity_max_notes": 65},
    {"continuity_max_notes": True}, {"continuity_max_notes": 2.5},
])
def test_preferences_reject_invalid_values_and_unrelated_configuration(patch):
    with pytest.raises(ValueError):
        UserPreferences.model_validate(patch)


def test_status_layout_defaults_and_round_trip_preserve_component_order():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"})
    assert config.preferences.status_components == ["model", "context", "cache", "speed"]
    values = UserPreferences.from_config(config).model_dump()
    values["status_components"] = ["speed", "model", "memory"]
    changed = UserPreferences.model_validate(values).apply_to(config)
    assert changed.preferences.status_components == ["speed", "model", "memory"]
    assert config.preferences.status_components == ["model", "context", "cache", "speed"]
    assert UserPreferences.from_config(changed).status_components == values["status_components"]
    values["status_components"] = []
    assert UserPreferences.model_validate(values).apply_to(config).preferences.status_components == []


def test_continuity_preferences_are_independent_and_preserve_existing_choices():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"})
    updated = UserPreferences.model_validate({
        **UserPreferences.from_config(config).model_dump(),
        "continuity_enabled": True, "continuity_use_enabled": False,
        "continuity_batch_exchanges": 5, "continuity_idle_seconds": 300,
        "continuity_max_notes": 7,
    }).apply_to(config)
    assert updated.memory.continuity_enabled and not updated.memory.continuity_use_enabled
    assert updated.memory.enabled and updated.memory.save_enabled
    assert updated.memory.continuity_batch_exchanges == 5
    assert updated.memory.continuity_idle_seconds == 300
    assert updated.memory.continuity_max_notes == 7
    assert UserPreferences.from_config(updated).continuity_max_notes == 7
    assert UserPreferences.from_config(updated).continuity_enabled
    assert not config.memory.continuity_enabled
