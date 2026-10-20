from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import time
import uuid
from contextlib import aclosing, asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from eidolon.avatar import AvatarCoordinator
from eidolon.config import (
    AppConfig, AuxiliaryModelConfig, ModelMetadata, OUTPUT_TOKEN_FIELDS, ProviderConfig,
    atomic_write, load_config, resolve_secret, save_config,
    clear_secret_cache, store_secret,
)
from eidolon.engine import ConversationEngine
from eidolon.images import ImageStore, MAX_UPLOAD_BYTES
from eidolon.mcp import MCPManager
from eidolon.memory import HindsightMemory, OutboxDispatcher
from eidolon.memory.maintenance import ContinuityWorker
from eidolon.memory.recovery import ManagedMemoryService
from eidolon.providers import OpenAIProvider, ProviderError
from eidolon.providers.routing import (
    AuxiliaryConfigurationError, configured_route_fingerprint, resolve_auxiliary,
)
from eidolon.providers.metadata import CATALOG_TTL, LOCAL_TTL, ModelMetadataResolver, local_endpoint
from eidolon.preferences import merge_preferences, merge_vision_model, merge_web_search
from eidolon.scheduler import Job, Scheduler, load_jobs, save_jobs
from eidolon.storage import Store
from eidolon.tools import ToolRegistry, WebTools
from eidolon.tools.registry import AGENT_NAMES
from eidolon.tools.vision import VisionAnalysis, resolve_vision


def bounded_memory_inspection(state: dict, *, max_bytes: int = 1_000_000) -> dict:
    """Keep a private list response below its socket allowance, including UTF-8."""
    def size(value) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    estimate = size(state)
    if estimate <= max_bytes:
        return state
    continuity = state["continuity"]
    collections = (
        ("continuity_history", continuity["history"]),
        ("maintenance_stages", continuity["maintenance"].get("stages", [])),
        ("legacy_pending", state["legacy_pending"]),
        ("reply_context", state["manifests"]),
        ("continuity_items", continuity["items"]),
    )
    omitted = {}
    # Leave room for the explicit omission accounting, then verify actual bytes.
    for name, entries in collections:
        # Keep one row so reviewing a held batch can always make progress.
        while len(entries) > 1 and estimate > max_bytes - 2000:
            estimate -= size(entries.pop()) + 2
            omitted[name] = omitted.get(name, 0) + 1
    state["inspection_omitted"] = omitted
    if size(state) > max_bytes:
        raise ValueError("Memory inspection metadata exceeds its bounded allowance")
    return state


class RoomService:
    def __init__(self, config: AppConfig, configuration: Path, *, engine_factory=None):
        self.config = config
        self.configuration = configuration
        # Acquire ownership before opening SQLite or recovering interrupted jobs.
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = config.state_dir / "service.lock"
        self._state_lock = lock_path.open("a")
        lock_path.chmod(0o600)
        try:
            fcntl.flock(self._state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._state_lock.close()
            raise RuntimeError("The room service already owns this state directory") from None
        self._closed = False
        try:
            self._initialize_state(config, engine_factory)
        except Exception:
            if hasattr(self, "store"):
                self.store.close()
            self._state_lock.close()
            raise

    def _initialize_state(self, config, engine_factory):
        self.store = Store(config.database_path)
        self.stop_event = asyncio.Event()
        self.generation_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._auxiliary_reload_lock = asyncio.Lock()
        self._auxiliary_applying = False
        self._skills_lock = asyncio.Lock()
        self._agent_mode_lock = asyncio.Lock()
        self._memory_delivery_lock = asyncio.Lock()
        self._memory_controls_lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._continuity_task: asyncio.Task | None = None
        self._continuity_runtime_error: str | None = None
        self._foreground_waiters = 0
        self._active_agent_mode = False
        self._connections: set[asyncio.Task] = set()
        self._integration_tasks: set[asyncio.Task] = set()
        self.memory_status = "not checked"
        self.service_error: str | None = None
        self.engine_factory = engine_factory or ConversationEngine
        self.model_metadata = ModelMetadataResolver(config.state_dir / "cache/models-dev.json")
        self._metadata_checked: dict[tuple, tuple[float, bool]] = {}
        self._metadata_pending: set[tuple] = set()
        self.current_session = self.store.session_for(timezone=config.timezone, reset_hour=config.reset_hour)
        self._initialize_components()

    def _initialize_components(self, *, preserve_auxiliary: bool = False):
        recovery = ManagedMemoryService(self.config.memory.url,
                                       self.config.state_dir / "memory-service/settings.json")
        self.memory = HindsightMemory(self.config.memory, resolve_secret, recovery=recovery.recover)
        self.mcp = MCPManager(self.config.mcp_servers)
        self.avatar = AvatarCoordinator(self.config.avatar, self.mcp)
        self.tools = ToolRegistry(self.config, self.memory, self.mcp, self.avatar)
        self.engine = self.engine_factory(self.config, self.store, self.memory, self.tools, self.avatar)
        self.dispatcher = OutboxDispatcher(self.memory, self.store, lock=self._memory_delivery_lock)
        self.scheduler = Scheduler(self.store, load_jobs(self.config.jobs_file), self._run_job)
        if not preserve_auxiliary:
            self._initialize_auxiliary(self.config)

    def _persist_configuration(self, candidate: AppConfig, path: Path) -> None:
        previous = load_config(path)
        if candidate.auxiliary.mode == "inherit" and (
            candidate.provider, candidate.selected_provider().model
        ) != (previous.provider, previous.selected_provider().model):
            candidate.auxiliary.context_window = None
        for profile in candidate.providers:
            old = next((p for p in previous.providers if p.name == profile.name), None)
            if old and self._connection_identity(old) != self._connection_identity(profile):
                profile.model_metadata = {}
                profile.models = []
                if candidate.auxiliary.provider == profile.name:
                    candidate.auxiliary.context_window = None
                if candidate.vision.provider == profile.name:
                    candidate.vision.context_window = None
        save_config(candidate, path)
        # Synchronous with persistence under the settings lock. Every save is
        # observed, including A→B→A before the next component reload.
        self.store.reconcile_auxiliary_route(configured_route_fingerprint(candidate))

    def _initialize_auxiliary(self, config: AppConfig) -> None:
        self._auxiliary_source = config.model_copy(deep=True)
        route = self.store.reconcile_auxiliary_route(configured_route_fingerprint(config))
        self._auxiliary_error = None
        self._auxiliary_execution = None
        try:
            self._auxiliary_execution = resolve_auxiliary(config, route["revision"])
        except AuxiliaryConfigurationError as exc:
            self._auxiliary_error = str(exc)
        execution = self._auxiliary_execution
        provider = (OpenAIProvider(execution.profile(), timeout=execution.timeout_seconds,
                                   stream_usage=execution.stream_usage) if execution else None)
        self.continuity_worker = ContinuityWorker(
            self.store, provider, timezone=config.timezone,
            batch_exchanges=config.memory.continuity_batch_exchanges,
            idle_seconds=config.memory.continuity_idle_seconds,
            max_notes=config.memory.continuity_max_notes,
            enabled=config.memory.continuity_enabled,
            timeout=execution.timeout_seconds if execution else 60,
            max_input_chars=execution.context_chars if execution else None,
            execution=execution, commit_result=self._commit_auxiliary,
            identity=self._auxiliary_source.identity,
        )

    async def _commit_auxiliary(self, execution, attempt_id, task, state, operations) -> None:
        # Read the saved file again: an external edit or a crash between file and
        # ledger writes must never authorize an obsolete result. No provider wait
        # or await occurs between this check and the SQLite CAS transaction.
        async with self._settings_lock:
            saved = load_config(self.configuration)
            route = self.store.reconcile_auxiliary_route(configured_route_fingerprint(saved))
            if (route["revision"] != execution.routing_revision
                    or route["fingerprint"] != execution.route_fingerprint):
                raise ValueError("Auxiliary routing changed")
            self.store.commit_continuity(
                task["id"], operations, expected_revision=state["revision"],
                control_revision=state["control_revision"], attempt_id=attempt_id,
                max_notes=saved.memory.continuity_max_notes,
            )

    async def _refresh_auxiliary(self) -> None:
        # Route application is independent of the foreground generation lock.
        async with self._auxiliary_reload_lock:
            self._auxiliary_applying = True
            try:
                async with self._settings_lock:
                    saved = load_config(self.configuration)
                    route = self.store.reconcile_auxiliary_route(configured_route_fingerprint(saved))
                    previous = getattr(self, "_auxiliary_execution", None)
                    unchanged = (configured_route_fingerprint(self._auxiliary_source)
                                 == route["fingerprint"] and
                                 (previous is None or previous.routing_revision == route["revision"]))
                    for attribute, field in (("enabled", "continuity_enabled"),
                                             ("batch_exchanges", "continuity_batch_exchanges"),
                                             ("max_notes", "continuity_max_notes"),
                                             ("idle_seconds", "continuity_idle_seconds")):
                        actual = getattr(self.continuity_worker, attribute,
                                         getattr(self._auxiliary_source.memory, field))
                        unchanged = unchanged and actual == getattr(saved.memory, field)
                if unchanged:
                    # New estimates apply at the next assembly boundary, while
                    # the in-flight immutable snapshot remains valid.
                    self._auxiliary_source = saved.model_copy(deep=True)
                    self.continuity_worker.identity = self._auxiliary_source.identity
                    return
                await self._cancel_continuity()
                await self.continuity_worker.close()
                async with self._settings_lock:
                    saved = load_config(self.configuration)
                    self._initialize_auxiliary(saved)
                    self.config.auxiliary = saved.auxiliary.model_copy(deep=True)
            finally:
                self._auxiliary_applying = False

    def auxiliary_status(self) -> dict:
        execution = self._auxiliary_execution
        return {"mode": self._auxiliary_source.auxiliary.mode,
                "provider": execution.provider if execution else self._auxiliary_source.auxiliary.provider,
                "model": execution.model if execution else self._auxiliary_source.auxiliary.model,
                "context_window": execution.context_window if execution else None,
                "context_chars": execution.context_chars if execution else None,
                "configuration_error": self._auxiliary_error,
                "notice": ("Auxiliary model is off; choose a model in Preferences to refresh current life."
                           if self._auxiliary_source.auxiliary.mode == "disabled" else self._auxiliary_error)}

    async def _cancel_continuity(self) -> None:
        """Stop model work before foreground generation or component replacement."""
        task = self._continuity_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self._continuity_task is task:
                self._continuity_task = None

    @asynccontextmanager
    async def _foreground_generation(self):
        # Reserve priority before awaiting cancellation. Maintenance may not
        # restart between cancellation and acquisition of the generation lock.
        self._foreground_waiters += 1
        try:
            await self._cancel_continuity()
            async with self.generation_lock:
                yield
        finally:
            self._foreground_waiters -= 1

    def _start_continuity(self, *, force: bool = False) -> bool:
        """Schedule at most one bounded pass, never waiting for a provider here."""
        if (self._auxiliary_applying or self._auxiliary_execution is None
                or self.stop_event.is_set() or self.generation_lock.locked()
                or self._foreground_waiters or self._continuity_task is not None
                or not (force or self.config.memory.continuity_enabled)):
            return False
        # Discovery changes the next execution snapshot, never the configured
        # route revision or an already running attempt.
        if getattr(self.continuity_worker, "execution", None) is not None:
            next_execution = resolve_auxiliary(self._auxiliary_source,
                                               self.store.auxiliary_route()["revision"])
            if next_execution.execution_fingerprint != self._auxiliary_execution.execution_fingerprint:
                self._initialize_auxiliary(self._auxiliary_source)
        if self._auxiliary_execution.mode == "separate":
            self._schedule_metadata_refresh(self._auxiliary_execution.profile())
        worker = self.continuity_worker
        async def run():
            try:
                await worker.run_once(force=force)
                self._continuity_runtime_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                # This failure is separate from recall and conversation state.
                self._continuity_runtime_error = "Current life update failed; refresh to retry."
            finally:
                if self._continuity_task is asyncio.current_task():
                    self._continuity_task = None
        self._continuity_task = asyncio.create_task(run(), name="continuity-maintenance")
        return True

    async def _start_integrations(self):
        try:
            await self.mcp.start()
            await self.avatar.start()
        except Exception:
            self.service_error = "An integration could not connect; check settings and connection status."
        await self.check_memory()

    async def check_memory(self):
        if not self.config.memory.bank.strip():
            self.memory_status = "unconfigured; select an existing endpoint and bank with eidolon setup --interactive"
            return
        # Connectivity is independent of the user's recall and saving choices.
        try:
            await self.memory.validate_bank()
            self.memory_status = f"connected to existing bank '{self.config.memory.bank}'"
        except Exception as exc:
            self.memory_status = str(exc)

    async def close_components(self, *, preserve_auxiliary: bool = False):
        if not preserve_auxiliary:
            await self._cancel_continuity()
            await self.continuity_worker.close()
        for task in self._integration_tasks:
            task.cancel()
        await asyncio.gather(*self._integration_tasks, return_exceptions=True)
        self._integration_tasks.clear()
        for component in (self.engine, self.tools, self.avatar, self.mcp, self.memory):
            with suppress(Exception):
                await component.close()

    async def close(self):
        if self._closed:
            return
        try:
            await self.close_components()
        finally:
            self.store.close()
            self._state_lock.close()
            self._closed = True

    def status(self, session_id: str | None = None):
        outbox = self.store.outbox_status()
        body = self.avatar.status()
        catalog = body.get("catalog", {})
        avatar_status = "disabled"
        if self.config.avatar.enabled:
            avatar_status = (
                f"connected · {len(catalog.get('expressions', []))} expressions, "
                f"{len(catalog.get('gestures', []))} gestures"
                if catalog.get("expressions") or catalog.get("gestures")
                else "connecting or unavailable"
            )
        if (not self.config.memory.retain_enabled or not self.config.memory.verified) and outbox["pending"]:
            outbox["error"] = outbox.get("error") or "Retention awaits verified configuration; queued exchanges are preserved."
        profile = self.config.selected_provider()
        provider_label = (profile.display_name or profile.name) + " · " + profile.model
        if effort := profile.parameters.get("reasoning_effort"):
            provider_label += " · " + str(effort)
        return {
            "pid": os.getpid(), "session": self.current_session,
            "provider": provider_label,
            "metrics": self.engine.metrics.get(session_id or self.current_session["id"], {}),
            "model_context": {"provider": profile.name, "model": profile.model,
                              "context_window": profile.context_window_for()},
            "auxiliary": self.auxiliary_status(),
            "memory": self.memory.last_error or self.memory_status,
            "avatar": avatar_status,
            "outbox": outbox,
            "busy": self.generation_lock.locked(), "error": self.service_error,
            "agent_mode": self.tools.agent_mode,
        }

    def memory_state(self, session_id: str | None = None) -> dict:
        """Private, bounded inspection data; reading never triggers recall or retention."""
        manifests = []
        for row in self.store.list_memory_manifests(session_id=session_id, limit=100):
            detail = row["manifest"]
            entries = detail.get("entries", [])
            continuity = detail.get("continuity", {})
            working = continuity.get("entries", [])
            preview = next((str(entry.get("text", ""))[:160]
                            for entry in entries if entry.get("text")), "")
            if not preview:
                preview = next((str(entry.get("content", ""))[:160]
                                for entry in working if entry.get("content")), "")
            manifests.append({**row, "manifest": {
                "status": detail.get("status", "ready"), "preview": preview,
                "entry_count": len(entries) + len(working) + len(continuity.get("recent_exchanges", [])),
                "effort": detail.get("effort"),
                "controls": detail.get("controls", {}),
            }})
        legacy = self.store.list_pending_legacy(limit=100)
        continuity = self.store.continuity_state(limit=100)
        # Evidence and historical revisions load individually on drill-down.
        # Repeating complete provenance across the list can exceed the socket
        # allowance even though the number of displayed rows is bounded.
        continuity["items"] = [{
            **{key: value for key, value in item.items()
               if key not in {"sources", "last_followup_evidence"}},
            "source_count": len(item.get("sources", [])),
            "detail_available": True,
        } for item in continuity["items"]]
        history = [{
            **{key: entry[key] for key in (
                "id", "item_id", "revision", "change_origin", "created_at",
            )},
            "preview": str(entry["data"].get("content", ""))[:160],
            "detail_available": True,
        } for entry in self.store.continuity_history(limit=100)]
        return bounded_memory_inspection({
            "controls": {
                "use_learned_memories": self.config.memory.enabled,
                "save_new_memories": self.config.memory.save_enabled,
                "pending_delivery_paused": self.store.memory_delivery_paused(),
                "automatic_continuity": self.config.memory.continuity_enabled,
                "use_continuity": self.config.memory.continuity_use_enabled,
            },
            "readiness": {"verified": self.config.memory.verified,
                          "retain_enabled": self.config.memory.retain_enabled,
                          "status": self.memory.last_error or self.memory_status},
            "outbox": self.store.outbox_status(), "manifests": manifests,
            "continuity": {
                **continuity,
                "max_notes": self.config.memory.continuity_max_notes,
                "history": history,
                "maintenance": {
                    **self.continuity_worker.status,
                    "auxiliary": self.auxiliary_status(),
                    "running": self._continuity_task is not None,
                    "runtime_error": self._continuity_runtime_error,
                    "automatic": self.config.memory.continuity_enabled,
                    "use_in_replies": (self.config.memory.enabled
                                       and self.config.memory.continuity_use_enabled),
                    "batch_exchanges": self.config.memory.continuity_batch_exchanges,
                    "idle_seconds": self.config.memory.continuity_idle_seconds,
                },
            },
            "legacy_pending": [{key: entry[key] for key in (
                "document_id", "generation_id", "status", "updated_at", "review_state",
            )} | {"source_preview": entry["payload"].get("content", "")[:4000],
                  "source_truncated": len(entry["payload"].get("content", "")) > 4000}
                for entry in legacy],
            "cached_at": datetime.now(timezone.utc).isoformat(),
        })

    async def doctor(self):
        await self.check_memory()
        profile = self.config.selected_provider()
        provider_status = "endpoint not checked"
        try:
            models = await OpenAIProvider(profile).list_models()
            provider_status = f"{len(models)} models available; chat capabilities require a conversation check"
        except ProviderError as exc:
            provider_status = str(exc)
        return {"provider": provider_status, "memory": self.memory_status,
                "auxiliary": {**self.auxiliary_status(), **self.continuity_worker.status},
                "mcp": self.mcp.status(), "avatar": self.avatar.status(),
                "outbox": self.store.outbox_status(), "prompts": list(self.config.prompts)}

    async def _reconcile_day(self, *, force: bool = False):
        if self.generation_lock.locked() and not force:
            return
        today = self.store.session_for(timezone=self.config.timezone, reset_hour=self.config.reset_hour)
        if today["id"] != self.current_session["id"]:
            await self.avatar.session_end(self.current_session["id"])
            self.current_session = today

    async def _reload_connections(self):
        await self._refresh_auxiliary()
        updated = load_config(self.configuration)
        if updated.model_dump() == self.config.model_dump():
            return
        metadata = {
            "providers": {"__all__": {"models", "display_name", "model_metadata"}},
            "preferences": True, "bindings": True, "auxiliary": True, "vision": True,
            "web_search": True, "identity": True,
        }
        if updated.model_dump(exclude=metadata) == self.config.model_dump(exclude=metadata):
            if updated.web_search != self.config.web_search:
                previous_web = self.tools.web
                self.tools.web = WebTools(updated.web_search, resolve_secret)
                await previous_web.close()
            self.config = updated
            self.engine.config = updated
            self.tools.config = updated
            return
        # Editing another saved connection affects future job/auxiliary use,
        # without replacing unrelated dialogue, avatar or MCP adapters.
        profile_metadata = {"models", "display_name", "model_metadata"}
        inactive_connections = {
            "providers", "auxiliary", "vision", "preferences", "bindings", "identity",
        }
        if (updated.provider == self.config.provider
                and updated.selected_provider().model_dump(exclude=profile_metadata)
                == self.config.selected_provider().model_dump(exclude=profile_metadata)
                and updated.model_dump(exclude=inactive_connections)
                == self.config.model_dump(exclude=inactive_connections)):
            self.config = updated
            self.engine.config = updated
            self.tools.config = updated
            return
        async with self._agent_mode_lock:
            # Keep the same delivery lock across adapter replacements. A pause
            # acknowledgement must also cover a request using the old adapter.
            async with self._memory_controls_lock, self._memory_delivery_lock:
                agent_mode = self.tools.agent_mode
                preserve_auxiliary = (
                    self._auxiliary_execution is not None
                    and self._auxiliary_execution.route_fingerprint
                    == configured_route_fingerprint(updated)
                    and self._auxiliary_execution.routing_revision
                    == self.store.auxiliary_route()["revision"]
                )
                await self.close_components(preserve_auxiliary=preserve_auxiliary)
                async with self._settings_lock:
                    # Closing adapters yields. Merge from the latest saved file
                    # so a concurrent auxiliary save cannot be rolled back here.
                    self.config = load_config(self.configuration)
                    self._initialize_components(preserve_auxiliary=preserve_auxiliary)
                self.tools.agent_mode = agent_mode
                self._integration_tasks.add(asyncio.create_task(self._start_integrations()))

    @staticmethod
    def _find_provider(config: AppConfig, name: str) -> ProviderConfig:
        profile = next((profile for profile in config.providers if profile.name == name), None)
        if profile is None:
            raise ValueError("Unknown provider; connect it first")
        return profile

    @staticmethod
    def _connection_identity(profile: ProviderConfig) -> tuple:
        return profile.endpoint, profile.api_key_ref, profile.headers

    def _provider_form(self, config: AppConfig, params: dict) -> tuple[ProviderConfig, str | None]:
        values = params.get("provider")
        if not isinstance(values, dict) or not values.get("name"):
            raise ValueError("Enter a provider name and base URL")
        raw_name = values["name"]
        existing = next((profile for profile in config.providers if profile.name == raw_name), None)
        name = existing.name if existing else raw_name.strip() if isinstance(raw_name, str) else raw_name
        if not isinstance(name, str) or not name.strip() or any(c in name for c in "\r\n\0"):
            raise ValueError("Provider name cannot be empty or contain control characters")
        if existing is None:
            existing = next((profile for profile in config.providers if profile.name == name), None)
        existing_values = existing.model_dump() if existing else {"model": ""}
        merged = existing_values | values | {"name": name}
        if existing:
            # Connection forms carry full older profiles, but do not edit these fields.
            for field in {"models", "model_metadata", "parameters", "presentation", "structured_verified",
                          "post_history"}:
                merged[field] = existing_values[field]
            if values.get("model", existing.model) != existing.model and "context_window" not in values:
                merged["context_window"] = None
            if values.get("model", existing.model) != existing.model:
                merged["vision"] = None
        if existing and params.get("connection_only") is True:
            for field in ("model", "context_window", "tools", "vision"):
                merged[field] = existing_values[field]
        api_key = params.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("API key must be text or a credential reference")
        api_key = api_key.strip() if api_key else None
        if existing and not api_key:
            merged["api_key_ref"] = existing.api_key_ref
        literal_key = None
        if api_key:
            if api_key.startswith(("env:", "keyring:")):
                merged["api_key_ref"] = api_key
            else:
                literal_key = api_key
        profile = ProviderConfig.model_validate(merged)
        if existing and (literal_key is not None or
                         self._connection_identity(profile) != self._connection_identity(existing)):
            profile.model_metadata = {}
        if existing and profile.endpoint != existing.endpoint:
            if existing.api_key_ref and not api_key:
                raise ValueError("Reenter the API key or its reference before changing this provider's base URL")
            secret_headers = {value for value in existing.headers.values()
                              if value.startswith(("env:", "keyring:"))}
            inherited = bool(secret_headers.intersection(profile.headers.values()))
            if inherited and params.get("confirm_header_credentials") is not True:
                raise ValueError("Clear or reenter secret header references before changing this provider's base URL")
            profile.models = []
        return profile, literal_key

    async def _discover_models(self, profile: ProviderConfig, api_key: str | None = None) -> dict:
        provider = OpenAIProvider(profile)
        try:
            models = await provider.list_models(api_key=api_key)
            result = {"models": models, "error": None}
        except ProviderError as exc:
            result = {"models": [], "error": str(exc)}
        except Exception:
            result = {"models": [], "error": "Model discovery failed; check the provider settings or enter a model ID manually."}
        try:
            metadata = await self.model_metadata.resolve(
                provider, list(dict.fromkeys([*result["models"], profile.model])),
                inventory=provider.model_inventory, api_key=api_key,
            )
            if metadata:
                result["model_metadata"] = {model: item.model_dump() for model, item in metadata.items()}
        except Exception:
            pass  # Metadata support is optional; discovery and chat remain available.
        return result

    @staticmethod
    def _metadata_key(profile: ProviderConfig) -> tuple:
        return (profile.endpoint, profile.api_key_ref, tuple(sorted(profile.headers.items())), profile.model)

    def _schedule_metadata_refresh(self, profile: ProviderConfig | None = None) -> None:
        profile = profile or self.config.selected_provider()
        if profile.model == "configure-model" or (
            profile.context_window is not None and profile.vision is not None
        ):
            return
        key = self._metadata_key(profile)
        if key in self._metadata_pending:
            return
        checked, found = self._metadata_checked.get(key, (0, False))
        runtime = local_endpoint(profile.endpoint) or profile.name.lower() in {
            "lmstudio", "ollama", "llama.cpp", "llamacpp",
        }
        ttl = (LOCAL_TTL if runtime else CATALOG_TTL) if found else 300
        metadata = profile.model_metadata.get(profile.model)
        if (metadata and key not in self._metadata_checked and not runtime
                and (profile.context_window is not None or metadata.context_window is not None)
                and (profile.vision is not None or metadata.vision is not None)):
            if 0 <= time.time() - metadata.updated_at < CATALOG_TTL:
                return
        if checked and time.monotonic() - checked < ttl:
            return
        self._metadata_pending.add(key)
        task = asyncio.create_task(self.refresh_model_metadata(profile.model_copy(deep=True)))
        self._integration_tasks.add(task)
        task.add_done_callback(self._integration_tasks.discard)

    async def refresh_model_metadata(self, profile: ProviderConfig) -> None:
        key = self._metadata_key(profile)
        found = False
        try:
            result = await self._discover_models(profile)
            metadata = result.get("model_metadata", {})
            selected = metadata.get(profile.model)
            found = selected is not None and 0 <= time.time() - selected["updated_at"] < CATALOG_TTL
            if not metadata:
                return
            async with self._settings_lock:
                candidate = load_config(self.configuration)
                current = self._find_provider(candidate, profile.name)
                if self._connection_identity(current) != self._connection_identity(profile):
                    return
                current.model_metadata.update({model: ModelMetadata.model_validate(item)
                                               for model, item in metadata.items()})
                self._persist_configuration(candidate, self.configuration)
                self._update_live_metadata(current)
        except Exception:
            pass  # Background metadata errors must never become conversation errors.
        finally:
            self._metadata_checked[key] = (time.monotonic(), found)
            self._metadata_pending.discard(key)

    def _update_live_metadata(self, profile: ProviderConfig) -> None:
        current = next((item for item in self.config.providers if item.name == profile.name), None)
        if current and self._connection_identity(current) == self._connection_identity(profile):
            current.model_metadata = profile.model_metadata.copy()
            self.engine.config = self.config
            source = next((p for p in self._auxiliary_source.providers if p.name == current.name), None)
            if source and self._connection_identity(source) == self._connection_identity(current):
                source.model_metadata = current.model_metadata.copy()
                if (self._continuity_task is None
                        and getattr(self.continuity_worker, "execution", None) is not None):
                    self._initialize_auxiliary(self._auxiliary_source)

    async def _apply_saved_configuration(self):
        async with self._settings_lock:
            saved = await asyncio.to_thread(load_config, self.configuration)
            # Running operations own copied snapshots; future requests can use
            # saved names even while unrelated connection changes are deferred.
            for config in (self.config, self.engine.config, self.tools.config):
                config.identity = saved.identity.model_copy(deep=True)
        await self._refresh_auxiliary()
        # Never replace components used by an in-flight response or scheduled job.
        if not self.generation_lock.locked():
            async with self.generation_lock:
                await self._reload_connections()
                await self._reconcile_day(force=True)

    async def _test_web_search(self, params: dict) -> dict:
        snapshot = await asyncio.to_thread(load_config, self.configuration)
        candidate, keys = merge_web_search(
            snapshot, params.get("web_search"), params.get("web_search_api_keys"),
        )
        provider = candidate.web_search.provider
        def resolver(reference: str | None) -> str | None:
            return keys.get(provider) or resolve_secret(reference)
        web = WebTools(candidate.web_search, resolver)
        try:
            result = await web.search("open source software")
            return {"provider": provider, "results": len(result["results"])}
        finally:
            await web.close()

    async def _list_models(self, name: str, model: str | None = None) -> dict:
        snapshot = load_config(self.configuration)
        profile = self._find_provider(snapshot, name).model_copy(deep=True)
        if model is not None:
            auxiliary = AuxiliaryModelConfig(model=model)
            if auxiliary.model != profile.model:
                profile.context_window = None
                profile.model = auxiliary.model
        result = await self._discover_models(profile)
        if result["error"] is not None:
            result["models"] = sorted(set(profile.models + ([profile.model] if profile.model else [])))
        async with self._settings_lock:
            candidate = load_config(self.configuration)
            current = self._find_provider(candidate, name)
            if self._connection_identity(current) == self._connection_identity(profile):
                if result["error"] is None:
                    current.models = result["models"]
                current.model_metadata.update({model: ModelMetadata.model_validate(item)
                    for model, item in result.get("model_metadata", {}).items()})
                self._persist_configuration(candidate, self.configuration)
                self._update_live_metadata(self._find_provider(candidate, name))
        # Inventory is metadata: no component reload or generation lock is needed.
        return result

    async def _save_provider(self, params: dict) -> dict:
        snapshot = load_config(self.configuration)
        profile, literal_key = self._provider_form(snapshot, params)
        discovery = await self._discover_models(profile, literal_key)
        if not profile.model.strip() and not discovery["models"]:
            raise ValueError("Enter a model ID before saving: " + (discovery["error"] or "No models were discovered"))
        async with self._settings_lock:
            candidate = load_config(self.configuration)
            # Reapply only this form to the latest file, preserving other pending settings.
            current, literal_key = self._provider_form(candidate, params)
            previous = next((item for item in snapshot.providers if item.name == current.name), None)
            latest = next((item for item in candidate.providers if item.name == current.name), None)
            if previous and latest and previous.model != latest.model:
                raise ValueError("Provider model changed while discovering models; reopen the connection and retry saving")
            if self._connection_identity(current) != self._connection_identity(profile):
                raise ValueError("Provider credentials changed while discovering models; retry saving")
            if not current.model.strip():
                current.model = discovery["models"][0]
            if discovery["error"] is None:
                current.models = discovery["models"]
            current.model_metadata.update({model: ModelMetadata.model_validate(item)
                for model, item in discovery.get("model_metadata", {}).items()})
            if literal_key:
                try:
                    current.api_key_ref = await asyncio.to_thread(
                        store_secret, "provider-" + uuid.uuid4().hex, literal_key)
                except Exception:
                    raise ValueError("Could not store the API key in Keychain; check Keychain access and retry") from None
            if (latest and self._connection_identity(current) != self._connection_identity(latest)
                    and candidate.auxiliary.provider == current.name):
                candidate.auxiliary.context_window = None
            candidate.providers = [current if item.name == current.name else item
                                   for item in candidate.providers] + (
                [] if any(item.name == current.name for item in candidate.providers) else [current]
            )
            self._persist_configuration(candidate, self.configuration)
        await self._apply_saved_configuration()
        models = current.models if discovery["error"] is None else sorted(
            set(current.models + ([current.model] if current.model else [])))
        return {"config": candidate.model_dump(mode="json"), "models": models,
                "warning": discovery["error"]}

    async def _select_model(self, params: dict) -> dict:
        model = params.get("model")
        effort = params.get("reasoning_effort")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Enter a model ID")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise ValueError("Choose a supported reasoning effort")
        for key, label in (("context_window", "Context Size"),
                           ("max_response_tokens", "Max Response Length")):
            if key not in params or key == "context_window" and params[key] is None:
                continue
            if type(params[key]) is not int or params[key] <= 0:
                raise ValueError(f"{label} must be a positive token count")
        async with self._settings_lock:
            candidate = load_config(self.configuration)
            profile = self._find_provider(candidate, params.get("name"))
            if (effort is not None and effort not in {
                    "none", "minimal", "low", "medium", "high", "xhigh", "max"}
                    and effort != profile.parameters.get("reasoning_effort")):
                raise ValueError("Choose a supported reasoning effort")
            if profile.model != model.strip():
                profile.context_window = None
                profile.vision = None
            profile.model = model.strip()
            if "context_window" in params:
                profile.context_window = params["context_window"]
            if "max_response_tokens" in params:
                field = profile.output_token_parameter()
                for key in OUTPUT_TOKEN_FIELDS:
                    profile.parameters.pop(key, None)
                profile.parameters[field] = params["max_response_tokens"]
            if effort is None:
                profile.parameters.pop("reasoning_effort", None)
            else:
                profile.parameters["reasoning_effort"] = effort
            candidate.provider = profile.name
            self._persist_configuration(candidate, self.configuration)
        await self._apply_saved_configuration()
        self._schedule_metadata_refresh(candidate.selected_provider())
        return candidate.model_dump(mode="json")

    async def _test_auxiliary(self, params: dict) -> dict:
        """Explicit synthetic test, sharing the preemptible auxiliary slot."""
        async with self._auxiliary_reload_lock:
            self._auxiliary_applying = True
            try:
                if self.generation_lock.locked() or self._foreground_waiters:
                    return {"ok": False, "notice": "A conversation is running; retry the synthetic test when idle."}
                async with self._settings_lock:
                    candidate = load_config(self.configuration)
                    candidate.auxiliary = AuxiliaryModelConfig.model_validate(params.get("auxiliary", {}))
                    execution = resolve_auxiliary(candidate)
                if execution is None:
                    return {"ok": False, "notice": "Auxiliary model is off. Choose a target to test."}
                await self._cancel_continuity()
                # Foreground priority is reserved before cancellation yields.
                if self.generation_lock.locked() or self._foreground_waiters or self.stop_event.is_set():
                    return {"ok": False, "notice": "Synthetic test deferred for foreground work."}

                async def run_test():
                    provider = OpenAIProvider(execution.profile(), timeout=execution.timeout_seconds,
                                              stream_usage=execution.stream_usage)
                    try:
                        text = ""
                        finished = False
                        async with asyncio.timeout(execution.timeout_seconds), aclosing(provider.stream([
                            {"role": "system", "content": "Synthetic connection test. Reply with exactly OK."},
                            {"role": "user", "content": "Please reply OK."},
                        ], tools=None, presentation="plain")) as stream:
                            async for event in stream:
                                if event["type"] == "token":
                                    text += event["text"]
                                    if len(text) > 1000:
                                        raise ValueError("Test output too large")
                                if event["type"] == "finish":
                                    finished = event.get("reason") == "stop"
                        ok = finished and text.strip() == "OK"
                        return {"ok": ok, "notice": "Synthetic connection test passed. Memory quality was not tested."
                                if ok else "Model connected but did not complete the synthetic OK response."}
                    except asyncio.CancelledError:
                        return {"ok": False, "notice": "Synthetic test interrupted for foreground work or a settings change."}
                    except Exception:
                        return {"ok": False, "notice": "Synthetic test failed. Check the connection, model ID and request options."}
                    finally:
                        if self._continuity_task is asyncio.current_task():
                            self._continuity_task = None

                task = asyncio.create_task(run_test(), name="auxiliary-synthetic-test")
                self._continuity_task = task
            finally:
                self._auxiliary_applying = False
        try:
            return await task
        finally:
            if self._continuity_task is task:
                self._continuity_task = None

    async def _test_vision(self, params: dict) -> dict:
        """Test a synthetic color image, with the same foreground priority as auxiliary tests."""
        import base64
        import struct
        import zlib

        async with self._auxiliary_reload_lock:
            if self.generation_lock.locked() or self._foreground_waiters:
                return {"ok": False, "notice": "A conversation is running; retry the image test when idle."}
            async with self._settings_lock:
                saved = await asyncio.to_thread(load_config, self.configuration)
                candidate = merge_vision_model(saved, params.get("vision", {}))
            if resolve_vision(candidate) is None:
                return {"ok": False, "notice": "Choose a separate vision model to test."}
            self._auxiliary_applying = True
            try:
                await self._cancel_continuity()
                if self.generation_lock.locked() or self._foreground_waiters or self.stop_event.is_set():
                    return {"ok": False, "notice": "Image test deferred for foreground work."}

                async def run_test():
                    try:
                        def chunk(kind, data):
                            return (struct.pack(">I", len(data)) + kind + data
                                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

                        png = (b"\x89PNG\r\n\x1a\n"
                               + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
                               + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 16) * 16))
                               + chunk(b"IEND", b""))
                        image = await asyncio.to_thread(
                            ImageStore(candidate.state_dir).upload,
                            base64.b64encode(png).decode("ascii"),
                        )
                        analysis = VisionAnalysis(candidate, {image["id"]})
                        result = await analysis.analyze(
                            image["id"], "What color is the image? Reply with exactly the color name."
                        )
                        ok = result["analysis"].strip().lower().strip(".*! ") == "red"
                        return {"ok": ok, "notice": "Synthetic image test passed."
                                if ok else "The model responded but did not identify the test image's color."}
                    except asyncio.CancelledError:
                        return {"ok": False, "notice": "Image test interrupted for foreground work or a settings change."}
                    except Exception:
                        return {"ok": False, "notice": "Image test failed. Check the connection, vision support and request options."}

                task = asyncio.create_task(run_test(), name="vision-synthetic-test")
                self._continuity_task = task
            finally:
                self._auxiliary_applying = False
        try:
            return await task
        finally:
            if self._continuity_task is task:
                self._continuity_task = None

    async def _maintenance(self):
        while not self.stop_event.is_set():
            try:
                await self.check_memory()
                if not self.generation_lock.locked():
                    async with self.generation_lock:
                        await self._reload_connections()
                    await self._reconcile_day()
                    jobs = load_jobs(self.config.jobs_file)
                    self.scheduler.jobs = jobs
                    for job in jobs:
                        self.store.save_job(job.id, job.model_dump())
                    await self.scheduler.tick()
                await self.dispatcher.dispatch_once()
                self._start_continuity()
                self.service_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.service_error = f"Background work needs attention: {type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(self.stop_event.wait(), 10)
            except TimeoutError:
                pass

    async def _run_job(self, job: Job, run: dict) -> str:
        async with self._foreground_generation():
            config = self.config.model_copy(deep=True)
            config.identity = load_config(self.configuration).identity
            if job.provider:
                config.provider = job.provider
            tools = self.tools
            engine = None
            permitted_tools = job.permitted_tools
            reply = ""
            try:
                if job.agent_mode:
                    if not config.selected_provider().tools:
                        raise ValueError("Enable tool calling for this job's provider to use agent mode")
                    tools = ToolRegistry(config, self.memory, self.mcp, self.avatar,
                                         background_agent=True)
                    await tools.set_agent_mode(True)
                    if not permitted_tools:
                        permitted_tools = sorted(AGENT_NAMES | {"read_skill"})
                engine = self.engine_factory(config, self.store, self.memory, tools, self.avatar)
                async for event in engine.generate(run["session_id"], job.prompt, interactive=False,
                                                   agent_mode=job.agent_mode,
                                                   permitted_tools=permitted_tools,
                                                   required_tools=job.required_tools, generation_id=run["id"]):
                    if event["type"] in {"error", "recall_required"}:
                        raise RuntimeError(event.get("message", "Scheduled generation failed"))
                    if event["type"] == "token":
                        reply += event.get("text", "")
            finally:
                try:
                    if engine is not None:
                        await engine.close()
                finally:
                    if tools is not self.tools:
                        await tools.close()
            return reply

    async def _change_job(self, method: str, params: dict) -> dict:
        """Apply one job change without overwriting other jobs or stale editor data."""
        async with self._settings_lock:
            saved_config = await asyncio.to_thread(load_config, self.configuration)
            path = saved_config.jobs_file or saved_config.state_dir / "jobs.json"
            original = await asyncio.to_thread(load_jobs, path)
            jobs = list(original)
            job = Job.model_validate(params["job"]) if method == "save_job" else None
            job_id = job.id if job else params["id"]
            previous = next((item for item in jobs if item.id == job_id), None)
            if method == "save_job" and params.get("create", False):
                if previous is not None:
                    raise ValueError("A job with this identifier already exists")
            elif previous is None:
                raise ValueError("This job was removed; reload the job list")
            if "expected_job" in params and (
                previous is None or previous != Job.model_validate(params["expected_job"])
            ):
                raise ValueError("This job changed since it was opened; reload before saving")
            if method == "toggle_job":
                if not isinstance(params.get("enabled"), bool):
                    raise ValueError("Job enabled must be a boolean")
                job = previous.model_copy(update={"enabled": params["enabled"]})
            if job is not None and (method == "save_job" or job.enabled):
                provider = job.provider or saved_config.provider
                profile = next((profile for profile in saved_config.providers
                                if profile.name == provider), None)
                if profile is None:
                    raise ValueError("Choose a configured provider for this job")
                if job.enabled and job.agent_mode and not profile.tools:
                    raise ValueError("Enable tool calling for this job's provider to use agent mode")
            jobs = [item for item in jobs if item.id != job_id]
            if job is not None:
                index = original.index(previous) if previous is not None else len(jobs)
                jobs.insert(index, job)
            await asyncio.to_thread(save_jobs, path, jobs, expected_jobs=original)
            if saved_config.jobs_file is None:
                saved_config.jobs_file = path
                await asyncio.to_thread(self._persist_configuration, saved_config, self.configuration)
            self.config.jobs_file = path
            self.scheduler.jobs = jobs
            for item in jobs:
                self.store.save_job(item.id, item.model_dump())
            return {"job": job.model_dump() if job is not None else None}

    async def _set_agent_mode(self, params: dict) -> dict:
        async with self._agent_mode_lock:
            enabled = params.get("enabled")
            if "enabled" in params and not isinstance(enabled, bool):
                raise ValueError("Agent mode enabled must be a boolean")
            if enabled is not False and not self.config.selected_provider().tools:
                if enabled is True or not self.tools.agent_mode:
                    raise ValueError("Enable tool calling for the selected provider before using /agent.")
            def cancel_agent_turn():
                if self._active_task and self._active_agent_mode:
                    self._active_task.cancel()
            agent_mode = await self.tools.set_agent_mode(enabled, on_disable=cancel_agent_turn)
            return {"agent_mode": agent_mode}

    async def handle(self, method: str, params: dict):
        if method == "memory_state":
            return self.memory_state(params.get("session_id"))
        if method in {"memory_create_note", "memory_edit_note", "memory_delete_note"}:
            async with self._settings_lock:
                saved = await asyncio.to_thread(load_config, self.configuration)
                max_notes = saved.memory.continuity_max_notes
                item = None
                if method == "memory_create_note":
                    item = await asyncio.to_thread(
                        self.store.create_continuity_item, params.get("content"), params.get("category"),
                        topic=params.get("topic"), importance=params.get("importance"),
                        max_notes=max_notes,
                    )
                else:
                    item_id = params.get("item_id")
                    if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
                        raise ValueError("Choose a current life note")
                    if method == "memory_edit_note":
                        item = await asyncio.to_thread(
                            self.store.edit_continuity_item, item_id,
                            {key: params[key] for key in
                             ("content", "category", "topic", "importance", "state") if key in params},
                            expected_revision=params.get("expected_revision"),
                            max_notes=max_notes,
                        )
                    else:
                        await asyncio.to_thread(
                            self.store.delete_continuity_item, item_id,
                            expected_revision=params.get("expected_revision"),
                        )
            return {"item": item, "snapshot": self.memory_state(params.get("session_id"))}
        if method == "memory_manifest":
            return self.store.get_memory_manifest(int(params["manifest_id"]))
        if method == "memory_continuity_item":
            item_id = params.get("item_id")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
                raise ValueError("Choose a current life item to inspect")
            return self.store.get_continuity_item(item_id)
        if method == "memory_continuity_history":
            history_id = params.get("history_id")
            if type(history_id) is not int or history_id < 1:
                raise ValueError("Choose a continuity revision to inspect")
            return self.store.get_continuity_history(history_id)
        if method == "memory_continuity_run":
            task_id = params.get("task_id")
            if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
                raise ValueError("Choose a current life refresh to inspect")
            run = self.store.continuity_run_changes(task_id)
            if run is None:
                return None
            # A complete run fits the socket allowance without source inventories,
            # hashes, or other provenance repeated for every historical version.
            fields = {"category", "content", "state", "event_at", "expires_at", "follow_up_at",
                      "observed_at", "last_mention_at", "promise_owner", "promise_kind"}
            return {**run, "changes": [{
                "after": {key: value for key, value in change["after"].items() if key in fields},
                "before": ({key: value for key, value in change["before"].items() if key in fields}
                           if change["before"] is not None else None),
                "created": change["created"],
            } for change in run["changes"]]}
        if method == "memory_refresh":
            await self._refresh_auxiliary()
            queued = self._start_continuity(force=True)
            return {**self.memory_state(params.get("session_id")), "refresh_queued": queued}
        if method == "memory_delivery":
            if not isinstance(params.get("pause"), bool):
                raise ValueError("Choose whether to pause pending memory delivery")
            async with self._memory_controls_lock:
                await self.dispatcher.pause_delivery(params["pause"])
            return self.memory_state(params.get("session_id"))
        if method == "memory_review_legacy":
            if not isinstance(params.get("approve"), bool):
                raise ValueError("Choose whether to include the held exchanges in memory")
            document_ids = params.get("document_ids")
            if (not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 100
                    or any(not isinstance(value, str) or not value for value in document_ids)):
                raise ValueError("Select the held exchanges shown in the review")
            async with self._memory_controls_lock:
                await self.dispatcher.review_legacy(params["approve"], document_ids=document_ids)
            return self.memory_state(params.get("session_id"))
        if method == "agent_mode":
            return await self._set_agent_mode(params)
        if method == "jobs":
            jobs = await asyncio.to_thread(load_jobs, self.config.jobs_file)
            return {"jobs": [job.model_dump() for job in jobs], "runs": self.store.list_job_runs()}
        if method in {"save_job", "toggle_job", "delete_job"}:
            return await self._change_job(method, params)
        if method == "skills":
            return {"skills": await asyncio.to_thread(self.tools.skills.list_skills)}
        if method == "get_skill":
            return await asyncio.to_thread(self.tools.skills.get, params["name"])
        if method in {"save_skill", "toggle_skill"}:
            async with self._skills_lock:
                if method == "toggle_skill":
                    return await asyncio.to_thread(
                        self.tools.skills.set_enabled, params["name"], params["enabled"],
                    )
                return await asyncio.to_thread(
                    self.tools.skills.save, params["name"], params["description"],
                    params["instructions"], enabled=params["enabled"],
                    create=params.get("create", False), expected_hash=params.get("expected_hash"),
                )
        if method == "status":
            await self._reconcile_day()
            self._schedule_metadata_refresh()
            return self.status(params.get("session_id"))
        if method == "overview":
            saved = load_config(self.configuration)
            return {"sessions": self.store.list_sessions(),
                    "runs": self.store.list_job_runs(),
                    "jobs": [j.model_dump() for j in load_jobs(self.config.jobs_file)],
                    "config": saved.model_dump(mode="json"),
                    "prompts": {name: Path(path).expanduser().read_text() for name, path in self.config.prompts.items()}}
        if method == "history":
            session_id = params["session_id"]
            return {"session": self.store.get_session(session_id), "messages": self.store.list_messages(session_id)}
        if method == "latest_model_request":
            return {"request": self.store.latest_model_request()}
        if method == "model_requests":
            session_id = params.get("session_id") or self.current_session["id"]
            return {"session": self.store.get_session(session_id),
                    "requests": self.store.list_model_requests(session_id)}
        if method == "model_request_metrics":
            return {"requests": self.store.list_request_metrics(
                params.get("session_id"), limit=params.get("limit", 100))}
        if method in {"model_request", "export_model_request"}:
            request = self.store.get_model_request(int(params["request_id"]))
            if method == "model_request":
                return request
            target = self.config.state_dir / "diagnostics" / "requests" / "latest.json"
            atomic_write(target, request["body"], mode=0o600)
            return {"path": str(target.resolve())}
        if method == "save_prompt":
            name = params["name"]
            if name not in self.config.prompts or len(params["text"]) > 1_000_000:
                raise ValueError("Unknown prompt or prompt exceeds the size limit")
            atomic_write(Path(self.config.prompts[name]).expanduser(), params["text"])
            return {"saved": name}
        if method == "preview":
            return await self.engine.preview_prompt(
                params.get("session_id") or self.current_session["id"], params.get("text", ""),
                agent_mode=self.tools.agent_mode,
            )
        if method == "clear_secret_cache":
            await asyncio.to_thread(clear_secret_cache, params["reference"])
            return {"cleared": True}
        if method == "list_models":
            return await self._list_models(params.get("name"), params.get("model"))
        if method == "discover_models":
            profile, literal_key = self._provider_form(load_config(self.configuration), params)
            return await self._discover_models(profile, literal_key)
        if method == "save_provider":
            return await self._save_provider(params)
        if method == "test_web_search":
            return await self._test_web_search(params)
        if method in {"save_preferences", "save_auxiliary_model", "save_preferences_with_auxiliary",
                      "save_preferences_with_vision", "save_vision_model",
                      "save_preferences_with_web_search"}:
            async with self._settings_lock:
                candidate = await asyncio.to_thread(load_config, self.configuration)
                if method not in {"save_auxiliary_model", "save_vision_model"}:
                    candidate = await asyncio.to_thread(
                        merge_preferences, candidate, params["preferences"],
                    )
                if method in {"save_auxiliary_model", "save_preferences_with_auxiliary"} or (
                    method in {"save_preferences_with_vision", "save_preferences_with_web_search"}
                    and "auxiliary" in params
                ):
                    values = params.get("auxiliary")
                    if not isinstance(values, dict):
                        raise ValueError("Choose auxiliary model settings")
                    auxiliary = AuxiliaryModelConfig.model_validate({
                        **candidate.auxiliary.model_dump(), **values,
                    })
                    if (auxiliary.mode, auxiliary.provider, auxiliary.model) != (
                        candidate.auxiliary.mode, candidate.auxiliary.provider,
                        candidate.auxiliary.model,
                    ) and "context_window" not in values:
                        auxiliary.context_window = None
                    candidate.auxiliary = auxiliary
                    resolve_auxiliary(candidate)  # Save-time cross-reference validation.
                if method in {"save_preferences_with_vision", "save_vision_model"} or (
                    method == "save_preferences_with_web_search" and "vision" in params
                ):
                    values = params.get("vision")
                    if not isinstance(values, dict):
                        raise ValueError("Choose vision model settings")
                    candidate = merge_vision_model(candidate, values)
                if method == "save_preferences_with_web_search":
                    candidate, keys = merge_web_search(
                        candidate, params.get("web_search"), params.get("web_search_api_keys"),
                    )
                    for provider, key in keys.items():
                        try:
                            reference = await asyncio.to_thread(
                                store_secret, "web-search-" + provider + "-" + uuid.uuid4().hex, key,
                            )
                        except Exception:
                            raise ValueError("Could not store the search API key in Keychain; check Keychain access and retry") from None
                        getattr(candidate.web_search, provider).api_key_ref = reference
                await asyncio.to_thread(self._persist_configuration, candidate, self.configuration)
            await self._apply_saved_configuration()
            vision_changed = method in {"save_preferences_with_vision", "save_vision_model"} or (
                method == "save_preferences_with_web_search" and "vision" in params
            )
            if (vision_changed
                    and self._continuity_task is not None
                    and self._continuity_task.get_name() == "vision-synthetic-test"):
                await self._cancel_continuity()
            if (method != "save_preferences" and self._auxiliary_execution
                    and (method != "save_preferences_with_web_search" or "auxiliary" in params)):
                self._schedule_metadata_refresh(self._auxiliary_execution.profile())
            if vision_changed:
                profile = resolve_vision(candidate)
                if profile:
                    self._schedule_metadata_refresh(profile)
            return candidate.model_dump(mode="json")
        if method == "test_auxiliary_model":
            return await self._test_auxiliary(params)
        if method == "test_vision_model":
            return await self._test_vision(params)
        if method == "upload_image":
            return await asyncio.to_thread(
                ImageStore(self.config.state_dir).upload, params.get("data_base64"),
                params.get("filename", "Clipboard image.png"), params.get("media_type", "image/png"),
            )
        if method == "maintenance_attempts":
            task_id = params.get("task_id")
            if not isinstance(task_id, str) or len(task_id) > 100:
                raise ValueError("Choose a maintenance task")
            return {"attempts": self.store.maintenance_attempts(task_id)}
        if method == "select_model":
            return await self._select_model(params)
        if method in {"select_provider", "save_theme", "save_settings"}:
            async with self._settings_lock:
                candidate = await asyncio.to_thread(load_config, self.configuration)
                if method == "select_provider":
                    candidate.provider = params["name"]
                elif method == "save_theme":
                    from eidolon.themes import resolve_theme, validate_theme

                    validate_theme(params["name"], params["colors"])
                    if candidate.theme.colors and not candidate.theme.custom_base:
                        candidate.theme.custom_base = resolve_theme(candidate.theme.name)
                        candidate.theme.custom_colors = dict(candidate.theme.colors)
                    candidate.theme.name = params["name"]
                    candidate.theme.colors = params["colors"]
                    if "custom_base" in params:
                        validate_theme(params["custom_base"], params["custom_colors"])
                        candidate.theme.custom_base = params["custom_base"]
                        candidate.theme.custom_colors = params["custom_colors"]
                else:
                    values = dict(params["config"])
                    values["auxiliary"] = candidate.auxiliary.model_dump()
                    values["vision"] = candidate.vision.model_dump()
                    candidate = AppConfig.model_validate(values)
                    if candidate.state_dir.resolve() != self.config.state_dir.resolve():
                        raise ValueError("Changing the state directory requires restarting with a separate configuration")
                await asyncio.to_thread(self._persist_configuration, candidate, self.configuration)
            await self._apply_saved_configuration()
            return candidate.model_dump(mode="json")
        if method == "save_jobs":
            jobs = [Job.model_validate(j) for j in params["jobs"]]
            if len({j.id for j in jobs}) != len(jobs):
                raise ValueError("Job identifiers must be unique")
            if not self.config.jobs_file:
                raise ValueError("Configure the jobs file first")
            save_jobs(self.config.jobs_file, jobs)
            return {"saved": len(jobs)}
        if method == "retry_job":
            self.store.retry_job_run(params["run_id"])
            return {"queued": True}
        if method == "cancel":
            if self._active_task:
                self._active_task.cancel()
            return {"cancelled": bool(self._active_task)}
        if method == "doctor":
            return await self.doctor()
        if method == "shutdown":
            self.stop_event.set()
            return {"stopping": True}
        raise ValueError("Unknown service request")

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        self._connections.add(task)
        async def send(event):
            writer.write((json.dumps(event, ensure_ascii=False, default=str) + "\n").encode())
            await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if len(line) > MAX_UPLOAD_BYTES:
                raise ValueError("Request exceeds the size limit")
            request = json.loads(line)
            method, params = request["method"], request.get("params", {})
            if method != "upload_image" and len(line) > 1_000_000:
                raise ValueError("Request exceeds the size limit")
            received_at = datetime.now(timezone.utc)
            if method == "generate":
                async with self._foreground_generation():
                    await self._reload_connections()
                    await self._reconcile_day(force=True)
                    text = params["text"]
                    images = params.get("images", [])
                    await asyncio.to_thread(ImageStore(self.config.state_dir).resolve, images)
                    if (not isinstance(text, str) or len(text) > 100_000
                            or not text.strip() and not images):
                        raise ValueError("Enter a message shorter than 100000 characters")
                    session_id = params.get("session_id") or self.current_session["id"]
                    if session_id != self.current_session["id"] and not params.get("resume"):
                        raise ValueError("Resume past sessions explicitly before continuing them")
                    self._active_task = task
                    self._active_agent_mode = self.tools.agent_mode
                    try:
                        async for event in self.engine.generate(
                            session_id, text, bypass_recall=bool(params.get("bypass_recall")),
                            agent_mode=self._active_agent_mode,
                            user_message_at=received_at,
                            **({"images": images} if images else {}),
                        ):
                            await send(event)
                    except asyncio.CancelledError:
                        await send({"type": "error", "message": "Response cancelled; no partial exchange was retained."})
                    finally:
                        self._active_task = None
                        self._active_agent_mode = False
                await self._reconcile_day()
            else:
                result = await self.handle(method, params)
                await send({"type": "result", "data": result})
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            with suppress(BrokenPipeError, ConnectionResetError):
                await send({"type": "error", "message": str(exc)})
        finally:
            with suppress(BrokenPipeError, ConnectionResetError):
                await send({"type": "done"})
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._connections.discard(task)

    async def run(self):
        socket = self.config.socket_path
        maintenance = None
        try:
            socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            socket.parent.chmod(0o700)
            socket.unlink(missing_ok=True)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, self.stop_event.set)
            server = await asyncio.start_unix_server(self.connection, path=str(socket),
                                                    limit=MAX_UPLOAD_BYTES + 1)
            socket.chmod(0o600)
            self._integration_tasks.add(asyncio.create_task(self._start_integrations()))
            maintenance = asyncio.create_task(self._maintenance())
            async with server:
                await self.stop_event.wait()
        finally:
            if maintenance:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            for connection in list(self._connections):
                connection.cancel()
            await asyncio.gather(*self._connections, return_exceptions=True)
            await self.close()
            socket.unlink(missing_ok=True)


async def run_service(configuration: Path):
    service = RoomService(load_config(configuration), configuration)
    await service.run()
