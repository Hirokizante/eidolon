from __future__ import annotations

import asyncio
import fcntl
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from eidolon.config import system_timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    prompt: str
    cron: str = "0 22 * * *"
    timezone: str = Field(default_factory=system_timezone)
    provider: str | None = None
    permitted_tools: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    enabled: bool = False
    agent_mode: bool = False
    grace_seconds: int = Field(default=21600, ge=0, le=604800)

    @model_validator(mode="after")
    def required_tools_are_permitted(self) -> "Job":
        if (len(set(self.required_tools)) != len(self.required_tools)
                or any(not name.strip() for name in self.required_tools)):
            raise ValueError("Required tool names must be nonempty and unique")
        if set(self.required_tools) - set(self.permitted_tools):
            raise ValueError("Required tools must also be listed in permitted_tools")
        return self

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("Job ID must contain letters, digits, dashes or underscores")
        return value

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("Use a valid five-field cron schedule")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA time zone, for example Europe/Paris") from None
        return value


def load_jobs(path: Path | str | None) -> list[Job]:
    if path is None or not Path(path).is_file():
        return []
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("jobs")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("Jobs file must contain a list of job objects")
    # Older files included output folders; results now live only in run history.
    jobs = [Job.model_validate({key: value for key, value in row.items()
                               if key not in {"compartment", "compartments"}}) for row in data]
    if len({j.id for j in jobs}) != len(jobs):
        raise ValueError("Job identifiers must be unique")
    return jobs


def save_jobs(path: Path | str, jobs: list[Job], *, expected_jobs: list[Job] | None = None) -> None:
    from eidolon.config import atomic_write
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".jobs.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Scheduled jobs are being changed; retry after the current update") from None
        if expected_jobs is not None and load_jobs(path) != expected_jobs:
            raise ValueError("Jobs changed since they were loaded; reload and try again")
        atomic_write(path, json.dumps([j.model_dump() for j in jobs], indent=2) + "\n")


class Scheduler:
    """Persisted cron scheduler. Instantiate after acquiring the service lock."""

    def __init__(self, store, jobs: list[Job], run_callback: Callable[[Job, dict], Awaitable[str]]):
        self.store, self.jobs, self.run_callback = store, jobs, run_callback
        self._tick_lock = asyncio.Lock()
        for job in jobs:
            store.save_job(job.id, job.model_dump())
        store.recover_job_runs()

    def plan(self, job: Job, now: datetime) -> list[dict]:
        if now.tzinfo is None:
            raise ValueError("Scheduler time must include a timezone")
        local = now.astimezone(ZoneInfo(job.timezone))
        cursor = self.store.job_cursor(job.id)
        occurrences = []
        if cursor:
            iterator = croniter(job.cron, datetime.fromisoformat(cursor).astimezone(ZoneInfo(job.timezone)))
            for _ in range(10000):
                occurrence = iterator.get_next(datetime)
                if occurrence > local:
                    break
                occurrences.append(occurrence)
            else:
                raise ValueError(f"Job {job.id} has over 10000 missed occurrences; review its schedule before continuing")
        else:
            # A newly installed job considers only its most recent occurrence.
            occurrences = [croniter(job.cron, local + timedelta(microseconds=1)).get_prev(datetime)]
        runs = []
        for index, occurrence in enumerate(occurrences):
            scheduled_at = occurrence.astimezone(timezone.utc).isoformat()
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"eidolon-room:job:{job.id}:{scheduled_at}"))
            age = now.astimezone(timezone.utc) - occurrence.astimezone(timezone.utc)
            eligible = index == len(occurrences) - 1 and age.total_seconds() <= job.grace_seconds
            runs.append({"id": run_id, "scheduled_at": scheduled_at,
                         "status": "queued" if eligible else "skipped"})
        # Cursor advances even when all occurrences are older than the grace period.
        self.store.record_job_plan(job.id, runs, now.astimezone(timezone.utc).isoformat())
        return runs

    async def tick(self, now: datetime | None = None) -> None:
        if self._tick_lock.locked():
            return
        now = now or datetime.now(timezone.utc)
        async with self._tick_lock:
            enabled = [j for j in self.jobs if j.enabled]
            for job in enabled:
                self.plan(job, now)
            await asyncio.gather(*(self._run_queued(job) for job in enabled))

    async def _run_queued(self, job: Job) -> None:
        for run in self.store.queued_job_runs(job.id):
            if not self.store.claim_job_run(run["id"]):
                continue
            try:
                session = self.store.create_session(f"job:{run['id']}", kind="job", title=job.name)
                run["session_id"] = session["id"]
                self.store.update_job_run(run["id"], session_id=session["id"])
                result = run["result"]
                if result is None:
                    # Recover the completed generation if the process died before
                    # persisting the run result. Do not issue a second model call.
                    existing = self.store.list_messages(session["id"])
                    result = next((m["content"] for m in reversed(existing) if m["role"] == "assistant"), None)
                if result is None:
                    result = await self.run_callback(job, run)
                if not isinstance(result, str) or not result.strip():
                    raise ValueError("Scheduled generation did not produce a result")
                self.store.update_job_run(run["id"], result=result, status="completed")
            except asyncio.CancelledError:
                # Startup recovery will use this same run and saved exchange.
                raise
            except Exception as error:
                self.store.update_job_run(run["id"], status="failed", error=f"{type(error).__name__}: {error}")
