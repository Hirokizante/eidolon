"""Independent Hindsight service supervisor, preserving an existing pg0 instance.

The app resolves its own credential references and invokes the separately
installed official Hindsight/pg0 runtime. It never imports another application's
plugins or environment. Stopping this API leaves PostgreSQL running.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from eidolon.config import atomic_write, resolve_secret


LABEL = "local.eidolon.hindsight"
CHECK_INTERVAL = 10
STARTUP_GRACE = 120
FAILURE_THRESHOLD = 3

# pg0 is installed only in the independent Hindsight runtime. Its native manager
# identifies the exact instance before start; the program returns no URI/secrets.
PG_MANAGER_PROGRAM = """
import json, os, pathlib
from pg0 import Pg0, Pg0AlreadyRunningError
s=json.loads(pathlib.Path(os.environ['EIDOLON_MEMORY_SETTINGS']).read_text())
d=s['database']
data=pathlib.Path(d['data_dir']).resolve()
if not (data/'PG_VERSION').is_file() or not data.parent.joinpath('instance.json').is_file():
    raise SystemExit('Preserved Hindsight database files are missing; refusing to create a replacement')
p=Pg0(name=d['instance'],port=d['port'],username=d['username'],
      password=os.environ['EIDOLON_MEMORY_DATABASE_PASSWORD'],database=d['database'],data_dir=str(data))
i=p.info()
if i.data_dir and pathlib.Path(i.data_dir).resolve()!=data:
    raise SystemExit('Hindsight storage identity differs from the preserved instance')
if i.running and i.port!=d['port']:
    raise SystemExit('Hindsight database port differs from the preserved instance')
started=False
if not i.running:
    try:
        p.start()
        started=True
    except Pg0AlreadyRunningError:
        pass
    i=p.info()
if not i.running or pathlib.Path(i.data_dir).resolve()!=data or i.port!=d['port']:
    raise SystemExit('Preserved Hindsight database did not start with its original identity')
print(json.dumps({'running':i.running,'pid':i.pid,'port':i.port,'data_dir':i.data_dir,'started':started}))
"""


def build_environment(settings: dict, resolver=resolve_secret) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTHON", "HERMES", "HINDSIGHT"))}
    environment.update(settings.get("environment", {}))
    for key, reference in settings.get("credential_refs", {}).items():
        value = resolver(reference)
        if value:
            environment[key] = value
    database = settings["database"]
    password = resolver(database["password_ref"])
    if password is None:
        raise ValueError("Independent Hindsight database credential is missing")
    environment["EIDOLON_MEMORY_DATABASE_PASSWORD"] = password
    environment["HINDSIGHT_API_DATABASE_URL"] = (
        f"postgresql://{quote(database['username'], safe='')}:{quote(password, safe='')}"
        f"@127.0.0.1:{database['port']}/{quote(database['database'], safe='')}"
    )
    environment["HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP"] = "false"
    environment["HINDSIGHT_API_WORKER_ENABLED"] = "true"
    environment["HINDSIGHT_API_LOG_LEVEL"] = "warning"
    return environment


def ensure_database(settings: dict, settings_path: Path, environment: dict, runner=subprocess.run) -> dict:
    child_env = {**environment, "EIDOLON_MEMORY_SETTINGS": str(settings_path)}
    result = runner([settings["runtime_python"], "-c", PG_MANAGER_PROGRAM], env=child_env,
                    capture_output=True, text=True, timeout=90)
    if result.returncode:
        # Never echo native manager stderr: it may contain a database URL.
        raise RuntimeError("The independent pg0 manager could not verify/start the preserved database; inspect its storage identity")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("The independent pg0 manager returned an invalid status") from None


def write_launchagent(settings_path: Path, *, python: Path | None = None, launchagents: Path | None = None) -> Path:
    python = python or Path(sys.executable)
    destination = (launchagents or Path.home() / "Library/LaunchAgents") / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logs = settings_path.parent
    data = {"Label": LABEL, "ProgramArguments": [str(python), "-m", "eidolon.memory.runtime", "--settings", str(settings_path)],
            "WorkingDirectory": str(settings_path.parent), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 15, "ExitTimeOut": 20, "StandardOutPath": str(logs / "supervisor.log"),
            "StandardErrorPath": str(logs / "supervisor-error.log")}
    atomic_write(destination, plistlib.dumps(data).decode(), mode=0o600)
    return destination


def api_is_live(settings: dict) -> bool:
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            url = f"http://127.0.0.1:{settings['api_port']}"
            response = client.get(url + "/health/live")
            if response.status_code == 404:
                # Older runtimes only expose readiness. Any response proves the
                # event loop is live; database errors must not cause a restart.
                client.get(url + "/health")
                return True
            return response.is_success
    except httpx.HTTPError:
        return False


def stop_api(api) -> None:
    if api is None or api.poll() is not None:
        return
    try:
        api.terminate()
    except ProcessLookupError:
        return
    try:
        api.wait(timeout=10)
    except subprocess.TimeoutExpired:
        api.kill()
        api.wait(timeout=5)


class ApiSupervisor:
    def __init__(self, settings: dict, settings_path: Path, environment: dict):
        self.settings, self.settings_path, self.environment = settings, settings_path, environment
        self.api = None
        self.started_at = 0.0
        self.failures = 0
        self.ready = False
        self.restart_count = 0
        self._started = False
        self.database_status: dict = {}

    def step(self) -> None:
        error = None
        database_ready = False
        try:
            self.database_status = ensure_database(self.settings, self.settings_path, self.environment)
            database_ready = True
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            # Native errors can contain secrets. A temporary manager failure
            # should not tear down an API which is still serving requests.
            error = "Preserved database verification failed; will retry"

        if self.api is not None:
            exit_code = self.api.poll()
            if exit_code is not None:
                print(f"Hindsight API exited (code {exit_code}); restarting", flush=True)
                self.api = None
            elif api_is_live(self.settings):
                self.failures = 0
                self.ready = True
            else:
                self.ready = False
                if time.monotonic() - self.started_at >= STARTUP_GRACE:
                    self.failures += 1
                    error = "Hindsight API is not responding; checking again"
                    if self.failures >= FAILURE_THRESHOLD:
                        print("Hindsight API failed repeated liveness checks; restarting", flush=True)
                        stop_api(self.api)
                        self.api = None

        if self.api is None and database_ready:
            try:
                with (self.settings_path.parent / "api.log").open("a") as log:
                    self.api = subprocess.Popen(
                        [self.settings["api_binary"], "--host", "127.0.0.1", "--port", str(self.settings["api_port"]),
                         "--log-level", "warning"], cwd=self.settings_path.parent,
                        env=self.environment, stdout=log, stderr=log,
                    )
                self.started_at = time.monotonic()
                self.failures = 0
                self.ready = False
                self.restart_count += int(self._started)
                self._started = True
                error = None
            except OSError:
                error = "Hindsight API could not start; will retry"

        status = {**self.database_status, "supervisor_pid": os.getpid(),
                  "api_pid": self.api.pid if self.api is not None else None,
                  "api_state": "unavailable" if self.api is None else (
                      "running" if self.ready else ("unresponsive" if self.failures else "starting")),
                  "restart_count": self.restart_count, "error": error}
        atomic_write(self.settings_path.parent / "status.json", json.dumps(status, indent=2) + "\n")


def supervise(settings_path: Path) -> None:
    settings_path = settings_path.resolve()
    settings = json.loads(settings_path.read_text())
    for name in ["supervisor.log", "supervisor-error.log", "api.log"]:
        path = settings_path.parent / name
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
    owner = (settings_path.parent / "supervisor.lock").open("a")
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("The independent Hindsight supervisor already owns this service") from None
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    supervisor = None
    try:
        supervisor = ApiSupervisor(settings, settings_path, build_environment(settings))
        while not stop.is_set():
            supervisor.step()
            # Exits and failed launches are throttled, including repeated crashes.
            stop.wait(CHECK_INTERVAL)
    finally:
        if supervisor is not None:
            stop_api(supervisor.api)
        # PostgreSQL serves the original bank and other Hindsight clients. The
        # TCP-connected API never owns/stops it; the manager is a standby starter.
        owner.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent existing-bank Hindsight supervisor")
    parser.add_argument("--settings", type=Path, required=True)
    arguments = parser.parse_args()
    supervise(arguments.settings)


if __name__ == "__main__":
    main()
