import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from eidolon.config import IdentityConfig, MemoryConfig
from eidolon.memory import HindsightMemory, MemoryUnavailable, OutboxDispatcher
from eidolon.memory.hindsight import MemoryCapacityUnavailable, capacity_retry_delay
from eidolon.memory.query import compose_recall_query
from eidolon.storage import Store


@pytest.mark.asyncio
async def test_missing_bank_fails_without_creation_or_recall():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"banks": [{"bank_id": "real"}]})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="mistyped"), client=client)
        with pytest.raises(MemoryUnavailable, match="not found"):
            await memory.recall("hi")
    assert [r.method for r in requests] == ["GET"]


@pytest.mark.asyncio
async def test_recall_is_bounded_unfiltered_and_outage_is_explicit():
    bodies = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"id": "legacy", "text": "Old shared history"}, {"id": "new", "text": "Recent memory"}]})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client)
        result = await memory.recall("Alex", max_chars=30)
        assert len(result) <= 30 and "Old shared history" in result
        assert "legacy" not in result
        assert "tags" not in bodies[0]
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(MemoryUnavailable, match="explicitly continue"):
            await HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client).recall("anything")


@pytest.mark.asyncio
async def test_outbox_tracks_async_completion_and_recovers_uncertain_submission(tmp_path):
    store = Store(tmp_path / "room.db")
    store.complete_turn(store.session_for()["id"], "Hi", "Hello", generation_id="stable")
    operation_id = store.pending_outbox()[0]["operation_id"]
    calls = []
    status = "not_found"
    failed_once = False
    def handler(request):
        nonlocal status, failed_once
        calls.append(request)
        if request.url.path == "/v1/default/banks":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        if request.url.path.endswith("/memories"):
            body = json.loads(request.content)
            assert body["operation_id"] == operation_id
            assert body["items"][0]["update_mode"] == "replace"
            status = "processing"
            if not failed_once:
                failed_once = True
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return httpx.Response(200, json={"async": True, "operation_id": operation_id})
        return httpx.Response(200, json={"status": status, "operation_id": operation_id})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client)
        await OutboxDispatcher(memory, store).dispatch_once()
        assert store.pending_outbox()[0]["status"] == "failed"
        # Transport failures defer delivery durably; resolve only when due.
        request_count = len(calls)
        await OutboxDispatcher(memory, store).dispatch_once()
        assert len(calls) == request_count
        store.update_outbox(store.pending_outbox()[0]["document_id"], status="failed")
        await OutboxDispatcher(memory, store).dispatch_once()
        assert store.pending_outbox()[0]["status"] == "submitted"
        assert len([r for r in calls if r.url.path.endswith("/memories")]) == 1
        status = "completed"
        await OutboxDispatcher(memory, store).dispatch_once()
        assert store.pending_outbox() == []
    store.close()


@pytest.mark.asyncio
async def test_unverified_retention_stays_durable_without_network(tmp_path):
    store = Store(tmp_path / "room.db")
    store.complete_turn(store.session_for()["id"], "Hi", "Hello")
    def handler(request):
        raise AssertionError("unverified retention must not call service")
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client)
        await OutboxDispatcher(memory, store).dispatch_once()
        assert store.outbox_status()["pending"] == 1
    store.close()


@pytest.mark.asyncio
async def test_failure_before_submission_retries_when_operation_returns_404(tmp_path):
    store = Store(tmp_path / "room.db")
    store.complete_turn(store.session_for()["id"], "Hi", "Hello")
    entry = store.pending_outbox()[0]
    attempts = 0
    def handler(request):
        nonlocal attempts
        if request.url.path == "/v1/default/banks":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        if "/operations/" in request.url.path:
            return httpx.Response(404, json={"detail": "not found"})
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("offline before submission", request=request)
        return httpx.Response(200, json={"async": True, "operation_id": entry["operation_id"]})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        dispatcher = OutboxDispatcher(HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client), store)
        await dispatcher.dispatch_once()
        assert store.pending_outbox()[0]["status"] == "failed"
        store.update_outbox(entry["document_id"], status="failed")
        await dispatcher.dispatch_once()
        assert store.pending_outbox()[0]["status"] == "submitted"
        assert attempts == 2
    store.close()


def test_recall_query_keeps_latest_message_and_bounds_multilingual_history():
    history = [{"role": "assistant", "content": "older response 🧠 東京 " * 1000}]
    query = compose_recall_query(history, "Where did we leave off?")
    assert query.startswith("You: Where did we leave off?")
    assert len(query.encode("utf-8")) <= 800
    assert "�" not in query
    long_message = "東京🧠" * 1000
    query = compose_recall_query(history, long_message)
    assert query.startswith("You: 東京🧠")
    assert "older response" not in query
    assert len(query.encode("utf-8")) <= 800
    assert "�" not in query


def test_recall_query_uses_recorded_names_and_current_fallback_without_changing_history():
    identity = IdentityConfig(user_name="Mira", companion_name="Orion")
    recorded = {"user_name": "Ari", "companion_name": "Vega"}
    history = [
        {"role": "user", "content": "An earlier question", "identity": recorded},
        {"role": "assistant", "content": "An earlier answer", "identity": recorded},
        {"role": "user", "content": "An older record without names"},
        {"role": "assistant", "content": "Its reply"},
        {"role": "tool", "content": "A tool result"},
    ]
    original = copy.deepcopy(history)

    query = compose_recall_query(history, "A new question", identity=identity)

    assert query == (
        "Mira: A new question\nRecent conversation:\n"
        "Ari: An earlier question\nVega: An earlier answer\n"
        "Mira: An older record without names\nOrion: Its reply\ntool: A tool result"
    )
    assert history == original


@pytest.mark.parametrize("message_length", [0, 1, 25, 80, 150, 500])
def test_recall_byte_budget_preserves_full_names_and_valid_unicode(message_length):
    identity = IdentityConfig(user_name="🧠" * 80, companion_name="🌌" * 80)
    history = [{"role": role, "content": "東京👩‍💻" * 200}
               for role in ("user", "assistant")]

    query = compose_recall_query(history, "🪴" * message_length, identity=identity)

    assert query.startswith(identity.user_name + ": ")
    assert len(query.encode("utf-8")) <= 800
    assert "�" not in query
    separator = "\nRecent conversation:\n"
    if separator in query:
        recent = query.split(separator)[1].splitlines()
        assert recent
        assert all(line.startswith((identity.user_name + ": ", identity.companion_name + ": "))
                   for line in recent)


def test_recall_history_remains_limited_to_six_latest_messages():
    identity = IdentityConfig(user_name="Mira", companion_name="Orion")
    history = [{"role": "assistant", "content": f"History marker {index}"} for index in range(7)]

    query = compose_recall_query(history, "A new question", identity=identity)

    assert "History marker 0" not in query
    assert query.endswith("Orion: History marker 6")
    assert query.count("Orion: History marker ") == 6


@pytest.mark.asyncio
async def test_recall_shortens_a_server_token_rejection_without_restart():
    bodies = []
    async def recovery():
        pytest.fail("A request validation error must not restart the daemon")
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(400, json={
                "detail": "Query too long: 700 tokens exceeds maximum of 500. Please shorten your query."})
        return httpx.Response(200, json={"results": [{"text": "Remembered"}]})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client, recovery=recovery)
        assert "Remembered" in await memory.recall("🧠" * 1000)
        assert len(bodies[0]["query"].encode("utf-8")) <= 800
        assert len(bodies[1]["query"].encode("utf-8")) <= 500
        assert "�" not in bodies[1]["query"]
        assert memory.last_error is None


@pytest.mark.asyncio
async def test_validation_rejection_is_redacted_and_not_retried():
    calls = []
    async def recovery():
        pytest.fail("Validation failures must not restart a service")
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        return httpx.Response(400, json={"detail": "postgresql://secret:password@host"})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client, recovery=recovery)
        with pytest.raises(MemoryUnavailable, match="HTTP 400") as failure:
            await memory.recall("hi")
        assert "password" not in str(failure.value)
        assert len(calls) == 2


@pytest.mark.parametrize("failure", ["connect", "timeout", "gateway"])
@pytest.mark.asyncio
async def test_safe_recall_recovers_and_retries_once(failure):
    attempts = 0
    recoveries = 0
    async def recovery():
        nonlocal recoveries
        recoveries += 1
        return True
    def handler(request):
        nonlocal attempts
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        attempts += 1
        if attempts == 1:
            if failure == "gateway":
                return httpx.Response(502)
            error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise error("offline", request=request)
        return httpx.Response(200, json={"results": [{"text": "Recovered"}]})
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client, recovery=recovery)
        assert "Recovered" in await memory.recall("hi")
    assert attempts == 2 and recoveries == 1


@pytest.mark.asyncio
async def test_recovery_is_bounded_and_does_not_replay_a_lost_retain(tmp_path):
    recoveries = 0
    requests = []
    async def recovery():
        nonlocal recoveries
        recoveries += 1
        return True
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/banks"):
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        raise httpx.ReadTimeout("uncertain outcome", request=request)
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client, recovery=recovery)
        with pytest.raises(MemoryUnavailable):
            await memory.retain({"content": "hello"}, "stable-operation")
        assert len([r for r in requests if r.method == "POST"]) == 1
        assert recoveries == 1
        requests.clear()
        with pytest.raises(MemoryUnavailable):
            await memory.recall("hi")
        assert len([r for r in requests if r.method == "POST"]) == 2
        assert recoveries == 2


@pytest.mark.parametrize(("value", "expected"), [
    (None, 1), ("", 1), ("5", 5), (" 12 ", 12), ("0", 1), ("-2", 1),
    ("1.5", 1), ("invalid", 1), ("99999999999999999999", 3600),
    ("Wed, 14 Oct 2026 00:00:30 GMT", 30),
    ("Tue, 13 Oct 2026 23:59:59 GMT", 1),
    ("Thu, 15 Oct 2026 00:00:00 GMT", 3600),
])
def test_capacity_retry_after_parsing_is_bounded(value, expected):
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert capacity_retry_delay(value, now=now) == expected


@pytest.mark.parametrize("status_code", [429, 503])
@pytest.mark.asyncio
async def test_capacity_retries_safe_recall_after_delay_without_daemon_recovery(
        status_code, monkeypatch):
    attempts = 0
    delays = []
    requests = []

    async def sleep(delay):
        delays.append(delay)

    async def recovery():
        pytest.fail("Capacity responses must not trigger daemon recovery")

    def handler(request):
        nonlocal attempts
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"results": [{"text": "Remembered"}]})

    monkeypatch.setattr("eidolon.memory.hindsight.asyncio.sleep", sleep)
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client, recovery=recovery)
        assert "Remembered" in await memory.recall("hi")
        assert memory.last_error is None
    assert attempts == 2 and delays == [3]
    assert all(request.headers["User-Agent"].startswith("Eidolon/") for request in requests)


@pytest.mark.parametrize(("timeout", "retry_after", "attempt_count", "delays"), [
    (1, "2", 1, []), (20, "9999999999", 1, []), (20, "invalid", 2, [1]),
])
@pytest.mark.asyncio
async def test_capacity_retry_is_bounded_by_deadline_and_attempt_count(
        timeout, retry_after, attempt_count, delays, monkeypatch):
    attempts = 0
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    async def recovery():
        pytest.fail("A busy healthy service must not be restarted")

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, headers={"Retry-After": retry_after},
                              json={"detail": "postgresql://private:password@host"})

    monkeypatch.setattr("eidolon.memory.hindsight.asyncio.sleep", sleep)
    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", timeout=timeout), client=client, recovery=recovery)
        with pytest.raises(MemoryCapacityUnavailable, match="HTTP 503") as failure:
            await memory.validate_bank()
        assert "password" not in str(failure.value)
        assert memory.last_error == str(failure.value)
    assert attempts == attempt_count and sleeps == delays


@pytest.mark.asyncio
async def test_memory_request_deadline_includes_transport_and_recovery():
    import asyncio

    calls = 0

    async def recovery():
        await asyncio.sleep(0.03)
        return True

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", timeout=0.01), client=client, recovery=recovery)
        with pytest.raises(MemoryUnavailable, match="explicitly continue"):
            await memory.recall("hi")
    assert calls == 1


@pytest.mark.parametrize("initial_status", ["queued", "submitted"])
@pytest.mark.parametrize("status_code", [429, 503])
@pytest.mark.asyncio
async def test_capacity_defers_outbox_across_restart_and_polls_before_replay(
        tmp_path, initial_status, status_code):
    path = tmp_path / "room.db"
    store = Store(path)
    store.complete_turn(store.session_for()["id"], "Hi", "Hello", generation_id="capacity")
    original = store.pending_outbox()[0]
    if initial_status == "submitted":
        store.update_outbox(original["document_id"], status="submitted")
    calls = []
    available = False

    async def recovery():
        pytest.fail("Background capacity must defer without daemon recovery")

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/default/banks":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        if not available:
            return httpx.Response(status_code, headers={"Retry-After": "300"})
        assert "/operations/" in request.url.path
        return httpx.Response(200, json={"status": "completed", "operation_id": original["operation_id"]})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True),
                                 client=client, recovery=recovery)
        before = datetime.now(timezone.utc)
        await OutboxDispatcher(memory, store).dispatch_once()
        delayed = store.pending_outbox()[0]
        assert delayed["status"] == "failed"
        retry_at = datetime.fromisoformat(delayed["next_retry_at"])
        assert before + timedelta(seconds=300) <= retry_at <= datetime.now(timezone.utc) + timedelta(seconds=300)
        assert delayed["operation_id"] == original["operation_id"]
        assert delayed["payload"] == original["payload"]
        assert store.outbox_status()["pending"] == 1
        request_count = len(calls)
        store.close()
        store = Store(path)
        assert store.pending_outbox()[0]["next_retry_at"] == delayed["next_retry_at"]
        await OutboxDispatcher(memory, store).dispatch_once()
        assert len(calls) == request_count
        assert store.pending_outbox()[0]["attempts"] == 1
        # Make the persisted deadline eligible without waiting five minutes.
        store.update_outbox(original["document_id"], status="failed",
                            next_retry_at=(before - timedelta(seconds=1)).isoformat())
        available = True
        await OutboxDispatcher(memory, store).dispatch_once()
        assert store.pending_outbox() == []
        assert calls[-1].method == "GET" and "/operations/" in calls[-1].url.path
        retains = [call for call in calls if call.url.path.endswith("/memories")]
        assert len(retains) == int(initial_status == "queued")
        if retains:
            body = json.loads(retains[0].content)
            assert body["operation_id"] == original["operation_id"]
            assert body["items"][0]["update_mode"] == "replace"
    store.close()


@pytest.mark.parametrize("busy_endpoint", ["banks", "memories"])
@pytest.mark.asyncio
async def test_capacity_cooldown_stops_backlog_and_includes_new_exchanges(tmp_path, busy_endpoint):
    path = tmp_path / "room.db"
    store = Store(path)
    session = store.session_for()["id"]
    for generation in ["first", "second"]:
        store.complete_turn(session, generation, "Hello", generation_id=generation)
    originals = store.pending_outbox()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/default/banks" and busy_endpoint != "banks":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        return httpx.Response(503, headers={"Retry-After": "3"})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client)
        dispatcher = OutboxDispatcher(memory, store)
        await dispatcher.dispatch_once()
        entries = store.pending_outbox()
        request_count = 1 if busy_endpoint == "banks" else 2
        assert len(requests) == request_count
        assert {entry["next_retry_at"] for entry in entries} == {entries[0]["next_retry_at"]}
        assert entries[0]["next_retry_at"] is not None
        assert sum(entry["attempts"] for entry in entries) == 1
        store.complete_turn(session, "third", "Hello", generation_id="third")
        assert len(store.pending_outbox()) == 3
        assert store.pending_outbox()[-1]["next_retry_at"] == entries[0]["next_retry_at"]
        store.close()
        store = Store(path)
        await OutboxDispatcher(memory, store).dispatch_once()
        assert len(requests) == request_count
        restored = {entry["document_id"]: entry for entry in store.pending_outbox()}
        for original in originals:
            entry = restored[original["document_id"]]
            assert entry["payload"] == original["payload"]
            assert entry["operation_id"] == original["operation_id"]
        assert store.outbox_status()["pending"] == 3
    store.close()
