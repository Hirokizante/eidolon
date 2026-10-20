import fcntl
import json
import plistlib
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from eidolon import cli
from eidolon.config import IdentityConfig, initialize_config, load_config, save_config
from eidolon.rebrand import ServiceHandoff, SourceService, migrate_state
from eidolon.storage import Store


class Keychain:
    def __init__(self):
        self.values = {("previous-app", "model"): "synthetic-credential"}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def delete_password(self, service, name):
        del self.values[service, name]


class Services:
    def __init__(self):
        self.events = []
        self.fail = False

    def stop(self, service):
        self.events.append(("stop", service.kind))

    def replace(self, services, config, configuration, *, activate):
        assert configuration.is_file()
        assert (config.state_dir / "migration/rebrand.json").is_file()
        self.events.append(("replace", activate))
        if self.fail:
            raise RuntimeError("synthetic replacement failure")

    def restore(self):
        self.events.append(("restore",))


@pytest.fixture
def installation(tmp_path, monkeypatch):
    config = initialize_config(state_dir=tmp_path / "source")
    config.identity = IdentityConfig(user_name="Rowan", companion_name="Aster")
    config.timezone = "Pacific/Auckland"
    config.memory.bank = "existing-bank"
    config.memory.enabled = config.memory.save_enabled = True
    config.memory.verified = config.memory.retain_enabled = True
    config.selected_provider().api_key_ref = "keyring:model"
    save_config(config)
    Path(config.prompts["SOUL"]).write_bytes(b"A private personality.\r\n")
    (config.state_dir / "memory-service").mkdir()
    (config.state_dir / "memory-service/settings.json").write_text(json.dumps({
        "runtime_python": "/independent/runtime/python",
        "credential_refs": {"HINDSIGHT_API_LLM_API_KEY": "keyring:model"},
        "database": {"data_dir": str(config.state_dir / "preserved-database"),
                     "password_ref": "keyring:model", "instance": "preserved"},
    }))
    store = Store(config.database_path)
    session = store.session_for(timezone=config.timezone)["id"]
    store.complete_turn(session, "A source fact.", "A source answer.", generation_id="kept")
    # Simulate a persisted task created with another product's namespace.
    with monkeypatch.context() as patch:
        patch.setattr("eidolon.storage.store.uuid.uuid5", lambda *a: UUID(int=17))
        task = store.create_maintenance_task(store.continuity_inputs(),
                                             expected_revision=0, control_revision=0)
    pending = store.pending_outbox()
    store.close()
    manifest = tmp_path / "private-manifest.json"
    manifest.write_text(json.dumps({"source": str(config.state_dir),
                                    "source_keychain_service": "previous-app"}))
    return config, manifest, tmp_path / "destination/config.yaml", task, pending


def test_copy_preserves_data_credentials_and_pending_identities(installation):
    original, manifest, destination, task, pending = installation
    keys, services = Keychain(), Services()
    report = migrate_state(manifest, destination, keychain=keys, services=services)
    copied = load_config(destination)
    assert copied.identity == original.identity
    assert copied.timezone == original.timezone
    assert copied.memory == original.memory
    assert copied.jobs_file == destination.parent / "jobs.json"
    assert Path(copied.prompts["SOUL"]).read_bytes() == b"A private personality.\r\n"
    assert Path(original.prompts["SOUL"]).read_bytes() == b"A private personality.\r\n"
    assert keys.values[("eidolon", "model")] == keys.values[("previous-app", "model")]
    assert report["status"] == "verified"
    assert "synthetic-credential" not in (destination.parent / "migration/rebrand.json").read_text()
    settings = json.loads((destination.parent / "memory-service/settings.json").read_text())
    assert settings["database"]["data_dir"] == str(original.state_dir / "preserved-database")
    store = Store(copied.database_path)
    try:
        assert store.pending_outbox() == pending
        resumed = store.create_maintenance_task(task["inputs"], expected_revision=0,
                                                 control_revision=0, resume_task_id=task["id"])
        assert resumed["id"] == task["id"]
        assert resumed["input_hash"] == task["input_hash"]
        assert store.create_maintenance_task(task["inputs"], expected_revision=0,
                                             control_revision=0)["id"] == task["id"]
        store.commit_continuity(task["id"], [{"op": "no_change", "expected_revision": 0}],
                                expected_revision=0, control_revision=0)
        assert store.create_maintenance_task(task["inputs"], expected_revision=0,
                                             control_revision=0)["id"] == task["id"]
        store.complete_turn(task["inputs"][0]["session_id"], "A source fact.", "A source answer.",
                            generation_id="kept")
        assert store.pending_outbox() == pending
    finally:
        store.close()
    assert original.database_path.is_file()
    assert services.events == []
    with pytest.raises(ValueError, match="must not exist"):
        migrate_state(manifest, destination, keychain=keys, services=services)


def test_sqlite_copy_includes_committed_wal_pages(installation):
    original, manifest, destination, *_ = installation
    with sqlite3.connect(original.database_path) as live:
        live.execute("PRAGMA journal_mode=WAL")
        session = live.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
        live.execute("INSERT INTO summaries VALUES (?,'kept WAL content','now')", (session,))
        live.commit()
        migrate_state(manifest, destination, keychain=Keychain(), services=Services())
        with sqlite3.connect(destination.parent / "room.sqlite3") as copied:
            assert copied.execute("SELECT content FROM summaries").fetchone() == (
                "kept WAL content",)


def test_migration_refuses_active_source_and_keeps_credentials(installation):
    original, manifest, destination, *_ = installation
    keys, services = Keychain(), Services()
    with (original.state_dir / "service.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="still running"):
            migrate_state(manifest, destination, keychain=keys, services=services)
    assert not destination.parent.exists()
    assert ("eidolon", "model") not in keys.values
    assert services.events == [("restore",)]


@pytest.mark.parametrize("failure", ["missing-secret", "conflicting-secret", "invalid-database"])
def test_failed_verification_never_publishes_or_overwrites(installation, failure):
    original, manifest, destination, *_ = installation
    keys = Keychain()
    if failure == "missing-secret":
        keys.values.clear()
    elif failure == "conflicting-secret":
        keys.values[("eidolon", "model")] = "different-existing-value"
    else:
        original.database_path.write_bytes(b"SQLite format 3\x00broken")
    before = keys.values.copy()
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        migrate_state(manifest, destination, keychain=keys, services=Services())
    assert not destination.parent.exists()
    assert keys.values == before
    assert not list(destination.parent.parent.glob(".eidolon-migration-*"))


def test_service_failure_preserves_recovery_copy_and_restores_source(installation, tmp_path):
    original, manifest, destination, *_ = installation
    source_agent = tmp_path / "previous.plist"
    source_agent.write_bytes(plistlib.dumps({"Label": "local.previous.service"}))
    values = json.loads(manifest.read_text())
    values["services"] = [{"kind": "app", "label": "local.previous.service", "plist": str(source_agent)}]
    manifest.write_text(json.dumps(values))
    keys, services = Keychain(), Services()
    services.fail = True
    with pytest.raises(RuntimeError, match="replacement failure"):
        migrate_state(manifest, destination, activate_services=True, keychain=keys, services=services)
    assert services.events == [("stop", "app"), ("replace", True), ("restore",)]
    assert ("eidolon", "model") not in keys.values
    assert original.database_path.is_file() and destination.is_file()
    assert json.loads((destination.parent / "migration/rebrand.json").read_text())["status"].startswith("failed")


def test_exact_config_aliases_are_private_inputs_and_do_not_change_prompt_text(installation):
    original, manifest, destination, *_ = installation
    raw = yaml.safe_load((original.state_dir / "config.yaml").read_text())
    raw["theme"]["name"] = "private-theme"
    raw["avatar"]["server"] = "private-route"
    raw["mcp_servers"] = [{"name": "private-route", "command": "example"}]
    (original.state_dir / "config.yaml").write_text(yaml.safe_dump(raw))
    values = json.loads(manifest.read_text())
    values["config_values"] = {"private-theme": "nord", "private-route": "avatar"}
    manifest.write_text(json.dumps(values))
    Path(original.prompts["SOUL"]).write_text("Keep private-theme as written.")
    migrate_state(manifest, destination, keychain=Keychain(), services=Services())
    copied = load_config(destination)
    assert copied.theme.name == "nord"
    assert copied.avatar.server == copied.mcp_servers[0].name == "avatar"
    assert Path(copied.prompts["SOUL"]).read_text() == "Keep private-theme as written."


@pytest.mark.parametrize("relative", ["source", "source/child"])
def test_overlapping_destination_is_rejected(installation, relative, tmp_path):
    _, manifest, _, *_ = installation
    with pytest.raises(ValueError, match="separate directories"):
        migrate_state(manifest, tmp_path / relative / "config.yaml", keychain=Keychain())


def test_source_symlinks_are_not_followed(installation, tmp_path):
    original, manifest, destination, *_ = installation
    (original.state_dir / "external").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        migrate_state(manifest, destination, keychain=Keychain())
    assert not destination.exists()


def test_manifest_must_be_outside_a_checkout(installation, tmp_path):
    _, manifest, destination, *_ = installation
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        migrate_state(manifest, destination, keychain=Keychain())


def test_cli_migration_does_not_require_destination_setup(installation, monkeypatch, capsys):
    _, manifest, destination, *_ = installation
    from eidolon import rebrand
    original = rebrand.migrate_state
    monkeypatch.setattr(rebrand, "migrate_state", lambda *a, **kw: original(
        *a, **kw, keychain=Keychain(), services=Services()))
    cli.main(["--config", str(destination), "migrate-state", "--manifest", str(manifest)])
    assert destination.is_file()
    assert "copied and verified" in capsys.readouterr().out


def test_real_service_handoff_uses_explicit_labels_and_preserves_agents(tmp_path, monkeypatch):
    config = initialize_config(state_dir=tmp_path / "destination")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    agent = tmp_path / "old.plist"
    agent.write_bytes(plistlib.dumps({"Label": "local.previous.service"}))
    service = SourceService(kind="app", label="local.previous.service", plist=agent)
    service._environment = {"EXAMPLE_CREDENTIAL": "synthetic-env-credential"}
    handoff = ServiceHandoff()
    loaded = {service.label}
    calls = []

    def run(*args):
        from subprocess import CompletedProcess
        calls.append(args)
        label = args[1].split("/")[-1] if len(args) > 1 else ""
        result = 0
        if args[0] == "print":
            result = 0 if label in loaded else 1
        elif args[0] == "bootout":
            loaded.discard(label)
        elif args[0] == "bootstrap":
            loaded.add(plistlib.loads(Path(args[2]).read_bytes())["Label"])
        return CompletedProcess(args, result)

    monkeypatch.setattr(handoff, "_run", run)
    handoff.stop(service)
    handoff.replace([service], config, config.state_dir / "config.yaml", activate=True)
    assert loaded == {"local.eidolon.service"}
    assert not agent.exists() and agent.with_suffix(".plist.pre-eidolon").exists()
    replacement = tmp_path / "home/Library/LaunchAgents/local.eidolon.service.plist"
    assert plistlib.loads(replacement.read_bytes())["ProgramArguments"][2] == "eidolon"
    assert plistlib.loads(replacement.read_bytes())["EnvironmentVariables"]["EXAMPLE_CREDENTIAL"] == "synthetic-env-credential"
    handoff.restore()
    assert loaded == {service.label} and agent.is_file() and not replacement.exists()
    assert calls[0] == ("print", f"gui/{__import__('os').getuid()}/local.previous.service")


async def test_fresh_install_keeps_local_history_without_external_memory(tmp_path, monkeypatch):
    from eidolon.engine import ConversationEngine
    from eidolon.service.runtime import RoomService

    config = initialize_config(state_dir=tmp_path / "fresh")
    assert config.identity == IdentityConfig()
    assert not config.memory.enabled and not config.memory.save_enabled
    assert config.memory.bank == ""
    assert not config.avatar.enabled and config.avatar.server == "avatar"
    assert "No personal profile has been configured" in Path(config.prompts["USER"]).read_text()

    class Provider:
        async def stream(self, *args, **kwargs):
            yield {"type": "token", "text": "A plain answer."}
            yield {"type": "finish", "reason": "stop"}

    class Memory:
        async def recall(self, *args, **kwargs):
            pytest.fail("Unconfigured memory must not be contacted")

    store = Store(config.database_path)
    engine = ConversationEngine(config, store, memory=Memory(), provider=Provider())
    try:
        session = store.session_for(timezone=config.timezone)["id"]
        events = [event async for event in engine.generate(session, "Hello")]
        assert events[-1]["type"] == "completed"
        assert [m["content"] for m in store.list_messages(session)] == ["Hello", "A plain answer."]
        assert store.pending_outbox() == []
    finally:
        await engine.close()
        store.close()
    service = RoomService(config, config.state_dir / "config.yaml")
    try:
        async def unexpected_probe():
            pytest.fail("Startup must not probe an unconfigured memory bank")
        monkeypatch.setattr(service.memory, "validate_bank", unexpected_probe)
        await service.check_memory()
        assert service.memory_status.startswith("unconfigured")
    finally:
        await service.close()


def test_timezone_setup_persists_detected_zone_and_preserves_existing_choice(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    destination = tmp_path / "fresh/config.yaml"
    cli.main(["--config", str(destination), "setup"])
    assert load_config(destination).timezone == "Pacific/Auckland"
    config = load_config(destination)
    config.timezone = "Europe/Paris"
    save_config(config)
    monkeypatch.setenv("TZ", "America/New_York")
    cli.main(["--config", str(destination), "setup"])
    assert load_config(destination).timezone == "Europe/Paris"
    assert "Timezone: Europe/Paris" in capsys.readouterr().out


def test_timezone_discovery_falls_back_to_utc(monkeypatch):
    from eidolon import config
    from datetime import datetime, timezone

    monkeypatch.setenv("TZ", "not-an-iana-zone")
    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable system timezone")
    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(config, "datetime", Clock)
    monkeypatch.setattr(Path, "resolve", unavailable)
    monkeypatch.setattr(Path, "read_text", unavailable)
    assert config.system_timezone() == "UTC"


def test_resumed_task_rejects_a_different_evidence_set(installation):
    original, _, _, task, _ = installation
    store = Store(original.database_path)
    try:
        inputs = [dict(row) for row in task["inputs"]]
        inputs[0]["user"] = "Different evidence"
        with pytest.raises(ValueError, match="does not match"):
            store.create_maintenance_task(inputs, expected_revision=0, control_revision=0,
                                           resume_task_id=task["id"])
    finally:
        store.close()


def test_migration_preserves_older_combined_memory_switch(installation):
    original, manifest, destination, *_ = installation
    path = original.state_dir / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["memory"].pop("save_enabled")
    path.write_text(yaml.safe_dump(raw))
    migrate_state(manifest, destination, keychain=Keychain(), services=Services())
    copied = load_config(destination)
    assert copied.memory.enabled and copied.memory.save_enabled


@pytest.mark.parametrize("report", [{"status": "failed"}, {}, [], "invalid-json"])
def test_incomplete_handoff_cannot_start_an_installation(tmp_path, report):
    config = initialize_config(state_dir=tmp_path / "private")
    path = config.state_dir / "migration/rebrand.json"
    path.parent.mkdir()
    path.write_text(report if report == "invalid-json" else json.dumps(report))
    with pytest.raises(ValueError, match="migration is incomplete"):
        load_config(config.state_dir / "config.yaml")


def test_cli_migration_failure_explains_recovery_without_echoing_private_errors(tmp_path, monkeypatch, capsys):
    from eidolon import rebrand
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-private-diagnostic")
    monkeypatch.setattr(rebrand, "migrate_state", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["--config", str(tmp_path / "destination/config.yaml"),
                  "migrate-state", "--manifest", str(tmp_path / "private-manifest.json")])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "preserved LaunchAgents" in output.err and "migration/rebrand.json" in output.err
    assert "synthetic-private-diagnostic" not in output.err and not output.out
