"""Hindsight HTTP integration, using app-owned connection settings only."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as SchemaValidationError
from pydantic import BaseModel, ValidationError
from referencing.exceptions import Unresolvable

from eidolon import __version__

from .query import bounded_query
from .models import MemoryRecord, RecallResult, ReflectionEvidence, ReflectionResult


MAX_CAPACITY_RETRY_SECONDS = 3600


def _local_schema_references(value) -> bool:
    """Schema validation must never retrieve another document over the network."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")):
                return False
            if not _local_schema_references(child):
                return False
    elif isinstance(value, list):
        return all(_local_schema_references(child) for child in value)
    return True


def capacity_retry_delay(value: str | None, *, now: datetime | None = None) -> float:
    """Parse Retry-After, bounding malformed, past, and excessive delays."""
    delay = 1.0
    if value:
        value = value.strip()
        if value.isascii() and value.isdigit():
            # Avoid converting an untrusted, arbitrarily large integer.
            delay = float(value) if len(value) <= 10 else MAX_CAPACITY_RETRY_SECONDS
        else:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is not None:
                    delay = (retry_at - (now or datetime.now(timezone.utc))).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    return max(1.0, min(MAX_CAPACITY_RETRY_SECONDS, delay))


class MemoryUnavailable(RuntimeError):
    """Memory could not be obtained; callers must offer explicit continuation."""


class MemoryCapacityUnavailable(MemoryUnavailable):
    def __init__(self, status_code: int, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Hindsight returned HTTP {status_code}; memory capacity is unavailable")


class RecallQueryTooLong(MemoryUnavailable):
    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"Hindsight recall query exceeds its {limit}-token limit")


class HindsightMemory:
    def __init__(self, config, credential_resolver: Callable | None = None, *, client: httpx.AsyncClient | None = None,
                 recovery: Callable | None = None):
        self.config = config
        self._resolver = credential_resolver
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(base_url=config.url.rstrip("/"), timeout=getattr(config, "timeout", 20))
        self._bank_path = "/v1/default/banks/" + quote(config.bank, safe="")
        self._validated = False
        self._recovery = recovery
        self.last_error: str | None = None

    async def close(self):
        if self._owned_client:
            await self.client.aclose()

    async def _recover(self) -> bool:
        if self._recovery is None:
            return False
        try:
            return await self._recovery()
        except Exception:
            return False

    async def _request(self, method: str, path: str, *, not_found_ok: bool = False,
                       retry_safe: bool = False, retry_capacity: bool = True, **kwargs) -> dict:
        headers = {"User-Agent": f"Eidolon/{__version__}"}
        if getattr(self.config, "api_key_ref", None):
            resolver = self._resolver
            if resolver is None:
                from eidolon.config import resolve_secret
                resolver = resolve_secret
            try:
                key = resolver(self.config.api_key_ref)
                if key:
                    headers["Authorization"] = f"Bearer {key}"
            except Exception:
                raise MemoryUnavailable("Hindsight credential is unavailable; check your app credential reference") from None
        try:
            timeout = getattr(self.config, "timeout", 20)
            deadline = asyncio.get_running_loop().time() + timeout
            async with asyncio.timeout(timeout):
                for attempt in range(2):
                    try:
                        response = await self.client.request(method, path, headers=headers, **kwargs)
                    except httpx.TransportError:
                        recovered = attempt == 0 and await self._recover()
                        # A lost retain acknowledgement belongs to the durable outbox.
                        if recovered and (method == "GET" or retry_safe):
                            continue
                        raise
                    if response.status_code in {429, 503}:
                        delay = capacity_retry_delay(response.headers.get("Retry-After"))
                        remaining = deadline - asyncio.get_running_loop().time()
                        if (attempt == 0 and retry_capacity and (method == "GET" or retry_safe)
                                and delay < remaining):
                            await asyncio.sleep(delay)
                            continue
                        # Capacity is not proof that the daemon needs restarting.
                        raise MemoryCapacityUnavailable(response.status_code, delay)
                    if attempt == 0 and response.status_code in {502, 504}:
                        recovered = await self._recover()
                        if recovered and (method == "GET" or retry_safe):
                            continue
                    break
            if response.status_code == 404 and not_found_ok:
                return {"status": "not_found"}
            if response.status_code >= 400:
                if response.status_code == 400 and path.endswith("/memories/recall"):
                    # Only recognize this fixed validation message. Other backend
                    # bodies can contain prompts, credentials, or database URLs.
                    try:
                        detail = response.json().get("detail")
                    except (ValueError, AttributeError):
                        detail = None
                    if isinstance(detail, str):
                        match = re.fullmatch(
                            r"Query too long: \d+ tokens exceeds maximum of ([1-9]\d{0,5})\. Please shorten your query\.",
                            detail,
                        )
                        if match:
                            raise RecallQueryTooLong(int(match[1]))
                # Never echo response bodies: a backend may include credential-bearing URLs.
                action = ("Check the recall request and bank settings" if response.status_code == 400
                          else "Check the service and existing bank configuration")
                raise MemoryUnavailable(f"Hindsight returned HTTP {response.status_code}. {action}")
            data = response.json()
            if not isinstance(data, dict):
                raise MemoryUnavailable("Hindsight returned an invalid response")
            self.last_error = None
            return data
        except MemoryUnavailable as error:
            self.last_error = str(error)
            raise
        except (httpx.HTTPError, ValueError, TimeoutError):
            self.last_error = "Hindsight is unavailable; retry or explicitly continue without memory for this turn"
            raise MemoryUnavailable(self.last_error) from None

    async def validate_bank(self, *, retry_capacity: bool = True) -> dict:
        if not self.config.bank.strip():
            self.last_error = "External memory is unconfigured; select an existing endpoint and bank"
            raise MemoryUnavailable(self.last_error)
        # A recall API may implicitly create a bank. Always check the list first.
        data = await self._request("GET", "/v1/default/banks", retry_capacity=retry_capacity)
        banks = data.get("banks")
        if not isinstance(banks, list) or any(not isinstance(bank, dict) for bank in banks):
            self._validated = False
            self.last_error = "Hindsight bank inventory is invalid"
            raise MemoryUnavailable(self.last_error)
        bank = next((b for b in banks if b.get("bank_id") == self.config.bank), None)
        if bank is None:
            self._validated = False
            self.last_error = f"Existing Hindsight bank '{self.config.bank}' was not found. A replacement bank was not created"
            raise MemoryUnavailable(self.last_error)
        self._validated = True
        return bank

    async def recall_result(self, query: str, *, max_chars: int = 8000,
                            enabled: bool | None = None) -> RecallResult:
        if not (self.config.enabled if enabled is None else enabled):
            return RecallResult()
        # Recheck for each request so a deleted/mistyped bank is never recreated.
        await self.validate_bank()
        body = {
            "query": bounded_query(query), "budget": getattr(self.config, "recall_budget", "low"),
            "max_tokens": max(64, min(4096, max_chars // 4)),
            "query_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            response = await self._request("POST", self._bank_path + "/memories/recall",
                                           retry_safe=True, json=body)
        except RecallQueryTooLong as error:
            # Byte-level tokenizers use at most one token per UTF-8 byte. This
            # conservative fallback also works with a smaller server token cap.
            shorter = bounded_query(body["query"], error.limit)
            if not shorter.strip() or shorter == body["query"]:
                raise
            body["query"] = shorter
            response = await self._request("POST", self._bank_path + "/memories/recall",
                                           retry_safe=True, json=body)
        results = response.get("results")
        if not isinstance(results, list):
            self.last_error = "Hindsight recall response is missing its memory results"
            raise MemoryUnavailable(self.last_error)
        try:
            return RecallResult(
                records=tuple(MemoryRecord.model_validate(item)
                              for item in results[:getattr(self.config, "max_memories", 12)]),
                query=body["query"], effort=body["budget"], usage=response.get("usage"),
                chunks=response.get("chunks"), source_facts=response.get("source_facts"),
                source_facts_truncated=response.get("source_facts_truncated"),
                trace=response.get("trace"),
            )
        except ValidationError:
            self.last_error = "Hindsight recall returned invalid memory records"
            raise MemoryUnavailable(self.last_error) from None

    async def recall(self, query: str, *, max_chars: int = 8000) -> str:
        """Compatibility text interface, with whole facts and compact useful hints."""
        from .coordinator import render_record

        result = await self.recall_result(query, max_chars=max_chars)
        # The coordinator owns the prompt envelope on the generation path.
        # Never truncate a fact or reintroduce backend IDs as a budget fallback.
        records = []
        used = 0
        for record in result.records:
            rendered, _ = render_record(record)
            cost = len(rendered) + (2 if records else 0)
            if used + cost <= max_chars:
                records.append(rendered)
                used += cost
        return "\n\n".join(records)

    async def reflect_result(self, query: str, *, max_chars: int = 8000,
                             response_schema: dict | type[BaseModel] | None = None,
                             effort: str | None = None,
                             enabled: bool | None = None) -> ReflectionResult:
        if not (self.config.enabled if enabled is None else enabled):
            raise MemoryUnavailable("Memory is disabled")
        effort = effort or getattr(self.config, "reflection_budget", "low")
        if effort not in {"low", "mid", "high"}:
            raise ValueError("Reflection effort must be low, mid, or high")
        schema = (response_schema.model_json_schema()
                  if isinstance(response_schema, type) and issubclass(response_schema, BaseModel)
                  else response_schema)
        if schema is not None:
            if not isinstance(schema, dict) or not _local_schema_references(schema):
                raise ValueError("Reflection response_schema must use local schema references")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ValueError("Reflection response_schema is not a valid JSON schema") from None
        await self.validate_bank()
        body = {"query": query[:16000], "budget": effort,
                "max_tokens": max(64, min(4096, max_chars // 4)),
                "include": {"facts": {}}}
        if schema is not None:
            body["response_schema"] = schema
        response = await self._request("POST", self._bank_path + "/reflect",
                                       retry_safe=True, json=body)
        try:
            # Preserve failures as typed status for diagnostics without echoing
            # raw service errors (which may include private provider content).
            extraction_error = response.get("structured_output_error")
            if extraction_error is not None and not isinstance(extraction_error, str):
                raise ValueError
            structured = response.get("structured_output")
            if schema is not None:
                if extraction_error or not isinstance(structured, dict):
                    raise ValueError
                Draft202012Validator(schema).validate(structured)
                if isinstance(response_schema, type):
                    response_schema.model_validate(structured)
            text = response.get("text", "" if schema is not None else None)
            if not isinstance(text, str):
                raise ValueError
            return ReflectionResult(
                text=text, effort=effort, structured_output=structured,
                structured_output_error=("Structured output could not be produced"
                                         if extraction_error else None),
                based_on=ReflectionEvidence.model_validate(response.get("based_on") or {}),
                usage=response.get("usage"), trace=response.get("trace"),
            )
        except (ValidationError, SchemaValidationError, Unresolvable, ValueError):
            self.last_error = "Hindsight reflection returned an invalid structured result"
            raise MemoryUnavailable(self.last_error) from None

    async def reflect(self, query: str, *, max_chars: int = 8000) -> str:
        """Compatibility text interface; typed callers retain actual evidence."""
        result = await self.reflect_result(query, max_chars=max_chars)
        return result.text[:max_chars]

    async def retain(self, payload: dict, operation_id: str) -> dict:
        if not getattr(self.config, "retain_enabled", False) or not getattr(self.config, "verified", False):
            raise MemoryUnavailable("Retention is disabled until existing-memory verification is complete")
        await self.validate_bank(retry_capacity=False)
        # Frozen exchanges use replace, never append: replay must not duplicate text.
        item = {**payload, "update_mode": "replace"}
        return await self._request("POST", self._bank_path + "/memories", json={"items": [item], "async": True, "operation_id": operation_id})

    async def operation(self, operation_id: str) -> dict:
        return await self._request("GET", self._bank_path + "/operations/" + quote(operation_id, safe=""), not_found_ok=True, retry_capacity=False)

    async def retry_operation(self, operation_id: str) -> dict:
        return await self._request("POST", self._bank_path + "/operations/" + quote(operation_id, safe="") + "/retry")


class OutboxDispatcher:
    def __init__(self, memory: HindsightMemory, store, *, lock: asyncio.Lock | None = None):
        self.memory, self.store = memory, store
        self._lock = lock if lock is not None else asyncio.Lock()

    async def pause_delivery(self, paused: bool) -> dict:
        """Serialize acknowledgement with any already-running submission."""
        async with self._lock:
            self.store.set_memory_delivery_paused(paused)
            return self.store.outbox_status()

    async def review_legacy(self, approve: bool,
                            document_ids: list[str] | None = None) -> dict:
        async with self._lock:
            await self._poll_review_operations()
            reviewed = self.store.review_legacy_memory(approve, document_ids=document_ids)
            return {**self.store.outbox_status(), "reviewed": reviewed}

    def _defer_failure(self, entry: dict, error: MemoryUnavailable) -> None:
        # Both an unavailable transport and a healthy busy service need a
        # durable cooldown. Retain its identity and poll before any replay.
        delay = min(MAX_CAPACITY_RETRY_SECONDS, 2 ** min(entry["attempts"], 12))
        if isinstance(error, MemoryCapacityUnavailable):
            delay = min(MAX_CAPACITY_RETRY_SECONDS, max(delay, error.retry_after))
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        self.store.update_outbox(entry["document_id"], status="failed", error=str(error),
                                 attempted=True, next_retry_at=retry_at)
        self.store.defer_outbox(retry_at)

    async def _operation_status(self, entry: dict) -> str:
        operation = await self.memory.operation(entry["operation_id"])
        if operation.get("operation_id", entry["operation_id"]) != entry["operation_id"]:
            raise MemoryUnavailable("Hindsight returned a different retention operation identity")
        status = operation.get("status")
        if status not in {"completed", "pending", "processing", "failed", "cancelled", "not_found"}:
            raise MemoryUnavailable("Unknown Hindsight retention operation status")
        return status

    async def _poll_review_operations(self) -> bool:
        """Resolve uncertain legacy writes without authorizing a backend mutation."""
        for entry in self.store.pending_review_operations():
            try:
                status = await self._operation_status(entry)
                if status == "completed":
                    self.store.update_outbox(entry["document_id"], status="completed")
                elif status in {"pending", "processing"}:
                    self.store.update_outbox(entry["document_id"], status="submitted")
                else:
                    # Only after polling can the user decide whether work may
                    # be retried. Failed operations retain the same UUID.
                    self.store.update_outbox(entry["document_id"], status="queued", attempted=True)
            except MemoryUnavailable as error:
                self._defer_failure(entry, error)
                return False
        return True

    async def dispatch_once(self) -> dict:
        if self._lock.locked():
            return self.store.outbox_status()
        async with self._lock:
            if not self.memory.config.retain_enabled or not self.memory.config.verified:
                return self.store.outbox_status()
            if not await self._poll_review_operations():
                return self.store.outbox_status()
            if self.store.memory_delivery_paused():
                return self.store.outbox_status()
            for entry in self.store.pending_outbox(ready_only=True):
                document_id = entry["document_id"]
                try:
                    if entry["status"] in {"submitted", "failed"} or entry["attempts"] > 0:
                        status = await self._operation_status(entry)
                        if status == "completed":
                            self.store.update_outbox(document_id, status="completed")
                            continue
                        if status in {"pending", "processing"}:
                            self.store.update_outbox(document_id, status="submitted")
                            continue
                        if status in {"failed", "cancelled"}:
                            retried = await self.memory.retry_operation(entry["operation_id"])
                            if retried.get("operation_id", entry["operation_id"]) != entry["operation_id"]:
                                raise MemoryUnavailable("Hindsight returned a different retention operation identity")
                            self.store.update_outbox(document_id, status="submitted", attempted=True)
                            continue
                    result = await self.memory.retain(entry["payload"], entry["operation_id"])
                    returned_id = result.get("operation_id")
                    if returned_id and returned_id != entry["operation_id"]:
                        raise MemoryUnavailable("Hindsight does not preserve the configured idempotent operation identity")
                    if result.get("async") is False and result.get("success") is True:
                        self.store.update_outbox(document_id, status="completed", attempted=True)
                    elif returned_id:
                        self.store.update_outbox(document_id, status="submitted", attempted=True)
                    else:
                        raise MemoryUnavailable("Hindsight retention did not return a trackable operation")
                except MemoryUnavailable as error:
                    self._defer_failure(entry, error)
                    break
            return self.store.outbox_status()
