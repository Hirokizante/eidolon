"""Revision provenance for note evidence, independently of prompt citations.

Rows contain identities, never additional copies of transcript bodies. A retained
association says where evidence came from, not whether it proves a current claim.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store


DISPOSITIONS = {"current", "background", "superseded", "disputed", "retracted"}


def identity(source: dict) -> tuple:
    return tuple(source.get(key) for key in ("exchange_id", "document_id", "role", "input_hash"))


def identified_sources(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [source for source in value if isinstance(source, dict)
            and type(source.get("exchange_id")) is int
            and isinstance(source.get("document_id"), str)
            and isinstance(source.get("input_hash"), str)
            and isinstance(source.get("role"), str)
            and source["role"] in {"user", "assistant"}
            and source.get("origin") == source["role"] + "_statement"]


def migrate(store: Store) -> None:
    """Run inside the version migration transaction, before advancing user_version."""
    store.db.execute("""CREATE TABLE IF NOT EXISTS continuity_support (
        id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, scope TEXT NOT NULL,
        revision INTEGER NOT NULL, exchange_id INTEGER NOT NULL,
        document_id TEXT NOT NULL, role TEXT NOT NULL, input_hash TEXT NOT NULL,
        support_role TEXT NOT NULL, disposition TEXT NOT NULL, task_id TEXT,
        change_origin TEXT NOT NULL,
        UNIQUE(item_id,scope,revision,exchange_id,document_id,role,input_hash)
    )""")
    store.db.execute("CREATE INDEX IF NOT EXISTS continuity_support_item "
                     "ON continuity_support(item_id,scope,id)")
    # Read all revisions in order. Preserve only identities whose original snapshot
    # is still eligible; unresolved historical JSON remains untouched either way.
    rows = store.db.execute(
        "SELECT item_id,scope,revision,task_id,change_origin,data FROM continuity_history "
        "ORDER BY id").fetchall()
    for row in rows:
        item = {**json.loads(row["data"]), "id": row["item_id"], "scope": row["scope"],
                "revision": row["revision"]}
        record(store, item, task_id=row["task_id"], change_origin=row["change_origin"], eligible_only=True)
    # Some older databases have a working item without a matching history row.
    for row in store.db.execute("SELECT * FROM continuity_items").fetchall():
        item = {**json.loads(row["data"]), "id": row["id"], "scope": row["scope"],
                "revision": row["revision"]}
        record(store, item, task_id=None, change_origin="migration", eligible_only=True)


def associations(store: Store, item_id: str, scope: str) -> list[dict]:
    rows = store.db.execute("""SELECT * FROM (
        SELECT *,row_number() OVER (
          PARTITION BY exchange_id,document_id,role,input_hash ORDER BY id DESC
        ) AS position FROM continuity_support WHERE item_id=? AND scope=?
    ) WHERE position=1 ORDER BY id""", (item_id, scope)).fetchall()
    result = []
    for row in rows:
        entry = {key: row[key] for key in row.keys() if key != "position"}
        originals = store.continuity_evidence([row["exchange_id"]], scope=scope)
        entry["eligible"] = bool(originals and originals[0]["document_id"] == row["document_id"]
                                  and store._continuity_hash(originals[0]) == row["input_hash"])
        entry["quality"] = "unknown"
        result.append(entry)
    return result


def record(store: Store, item: dict, *, task_id: str | None, change_origin: str,
           eligible_only: bool = False, dispositions: dict[tuple, str] | None = None) -> None:
    backfill_roles = {}
    if eligible_only:
        # Mechanically malformed legacy identities stay in immutable history;
        # migration must not turn them into usable historical candidates.
        sources = item.get("sources", [])
        sources = sources if isinstance(sources, list) else []
        valid = identified_sources(sources)
        backfill_roles = {identity(source): (
            "claim" if index < item.get("claim_source_count", len(sources)) else "transition")
            for index, source in enumerate(sources) if source in valid}
        item = {**item, "sources": valid}
    selected = {identity(source): source for source in item.get("sources", [])}
    previous = associations(store, item["id"], item["scope"])
    entries = {identity(entry): entry for entry in previous}
    for index, source in enumerate(item.get("sources", [])):
        entries[identity(source)] = {**source, "support_role": backfill_roles.get(identity(source),
            "claim" if index < item.get("claim_source_count", len(selected)) else "transition")}
    for key, entry in entries.items():
        if not all(value is not None for value in key):
            continue
        if eligible_only:
            originals = store.continuity_evidence([entry["exchange_id"]], scope=item["scope"])
            if (not originals or originals[0]["document_id"] != entry["document_id"]
                    or store._continuity_hash(originals[0]) != entry["input_hash"]):
                continue
        disposition = (dispositions or {}).get(key)
        if disposition is None:
            disposition = "current" if key in selected else entry.get("disposition", "background")
            if key not in selected and disposition == "current":
                disposition = "background"
        prior = next((old for old in previous if identity(old) == key), None)
        if key in selected and prior and prior["disposition"] == "retracted":
            from ..memory.continuity import ContinuityValidationError
            raise ContinuityValidationError("Retracted support cannot be selected", code="source")
        if (prior and prior["disposition"] == disposition
                and prior["support_role"] == entry["support_role"]):
            continue
        store.db.execute(
            "INSERT OR IGNORE INTO continuity_support(item_id,scope,revision,exchange_id,"
            "document_id,role,input_hash,support_role,disposition,task_id,change_origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item["id"], item["scope"], item["revision"], *key,
             entry["support_role"], disposition, task_id, change_origin))


def candidates(store: Store, item_id: str, scope: str, *, limit: int = 32) -> list[dict]:
    """Bounded historical originals, with full identity/eligibility rechecks."""
    result = []
    for entry in reversed(associations(store, item_id, scope)):
        if entry["eligible"] and entry["disposition"] != "retracted":
            original = store.continuity_evidence([entry["exchange_id"]], scope=scope)[0]
            source = {key: entry[key] for key in ("exchange_id", "document_id", "role", "input_hash")}
            source.update(origin=entry["role"] + "_statement", excerpt=original[entry["role"]])
            result.append(source)
            if len(result) == limit:
                break
    return result


def pending_repair(store: Store, item_id: str, scope: str, *, exclude_task_id: str = "") -> bool:
    return store.db.execute(
        "SELECT 1 FROM maintenance_stages s JOIN maintenance_tasks t ON t.id=s.task_id "
        "WHERE t.scope=? AND s.stage='continuity' AND s.status!='completed' "
        "AND coalesce(json_extract(s.recovery_state,'$.outcome'),'')!='resolved' "
        "AND json_extract(s.recovery_state,'$.repair_target')=? AND t.id!=? LIMIT 1",
        (scope, item_id, exclude_task_id),
    ).fetchone() is not None


def commit_repair(store: Store, task: dict, operation: dict, *, attempt_id: str | None) -> dict:
    """Persistence defense, called inside Store's CAS and source transaction."""
    from ..memory.continuity import ContinuityValidationError
    from .store import utc_now

    stage = task["stages"]["continuity"]
    recovery = stage.get("recovery_state") or {}
    item_id = operation.get("item_id")
    item = store.get_continuity_item(item_id, task["scope"])
    authorized = (recovery.get("repair_target") == item_id or
                  recovery.get("code") == "validation.saved_support"
                  and recovery.get("outcome") in {None, "repair"}
                  and item_id in recovery.get("targets", []))
    if (not authorized or item is None
            or set(operation) != {"op", "item_id", "expected_revision", "sources", "dispositions"}):
        raise ContinuityValidationError("Repair requires a reported saved-support target", code="source")
    if (item.get("locked") or item.get("user_created") or item.get("origin") == "user_note"
            or item.get("suppressed")):
        raise ContinuityValidationError("Repair cannot change protected notes", code="protected")
    if operation["expected_revision"] != item["revision"]:
        raise ContinuityValidationError("Repair target changed", code="revision")
    known = {identity(source): source for source in candidates(store, item_id, task["scope"])}
    sources = operation["sources"]
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 8
            or any(not isinstance(source, dict) or identity(source) not in known
                   or source != known[identity(source)] for source in sources)
            or len({identity(source) for source in sources}) != len(sources)):
        raise ContinuityValidationError("Repair originals changed or are foreign", code="source")
    selected = {identity(source) for source in sources}
    durable = associations(store, item_id, task["scope"])
    transitions = {identity(entry) for entry in durable if entry["support_role"] == "transition"
                   and entry["disposition"] == "current"}
    from ..memory.continuity import POLICY_ARCHIVE_REASONS
    if (item.get("state") != "open" and item.get("archive_reason") not in POLICY_ARCHIVE_REASONS
            and (not transitions or not transitions <= selected)):
        raise ContinuityValidationError("Repair must preserve terminal chronology", code="repair_unresolved")
    roles = {identity(entry): entry["support_role"] for entry in durable}
    sources = sorted(sources, key=lambda source: roles[identity(source)] == "transition")
    claim_count = sum(roles[identity(source)] == "claim" for source in sources)
    if not claim_count:
        raise ContinuityValidationError("Repair needs claim support", code="repair_unresolved")
    dispositions = {}
    if not isinstance(operation["dispositions"], list) or len(operation["dispositions"]) > 32:
        raise ContinuityValidationError("Invalid repair dispositions", code="source")
    for entry in operation["dispositions"]:
        source = entry.get("source", {}) if isinstance(entry, dict) else {}
        key = identity(source)
        disposition = entry.get("disposition") if isinstance(entry, dict) else None
        if (key not in known or source != known[key] or key in selected or key in dispositions
                or disposition not in DISPOSITIONS - {"current"}):
            raise ContinuityValidationError("Invalid repair disposition", code="source")
        dispositions[key] = disposition
    # Preserve the stored text and every piece of authored/temporal metadata.
    row = store.db.execute("SELECT data FROM continuity_items WHERE id=?", (item_id,)).fetchone()
    saved = json.loads(row["data"])
    saved.update(id=item_id, scope=task["scope"], sources=sources, claim_source_count=claim_count,
                 revision=item["revision"] + 1)
    store._save_continuity_item(saved, task_id=task["id"], change_origin="evidence_repair",
                                support_dispositions=dispositions)
    store.db.execute("UPDATE continuity_scopes SET revision=revision+1 WHERE scope=?", (task["scope"],))
    # Repair resolves the saved-support defect, not the original maintenance batch.
    # Its evidence stays unresolved until an ordinary proposal commits.
    store.db.execute(
        "UPDATE maintenance_stages SET status='pending',error=NULL,reason=NULL,"
        "pause_fingerprint=NULL,set_aside=0,next_retry_at=NULL,recovery_state=?,updated_at=? "
        "WHERE task_id=? AND stage='continuity'",
        (json.dumps({**recovery, "outcome": "resolved"}), utc_now(), task["id"]))
    if task["id"].startswith("repair:"):
        store.db.execute(
            "UPDATE maintenance_stages SET status='completed',committed_revision="
            "(SELECT revision FROM continuity_scopes WHERE scope=?) "
            "WHERE task_id=? AND stage='continuity'", (task["scope"], task["id"]))
    if attempt_id is not None:
        store._finish_maintenance_attempt(attempt_id, "repaired")
    return store.continuity_state(task["scope"])
