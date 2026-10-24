"""Bounded, restartable continuity maintenance outside conversation generation.

Only original eligible completed CHAT exchanges are input. Generated proposals
are stored directly by Store and never enter the transcript or retain outbox.
All database calls finish before an awaited provider operation starts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from uuid import uuid4

from eidolon.config import IdentityConfig, ProviderConfig
from eidolon.providers.context import ContextBudget, TokenCounter
from eidolon.providers.metrics import measured_stream
from eidolon.providers.maintenance import MaintenanceOutput, maintenance_output
from eidolon.providers.routing import AuxiliaryExecution, digest

from .continuity import (
    DEFAULT_MAX_NOTES, IMPORTANCE_RANK, MAX_NOTES, THINKING_TIMEOUT_NOTICE,
    ContinuityValidationError, Scope,
    active_items, aware_time, needs_note_metadata,
)
from .proposals import parse_proposal, task_contract
from .maintenance_budget import ResponseLimits


LEGACY_REWRITE_BATCH_NOTES = 4


def validation_failure_notice(reason: str) -> str:
    return f"Current life proposal rejected: {reason} Retry pending."


def numeric_usage(usage: Any) -> dict[str, int | float]:
    """Keep provider token counters, never arbitrary usage text or identifiers."""
    counts = {}
    if not isinstance(usage, dict):
        return counts
    token_keys = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens",
                  "output_tokens", "cached_tokens", "cache_write_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "reasoning_tokens", "audio_tokens",
                  "text_tokens", "image_tokens", "accepted_prediction_tokens",
                  "rejected_prediction_tokens"}
    detail_keys = {"prompt_tokens_details", "completion_tokens_details",
                   "input_tokens_details", "output_tokens_details"}
    for key, value in usage.items():
        details = key in detail_keys and isinstance(value, dict)
        values = value.items() if details else [(key, value)]
        for name, amount in values:
            if (name in token_keys and type(amount) in {int, float} and amount >= 0
                    and (type(amount) is int or math.isfinite(amount))):
                counts[key + "." + name if details else name] = amount
    return counts


class _ThinkingTimeout(TimeoutError):
    """The request deadline expired during reasoning before any answer text."""


async def synthetic_maintenance_test(provider: Any, execution: AuxiliaryExecution) -> dict:
    """One explicit inference through production assembly/validation, with no store.

    Fixed names, times and evidence never consult the installation's identity,
    notes, conversations or memory. Nothing is committed, even on success.
    """
    from .proposals import ORDINARY_CONTRACT

    worker = ContinuityWorker(None, provider, timeout=execution.timeout_seconds,
                              max_input_chars=execution.context_chars)
    task = {"id": "synthetic-test", "scope": "real_life", "inputs": [{
        "exchange_id": 1, "document_id": "synthetic-project", "scope": "real_life",
        "created_at": "2026-01-01T12:00:00+00:00",
        "user_created_at": "2026-01-01T12:00:00+00:00",
        "assistant_created_at": "2026-01-01T12:00:01+00:00",
        "user_content": "My creative project is unfinished. I want to return to it later.",
        "assistant_content": "We can return to the creative project later.",
        "identity": {"user_name": "You", "companion_name": "Companion"},
    }]}
    state = {"items": [], "source_exchanges": [], "revision": 0,
             "control_revision": 0, "active_count": 0}
    output = worker._output(task["id"])
    result = {"ok": False, "connected": False, "contract_compatible": False,
              "contract": ORDINARY_CONTRACT, "output_mode": "prompt",
              "capability_source": "unknown"}
    if output is not None:
        result.update(output.provenance())
    try:
        async with asyncio.timeout(execution.timeout_seconds):
            worker.counter = await asyncio.to_thread(TokenCounter, worker.model)
            await worker._propose(task, state, output=output)
        result.update(ok=True, contract_compatible=True)
        label = {"json_schema": "schema", "json_object": "JSON", "prompt": "prompt JSON"}[
            result["output_mode"]]
        result["notice"] = (f"Contract passed ({label}). Quality and enforcement untested.")
    except ContinuityValidationError as exc:
        result["reason_code"] = "validation." + exc.code
        result["notice"] = exc.public_reason
    except Exception:
        result["notice"] = ("Connected; contract failed. Check model options and output limits."
                            if worker.connected else "No provider response. Check connection, "
                            "model options and capacity.")
    result["connected"] = worker.connected
    return result


class ContinuityWorker:
    """One bounded task at a time; foreground work can cancel the network await.

    Updating identity affects later attempts; each attempt freezes its own names.
    """

    def __init__(self, store: Any, provider: Any, *, timezone: str = "UTC",
                 scope: Scope = "real_life", batch_exchanges: int = 8,
                 idle_seconds: int = 600, enabled: bool = False,
                 max_notes: int = DEFAULT_MAX_NOTES, recovery_calls: int = 6,
                 timeout: float = 60, max_input_chars: int | None = None,
                 max_output_chars: int = 24_000, execution: Any = None,
                 commit_result: Any = None, identity: IdentityConfig | None = None):
        if not 1 <= batch_exchanges <= 32 or not 1 <= idle_seconds <= 86400:
            raise ValueError("Continuity batching values are outside their supported bounds")
        if type(max_notes) is not int or not 1 <= max_notes <= MAX_NOTES:
            raise ValueError("Maximum active notes must be an integer from 1 to 64")
        if (timeout <= 0 or max_output_chars < 100
                or max_input_chars is not None and max_input_chars < 100):
            raise ValueError("Continuity resource bounds must be positive")
        self.store = store
        self.provider = provider
        self.timezone = timezone
        self.scope = scope
        self.batch_exchanges = batch_exchanges
        self.idle_seconds = idle_seconds
        if type(recovery_calls) is not int or not 1 <= recovery_calls <= 100:
            raise ValueError("Recovery calls must be an integer from 1 to 100")
        self.max_notes = max_notes
        self.recovery_calls = recovery_calls
        self.enabled = enabled
        self.timeout = timeout
        self.execution = execution
        self.commit_result = commit_result
        self.identity = identity if identity is not None else IdentityConfig()
        self._attempt_identity: dict[str, str] | None = None
        profile = execution.profile() if execution else getattr(provider, "config", None)
        self._configure_budget(profile)
        self.model = getattr(profile, "model", "")
        self.counter: TokenCounter | None = None
        self.max_output_chars = max_output_chars
        self.max_input_chars = max_input_chars
        self._active_task: asyncio.Task | None = None
        self._task_id: str | None = None
        self._last_result = "idle"
        self._attempt_id: str | None = None
        self.connected = False
        self._dispatched = False

    def _output(self, task_id: str) -> MaintenanceOutput | None:
        factory = getattr(self.provider, "maintenance_output", None)
        return factory(task_id) if factory else None

    def _configure_budget(self, profile: Any) -> None:
        self.budget = ContextBudget.for_profile(profile)
        thinking = getattr(profile, "parameters", {}).get("thinking")
        reserve = (thinking.get("budget_tokens") if isinstance(thinking, dict)
                   and thinking.get("type") != "disabled" else None)
        self.output_tokens = max(0, self.budget.output_tokens - (
            reserve if type(reserve) is int and reserve > 0 else 0))

    @property
    def status(self) -> dict:
        status = self.store.maintenance_status(scope=self.scope)
        return {**status, "running": bool(self._active_task and not self._active_task.done()),
                "enabled": self.enabled, "last_result": self._last_result,
                "batch_exchanges": self.batch_exchanges, "idle_seconds": self.idle_seconds,
                "max_notes": self.max_notes, "recovery_calls": self.recovery_calls}

    def cancel(self) -> None:
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()

    async def close(self) -> None:
        task = self._active_task
        self.cancel()
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    def _batch(self, inputs: list[dict], state: dict) -> list[dict]:
        """Keep an oldest-first prefix of unassigned complete exchanges."""
        result = []
        limit = self._response_limits("ordinary").max_operations
        for row in inputs:
            if len(result) == limit:
                break
            candidate = result + [row]
            # Task identity stays local; only evidence and state enter model input.
            task = {"id": "z-" * 18, "scope": self.scope,
                    "inputs": candidate}
            try:
                self._messages(task, state)
            except ContinuityValidationError:
                if not result:
                    raise
                break
            result.append(row)
        return result

    def _response_limits(self, task_id: str) -> ResponseLimits:
        return ResponseLimits.for_task(task_id, self.output_tokens, self.max_output_chars)

    def _messages(self, task: dict, state: dict) -> list[dict]:
        # Every active topic is mandatory context. Detailed bodies and complete
        # supporting exchanges are optional, but index-only notes cannot be targets.
        limits = self._response_limits(task["id"])
        if (not task["id"].startswith(("narrative:", "review:"))
                and len(task["inputs"]) > limits.max_operations):
            raise ContinuityValidationError("Captured batch exceeds response plan",
                                            code="output_budget")
        current = active_items(state.get("items", []), scope=self.scope)
        items = sorted(current, key=lambda item: (
            bool(item.get("locked", item.get("manual_lock"))),
            IMPORTANCE_RANK.get(item.get("importance"), 1), item.get("last_mention_at", "")
        ))
        inactive = [item for item in state.get("items", [])
                    if item not in current and not item.get("invalidated")
                    and not item.get("suppressed") and item.get("scope") == self.scope]
        items += sorted(inactive, key=lambda item: item.get("last_mention_at", ""),
                        reverse=True)[:8]
        if len(items) > 999:
            raise ContinuityValidationError("Complete topic index exceeds reference bounds",
                                            code="input_state")
        rewrite = task["id"].startswith("narrative:")
        rewrite_limit = limits.max_operations
        task_ids = {row["exchange_id"] for row in task["inputs"]}
        rewrite_targets: set[str] = set()
        if rewrite:
            for item in current:
                if ((item.get("content_format") != "narrative" or needs_note_metadata(item))
                        and not (item.get("locked", item.get("manual_lock"))
                                 or item.get("user_created") or item.get("origin") == "user_note")
                        and item.get("sources")
                        and {source["exchange_id"] for source in item["sources"]} <= task_ids):
                    rewrite_targets.add(item["id"])
                    if len(rewrite_targets) == min(LEGACY_REWRITE_BATCH_NOTES, rewrite_limit):
                        break
            items.sort(key=lambda item: item["id"] not in rewrite_targets)
        fields = ("id", "revision", "category", "topic", "importance", "metadata_pending",
                  "content", "content_format", "origin",
                  "scope", "state", "status", "observed_at",
                  "locked", "manual_lock", "user_created", "invalidated", "event_at", "expires_at",
                  "follow_up_at",
                  "last_followup_at", "promise_owner", "promise_kind")
        document = {
            "response_limits": limits.document(),
            "current_identity": (self._attempt_identity if self._attempt_identity is not None
                                 else self.identity.model_dump()),
            "scope": task["scope"], "timezone": self.timezone,
            "max_notes": self.max_notes,
            "active_count": state.get("active_count", len(current)),
            "available_slots": max(0, self.max_notes - state.get("active_count", len(current))),
            "items": [],
            "topic_index": [{"id": item["id"], "topic": item.get("topic", ""),
                             "importance": item.get("importance", "moderate"),
                             "category": item.get("category"), "state": item.get("state", "open"),
                             "locked": bool(item.get("locked", item.get("manual_lock"))),
                             "user_created": bool(item.get("user_created")
                                                  or item.get("origin") == "user_note"),
                             "protected": bool(item.get("locked", item.get("manual_lock"))
                                               or item.get("user_created")
                                               or item.get("origin") == "user_note")}
                            for item in items],
            "supporting_exchanges": [],
            "legacy_rewrite_ids": [],
            "rewrite_existing": rewrite,
            "review_existing": task["id"].startswith("review:"),
            "snapshot_revision": state["revision"],
            "control_revision": state["control_revision"],
            "exchanges": [{key: value for key, value in row.items()
                           if key not in {"user", "assistant", "id"}}
                          for row in task["inputs"]],
        }
        error = task.get("stages", {}).get("continuity", {}).get("error")
        # Only application-owned diagnostic text may enter a retry request.
        for reason in (*ContinuityValidationError.REASONS.values(),
                       *ContinuityValidationError.DETAILS):
            if error == validation_failure_notice(reason):
                document["previous_rejection"] = reason
                break

        def messages() -> list[dict]:
            _, instructions, _, request_type = task_contract(task["id"])
            payload = request_type(document, inputs=task["inputs"], state=state,
                                   scope=self.scope, timezone=self.timezone,
                                   max_notes=self.max_notes).document
            return [{"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

        if self.counter is None:
            self.counter = TokenCounter(self.model)

        def fits() -> bool:
            assembled = messages()
            output = self._output(task["id"])
            response_format = output.response_format if output else None
            if self.max_input_chars is not None:
                chars = sum(len(json.dumps(message, ensure_ascii=False)) for message in assembled)
                if response_format:
                    chars += len(json.dumps(response_format, ensure_ascii=False))
                if chars > self.max_input_chars:
                    return False
            return self.budget.fits(self.counter.request(
                assembled, response_format=response_format))

        exchanges = document["exchanges"]
        index = document["topic_index"]
        document["exchanges"] = []
        document["topic_index"] = []
        if not fits():
            raise ContinuityValidationError("Instructions and reserves exceed settings",
                                            code="input_overhead")
        document["topic_index"] = index
        if not fits():
            raise ContinuityValidationError("Complete topic index exceeds settings",
                                            code="input_state")
        document["exchanges"] = exchanges
        if not fits():
            raise ContinuityValidationError("The complete source batch exceeds the input allowance",
                                            code="input_limit")
        evidence = {row["exchange_id"]: row for row in state.get("source_exchanges", [])}
        evidence.update({row["exchange_id"]: row for row in task["inputs"]})
        for item in items:
            entry = {key: item[key] for key in fields if key in item}
            if item.get("content_format") != "narrative" and item.get("sources"):
                # A legacy note's content is already present in its originals.
                entry.pop("content", None)
            entry["sources"] = []
            supporting = document["supporting_exchanges"]
            supplied_ids = task_ids | {row["exchange_id"] for row in supporting}
            required_ids = {source["exchange_id"] for source in item.get("sources", [])}
            if not task["id"].startswith("review:") and not required_ids <= evidence.keys():
                continue
            extra = [evidence[key] for key in sorted(required_ids - supplied_ids)
                     if key in evidence and not task["id"].startswith("review:")]
            document["supporting_exchanges"] = supporting + [
                {key: value for key, value in row.items() if key not in {"user", "assistant", "id"}}
                for row in extra]
            for source in item.get("sources", []):
                support = {key: source[key] for key in
                           ("exchange_id", "document_id", "role", "origin")}
                if source["exchange_id"] not in task_ids:
                    support["excerpt"] = source["excerpt"]
                if row := evidence.get(source["exchange_id"]):
                    support["timestamp"] = row.get(source["role"] + "_created_at", row["created_at"])
                    if "identity" in row:
                        support["identity"] = row["identity"]
                entry["sources"].append(support)
            document["items"].append(entry)
            convert = (item in current and (item.get("content_format") != "narrative"
                                           or needs_note_metadata(item))
                       and (not rewrite or item["id"] in rewrite_targets)
                       and item.get("origin") != "user_note"
                       and not item.get("user_created")
                       and not item.get("locked", item.get("manual_lock"))
                       and bool(item.get("sources")))
            if convert:
                document["legacy_rewrite_ids"].append(item["id"])
            try:
                detail_fits = fits()
            except ContinuityValidationError as exc:
                # Complete background adds both speakers' references. A full
                # optional reference table leaves this note index-only too.
                if exc.code != "input_limit":
                    raise
                detail_fits = False
            if not detail_fits:
                document["items"].pop()
                document["supporting_exchanges"] = supporting
                if convert:
                    document["legacy_rewrite_ids"].pop()
                if item["id"] in rewrite_targets:
                    raise ContinuityValidationError("Required rewrite details exceed settings",
                                                    code="input_state")
        return messages()

    def _source_exchanges(self, state: dict) -> list[dict]:
        ids = list(dict.fromkeys(source["exchange_id"] for item in state.get("items", [])
                                 if not item.get("invalidated")
                                 for source in item.get("sources", [])))
        return [row for start in range(0, len(ids), 32)
                for row in self.store.continuity_evidence(ids[start:start + 32], scope=self.scope)]

    def _legacy_inputs(self, state: dict) -> list[dict]:
        """Convert existing extractive notes even when there are no new exchanges."""
        evidence = {row["exchange_id"]: row for row in state["source_exchanges"]}
        ids: set[int] = set()
        notes = 0
        for item in active_items(state["items"], scope=self.scope):
            if (item.get("content_format") == "narrative" and not needs_note_metadata(item)
                    or item.get("locked")
                    or item.get("user_created") or item.get("origin") == "user_note"):
                continue
            candidate = ids | {source["exchange_id"] for source in item.get("sources", [])}
            if len(candidate) > 32 or not candidate <= evidence.keys():
                continue
            rows = [evidence[key] for key in sorted(candidate)]
            try:
                fits = bool(rows)
                if rows:
                    self._messages({"id": "narrative:fit", "scope": self.scope, "inputs": rows}, state)
            except ContinuityValidationError:
                fits = False
            if fits:
                ids = candidate
                notes += 1
                if notes == min(LEGACY_REWRITE_BATCH_NOTES,
                                self._response_limits("narrative:fit").max_operations):
                    break
            elif not ids:
                # Persist the complete oldest note's sources as a held rewrite,
                # so a smaller model cannot silently skip an oversized note.
                return rows
        return [evidence[key] for key in sorted(ids)]

    async def _propose(self, task: dict, state: dict, *,
                       output: MaintenanceOutput | None = None,
                       on_dispatch: Callable[[], None] | None = None) -> list[dict]:
        chunks: list[str] = []
        size = 0
        finished = False
        reasoning_seen = False
        messages = self._messages(task, state)
        _, _, _, request_type = task_contract(task["id"])
        request = request_type(json.loads(messages[-1]["content"]), inputs=task["inputs"],
                               state=state, scope=self.scope, timezone=self.timezone,
                               max_notes=self.max_notes)
        record = getattr(self.store, "record_request_metrics", None)
        output = output or self._output(task["id"])
        options = {"output": output} if output is not None else {}

        def record_metrics(**values: Any) -> None:
            if record and (self._attempt_id is not None or self.execution is None):
                record(role="auxiliary", provider=getattr(
                    getattr(self.provider, "config", None), "name", "unknown"),
                    model=self.model, task_id=task["id"], attempt_id=self._attempt_id, **values)

        # Production adapters announce dispatch after payload/credential preparation.
        # Scripted providers without that event begin at stream consumption.
        if on_dispatch and not getattr(self.provider, "reports_dispatch", False):
            on_dispatch()
        try:
            async with (
                asyncio.timeout(self.timeout),
                aclosing(measured_stream(
                    self.provider.stream(messages, tools=None, presentation="plain", **options),
                    record=record_metrics if record else None,
                )) as stream,
            ):
                async for event in stream:
                    if event.get("type") == "request" and on_dispatch:
                        on_dispatch()
                    if event.get("type") in {"response", "token", "progress", "usage", "finish"}:
                        self.connected = True
                    if event.get("type") == "progress" and event.get("phase") == "reasoning":
                        reasoning_seen = True
                    elif event.get("type") == "token":
                        token = event.get("text")
                        if not isinstance(token, str):
                            raise ContinuityValidationError("The maintenance output is not text")
                        size += len(token)
                        if size > self.max_output_chars:
                            raise ContinuityValidationError(
                                "The maintenance output exceeds its allowance", code="output_limit")
                        chunks.append(token)
                    elif event.get("type") == "tool_call":
                        raise ContinuityValidationError("Maintenance cannot call tools")
                    elif event.get("type") == "usage":
                        counts = numeric_usage(event.get("usage"))
                        if counts and self.store is not None:
                            self.store.record_maintenance_usage(
                                task["id"], "continuity", usage=counts,
                                attempt_id=self._attempt_id)
                    elif event.get("type") == "finish":
                        if event.get("reason") != "stop":
                            raise ContinuityValidationError("The maintenance output is incomplete",
                                                            code="output_incomplete")
                        finished = True
        except TimeoutError:
            if reasoning_seen and not size:
                raise _ThinkingTimeout from None
            raise
        if not finished:
            raise ContinuityValidationError("The maintenance stream did not finish cleanly",
                                            code="output_incomplete")
        proposal = parse_proposal("".join(chunks))
        limits = self._response_limits(task["id"])
        if self.counter.value(proposal) > limits.response_tokens:
            raise ContinuityValidationError("Response exceeds planned tokens", code="output_plan")
        return request.compile(proposal)

    def _start_attempt(self, task: dict, output: MaintenanceOutput | None) -> None:
        if self._dispatched:
            return
        if self.execution is not None:
            provenance = self.execution.provenance()
            original_output = {key: provenance.get(key) for key in
                               ("contract", "output_mode", "capability_source",
                                "provider_schema_hash")}
            if output is not None:
                provenance.update(output.provenance())
            contract, instructions, schema, _ = task_contract(task["id"])
            provenance["contract"] = contract
            provenance["prompt_hash"] = hashlib.sha256(instructions.encode()).hexdigest()
            provenance["schema_hash"] = digest({
                "contract": contract, "schema": schema.model_json_schema(),
            })
            if (task["id"].startswith(("narrative:", "review:"))
                    or any(provenance[key] != value for key, value in original_output.items())):
                provenance["execution_fingerprint"] = digest({
                    "execution": provenance["execution_fingerprint"],
                    "prompt": provenance["prompt_hash"], "schema": provenance["schema_hash"],
                    "output": output.provenance() if output else original_output,
                })
            self._attempt_id = self.store.start_maintenance_attempt(
                task["id"], provenance,
            )
        else:
            self.store.record_maintenance_usage(task["id"], "continuity", called=True)
        self._dispatched = True

    def _recovery_fingerprint(self, reason_code: str | None = None) -> str:
        # Effective inference settings exclude inventory, UI, enable/Off and batching.
        # A route revision alone is not a relevant configuration change.
        profile = self.execution.profile() if self.execution else getattr(
            self.provider, "config", None)
        settings = {"connection": {
            key: getattr(profile, key, None) for key in ("endpoint", "headers", "api_key_ref")
        }}
        if reason_code != "authentication":
            output = maintenance_output(profile, "ordinary") if isinstance(
                profile, ProviderConfig) else None
            settings.update(model=getattr(profile, "model", None),
                            parameters=getattr(profile, "parameters", {}),
                            response_format=output.response_format if output else None)
        if reason_code and reason_code.startswith("validation."):
            settings.update(input_chars=self.max_input_chars, timeout=self.timeout,
                            output_chars=self.max_output_chars,
                            context_window=self.budget.window,
                            timezone=self.timezone, identity=self.identity.model_dump())
            if reason_code == "validation.input_state":
                settings["max_notes"] = self.max_notes
        return digest(settings)

    def _failed(self, task: dict, *, reason: str, reason_code: str,
                immediate: bool = False) -> None:
        stage = task.get("stages", {}).get("continuity", {})
        attempts = stage.get("attempts", 0)
        delay = 0 if immediate else min(3600, 30 * 2 ** min(attempts, 7))
        from .diagnostics import CONFIGURATION_REASONS
        paused = reason_code in CONFIGURATION_REASONS
        self.store.prepare_maintenance_recovery(
            task["id"], call_limit=self.recovery_calls,
            fingerprint=self._recovery_fingerprint(reason_code))
        saved = self.store.get_maintenance_stage(task["id"])
        retry = (None if paused or saved["budget"]["exhausted"]
                 or reason_code == "validation.evidence_changed" else
                 (datetime.now(dt_timezone.utc) + timedelta(seconds=delay)).isoformat())
        self.store.fail_maintenance_stage(task["id"], "continuity", reason,
                                          next_retry_at=retry, reason_code=reason_code,
                                          pause_fingerprint=self._recovery_fingerprint(reason_code)
                                          if paused else None)
        self._last_result = reason

    async def run_once(self, *, force: bool = False, retry_task_id: str | None = None) -> bool:
        """Commit at most one batch; retain gaps separately from newer progress.

        Manual force bypasses activation, batching, and retry delay but cannot
        bypass evidence, eligibility, locks, or revision validation.
        """
        if self._active_task is not None or not (self.enabled or force):
            return False
        self._active_task = asyncio.current_task()
        task = None
        outcome = "failed"
        reason_code = None
        try:
            # Capture before tokenizer initialization can yield. Budgeting and
            # inference share these names; saved evidence keeps its own attribution.
            self._attempt_identity = self.identity.model_dump()
            profile = self.execution.profile() if self.execution else getattr(
                self.provider, "config", None)
            self._configure_budget(profile)
            if self.counter is None:
                self.counter = await asyncio.to_thread(TokenCounter, self.model)
            state = self.store.continuity_state(scope=self.scope, limit=None, maintenance=True)
            state["source_exchanges"] = self._source_exchanges(state)
            task = (self.store.get_maintenance_task(retry_task_id) if retry_task_id else
                    self.store.pending_maintenance_task(scope=self.scope, stage="continuity",
                                                        include_retained=force))
            # Recheck configuration-paused work after relevant settings change.
            if task is None and not force:
                for retained in self.store.retained_maintenance_tasks(scope=self.scope):
                    if self.store.prepare_maintenance_recovery(
                        retained["id"], call_limit=self.recovery_calls,
                        fingerprint=self._recovery_fingerprint(
                            (retained["stages"]["continuity"].get("reason") or {}).get("code")),
                    ):
                        task = retained
                        break
            now = datetime.now(dt_timezone.utc)
            if task:
                if not self.store.prepare_maintenance_recovery(
                    task["id"], call_limit=self.recovery_calls,
                    fingerprint=self._recovery_fingerprint(
                        (task["stages"]["continuity"].get("reason") or {}).get("code")),
                    retry=force,
                ):
                    self._last_result = "recovery paused or exhausted"
                    return False
                stage = task.get("stages", {}).get("continuity", {})
                retry = stage.get("next_retry_at")
                if retry and aware_time(retry) > now and not force:
                    self._last_result = "waiting for retry"
                    return False
                # Re-snapshot after an edit before asking the model again. The
                # fixed input identity/hash is preserved across every retry.
                task = self.store.create_maintenance_task(
                    task["inputs"], scope=self.scope, expected_revision=state["revision"],
                    control_revision=state["control_revision"],
                    rewrite=task["id"].startswith("narrative:"),
                    rewrite_task_id=task["id"] if task["id"].startswith("narrative:") else None,
                    review=task["id"].startswith("review:"),
                    review_task_id=task["id"] if task["id"].startswith("review:") else None,
                    review_key=str(self.max_notes),
                    resume_task_id=task["id"],
                )
            else:
                inputs = self.store.maintenance_inputs(limit=self.batch_exchanges, scope=self.scope)
                current = active_items(state["items"], scope=self.scope)
                review = (not inputs and state.get("active_count", len(current)) > self.max_notes
                          and any(not (item.get("locked") or item.get("user_created")
                                       or item.get("invalidated") or item.get("suppressed"))
                                  for item in current))
                rewrite = not inputs and not review
                if rewrite:
                    inputs = self._legacy_inputs(state)
                if not inputs and not review:
                    self._last_result = "idle"
                    return False
                latest = max((aware_time(row.get("completed_at", row["created_at"]))
                              for row in inputs), default=now)
                if (not rewrite and not review and not force and len(inputs) < self.batch_exchanges
                        and (now - latest).total_seconds() < self.idle_seconds):
                    self._last_result = "coalescing eligible exchanges"
                    return False
                # If a single source cannot fit, persist it for explicit retry;
                # keep its originals unresolved while allowing newer batches to progress.
                try:
                    bounded = inputs if rewrite or review else self._batch(inputs, state)
                except ContinuityValidationError:
                    task = self.store.create_maintenance_task(
                        inputs[:1], scope=self.scope, expected_revision=state["revision"],
                        control_revision=state["control_revision"],
                        rewrite=rewrite,
                        review=review, review_key=str(self.max_notes),
                    )
                    raise
                task = self.store.create_maintenance_task(
                    bounded, scope=self.scope, expected_revision=state["revision"],
                    control_revision=state["control_revision"],
                    rewrite=rewrite,
                    review=review, review_key=str(self.max_notes),
                )
            if task["stages"]["continuity"]["status"] == "completed":
                # A no-change legacy rewrite leaves both the notes and scope
                # revision intact, so its deterministic task ID remains the same.
                # Completed work must stop here, before any paid provider call.
                if not (force and task["id"].startswith(("narrative:", "review:"))):
                    self._last_result = "already completed"
                    return False
                # An explicit refresh may reconsider unchanged legacy notes.
                # A pending retry keeps its original identity via the branch above.
                review = task["id"].startswith("review:")
                task = self.store.create_maintenance_task(
                    task["inputs"], scope=self.scope, expected_revision=state["revision"],
                    control_revision=state["control_revision"], rewrite=not review,
                    rewrite_task_id="narrative:" + str(uuid4()) if not review else None,
                    review=review, review_key=str(self.max_notes),
                    review_task_id="review:" + str(uuid4()) if review else None,
                )
            if not self.store.prepare_maintenance_recovery(
                task["id"], call_limit=self.recovery_calls,
                fingerprint=self._recovery_fingerprint(
                    (task["stages"]["continuity"].get("reason") or {}).get("code")),
            ):
                self._last_result = "recovery paused or exhausted"
                return False
            self._task_id = task["id"]
            # The task may be from an older worker configuration. Enforce the
            # allowance again without truncating its immutable evidence set.
            self._messages(task, state)
            claim = getattr(self.store, "claim_maintenance_stage", None)
            if claim and not claim(task["id"], "continuity"):
                self._last_result = "already claimed"
                return False
            output = self._output(task["id"])
            operations = await self._propose(
                task, state, output=output, on_dispatch=lambda: self._start_attempt(task, output))
            if self.commit_result is not None:
                await self.commit_result(self.execution, self._attempt_id, task, state, operations)
            else:
                self.store.commit_continuity(task["id"], operations,
                                             expected_revision=state["revision"],
                                             control_revision=state["control_revision"],
                                             attempt_id=self._attempt_id, max_notes=self.max_notes)
            outcome = "committed"
            self._last_result = "committed"
            return True
        except asyncio.CancelledError:
            outcome = "interrupted"
            reason_code = "interrupted"
            if task:
                self._failed(task, reason="Auxiliary request interrupted; retry pending",
                             reason_code=reason_code, immediate=True)
            raise
        except ContinuityValidationError as exc:
            reason_code = "validation." + exc.code
            if task:
                self._failed(task, reason=validation_failure_notice(exc.public_reason),
                             reason_code=reason_code)
            return False
        except TimeoutError as exc:
            reason_code = "thinking_timeout" if isinstance(exc, _ThinkingTimeout) else "timeout"
            if task:
                self._failed(task, reason=THINKING_TIMEOUT_NOTICE if isinstance(exc, _ThinkingTimeout)
                             else "Maintenance provider timed out; retry pending",
                             reason_code=reason_code)
            return False
        except Exception as exc:
            from eidolon.providers.openai import (
                ProviderCapabilityError, ProviderError, ProviderOutputLimitError,
            )
            if task:
                reason_code = "unknown"
                reason = "Maintenance unavailable or state changed; retry pending"
                if isinstance(exc, ProviderCapabilityError):
                    outcome = "configuration_error"
                    reason_code = "configuration"
                    reason = ("Auxiliary request rejected; check the model ID, output-token field "
                              "and reasoning effort in Preferences. Retry pending.")
                elif isinstance(exc, ProviderOutputLimitError):
                    reason_code = "validation.output_tokens"
                    reason = validation_failure_notice(
                        ContinuityValidationError.REASONS["output_tokens"])
                elif isinstance(exc, ProviderError):
                    reason_code = exc.category
                    reason = ("Auxiliary provider unavailable; check its connection, credentials "
                              "and model ID. Retry pending.")
                elif self.execution is not None and self.store.auxiliary_route()["revision"] != (
                    self.execution.routing_revision
                ):
                    outcome = "stale"
                    reason_code = "stale"
                    reason = "Auxiliary routing changed; retry pending"
                self._failed(task, reason=reason, reason_code=reason_code,
                             immediate=outcome == "stale")
            return False
        finally:
            if self._attempt_id is not None:
                self.store.finish_maintenance_attempt(self._attempt_id, outcome,
                                                      reason_code=reason_code)
            self._attempt_id = None
            self._dispatched = False
            self._attempt_identity = None
            self._active_task = None
            self._task_id = None
