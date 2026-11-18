"""Independent current-life changes through the maintenance service."""
import asyncio
import json
import sqlite3

import pytest

from test_ordinary_proposals import CompactProvider, refresh
from test_service import room


def admission(topic: str, source: str) -> dict:
    return {"op": "upsert", "topic": topic, "importance": "moderate",
            "category": "thread", "content": f"The user's {topic.lower()} remains unfinished.",
            "sources": [source], "change": {"kind": "development", "evidence": [source]}}


async def test_retry_after_restart_requests_only_the_unresolved_topic(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"],
                                    "My garden and quilt are both unfinished.", "Understood.")
        provider = CompactProvider(lambda doc: {"operations": [
            admission("Garden", doc["exchanges"][0]["source_refs"]["user"]),
            admission("Quilt", "s999"),
        ]}, adapt=False)
        state = (await refresh(service, client, provider))["continuity"]
        assert [item["topic"] for item in state["items"]] == ["Garden"]
        assert state["maintenance"]["evidence"] == {"processed": 0, "unresolved": 1}
        task_id = state["maintenance"]["latest_attempt"]["task_id"]
        before = await client.request("memory_continuity_run", task_id=task_id)
        assert before["status"] == "partial"
        assert [group["status"] for group in before["groups"]] == ["completed", "failed"]
        assert before["groups"][1]["reason"]["code"] == "validation.reference"

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        after = await client.request("memory_continuity_run", task_id=task_id)
        assert after["groups"] == before["groups"]
        provider = CompactProvider(lambda doc: {"operations": [
            admission("Quilt", doc["exchanges"][0]["source_refs"]["user"]),
        ]}, adapt=False)
        service.continuity_worker.provider = provider
        assert (await client.request("memory_retry", task_id=task_id))["refresh_queued"]
        if service._continuity_task is not None:
            await service._continuity_task
        assert len(provider.calls) == 1
        request = json.loads(provider.calls[0][-1]["content"])
        assert request["unresolved_group"]["topics"] == ["Quilt"]
        final = await client.request("memory_continuity_run", task_id=task_id)
        assert final["status"] == "completed"
        assert [group["id"] for group in final["groups"]] == [
            group["id"] for group in before["groups"]]
        assert len(final["changes"]) == 2
        assert (await client.request("status"))["maintenance"]["evidence"] == {
            "processed": 1, "unresolved": 0}


async def test_well_framed_same_target_conflict_cannot_save_either_change(tmp_path, monkeypatch):
    from test_current_life_policy import seed_note

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store)
        service.store.complete_turn(service.current_session["id"],
                                    "I added sketches to my project and started a garden.", "Okay.")

        def respond(doc):
            source = doc["exchanges"][0]["source_refs"]["user"]
            update = {**admission(note["topic"], source), "note_ref": doc["items"][0]["note_ref"]}
            return {"operations": [update, {**update, "content": "The project has sketches."},
                                   admission("Garden", source)]}

        await refresh(service, client, CompactProvider(respond, adapt=False))
        assert service.store.get_continuity_item(note["id"]) == note
        assert service.store.continuity_state()["active_count"] == 2
        status = (await client.request("status"))["maintenance"]
        run = await client.request("memory_continuity_run", task_id=status["latest_attempt"]["task_id"])
        assert run["status"] == "partial"
        assert [group["status"] for group in run["groups"]] == ["failed", "completed"]
        assert run["groups"][0]["reason"]["code"] == "validation.duplicate_operation"


async def test_whole_response_shape_failure_never_prepares_groups(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "I started a garden.", "Okay.")
        provider = CompactProvider(lambda doc: {"operations": [
            admission("Garden", doc["exchanges"][0]["source_refs"]["user"]),
            {"op": "upsert", "topic": "Quilt"},
        ]}, adapt=False)
        state = (await refresh(service, client, provider))["continuity"]
        assert not state["items"]
        run = await client.request("memory_continuity_run", task_id=
                                   state["maintenance"]["latest_attempt"]["task_id"])
        assert run["status"] == "failed" and not run["groups"]
        assert state["maintenance"]["evidence"] == {"processed": 0, "unresolved": 1}


@pytest.mark.parametrize("commits", [0, 1, 2])
@pytest.mark.parametrize("restart", ["cancel", "process_loss"])
async def test_restart_resumes_prepared_groups_without_dispatch_or_duplicate_write(
    tmp_path, monkeypatch, commits, restart,
):
    resume_dir = tmp_path
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"],
                                    "I started a garden and a quilt.", "Okay.")
        await client.request("save_preferences", preferences={"continuity_recovery_calls": 1})
        provider = CompactProvider(lambda doc: {"operations": [
            admission(topic, doc["exchanges"][0]["source_refs"]["user"])
            for topic in ("Garden", "Quilt")
        ]}, adapt=False)
        boundary = asyncio.Event()
        actual_commit = service.continuity_worker.commit_result
        completed = 0

        async def commit_at_boundary(*args, **kwargs):
            nonlocal completed
            if completed == commits:
                boundary.set()
                await asyncio.Event().wait()
            await actual_commit(*args, **kwargs)
            completed += 1
            if completed == 2:
                boundary.set()
                await asyncio.Event().wait()

        service.continuity_worker.provider = provider
        service.continuity_worker.commit_result = commit_at_boundary
        assert (await client.request("memory_refresh"))["refresh_queued"]
        await asyncio.wait_for(boundary.wait(), timeout=5)
        task = service._continuity_task
        if restart == "process_loss":
            from contextlib import closing

            # Capture the durable database before the original worker can run
            # cancellation cleanup, including any still-running request row.
            resume_dir = tmp_path / "restarted"
            db_path = resume_dir / service.store.path.relative_to(tmp_path)
            db_path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db_path)) as snapshot:
                service.store.db.backup(snapshot)
        service.continuity_worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        before = (await client.request("status"))["maintenance"]
        assert before["model_calls"] == 1
        assert len(service.store.continuity_history()) == commits
        assert before["evidence"]["processed"] == int(commits == 2)
        task_id = before["latest_attempt"]["task_id"]

    async with room(resume_dir, monkeypatch) as (service, client, _, _):
        interrupted = (await client.request("status"))["maintenance"]["latest_attempt"]
        assert interrupted["outcome"] == ("committed" if commits == 2 else "interrupted")
        if commits < 2:
            await refresh(service, client, provider)
        run = await client.request("memory_continuity_run", task_id=task_id)
        assert run["status"] == "completed"
        assert run["budget"]["used"] == run["budget"]["pass"] == 1
        assert len(provider.calls) == 1
        assert len(run["changes"]) == 2 and all(change["created"] for change in run["changes"])
        assert (await client.request("status"))["maintenance"]["evidence"] == {
            "processed": 1, "unresolved": 0}
        for group in run["groups"]:
            service.store.commit_continuity(
                task_id, [{"op": "no_change"}], expected_revision=0, control_revision=0,
                group_id=group["id"],
            )
        assert len(service.store.continuity_history()) == 2


@pytest.mark.parametrize("mutation", ["locked", "withdrawn", "none"])
async def test_current_state_and_sources_are_revalidated_for_each_group(tmp_path, monkeypatch, mutation):
    from test_current_life_policy import seed_note

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        note = seed_note(service.store)
        await client.request("save_preferences", preferences={"continuity_max_notes": 2})
        for kind in ("Anniversary", "Garden"):
            service.store.complete_turn(service.current_session["id"],
                                        f"My {kind.lower()} is coming up.", "Okay.")

        def respond(doc):
            sources = {topic: row["source_refs"]["user"]
                       for topic, row in zip(("Anniversary", "Garden"), doc["exchanges"], strict=True)}
            ref = doc["items"][0]["note_ref"]
            archive = {"op": "archive", "note_ref": ref, "archive_reason": "replaced",
                       "decision_reason": "The anniversary matters more.", "sources": []}
            replace = {**admission("Anniversary", sources["Anniversary"]), "importance": "high",
                       "replaces_ref": ref, "decision_reason": "The anniversary matters more."}
            if mutation == "locked":
                # This affects only the replacement's target, despite changing
                # the scope and control revisions while inference is in flight.
                service.store.edit_continuity_item(
                    note["id"], {"locked": True}, expected_revision=note["revision"])
            elif mutation == "withdrawn":
                # No public API revokes eligibility for a new chat exchange.
                exchange_id = doc["exchanges"][0]["exchange_id"]
                with service.store.db:
                    service.store.db.execute(
                        "UPDATE generations SET memory_eligible=0 WHERE id="
                        "(SELECT generation_id FROM messages WHERE id=?)", (exchange_id,))
            return {"operations": [archive, replace, admission("Garden", sources["Garden"])]}

        await refresh(service, client, CompactProvider(respond, adapt=False))
        status = (await client.request("status"))["maintenance"]
        run = await client.request("memory_continuity_run", task_id=status["latest_attempt"]["task_id"])
        notes = service.store.list_continuity_items(include_inactive=False)
        if mutation == "none":
            assert {item["topic"] for item in notes} == {"Anniversary", "Garden"}
            assert run["status"] == "completed" and len(run["changes"]) == 3
        else:
            assert {item["topic"] for item in notes} == {note["topic"], "Garden"}
            assert run["status"] == "partial" and len(run["changes"]) == 1
            assert run["groups"][0]["reason"]["code"] == (
                "validation.revision" if mutation == "locked" else "validation.evidence_changed")
            assert status["evidence"] == {"processed": 2, "unresolved": 1}


async def test_independent_admissions_recheck_the_last_slot(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        await client.request("save_preferences", preferences={"continuity_max_notes": 1})
        service.store.complete_turn(service.current_session["id"],
                                    "I started a garden and a quilt.", "Okay.")
        provider = CompactProvider(lambda doc: {"operations": [
            admission(topic, doc["exchanges"][0]["source_refs"]["user"])
            for topic in ("Garden", "Quilt")
        ]}, adapt=False)
        state = (await refresh(service, client, provider))["continuity"]
        assert [item["topic"] for item in state["items"]] == ["Garden"]
        run = await client.request("memory_continuity_run", task_id=
                                   state["maintenance"]["latest_attempt"]["task_id"])
        assert run["status"] == "partial"
        assert run["groups"][1]["reason"]["code"] == "validation.capacity"


async def test_group_correction_cannot_claim_another_topic_or_bypass_durable_plan(tmp_path, monkeypatch):
    from eidolon.memory.continuity import ContinuityValidationError

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"],
                                    "I started a garden and a quilt.", "Okay.")
        initial = CompactProvider(lambda doc: {"operations": [
            admission("Garden", doc["exchanges"][0]["source_refs"]["user"]),
            admission("Quilt", "s999"),
        ]}, adapt=False)
        state = (await refresh(service, client, initial))["continuity"]
        task_id = state["maintenance"]["latest_attempt"]["task_id"]
        with pytest.raises(ContinuityValidationError, match="commit individually"):
            service.store.commit_continuity(
                task_id, [{"op": "no_change", "expected_revision": state["revision"]}],
                expected_revision=state["revision"], control_revision=state["control_revision"],
            )
        correction = CompactProvider(lambda doc: {"operations": [
            admission("Unrelated", doc["exchanges"][0]["source_refs"]["user"]),
        ]}, adapt=False)
        state = (await refresh(service, client, correction))["continuity"]
        assert [item["topic"] for item in state["items"]] == ["Garden"]
        run = await client.request("memory_continuity_run", task_id=task_id)
        assert run["status"] == "partial"
        assert run["groups"][1]["reason"]["code"] == "validation.conflict"
        assert state["maintenance"]["evidence"] == {"processed": 0, "unresolved": 1}


async def test_v16_migration_preserves_indivisible_runs_and_budget(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"], "Good morning.", "Hello.")
        state = (await refresh(service, client, CompactProvider(
            lambda doc: {"operations": [{"op": "no_change"}]})))["continuity"]
        complete = state["maintenance"]["latest_attempt"]["task_id"]
        service.store.complete_turn(service.current_session["id"], "I started a garden.", "Okay.")
        state = (await refresh(service, client, CompactProvider(
            lambda doc: {"operations": []})))["continuity"]
        failed = state["maintenance"]["latest_attempt"]["task_id"]
        runs = [await client.request("memory_continuity_run", task_id=task_id)
                for task_id in (complete, failed)]
        with service.store.db:
            service.store.db.execute("DROP TABLE maintenance_groups")
            service.store.db.execute("PRAGMA user_version=16")

    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        assert ".pre-v17-" in service.store.migration_backup_path.name
        for before in runs:
            after = await client.request("memory_continuity_run", task_id=before["task_id"])
            assert after["status"] == before["status"]
            assert after["budget"] == before["budget"]
            assert after["attempts"] == before["attempts"]
            assert after["groups"] == []
        assert (await client.request("status"))["maintenance"]["evidence"] == {
            "processed": 1, "unresolved": 1}


@pytest.mark.parametrize("reply", ["reference_correction", "no_change"])
async def test_retry_resolves_an_unknown_reference_by_topic_or_keeps_the_gap(tmp_path, monkeypatch, reply):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        service.store.complete_turn(service.current_session["id"],
                                    "I started a garden and a quilt.", "Okay.")
        initial = CompactProvider(lambda doc: {"operations": [
            admission("Garden", doc["exchanges"][0]["source_refs"]["user"]),
            {**admission("Quilt", doc["exchanges"][0]["source_refs"]["user"]), "note_ref": "n999"},
        ]}, adapt=False)
        state = (await refresh(service, client, initial))["continuity"]
        task_id = state["maintenance"]["latest_attempt"]["task_id"]

        def correct(doc):
            assert doc["unresolved_group"]["topics"] == ["Quilt"]
            assert "n999" not in json.dumps(doc["unresolved_group"])
            return {"operations": [{"op": "no_change"}] if reply == "no_change" else [
                admission("Quilt", doc["exchanges"][0]["source_refs"]["user"]),
            ]}

        await refresh(service, client, CompactProvider(correct, adapt=False))
        run = await client.request("memory_continuity_run", task_id=task_id)
        if reply == "no_change":
            assert run["status"] == "partial"
            assert run["groups"][1]["reason"]["code"] == "validation.group_deferred"
            assert (await client.request("status"))["maintenance"]["evidence"]["unresolved"] == 1
        else:
            assert run["status"] == "completed"
            assert len(run["changes"]) == 2


async def test_correction_cannot_substitute_a_different_exchange(tmp_path, monkeypatch):
    async with room(tmp_path, monkeypatch) as (service, client, _, _):
        for text in ("I started a quilt.", "I started a garden."):
            service.store.complete_turn(service.current_session["id"], text, "Okay.")

        def initial(doc):
            quilt, garden = [row["source_refs"]["user"] for row in doc["exchanges"]]
            return {"operations": [
                {**admission("Quilt", quilt), "sources": [quilt, "s999"]},
                admission("Garden", garden),
            ]}

        state = (await refresh(service, client, CompactProvider(initial, adapt=False)))["continuity"]
        task_id = state["maintenance"]["latest_attempt"]["task_id"]

        def substitute(doc):
            quilt, garden = [row["source_refs"]["user"] for row in doc["exchanges"]]
            assert [quilt, doc["exchanges"][0]["source_refs"]["assistant"]] in (
                doc["unresolved_group"]["changes"][0]["required_sources"])
            return {"operations": [admission("Quilt", garden)]}

        await refresh(service, client, CompactProvider(substitute, adapt=False))
        run = await client.request("memory_continuity_run", task_id=task_id)
        assert run["status"] == "partial"
        assert run["groups"][0]["reason"]["code"] == "validation.conflict"
        assert len(run["changes"]) == 1
