import pytest

from eidolon.config import AppConfig, VisionModelConfig, load_config, save_config
from eidolon.preferences import merge_vision_model


def test_vision_defaults_preserve_existing_configuration_and_round_trip(tmp_path):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "private")
    assert config.vision.mode == "disabled"
    assert AppConfig.model_validate(config.model_dump(exclude={"vision"})).vision == config.vision
    candidate = merge_vision_model(config, {
        "mode": "separate", "provider": "local", "model": "synthetic/vision",
        "context_window": 128000, "max_output_tokens": 2048, "reasoning_effort": "low",
    })
    assert candidate.selected_provider() == config.selected_provider()
    assert candidate.auxiliary == config.auxiliary
    assert candidate.vision.provider == "local"
    assert load_config(save_config(candidate)).vision == candidate.vision
    assert config.vision.mode == "disabled"


@pytest.mark.parametrize("settings", [
    {"mode": "inherit"}, {"provider": ""}, {"model": "bad\nmodel"},
    {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
    {"max_output_tokens": 127}, {"max_output_tokens": 8193}, {"context_window": 0},
    {"api_key_ref": "env:VISION_KEY"}, {"endpoint": "https://example.com/v1"},
    {"output_token_field": "max_output_tokens"},
])
def test_vision_role_rejects_invalid_options_and_connection_credentials(settings):
    with pytest.raises(ValueError):
        VisionModelConfig.model_validate(settings)


@pytest.mark.parametrize("settings", [
    {"mode": "separate"},
    {"mode": "separate", "provider": "local"},
    {"mode": "separate", "provider": "missing", "model": "synthetic/vision"},
])
def test_vision_preferences_validate_saved_connection_and_model(settings):
    with pytest.raises(ValueError, match="Choose|missing"):
        merge_vision_model(AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), settings)


def test_vision_target_changes_clear_only_unconfirmed_context_override():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, vision={
        "mode": "separate", "provider": "local", "model": "synthetic/old", "context_window": 128000,
    })
    changed = merge_vision_model(config, {"model": "synthetic/new"})
    assert changed.vision.context_window is None
    confirmed = merge_vision_model(config, {"model": "synthetic/new", "context_window": 128000})
    assert confirmed.vision.context_window == 128000
    limit_only = merge_vision_model(config, {"max_output_tokens": 2048})
    assert limit_only.vision.context_window == 128000


def test_broken_external_vision_binding_does_not_block_dialogue_configuration():
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, vision={"mode": "separate", "provider": "missing"})
    assert config.selected_provider().name == "local"
    disabled = merge_vision_model(config, {"mode": "disabled"})
    assert disabled.vision.mode == "disabled"
