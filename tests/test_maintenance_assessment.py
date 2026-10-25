"""Measured synthetic recovery at the existing service/storage boundary.

JUnit properties contain aggregate data only; fabricated usage is explicitly
labelled and must never be presented as measured model token consumption.
"""
import json
import time
from collections import Counter

import pytest

from eidolon.config import load_config
from eidolon.providers.openai import ProviderCapabilityError, ProviderError
from eidolon.service.runtime import RoomService
from test_current_life_policy import seed_note
import test_maintenance_recovery as recovery
from test_maintenance_recovery import automatic
from test_ordinary_proposals import refresh
from test_service import room

clock = recovery.clock


@pytest.mark.parametrize("scenario", [
    "malformed", "empty", "truncated", "transient", "configuration", "restart",
    "split", "retained_gap", "chronology", "justified_no_change",
])
async def test_recovery_assessment(tmp_path, monkeypatch, clock, record_property, scenario):
    class Provider:
        calls = 0

        async def stream(self, messages, **kwargs):
            self.calls += 1
            document = json.loads(messages[-1]["content"])
            yield {"type": "usage", "usage": {"prompt_tokens": 42, "completion_tokens": 7}}
            if scenario == "configuration":
                raise ProviderCapabilityError("PRIVATE synthetic diagnostic")
            if scenario == "transient" and self.calls == 1:
                raise ProviderError("PRIVATE synthetic diagnostic")
            if scenario == "empty" and self.calls == 1:
                yield {"type": "finish", "reason": "stop"}
                return
            if scenario == "truncated" and self.calls == 1:
                yield {"type": "token", "text": '{"operations":['}
                yield {"type": "finish", "reason": "length"}
                return
            invalid = ((scenario in {"malformed", "restart"} and self.calls == 1)
                       or (scenario == "retained_gap" and self.calls <= 6)
                       or (scenario == "split" and len(document["exchanges"]) > 1))
            if invalid:
                response = {"operations": []}
            elif scenario == "justified_no_change":
                response = {"operations": [{"op": "no_change"}]}
            else:
                row = document["exchanges"][0]
                garden = "garden" in row["user_content"]
                operation = {"op": "upsert", "category": "thread", "importance": "moderate",
                    "topic": "Garden project" if garden else "Interview preparation",
                    "content": ("The user's garden project is unfinished." if garden else
                                "The user's interview is tomorrow."),
                    "sources": [row["source_refs"]["user"]]}
                if scenario == "chronology":
                    operation.update(note_ref=document["items"][0]["note_ref"], topic="House move",
                                     content="The user is preparing for a house move.")
                response = {"operations": [operation]}
            yield {"type": "token", "text": json.dumps(response)}
            yield {"type": "finish", "reason": "stop"}

    async with room(tmp_path, monkeypatch) as (service, client, dialogue, _):
        await client.request("save_preferences", preferences={
            "continuity_enabled": True, "continuity_daily_retry_calls": 0,
        })
        before = None
        if scenario == "chronology":
            seed_note(service.store, topic="House move", text="My house move is ongoing.")
            before = service.store.continuity_state()["items"]
            service.store.complete_turn(service.current_session["id"],
                "I am preparing for my house move.", "Okay.",
                metadata={"user_message_at": "2020-01-01T00:00:00+00:00"})
        else:
            text = "Hello." if scenario == "justified_no_change" else "My interview is tomorrow."
            service.store.complete_turn(service.current_session["id"], text, "Understood.")
        if scenario == "split":
            service.store.complete_turn(service.current_session["id"],
                                        "My garden project is unfinished.", "Understood.")
        provider = Provider()
        started = time.perf_counter()
        await refresh(service, client, provider)
        active = service
        try:
            if scenario == "restart":
                await service.close()
                active = RoomService(load_config(service.configuration), service.configuration)
            for _ in range(7):
                clock.advance()
                await automatic(active, provider)
            if scenario == "retained_gap":
                active.store.complete_turn(active.current_session["id"],
                                           "My garden project is unfinished.", "Understood.")
                clock.advance()
                await automatic(active, provider)
            status = await active.handle("status", {})
            maintenance = status["maintenance"]
            stages = maintenance["stages"]
            details = [await active.handle("memory_continuity_run", {"task_id": row["task_id"]})
                       for row in stages]
            attempts = [attempt for detail in details for attempt in detail["attempts"]]
            # Parent stages also appear in the aggregate. Deduplicate by durable ID.
            attempts = list({attempt["id"]: attempt for attempt in attempts}.values())
            counts = Counter(attempt["outcome"] for attempt in attempts)
            expected_calls = {"configuration": 1, "split": 4, "retained_gap": 7,
                              "chronology": 6, "justified_no_change": 1}.get(scenario, 2)
            assert provider.calls == maintenance["model_calls"] == len(attempts) == expected_calls
            assert not dialogue.requests
            notes = active.store.continuity_state()["items"]
            unresolved = scenario in {"configuration", "retained_gap", "chronology"}
            assert maintenance["evidence"]["unresolved"] == int(unresolved)
            if scenario == "chronology":
                assert notes == before
                assert {a["reason"]["code"] for a in attempts} == {"validation.chronology"}
            elif scenario == "configuration":
                assert maintenance["unresolved"]["configuration_attention"] == 1
                assert not notes
            elif scenario == "justified_no_change":
                assert not notes and counts == {"committed": 1}
            else:
                assert len(notes) == (2 if scenario == "split" else 1)
                assert any("unfinished" in n["content"] if scenario == "retained_gap" else
                           "tomorrow" in n["content"] for n in notes)
            aggregate = {"scenario": scenario, "mode": "scripted", "samples": 1,
                "calls": provider.calls, "attempt_outcomes": dict(counts),
                "recovered_failure": bool(counts["failed"] and not unresolved),
                "justified_no_change": scenario == "justified_no_change",
                "evidence": maintenance["evidence"], "useful_notes": len(notes) if before is None else 0,
                "latency_seconds": time.perf_counter() - started,
                "usage_kind": "fabricated provider counters; accounting check only",
                "usage": {key: sum((a["usage"] or {}).get(key, 0) for a in attempts)
                          for key in ("prompt_tokens", "completion_tokens")},
                "attempts": [{"outcome": a["outcome"], "reason_code": (a["reason"] or {}).get("code"),
                              "elapsed_seconds": a["elapsed_seconds"], "usage": a["usage"]}
                             for a in attempts]}
            assert "PRIVATE" not in json.dumps(aggregate)
            record_property("maintenance_assessment", json.dumps(aggregate))
        finally:
            if active is not service:
                await active.close()
