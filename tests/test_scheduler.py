import asyncio
import json
from datetime import datetime

import pytest

from eidolon.scheduler import Job, Scheduler, load_jobs, save_jobs
from eidolon.storage import Store


def test_agent_mode_defaults_off_and_round_trips(tmp_path):
    path = tmp_path / "jobs.json"
    job = Job(id="daily", name="Daily", prompt="Write a note")
    assert not job.agent_mode
    job.agent_mode = True
    save_jobs(path, [job])
    assert load_jobs(path)[0].agent_mode


def test_job_updates_reject_stale_file_snapshot(tmp_path):
    path = tmp_path / "jobs.json"
    job = Job(id="daily", name="Daily", prompt="Write a note")
    save_jobs(path, [job])
    snapshot = load_jobs(path)
    changed = job.model_copy(update={"name": "Changed externally"})
    save_jobs(path, [changed])
    with pytest.raises(ValueError, match="changed"):
        save_jobs(path, [job.model_copy(update={"enabled": True})], expected_jobs=snapshot)
    assert load_jobs(path) == [changed]


def test_jobs_with_required_tools_load_and_preserve_the_requirement(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps([{
        "id": "daily", "name": "Daily note", "prompt": "Write a note",
        "permitted_tools": ["read_skill", "read_file"], "required_tools": ["read_file"],
    }]))
    job = load_jobs(path)[0]
    assert job.required_tools == ["read_file"] and not job.enabled
    save_jobs(path, [job])
    assert load_jobs(path)[0].required_tools == ["read_file"]
    assert Job(id="ordinary", name="Ordinary", prompt="Note").required_tools == []


@pytest.mark.parametrize("required", [["unpermitted"], ["read_file", "read_file"], [""]])
def test_required_tools_cannot_grant_permissions_or_contain_invalid_names(required):
    with pytest.raises(ValueError):
        Job(id="daily", name="Daily", prompt="Note", permitted_tools=["read_file"],
            required_tools=required)


def test_legacy_output_folders_are_discarded_on_load_and_save(tmp_path):
    path = tmp_path / "jobs.json"
    job = Job(id="daily", name="Daily", prompt="Reflect on the day", enabled=True)
    path.write_text(json.dumps([{
        **job.model_dump(), "compartment": "Daily", "compartments": ["Letters", "Stories"],
    }]))
    assert load_jobs(path) == [job]
    save_jobs(path, load_jobs(path))
    assert json.loads(path.read_text()) == [job.model_dump()]
    path.write_text('[null]')
    with pytest.raises(ValueError, match="job objects"):
        load_jobs(path)


@pytest.mark.asyncio
async def test_wake_runs_latest_only_and_restart_does_not_duplicate(tmp_path):
    store = Store(tmp_path / "room.db")
    job = Job(id="daily", name="Daily", prompt="Write", enabled=True)
    calls = []
    async def callback(job, run):
        calls.append(run["id"])
        return "A note"
    scheduler = Scheduler(store, [job], callback)
    scheduler.plan(job, datetime.fromisoformat("2026-10-07T21:00:00+08:00"))
    await scheduler.tick(datetime.fromisoformat("2026-10-10T23:00:00+08:00"))
    runs = store.list_job_runs()
    assert sum(r["status"] == "completed" for r in runs) == 1
    assert sum(r["status"] == "skipped" for r in runs) == 4
    assert len(calls) == 1
    store.close()
    store = Store(tmp_path / "room.db")
    await Scheduler(store, [job], callback).tick(datetime.fromisoformat("2026-10-10T23:30:00+08:00"))
    assert len(calls) == 1
    assert store.list_job_runs()[0]["result"] == "A note"
    assert not list(tmp_path.rglob("*.md"))
    store.close()


@pytest.mark.asyncio
async def test_saved_result_recovers_without_generation(tmp_path):
    store = Store(tmp_path / "room.db")
    job = Job(id="daily", name="Daily", prompt="Write", enabled=True)
    async def never(job, run):
        raise AssertionError("recovery must use persisted result")
    scheduler = Scheduler(store, [job], never)
    runs = scheduler.plan(job, datetime.fromisoformat("2026-10-10T22:00:00+08:00"))
    run = runs[0]
    assert store.claim_job_run(run["id"])
    store.update_job_run(run["id"], result="Saved note")
    scheduler = Scheduler(store, [job], never)
    await scheduler.tick(datetime.fromisoformat("2026-10-10T22:10:00+08:00"))
    assert store.list_job_runs()[0]["status"] == "completed"
    assert store.list_job_runs()[0]["result"] == "Saved note"
    assert not list(tmp_path.rglob("*.md"))
    store.close()


async def test_completed_exchange_recovers_after_interrupted_callback(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    job = Job(id="daily", name="Daily", prompt="Reflect", enabled=True)
    scheduled = datetime.fromisoformat("2026-10-10T22:00:00+08:00")

    async def interrupted(job, run):
        store.complete_turn(run["session_id"], job.prompt, "Complete result", retain=False)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await Scheduler(store, [job], interrupted).tick(scheduled)
    assert store.list_job_runs()[0]["status"] == "running"
    assert store.list_job_runs()[0]["result"] is None
    store.close()

    async def never(job, run):
        raise AssertionError("The completed exchange must be recovered without another request")

    reopened = Store(path)
    try:
        await Scheduler(reopened, [job], never).tick(scheduled)
        run = reopened.list_job_runs()[0]
        assert run["status"] == "completed" and run["result"] == "Complete result"
        assert len(reopened.list_messages(run["session_id"])) == 2
        assert not list(tmp_path.rglob("*.md"))
    finally:
        reopened.close()


async def test_job_session_keeps_actual_start_across_retry_and_restart(tmp_path, monkeypatch):
    clock = "2026-10-10T20:30:00+00:00"
    monkeypatch.setattr("eidolon.storage.store.utc_now", lambda: clock)
    path = tmp_path / "room.db"
    store = Store(path)
    job = Job(id="daily", name="Daily", prompt="Write", enabled=True)

    async def fail(job, run):
        raise RuntimeError("Provider offline")

    scheduler = Scheduler(store, [job], fail)
    scheduled = datetime.fromisoformat("2026-10-10T22:00:00+08:00")
    await scheduler.tick(scheduled)
    run = store.list_job_runs()[0]
    session = store.get_session(run["session_id"])
    assert session["created_at"] == clock
    assert session["created_at"] != run["scheduled_at"]
    store.close()

    clock = "2026-10-11T20:30:00+00:00"
    store = Store(path)
    store.retry_job_run(run["id"])

    async def complete(job, run):
        return "Recovered note"

    await Scheduler(store, [job], complete).tick(scheduled)
    assert store.get_session(run["session_id"])["created_at"] == session["created_at"]
    assert store.list_job_runs()[0]["status"] == "completed"
    store.close()


@pytest.mark.asyncio
async def test_nonoverlap_and_grace_policy(tmp_path):
    store = Store(tmp_path / "room.db")
    job = Job(id="daily", name="Daily", prompt="Write", enabled=True, grace_seconds=60)
    gate = asyncio.Event()
    calls = []
    async def callback(job, run):
        calls.append(run["id"])
        await gate.wait()
        return "Note"
    scheduler = Scheduler(store, [job], callback)
    task = asyncio.create_task(scheduler.tick(datetime.fromisoformat("2026-10-10T22:00:00+08:00")))
    await asyncio.sleep(0)
    await scheduler.tick(datetime.fromisoformat("2026-10-10T22:00:01+08:00"))
    gate.set()
    await task
    assert len(calls) == 1
    await scheduler.tick(datetime.fromisoformat("2026-10-11T22:05:00+08:00"))
    assert store.list_job_runs()[0]["status"] == "skipped"
    store.close()
