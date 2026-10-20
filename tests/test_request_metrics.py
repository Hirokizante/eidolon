import asyncio
from contextlib import aclosing

import pytest

from eidolon.providers.metrics import measured_stream, request_metrics
from eidolon.storage import Store


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 5000, "completion_tokens": 100,
      "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500}},
     (5000, 100, 4000, 500)),
    ({"input_tokens": 500, "output_tokens": 100, "cache_read_input_tokens": 4000,
      "cache_creation_input_tokens": 500}, (5000, 100, 4000, 500)),
    ({"input_tokens": 5000, "output_tokens": 100,
      "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500}},
     (5000, 100, 4000, 500)),
    ({"prompt_tokens": 100, "completion_tokens": 0,
      "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}, (100, 0, 0, 0)),
    ({}, (None, None, None, None)),
    ({"prompt_tokens": True, "completion_tokens": -1,
      "prompt_tokens_details": {"cached_tokens": "100", "cache_write_tokens": False}},
     (None, None, None, None)),
])
def test_reported_input_output_and_cache_counts_remain_distinct(usage, expected):
    metrics = request_metrics(usage, None)
    assert tuple(metrics[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                                          "cache_write_tokens")) == expected


async def test_each_dispatch_records_latency_and_only_reported_numeric_usage(monkeypatch):
    ticks = iter([10, 12, 16])
    monkeypatch.setattr("eidolon.providers.metrics.monotonic", lambda: next(ticks))
    records = []

    async def stream():
        yield {"type": "request", "body": "Private request body"}
        yield {"type": "token", "text": "Private reply"}
        yield {"type": "usage", "usage": {"prompt_tokens": 5000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500},
            "unsafe": "Private provider text"}, "duration": 4}
        yield {"type": "finish", "reason": "stop"}

    measured = measured_stream(stream(), record=lambda **row: records.append(row))
    events = [event async for event in measured]
    assert len(events) == 4 and len(records) == 1
    assert records[0]["outcome"] == "completed"
    metrics = records[0]["metrics"]
    assert metrics["input_tokens"] == 5000 and metrics["output_tokens"] == 100
    assert metrics["cache_read_tokens"] == 4000 and metrics["cache_write_tokens"] == 500
    assert metrics["duration_seconds"] == 6 and metrics["time_to_first_token_seconds"] == 2
    assert "Private" not in str(records)


@pytest.mark.parametrize("failure", [False, True])
async def test_measurements_preserve_unknown_usage_for_completed_and_failed_streams(failure):
    records = []

    async def stream():
        if failure:
            raise RuntimeError("Synthetic failure")
        yield {"type": "finish", "reason": "stop"}

    try:
        async for _ in measured_stream(stream(), record=lambda **row: records.append(row)):
            pass
    except RuntimeError:
        assert failure
    assert len(records) == 1
    assert records[0]["outcome"] == ("failed" if failure else "completed")
    metrics = records[0]["metrics"]
    assert metrics["duration_seconds"] >= 0
    assert metrics["time_to_first_token_seconds"] is None
    assert metrics["input_tokens"] is metrics["cache_read_tokens"] is None


async def test_cancelled_requests_keep_partial_reported_usage_and_close_transport():
    records, closed = [], []
    started = asyncio.Event()

    async def stream():
        try:
            yield {"type": "usage", "usage": {"prompt_tokens": 100}}
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    async def consume():
        measured = measured_stream(stream(), record=lambda **row: records.append(row))
        async with aclosing(measured) as events:
            async for _ in events:
                pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True] and len(records) == 1
    assert records[0]["outcome"] == "cancelled"
    assert records[0]["metrics"]["input_tokens"] == 100
    assert records[0]["metrics"]["output_tokens"] is None


async def test_separate_stream_usage_packets_merge_counts_without_double_counting():
    records = []

    async def stream():
        yield {"type": "usage", "usage": {"prompt_tokens": 5000,
            "prompt_tokens_details": {"cached_tokens": 4000}}}
        yield {"type": "usage", "usage": {"completion_tokens": 100,
            "prompt_tokens_details": {"cache_write_tokens": 500}}}
        yield {"type": "finish", "reason": "stop"}

    async for _ in measured_stream(stream(), record=lambda **row: records.append(row)):
        pass
    metrics = records[0]["metrics"]
    assert metrics["input_tokens"] == 5000 and metrics["output_tokens"] == 100
    assert metrics["cache_read_tokens"] == 4000 and metrics["cache_write_tokens"] == 500
    assert metrics["context_tokens"] == 5100 and metrics["cache_hit_rate"] == .8


async def test_closing_after_completion_keeps_the_completed_request_outcome():
    records = []

    async def stream():
        yield {"type": "finish", "reason": "stop"}

    measured = measured_stream(stream(), record=lambda **row: records.append(row))
    async with aclosing(measured) as events:
        async for event in events:
            if event["type"] == "finish":
                break
    assert records[0]["outcome"] == "completed"


def test_v8_upgrade_adds_private_numeric_metrics_and_preserves_them_on_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.create_session("today", kind="chat")
    store.complete_turn("today", "Original user message", "Original reply")
    with store.db:
        store.db.execute("DROP TABLE model_request_metrics")
        store.db.execute("PRAGMA user_version=8")
    store.close()
    store = Store(path)
    assert store.migration_backup_path and store.migration_backup_path.is_file()
    store.record_request_metrics(role="dialogue", session_id="today", generation_id="one",
        attempt_id="attempt", round_index=0, provider="synthetic", model="test-model",
        outcome="completed", metrics={"input_tokens": 5000, "cache_read_tokens": 4000,
                                       "cache_write_tokens": 0, "duration_seconds": 1.25})
    for invalid in ({"body": "private"}, {"input_tokens": True}, {"input_tokens": -1},
                    {"duration_seconds": float("nan")}):
        with pytest.raises(ValueError, match="numeric measurements"):
            store.record_request_metrics(role="vision", provider="test", model="test",
                                         outcome="failed", metrics=invalid)
    records = store.list_request_metrics("today")
    store.close()
    store = Store(path)
    try:
        assert store.migration_backup_path is None
        assert store.list_request_metrics("today") == records
        assert store.list_messages("today")[0]["content"] == "Original user message"
        assert len(records) == 1 and records[0]["metrics"]["cache_write_tokens"] == 0
        assert not {"body", "messages", "headers", "usage"}.intersection(records[0])
        assert "Original" not in str(records)
    finally:
        store.close()


def test_numeric_request_log_keeps_latest_thousand_across_routes(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        for index in range(1005):
            store.record_request_metrics(role="vision" if index % 2 else "dialogue",
                session_id="first" if index % 2 else "second", provider="synthetic",
                model="test-model", outcome="completed", metrics={"input_tokens": index})
        records = store.list_request_metrics(limit=1000)
        assert len(records) == 1000
        assert records[0]["metrics"]["input_tokens"] == 1004
        assert records[-1]["metrics"]["input_tokens"] == 5
        filtered = store.list_request_metrics("first", limit=1000)
        assert len(filtered) == 500 and all(row["session_id"] == "first" for row in filtered)
        with pytest.raises(ValueError, match="between 1 and 1000"):
            store.list_request_metrics(limit=1001)
    finally:
        store.close()
