"""Scripted semantic decisions through refresh; these do not measure live judgment."""

import pytest

from test_current_life_policy import seed_note
from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


@pytest.mark.parametrize(
    "text",
    [
        "It now needs a larger vehicle.",
        "Relocating now needs a larger vehicle. Can you explain telescopes?",
        "It now needs a larger vehicle. Perhaps the weather will improve?",
        "It now needs a larger vehicle. In our story, a dragon drives a truck.",
    ],
)
async def test_designated_development_accepts_paraphrases_and_independent_asides(
    tmp_path,
    monkeypatch,
    text,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(service.current_session["id"], text, "Understood.")

        def respond(doc):
            source = doc["exchanges"][0]["source_refs"]["user"]
            return {
                "operations": [
                    {
                        "op": "upsert",
                        "note_ref": doc["items"][0]["note_ref"],
                        "topic": "Move",
                        "importance": "moderate",
                        "category": "thread",
                        "content": "The user's move now needs a larger vehicle.",
                        "sources": [source],
                        "change": {"kind": "development", "evidence": [source]},
                    }
                ]
            }

        await refresh(service, client, CompactProvider(respond))
        saved = service.store.get_continuity_item(note["id"])
        assert saved["content"] == "The user's move now needs a larger vehicle."
        assert saved["sources"][0]["excerpt"] == text
        assert service.store.pending_maintenance_task() is None


@pytest.mark.parametrize("closed", [False, True])
async def test_old_background_enriches_history_without_replacing_or_reopening(
    tmp_path, monkeypatch, closed
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        if closed:
            service.store.complete_turn(
                service.current_session["id"], "It's all wrapped up!", "Okay."
            )
            await refresh(
                service,
                client,
                CompactProvider(
                    lambda doc: {
                        "operations": [
                            {
                                "op": "archive",
                                "note_ref": doc["items"][0]["note_ref"],
                                "content": "The user's move has concluded.",
                                "archive_reason": "completed",
                                "decision_reason": "Confirmed conclusion.",
                                "sources": ["s1"],
                                "change": {
                                    "kind": "closure",
                                    "evidence": ["s1"],
                                    "intent": "The move concluded.",
                                },
                            }
                        ]
                    }
                ),
            )
            note = service.store.get_continuity_item(note["id"])
        service.store.complete_turn(
            service.current_session["id"],
            "Grandmother built those boxes.",
            "Okay.",
            metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "upsert",
                            "note_ref": doc["items"][0]["note_ref"],
                            "topic": note["topic"],
                            "importance": note["importance"],
                            "category": note["category"],
                            "content": note["content"],
                            "sources": ["s1"],
                            "change": {
                                "kind": "background",
                                "context": "The boxes were built by the user's grandmother.",
                            },
                        }
                    ]
                }
            ),
        )
        saved = service.store.get_continuity_item(note["id"])
        for field in ("content", "state", "observed_at", "sources"):
            assert saved[field] == note[field]
        assert [entry["content"] for entry in saved["historical_context"]] == [
            "The boxes were built by the user's grandmother."
        ]
        assert service.store.pending_maintenance_task() is None
        associations = (await client.request("memory_continuity_item", item_id=note["id"]))[
            "evidence_associations"
        ]
        assert any(row["disposition"] == "background" for row in associations)


@pytest.mark.parametrize("kind", ["correction", "defer"])
async def test_ambiguous_consequential_decision_stays_visible_without_retry_loop(
    tmp_path, monkeypatch, kind
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"], "That wasn't quite what I meant.", "Okay."
        )

        def respond(doc):
            ref = doc["items"][0]["note_ref"]
            if kind == "defer":
                return {
                    "operations": [
                        {"op": "defer", "reason": "semantic_uncertainty", "note_ref": ref}
                    ]
                }
            return {
                "operations": [
                    {
                        "op": "upsert",
                        "note_ref": ref,
                        "topic": note["topic"],
                        "importance": note["importance"],
                        "category": note["category"],
                        "content": "The user's move needs reconsideration.",
                        "sources": ["s1"],
                        "change": {
                            "kind": "correction",
                            "evidence": ["s1"],
                            "intent": "Potential correction.",
                            "temporal": "unresolved",
                        },
                    }
                ]
            }

        provider = CompactProvider(respond)
        await refresh(service, client, provider)
        assert service.store.get_continuity_item(note["id"]) == note
        task = service.store.pending_maintenance_task()
        stage = task["stages"]["continuity"]
        assert stage["reason"]["code"] == "validation.semantic_uncertainty"
        assert stage["reason"]["disposition"] == "defer"
        assert len(provider.calls) == 1


@pytest.mark.parametrize("closure", [False, True])
async def test_chronology_uses_designated_claim_evidence_and_keeps_old_background(
    tmp_path, monkeypatch, closure
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        for text, timestamp in [
            ("Those boxes were a gift.", "2020-01-01T00:00:00+00:00"),
            (
                "It's all wrapped up!" if closure else "It needs storage now.",
                "2090-01-01T00:00:00+00:00",
            ),
        ]:
            service.store.complete_turn(
                service.current_session["id"],
                text,
                "Okay.",
                metadata={"user_message_at": timestamp},
            )

        def respond(doc):
            refs = [row["source_refs"]["user"] for row in doc["exchanges"]]
            op = {
                "op": "upsert",
                "note_ref": doc["items"][0]["note_ref"],
                "topic": note["topic"],
                "importance": note["importance"],
                "category": note["category"],
                "content": "The user's move has concluded."
                if closure
                else "The user's move now needs storage.",
                "sources": refs,
                "change": {"kind": "development", "evidence": [refs[1]]},
            }
            if closure:
                op.pop("topic")
                op.pop("importance")
                op.pop("category")
                op.update(
                    op="archive", archive_reason="completed", decision_reason="Confirmed closure."
                )
                op["change"].update(kind="closure", intent="Confirmed move completion.")
            return {"operations": [op]}

        await refresh(service, client, CompactProvider(respond))
        saved = service.store.get_continuity_item(note["id"])
        assert saved["observed_at"] == "2090-01-01T00:00:00+00:00"
        assert saved["state"] == ("archived" if closure else "open")
        assert any(source["excerpt"] == "Those boxes were a gift." for source in saved["sources"])
        assert service.store.pending_maintenance_task() is None


async def test_newer_unrelated_citation_cannot_lend_its_clock_to_designated_old_closure(
    tmp_path, monkeypatch
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        for text, timestamp in [
            ("It's done.", "2020-01-01T00:00:00+00:00"),
            ("My greenhouse needs a roof.", "2090-01-01T00:00:00+00:00"),
        ]:
            service.store.complete_turn(
                service.current_session["id"],
                text,
                "Okay.",
                metadata={"user_message_at": timestamp},
            )
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "archive",
                            "note_ref": doc["items"][0]["note_ref"],
                            "archive_reason": "completed",
                            "decision_reason": "Closure was reported.",
                            "content": "The user's move concluded.",
                            "sources": [row["source_refs"]["user"] for row in doc["exchanges"]],
                            "change": {
                                "kind": "closure",
                                "evidence": ["s1"],
                                "intent": "Reported move closure.",
                            },
                        }
                    ]
                }
            ),
        )
        assert service.store.get_continuity_item(note["id"]) == note
        stage = service.store.pending_maintenance_task()["stages"]["continuity"]
        assert stage["reason"]["code"] == "validation.chronology"


@pytest.mark.parametrize("kind", ["correction", "reopening"])
async def test_deliberate_correction_or_reopening_requires_declared_intent(
    tmp_path, monkeypatch, kind
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        if kind == "reopening":
            service.store.complete_turn(
                service.current_session["id"], "The move is complete.", "Okay."
            )
            await refresh(
                service,
                client,
                CompactProvider(
                    lambda doc: {
                        "operations": [
                            {
                                "op": "archive",
                                "note_ref": doc["items"][0]["note_ref"],
                                "archive_reason": "completed",
                                "decision_reason": "Confirmed move completion.",
                                "content": "The user's move is complete.",
                                "sources": ["s1"],
                                "change": {
                                    "kind": "closure",
                                    "evidence": ["s1"],
                                    "intent": "Confirmed completion.",
                                },
                            }
                        ]
                    }
                ),
            )
        service.store.complete_turn(
            service.current_session["id"], "Actually the move needs another delivery.", "Okay."
        )

        def respond(doc):
            change = {
                "kind": kind,
                "evidence": ["s1"],
                "intent": "The user explicitly revised the move's status.",
            }
            if kind == "correction":
                change["temporal"] = "ordered"
            return {
                "operations": [
                    {
                        "op": "upsert",
                        "note_ref": doc["items"][0]["note_ref"],
                        "topic": "Move",
                        "importance": "low",
                        "category": "thread",
                        "content": "The user's move needs another delivery.",
                        "sources": ["s1"],
                        "change": change,
                    }
                ]
            }

        await refresh(service, client, CompactProvider(respond))
        saved = service.store.get_continuity_item(note["id"])
        assert saved["state"] == "open" and saved["change_kind"] == kind
        assert saved["content"] == "The user's move needs another delivery."
        assert service.store.pending_maintenance_task() is None


@pytest.mark.parametrize(
    "text",
    [
        "It's all wrapped up!",
        "It's all wrapped up! Can you explain telescopes?",
        "It's all wrapped up! Perhaps the weather will improve.",
        "It's all wrapped up! In our story a dragon drives a truck.",
    ],
)
async def test_closure_uses_explicit_semantic_decision_in_full_mixed_context(
    tmp_path, monkeypatch, text
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(service.current_session["id"], text, "Okay.")
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "archive",
                            "note_ref": doc["items"][0]["note_ref"],
                            "archive_reason": "completed",
                            "decision_reason": "The user confirmed the move concluded.",
                            "content": "The user's move concluded.",
                            "sources": ["s1"],
                            "change": {
                                "kind": "closure",
                                "evidence": ["s1"],
                                "intent": "Explicit move conclusion.",
                            },
                        }
                    ]
                }
            ),
        )
        saved = service.store.get_continuity_item(note["id"])
        assert saved["state"] == "archived"
        assert saved["sources"][-1]["excerpt"] == text
        assert service.store.pending_maintenance_task() is None


@pytest.mark.parametrize(
    "text,content",
    [
        (
            "My brother says he might relocate. He hasn't decided.",
            "The user's brother may relocate; he has not decided.",
        ),
        (
            "In our story my character considers a move. This is only fiction.",
            "The user is considering a fictional character's move, not a real relocation.",
        ),
    ],
)
async def test_scripted_summary_preserves_related_qualifiers_and_attribution(
    tmp_path, monkeypatch, text, content
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], text, "Okay.")
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "upsert",
                            "topic": "Discussing a move",
                            "category": "thread",
                            "importance": "low",
                            "content": content,
                            "sources": ["s1"],
                            "change": {"kind": "development", "evidence": ["s1"]},
                        }
                    ]
                }
            ),
        )
        saved = service.store.continuity_state()["items"][0]
        assert saved["content"] == content and saved["sources"][0]["excerpt"] == text
        assert saved.get("promise_owner") is None


async def test_shared_long_words_do_not_force_a_scripted_semantic_update(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Interview", text="My interview is ongoing.")
        service.store.complete_turn(
            service.current_session["id"],
            "The interview documentary describes astrophysics.",
            "Okay.",
        )
        await refresh(
            service, client, CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        )
        assert service.store.get_continuity_item(note["id"]) == note
        assert service.store.pending_maintenance_task() is None


async def test_v1_pending_task_resumes_with_new_decision_and_preserved_accounting(
    tmp_path, monkeypatch
):
    import json

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "My move is ongoing.", "Okay.")
        # Simulate a prior-version pending batch/attempt. No old output is translated.
        await refresh(service, client, CompactProvider(lambda doc: {"operations": []}))
        task = service.store.pending_maintenance_task()
        original_inputs = task["inputs"]
        task_id = task["id"]
        before = service.store.get_maintenance_stage(task_id)["budget"]
        with service.store.db:
            for row in service.store.db.execute(
                "SELECT id,provenance FROM maintenance_attempts"
            ).fetchall():
                provenance = json.loads(row["provenance"])
                provenance["contract"] = "current-life-ordinary-v1"
                service.store.db.execute(
                    "UPDATE maintenance_attempts SET provenance=? WHERE id=?",
                    (json.dumps(provenance), row["id"]),
                )
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        assert service.store.pending_maintenance_task()["inputs"] == original_inputs
        assert service.store.get_maintenance_stage(task_id)["budget"] == before

        def respond(doc):
            assert doc["contract"] == "current-life-ordinary-v2"
            return {
                "operations": [
                    {
                        "op": "upsert",
                        "topic": "Move",
                        "importance": "low",
                        "category": "thread",
                        "content": "The user's move is ongoing.",
                        "sources": ["s1"],
                        "change": {"kind": "development", "evidence": ["s1"]},
                    }
                ]
            }

        await refresh(service, client, CompactProvider(respond, adapt=False))
        assert service.store.pending_maintenance_task() is None
        details = await client.request("memory_continuity_run", task_id=task_id)
        assert {attempt["contract"] for attempt in details["attempts"]} == {
            "current-life-ordinary-v1",
            "current-life-ordinary-v2",
        }
        assert details["budget"]["pass"] == before["pass"] + 1
        assert details["budget"]["used"] == 1  # Explicit refresh starts one bounded pass.
        assert len(details["attempts"]) == 2


@pytest.mark.parametrize("failure", ["missing", "foreign", "closure_without_intent"])
async def test_new_contract_never_guesses_intent_or_borrows_uncited_evidence(
    tmp_path, monkeypatch, failure
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"], "The move needs storage.", "Okay."
        )

        def respond(doc):
            op = {
                "op": "upsert",
                "note_ref": doc["items"][0]["note_ref"],
                "topic": "Move",
                "importance": "low",
                "category": "thread",
                "content": "The user's move needs storage.",
                "sources": ["s1"],
            }
            if failure == "foreign":
                op["change"] = {"kind": "development", "evidence": ["s2"]}
            elif failure == "closure_without_intent":
                op["change"] = {"kind": "closure", "evidence": ["s1"]}
            return {"operations": [op]}

        await refresh(service, client, CompactProvider(respond, adapt=False))
        assert service.store.get_continuity_item(note["id"]) == note
        assert service.store.pending_maintenance_task() is not None


@pytest.mark.parametrize("failure", ["missing", "uncited", "clipped", "old", "wrong_kind", "policy_change"])
def test_direct_commit_rechecks_change_contract_and_rolls_back(tmp_path, failure):
    from contextlib import closing

    from eidolon.memory.continuity import ContinuityValidationError
    from eidolon.storage import Store
    from test_continuity_narrative import add, narrative

    with closing(Store(tmp_path / "room.sqlite3")) as store:
        note = seed_note(store, topic="Move", text="My move is ongoing.")
        source = add(
            store,
            "The move needs another vehicle.",
            metadata={
                "user_message_at": "2020-01-01T00:00:00+00:00"
                if failure == "old"
                else "2090-01-01T00:00:00+00:00"
            },
        )
        state = store.continuity_state()
        task = store.create_maintenance_task(
            [source],
            expected_revision=state["revision"],
            control_revision=state["control_revision"],
        )
        update = narrative(
            source,
            "The user's move needs another vehicle.",
            topic="Move",
            item_id=note["id"],
            expected_revision=note["revision"],
        )
        update["sources"][0]["excerpt"] = source["user"]
        if failure == "missing":
            update.pop("change")
        elif failure == "uncited":
            update["change"]["evidence"][0]["role"] = "assistant"
            update["change"]["evidence"][0]["origin"] = "assistant_statement"
        elif failure == "clipped":
            update["change"]["evidence"][0]["excerpt"] = "needs another vehicle."
        elif failure == "wrong_kind":
            update["change"] = {"kind": "background", "context": "Attempted replacement."}
        if failure == "policy_change":
            update.update(op="archive", sources=[], archive_reason="replaced",
                          decision_reason="Keep the more important transport topic.",
                          change={"kind": "background", "context": "Cannot bypass allocation."})
            update.pop("content")
        admission = narrative(source, "The user needs transport.", topic="Transport")
        if failure == "policy_change":
            admission.update(importance="high", replaces_item_id=note["id"],
                             decision_reason="Transport is more consequential.")
        admission["sources"][0]["excerpt"] = source["user"]
        with pytest.raises(ContinuityValidationError):
            store.commit_continuity(
                task["id"],
                [admission, update],
                expected_revision=state["revision"],
                control_revision=state["control_revision"],
            )
        assert store.continuity_state() == state
        assert len(store.continuity_history()) == 1
        assert store.pending_maintenance_task()["id"] == task["id"]


@pytest.mark.parametrize("mutation", ["ineligible", "changed", "retracted"])
async def test_background_context_is_withheld_when_its_original_is_no_longer_usable(
    tmp_path, monkeypatch, mutation
):
    import json

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"], "Those boxes came from a grandparent.", "Okay."
        )
        background = service.store.recent_continuity_inputs(limit=1)[0]
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "upsert",
                            "note_ref": doc["items"][0]["note_ref"],
                            "topic": "Move",
                            "importance": note["importance"],
                            "category": note["category"],
                            "content": note["content"],
                            "sources": ["s1"],
                            "change": {
                                "kind": "background",
                                "context": "The boxes came from a grandparent.",
                            },
                        }
                    ]
                }
            ),
        )
        with service.store.db:
            if mutation == "ineligible":
                service.store.db.execute(
                    "UPDATE generations SET memory_eligible=0 WHERE id=?",
                    (background["generation_id"],),
                )
            elif mutation == "changed":
                service.store.db.execute(
                    "UPDATE messages SET content='Changed original.' WHERE id=?",
                    (background["exchange_id"],),
                )
            else:
                service.store.db.execute(
                    "UPDATE continuity_support SET disposition='retracted' WHERE exchange_id=?",
                    (background["exchange_id"],),
                )
        saved = await client.request("memory_continuity_item", item_id=note["id"])
        assert saved["content"] == note["content"] and not saved["invalidated"]
        assert saved.get("historical_context", []) == []
        assert saved["unresolved_historical_context"] == 1
        service.store.complete_turn(
            service.current_session["id"], "A new unrelated discussion.", "Okay."
        )
        provider = CompactProvider(lambda doc: {"operations": [{"op": "no_change"}]})
        await refresh(service, client, provider)
        doc = json.loads(provider.calls[0][-1]["content"])
        assert "grandparent" not in json.dumps(doc["items"])


async def test_new_state_observation_does_not_regress_a_later_background_mention(
    tmp_path, monkeypatch
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"],
            "Those boxes came from a grandparent.",
            "Okay.",
            metadata={"user_message_at": "2090-01-01T00:00:00+00:00"},
        )
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "upsert",
                            "note_ref": doc["items"][0]["note_ref"],
                            "topic": "Move",
                            "importance": note["importance"],
                            "category": note["category"],
                            "content": note["content"],
                            "sources": ["s1"],
                            "change": {
                                "kind": "background",
                                "context": "The boxes came from a grandparent.",
                            },
                        }
                    ]
                }
            ),
        )
        service.store.complete_turn(
            service.current_session["id"],
            "It needs another delivery.",
            "Okay.",
            metadata={"user_message_at": "2080-01-01T00:00:00+00:00"},
        )
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "upsert",
                            "note_ref": doc["items"][0]["note_ref"],
                            "topic": "Move",
                            "importance": note["importance"],
                            "category": note["category"],
                            "content": "The user's move needs another delivery.",
                            "sources": ["s1"],
                            "change": {"kind": "development", "evidence": ["s1"]},
                        }
                    ]
                }
            ),
        )
        saved = service.store.get_continuity_item(note["id"])
        assert saved["observed_at"] == "2080-01-01T00:00:00+00:00"
        assert saved["last_mention_at"] == "2090-01-01T00:00:00+00:00"


@pytest.mark.parametrize("withdraw", [False, True])
async def test_new_development_can_carry_linked_history_only_while_original_remains_eligible(
    tmp_path,
    monkeypatch,
    withdraw,
):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store, topic="Move", text="My move is ongoing.")
        service.store.complete_turn(
            service.current_session["id"],
            "The boxes were built by my grandmother. She kept them.",
            "Okay.",
            metadata={"user_message_at": "2020-01-01T00:00:00+00:00"},
        )
        background = service.store.recent_continuity_inputs(limit=1)[0]
        await refresh(
            service,
            client,
            CompactProvider(
                lambda doc: {
                    "operations": [
                        {
                            "op": "upsert",
                            "note_ref": doc["items"][0]["note_ref"],
                            "topic": "Move",
                            "importance": note["importance"],
                            "category": note["category"],
                            "content": note["content"],
                            "sources": ["s1"],
                            "change": {
                                "kind": "background",
                                "context": "The grandmother built and kept the boxes.",
                            },
                        }
                    ]
                }
            ),
        )
        before = service.store.get_continuity_item(note["id"])
        service.store.complete_turn(
            service.current_session["id"],
            "The move needs another delivery of those boxes.",
            "Okay.",
        )

        def respond(doc):
            history = doc["items"][0]["historical_context"][0]
            retained = history["sources"][0]["source_ref"]
            original = next(
                row
                for row in doc["supporting_exchanges"]
                if row["exchange_id"] == background["exchange_id"]
            )
            assert (
                original["user_content"] == "The boxes were built by my grandmother. She kept them."
            )
            assert original["source_refs"]["user"] == retained
            if withdraw:
                with service.store.db:
                    service.store.db.execute(
                        "UPDATE generations SET memory_eligible=0 WHERE id=?",
                        (background["generation_id"],),
                    )
            return {
                "operations": [
                    {
                        "op": "upsert",
                        "note_ref": doc["items"][0]["note_ref"],
                        "topic": "Move",
                        "importance": note["importance"],
                        "category": note["category"],
                        "content": "The user's move needs another delivery of the boxes their grandmother built.",
                        "sources": ["s1"],
                        "retained_sources": [retained],
                        "change": {"kind": "development", "evidence": ["s1"]},
                    }
                ]
            }

        await refresh(service, client, CompactProvider(respond))
        saved = service.store.get_continuity_item(note["id"])
        if withdraw:
            assert saved["content"] == before["content"]
            assert saved["revision"] == before["revision"]
            assert service.store.pending_maintenance_task() is not None
        else:
            assert "another delivery" in saved["content"]
            assert "The boxes were built by my grandmother. She kept them." in [
                source["excerpt"] for source in saved["sources"]
            ]
            assert service.store.pending_maintenance_task() is None
