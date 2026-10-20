import asyncio
import json

import httpx
import pytest

from eidolon.config import MemoryConfig
from eidolon.memory import HindsightMemory, OutboxDispatcher
from eidolon.storage import Store


def legacy_entry(store, *, status="queued", generation="legacy"):
    store.complete_turn(store.session_for()["id"], "Synthetic question", "Synthetic reply",
                        generation_id=generation)
    entry = next(row for row in store.pending_outbox() if row["generation_id"] == generation)
    with store.db:
        store.db.execute("UPDATE generations SET memory_eligible=NULL WHERE id=?", (generation,))
        store.db.execute("UPDATE outbox SET review_state='pending',status=? WHERE document_id=?",
                         (status, entry["document_id"]))
    return entry


async def test_accepted_work_delivers_with_new_saving_and_recall_disabled(tmp_path):
    store = Store(tmp_path / "room.db")
    store.complete_turn(store.session_for()["id"], "Accepted", "Eligible reply")
    store.complete_turn(store.session_for()["id"], "Private", "History only", retain=False)
    bodies = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"async": True, "operation_id": body["operation_id"]})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        config = MemoryConfig(bank="hermes", enabled=False, save_enabled=False, retain_enabled=True, verified=True)
        await OutboxDispatcher(HindsightMemory(config, client=client), store).dispatch_once()
    assert len(bodies) == 1
    assert "Accepted" in bodies[0]["items"][0]["content"]
    assert "Private" not in json.dumps(bodies)
    store.close()


@pytest.mark.parametrize(("operation_status", "reviewed", "local_status"), [
    ("processing", 0, "submitted"), ("pending", 0, "submitted"),
    ("completed", 0, "completed"), ("not_found", 1, "queued"),
    ("failed", 1, "queued"), ("cancelled", 1, "queued"),
])
async def test_legacy_review_polls_before_decision_without_replaying_held_work(
        tmp_path, operation_status, reviewed, local_status):
    store = Store(tmp_path / "room.db")
    entry = legacy_entry(store, status="submitted")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and "/operations/" in request.url.path
        return httpx.Response(200, json={"status": operation_status})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        # Reviewing an old operation is read-only and does not require enabling
        # new learning, recall, or submission readiness.
        memory = HindsightMemory(MemoryConfig(bank="hermes", enabled=False, save_enabled=False), client=client)
        dispatcher = OutboxDispatcher(memory, store)
        await dispatcher.pause_delivery(True)
        result = await dispatcher.review_legacy(False, document_ids=[entry["document_id"]])
    assert result["reviewed"] == reviewed and len(requests) == 1
    row = store.db.execute("SELECT * FROM outbox").fetchone()
    assert row["status"] == local_status
    assert row["operation_id"] == entry["operation_id"]
    assert row["review_state"] == ("rejected" if reviewed else "pending")
    store.close()


async def test_approved_previously_failed_legacy_operation_retries_same_identity(tmp_path):
    store = Store(tmp_path / "room.db")
    entry = legacy_entry(store, status="failed")
    methods = []

    def handler(request):
        methods.append((request.method, request.url.path))
        assert entry["operation_id"] in request.url.path
        if request.method == "GET":
            return httpx.Response(200, json={"status": "failed"})
        assert request.url.path.endswith("/retry")
        return httpx.Response(200, json={"operation_id": entry["operation_id"]})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client)
        dispatcher = OutboxDispatcher(memory, store)
        result = await dispatcher.review_legacy(True, document_ids=[entry["document_id"]])
        assert result["reviewed"] == 1 and len(methods) == 1
        await dispatcher.dispatch_once()
    assert [method for method, _ in methods] == ["GET", "GET", "POST"]
    assert store.pending_outbox()[0]["status"] == "submitted"
    store.close()


async def test_legacy_queued_work_never_submits_without_explicit_selected_review(tmp_path):
    store = Store(tmp_path / "room.db")
    selected = legacy_entry(store, generation="selected")
    unselected = legacy_entry(store, generation="unseen")
    submitted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        body = json.loads(request.content)
        submitted.append(body["items"][0]["document_id"])
        return httpx.Response(200, json={"async": True, "operation_id": body["operation_id"]})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        dispatcher = OutboxDispatcher(
            HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client), store)
        await dispatcher.dispatch_once()
        assert submitted == []
        await dispatcher.review_legacy(True, document_ids=[selected["document_id"]])
        await dispatcher.dispatch_once()
    assert submitted == [selected["document_id"]]
    assert next(row for row in store.pending_outbox()
                if row["document_id"] == unselected["document_id"])["review_state"] == "pending"
    store.close()


async def test_pause_acknowledgement_waits_for_submission_and_survives_restart(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    store.complete_turn(store.session_for()["id"], "Accepted", "Eligible reply")
    submitted = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"banks": [{"bank_id": "hermes"}]})
        body = json.loads(request.content)
        submitted.set()
        await release.wait()
        return httpx.Response(200, json={"async": True, "operation_id": body["operation_id"]})

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes", retain_enabled=True, verified=True), client=client)
        dispatcher = OutboxDispatcher(memory, store)
        dispatch = asyncio.create_task(dispatcher.dispatch_once())
        await submitted.wait()
        pause = asyncio.create_task(dispatcher.pause_delivery(True))
        await asyncio.sleep(0)
        assert not pause.done()
        release.set()
        await dispatch
        assert (await pause)["delivery_paused"] is True
        store.close()
        store = Store(path)
        assert store.memory_delivery_paused() is True
        # The paused dispatcher must not start a retain or retry call.
        await OutboxDispatcher(memory, store).dispatch_once()
    store.close()


async def test_transport_backoff_defers_held_review_without_deciding_or_replaying(tmp_path):
    path = tmp_path / "room.db"
    store = Store(path)
    entry = legacy_entry(store, status="failed")
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("Synthetic outage", request=request)

    async with httpx.AsyncClient(base_url="http://memory", transport=httpx.MockTransport(handler)) as client:
        memory = HindsightMemory(MemoryConfig(enabled=True, save_enabled=True, bank="hermes"), client=client)
        dispatcher = OutboxDispatcher(memory, store)
        result = await dispatcher.review_legacy(False, document_ids=[entry["document_id"]])
        assert result["reviewed"] == 0 and result["held"] == 1
        store.close()
        store = Store(path)
        result = await OutboxDispatcher(memory, store).review_legacy(
            False, document_ids=[entry["document_id"]])
        assert result["reviewed"] == 0
        assert len(requests) == 1
        assert store.pending_outbox()[0]["next_retry_at"] is not None
    store.close()
