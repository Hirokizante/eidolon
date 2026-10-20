"""Explicit, journaled one-time schedule handoff; never imported by normal runtime.

The source's pause fields and file locks were audited read-only in cron/jobs.py:
enabled=False, state='paused', paused_at, paused_reason; .jobs.lock and the
per-job .fire-<uuid5>.lock fence. No source package is imported or executed.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..config import atomic_write
from .jobs import load_jobs


def _read(path):
    raw = Path(path).read_bytes()
    return raw, json.loads(raw.decode("utf-8-sig"))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _write(path, value):
    atomic_write(Path(path), json.dumps(value, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    _sync_directory(Path(path).parent)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The scheduler or another handoff owns a required lock; retry after it finishes") from None
        yield


def _backup(path, raw):
    # Exclusive creation protects previous checkpoints. Backups contain no .env files.
    with path.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _rows(payload):
    records = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("Schedule file must contain a list of job objects")
    identifiers = [row.get("id") for row in records]
    if None in identifiers or len(identifiers) != len(set(identifiers)):
        raise ValueError("Schedule IDs must be present and unique")
    return records


def _source_job_id(app_id):
    if not app_id.startswith("migrated-"):
        raise ValueError("Only explicitly migrated schedules can be transferred")
    return app_id.removeprefix("migrated-")


def transfer(config, source: Path | str | None = None, *, scheduled_job_verified: bool = False,
             service_installed: bool = False, verification_evidence: str = "", job_ids=None) -> dict:
    """Pause matching original schedules, then enable verified replacements.

    Verification flags are explicit caller attestations following an isolated
    scheduled job test and LaunchAgent installation. They are required even
    when resuming an interrupted handoff. The initial source must exactly match
    the app-owned migration checkpoint; unreviewed source changes fail closed.
    Source locations come only from the caller or that private checkpoint.
    File writes are atomic individually, with a durable phase journal ensuring
    a crash can leave both schedulers disabled but can never enable both.
    """
    if not scheduled_job_verified or not service_installed or not verification_evidence.strip():
        raise ValueError("Verify an isolated scheduled job and the installed background service, and provide verification evidence before activating migrated schedules")
    if not config.jobs_file:
        raise ValueError("Configure the app-owned jobs file before schedule handoff")
    migration = Path(config.state_dir) / "migration"
    manifest_path = migration / "manifest.json"
    with _lock(migration / "handoff.lock"):
        _, manifest = _read(manifest_path)
        recorded_source = manifest.get("source")
        if not isinstance(recorded_source, str) or not recorded_source.strip():
            raise ValueError("Schedule handoff requires a recorded migration source directory")
        if isinstance(source, str) and not source.strip():
            raise ValueError("Schedule handoff requires a nonempty source directory")
        profile = Path(recorded_source if source is None else source).expanduser().resolve()
        if profile != Path(recorded_source).expanduser().resolve():
            raise ValueError("Schedule source differs from the recorded one-time migration source")
        source_path = profile / "cron" / "jobs.json"
        app_path = Path(config.jobs_file).expanduser().resolve()
        if source_path.resolve() == app_path:
            raise ValueError("Original and replacement schedules must use independent files")
        records = manifest.get("jobs", [])
        selected = sorted(job_ids or [record["id"] for record in records])
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("Select at least one distinct migrated schedule")
        known = {record["id"]: record for record in records}
        for app_id in selected:
            if app_id not in known or not known[app_id].get("timezone_verified"):
                raise ValueError("Each selected schedule needs recorded source timezone verification")
            _source_job_id(app_id)
        with ExitStack() as locks:
            # Match the source's lock order: per-job fence before the jobs file lock.
            for app_id in selected:
                key = f"{source_path.parent.resolve()}::{_source_job_id(app_id)}"
                fence = source_path.parent / f".fire-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}.lock"
                locks.enter_context(_lock(fence))
            locks.enter_context(_lock(source_path.parent / ".jobs.lock"))
            locks.enter_context(_lock(app_path.parent / ".jobs.lock"))
            source_raw, source_data = _read(source_path)
            app_raw, app_data = _read(app_path)
            source_rows, app_rows = _rows(source_data), _rows(app_data)
            source_by_id = {row["id"]: row for row in source_rows}
            app_by_id = {row["id"]: row for row in app_rows}
            handoff = manifest.get("schedule_handoff")
            if handoff:
                if handoff["job_ids"] != selected or handoff["source_path"] != str(source_path):
                    raise ValueError("An existing handoff covers a different schedule selection")
                if _hash(source_data) not in {handoff["source_before_hash"], handoff["source_after_hash"]}:
                    raise ValueError("Original schedules changed outside this handoff; review the private checkpoint before proceeding")
                if _hash(app_data) not in {handoff["app_before_hash"], handoff["app_after_hash"]}:
                    raise ValueError("App schedules changed outside this handoff; review the private checkpoint before proceeding")
                if handoff["status"] == "complete":
                    if (_hash(source_data) != handoff["source_after_hash"]
                            or _hash(app_data) != handoff["app_after_hash"]):
                        raise ValueError("A completed handoff was changed; review schedules before any further activation")
                    return copy.deepcopy(handoff)
            else:
                _, original = _read(migration / "original-jobs.json")
                if _hash(source_data) != _hash(original):
                    raise ValueError("Original schedules changed since migration; review and refresh the checkpoint explicitly before transfer")
                stamp = datetime.now(timezone.utc).isoformat()
                replacements = {job.id: job for job in load_jobs(app_path)}
                paused, activated = copy.deepcopy(source_data), copy.deepcopy(app_data)
                paused_rows = {row["id"]: row for row in _rows(paused)}
                active_rows = {row["id"]: row for row in _rows(activated)}
                for app_id in selected:
                    source_id = _source_job_id(app_id)
                    if source_id not in source_by_id or app_id not in app_by_id:
                        raise ValueError("A selected source or replacement job is missing")
                    old, new = source_by_id[source_id], app_by_id[app_id]
                    if old.get("fire_claim") or old.get("run_claim") or old.get("state") in {"running", "firing"}:
                        raise ValueError("An original scheduled run is still active; complete it before handoff")
                    cron = old.get("cron") or (old.get("schedule") or {}).get("expr")
                    if (new.get("cron") != cron or known[app_id].get("cron") != cron
                            or new.get("timezone") != known[app_id].get("timezone")):
                        raise ValueError("Source, replacement, and verified migration schedules must match")
                    if new.get("enabled"):
                        raise ValueError("Replacement job is already enabled before the original was transferred")
                    new_job = replacements[app_id]
                    new_job.enabled = True
                    active_rows[app_id].clear()
                    active_rows[app_id].update(new_job.model_dump())
                    paused_rows[source_id].update(enabled=False, state="paused", paused_at=stamp,
                                                  paused_reason="Transferred to the app scheduler after isolated schedule verification")
                    paused_rows[source_id].pop("pending_slot", None)
                if isinstance(paused, dict):
                    paused["updated_at"] = stamp
                checkpoint = migration / ("schedule-handoff-" + uuid.uuid4().hex)
                checkpoint.mkdir(mode=0o700)
                source_backup, app_backup = checkpoint / "source-jobs.json", checkpoint / "room-jobs.json"
                _backup(source_backup, source_raw)
                _backup(app_backup, app_raw)
                # Expected after-images make resumption deterministic without storing prompts in the manifest.
                _write(checkpoint / "source-paused.json", paused)
                _write(checkpoint / "room-enabled.json", activated)
                handoff = {"status": "prepared", "created_at": stamp, "job_ids": selected,
                           "source_path": str(source_path), "app_path": str(app_path),
                           "checkpoint": str(checkpoint), "source_backup": str(source_backup),
                           "app_backup": str(app_backup), "source_before_hash": _hash(source_data),
                           "source_after_hash": _hash(paused), "app_before_hash": _hash(app_data),
                           "app_after_hash": _hash(activated), "verification_evidence": verification_evidence}
                manifest["schedule_handoff"] = handoff
                _write(manifest_path, manifest)
            checkpoint = Path(handoff["checkpoint"])
            _, paused = _read(checkpoint / "source-paused.json")
            _, activated = _read(checkpoint / "room-enabled.json")
            if _hash(paused) != handoff["source_after_hash"] or _hash(activated) != handoff["app_after_hash"]:
                raise ValueError("Schedule handoff after-images no longer match the private checkpoint")
            if _hash(source_data) == handoff["source_before_hash"]:
                if _hash(app_data) != handoff["app_before_hash"]:
                    raise ValueError("Replacement was activated while the source remained active; review schedules")
                _write(source_path, paused)
            handoff["status"] = "source_disabled"
            _write(manifest_path, manifest)
            if _hash(app_data) == handoff["app_before_hash"]:
                _write(app_path, activated)
            handoff["status"] = "complete"
            handoff["completed_at"] = datetime.now(timezone.utc).isoformat()
            for record in records:
                if record["id"] in selected:
                    record["enabled"] = True
            _write(manifest_path, manifest)
            return copy.deepcopy(handoff)
