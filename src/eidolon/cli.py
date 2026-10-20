from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from .avatar.setup import configure_existing_vessel
from .config import (
    AppConfig, IdentityConfig, config_path, delete_secret, initialize_config,
    load_config, save_config, store_secret,
)


async def _notify_secret_change(configuration: Path, reference: str) -> None:
    """Invalidate the running service's cache without sending the credential."""
    from .service.client import RoomClient
    try:
        config = load_config(configuration)
        async with asyncio.timeout(3):
            await RoomClient(config.socket_path).request("clear_secret_cache", reference=reference)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        # Secret commands also work before setup or while the service is stopped.
        # A new service process will read the updated value directly.
        pass


def _migration_source(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("An explicit source directory is required")
    return Path(value)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="eidolon", description="A configurable macOS terminal companion")
    root.add_argument("--config", type=Path, help="App-owned configuration file")
    sub = root.add_subparsers(dest="command")
    setup = sub.add_parser("setup", help="Initialize or update app-owned configuration")
    setup.add_argument("--user-name", help="Your display name (omitting preserves the saved value)")
    setup.add_argument(
        "--companion-name", help="Companion's display name (omitting preserves the saved value)",
    )
    setup.add_argument("--project", type=Path, help="Import local prompts once from this folder; defaults are bundled")
    setup.add_argument("--avatar-bridge", type=_migration_source,
                       help="Explicit standalone bridge directory containing server.py and pyproject.toml")
    setup.add_argument("--avatar-credentials", type=Path,
                       help="Source credential file; only explicitly selected fields are imported")
    setup.add_argument("--avatar-token-env", help="Source environment field required by the bridge")
    setup.add_argument("--avatar-signing-env", help="Source environment field for lifecycle signing")
    setup.add_argument("--avatar-profile", help="External lifecycle profile (omitting preserves private configuration)")
    setup.add_argument(
        "--interactive", action="store_true", help="Configure names, provider, and credentials",
    )
    setup.add_argument("--install-service", action="store_true", help="Install a macOS LaunchAgent")
    sub.add_parser("doctor", help="Check configured integrations without writing to the memory bank")
    service = sub.add_parser("service", help="Manage the background room service")
    service.add_argument("action", choices=["install", "uninstall", "start", "stop", "status", "run"])
    migration = sub.add_parser("migrate", help="One-time copy from the old profile into app-owned files")
    migration.add_argument("--source", type=_migration_source, required=True,
                           help="Explicit source profile directory")
    migration.add_argument(
        "--avatar-secret-env", help="Optional source environment field for the avatar signing key",
    )
    migration.add_argument("--without-credentials", action="store_true")
    rebrand = sub.add_parser("migrate-state", help="Copy an existing installation using a private JSON manifest")
    rebrand.add_argument("--manifest", type=Path, required=True,
                         help="Private manifest with explicit source paths and service identifiers")
    rebrand.add_argument("--activate-services", action="store_true",
                         help="Activate replacement services after verifying the copied installation")
    handoff = sub.add_parser("migrate-jobs", help="Hand over a verified scheduled-job replacement")
    handoff.add_argument("--activate", action="store_true", required=True)
    handoff.add_argument("--source", type=Path)
    handoff.add_argument("--verification", type=Path, required=True, help="Completed isolated job verification report")
    secret = sub.add_parser("secret", help="Store credentials in macOS Keychain")
    secret.add_argument("action", choices=["set", "delete"])
    secret.add_argument("name")
    return root


def interactive_setup(config: AppConfig, *, prompt_avatar: bool = True) -> None:
    user_name = input(f"Your name [{config.identity.user_name}] (Enter keeps current): ")
    companion_name = input(
        f"Companion's name [{config.identity.companion_name}] (Enter keeps current): "
    )
    config.identity = IdentityConfig(
        user_name=user_name or config.identity.user_name,
        companion_name=companion_name or config.identity.companion_name,
    )
    profile = config.selected_provider()
    profile.endpoint = input(f"Provider endpoint [{profile.endpoint}]: ").strip() or profile.endpoint
    profile.model = input(f"Model [{profile.model}]: ").strip() or profile.model
    credential = getpass.getpass("Provider API key (blank preserves existing/local keyless): ").strip()
    if credential:
        profile.api_key_ref = store_secret(f"provider-{profile.name}", credential)
    search_key = getpass.getpass("Tavily API key (optional; choose other search providers in /preferences): ").strip()
    if search_key:
        config.web_search.tavily.api_key_ref = store_secret("tavily", search_key)
    config.timezone = input(
        f"IANA timezone [{config.timezone}] (UTC fallback; Enter keeps current): "
    ).strip() or config.timezone
    config.memory.url = input(f"Existing Hindsight URL [{config.memory.url}]: ").strip() or config.memory.url
    config.memory.bank = input(f"Existing bank [{config.memory.bank}] (blank leaves memory unconfigured): ").strip() or config.memory.bank
    roots = input("Permitted read-only folders (comma separated, blank preserves): ").strip()
    if roots:
        config.allowed_roots = [str(Path(value.strip()).expanduser().resolve()) for value in roots.split(",")]
    if prompt_avatar:
        bridge = input("Standalone Vessel bridge directory (blank preserves avatar settings): ").strip()
        if bridge:
            source = input("Source credential file (blank imports no credentials): ").strip()
            token_field = signing_field = None
            if source:
                token_field = input("Bridge token environment field (blank skips): ").strip() or None
                signing_field = input("Lifecycle signing environment field (blank skips): ").strip() or None
            profile = input(f"External lifecycle profile [{config.avatar.profile}] (Enter keeps current): ")
            configure_existing_vessel(
                config, bridge, credential_file=Path(source) if source else None,
                body_token_env=token_field, signing_key_env=signing_field,
                profile=profile or None, credential_store=store_secret,
            )


async def diagnostic(config: AppConfig, configuration: Path) -> dict:
    from .service.client import RoomClient
    try:
        return await RoomClient(config.socket_path).request("doctor")
    except ConnectionError:
        from .memory import HindsightMemory
        from .memory.recovery import ManagedMemoryService
        from .config import resolve_secret
        import httpx
        recovery = ManagedMemoryService(config.memory.url, config.state_dir / "memory-service/settings.json")
        memory = HindsightMemory(config.memory, resolve_secret, recovery=recovery.recover)
        try:
            if config.memory.enabled:
                try:
                    await memory.validate_bank()
                    memory_status = f"existing bank '{config.memory.bank}' available; retention {'enabled' if config.memory.retain_enabled else 'disabled pending verification'}"
                except Exception as exc:
                    memory_status = str(exc)
            else:
                memory_status = "disabled by configuration"
            profile = config.selected_provider()
            try:
                headers = {key: resolve_secret(value) if value.startswith(("env:", "keyring:")) else value
                           for key, value in profile.headers.items()}
                if profile.api_key_ref:
                    headers["Authorization"] = "Bearer " + resolve_secret(profile.api_key_ref)
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.get(profile.endpoint + "/models", headers=headers)
                provider_status = f"model inventory HTTP {response.status_code}; verify streaming/tool/suffix capabilities separately"
            except Exception:
                provider_status = "unreachable or credential unavailable"
            from .providers.routing import AuxiliaryConfigurationError, resolve_auxiliary
            auxiliary = {"mode": config.auxiliary.mode, "state": "service stopped",
                         "provider": config.auxiliary.provider, "model": config.auxiliary.model,
                         "configuration_error": None}
            try:
                execution = resolve_auxiliary(config)
                if execution:
                    auxiliary.update(provider=execution.provider, model=execution.model,
                                     context_window=execution.context_window)
            except AuxiliaryConfigurationError as exc:
                auxiliary["configuration_error"] = str(exc)
            return {"provider": provider_status, "memory": memory_status,
                    "auxiliary": auxiliary,
                    "prompts": {name: "ready" if Path(path).is_file() else "MISSING" for name, path in config.prompts.items()},
                    "avatar": "configured; connect through eidolon service start" if config.avatar.enabled else "disabled",
                    "service": "stopped", "standalone": True}
        finally:
            await memory.close()


def main(argv=None):
    args = parser().parse_args(argv)
    configuration = (args.config or config_path()).expanduser().resolve()
    if configuration.name == "config.json":
        # Existing LaunchAgents can keep their old argument while using YAML.
        configuration = configuration.with_suffix(".yaml")
    try:
        if args.command == "secret":
            if args.action == "set":
                value = getpass.getpass(f"Credential for {args.name}: ")
                if not value:
                    raise ValueError("Credential cannot be empty")
                reference = store_secret(args.name, value)
                asyncio.run(_notify_secret_change(configuration, reference))
                print("Stored " + reference)
            else:
                reference = delete_secret(args.name)
                asyncio.run(_notify_secret_change(configuration, reference))
                print("Credential deleted")
            return
        if args.command == "setup":
            if args.avatar_bridge is None and any(value is not None for value in (
                args.avatar_credentials, args.avatar_token_env, args.avatar_signing_env,
                args.avatar_profile,
            )):
                raise ValueError("Avatar setup options require an explicit bridge directory")
            config = initialize_config(args.project, state_dir=configuration.parent, path=configuration)
            config.identity = IdentityConfig(
                user_name=args.user_name if args.user_name is not None else config.identity.user_name,
                companion_name=(args.companion_name if args.companion_name is not None
                                else config.identity.companion_name),
            )
            if args.avatar_bridge is not None:
                configure_existing_vessel(
                    config, args.avatar_bridge, credential_file=args.avatar_credentials,
                    body_token_env=args.avatar_token_env, signing_key_env=args.avatar_signing_env,
                    profile=args.avatar_profile, credential_store=store_secret,
                )
            if args.interactive:
                interactive_setup(config, prompt_avatar=args.avatar_bridge is None)
            save_config(config, configuration)
            if args.install_service:
                from .service.launchd import install
                install(config, configuration)
            print(f"Eidolon configured: {configuration}\nTimezone: {config.timezone} "
                  "(UTC fallback when system detection is unavailable).\n"
                  "Change the timezone with eidolon setup --interactive.\n"
                  "Check connections with eidolon doctor; open with eidolon.")
            return
        if args.command == "migrate-state":
            from .rebrand import migrate_state
            report = migrate_state(args.manifest, configuration,
                                   activate_services=args.activate_services)
            print("Installation copied and verified; original data retained for recovery.\n"
                  + json.dumps(report, indent=2))
            return
        config = load_config(configuration)
        if args.command == "migrate":
            from .migration import migrate_hermes
            migrate_hermes(config, args.source, import_credentials=not args.without_credentials,
                           avatar_secret_env=args.avatar_secret_env)
            save_config(config, configuration)
            print("Profile imported. Review the private migration report. "
                  "Imported jobs and memory retention remain disabled.")
        elif args.command == "migrate-jobs":
            from .scheduler.migration import transfer
            from .service.launchd import plist_path
            evidence = json.loads(args.verification.read_text())
            report = transfer(config, args.source,
                              scheduled_job_verified=evidence.get("scheduled_job_verified") is True,
                              service_installed=plist_path().exists(),
                              verification_evidence=str(args.verification.resolve()))
            print(json.dumps(report, indent=2))
        elif args.command == "doctor":
            print(json.dumps(asyncio.run(diagnostic(config, configuration)), indent=2))
        elif args.command == "service":
            from .service import launchd
            if args.action == "run":
                from .service.runtime import run_service
                asyncio.run(run_service(configuration))
            elif args.action == "install":
                print(f"Installed {launchd.install(config, configuration)}")
            elif args.action == "start":
                asyncio.run(launchd.start(config, configuration))
                print("Room service started")
            elif args.action == "stop":
                asyncio.run(launchd.stop(config))
                print("Room service stopped")
            elif args.action == "uninstall":
                asyncio.run(launchd.uninstall(config))
                print("LaunchAgent removed; history and memories preserved")
            elif args.action == "status":
                from .service.client import RoomClient
                try:
                    print(json.dumps(asyncio.run(RoomClient(config.socket_path).request("status")), indent=2))
                except ConnectionError:
                    print("Room service is stopped")
        else:
            from .service.launchd import start
            from .ui.app import RoomApp
            asyncio.run(start(config, configuration))
            RoomApp(config).run()
    except KeyboardInterrupt:
        print("\nRoom closed.")
    except (Exception,) as exc:
        if args.command == "migrate-state":
            print("Installation migration failed; source data is preserved. "
                  "If a destination was created, inspect its private migration/rebrand.json "
                  "report and preserved LaunchAgents before retrying or starting either installation.",
                  file=sys.stderr)
        elif args.command == "migrate":
            print("Profile import failed. Check the source profile and private configuration.",
                  file=sys.stderr)
        else:
            print(f"Eidolon: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
