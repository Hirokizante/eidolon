"""Explicit installation migration using private predecessor identifiers.

Source state is copied, never rebranded in place. Persisted evidence and pending
operations retain their original identities; only configuration paths change.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, PrivateAttr, field_validator

from .config import (
    AppConfig, SettingsModel, _require_private_location, atomic_write,
    save_config, validate_secret_reference,
)


class SourceService(SettingsModel):
    kind: Literal["app", "memory"]
    label: str
    plist: Path
    _environment: dict[str, str] = PrivateAttr(default_factory=dict)

    @field_validator("label")
    @classmethod
    def valid_label(cls, value: str) -> str:
        if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for c in value):
            raise ValueError("Service labels must contain only letters, numbers, dots, hyphens, and underscores")
        if value in {"local.eidolon.service", "local.eidolon.hindsight"}:
            raise ValueError("The source service must differ from the destination service")
        return value


class MigrationManifest(SettingsModel):
    source: Path
    configuration: str = "config.yaml"
    source_keychain_service: str | None = None
    # Exact configuration values, e.g. retired theme aliases or MCP route names.
    # Never apply these substitutions to transcripts, prompts, or queued payloads.
    config_values: dict[str, str] = Field(default_factory=dict)
    credential_names: list[str] = Field(default_factory=list)
    services: list[SourceService] = Field(default_factory=list)

    @field_validator("configuration")
    @classmethod
    def config_filename(cls, value: str) -> str:
        if Path(value).name != value or value in {"", ".", ".."}:
            raise ValueError("The source configuration must be a filename within the source directory")
        return value


def _rewrite(value: Any, source: Path, destination: Path, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite(item, source, destination, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, source, destination, replacements) for item in value]
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        if value == str(source) or value.startswith(str(source) + "/"):
            return str(destination) + value[len(str(source)):]
    return value


def _references(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_references(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_references(item) for item in value)) if value else set()
    if isinstance(value, str) and value.startswith("keyring:"):
        return {validate_secret_reference(value)[8:]}
    return set()


def _database_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in connection.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _copy_database(source: Path, target: Path) -> None:
    # Read-only backup includes committed WAL pages without modifying the source.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as copied:
            original.backup(copied)
            if (copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or copied.execute("PRAGMA foreign_key_check").fetchall()
                    or _database_digest(original) != _database_digest(copied)):
                raise ValueError("The copied database failed verification")


class ServiceHandoff:
    """Quiesce explicit source agents and restore them if handoff fails."""

    def __init__(self) -> None:
        self.stopped: list[SourceService] = []
        self.retired: list[tuple[Path, Path]] = []
        self.installed: list[tuple[str, Path]] = []

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/launchctl", *args], capture_output=True, timeout=15)

    def stop(self, service: SourceService) -> None:
        domain = f"gui/{os.getuid()}"
        identity = f"{domain}/{service.label}"
        if self._run("print", identity).returncode == 0:
            # Record before bootout so even an uncertain response is recoverable.
            self.stopped.append(service)
            self._run("bootout", identity)
            if self._run("print", identity).returncode == 0:
                raise RuntimeError("The source service could not be stopped")

    def replace(self, services: list[SourceService], config: AppConfig,
                configuration: Path, *, activate: bool) -> None:
        from .service import launchd
        from .memory import runtime
        targets = {"app": launchd.plist_path(),
                   "memory": Path.home() / "Library/LaunchAgents" / f"{runtime.LABEL}.plist"}
        if any(targets[s.kind].exists() for s in services):
            raise ValueError("A destination LaunchAgent already exists; inspect it before migration")
        ordered = sorted(services, key=lambda service: service.kind != "memory")
        for service in ordered:
            backup = service.plist.with_suffix(".plist.pre-eidolon")
            if backup.exists():
                raise ValueError("A source LaunchAgent backup already exists")
            service.plist.rename(backup)
            self.retired.append((service.plist, backup))
            label = launchd.LABEL if service.kind == "app" else runtime.LABEL
            target = targets[service.kind]
            self.installed.append((label, target))
            if service.kind == "app":
                launchd.install(config, configuration)
            else:
                settings = config.state_dir / "memory-service/settings.json"
                if not settings.is_file():
                    raise ValueError("Managed-memory service settings are missing")
                runtime.write_launchagent(settings)
            # Preserve private env: credentials and integration environment from
            # the source agent, alongside the new launcher's required defaults.
            if service._environment:
                payload = plistlib.loads(target.read_bytes())
                payload["EnvironmentVariables"] = {
                    **payload.get("EnvironmentVariables", {}), **service._environment,
                }
                atomic_write(target, plistlib.dumps(payload).decode(), mode=0o600)
        if activate:
            for _, target in self.installed:
                result = self._run("bootstrap", f"gui/{os.getuid()}", str(target))
                if result.returncode:
                    raise RuntimeError("The replacement service could not be activated")

    def restore(self) -> None:
        failed = False
        for label, target in reversed(self.installed):
            identity = f"gui/{os.getuid()}/{label}"
            self._run("bootout", identity)
            if self._run("print", identity).returncode == 0:
                # Never restart the source while the replacement still owns work.
                raise RuntimeError("Replacement service is still active; source services remain stopped")
            target.unlink(missing_ok=True)
        for original, backup in reversed(self.retired):
            backup.rename(original)
        for service in reversed(self.stopped):
            result = self._run("bootstrap", f"gui/{os.getuid()}", str(service.plist))
            failed |= result.returncode != 0
        if failed:
            raise RuntimeError("A source service needs manual recovery from its preserved LaunchAgent")


def migrate_state(manifest_path: Path, configuration: Path, *, activate_services: bool = False,
                  keychain: Any = None, services: Any = None) -> dict:
    """Copy a stopped installation into an absent destination, then hand off services.

    A failed published destination is retained for inspection. Retrying never
    merges or overwrites state. The original remains available for recovery.
    """
    manifest_path = manifest_path.expanduser().resolve()
    _require_private_location(manifest_path)
    manifest = MigrationManifest.model_validate(json.loads(manifest_path.read_text()))
    source = manifest.source.expanduser().resolve()
    configuration = configuration.expanduser().resolve()
    destination = configuration.parent
    for path in (source, destination):
        _require_private_location(path)
    if (source == destination or source in destination.parents or destination in source.parents):
        raise ValueError("Migration source and destination must be separate directories")
    if not source.is_dir() or not (source / manifest.configuration).is_file():
        raise ValueError("An explicit source installation and configuration are required")
    if destination.exists():
        raise ValueError("Migration destination must not exist; existing state is never overwritten")
    if activate_services and not manifest.services:
        raise ValueError("Service activation requires explicit source service entries")
    if len({s.kind for s in manifest.services}) != len(manifest.services):
        raise ValueError("Only one source service of each kind may be migrated")
    for service in manifest.services:
        service.plist = service.plist.expanduser().resolve()
        _require_private_location(service.plist)
        payload = plistlib.loads(service.plist.read_bytes())
        if payload.get("Label") != service.label:
            raise ValueError("Source LaunchAgent label does not match the manifest")
        environment = payload.get("EnvironmentVariables", {})
        if (not isinstance(environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in environment.items())):
            raise ValueError("Source LaunchAgent environment must contain string values")
        service._environment = _rewrite(environment, source, destination, manifest.config_values)
    # Refuse symlinks before reading/copying state or following external paths.
    entries = list(source.rglob("*"))
    if any(p.is_symlink() for p in entries):
        raise ValueError("Source state contains symlinks; resolve them explicitly before migration")
    raw = yaml.safe_load((source / manifest.configuration).read_text())
    if not isinstance(raw, dict):
        raise ValueError("Source configuration must be an object")
    if Path(raw.get("state_dir", source)).expanduser().resolve() != source:
        raise ValueError("Source configuration state directory differs from the manifest")
    # Before saving was independent, an enabled recall switch also enabled saving.
    source_memory = raw.get("memory")
    if isinstance(source_memory, dict) and "enabled" in source_memory and "save_enabled" not in source_memory:
        source_memory["save_enabled"] = source_memory["enabled"]
    rewritten = _rewrite(raw, source, destination, manifest.config_values)
    rewritten["state_dir"] = str(destination)
    config = AppConfig.model_validate(rewritten)
    # Path substitution must not leave editable prompts pointing into the source.
    if any(not Path(p).is_relative_to(destination) for p in config.prompts.values()):
        raise ValueError("Source prompts must be inside source state before migration")
    if config.jobs_file is not None and not config.jobs_file.is_relative_to(destination):
        raise ValueError("Source jobs must be inside source state before migration")
    memory_settings = source / "memory-service/settings.json"
    settings = None
    if memory_settings.is_file():
        original_settings = json.loads(memory_settings.read_text())
        settings = _rewrite(original_settings, source, destination, manifest.config_values)
        # pg0 instance identity and its PostgreSQL files stay attached in place.
        settings["database"] = original_settings["database"]
    names = _references(rewritten) | _references(settings) | set(manifest.credential_names)
    for name in names:
        validate_secret_reference("keyring:" + name)
    if names and not manifest.source_keychain_service:
        raise ValueError("An explicit source Keychain service is required for credential transfer")
    if names and manifest.source_keychain_service == "eidolon":
        raise ValueError("Source and destination Keychain services must differ")
    if keychain is None:
        import keyring
        keychain = keyring
    services = services or ServiceHandoff()
    added_credentials: list[str] = []
    staging = None
    published = False
    report = {"status": "verified", "credentials": sorted(names),
              "source_preserved": True, "services_activated": activate_services,
              "services": [s.kind for s in manifest.services]}
    try:
        for service in manifest.services:
            services.stop(service)
        # The same lock is held by the application before it can open SQLite or run jobs.
        lock = source / "service.lock"
        if lock.is_symlink():
            raise ValueError("Source service lock must not be a symlink")
        with lock.open("a") as owner:
            try:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("The source application is still running; stop it before migration") from None
            entries = list(source.rglob("*"))
            if any(p.is_symlink() for p in entries):
                raise ValueError("Source state contains symlinks; resolve them explicitly before migration")
            locked_raw = yaml.safe_load((source / manifest.configuration).read_text())
            locked_memory = locked_raw.get("memory", {}) if isinstance(locked_raw, dict) else {}
            if isinstance(locked_memory, dict) and "enabled" in locked_memory and "save_enabled" not in locked_memory:
                locked_memory["save_enabled"] = locked_memory["enabled"]
            if locked_raw != raw:
                raise ValueError("Source configuration changed while stopping services; retry migration")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = Path(tempfile.mkdtemp(prefix=".eidolon-migration-", dir=destination.parent))
            for path in entries:
                relative = path.relative_to(source)
                if path.name in {"service.lock"} or path.name.endswith(("-wal", "-shm")):
                    continue
                mode = path.stat().st_mode
                if stat.S_ISSOCK(mode):
                    continue
                target = staging / relative
                if path.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                if not stat.S_ISREG(mode):
                    raise ValueError("Source state contains unsupported special files")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with path.open("rb") as stream:
                    database = stream.read(16) == b"SQLite format 3\x00"
                if database:
                    _copy_database(path, target)
                else:
                    shutil.copyfile(path, target)
                target.chmod(0o600)
            # Validate prompts/configuration in staging while serializing final paths.
            for path in config.prompts.values():
                if not (staging / Path(path).relative_to(destination)).is_file():
                    raise ValueError("A configured prompt is missing from the copied state")
            if config.jobs_file and config.jobs_file.is_relative_to(destination):
                from .scheduler import load_jobs
                load_jobs(staging / config.jobs_file.relative_to(destination))
            if settings is not None:
                atomic_write(staging / "memory-service/settings.json", json.dumps(settings, indent=2) + "\n")
            # save_config would privatize final paths before publication; write its
            # already-validated equivalent here, then validate again after publish.
            atomic_write(staging / configuration.name,
                         yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
            for name in sorted(names):
                value = keychain.get_password(manifest.source_keychain_service, name)
                if not value:
                    raise ValueError("A source Keychain credential is missing")
                existing = keychain.get_password("eidolon", name)
                if existing is not None and existing != value:
                    raise ValueError("A destination Keychain credential differs; it was not overwritten")
                if existing is None:
                    added_credentials.append(name)
                    keychain.set_password("eidolon", name, value)
                if keychain.get_password("eidolon", name) != value:
                    raise ValueError("Credential transfer could not be verified")
            atomic_write(staging / "migration/rebrand.json", json.dumps(report, indent=2) + "\n")
            if destination.exists():
                raise ValueError("Migration destination appeared during copying; it was not overwritten")
            staging.rename(destination)
            published = True
            save_config(config, configuration)
            if manifest.services:
                services.replace(manifest.services, config, configuration, activate=activate_services)
        return report
    except BaseException:
        if published:
            atomic_write(destination / "migration/rebrand.json",
                         json.dumps({**report, "status": "failed; inspect before retrying"}, indent=2) + "\n")
        try:
            services.restore()
        finally:
            for name in reversed(added_credentials):
                keychain.delete_password("eidolon", name)
        raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
