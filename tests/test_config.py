import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from eidolon.config import (
    PROMPT_NAMES, AppConfig, AvatarConfig, MCPServerConfig, MemoryConfig, ProviderConfig,
    TavilyConfig, initialize_config, load_config, resolve_secret, save_config,
)


def test_memory_user_choices_are_independent_and_preserve_legacy_opt_out():
    assert not MemoryConfig().enabled and not MemoryConfig().save_enabled
    legacy = MemoryConfig.model_validate({"enabled": False})
    assert not legacy.enabled and not legacy.save_enabled
    recall_off = MemoryConfig(enabled=False, save_enabled=True)
    assert not recall_off.enabled and recall_off.save_enabled
    save_off = MemoryConfig(enabled=True, save_enabled=False, verified=True, retain_enabled=True)
    assert save_off.enabled and not save_off.save_enabled
    assert save_off.verified and save_off.retain_enabled
    # Re-enabling recall does not override the independently saved choice.
    legacy.enabled = True
    assert not legacy.save_enabled
    assert MemoryConfig.model_validate(legacy.model_dump()) == legacy


def test_continuity_rollout_defaults_and_tunable_bounds():
    config = MemoryConfig.model_validate({"enabled": True, "save_enabled": True})
    assert not config.continuity_enabled and not config.continuity_use_enabled
    assert config.continuity_batch_exchanges == 8 and config.continuity_idle_seconds == 600
    for field, value in (("continuity_batch_exchanges", 0), ("continuity_batch_exchanges", 33),
                         ("continuity_idle_seconds", 0), ("continuity_idle_seconds", 86401)):
        with pytest.raises(ValueError):
            MemoryConfig.model_validate({field: value})


@pytest.mark.parametrize("disabled", [False, 0, "false", "FALSE", "off", "no", "0"])
def test_legacy_memory_opt_out_accepts_existing_boolean_spellings(disabled):
    config = MemoryConfig.model_validate({"enabled": disabled})
    assert not config.enabled and not config.save_enabled


def test_own_paths_preserved_and_no_secret_literals(tmp_path):
    project = tmp_path / "prompts"
    project.mkdir()
    for name in ["SYSTEM", "SOUL", "USER", "POST_HISTORY"]:
        (project / f"{name}.md").write_text(name)
    state = tmp_path / "state"
    config = initialize_config(project, state_dir=state)
    assert initialize_config(project, state_dir=state) == config
    assert load_config(state / "config.yaml").prompts["SOUL"] == str(state / "prompts/SOUL.md")
    for name in PROMPT_NAMES:
        private = Path(config.prompts[name])
        assert private.read_text() == name
        assert private.stat().st_mode & 0o777 == 0o600
        assert (project / f"{name}.md").read_text() == name
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "prompts").stat().st_mode & 0o777 == 0o700
    assert (state / "config.yaml").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        resolve_secret("literal-api-key")


def test_validated_profile_and_timezone(monkeypatch):
    with pytest.raises(ValueError):
        AppConfig(provider="missing")
    with pytest.raises(ValueError):
        ProviderConfig(endpoint="https://secret:password@example.com")
    monkeypatch.setenv("EIDOLON_TEST_KEY", "test-key")
    assert resolve_secret("env:EIDOLON_TEST_KEY") == "test-key"
    monkeypatch.delenv("EIDOLON_TEST_KEY")
    with pytest.raises(ValueError):
        resolve_secret("env:EIDOLON_TEST_KEY")


def test_legacy_character_cap_is_retired_without_overriding_discovered_capacity(tmp_path):
    config = initialize_config(state_dir=tmp_path / "state")
    values = config.model_dump(mode="json")
    profile = values["providers"][0]
    profile["context_chars"] = 32000
    profile["model_metadata"] = {profile["model"]: {
        "context_window": 1_000_000, "source": "provider /models", "updated_at": 0,
    }}
    path = config.state_dir / "config.yaml"
    path.write_text(yaml.safe_dump(values))
    loaded = load_config(path)
    assert loaded.selected_provider().context_window is None
    assert loaded.selected_provider().context_window_for() == 1_000_000
    assert "context_chars" not in ProviderConfig.model_json_schema()["properties"]
    save_config(loaded, path)
    assert "context_chars" not in yaml.safe_load(path.read_text())["providers"][0]
    profile["context_window"] = 128_000
    assert ProviderConfig.model_validate(profile).context_window_for() == 128_000
    with pytest.raises(ValueError):
        ProviderConfig.model_validate(profile | {"unknown_budget": 100})


def test_legacy_output_settings_do_not_expose_retired_preferences(tmp_path):
    config = initialize_config(state_dir=tmp_path / "state")
    target = config.state_dir / "config.yaml"
    values = config.model_dump(mode="json")
    old_folder = tmp_path / "old-notes"
    old_folder.mkdir()
    existing = old_folder / "kept.md"
    existing.write_text("An existing private file.")
    values["drawer_root"] = str(old_folder)
    values["preferences"]["status_components"] = ["memory", "notes", "model"]
    target.write_text(yaml.safe_dump(values))
    loaded = load_config(target)
    assert loaded.preferences.status_components == ["memory", "model"]
    assert "drawer_root" not in loaded.model_dump()
    assert "drawer_root" not in AppConfig.model_json_schema()["properties"]
    save_config(loaded, target)
    assert "drawer_root" not in yaml.safe_load(target.read_text())
    assert existing.read_text() == "An existing private file."
    values["unknown_setting"] = True
    target.write_text(yaml.safe_dump(values))
    with pytest.raises(ValueError):
        load_config(target)


def test_setup_uses_bundled_defaults_and_preserves_customizations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "SOUL.md").write_text("unrelated local personality")
    state = tmp_path / "state"
    config = initialize_config(state_dir=state)
    for name in PROMPT_NAMES:
        assert Path(config.prompts[name]).read_bytes() == files("eidolon").joinpath(
            "defaults", f"{name}.md"
        ).read_bytes()
    Path(config.prompts["SOUL"]).write_text("private customization")
    config.identity.user_name = "Aster"
    config.identity.companion_name = "Nova"
    config.selected_provider().model = "private-model"
    save_config(config)
    (state / "jobs.json").write_text('[{"custom": true}]\n')
    again = initialize_config(state_dir=state)
    assert Path(again.prompts["SOUL"]).read_text() == "private customization"
    assert again.identity == config.identity
    assert again.selected_provider().model == "private-model"
    assert json.loads((state / "jobs.json").read_text()) == [{"custom": True}]
    assert (tmp_path / "SOUL.md").read_text() == "unrelated local personality"


def test_partial_import_accepts_post_history_alias_and_seeds_missing_defaults(tmp_path):
    project = tmp_path / "import"
    project.mkdir()
    (project / "POST HISTORY.md").write_bytes(b"custom suffix\r\n")
    config = initialize_config(project, state_dir=tmp_path / "state")
    assert Path(config.prompts["POST_HISTORY"]).read_bytes() == b"custom suffix\r\n"
    assert Path(config.prompts["SOUL"]).is_file()


def test_legacy_config_migrates_exact_prompt_contents_without_changing_settings(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    prompts = {}
    for name in PROMPT_NAMES:
        source = project / f"{name}.md"
        source.write_bytes((f"custom {name}\r\n" + "Unicode: ☾\n").encode())
        prompts[name] = str(source)
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    config = AppConfig(state_dir=state, prompts=prompts)
    config.selected_provider().model = "custom-model"
    config.selected_provider().api_key_ref = "env:EIDOLON_TEST_KEY"
    original = config.model_dump(mode="json")
    target = state / "config.json"
    target.write_text(config.model_dump_json())
    migrated = load_config(target)
    for name in PROMPT_NAMES:
        assert Path(migrated.prompts[name]).read_bytes() == Path(prompts[name]).read_bytes()
        assert Path(prompts[name]).exists()
    assert migrated.model_dump(mode="json") | {"prompts": prompts} == original
    assert load_config(target) == migrated
    assert target.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700


def test_missing_legacy_prompt_fails_before_writing_defaults(tmp_path):
    state = tmp_path / "state"
    config = AppConfig(state_dir=state, prompts={"SOUL": str(tmp_path / "missing.md")})
    with pytest.raises(ValueError, match="Missing prompt source for SOUL"):
        save_config(config)
    assert not state.exists()


def test_conflicting_private_prompt_is_never_overwritten(tmp_path):
    state = tmp_path / "state"
    (state / "prompts").mkdir(parents=True)
    destination = state / "prompts/SOUL.md"
    destination.write_text("already private")
    source = tmp_path / "SOUL.md"
    source.write_text("configured source")
    config = AppConfig(state_dir=state, prompts={"SOUL": str(source)})
    with pytest.raises(ValueError, match="conflicts"):
        save_config(config)
    assert destination.read_text() == "already private"
    assert source.read_text() == "configured source"
    assert not (state / "config.yaml").exists()
    assert not (state / "prompts/SYSTEM.md").exists()


def test_private_prompt_symlink_cannot_redirect_edits_into_source_tree(tmp_path):
    state = tmp_path / "state"
    (state / "prompts").mkdir(parents=True)
    source = tmp_path / "SOUL.md"
    source.write_text("source personality")
    (state / "prompts/SOUL.md").symlink_to(source)
    with pytest.raises(ValueError, match="symbolic link"):
        initialize_config(state_dir=state)
    assert source.read_text() == "source personality"


def test_explicit_configuration_path_is_idempotent(tmp_path):
    target = tmp_path / "state/settings.json"
    config = initialize_config(state_dir=target.parent, path=target)
    config.selected_provider().model = "custom-model"
    save_config(config, target)
    assert initialize_config(state_dir=target.parent, path=target).selected_provider().model == "custom-model"
    assert not (target.parent / "config.json").exists()


def test_example_configuration_is_valid_default_data_and_never_a_live_target(tmp_path):
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    assert AppConfig.model_validate(yaml.safe_load(example.read_text())) == AppConfig(timezone="UTC")
    target = tmp_path / "config.example.yaml"
    contents = example.read_bytes()
    target.write_bytes(contents)
    with pytest.raises(ValueError, match="template"):
        load_config(target)
    with pytest.raises(ValueError, match="template"):
        save_config(AppConfig(state_dir=tmp_path / "state"), target)
    assert target.read_bytes() == contents
    assert not (tmp_path / "state").exists()


def test_symlink_cannot_make_an_example_template_a_live_config(tmp_path):
    example = tmp_path / "config.example.yaml"
    example.write_text("{}\n")
    target = tmp_path / "config.json"
    target.symlink_to(example)
    with pytest.raises(ValueError, match="template"):
        load_config(target)
    assert example.read_text() == "{}\n"


@pytest.mark.parametrize("worktree", [False, True])
def test_runtime_state_and_custom_named_configuration_cannot_live_in_git(tmp_path, worktree):
    project = tmp_path / "project"
    project.mkdir()
    marker = project / ".git"
    if worktree:
        marker.write_text("gitdir: /path/to/main/.git/worktrees/test\n")
    else:
        marker.mkdir()
    with pytest.raises(ValueError, match="outside Git checkouts"):
        initialize_config(state_dir=project / "custom-runtime")
    with pytest.raises(ValueError, match="outside Git checkouts"):
        save_config(AppConfig(state_dir=tmp_path / "private"), project / "custom-settings.json")
    assert not (project / "custom-runtime").exists()
    assert not (project / "custom-settings.json").exists()
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("model,field", [
    (ProviderConfig, "api_key_ref"), (MemoryConfig, "api_key_ref"),
    (TavilyConfig, "api_key_ref"), (AvatarConfig, "signing_key_ref"),
])
def test_literal_credentials_rejected_on_input_and_assignment_without_echo(model, field):
    literal = "private-literal-value"
    with pytest.raises(ValueError) as error:
        model(**{field: literal})
    assert literal not in str(error.value)
    config = model(**{field: "env:EXAMPLE_KEY"})
    with pytest.raises(ValueError):
        setattr(config, field, literal)


@pytest.mark.parametrize("reference", ["env:", "env:BAD NAME", "keyring:", "keyring: name", "keyring:name\n"])
def test_malformed_secret_references_are_rejected(reference):
    with pytest.raises(ValueError, match="references"):
        resolve_secret(reference)


@pytest.mark.parametrize("factory", [
    lambda value: ProviderConfig(headers={"Authorization": value}),
    lambda value: ProviderConfig(headers={"X-API-Key": value}),
    lambda value: MCPServerConfig(name="test", headers={"Cookie": value}),
    lambda value: MCPServerConfig(name="test", env={"EIDOLON_BODY_TOKEN": value}),
    lambda value: MCPServerConfig(name="test", env={"SERVICE_API_KEY": value}),
])
def test_secret_headers_and_environment_require_references(factory):
    with pytest.raises(ValueError) as error:
        factory("private-literal-value")
    assert "private-literal-value" not in str(error.value)
    factory("keyring:test")
    factory("env:TEST_KEY")


def test_public_headers_and_environment_literals_are_allowed():
    ProviderConfig(headers={"User-Agent": "eidolon-room", "HTTP-Referer": "https://example.com"})
    MCPServerConfig(name="test", env={"PORT": "8766", "LOG_LEVEL": "info"})


def test_mutated_secret_dictionary_rejected_before_config_write(tmp_path):
    config = AppConfig(state_dir=tmp_path / "state")
    config.selected_provider().headers["Authorization"] = "private-literal-value"
    with pytest.raises(ValueError) as error:
        save_config(config)
    assert "private-literal-value" not in str(error.value)
    assert not config.state_dir.exists()


@pytest.mark.parametrize("model,field,extra", [
    (ProviderConfig, "endpoint", {}), (MemoryConfig, "url", {}),
    (MCPServerConfig, "url", {"name": "test"}), (AvatarConfig, "lifecycle_url", {}),
])
def test_connection_urls_reject_embedded_credentials(model, field, extra):
    with pytest.raises(ValueError) as error:
        model(**extra, **{field: "https://name:private-password@example.com"})
    assert "private-password" not in str(error.value)


def test_provider_inventory_and_display_name_are_optional_and_persist_privately(tmp_path):
    legacy = ProviderConfig.model_validate({"name": "custom", "model": "manual"})
    assert legacy.display_name is None and legacy.models == []
    legacy.display_name = "  My endpoint  "
    legacy.models = ["z-model", "a-model", "z-model", ""]
    config = AppConfig(state_dir=tmp_path / "state", provider="custom", providers=[legacy])
    target = save_config(config)
    restored = load_config(target).selected_provider()
    assert restored.name == "custom" and restored.display_name == "My endpoint"
    assert restored.models == ["a-model", "z-model"]
    assert target.stat().st_mode & 0o777 == 0o600
    restored.display_name = "   "
    assert restored.display_name is None


@pytest.mark.parametrize("entry_point", ["load", "setup"])
def test_json_configuration_migrates_to_yaml_preserving_backup_and_customizations(
    tmp_path, monkeypatch, entry_point,
):
    state = tmp_path / "private"
    state.mkdir()
    monkeypatch.setenv("EIDOLON_HOME", str(state))
    config = AppConfig(state_dir=state, timezone="Europe/London", reset_hour=6)
    config.selected_provider().model = "custom-model"
    config.theme.colors = {"accent": "#123456"}
    config.bindings = {"send": "ctrl+enter", "newline": "enter"}
    original = config.model_dump_json(indent=2)
    legacy = state / "config.json"
    legacy.write_text(original)
    restored = load_config() if entry_point == "load" else initialize_config(state_dir=state)
    assert restored.timezone == "Europe/London" and restored.reset_hour == 6
    assert restored.selected_provider().model == "custom-model"
    assert restored.bindings == config.bindings and restored.theme == config.theme
    assert legacy.read_text() == original
    target = state / "config.yaml"
    assert yaml.safe_load(target.read_text())["timezone"] == "Europe/London"
    assert target.stat().st_mode & 0o777 == legacy.stat().st_mode & 0o777 == 0o600
    restored.reset_hour = 8
    save_config(restored)
    assert load_config().reset_hour == 8
    assert legacy.read_text() == original


def test_existing_yaml_wins_over_legacy_json_and_invalid_yaml_is_not_replaced(tmp_path):
    config = initialize_config(state_dir=tmp_path / "private")
    (config.state_dir / "config.json").write_text(config.model_dump_json())
    config.reset_hour = 9
    target = save_config(config)
    assert load_config(target).reset_hour == 9
    target.write_text("timezone: [\n")
    with pytest.raises(ValueError, match="syntax"):
        load_config(target)
    assert target.read_text() == "timezone: [\n"


@pytest.fixture
def identity_configuration(tmp_path):
    state = tmp_path / "private"
    directory = state / "prompts"
    directory.mkdir(parents=True)
    prompts = {}
    for name in PROMPT_NAMES:
        source = directory / f"{name}.md"
        source.write_bytes(f"Synthetic {name}: Aster and Nova.\r\n星 👩🏽‍🚀\n".encode("utf-8"))
        prompts[name] = str(source)
    config = AppConfig(state_dir=state, prompts=prompts, timezone="Etc/UTC", reset_hour=7,
                       jobs_file=state / "jobs.json")
    config.selected_provider().model = "fixture-model"
    config.selected_provider().api_key_ref = "env:EXAMPLE_MODEL_KEY"
    config.selected_provider().parameters = {"temperature": 0.4}
    config.memory.bank = "fixture-bank"
    config.memory.save_enabled = False
    config.avatar.server = "fixture-bridge"
    config.avatar.signing_key_ref = "env:EXAMPLE_AVATAR_KEY"
    config.theme.colors = {"accent": "#123456"}
    config.jobs_file.write_bytes(b'[{"fixture": true}]\n')
    return config


@pytest.mark.parametrize("suffix", ["yaml", "json"])
@pytest.mark.parametrize("identity,expected", [
    (None, {"user_name": "You", "companion_name": "Companion"}),
    ({}, {"user_name": "You", "companion_name": "Companion"}),
    ({"user_name": "Aster"}, {"user_name": "Aster", "companion_name": "Companion"}),
    ({"companion_name": "Nova"}, {"user_name": "You", "companion_name": "Nova"}),
])
def test_older_configurations_default_only_omitted_identity_fields(
    identity_configuration, suffix, identity, expected,
):
    config = identity_configuration
    values = config.model_dump(mode="json")
    values.pop("identity")
    if identity is not None:
        values["identity"] = identity
    target = config.state_dir / f"config.{suffix}"
    contents = json.dumps(values) if suffix == "json" else yaml.safe_dump(values)
    target.write_text(contents, encoding="utf-8")
    restored = load_config(target)
    assert restored.identity.model_dump() == expected
    assert restored.model_dump(exclude={"identity"}) == config.model_dump(exclude={"identity"})
    assert target.read_text(encoding="utf-8") == contents
    save_config(restored, target)
    assert load_config(target) == restored


@pytest.mark.parametrize("suffix", ["yaml", "json"])
@pytest.mark.parametrize("names", [("Aster", "Nova"), ("Orion", "Vega"),
                                   ("e\u0301 星 👩🏽‍🚀", "[bold]{{name}}[/bold]")])
def test_identity_round_trip_preserves_unrelated_settings_jobs_and_prompt_bytes(
    identity_configuration, suffix, names,
):
    config = identity_configuration
    unrelated = config.model_dump(exclude={"identity"})
    prompts = {name: Path(path).read_bytes() for name, path in config.prompts.items()}
    jobs = config.jobs_file.read_bytes()
    config.identity.user_name = f"  {names[0]}  "
    config.identity.companion_name = f"  {names[1]}  "
    target = config.state_dir / f"config.{suffix}"
    save_config(config, target)
    restored = load_config(target)
    assert (restored.identity.user_name, restored.identity.companion_name) == names
    assert restored.model_dump(exclude={"identity"}) == unrelated
    assert {name: Path(path).read_bytes() for name, path in restored.prompts.items()} == prompts
    assert restored.jobs_file.read_bytes() == jobs
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("suffix", ["yaml", "json"])
@pytest.mark.parametrize("field", ["user_name", "companion_name"])
def test_invalid_identity_in_a_file_fails_privately_without_rewriting_it(
    identity_configuration, suffix, field,
):
    config = identity_configuration
    values = config.model_dump(mode="json")
    values["identity"][field] = "private-fixture\n"
    target = config.state_dir / f"config.{suffix}"
    contents = json.dumps(values) if suffix == "json" else yaml.safe_dump(values)
    target.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=f"identity.{field}") as error:
        load_config(target)
    assert "private-fixture" not in str(error.value)
    assert target.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("field", ["user_name", "companion_name"])
def test_invalid_identity_is_revalidated_before_a_saved_config_is_changed(
    identity_configuration, field,
):
    config = identity_configuration
    target = save_config(config)
    contents = target.read_bytes()
    prompts = {name: Path(path).read_bytes() for name, path in config.prompts.items()}
    config.identity = config.identity.model_copy(update={field: "private-fixture\n"})
    with pytest.raises(ValueError, match=f"identity.{field}") as error:
        save_config(config, target)
    assert "private-fixture" not in str(error.value)
    assert target.read_bytes() == contents
    assert {name: Path(path).read_bytes() for name, path in config.prompts.items()} == prompts
