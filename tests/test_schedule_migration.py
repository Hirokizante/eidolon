import fcntl
import json
from pathlib import Path

import pytest

from eidolon.config import AppConfig
from eidolon.scheduler import Job
from eidolon.scheduler import migration


VERIFIED = {"scheduled_job_verified": True, "service_installed": True,
            "verification_evidence": "Isolated scheduled job completed; background service installed"}


@pytest.fixture(autouse=True)
def isolated_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def reject_credential_write(*args):
        pytest.fail("Schedule handoff unexpectedly accessed live credential storage")

    monkeypatch.setattr("keyring.set_password", reject_credential_write)


@pytest.fixture
def schedules(tmp_path):
    source = tmp_path / "legacy-profile"
    original = source / "cron" / "jobs.json"
    original.parent.mkdir(parents=True)
    source_data = {"jobs": [{"id": "daily", "name": "Daily reflection", "enabled": True,
                            "state": "scheduled", "paused_at": None, "paused_reason": None,
                            "fire_claim": None, "prompt": "Reflect on the day.",
                            "schedule": {"kind": "cron", "expr": "0 22 * * *"},
                            "next_run_at": "2026-10-10T22:00:00+08:00"},
                           {"id": "unrelated", "name": "Keep me", "enabled": True,
                            "schedule": {"kind": "cron", "expr": "0 8 * * *"}}],
                   "version": 7, "private_metadata": "not-public"}
    original.write_text(json.dumps(source_data))
    config = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, state_dir=tmp_path / "room", jobs_file=tmp_path / "room" / "jobs.json")
    checkpoint = config.state_dir / "migration"
    checkpoint.mkdir(parents=True)
    (checkpoint / "original-jobs.json").write_text(json.dumps(source_data, indent=2))
    manifest = {"source": str(source), "jobs": [{"id": "migrated-daily", "cron": "0 22 * * *",
                "timezone": "Asia/Manila", "timezone_verified": True, "enabled": False}],
                "credential_names": ["provider"]}
    (checkpoint / "manifest.json").write_text(json.dumps(manifest))
    job = Job(id="migrated-daily", name="Daily reflection", prompt="Return full note", enabled=False)
    config.jobs_file.write_text(json.dumps({"jobs": [job.model_dump(),
        Job(id="other", name="Other", prompt="Other").model_dump()], "settings": {"preserve": True}}))
    return config, source, original, source_data


@pytest.mark.parametrize("use_manifest_source", [False, True])
def test_transfer_pauses_only_matching_source_preserves_settings_and_is_idempotent(
    schedules, use_manifest_source,
):
    config, source, original, before = schedules
    old_raw = original.read_bytes()
    app_raw = config.jobs_file.read_bytes()
    selected_source = None if use_manifest_source else source
    report = migration.transfer(config, selected_source, **VERIFIED)
    assert report["status"] == "complete"
    after = json.loads(original.read_text())
    assert after["jobs"][0]["enabled"] is False
    assert after["jobs"][0]["state"] == "paused"
    assert after["jobs"][0]["paused_at"]
    assert after["jobs"][0]["paused_reason"] == (
        "Transferred to the app scheduler after isolated schedule verification"
    )
    assert after["jobs"][1] == before["jobs"][1]
    assert after["version"] == before["version"]
    app = json.loads(config.jobs_file.read_text())
    assert app["jobs"][0]["enabled"] is True
    assert app["jobs"][0]["id"] == "migrated-daily"
    assert app["jobs"][0]["cron"] == "0 22 * * *"
    assert "compartment" not in app["jobs"][0]
    assert "compartments" not in app["jobs"][0]
    assert app["jobs"][1]["enabled"] is False
    assert app["settings"] == {"preserve": True}
    assert Path(report["source_backup"]).read_bytes() == old_raw
    assert Path(report["app_backup"]).read_bytes() == app_raw
    assert Path(report["source_backup"]).stat().st_mode & 0o777 == 0o600
    assert Path(report["app_backup"]).stat().st_mode & 0o777 == 0o600
    assert report["source_path"] == str(original)
    assert report["job_ids"] == ["migrated-daily"]
    assert report["verification_evidence"] == VERIFIED["verification_evidence"]
    assert "not-public" not in json.dumps(report)
    completed = {path: path.read_bytes() for path in (original, config.jobs_file)}
    assert migration.transfer(config, selected_source, **VERIFIED) == report
    assert {path: path.read_bytes() for path in completed} == completed
    assert len(list((config.state_dir / "migration").glob("schedule-handoff-*"))) == 1
    manifest = json.loads((config.state_dir / "migration/manifest.json").read_text())
    assert manifest["jobs"][0]["enabled"]
    assert manifest["credential_names"] == ["provider"]


@pytest.mark.parametrize("phase", ["before-source", "after-source", "after-app"])
def test_interrupted_handoff_resumes_without_both_schedulers_enabled(schedules, monkeypatch, phase):
    config, source, original, _ = schedules
    source_raw, app_raw = original.read_bytes(), config.jobs_file.read_bytes()
    real_write = migration._write
    failed = False
    def crash(path, payload):
        nonlocal failed
        trigger = (Path(path) == original and phase in {"before-source", "after-source"}) or (Path(path) == config.jobs_file and phase == "after-app")
        if trigger and not failed:
            failed = True
            if phase != "before-source":
                real_write(path, payload)
            raise OSError("Interrupted write")
        real_write(path, payload)
    monkeypatch.setattr(migration, "_write", crash)
    with pytest.raises(OSError, match="Interrupted"):
        migration.transfer(config, source, **VERIFIED)
    source_enabled = json.loads(original.read_text())["jobs"][0]["enabled"]
    app_enabled = json.loads(config.jobs_file.read_text())["jobs"][0]["enabled"]
    assert not (source_enabled and app_enabled)
    manifest_path = config.state_dir / "migration/manifest.json"
    interrupted = json.loads(manifest_path.read_text())["schedule_handoff"]
    checkpoint = Path(interrupted["checkpoint"])
    checkpoint_files = {path: path.read_bytes() for path in checkpoint.iterdir()}
    assert Path(interrupted["source_backup"]).read_bytes() == source_raw
    assert Path(interrupted["app_backup"]).read_bytes() == app_raw
    for missing in VERIFIED:
        verification = {**VERIFIED, missing: "" if missing == "verification_evidence" else False}
        with pytest.raises(ValueError, match="Verify an isolated"):
            migration.transfer(config, **verification)
    monkeypatch.setattr(migration, "_write", real_write)
    config.identity.user_name = "Aster"
    config.identity.companion_name = "Echo"
    report = migration.transfer(config, **VERIFIED)
    assert report["status"] == "complete"
    assert report["checkpoint"] == str(checkpoint)
    assert {path: path.read_bytes() for path in checkpoint.iterdir()} == checkpoint_files
    assert len(list(checkpoint.parent.glob("schedule-handoff-*"))) == 1
    assert not json.loads(original.read_text())["jobs"][0]["enabled"]
    assert json.loads(config.jobs_file.read_text())["jobs"][0]["enabled"]
    assert [job["id"] for job in json.loads(config.jobs_file.read_text())["jobs"]] == [
        "migrated-daily", "other",
    ]


@pytest.mark.parametrize("profile_name,user_name,companion_name", [
    ("arbitrary-source", "Orion", "Vega"),
    ("星 profile", "Aster [bold]", "Echo {companion}"),
])
def test_handoff_uses_arbitrary_private_source_independently_of_display_names(
    schedules, profile_name, user_name, companion_name,
):
    config, source, original, before = schedules
    selected = source.with_name(profile_name)
    source.rename(selected)
    original = selected / "cron/jobs.json"
    manifest_path = config.state_dir / "migration/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"] = str(selected)
    manifest_path.write_text(json.dumps(manifest))
    config.identity.user_name = user_name
    config.identity.companion_name = companion_name

    report = migration.transfer(config, str(selected), **VERIFIED)

    paused = json.loads(original.read_text())["jobs"][0]
    assert paused["prompt"] == before["jobs"][0]["prompt"]
    assert paused["name"] == before["jobs"][0]["name"]
    assert paused["paused_reason"] == (
        "Transferred to the app scheduler after isolated schedule verification"
    )
    assert report["source_path"] == str(original)
    assert report["job_ids"] == ["migrated-daily"]
    assert user_name not in json.dumps(report) and companion_name not in json.dumps(report)


@pytest.mark.parametrize("recorded_source", [..., None, "", "   ", 42, [], {}])
@pytest.mark.parametrize("use_explicit_source", [False, True])
def test_missing_manifest_source_fails_generically_without_schedule_changes(
    schedules, monkeypatch, recorded_source, use_explicit_source,
):
    config, source, original, _ = schedules
    monkeypatch.chdir(source)
    manifest_path = config.state_dir / "migration/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if recorded_source is ...:
        manifest.pop("source")
    else:
        manifest["source"] = recorded_source
    manifest_path.write_text(json.dumps(manifest))
    before = {path: path.read_bytes() for path in (original, config.jobs_file, manifest_path)}

    with pytest.raises(ValueError, match="recorded migration source directory") as error:
        migration.transfer(config, source if use_explicit_source else None, **VERIFIED)

    assert str(source) not in str(error.value)
    assert {path: path.read_bytes() for path in before} == before
    assert not list(manifest_path.parent.glob("schedule-handoff-*"))


@pytest.mark.parametrize("explicit_source", ["", "   "])
def test_blank_explicit_source_fails_instead_of_falling_back_to_manifest(
    schedules, monkeypatch, explicit_source,
):
    config, source, original, _ = schedules
    monkeypatch.chdir(source)
    manifest_path = config.state_dir / "migration/manifest.json"
    before = {path: path.read_bytes() for path in (original, config.jobs_file, manifest_path)}

    with pytest.raises(ValueError, match="nonempty source directory"):
        migration.transfer(config, explicit_source, **VERIFIED)

    assert {path: path.read_bytes() for path in before} == before
    assert not list(manifest_path.parent.glob("schedule-handoff-*"))


def test_explicit_source_must_match_manifest_without_echoing_paths(schedules):
    config, source, original, _ = schedules
    manifest_path = config.state_dir / "migration/manifest.json"
    before = {path: path.read_bytes() for path in (original, config.jobs_file, manifest_path)}

    with pytest.raises(ValueError, match="differs from.*recorded") as error:
        migration.transfer(config, source.with_name("other-private-source"), **VERIFIED)

    assert "other-private-source" not in str(error.value)
    assert str(source) not in str(error.value)
    assert {path: path.read_bytes() for path in before} == before


def test_changed_source_fails_without_overwriting_changes(schedules):
    config, source, original, _ = schedules
    changed = json.loads(original.read_text())
    changed["jobs"][1]["name"] = "Edited independently"
    original.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="changed since migration"):
        migration.transfer(config, source, **VERIFIED)
    assert json.loads(original.read_text()) == changed
    assert not json.loads(config.jobs_file.read_text())["jobs"][0]["enabled"]


def test_changed_app_after_interruption_fails_without_overwriting_changes(schedules, monkeypatch):
    config, source, original, _ = schedules
    real_write = migration._write
    def crash(path, payload):
        if Path(path) == config.jobs_file:
            raise OSError("Interrupted")
        real_write(path, payload)
    monkeypatch.setattr(migration, "_write", crash)
    with pytest.raises(OSError):
        migration.transfer(config, source, **VERIFIED)
    changed = json.loads(config.jobs_file.read_text())
    changed["settings"]["preserve"] = "changed"
    config.jobs_file.write_text(json.dumps(changed))
    monkeypatch.setattr(migration, "_write", real_write)
    with pytest.raises(ValueError, match="App schedules changed"):
        migration.transfer(config, source, **VERIFIED)
    assert json.loads(config.jobs_file.read_text()) == changed
    assert not json.loads(original.read_text())["jobs"][0]["enabled"]


@pytest.mark.parametrize("missing", ["scheduled_job_verified", "service_installed", "verification_evidence"])
def test_required_verification_cannot_be_omitted(schedules, missing):
    config, source, original, _ = schedules
    verification = dict(VERIFIED)
    verification[missing] = "" if missing == "verification_evidence" else False
    with pytest.raises(ValueError, match="Verify an isolated"):
        migration.transfer(config, source, **verification)
    assert json.loads(original.read_text())["jobs"][0]["enabled"]


def test_active_source_claim_and_unverified_timezone_block_handoff(schedules):
    config, source, original, _ = schedules
    data = json.loads(original.read_text())
    data["jobs"][0]["fire_claim"] = {"owner": "active"}
    original.write_text(json.dumps(data))
    (config.state_dir / "migration/original-jobs.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="still active"):
        migration.transfer(config, source, **VERIFIED)
    data["jobs"][0]["fire_claim"] = None
    original.write_text(json.dumps(data))
    (config.state_dir / "migration/original-jobs.json").write_text(json.dumps(data))
    manifest_path = config.state_dir / "migration/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["jobs"][0]["timezone_verified"] = False
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="timezone verification"):
        migration.transfer(config, source, **VERIFIED)


def test_running_source_writer_lock_prevents_handoff(schedules):
    config, source, original, _ = schedules
    with (original.parent / ".jobs.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="owns a required lock"):
            migration.transfer(config, source, **VERIFIED)
    assert json.loads(original.read_text())["jobs"][0]["enabled"]
