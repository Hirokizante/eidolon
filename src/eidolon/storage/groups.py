"""Durable ordinary-maintenance group plans and source obligations."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from ..memory.continuity import ContinuityValidationError, topic_key
from ..memory.diagnostics import read_reason, safe_reason
from ..memory.proposals import GroupProposal

if TYPE_CHECKING:
    from .store import Store


def load(store: Store, task_id: str) -> list[dict]:
    return [
        {**dict(row), **{key: json.loads(row[key]) if row[key] is not None else None
                         for key in ("positions", "obligation", "operations", "reason",
                                     "execution", "metadata", "metadata_outcomes")}}
        for row in store.db.execute(
            "SELECT * FROM maintenance_groups WHERE task_id=? ORDER BY position", (task_id,),
        )
    ]


def _change_identity(operation: dict) -> tuple:
    return (operation["op"], operation.get("target_id") or operation.get("item_id"),
            topic_key(operation.get("topic") or "") if not (
                operation.get("target_id") or operation.get("item_id")) else "",
            operation.get("replaces_item_id"),
            bool(operation.get("policy_archive") or operation.get("archive_reason") == "replaced"))


def check_correction(operations: list[dict], obligation: dict) -> None:
    if obligations := obligation["changes"]:
        if len(operations) == 1 and operations[0]["op"] == "no_change":
            raise ContinuityValidationError("The change obligation remains open", code="group_deferred")
        required: dict[tuple, set[int]] = {}
        for change in obligations:
            required.setdefault(_change_identity(change), set()).update(change["source_ids"])
        proposed = {_change_identity(operation): operation for operation in operations}
        if (proposed.keys() == required.keys() and len(operations) == len(proposed)
                and all(required[key] <= {source["exchange_id"] for source in operation["sources"]}
                        for key, operation in proposed.items())):
            return
    elif len(operations) == 1 and operations[0]["op"] == "no_change":
        return
    raise ContinuityValidationError("Correction changes the saved obligations", code="conflict")


def save(store: Store, task_id: str, proposals: list[GroupProposal], *,
         attempt_id: str | None, group_id: str | None = None) -> None:
    from .store import utc_now

    with store._lock, store.db:
        store.db.execute("BEGIN IMMEDIATE")
        task = store._maintenance_task(task_id)
        if task["stages"]["continuity"]["status"] not in {"pending", "failed"}:
            raise ContinuityValidationError("Maintenance stage is closed", code="conflict")
        existing = load(store, task_id)
        if existing and group_id is None:
            return
        attempt = store._check_continuity_execution(task_id, attempt_id)
        execution = {key: attempt["provenance"][key] for key in (
            "routing_revision", "route_fingerprint"
        )} if attempt else None
        if group_id is not None:
            saved = next((row for row in existing if row["id"] == group_id), None)
            if saved is None or len(proposals) != 1:
                raise ContinuityValidationError("Unknown maintenance group", code="conflict")
            if saved["status"] != "failed":
                return
            if saved["attempt_id"] != attempt_id:
                raise ContinuityValidationError("Correction is obsolete", code="conflict")
            proposal = proposals[0]
            if proposal.operations is None or len(proposal.operations) > len(saved["positions"]):
                raise ContinuityValidationError("Correction exceeds the original group", code="conflict")
            check_correction(proposal.operations, saved["obligation"])
            store.db.execute(
                "UPDATE maintenance_groups SET operations=?,status='prepared',"
                "execution=?,attempt_id=? WHERE id=?",
                (json.dumps(proposal.operations), json.dumps(execution), attempt_id, group_id),
            )
        else:
            for proposal in proposals:
                position = proposal.positions[0]
                identifier = str(uuid5(NAMESPACE_URL, f"eidolon-room:maintenance:{task_id}:{position}"))
                error = proposal.error
                reason = safe_reason("validation." + error.code, context={
                    "operation_index": error.operation_index, "field": error.field,
                    "attempt_id": attempt_id,
                }) if error else None
                store.db.execute(
                    "INSERT INTO maintenance_groups(id,task_id,position,positions,obligation,"
                    "status,operations,reason,execution,attempt_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (identifier, task_id, position, json.dumps(proposal.positions),
                     json.dumps(proposal.obligation), "failed" if error else "prepared",
                     json.dumps(proposal.operations), json.dumps(reason),
                     json.dumps(execution), attempt_id),
                )
        store.db.execute(
            "UPDATE maintenance_stages SET updated_at=? WHERE task_id=? AND stage='continuity'",
            (utc_now(), task_id),
        )


def note_correction(store: Store, task_id: str, group_id: str, attempt_id: str | None) -> None:
    if not store.db.execute(
        "UPDATE maintenance_groups SET corrections=corrections+1,attempt_id=? "
        "WHERE task_id=? AND id=? AND status='failed'",
        (attempt_id, task_id, group_id),
    ).rowcount:
        raise ContinuityValidationError("Maintenance group no longer needs correction", code="conflict")


def fail(store: Store, task_id: str, group_id: str, failure: ContinuityValidationError, *,
         previous_status: str = "prepared", attempt_id: str | None = None) -> None:
    from .store import utc_now

    with store._lock, store.db:
        reason = safe_reason("validation." + failure.code, context={
            "field": failure.field, "operation_index": failure.operation_index,
            "attempt_id": attempt_id,
        })
        changed = store.db.execute(
            "UPDATE maintenance_groups SET status='failed',reason=?,operations=NULL "
            "WHERE id=? AND task_id=? AND status=?",
            (json.dumps(reason), group_id, task_id, previous_status),
        ).rowcount
        if changed:
            store.db.execute(
                "UPDATE maintenance_stages SET status='failed',updated_at=? "
                "WHERE task_id=? AND stage='continuity'", (utc_now(), task_id),
            )


def outcomes(rows: list[dict]) -> list[dict]:
    return sorted((entry for row in rows for entry in row["metadata_outcomes"] or []),
                  key=lambda entry: entry["operation"])


def status(store: Store, task_id: str) -> tuple[list[dict], list[dict]]:
    rows = store.db.execute(
        "SELECT id,status,reason,metadata_outcomes FROM maintenance_groups "
        "WHERE task_id=? ORDER BY position", (task_id,),
    ).fetchall()
    return (
        [{"id": row["id"], "status": row["status"],
          "reason": read_reason(json.loads(row["reason"])) if row["reason"] else None}
         for row in rows],
        outcomes([{"metadata_outcomes": json.loads(row["metadata_outcomes"])}
                  for row in rows if row["metadata_outcomes"]]),
    )


def reconcile(store: Store, task_id: str, *, revision: int, attempt_id: str | None = None) -> None:
    """Settle evidence only after every obligation that still needs it is resolved."""
    from .store import utc_now

    rows = load(store, task_id)
    unfinished = [row for row in rows if row["status"] != "completed"]
    held = {source for row in unfinished for source in row["obligation"]["source_ids"]}
    covered = {source for row in rows for source in row["obligation"]["source_ids"]}
    task = store._maintenance_task(task_id)
    inputs = task["input_ids"]
    releasable = set(inputs) if not unfinished else covered - held
    store.db.executemany(
        "UPDATE continuity_evidence SET processed=1 WHERE scope=? AND task_id=? AND exchange_id=?",
        [(task["scope"], task_id, source) for source in inputs if source in releasable],
    )
    recovery = {**(task["stages"]["continuity"].get("recovery_state") or {}),
                "metadata_outcomes": outcomes(rows)}
    if not unfinished:
        store._complete_continuity(task, revision, attempt_id, {**recovery, "outcome": "resolved"})
        return
    metadata_only = all(row["status"] == "metadata_pending" for row in unfinished)
    if metadata_only:
        store.db.execute(
            "UPDATE maintenance_stages SET status='awaiting_evidence',error=NULL,reason=NULL,"
            "next_retry_at=NULL,pause_fingerprint=NULL,set_aside=0,attempts=attempts+1,"
            "committed_revision=?,updated_at=?,recovery_state=? "
            "WHERE task_id=? AND stage='continuity'",
            (revision, utc_now(), json.dumps({**recovery, "outcome": "deferred"}), task_id),
        )
    else:
        store.db.execute(
            "UPDATE maintenance_stages SET committed_revision=?,updated_at=?,recovery_state=? "
            "WHERE task_id=? AND stage='continuity'",
            (revision, utc_now(), json.dumps(recovery), task_id),
        )


def reconcile_metadata(store: Store, scope: str, *, revised_ids: set[str], revision: int) -> None:
    """A later evidenced update can resolve metadata without replaying the old change."""
    if not revised_ids:
        return
    tasks = set()
    rows = store.db.execute(
        "SELECT g.id,g.task_id,g.metadata FROM maintenance_groups g "
        "JOIN maintenance_tasks t ON t.id=g.task_id "
        "WHERE t.scope=? AND g.status='metadata_pending'", (scope,),
    ).fetchall()
    for row in rows:
        pending = json.loads(row["metadata"])
        if not revised_ids.intersection(pending):
            continue
        unresolved = {}
        for item_id, fields in pending.items():
            item = store.get_continuity_item(item_id, scope=scope)
            remaining = set(fields) & set(item.get("pending_metadata", [])) if item else set()
            if remaining:
                unresolved[item_id] = sorted(remaining)
        if not unresolved:
            store.db.execute(
                "UPDATE maintenance_groups SET status='completed',metadata=NULL,reason=NULL "
                "WHERE id=?", (row["id"],),
            )
            tasks.add(row["task_id"])
        else:
            store.db.execute("UPDATE maintenance_groups SET metadata=? WHERE id=?",
                             (json.dumps(unresolved), row["id"]))
    for task_id in sorted(tasks):
        reconcile(store, task_id, revision=revision)
