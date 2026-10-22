"""Nonblocking cosmetic cues and the Vessel's existing signed lifecycle wire."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone

import httpx

from eidolon.config import resolve_secret


class AvatarCoordinator:
    def __init__(
        self, config, mcp, *, client: httpx.AsyncClient | None = None, cue_ttl: float = 3.0
    ):
        self.config, self.mcp = config, mcp
        self.client = client or httpx.AsyncClient(timeout=2, trust_env=False)
        self.owns_client = client is None
        self.cue_ttl = cue_ttl
        self._catalog = {"expressions": [], "gestures": []}
        self._catalog_at: float | None = None
        self._catalog_generation = None
        self._catalog_lock = asyncio.Lock()
        self._current: tuple | None = None
        self._sequence = 0
        self._seen = OrderedDict()
        self._started = OrderedDict()
        self._ended = OrderedDict()
        self._queues = {kind: asyncio.Queue(maxsize=8) for kind in ("expression", "gesture")}
        self._lifecycle = asyncio.Queue(maxsize=64)
        self._tasks: list[asyncio.Task] = []
        self.diagnostics = deque(maxlen=100)

    def _diagnostic(self, kind: str, state: str, **metadata):
        self.diagnostics.append({"kind": kind, "state": state, "time": time.time(), **metadata})

    @staticmethod
    def _remember(store, key):
        store[key] = True
        store.move_to_end(key)
        while len(store) > 2048:
            store.popitem(last=False)

    async def start(self):
        if not self.config.enabled or self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._cue_worker(kind), name=f"avatar-{kind}")
            for kind in self._queues
        ]
        self._tasks += [
            asyncio.create_task(self._lifecycle_worker(), name="avatar-lifecycle"),
            asyncio.create_task(self._refresh_loop(), name="avatar-catalog"),
        ]

    async def close(self):
        if self._current:
            session, turn, attempt = self._current
            await self.end_turn(session, turn, attempt)
        if self._tasks:
            try:
                await asyncio.wait_for(self._lifecycle.join(), 2.5)
            except TimeoutError:
                pass
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.owns_client:
            await self.client.aclose()

    async def catalog(self, *, refresh: bool = False) -> dict:
        if not self.config.enabled:
            return {"expressions": [], "gestures": []}
        generation = (
            self.mcp.generation(self.config.server) if hasattr(self.mcp, "generation") else None
        )
        if generation != self._catalog_generation:
            refresh = True
        if (not refresh and self._catalog_at is not None
                and time.monotonic() - self._catalog_at < self.config.catalog_ttl):
            return {key: list(value) for key, value in self._catalog.items()}
        async with self._catalog_lock:
            if (not refresh and self._catalog_at is not None
                    and time.monotonic() - self._catalog_at < self.config.catalog_ttl):
                return {key: list(value) for key, value in self._catalog.items()}
            try:
                result = await self.mcp.call(self.config.server, "list_emotes", {})
                payload = result.get("structured")
                if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                    payload = payload["result"]
                if not isinstance(payload, dict):
                    texts = result.get("content", [])
                    payload = (
                        json.loads("\n".join(item.get("text", "") for item in texts))
                        if texts
                        else result
                    )
                if "expressions" not in payload or "emotes" not in payload:
                    raise ValueError("Body catalog is unavailable")
                expressions, gestures = payload.get("expressions", []), payload.get("emotes", {})
                if not isinstance(expressions, (list, dict)) or not isinstance(
                    gestures, (list, dict)
                ):
                    raise ValueError("Invalid body catalog")
                self._catalog = {
                    "expressions": [name for name in expressions if isinstance(name, str)],
                    "gestures": [name for name in gestures if isinstance(name, str)],
                }
                self._catalog_at = time.monotonic()
                self._catalog_generation = (
                    self.mcp.generation(self.config.server)
                    if hasattr(self.mcp, "generation")
                    else None
                )
            except Exception as exc:
                self._diagnostic("catalog", "unavailable", error=type(exc).__name__)
                # An offline/stale catalog does not authorize gestures after reconnect.
                self._catalog = {"expressions": [], "gestures": []}
                self._catalog_at = None
        return {key: list(value) for key, value in self._catalog.items()}

    async def _refresh_loop(self):
        retry_delay = 1.0
        while True:
            await self.catalog(refresh=True)
            if self._catalog_at is not None:
                retry_delay = 1.0
                await asyncio.sleep(self.config.catalog_ttl)
            else:
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 2)

    async def begin_turn(self, session_id: str, turn_id: str, attempt_id: str):
        if not self.config.enabled:
            return
        await self.start()
        self._current = (session_id, turn_id, attempt_id)
        self._drop_pending()
        key = (session_id, turn_id)
        if key not in self._started:
            self._remember(self._started, key)
            self._event("pre_llm_call", session_id, turn_id)

    def _drop_pending(self):
        for queue in self._queues.values():
            while not queue.empty():
                item = queue.get_nowait()
                queue.task_done()
                self._diagnostic(item[0], "dropped", name=item[1])

    def enqueue(
        self,
        kind: str,
        name: str,
        session_id: str,
        turn_id: str,
        attempt_id: str,
        *,
        arguments: dict | None = None,
    ) -> bool:
        if not self.config.enabled or kind not in self._queues:
            return False
        current = (session_id, turn_id, attempt_id)
        if current != self._current:
            return False
        vocabulary = self._catalog["expressions" if kind == "expression" else "gestures"]
        if name not in vocabulary:
            self._diagnostic(kind, "unknown_name", name=name)
            self._catalog_at = None
            return False
        # Shared with explicit cosmetic tool calls, including retries of this turn.
        key = (session_id, turn_id, kind)
        if key in self._seen:
            return False
        self._sequence += 1
        try:
            self._queues[kind].put_nowait(
                (
                    kind,
                    name,
                    current,
                    time.monotonic(),
                    self._sequence,
                    dict(arguments) if arguments else None,
                )
            )
        except asyncio.QueueFull:
            self._diagnostic(kind, "queue_full", name=name)
            return False
        self._remember(self._seen, key)
        return True

    async def explicit(
        self, kind: str, name: str, context: dict, *, arguments: dict | None = None
    ) -> dict:
        accepted = self.enqueue(
            kind,
            name,
            context.get("session_id", ""),
            context.get("turn_id", ""),
            context.get("attempt_id", context.get("generation_id", "")),
            arguments=arguments,
        )
        return {
            "queued": accepted,
            "detail": "Body cue queued"
            if accepted
            else "Body cue unavailable, invalid, or already used for this turn",
        }

    async def _cue_worker(self, kind):
        queue = self._queues[kind]
        while True:
            _, name, current, queued, sequence, overrides = await queue.get()
            identity = {
                "session_id": current[0],
                "turn_id": current[1],
                "attempt_id": current[2],
                "sequence": sequence,
            }
            try:
                if current != self._current or time.monotonic() - queued > self.cue_ttl:
                    self._diagnostic(kind, "expired", name=name, **identity)
                    continue
                tool = "set_expression" if kind == "expression" else "play_emote"
                arguments = {"name": name} if kind == "expression" else {"emote": name}
                if overrides:
                    arguments.update(overrides)
                self._diagnostic(kind, "sent", name=name, **identity)
                result = await self.mcp.call(self.config.server, tool, arguments)
                raw = json.dumps(result, ensure_ascii=False)
                self._diagnostic(kind, "acknowledged", name=name, result=raw[:2000], **identity)
                if "unknown" in raw.lower():
                    self._catalog_at = None
                    # Refresh authorizes later turns; the rejected command is never replayed.
                    await self.catalog(refresh=True)
            except Exception as exc:
                self._diagnostic(
                    kind, "unknown_outcome", name=name, error=type(exc).__name__, **identity
                )
            finally:
                queue.task_done()

    async def end_turn(self, session_id: str, turn_id: str, attempt_id: str):
        key = (session_id, turn_id)
        if self._current == (session_id, turn_id, attempt_id):
            self._current = None
            self._drop_pending()
        if self.config.enabled and key in self._started and key not in self._ended:
            self._remember(self._ended, key)
            self._event("post_llm_call", session_id, turn_id)

    async def cancel_turn(self, session_id: str, turn_id: str, attempt_id: str):
        await self.end_turn(session_id, turn_id, attempt_id)

    async def session_end(self, session_id: str):
        if self.config.enabled:
            self._event("on_session_end", session_id, "")

    def _event(self, event: str, session_id: str, turn_id: str):
        payload = {
            "hook_event_name": event,
            "profile": self.config.profile,
            "session_id": session_id,
            "extra": {"turn_id": turn_id},
            "delivery_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._lifecycle.put_nowait(payload)
        except asyncio.QueueFull:
            self._diagnostic("lifecycle", "queue_full", event=event)

    async def _lifecycle_worker(self):
        while True:
            payload = await self._lifecycle.get()
            try:
                secret = resolve_secret(self.config.signing_key_ref)
                if not secret:
                    self._diagnostic("lifecycle", "unconfigured", event=payload["hook_event_name"])
                    continue
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                response = await self.client.post(
                    self.config.lifecycle_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hermes-Signature-256": signature,
                    },
                )
                response.raise_for_status()
                self._diagnostic("lifecycle", "delivered", event=payload["hook_event_name"])
            except Exception as exc:
                self._diagnostic(
                    "lifecycle",
                    "unavailable",
                    event=payload["hook_event_name"],
                    error=type(exc).__name__,
                )
            finally:
                self._lifecycle.task_done()

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "catalog": self._catalog,
            "activity": list(self.diagnostics)[-10:],
        }
