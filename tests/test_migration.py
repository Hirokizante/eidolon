import json
from pathlib import Path

import pytest

from eidolon.config import AppConfig, IdentityConfig, initialize_config, load_config
from eidolon.migration import migrate_hermes
from eidolon.scheduler import Job, load_jobs, save_jobs


@pytest.fixture(autouse=True)
def isolated_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def reject_credential_write(*args):
        pytest.fail("Migration unexpectedly accessed live credential storage")

    monkeypatch.setattr("keyring.set_password", reject_credential_write)


@pytest.mark.parametrize("user_name,companion_name", [
    ("Orion", "Vega"),
    ("星 [bold] {user}", "Echo"),
])
def test_one_time_migration_copies_settings_and_keeps_jobs_disabled(
    tmp_path, user_name, companion_name,
):
    source = tmp_path / "old-profile"
    (source / "memories").mkdir(parents=True)
    (source / "cron").mkdir()
    (source / "config.yaml").write_text("model:\n  default: test-model\n  base_url: https://example.com/v1\n")
    (source / ".env").write_text("HERMES_CUSTOM_EXAMPLE_COM_API_KEY=super-secret-value\nTAVILY_API_KEY=search-secret-value\n")
    (source / "memories/USER.md").write_text("Aster likes shared projects.")
    (source / "memories/MEMORY.md").write_text("History stays intact.")
    (source / "cron/jobs.json").write_text(json.dumps({"jobs": [{"id": "old", "name": "Daily reflection", "prompt": "Write a note", "schedule": {"kind": "cron", "expr": "0 22 * * *"}, "next_run_at": "2026-10-10T22:00:00+08:00"}]}))
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "Room", prompts={"USER": str(tmp_path / "Room/USER.md")},
                       identity=IdentityConfig(user_name=user_name, companion_name=companion_name))
    captured = {}
    def credential_store(name, value):
        captured[name] = value
        return "keyring:" + name
    report = migrate_hermes(config, source, credential_store=credential_store)
    assert report["jobs"][0]["timezone_verified"]
    assert captured["provider-migrated"] == "super-secret-value"
    assert config.selected_provider().endpoint == "https://example.com/v1"
    assert config.provider == "migrated"
    assert not config.selected_provider().structured_verified
    assert config.selected_provider().api_key_ref == "keyring:provider-migrated"
    assert config.memory.bank == "hermes" and not config.memory.retain_enabled
    assert not load_jobs(config.jobs_file)[0].enabled
    assert (tmp_path / "Room/USER.md").read_text() == (
        f"# {user_name} — continuity profile\n\nAster likes shared projects."
    )
    assert Path(config.prompts["USER"]).read_text() == (tmp_path / "Room/USER.md").read_text()
    assert load_config(config.state_dir / "config.yaml").identity == config.identity
    for path in (tmp_path / "Room").rglob("*"):
        if path.is_file():
            assert "super-secret-value" not in path.read_text()
            assert "search-secret-value" not in path.read_text()
    before = {path: path.read_bytes() for path in config.state_dir.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="already has"):
        migrate_hermes(config, source, credential_store=credential_store)
    assert {path: path.read_bytes() for path in config.state_dir.rglob("*") if path.is_file()} == before
    assert (source / ".env").read_text().startswith("HERMES_CUSTOM")


def test_migration_requires_a_source_argument(tmp_path):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")

    with pytest.raises(TypeError, match="source"):
        migrate_hermes(config, credential_store=lambda *args: pytest.fail("Unexpected credential write"))

    assert not config.state_dir.exists()


@pytest.mark.parametrize("source", [None, "", "   "])
def test_migration_rejects_an_empty_source_without_discovery(tmp_path, monkeypatch, source):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")

    def reject_discovery(*args):
        pytest.fail("Migration attempted to discover a source in the home directory")

    monkeypatch.setattr("pathlib.Path.home", reject_discovery)
    with pytest.raises(ValueError, match="explicit.*source"):
        migrate_hermes(config, source)

    assert not config.state_dir.exists()


@pytest.mark.parametrize("field,value", [
    ("EXAMPLE_LIFECYCLE", "fixture-avatar-value"),
    ("BRIDGE_SIGNING_KEY", "abc123"),
    ("BRIDGE_MATERIAL", 'fixture-"秘密"-value'),
])
def test_avatar_credential_uses_only_the_explicit_source_field_and_is_redacted(
    tmp_path, field, value,
):
    source = tmp_path / "arbitrary-profile"
    (source / "memories").mkdir(parents=True)
    (source / "cron").mkdir()
    environment = f"{field}={value}\nOTHER_AVATAR_SECRET=unselected-fixture-value\n"
    (source / ".env").write_text(environment)
    for name in ("USER", "MEMORY"):
        (source / "memories" / f"{name}.md").write_text(f"Keep Aster's words. Secret: {value}")
    (source / "cron/jobs.json").write_text(json.dumps({"jobs": [{
        "id": "review", "prompt": f"Review the day. Secret: {value}", "cron": "0 8 * * *",
    }]}))
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")
    captured = {}

    def credential_store(name, credential):
        captured[name] = credential
        return f"keyring:{name}"

    report = migrate_hermes(config, source, avatar_secret_env=field, credential_store=credential_store)

    assert captured == {"avatar-lifecycle": value}
    assert report["credential_names"] == ["avatar-lifecycle"]
    assert config.avatar.signing_key_ref == "keyring:avatar-lifecycle"
    assert not config.avatar.enabled
    assert (source / ".env").read_text() == environment
    assert (source / "memories/USER.md").read_text().endswith(value)
    assert "Keep Aster's words." in Path(config.prompts["USER"]).read_text()
    assert "credential removed during migration" in load_jobs(config.jobs_file)[0].prompt
    original_jobs = json.loads((config.state_dir / "migration/original-jobs.json").read_text())
    assert original_jobs["jobs"][0]["prompt"] == (
        "Review the day. Secret: [credential removed during migration]"
    )
    for path in config.state_dir.rglob("*"):
        if path.is_file():
            assert value not in path.read_text()
            assert "unselected-fixture-value" not in path.read_text()


@pytest.mark.parametrize("field", [None, "MISSING_SIGNING_FIELD"])
def test_avatar_credential_is_preserved_without_a_matching_explicit_field(tmp_path, field):
    source = tmp_path / "arbitrary-profile"
    source.mkdir()
    (source / ".env").write_text("OTHER_AVATAR_SECRET=unselected-fixture-value\n")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")
    config.avatar.signing_key_ref = "env:CURRENT_AVATAR_KEY"

    report = migrate_hermes(config, source, avatar_secret_env=field)

    assert not report["credential_names"]
    assert config.avatar.signing_key_ref == "env:CURRENT_AVATAR_KEY"


@pytest.mark.parametrize("field", ["", " ", "PRIVATE-FIXTURE", "PRIVATE_FIXTURE\n", "FIELD=value"])
def test_invalid_avatar_field_is_rejected_without_echo_or_side_effects(tmp_path, field):
    source = tmp_path / "arbitrary-profile"
    source.mkdir()
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")

    with pytest.raises(ValueError, match="environment field") as error:
        migrate_hermes(config, source, avatar_secret_env=field)

    assert "PRIVATE" not in str(error.value) and "value" not in str(error.value)
    assert not config.state_dir.exists()


@pytest.mark.parametrize("is_file", [False, True])
def test_missing_or_nondirectory_source_fails_without_echoing_the_path(tmp_path, is_file):
    source = tmp_path / "private-fixture-source"
    if is_file:
        source.write_text("Synthetic source content")
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")

    with pytest.raises(FileNotFoundError, match="source profile does not exist") as error:
        migrate_hermes(config, source)

    assert "private-fixture" not in str(error.value)
    assert not config.state_dir.exists()


@pytest.fixture
def credential_source(tmp_path):
    source = tmp_path / "credential-profile"
    (source / "memories").mkdir(parents=True)
    (source / "config.yaml").write_text(
        "model:\n  default: fixture-model\n  base_url: https://example.com/v1\n"
    )
    (source / ".env").write_text(
        "OPENAI_API_KEY=fixture-provider-value\nTAVILY_API_KEY=fixture-search-value\n"
        "EXAMPLE_LIFECYCLE=fixture-avatar-value\n"
    )
    (source / "memories/USER.md").write_text(
        "Aster's source profile. fixture-provider-value fixture-search-value fixture-avatar-value"
    )
    return source


def test_without_credentials_still_redacts_copies_and_preserves_other_references(
    tmp_path, credential_source,
):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")
    config.tavily.api_key_ref = "env:CURRENT_SEARCH_KEY"
    config.avatar.signing_key_ref = "env:CURRENT_AVATAR_KEY"

    report = migrate_hermes(config, credential_source, import_credentials=False,
                            avatar_secret_env="EXAMPLE_LIFECYCLE")

    assert not report["credential_names"]
    assert config.provider == "migrated" and config.selected_provider().api_key_ref is None
    assert config.tavily.api_key_ref == "env:CURRENT_SEARCH_KEY"
    assert config.avatar.signing_key_ref == "env:CURRENT_AVATAR_KEY"
    profile = Path(config.prompts["USER"]).read_text()
    assert "Aster's source profile." in profile
    assert profile.count("[credential removed during migration]") == 3


def test_credential_store_failure_keeps_values_and_exception_details_out_of_reports(
    tmp_path, credential_source,
):
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room")
    config.tavily.api_key_ref = "env:CURRENT_SEARCH_KEY"
    config.avatar.signing_key_ref = "env:CURRENT_AVATAR_KEY"

    def unavailable_store(*args):
        raise RuntimeError("synthetic-sensitive-detail")

    report = migrate_hermes(config, credential_source, avatar_secret_env="EXAMPLE_LIFECYCLE",
                            credential_store=unavailable_store)

    assert len(report["warnings"]) == 3 and not report["credential_names"]
    assert "synthetic-sensitive-detail" not in json.dumps(report)
    assert config.tavily.api_key_ref == "env:CURRENT_SEARCH_KEY"
    assert config.avatar.signing_key_ref == "env:CURRENT_AVATAR_KEY"
    for path in config.state_dir.rglob("*"):
        if path.is_file():
            assert "fixture-provider-value" not in path.read_text()
            assert "fixture-search-value" not in path.read_text()
            assert "fixture-avatar-value" not in path.read_text()


@pytest.mark.parametrize("repetitions", [1, 20], ids=["short", "long"])
@pytest.mark.parametrize("configured_user_path", [False, True], ids=["default-path", "configured-path"])
def test_existing_custom_prompts_and_source_bytes_are_preserved(
    tmp_path, credential_source, repetitions, configured_user_path,
):
    config = initialize_config(state_dir=tmp_path / "room")
    for name, path in config.prompts.items():
        Path(path).write_bytes((f"Synthetic private {name}: Aster and Nova.\r\n" * repetitions).encode())
    before = {path: Path(path).read_bytes() for path in config.prompts.values()}
    source_before = {path: path.read_bytes() for path in credential_source.rglob("*") if path.is_file()}
    if not configured_user_path:
        config.prompts.pop("USER")

    report = migrate_hermes(config, credential_source, import_credentials=False,
                            avatar_secret_env="EXAMPLE_LIFECYCLE")

    assert {path: Path(path).read_bytes() for path in before} == before
    assert {path: path.read_bytes() for path in source_before} == source_before
    assert any("Existing USER.md was preserved" in warning for warning in report["warnings"])
    assert "Aster's source profile." in (config.state_dir / "migration/USER.md").read_text()


@pytest.mark.parametrize("source_settings,job_settings,verified", [
    ("", {"timezone": "Etc/UTC"}, True),
    ("timezone: Etc/UTC\n", {}, True),
    ("", {"next_run_at": "2026-10-10T08:00:00+00:00"}, True),
    ("", {"next_run_at": "2026-10-10T08:00:00+08:00"}, False),
    ("", {"next_run_at": "2026-10-10T08:00:00"}, False),
    ("", {}, False),
])
def test_imported_jobs_remain_disabled_with_explicit_timezone_evidence(
    tmp_path, source_settings, job_settings, verified,
):
    source = tmp_path / "schedule-profile"
    (source / "cron").mkdir(parents=True)
    (source / "config.yaml").write_text(source_settings)
    (source / "cron/jobs.json").write_text(json.dumps({"jobs": [{
        "id": "daily", "prompt": "Synthetic daily note", "cron": "0 8 * * *", **job_settings,
    }]}))
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room", timezone="Etc/UTC")

    report = migrate_hermes(config, source, import_credentials=False)

    assert report["jobs"][0]["timezone_verified"] is verified
    assert report["jobs"][0]["timezone_evidence"]
    assert report["jobs"][0]["enabled"] is False
    assert report["jobs"][0]["timezone"] == "Etc/UTC"
    assert not load_jobs(config.jobs_file)[0].enabled
    assert not config.memory.retain_enabled and config.memory.bank == "hermes"


def test_import_preserves_existing_jobs_and_skips_unsupported_schedules(tmp_path):
    source = tmp_path / "schedule-profile"
    (source / "cron").mkdir(parents=True)
    (source / "cron/jobs.json").write_text(json.dumps({"jobs": [
        {"id": "daily", "prompt": "Source duplicate", "cron": "0 8 * * *"},
        {"id": "new", "prompt": "New source job", "cron": "0 9 * * *"},
        {"id": "unsupported", "schedule": {"kind": "interval", "seconds": 60}},
    ]}))
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room", jobs_file=tmp_path / "room/jobs.json")
    existing = Job(id="migrated-daily", name="Saved daily note", prompt="Keep saved prompt",
                   enabled=True)
    save_jobs(config.jobs_file, [existing])

    report = migrate_hermes(config, source, import_credentials=False)

    jobs = load_jobs(config.jobs_file)
    assert jobs[0] == existing
    assert [job.id for job in jobs] == ["migrated-daily", "migrated-new"]
    assert jobs[1].prompt == "New source job" and not jobs[1].enabled
    assert any("unsupported schedule" in warning for warning in report["warnings"])
