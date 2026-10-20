"""Reproduce installed 0.10.2/1.0.0 migration and scope checks without a database.

Supply both installed runtime Python interpreters explicitly. Each subprocess gets
a clean environment and temporary working directory, preventing private credentials
or discovered .env files from entering these checks. Alembic runs in SQL-only mode;
the scope probe never initializes an engine or accesses its database backend.

The JSON result verifies installed source behavior, not API startup, persisted
database migrations, or configured HTTP credential authorization. Those remain
separate isolated integration checks.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


CHILD_PROBE = r'''
import asyncio
import io
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.dont_write_bytecode = True
OLD_REVISION = "e5b1c7d3a902"
NEW_REVISION = "c8e2f4a6b1d3"
DUMMY_URL = "postgresql://synthetic:synthetic@127.0.0.1:1/synthetic"
role = sys.argv[1]
stage = "installed_version"


class ProbeFailure(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise ProbeFailure(message)


def offline_sql(cfg, command, revision_range, expected_statement):
    output = io.StringIO()
    cfg.output_buffer = output
    command(cfg, revision_range, sql=True)
    statements = [line.strip() for line in output.getvalue().splitlines()
                  if line.strip() and not line.lstrip().startswith("--")]
    require(statements == ["BEGIN;", expected_statement, "COMMIT;"],
            "Offline migration emitted statements other than its revision-ledger update")
    return {"only_revision_ledger_sql": True, "statements": statements}


async def scoped_deletion_checks():
    from hindsight_api.engine.memory_engine import MemoryEngine
    from hindsight_api.engine.search.tags import TagGroupLeaf
    from hindsight_api.extensions import OperationValidationError
    from hindsight_api.models import RequestContext

    engine = object.__new__(MemoryEngine)
    engine._authenticate_tenant = AsyncMock()
    engine._get_backend = AsyncMock(side_effect=AssertionError("Database backend accessed"))
    cases = (
        ("read_scoped", [TagGroupLeaf(tags=["phase0:test"], match="all_strict")], None),
        ("write_scoped", None, ["phase0:test"]),
        ("writes_denied", None, []),
    )
    results = []
    for name, read_scope, write_scope in cases:
        engine._operation_validator = SimpleNamespace(
            resolve_tag_scope=AsyncMock(return_value=read_scope),
            resolve_write_tag_scope=AsyncMock(return_value=write_scope),
        )
        try:
            await engine.delete_bank("eidolon-phase0-synthetic-source-contract",
                                     request_context=RequestContext())
        except OperationValidationError as error:
            require(error.status_code == 403, "Scoped deletion did not return 403")
        else:
            raise ProbeFailure("Scoped whole-bank deletion was not refused")
        engine._get_backend.assert_not_called()
        results.append({"scope": name, "http_status_equivalent": 403,
                        "backend_accessed": False})
    engine._operation_validator = None
    await engine._refuse_bank_wide_if_scoped("eidolon-phase0-synthetic-source-contract",
                                            RequestContext(), "delete the bank")
    return {"status": "passed", "scoped_deletions": results,
            "no_validator": "unscoped by design",
            "scope": "Installed engine guard with stub tenant authentication; no HTTP request"}


try:
    import hindsight_api
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.script.revision import ResolutionError
    from alembic.util.exc import CommandError

    expected_version = "0.10.2" if role == "old" else "1.0.0"
    require(hindsight_api.__version__ == expected_version,
            "Supplied runtime does not contain the expected Hindsight release")
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(hindsight_api.__file__).parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DUMMY_URL)
    cfg.set_main_option("path_separator", "os")
    scripts = ScriptDirectory.from_config(cfg)
    stage = "migration_head"
    expected_head = OLD_REVISION if role == "old" else NEW_REVISION
    require(scripts.get_heads() == [expected_head], "Installed migration head is unexpected")
    result = {"status": "passed", "role": role, "api_version": expected_version,
              "migration_head": expected_head, "checks": {}}
    if role == "candidate":
        stage = "offline_migrations"
        require(scripts.get_revision(NEW_REVISION).down_revision == OLD_REVISION,
                "Candidate revision does not directly follow the baseline revision")
        result["checks"]["offline_upgrade"] = offline_sql(
            cfg, command.upgrade, OLD_REVISION + ":" + NEW_REVISION,
            "UPDATE alembic_version SET version_num='" + NEW_REVISION +
            "' WHERE alembic_version.version_num = '" + OLD_REVISION + "';")
        result["checks"]["offline_downgrade"] = offline_sql(
            cfg, command.downgrade, NEW_REVISION + ":" + OLD_REVISION,
            "UPDATE alembic_version SET version_num='" + OLD_REVISION +
            "' WHERE alembic_version.version_num = '" + NEW_REVISION + "';")
        stage = "tag_scoped_delete_bank"
        result["checks"]["tag_scoped_delete_bank"] = asyncio.run(scoped_deletion_checks())
    else:
        stage = "unknown_newer_revision"
        try:
            scripts._upgrade_revs("heads", NEW_REVISION)
        except CommandError as error:
            require(isinstance(error.__cause__, ResolutionError),
                    "Unknown revision raised an unexpected Alembic failure")
        else:
            raise ProbeFailure("Old runtime unexpectedly resolves the candidate revision")
        from hindsight_api import migrations

        warnings = []
        class CaptureWarning(logging.Handler):
            def emit(self, record):
                warnings.append(record.getMessage())

        def unknown_ledger_upgrade(config, target):
            require(target == "heads", "Migration wrapper requested an unexpected target")
            ScriptDirectory.from_config(config)._upgrade_revs(target, NEW_REVISION)

        handler = CaptureWarning(level=logging.WARNING)
        migrations.logger.addHandler(handler)
        try:
            with patch("hindsight_api.extensions.loader.collect_alembic_version_locations",
                       return_value=[]), patch.object(migrations.command, "upgrade", unknown_ledger_upgrade):
                migrations._run_migrations_internal(DUMMY_URL, cfg.get_main_option("script_location"))
        finally:
            migrations.logger.removeHandler(handler)
        require(any("newer migration revision" in message and "Skipping migrations" in message
                    for message in warnings), "Old migration wrapper did not report its newer-revision skip")
        result["checks"]["unknown_newer_revision"] = {
            "status": "passed", "resolution_error_caught": True,
            "migration_wrapper_returned_normally": True,
            "scope": "Actual old wrapper; Alembic callback resolves a synthetic newer ledger without DB I/O"}
    print(json.dumps(result, sort_keys=True))
except Exception as error:
    result = {"status": "failed", "role": role, "check": stage,
              "error_type": type(error).__name__}
    if isinstance(error, ProbeFailure):
        result["reason"] = str(error)
    print(json.dumps(result, sort_keys=True))
    sys.exit(1)
'''


def run_runtime(python: Path, role: str, timeout: float) -> dict:
    """Run a source-only probe without inheriting credentials or private paths."""
    environment = {
        "PATH": "/usr/bin:/bin",
        # Avoid Alembic env.py's optional dotenv loading. This URL is never used
        # online; both migration modes explicitly request SQL-only output.
        "HINDSIGHT_API_DATABASE_URL": "postgresql://synthetic:synthetic@127.0.0.1:1/synthetic",
    }
    try:
        interpreter = python.expanduser().absolute()
        if not interpreter.is_file():
            return {"status": "failed", "role": role, "reason": "Runtime interpreter is missing"}
        with tempfile.TemporaryDirectory(prefix="eidolon-memory-runtime-contract-") as directory:
            completed = subprocess.run(
                [str(interpreter), "-I", "-c", CHILD_PROBE, role], env=environment,
                cwd=directory, capture_output=True, text=True, timeout=timeout, check=False,
            )
        result = json.loads(completed.stdout)
        if (not isinstance(result, dict) or result.get("role") != role
                or result.get("status") not in {"passed", "failed"}
                or (completed.returncode != 0 and result.get("status") == "passed")):
            raise ValueError("Invalid source probe result")
        return result
    except subprocess.TimeoutExpired:
        return {"status": "failed", "role": role, "reason": "Runtime source probe timed out"}
    except (OSError, ValueError):
        # Never expose child stderr or malformed stdout: a dependency failure may
        # include connection details or unrelated environment information.
        return {"status": "failed", "role": role, "reason": "Runtime source probe could not complete"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-python", required=True, type=Path,
                        help="Explicit installed Hindsight 0.10.2 runtime interpreter")
    parser.add_argument("--candidate-python", required=True, type=Path,
                        help="Explicit installed Hindsight 1.0.0 runtime interpreter")
    parser.add_argument("--timeout", type=float, default=120,
                        help="Maximum seconds per source-only subprocess")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(run_runtime, args.old_python, "old", args.timeout)
        candidate = executor.submit(run_runtime, args.candidate_python, "candidate", args.timeout)
        results = {"old": old.result(), "candidate": candidate.result()}
    report = {
        "kind": "installed Hindsight source contract; no database connection",
        "status": "passed" if all(r["status"] == "passed" for r in results.values()) else "failed",
        "runtimes": results,
        "http_scoped_authorization": {
            "status": "not_run",
            "reason": "No HTTP scoped validator or credential is configured by this source-only probe",
        },
        "integration_scope": "API restart, database migration and rollback require isolated database checks",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
