from pathlib import Path

import pytest

from eidolon import cli
from eidolon import config as configuration_module
from eidolon.config import IdentityConfig, initialize_config, load_config, save_config


@pytest.fixture(autouse=True)
def isolated_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def unexpected_input(*args, **kwargs):
        pytest.fail("Setup unexpectedly requested input or accessed credential storage")

    monkeypatch.setattr("builtins.input", unexpected_input)
    monkeypatch.setattr(cli.getpass, "getpass", unexpected_input)
    monkeypatch.setattr(cli, "store_secret", unexpected_input)
    monkeypatch.setattr("keyring.set_password", unexpected_input)


@pytest.fixture
def configured_room(tmp_path):
    config = initialize_config(state_dir=tmp_path / "state")
    config.identity = IdentityConfig(user_name="Aster", companion_name="Nova")
    config.timezone = "Etc/UTC"
    config.selected_provider().model = "fixture-model"
    config.selected_provider().api_key_ref = "env:EXAMPLE_MODEL_KEY"
    config.memory.bank = "fixture-bank"
    config.memory.save_enabled = False
    config.avatar.server = "fixture-bridge"
    config.theme.colors = {"accent": "#123456"}
    for path in config.prompts.values():
        Path(path).write_bytes(b"Synthetic private prompt: Aster and Nova.\r\n")
    config.jobs_file.write_bytes(b'[{"fixture": true}]\n')
    save_config(config)
    return config


def answer_setup(
    monkeypatch, user_name, companion_name, *, settings=("", "", "", "", ""), avatar=("",),
):
    answers = iter((user_name, companion_name, *settings[:2], "", *settings[2:], *avatar))
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    def blank_secret(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli.getpass, "getpass", blank_secret)
    return prompts


@pytest.fixture
def standalone_bridge(tmp_path, monkeypatch):
    bridge = tmp_path / "synthetic-installation/motion-bridge"
    bridge.mkdir(parents=True)
    (bridge / "server.py").touch()
    (bridge / "pyproject.toml").touch()
    credentials = tmp_path / "selected.env"
    credentials.write_text("STAGE_ACCESS=fixture-token\nSTAGE_SIGNING=fixture-signing\n")
    monkeypatch.setattr("eidolon.avatar.setup.shutil.which", lambda _: "/standalone/uv")
    return bridge, credentials


def test_setup_accepts_explicit_bridge_credentials_and_external_route(
    configured_room, standalone_bridge, monkeypatch, capsys,
):
    bridge, credentials = standalone_bridge
    copied = {}

    def store(name, value):
        copied[name] = value
        return "keyring:" + name

    monkeypatch.setattr(cli, "store_secret", store)

    cli.main(["setup", "--avatar-bridge", str(bridge),
              "--avatar-credentials", str(credentials), "--avatar-token-env", "STAGE_ACCESS",
              "--avatar-signing-env", "STAGE_SIGNING", "--avatar-profile", "stage-42"])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert restored.avatar.enabled and restored.avatar.profile == "stage-42"
    assert restored.avatar.server == "fixture-bridge"
    assert restored.avatar.signing_key_ref == "keyring:vessel-signing"
    assert restored.mcp_servers[0].env == {"STAGE_ACCESS": "keyring:vessel-body-token"}
    assert copied == {"vessel-body-token": "fixture-token", "vessel-signing": "fixture-signing"}
    assert restored.identity == configured_room.identity
    output = capsys.readouterr()
    assert str(bridge) not in output.out + output.err
    assert str(credentials) not in output.out + output.err
    assert "fixture-signing" not in output.out + output.err


def test_interactive_setup_asks_for_bridge_without_home_discovery(
    configured_room, standalone_bridge, monkeypatch,
):
    bridge, credentials = standalone_bridge
    prompts = answer_setup(monkeypatch, "", "", avatar=(
        str(bridge), str(credentials), "STAGE_ACCESS", "STAGE_SIGNING", "stage-月",
    ))
    monkeypatch.setattr(cli, "store_secret", lambda name, value: "keyring:" + name)

    def reject_home(cls):
        pytest.fail("Interactive setup unexpectedly discovered the home directory")

    monkeypatch.setattr(Path, "home", classmethod(reject_home))
    cli.main(["setup", "--interactive"])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert any("bridge directory" in prompt for prompt in prompts)
    assert any("credential file" in prompt for prompt in prompts)
    assert restored.avatar.profile == "stage-月" and restored.avatar.enabled
    assert restored.mcp_servers[0].env == {"STAGE_ACCESS": "keyring:vessel-body-token"}


def test_setup_help_explains_explicit_avatar_inputs(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["setup", "--help"])
    assert result.value.code == 0
    output = capsys.readouterr()
    for option in ("--avatar-bridge", "--avatar-credentials", "--avatar-token-env",
                   "--avatar-signing-env", "--avatar-profile"):
        assert option in output.out
    assert "explicitly selected" in output.out and "preserves" in output.out
    assert not output.err


@pytest.mark.parametrize("option", [
    "--avatar-credentials", "--avatar-token-env", "--avatar-signing-env", "--avatar-profile",
])
def test_setup_avatar_options_require_a_bridge_without_changing_saved_config(
    configured_room, capsys, option,
):
    path = configured_room.state_dir / "config.yaml"
    before = path.read_bytes()

    with pytest.raises(SystemExit) as result:
        cli.main(["setup", option, "fixture-private-value"])

    assert result.value.code == 1 and path.read_bytes() == before
    output = capsys.readouterr()
    assert "explicit bridge directory" in output.err
    assert "fixture-private-value" not in output.err


@pytest.mark.parametrize("options", [
    ["--avatar-token-env", "STAGE_ACCESS"],
    ["--avatar-signing-env", "MISSING_FIELD"],
    ["--avatar-profile", "bad\nroute"],
])
def test_setup_avatar_errors_preserve_saved_settings_without_source_disclosure(
    configured_room, standalone_bridge, capsys, options,
):
    bridge, credentials = standalone_bridge
    path = configured_room.state_dir / "config.yaml"
    before = path.read_bytes()
    if "MISSING_FIELD" in options:
        options = [*options, "--avatar-credentials", str(credentials)]

    with pytest.raises(SystemExit) as result:
        cli.main(["setup", "--avatar-bridge", str(bridge), *options])

    assert result.value.code == 1 and path.read_bytes() == before
    output = capsys.readouterr()
    for private in (str(bridge), str(credentials), "fixture-signing", "fixture-token", "bad\nroute"):
        assert private not in output.out + output.err


def test_interactive_bridge_setup_can_skip_credentials_and_preserve_private_route(
    configured_room, standalone_bridge, monkeypatch,
):
    bridge, _ = standalone_bridge
    configured_room.avatar.profile = "external-slot-7"
    configured_room.avatar.signing_key_ref = "env:EXAMPLE_SIGNING"
    save_config(configured_room)
    answer_setup(monkeypatch, "", "", avatar=(str(bridge), "", ""))

    cli.main(["setup", "--interactive"])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert restored.avatar.enabled and restored.avatar.profile == "external-slot-7"
    assert restored.avatar.signing_key_ref == "env:EXAMPLE_SIGNING"
    assert restored.mcp_servers[0].env == {}


def test_interactive_setup_with_bridge_flag_does_not_ask_for_another_bridge(
    configured_room, standalone_bridge, monkeypatch,
):
    bridge, _ = standalone_bridge
    prompts = answer_setup(monkeypatch, "", "", avatar=())

    cli.main(["setup", "--interactive", "--avatar-bridge", str(bridge)])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert restored.avatar.enabled and restored.avatar.profile == "companion"
    assert not any("bridge directory" in prompt for prompt in prompts)


def test_setup_without_names_initializes_neutral_defaults(tmp_path, capsys):
    cli.main(["setup"])

    config = load_config(tmp_path / "state/config.yaml")
    assert config.identity.model_dump() == {"user_name": "You", "companion_name": "Companion"}
    output = capsys.readouterr()
    assert "Eidolon configured:" in output.out
    assert not output.err


@pytest.mark.parametrize("options,expected", [
    ([], ("Aster", "Nova")),
    (["--user-name", "  Orion  "], ("Orion", "Nova")),
    (["--companion-name", "  Vega  "], ("Aster", "Vega")),
    (["--user-name", "Orion", "--companion-name", "Vega"], ("Orion", "Vega")),
])
def test_setup_patches_only_explicit_names(configured_room, options, expected):
    config = configured_room
    unrelated = config.model_dump(exclude={"identity"})
    configuration = config.state_dir / "config.yaml"

    cli.main(["--config", str(configuration), "setup", *options])

    restored = load_config(configuration)
    assert (restored.identity.user_name, restored.identity.companion_name) == expected
    assert restored.model_dump(exclude={"identity"}) == unrelated
    for path in restored.prompts.values():
        assert Path(path).read_bytes() == b"Synthetic private prompt: Aster and Nova.\r\n"
    assert restored.jobs_file.read_bytes() == b'[{"fixture": true}]\n'
    assert configuration.stat().st_mode & 0o777 == 0o600


def test_setup_initializes_both_explicit_names(tmp_path):
    cli.main(["setup", "--user-name", "Orion", "--companion-name", "Vega"])

    config = load_config(tmp_path / "state/config.yaml")
    assert config.identity.model_dump() == {"user_name": "Orion", "companion_name": "Vega"}


@pytest.mark.parametrize("option,expected", [
    ("--user-name", {"user_name": "Orion", "companion_name": "Companion"}),
    ("--companion-name", {"user_name": "You", "companion_name": "Orion"}),
])
def test_first_setup_defaults_only_the_omitted_name(tmp_path, option, expected):
    cli.main(["setup", option, "Orion"])

    assert load_config(tmp_path / "state/config.yaml").identity.model_dump() == expected


def test_setup_with_names_preserves_imported_prompt_bytes(tmp_path):
    project = tmp_path / "source-prompts"
    project.mkdir()
    content = "Synthetic personality: Aster and Nova.\r\n星\n".encode("utf-8")
    (project / "SOUL.md").write_bytes(content)

    cli.main(["setup", "--project", str(project), "--user-name", "Orion",
              "--companion-name", "Vega"])

    config = load_config(tmp_path / "state/config.yaml")
    assert config.identity.model_dump() == {"user_name": "Orion", "companion_name": "Vega"}
    assert Path(config.prompts["SOUL"]).read_bytes() == content
    assert (project / "SOUL.md").read_bytes() == content


def test_setup_help_explains_optional_name_flags(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["setup", "--help"])

    assert result.value.code == 0
    output = capsys.readouterr()
    assert "--user-name" in output.out and "--companion-name" in output.out
    assert "preserves" in output.out
    assert not output.err


def test_interactive_setup_shows_saved_names_and_preserves_blank_input(
    configured_room, monkeypatch,
):
    config = configured_room
    prompts = answer_setup(monkeypatch, "", "")

    cli.main(["setup", "--interactive"])

    assert "Your name [Aster]" in prompts[0]
    assert "Companion's name [Nova]" in prompts[1]
    assert load_config(config.state_dir / "config.yaml") == config


def test_first_interactive_setup_keeps_neutral_defaults_on_enter(tmp_path, monkeypatch):
    prompts = answer_setup(monkeypatch, "", "")

    cli.main(["setup", "--interactive"])

    assert "Your name [You]" in prompts[0]
    assert "Companion's name [Companion]" in prompts[1]
    assert load_config(tmp_path / "state/config.yaml").identity == IdentityConfig()


def test_interactive_names_keep_provider_memory_and_folder_setup_working(
    configured_room, tmp_path, monkeypatch,
):
    roots = [tmp_path / "documents", tmp_path / "notes"]
    answer_setup(monkeypatch, "Orion", "Vega", settings=(
        "http://127.0.0.1:4321/v1", "another-model", "http://127.0.0.1:4322",
        "another-bank", ", ".join(str(path) for path in roots),
    ))

    cli.main(["setup", "--interactive"])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert restored.identity.model_dump() == {"user_name": "Orion", "companion_name": "Vega"}
    assert restored.selected_provider().endpoint == "http://127.0.0.1:4321/v1"
    assert restored.selected_provider().model == "another-model"
    assert restored.selected_provider().api_key_ref == "env:EXAMPLE_MODEL_KEY"
    assert restored.memory.url == "http://127.0.0.1:4322"
    assert restored.memory.bank == "another-bank"
    assert restored.allowed_roots == [str(path.resolve()) for path in roots]
    for path in restored.prompts.values():
        assert Path(path).read_bytes() == b"Synthetic private prompt: Aster and Nova.\r\n"


@pytest.mark.parametrize("names,expected", [
    (("  Orion  ", ""), {"user_name": "Orion", "companion_name": "Nova"}),
    (("", "  Vega  "), {"user_name": "Aster", "companion_name": "Vega"}),
])
def test_interactive_setup_changes_only_nonblank_names(
    configured_room, monkeypatch, names, expected,
):
    answer_setup(monkeypatch, *names)

    cli.main(["setup", "--interactive"])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert restored.identity.model_dump() == expected
    assert restored.model_dump(exclude={"identity"}) == configured_room.model_dump(
        exclude={"identity"}
    )


@pytest.mark.parametrize("interactive", [False, True], ids=["flags", "interactive"])
@pytest.mark.parametrize("names", [
    ("  Orion  ", "  Vega  "),
    ("星 e\u0301 👩🏽‍🚀", "[bold]{{name}}[/bold]"),
    ("x" * 80, "x" * 80),
])
def test_setup_modes_preserve_literal_valid_names(
    configured_room, monkeypatch, interactive, names,
):
    if interactive:
        answer_setup(monkeypatch, *names)
        options = ["--interactive"]
    else:
        options = ["--user-name", names[0], "--companion-name", names[1]]

    cli.main(["setup", *options])

    restored = load_config(configured_room.state_dir / "config.yaml")
    assert (restored.identity.user_name, restored.identity.companion_name) == tuple(
        name.strip() for name in names
    )


def test_interactive_setup_shows_explicit_flags_as_current_values(configured_room, monkeypatch):
    prompts = answer_setup(monkeypatch, "", "Deneb")

    cli.main(["setup", "--user-name", "Orion", "--companion-name", "Vega", "--interactive"])

    assert "Your name [Orion]" in prompts[0]
    assert "Companion's name [Vega]" in prompts[1]
    restored = load_config(configured_room.state_dir / "config.yaml")
    assert restored.identity.model_dump() == {"user_name": "Orion", "companion_name": "Deneb"}


@pytest.mark.parametrize("interactive", [False, True], ids=["flags", "interactive"])
@pytest.mark.parametrize("field", ["user_name", "companion_name"])
@pytest.mark.parametrize("invalid", [
    pytest.param("   ", id="spaces-only"),
    pytest.param("private-fixture\n", id="newline"),
    pytest.param("\tprivate-fixture", id="leading-control"),
    pytest.param("private-fixture\u2066", id="bidi-isolate"),
    pytest.param("private-fixture\ud800", id="invalid-utf8"),
    pytest.param("private-fixture" + "x" * 66, id="81-code-points"),
])
def test_invalid_setup_names_leave_the_saved_pair_intact_without_echoing_input(
    configured_room, monkeypatch, capsys, interactive, field, invalid,
):
    config = configured_room
    configuration = config.state_dir / "config.yaml"
    before = configuration.read_bytes()
    names = {"user_name": "Orion", "companion_name": "Vega", field: invalid}
    if interactive:
        prompts = answer_setup(monkeypatch, names["user_name"], names["companion_name"])
        options = ["--interactive"]
    else:
        options = ["--user-name", names["user_name"], "--companion-name", names["companion_name"]]

    with pytest.raises(SystemExit) as result:
        cli.main(["setup", *options])

    assert result.value.code == 1
    assert configuration.read_bytes() == before
    assert load_config(configuration) == config
    for path in config.prompts.values():
        assert Path(path).read_bytes() == b"Synthetic private prompt: Aster and Nova.\r\n"
    assert config.jobs_file.read_bytes() == b'[{"fixture": true}]\n'
    output = capsys.readouterr()
    assert field in output.err and "Name must" in output.err
    assert "private-fixture" not in output.err and "input_value" not in output.err
    assert not output.out
    if interactive:
        assert len(prompts) == 2


@pytest.mark.parametrize("field", ["user-name", "companion-name"])
def test_explicit_empty_flags_fail_before_interactive_input(configured_room, capsys, field):
    configuration = configured_room.state_dir / "config.yaml"
    before = configuration.read_bytes()

    with pytest.raises(SystemExit) as result:
        cli.main(["setup", f"--{field}", "", "--interactive"])

    assert result.value.code == 1
    assert configuration.read_bytes() == before
    output = capsys.readouterr()
    assert field.replace("-", "_") in output.err and "1–80" in output.err
    assert not output.out


@pytest.mark.parametrize("interactive", [False, True], ids=["flags", "interactive"])
def test_invalid_first_setup_never_saves_a_partially_valid_name_pair(
    tmp_path, monkeypatch, interactive,
):
    if interactive:
        answer_setup(monkeypatch, "Orion", "private-fixture\n")
        options = ["--interactive"]
    else:
        options = ["--user-name", "Orion", "--companion-name", "private-fixture\n"]

    with pytest.raises(SystemExit) as result:
        cli.main(["setup", *options])

    assert result.value.code == 1
    assert load_config(tmp_path / "state/config.yaml").identity == IdentityConfig()


@pytest.mark.parametrize("field", ["user_name", "companion_name"])
def test_interactive_validation_keeps_the_entire_in_memory_pair(configured_room, monkeypatch, field):
    before = configured_room.model_dump()
    names = {"user_name": "Orion", "companion_name": "Vega", field: "private-fixture\n"}
    answer_setup(monkeypatch, names["user_name"], names["companion_name"])

    with pytest.raises(ValueError, match=field):
        cli.interactive_setup(configured_room)

    assert configured_room.model_dump() == before


@pytest.mark.parametrize("interactive", [False, True], ids=["flags", "interactive"])
def test_setup_write_failure_preserves_saved_names_and_allows_retry(
    configured_room, monkeypatch, capsys, interactive,
):
    configuration = configured_room.state_dir / "config.yaml"
    before = configuration.read_bytes()
    replace = configuration_module.os.replace

    def reject_configuration_write(source, destination):
        if Path(destination) == configuration:
            raise OSError("Synthetic configuration replacement failure")
        return replace(source, destination)

    if interactive:
        answer_setup(monkeypatch, "Orion", "Vega")
        options = ["--interactive"]
    else:
        options = ["--user-name", "Orion", "--companion-name", "Vega"]
    with monkeypatch.context() as writes:
        writes.setattr(configuration_module.os, "replace", reject_configuration_write)
        with pytest.raises(SystemExit) as result:
            cli.main(["setup", *options])

    assert result.value.code == 1
    assert configuration.read_bytes() == before
    assert load_config(configuration) == configured_room
    assert not list(configuration.parent.glob(".eidolon-*"))
    output = capsys.readouterr()
    assert "Synthetic configuration replacement failure" in output.err
    assert not output.out

    if interactive:
        answer_setup(monkeypatch, "Orion", "Vega")
    cli.main(["setup", *options])

    restored = load_config(configuration)
    assert restored.identity.model_dump() == {"user_name": "Orion", "companion_name": "Vega"}


@pytest.mark.parametrize("options", [[], ["--source", ""], ["--source", "   "]])
def test_migrate_requires_an_explicit_source_before_loading_configuration(
    monkeypatch, capsys, options,
):
    def reject_load(*args):
        pytest.fail("Migration loaded configuration without an explicit source")

    monkeypatch.setattr(cli, "load_config", reject_load)

    with pytest.raises(SystemExit) as result:
        cli.main(["migrate", *options])

    assert result.value.code == 2
    output = capsys.readouterr()
    assert "--source" in output.err and "required" in output.err
    assert not output.out


def test_migrate_help_explains_explicit_avatar_credential_lookup(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["migrate", "--help"])

    assert result.value.code == 0
    output = capsys.readouterr()
    assert "--source" in output.out and "--avatar-secret-env" in output.out
    assert "environment field" in output.out
    assert not output.err


@pytest.mark.parametrize("without_credentials", [False, True])
@pytest.mark.parametrize("avatar_field", [None, "EXAMPLE_LIFECYCLE"])
def test_migrate_passes_explicit_options_and_keeps_the_report_private(
    configured_room, tmp_path, monkeypatch, capsys, without_credentials, avatar_field,
):
    from eidolon import migration

    source = tmp_path / "private-fixture-source"
    captured = []

    def import_profile(config, selected_source, **options):
        captured.append((config.identity, selected_source, options))
        return {"source": str(source), "warnings": ["private-fixture-content"]}

    monkeypatch.setattr(migration, "migrate_hermes", import_profile)
    options = ["--without-credentials"] if without_credentials else []
    if avatar_field is not None:
        options.extend(["--avatar-secret-env", avatar_field])

    cli.main(["migrate", "--source", str(source), *options])

    assert captured == [(configured_room.identity, source, {
        "import_credentials": not without_credentials, "avatar_secret_env": avatar_field,
    })]
    output = capsys.readouterr()
    assert "Profile imported" in output.out and "private migration report" in output.out
    assert "private-fixture" not in output.out and str(tmp_path) not in output.out
    assert not output.err


def test_migrate_imports_a_synthetic_profile_through_the_cli(tmp_path, capsys):
    config = initialize_config(state_dir=tmp_path / "state")
    config.identity = IdentityConfig(user_name="Orion", companion_name="Vega")
    save_config(config)
    source = tmp_path / "explicit-profile"
    (source / "memories").mkdir(parents=True)
    content = "Synthetic profile: Aster and Nova keep their history.\n"
    (source / "memories/USER.md").write_text(content)

    cli.main(["migrate", "--source", str(source), "--without-credentials"])

    restored = load_config(config.state_dir / "config.yaml")
    assert restored.identity == config.identity
    assert Path(restored.prompts["USER"]).read_text() == f"# Orion — continuity profile\n\n{content}"
    assert (source / "memories/USER.md").read_text() == content
    assert not restored.memory.retain_enabled
    assert (restored.state_dir / "migration/manifest.json").is_file()
    output = capsys.readouterr()
    assert "Imported jobs and memory retention remain disabled" in output.out
    assert not output.err


def test_migrate_failure_does_not_print_source_paths_or_content(tmp_path, capsys):
    initialize_config(state_dir=tmp_path / "state")
    source = tmp_path / "private-fixture-source"
    source.mkdir()
    (source / "config.yaml").write_text("model: [private-fixture-content\n")

    with pytest.raises(SystemExit) as result:
        cli.main(["migrate", "--source", str(source), "--without-credentials"])

    assert result.value.code == 1
    output = capsys.readouterr()
    assert "Profile import failed" in output.err
    assert "private-fixture" not in output.err and str(tmp_path) not in output.err
    assert not output.out
