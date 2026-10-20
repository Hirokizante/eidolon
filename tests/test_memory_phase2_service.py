"""Background continuity does not compete with conversations or recall health."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from eidolon.config import IdentityConfig, load_config, save_config
from eidolon.service.runtime import RoomService
from test_service import room


class Worker:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.closed = False

    @property
    def status(self):
        return {"pending": 1, "error": None, "stages": []}

    async def run_once(self, *, force=False):
        self.calls.append(force)
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.stopped.set()

    async def close(self):
        self.closed = True


async def test_background_provider_limits_do_not_change_conversation_parameters(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        config = service.config.model_dump(mode="json")
        config["providers"][0]["parameters"] = {
            "reasoning_effort": "high", "max_completion_tokens": 8192,
            "temperature": .7,
        }
        await client.request("save_settings", config=config)
        chat = service.config.selected_provider()
        background = service.continuity_worker.provider.config
        assert background is not chat and background.parameters is not chat.parameters
        assert background.parameters == {
            "reasoning_effort": "low", "max_completion_tokens": 4096, "temperature": .7,
        }
        assert chat.parameters == config["providers"][0]["parameters"]
        # A plain profile gets a bounded completion without adding effort support.
        config["providers"][0]["parameters"] = {}
        await client.request("save_settings", config=config)
        assert service.continuity_worker.provider.config.parameters == {"max_tokens": 4096}
        assert service.config.selected_provider().parameters == {}


async def test_continuity_lists_load_full_current_and_historical_evidence_on_drilldown(
    tmp_path, monkeypatch,
):
    from test_memory_phase2_engine import seed
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        item = seed(service.store)
        state = await client.request("memory_state")
        summary = state["continuity"]["items"][0]
        history = state["continuity"]["history"][0]
        assert summary["content"] == item["content"] and summary["source_count"] == 1
        assert "sources" not in summary and "data" not in history
        detail = await client.request("memory_continuity_item", item_id=summary["id"])
        assert detail["sources"][0]["excerpt"] == item["sources"][0]["excerpt"]
        assert detail["sources"][0]["excerpt"] != item["content"]
        older = await client.request("memory_continuity_history", history_id=history["id"])
        assert older["data"]["sources"][0]["excerpt"] == item["sources"][0]["excerpt"]
        assert not provider.requests and not service.memory.queries
        assert await client.request("memory_continuity_item", item_id="unknown") is None


async def test_manual_notes_create_edit_delete_without_model_or_retain_calls(tmp_path, monkeypatch):
    from test_memory_phase2_engine import seed

    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        learned = seed(service.store)
        created = await client.request("memory_create_note", category="emotion",
                                       topic="Rainy-day feeling", importance="low",
                                       content="A deliberate rainy-day feeling 雨.")
        note = created["item"]
        assert note["user_created"] and note["locked"] and note["category"] == "emotion"
        assert any(item["id"] == note["id"] and item["user_created"]
                   for item in created["snapshot"]["continuity"]["items"])
        edited = await client.request("memory_edit_note", item_id=note["id"],
                                      expected_revision=1, content="A revised deliberate note.",
                                      category="relationship")
        assert edited["item"]["user_created"] and edited["item"]["locked"]
        assert edited["item"]["category"] == "relationship"
        auto_edit = await client.request("memory_edit_note", item_id=learned["id"],
                                         expected_revision=learned["revision"],
                                         content="My creative project needs a revised sketch.",
                                         category="thread")
        assert not auto_edit["item"].get("user_created") and not auto_edit["item"]["locked"]
        with pytest.raises(RuntimeError, match="revision changed"):
            await client.request("memory_delete_note", item_id=note["id"], expected_revision=1)
        deleted = await client.request("memory_delete_note", item_id=note["id"], expected_revision=2)
        assert service.store.get_continuity_item(note["id"]) is None
        assert all(item["id"] != note["id"] for item in deleted["snapshot"]["continuity"]["items"])
        await client.request("memory_delete_note", item_id=learned["id"],
                             expected_revision=auto_edit["item"]["revision"])
        assert service.store.continuity_state()["items"] == []
        assert not provider.requests and not service.memory.queries
        # Notes are local working context, never synthetic completed exchanges.
        assert len(service.store.continuity_inputs()) == 1


async def test_saved_note_limit_updates_worker_and_governs_manual_admission(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        await client.request("save_preferences", preferences={"continuity_max_notes": 1})
        assert service.continuity_worker.max_notes == 1
        note = (await client.request(
            "memory_create_note", topic="A gathering", importance="moderate",
            category="relationship", content="A gathering is being arranged.",
        ))["item"]
        with pytest.raises(RuntimeError, match="Current life is full"):
            await client.request("memory_create_note", topic="Anniversary", importance="high",
                                 category="relationship", content="An anniversary is approaching.")
        snapshot = await client.request("memory_state")
        assert snapshot["continuity"]["active_count"] == snapshot["continuity"]["max_notes"] == 1
        await client.request("memory_edit_note", item_id=note["id"], expected_revision=1,
                             state="archived")
        added = await client.request("memory_create_note", topic="Anniversary", importance="high",
                                      category="relationship", content="An anniversary is approaching.")
        assert added["item"]["importance"] == "high"
        assert service.store.get_continuity_item(note["id"])["state"] == "archived"
        assert not provider.requests and not service.memory.queries


@pytest.mark.parametrize("params", [
    {"content": "", "category": "thread"},
    {"content": "   ", "category": "thread"},
    {"content": "x" * 4001, "category": "thread"},
    {"content": "A note.", "category": "unknown"},
    {"content": "A note.", "category": []},
])
async def test_manual_note_rejects_invalid_content_and_labels(tmp_path, monkeypatch, params):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        with pytest.raises(RuntimeError):
            await client.request("memory_create_note", **params)
        assert service.store.continuity_state()["items"] == []


@pytest.mark.parametrize("revision", [None, True, "1", 0])
async def test_manual_note_rejects_invalid_revisions_without_changing_data(
    tmp_path, monkeypatch, revision,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = service.store.create_continuity_item(
            "My creative project needs a sketch.", "thread",
            topic="Creative project", importance="moderate",
        )
        for method in ("memory_edit_note", "memory_delete_note"):
            with pytest.raises(RuntimeError, match="saved continuity revision"):
                await client.request(method, item_id=note["id"], expected_revision=revision,
                                     content="Changed wording.", category="situation")
        assert service.store.get_continuity_item(note["id"]) == note


async def test_continuity_run_exposes_saved_note_versions_without_diagnostics(tmp_path, monkeypatch):
    from test_memory_phase2_engine import seed
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        item = seed(service.store)
        task_id = service.store.continuity_history()[0]["task_id"]
        run = await client.request("memory_continuity_run", task_id=task_id)
        assert run["status"] == "completed" and len(run["changes"]) == 1
        change = run["changes"][0]
        assert change["created"] and change["before"] is None
        assert change["after"]["content"] == item["content"]
        assert not {"sources", "id", "revision", "scope", "origin"}.intersection(change["after"])
        state = await client.request("memory_state")
        stage = next(row for row in state["continuity"]["maintenance"]["stages"]
                     if row["task_id"] == task_id)
        assert stage["change_count"] == 1
        assert "changes" not in stage  # Full note pairs load only when opening a run.
        assert await client.request("memory_continuity_run", task_id="unknown") is None
        assert not provider.requests and not service.memory.queries


async def test_continuity_run_reports_active_work_and_then_the_committed_changes(tmp_path, monkeypatch):
    from test_memory_maintenance import Provider, add, learn
    entered, release = asyncio.Event(), asyncio.Event()
    class PausedProvider(Provider):
        async def stream(self, *args, **kwargs):
            entered.set()
            await release.wait()
            async for event in super().stream(*args, **kwargs):
                yield event
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        add(service.store)
        service.continuity_worker.provider = PausedProvider(transform=learn)
        assert service._start_continuity(force=True)
        task = service._continuity_task
        try:
            await entered.wait()
            task_id = service.store.maintenance_status()["latest_attempt"]["task_id"]
            run = await client.request("memory_continuity_run", task_id=task_id)
            assert run["status"] == "running" and run["changes"] == []
        finally:
            release.set()
            await task
        run = await client.request("memory_continuity_run", task_id=task_id)
        assert run["status"] == "completed" and len(run["changes"]) == 1
        assert run["changes"][0]["created"]
        assert not provider.requests and not service.memory.queries


async def test_continuity_run_keeps_all_note_versions_within_the_socket_allowance(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        text = "🌸" * 4000
        note = {"content": text, "category": "thread", "state": "open", "id": "internal-id",
                "sources": [{"excerpt": "A diagnostic source." * 4000} for _ in range(32)],
                "input_hash": "internal-hash", "revision": 99}
        monkeypatch.setattr(service.store, "continuity_run_changes", lambda task_id: {
            "status": "completed", "updated_at": "2026-10-14T04:00:00+00:00",
            "changes": [{"after": note, "before": note, "created": False} for _ in range(32)],
        })
        run = await client.request("memory_continuity_run", task_id="synthetic-large-run")
        assert len(run["changes"]) == 32
        assert all(change["after"]["content"] == change["before"]["content"] == text
                   for change in run["changes"])
        encoded = json.dumps(run, ensure_ascii=False).encode("utf-8")
        assert len(encoded) < 2_000_000
        assert b"internal-id" not in encoded and b"diagnostic source" not in encoded


@pytest.mark.parametrize("task_id", [None, False, 5, "", "x" * 201])
async def test_continuity_run_rejects_invalid_selection(tmp_path, monkeypatch, task_id):
    async with room(tmp_path, monkeypatch) as (_, client, _, _):
        with pytest.raises(RuntimeError, match="Choose a current life refresh"):
            await client.request("memory_continuity_run", task_id=task_id)


async def test_memory_state_limits_utf8_bytes_and_discloses_whole_row_omissions(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        content = "🌸" * 4000
        items = [{"id": str(index), "revision": 1, "category": "thread", "state": "open",
                  "content": content, "origin": "user_note", "sources": []}
                 for index in range(100)]
        monkeypatch.setattr(service.store, "continuity_state", lambda **kwargs: {
            "scope": "real_life", "revision": 1, "control_revision": 0,
            "watermark": 0, "items": items,
        })
        monkeypatch.setattr(service.store, "list_pending_legacy", lambda **kwargs: [{
            "document_id": f"held-{index}", "generation_id": f"legacy-{index}",
            "status": "queued", "updated_at": "2026-10-15T00:00:00+00:00",
            "review_state": "pending", "payload": {"content": content},
        } for index in range(100)])
        state = await client.request("memory_state")
        assert len(json.dumps(state, ensure_ascii=False).encode("utf-8")) <= 1_000_000
        assert 0 < len(state["continuity"]["items"]) < 100
        assert state["inspection_omitted"]["continuity_items"] == (
            100 - len(state["continuity"]["items"])
        )
        assert all(item["content"] == content for item in state["continuity"]["items"])
        assert state["legacy_pending"]  # A held review can always advance through shown sources.
        assert state["controls"]["save_new_memories"] and not provider.requests
        assert len(items) == 100  # Inspection did not mutate the stored collection.


async def test_manual_refresh_returns_promptly_and_bounds_concurrent_work(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, provider, _):
        worker = Worker()
        service.continuity_worker = worker
        assert not service._start_continuity()
        result = await asyncio.wait_for(client.request("memory_refresh"), .5)
        await worker.started.wait()
        assert result["refresh_queued"] and result["continuity"]["maintenance"]["running"]
        again = await asyncio.wait_for(client.request("memory_refresh"), .5)
        assert not again["refresh_queued"] and worker.calls == [True]
        assert not service.generation_lock.locked() and not provider.requests
        state = await client.request("memory_state")
        assert state["readiness"]["status"] == "not checked"
        assert state["continuity"]["maintenance"]["pending"] == 1
        assert not state["controls"]["automatic_continuity"]
        worker.release.set()
        await asyncio.gather(service._continuity_task)


async def test_foreground_cancels_continuity_before_acquiring_generation_lock(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        worker = Worker()
        service.continuity_worker = worker
        await client.request("memory_refresh")
        await worker.started.wait()
        # A foreground waiter reserves priority while the lock remains occupied.
        async with service.generation_lock:
            generate = asyncio.create_task(client.request(
                "generate", session_id=service.current_session["id"], text="Synthetic foreground",
            ))
            await asyncio.wait_for(worker.stopped.wait(), .5)
            assert service._foreground_waiters == 1
            assert not service._start_continuity(force=True)
            assert not generate.done()
        result = await asyncio.wait_for(generate, 1)
        assert result is None
        assert service._foreground_waiters == 0 and service._continuity_task is None


async def test_reload_cancels_continuity_before_replacing_components(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        worker = Worker()
        service.continuity_worker = worker
        await client.request("memory_refresh")
        await worker.started.wait()
        await client.request("save_preferences", preferences={"continuity_enabled": True})
        assert worker.stopped.is_set() and worker.closed
        assert service.continuity_worker is not worker and service._continuity_task is None
        assert service.config.memory.continuity_enabled


async def test_continuity_failure_is_separate_from_recall_readiness(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        worker = Worker()
        async def failure(**kwargs):
            raise RuntimeError("Synthetic private provider content")
        worker.run_once = failure
        service.continuity_worker = worker
        result = await client.request("memory_refresh")
        if service._continuity_task is not None:
            await service._continuity_task
        state = await client.request("memory_state")
        assert state["readiness"] == result["readiness"]
        assert state["continuity"]["maintenance"]["runtime_error"]
        assert "private provider content" not in str(state)
        assert service.service_error is None


async def test_service_worker_interruption_retries_original_evidence_without_new_exchange(
    tmp_path, monkeypatch,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"],
                                    "Synthetic pending sketch.", "Synthetic acknowledgement.")
        started, release = asyncio.Event(), asyncio.Event()
        class MaintenanceProvider:
            async def stream(self, messages, **kwargs):
                started.set()
                await release.wait()
                data = json.loads(messages[1]["content"])
                yield {"type": "token", "text": json.dumps({"operations": [{
                    "op": "no_change", "expected_revision": data["snapshot_revision"],
                    "sources": [],
                }]})}
                yield {"type": "finish", "reason": "stop"}
        service.continuity_worker.provider = MaintenanceProvider()
        before = {table: service.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("generations", "messages", "outbox")}
        await client.request("memory_refresh")
        await asyncio.wait_for(started.wait(), 1)
        original = service.store.pending_maintenance_task()
        async with service._foreground_generation():
            assert not service._continuity_task
            assert service.store.continuity_state()["watermark"] == 0
        retry = service.store.pending_maintenance_task()
        assert retry["id"] == original["id"] and retry["input_ids"] == original["input_ids"]
        assert retry["stages"]["continuity"]["status"] == "failed"
        release.set()
        await client.request("memory_refresh")
        if service._continuity_task is not None:
            await service._continuity_task
        assert service.store.pending_maintenance_task() is None
        assert service.store.continuity_state()["watermark"] == original["input_watermark"]
        after = {table: service.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                 for table in before}
        assert after == before


async def test_external_rename_and_restart_preserve_pending_evidence_and_delivery(tmp_path, monkeypatch):
    from test_memory_maintenance import Provider, learn

    started = asyncio.Event()

    class HeldProvider:
        async def stream(self, messages, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield {}

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        recorded = {"user_name": "Aster", "companion_name": "Vale"}
        service.store.complete_turn(service.current_session["id"], "My synthetic project is open.",
                                    "Understood.", metadata={"identity": recorded})
        service.store.complete_turn(service.current_session["id"], "A legacy synthetic source.",
                                    "Understood.")
        service.store.complete_turn(service.current_session["id"], "An opted-out source.",
                                    "Understood.", retain=False)
        for entry in service.store.pending_outbox():
            service.store.update_outbox(entry["document_id"], status="failed", attempted=True)
        saved_rows = {table: [dict(row) for row in service.store.db.execute(f"SELECT * FROM {table}")]
                      for table in ("generations", "messages", "outbox")}
        worker = service.continuity_worker
        worker.provider = HeldProvider()
        await client.request("memory_refresh")
        await asyncio.wait_for(started.wait(), 5)
        task = service.store.pending_maintenance_task()
        route = service.store.auxiliary_route()
        saved = load_config(service.configuration)
        saved.identity = IdentityConfig(user_name="River", companion_name="Sol")
        save_config(saved, service.configuration)
        await service._refresh_auxiliary()
        assert service.continuity_worker is worker
        assert worker.identity == saved.identity
        assert service._continuity_task is not None and not service._continuity_task.done()
        assert service.store.auxiliary_route() == route
        await service._cancel_continuity()
        assert service.store.maintenance_attempts(task["id"])[0]["outcome"] == "interrupted"
        configuration = service.configuration

    restarted = RoomService(load_config(configuration), configuration)
    try:
        pending = restarted.store.pending_maintenance_task()
        for key in ("id", "inputs", "input_ids", "input_hash", "input_watermark"):
            assert pending[key] == task[key]
        assert restarted.store.auxiliary_route() == route
        provider = Provider(transform=learn)
        restarted.continuity_worker.provider = provider
        assert await restarted.continuity_worker.run_once(force=True)
        document = json.loads(provider.calls[0][-1]["content"])
        assert document["current_identity"] == saved.identity.model_dump()
        assert len(document["exchanges"]) == 2
        assert document["exchanges"][0]["identity"] == recorded
        assert "identity" not in document["exchanges"][1]
        item = restarted.store.continuity_state()["items"][0]
        assert item["sources"][0]["exchange_id"] == document["exchanges"][0]["exchange_id"]
        assert item["sources"][0]["role"] == "user"
        assert item["sources"][0]["excerpt"] == "My synthetic project is open."
        assert restarted.store.pending_maintenance_task() is None
        assert {table: [dict(row) for row in restarted.store.db.execute(f"SELECT * FROM {table}")]
                for table in saved_rows} == saved_rows
        attempts = restarted.store.maintenance_attempts(task["id"])
        assert [row["outcome"] for row in attempts] == ["committed", "interrupted"]
        assert attempts[0]["provenance"] == attempts[1]["provenance"]

        polled, delivered = [], []

        async def operation(operation_id):
            polled.append(operation_id)
            return {"status": "not_found", "operation_id": operation_id}

        async def retain(payload, operation_id):
            delivered.append((payload, operation_id))
            return {"async": False, "success": True}

        monkeypatch.setattr(restarted.memory, "operation", operation, raising=False)
        monkeypatch.setattr(restarted.memory, "retain", retain, raising=False)
        restarted.memory.config.retain_enabled = True
        restarted.memory.config.verified = True
        await restarted.dispatcher.dispatch_once()
        assert polled == [row["operation_id"] for row in saved_rows["outbox"]]
        assert delivered == [(json.loads(row["payload"]), row["operation_id"])
                             for row in saved_rows["outbox"]]
        for before, after in zip(saved_rows["outbox"], restarted.store.db.execute("SELECT * FROM outbox"),
                                 strict=True):
            assert after["status"] == "completed"
            for key in ("generation_id", "document_id", "operation_id", "payload"):
                assert after[key] == before[key]
    finally:
        await restarted.close()


async def test_socket_message_received_before_midnight_keeps_date_while_queued(
    tmp_path, monkeypatch,
):
    original = datetime(2026, 10, 15, 15, 59, tzinfo=timezone.utc)
    clock = [original]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr("eidolon.service.runtime.datetime", Clock)
    monkeypatch.setattr("eidolon.engine.conversation.datetime", Clock)
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        async with service.generation_lock:
            generate = asyncio.create_task(client.request(
                "generate", text="Synthetic interview tomorrow.",
            ))
            async with asyncio.timeout(1):
                while not service._foreground_waiters:
                    await asyncio.sleep(.001)
            # 23:59 -> 00:01 in Asia/Manila while waiting for an earlier response.
            clock[0] = datetime(2026, 10, 15, 16, 1, tzinfo=timezone.utc)
        await asyncio.wait_for(generate, 1)
        source = service.store.continuity_inputs()[0]
        assert source["user_created_at"] == original.isoformat()
        from eidolon.memory.continuity import resolve_event_time
        assert resolve_event_time("tomorrow", source["user_created_at"]).startswith("2026-10-16")
