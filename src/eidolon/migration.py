"""Explicit one-time migration. Normal application code never reads Hermes."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig, ProviderConfig, atomic_write, save_config, store_secret
from .scheduler import Job, load_jobs, save_jobs


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.removeprefix("export ").split("=", 1)
            data[key.strip()] = value.strip().strip("\"'")
    return data


def _redact(text: str, credentials: dict[str, str], avatar_secret_env: str | None = None) -> str:
    for key, value in credentials.items():
        if value and (key == avatar_secret_env or len(value) > 6 and any(
            word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        )):
            # Schedule backups may JSON-escape source credential values.
            escaped = json.dumps(value)[1:-1]
            if escaped != value:
                text = text.replace(escaped, "[credential removed during migration]")
            text = text.replace(value, "[credential removed during migration]")
    return text


def migrate_hermes(
    config: AppConfig, source: Path | str, *, import_credentials: bool = True,
    avatar_secret_env: str | None = None,
    credential_store: Callable[[str, str], str] = store_secret,
) -> dict:
    """Copy settings and reviewable continuity once; never enables old cron jobs.

    Imported provider capabilities start unverified. Secrets move directly to
    the app's credential store, never into JSON, prompts, reports, or stdout.
    Avatar credentials are read only from an explicitly named source field.
    """
    if source is None or (isinstance(source, str) and not source.strip()):
        raise ValueError("An explicit migration source directory is required")
    if avatar_secret_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", avatar_secret_env):
        raise ValueError("Avatar credential source must name an environment field")
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError("The requested migration source profile does not exist")
    destination = config.state_dir / "migration"
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        raise ValueError("This room already has a migration record. Inspect app-owned migration files before importing again")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    import yaml
    raw_settings = yaml.safe_load((source / "config.yaml").read_text()) if (source / "config.yaml").is_file() else {}
    raw_settings = raw_settings or {}
    credentials = read_env(source / ".env")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "source": str(source), "copied_files": [],
              "credential_names": [], "jobs": [], "warnings": []}
    for name in ("MEMORY", "USER"):
        original = source / "memories" / f"{name}.md"
        if original.is_file():
            target = destination / f"{name}.md"
            atomic_write(target, _redact(original.read_text(), credentials, avatar_secret_env))
            report["copied_files"].append(str(target))
    # Keep USER as an editable copy, with provenance; do not ingest supplements.
    profile = destination / "USER.md"
    if profile.is_file():
        target = Path(config.prompts.get("USER", config.state_dir / "prompts/USER.md"))
        existing = target.read_text() if target.is_file() else ""
        seed_profile = files(__package__).joinpath("defaults", "USER.md").read_text(encoding="utf-8")
        if not existing.strip() or existing == seed_profile:
            atomic_write(target, f"# {config.identity.user_name} — continuity profile\n\n"
                         + profile.read_text())
            report["copied_files"].append(str(target))
        else:
            report["warnings"].append("Existing USER.md was preserved. Review migration/USER.md and MEMORY.md before merging additional continuity")
    model = raw_settings.get("model", {})
    if isinstance(model, str):
        model = {"default": model}
    endpoint = model.get("base_url") if isinstance(model, dict) else None
    if endpoint and model.get("default"):
        provider = ProviderConfig(name="migrated", endpoint=endpoint, model=model["default"], tools=True,
                                  presentation="prefix", post_history="system", structured_verified=False)
        # Match the endpoint's custom credential, not another provider's key.
        from urllib.parse import urlsplit
        hostname = urlsplit(endpoint).hostname or ""
        custom_key = "HERMES_CUSTOM_" + re.sub(r"[^A-Z0-9]", "_", hostname.upper()) + "_API_KEY"
        value = credentials.get(custom_key) or credentials.get("OPENAI_API_KEY")
        if import_credentials and value:
            try:
                provider.api_key_ref = credential_store("provider-migrated", value)
                report["credential_names"].append("provider-migrated")
            except Exception:
                report["warnings"].append(
                    "Provider credential could not be copied to the app credential store; "
                    "configure it separately"
                )
        config.providers = [p for p in config.providers if p.name != provider.name] + [provider]
        config.provider = provider.name
    if import_credentials:
        credential_fields = [("TAVILY_API_KEY", "tavily")]
        if avatar_secret_env is not None:
            credential_fields.append((avatar_secret_env, "avatar-lifecycle"))
        for env_key, name in credential_fields:
            if credentials.get(env_key):
                try:
                    reference = credential_store(name, credentials[env_key])
                    if name == "tavily":
                        config.tavily.api_key_ref = reference
                    else:
                        config.avatar.signing_key_ref = reference
                    report["credential_names"].append(name)
                except Exception:
                    report["warnings"].append(f"Credential {name} could not be copied; configure it separately")
    jobs_file = source / "cron/jobs.json"
    migrated_jobs = []
    if jobs_file.is_file():
        original = json.loads(jobs_file.read_text())
        atomic_write(destination / "original-jobs.json",
                     _redact(json.dumps(original, indent=2), credentials, avatar_secret_env))
        for row in original.get("jobs", []):
            schedule = row.get("schedule") or {}
            cron = row.get("cron") or schedule.get("expr")
            if schedule.get("kind", "cron") != "cron" or not cron:
                report["warnings"].append(f"Job {row.get('name', row.get('id'))} has an unsupported schedule")
                continue
            explicit_tz = row.get("timezone") or row.get("tz") or raw_settings.get("timezone")
            timezone_name = explicit_tz or config.timezone
            verified = False
            evidence = "No explicit source timezone or dated occurrence was available"
            if explicit_tz:
                ZoneInfo(timezone_name)
                verified = True
                evidence = "Explicit timezone in migration source"
            elif row.get("next_run_at"):
                occurrence = datetime.fromisoformat(row["next_run_at"])
                candidate = occurrence.astimezone(ZoneInfo(config.timezone))
                from croniter import croniter
                verified = occurrence.tzinfo is not None and occurrence.utcoffset() == candidate.utcoffset() and croniter.match(cron, candidate)
                if verified:
                    evidence = f"Source next occurrence {occurrence.isoformat()} matches cron in {config.timezone}; source did not store an IANA timezone"
            prompt = _redact(row.get("prompt", ""), credentials, avatar_secret_env)
            job = Job(id="migrated-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(row.get("id", "job"))),
                      name=row.get("name", "Migrated task"), prompt=prompt, cron=cron, timezone=timezone_name,
                      provider=config.provider, permitted_tools=["list_files", "read_file", "search_files", "memory_recall"],
                      enabled=False)
            migrated_jobs.append(job)
            report["jobs"].append({"id": job.id, "cron": cron, "timezone": timezone_name, "timezone_verified": verified,
                                   "timezone_evidence": evidence, "enabled": False})
        report["warnings"].append("Migrated jobs remain disabled until the replacement is verified and the original schedule is disabled, preventing duplicate scheduled runs")
    config.jobs_file = config.jobs_file or config.state_dir / "jobs.json"
    existing_jobs = load_jobs(config.jobs_file)
    identifiers = {j.id for j in existing_jobs}
    save_jobs(config.jobs_file, existing_jobs + [j for j in migrated_jobs if j.id not in identifiers])
    # The bank name is deliberately preserved; migration never creates/reconfigures it.
    config.memory.retain_enabled = False
    save_config(config)
    atomic_write(manifest_path, json.dumps(report, indent=2) + "\n")
    return report


import_hermes = migrate_hermes
