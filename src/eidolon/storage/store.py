from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import IdentityConfig


def utc_now() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def daily_key(now: datetime | None = None, timezone: str = "UTC", reset_hour: int = 4) -> str:
    if not 0 <= reset_hour <= 23:
        raise ValueError("Reset hour must be between 0 and 23")
    now = now or datetime.now(dt_timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Session time must include a timezone")
    local = now.astimezone(ZoneInfo(timezone))
    return (local.date() - timedelta(days=int(local.hour < reset_hour))).isoformat()


class Store:
    """SQLite store; a completed exchange and its outbox item commit together.

    The background service owns this connection. A small lock also allows safe
    diagnostic reads from worker threads; all write transactions are synchronous.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 11:
            self.db.close()
            raise ValueError("This database requires a newer Eidolon version")
        self.migration_backup_path: Path | None = None
        if version < 11 and self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone():
            # SQLite's backup API captures a consistent snapshot, including WAL
            # contents. Back up before any schema or retention-policy mutation.
            backup_dir = self.path.parent / "backups"
            if any((parent / ".git").exists() for parent in self.path.resolve().parents):
                backup_dir = Path(tempfile.mkdtemp(prefix="eidolon-room-backup-"))
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup_dir.chmod(0o700)
            stamp = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.migration_backup_path = backup_dir / f"{self.path.name}.pre-v11-{stamp}.sqlite3"
            with sqlite3.connect(self.migration_backup_path) as backup:
                self.db.backup(backup)
            self.migration_backup_path.chmod(0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, day_key TEXT, kind TEXT NOT NULL DEFAULT 'chat',
            title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            UNIQUE(day_key,kind)
          );
          CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            created_at TEXT NOT NULL, metadata TEXT NOT NULL, document_id TEXT UNIQUE NOT NULL,
            memory_eligible INTEGER CHECK(memory_eligible IN (0,1))
          );
          CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            generation_id TEXT NOT NULL REFERENCES generations(id), role TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(generation_id,role)
          );
          CREATE INDEX IF NOT EXISTS message_session ON messages(session_id,id);
          CREATE TABLE IF NOT EXISTS model_requests (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            generation_id TEXT NOT NULL, attempt_id TEXT NOT NULL, round_index INTEGER NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, user_message TEXT NOT NULL,
            method TEXT NOT NULL, endpoint TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(attempt_id,round_index)
          );
          CREATE INDEX IF NOT EXISTS request_session ON model_requests(session_id,id);
          CREATE TABLE IF NOT EXISTS model_request_metrics (
            id INTEGER PRIMARY KEY, role TEXT NOT NULL,
            session_id TEXT, generation_id TEXT, attempt_id TEXT, task_id TEXT,
            round_index INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
            outcome TEXT NOT NULL, metrics TEXT NOT NULL, created_at TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS request_metrics_session
            ON model_request_metrics(session_id,id);
          CREATE TABLE IF NOT EXISTS memory_manifests (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            generation_id TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE,
            manifest TEXT NOT NULL, created_at TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS manifest_session ON memory_manifests(session_id,id);
          CREATE TABLE IF NOT EXISTS memory_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS continuity_scopes (
            scope TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
            control_revision INTEGER NOT NULL DEFAULT 0, watermark INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS continuity_items (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES continuity_scopes(scope),
            revision INTEGER NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS continuity_scope ON continuity_items(scope,id);
          CREATE TABLE IF NOT EXISTS continuity_history (
            id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, scope TEXT NOT NULL,
            revision INTEGER NOT NULL, task_id TEXT, change_origin TEXT NOT NULL,
            data TEXT NOT NULL, created_at TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS continuity_history_task
            ON continuity_history(task_id,scope,id);
          CREATE INDEX IF NOT EXISTS continuity_history_item
            ON continuity_history(item_id,scope,id);
          CREATE TABLE IF NOT EXISTS maintenance_tasks (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES continuity_scopes(scope),
            input_ids TEXT NOT NULL, inputs TEXT NOT NULL, input_hash TEXT NOT NULL,
            input_watermark INTEGER NOT NULL, expected_revision INTEGER NOT NULL,
            control_revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS maintenance_stages (
            task_id TEXT NOT NULL REFERENCES maintenance_tasks(id), stage TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0, usage TEXT,
            error TEXT, next_retry_at TEXT, committed_revision INTEGER,
            updated_at TEXT NOT NULL, PRIMARY KEY(task_id,stage)
          );
          CREATE TABLE IF NOT EXISTS auxiliary_routing (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            revision INTEGER NOT NULL, fingerprint TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS maintenance_attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, stage TEXT NOT NULL,
            provenance TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
            outcome TEXT NOT NULL, elapsed_seconds REAL, usage TEXT,
            FOREIGN KEY(task_id,stage) REFERENCES maintenance_stages(task_id,stage)
          );
          CREATE INDEX IF NOT EXISTS maintenance_attempt_stage
            ON maintenance_attempts(task_id,stage,started_at);
          CREATE TABLE IF NOT EXISTS summaries (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id), content TEXT NOT NULL,
            updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS outbox (
            document_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES generations(id),
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            operation_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, next_retry_at TEXT,
            review_state TEXT NOT NULL DEFAULT 'approved'
              CHECK(review_state IN ('approved','pending','rejected'))
          );
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, cursor TEXT, updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS job_runs (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL, session_id TEXT,
            result TEXT, error TEXT, updated_at TEXT NOT NULL,
            UNIQUE(job_id,scheduled_at)
          );
          DELETE FROM model_requests WHERE id NOT IN (
            SELECT id FROM model_requests ORDER BY id DESC LIMIT 1
          );
        """)
        # Preserve old runs and their results while retiring file destinations.
        with self.db:
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(job_runs)")}
            if "note_path" in columns:
                self.db.execute("ALTER TABLE job_runs DROP COLUMN note_path")
            outbox_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(outbox)")}
            if "next_retry_at" not in outbox_columns:
                self.db.execute("ALTER TABLE outbox ADD COLUMN next_retry_at TEXT")
            generation_columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(generations)")
            }
            if "memory_eligible" not in generation_columns:
                self.db.execute(
                    "ALTER TABLE generations ADD COLUMN memory_eligible INTEGER "
                    "CHECK(memory_eligible IN (0,1))"
                )
            if "review_state" not in outbox_columns:
                self.db.execute(
                    "ALTER TABLE outbox ADD COLUMN review_state TEXT NOT NULL DEFAULT 'approved' "
                    "CHECK(review_state IN ('approved','pending','rejected'))"
                )
            stage_columns = {row["name"] for row in self.db.execute(
                "PRAGMA table_info(maintenance_stages)"
            )}
            if "model_calls" not in stage_columns:
                self.db.execute(
                    "ALTER TABLE maintenance_stages ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0"
                )
            if "usage" not in stage_columns:
                self.db.execute("ALTER TABLE maintenance_stages ADD COLUMN usage TEXT")
            for column in ("usage_totals", "committed_execution"):
                if column not in stage_columns:
                    self.db.execute(f"ALTER TABLE maintenance_stages ADD COLUMN {column} TEXT")
            # Existing lifetime calls count toward the first bounded pass on upgrade.
            for column, declaration in (
                ("recovery_limit", "INTEGER"),
                ("pass_start_calls", "INTEGER NOT NULL DEFAULT 0"),
                ("recovery_pass", "INTEGER NOT NULL DEFAULT 1"),
                ("pause_fingerprint", "TEXT"),
            ):
                if column not in stage_columns:
                    self.db.execute(
                        f"ALTER TABLE maintenance_stages ADD COLUMN {column} {declaration}")
            attempt_columns = {row["name"] for row in self.db.execute(
                "PRAGMA table_info(maintenance_attempts)"
            )}
            if "reason" not in attempt_columns:
                self.db.execute("ALTER TABLE maintenance_attempts ADD COLUMN reason TEXT")
            if "reason" not in stage_columns:
                self.db.execute("ALTER TABLE maintenance_stages ADD COLUMN reason TEXT")
            if "attempt_counts" not in stage_columns:
                self.db.execute("ALTER TABLE maintenance_stages ADD COLUMN attempt_counts TEXT")
                # Old pruned outcomes are unknowable; model_calls retains their total.
                stages = self.db.execute("SELECT task_id,stage FROM maintenance_stages").fetchall()
                for row in stages:
                    counts = {a["outcome"]: a["n"] for a in self.db.execute(
                        "SELECT outcome,count(*) AS n FROM maintenance_attempts "
                        "WHERE task_id=? AND stage=? AND outcome!='running' GROUP BY outcome",
                        (row["task_id"], row["stage"]),
                    )}
                    self.db.execute(
                        "UPDATE maintenance_stages SET attempt_counts=? "
                        "WHERE task_id=? AND stage=?",
                        (json.dumps(counts), row["task_id"], row["stage"]),
                    )
            # A running attempt observed on restart was interrupted, not a model failure.
            for row in self.db.execute(
                "SELECT id FROM maintenance_attempts WHERE outcome='running'"
            ).fetchall():
                self._finish_maintenance_attempt(row["id"], "interrupted",
                                                 reason_code="interrupted")
                self.db.execute(
                    "UPDATE maintenance_stages SET reason=(SELECT reason FROM maintenance_attempts "
                    "WHERE id=?) WHERE (task_id,stage)=(SELECT task_id,stage FROM "
                    "maintenance_attempts WHERE id=?) AND status!='completed' AND reason IS NULL",
                    (row["id"], row["id"]),
                )
            if version < 6:
                self.db.execute(
                    "UPDATE outbox SET review_state='pending' WHERE status!='completed' "
                    "AND generation_id IN (SELECT id FROM generations WHERE memory_eligible IS NULL)"
                )
            self.db.execute("PRAGMA user_version=11")
        self.db.commit()
        self.path.chmod(0o600)

    def close(self) -> None:
        self.db.close()

    def session_for(self, now: datetime | None = None, timezone: str = "UTC", reset_hour: int = 4) -> dict:
        key = daily_key(now, timezone, reset_hour)
        with self._lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,'chat',?,?)",
                            (f"day:{key}", key, key, utc_now()))
            return dict(self.db.execute("SELECT * FROM sessions WHERE day_key=? AND kind='chat'", (key,)).fetchone())

    daily_session = session_for

    def create_session(self, session_id: str | None = None, *, kind: str = "job", title: str = "") -> dict:
        session_id = session_id or str(uuid.uuid4())
        with self._lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES (?,NULL,?,?,?)", (session_id, kind, title, utc_now()))
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(f"Session does not exist: {session_id}")
        return dict(row)

    def list_sessions(self, *, kind: str | None = None, limit: int = 100) -> list[dict]:
        where, params = (" WHERE kind=?", [kind]) if kind else ("", [])
        return [dict(r) for r in self.db.execute("SELECT * FROM sessions" + where + " ORDER BY created_at DESC LIMIT ?", (*params, limit))]

    def list_messages(self, session_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT m.*,g.metadata AS generation_metadata FROM messages m "
            "JOIN generations g ON g.id=m.generation_id WHERE m.session_id=? ORDER BY m.id",
            (session_id,),
        )
        result = []
        for row in rows:
            message = dict(row)
            metadata = json.loads(message.pop("generation_metadata"))
            if message["role"] == "user" and metadata.get("user_message_at"):
                # Older versions saved both rows at completion, but kept the send time here.
                message["created_at"] = metadata["user_message_at"]
            if message["role"] == "user" and metadata.get("images"):
                message["images"] = metadata["images"]
            if "identity" in metadata:
                message["identity"] = metadata["identity"]
            result.append(message)
        return result

    history = list_messages

    def memory_query_history(self, session_id: str) -> list[dict]:
        """Use transcript context without later resending explicitly opted-out turns.

        Legacy eligibility is unknown. Its existing transcript remains available
        as recall query context; this does not authorize a retention write.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT m.*,g.metadata AS generation_metadata FROM messages m "
                "JOIN generations g ON g.id=m.generation_id "
                "WHERE m.session_id=? AND (g.memory_eligible=1 OR g.memory_eligible IS NULL) "
                "ORDER BY m.id", (session_id,),
            )
            result = []
            for row in rows:
                message = dict(row)
                metadata = json.loads(message.pop("generation_metadata"))
                if "identity" in metadata:
                    message["identity"] = metadata["identity"]
                result.append(message)
            return result

    def record_model_request(self, session_id: str, *, generation_id: str, attempt_id: str,
                             round_index: int, provider: str, model: str, user_message: str,
                             method: str, endpoint: str, body: str) -> dict:
        # Requests survive failed/cancelled turns, so generation_id has no foreign key.
        # They are private diagnostics and never become transcript or memory content.
        with self._lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO model_requests(session_id,generation_id,attempt_id,round_index,"
                "provider,model,user_message,method,endpoint,body,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, generation_id, attempt_id, round_index, provider, model,
                 user_message, method, endpoint, body, utc_now()),
            )
            self.db.execute("DELETE FROM model_requests WHERE id != ?", (cursor.lastrowid,))
        return self.get_model_request(cursor.lastrowid)

    def latest_model_request(self) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM model_requests ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def record_request_metrics(self, *, role: str, provider: str, model: str,
                               metrics: dict, outcome: str, session_id: str | None = None,
                               generation_id: str | None = None, attempt_id: str | None = None,
                               task_id: str | None = None, round_index: int = 0) -> None:
        """Keep bounded numeric request diagnostics independently of the last raw body."""
        from eidolon.providers.metrics import METRIC_NAMES

        if role not in {"dialogue", "vision", "auxiliary"}:
            raise ValueError("Unknown request role")
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ValueError("Unknown request outcome")
        if (not isinstance(metrics, dict) or metrics.keys() - METRIC_NAMES
                or any(value is not None and (
                    type(value) not in {int, float} or value < 0
                    or type(value) is float and not math.isfinite(value)
                ) for value in metrics.values())):
            raise ValueError("Request metrics must contain only supported numeric measurements")
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO model_request_metrics(role,session_id,generation_id,attempt_id,"
                "task_id,round_index,provider,model,outcome,metrics,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (role, session_id, generation_id, attempt_id, task_id, round_index,
                 provider, model, outcome, json.dumps(metrics), utc_now()),
            )
            # Preserve a useful rolling diagnostic window without unbounded state growth.
            self.db.execute("DELETE FROM model_request_metrics WHERE id <= "
                            "(SELECT max(id)-1000 FROM model_request_metrics)")

    def list_request_metrics(self, session_id: str | None = None, *,
                             limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Request metric limit must be between 1 and 1000")
        where, parameters = (" WHERE session_id=?", [session_id]) if session_id else ("", [])
        with self._lock:
            rows = self.db.execute("SELECT * FROM model_request_metrics" + where
                                   + " ORDER BY id DESC LIMIT ?", [*parameters, limit]).fetchall()
        return [{**dict(row), "metrics": json.loads(row["metrics"])} for row in rows]

    def list_model_requests(self, session_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id,session_id,generation_id,attempt_id,round_index,provider,model,"
            "substr(user_message,1,120) AS user_message,method,endpoint,created_at "
            "FROM model_requests WHERE session_id=? ORDER BY id DESC", (session_id,),
        )]

    def get_model_request(self, request_id: int) -> dict:
        row = self.db.execute("SELECT * FROM model_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(f"Model request does not exist: {request_id}")
        return dict(row)

    def record_memory_manifest(self, session_id: str, generation_id: str,
                               attempt_id: str, manifest: dict) -> dict:
        """Keep private supplied-context evidence, including unsuccessful attempts."""
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO memory_manifests(session_id,generation_id,attempt_id,manifest,created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET manifest=excluded.manifest",
                (session_id, generation_id, attempt_id, json.dumps(manifest), utc_now()),
            )
            row = self.db.execute(
                "SELECT id FROM memory_manifests WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            self.db.execute(
                "DELETE FROM memory_manifests WHERE id NOT IN "
                "(SELECT id FROM memory_manifests ORDER BY id DESC LIMIT 100)"
            )
        return self.get_memory_manifest(row["id"])

    def list_memory_manifests(self, session_id: str | None = None,
                             limit: int = 100) -> list[dict]:
        where, params = (" WHERE session_id=?", [session_id]) if session_id else ("", [])
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM memory_manifests" + where + " ORDER BY id DESC LIMIT ?",
                (*params, max(0, min(limit, 100))),
            ).fetchall()
        return [{**dict(row), "manifest": json.loads(row["manifest"])} for row in rows]

    def get_memory_manifest(self, manifest_id: int) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM memory_manifests WHERE id=?", (manifest_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Memory manifest does not exist: {manifest_id}")
        return {**dict(row), "manifest": json.loads(row["manifest"])}

    def memory_delivery_paused(self) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM memory_state WHERE key='delivery_paused'"
            ).fetchone()
        return row is not None and row["value"] == "true"

    def set_memory_delivery_paused(self, paused: bool) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO memory_state(key,value) VALUES ('delivery_paused',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("true" if paused else "false",),
            )

    def list_pending_legacy(self, limit: int = 100) -> list[dict]:
        """Select held sources before limiting, independently of accepted backlog."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM outbox WHERE review_state='pending' AND status!='completed' "
                "ORDER BY updated_at LIMIT ?", (max(0, min(limit, 100)),),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def review_legacy_memory(self, approve: bool,
                             document_ids: list[str] | None = None) -> int:
        """Review only queued legacy work; uncertain submissions must be polled first.

        A decision is one-way. It never changes payloads, operation identities,
        or an eligibility decision already captured for a new exchange.
        """
        where, params = "", []
        if document_ids is not None:
            if not document_ids:
                return 0
            where = " AND o.document_id IN (" + ",".join("?" for _ in document_ids) + ")"
            params = list(document_ids)
        with self._lock, self.db:
            rows = self.db.execute(
                "SELECT o.document_id,o.generation_id FROM outbox o JOIN generations g "
                "ON g.id=o.generation_id WHERE o.review_state='pending' AND o.status='queued' "
                "AND g.memory_eligible IS NULL" + where, params,
            ).fetchall()
            self.db.executemany(
                "UPDATE generations SET memory_eligible=? WHERE id=?",
                [(int(approve), row["generation_id"]) for row in rows],
            )
            self.db.executemany(
                "UPDATE outbox SET review_state=?,updated_at=? WHERE document_id=?",
                [("approved" if approve else "rejected", utc_now(), row["document_id"])
                 for row in rows],
            )
        return len(rows)

    def get_summary(self, session_id: str) -> str:
        row = self.db.execute("SELECT content FROM summaries WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else ""

    summary = get_summary

    def set_summary(self, session_id: str, summary: str) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at", (session_id, summary, utc_now()))

    def complete_turn(self, session_id: str, user: str, assistant: str,
                      generation_id: str | None = None, metadata: dict | None = None,
                      retain: bool = True) -> dict:
        """Commit an exchange with optional attribution under ``metadata.identity``.

        The first completion owns its metadata, memory eligibility, and outbox payload.
        """
        generation_id = generation_id or str(uuid.uuid4())
        document_id = f"eidolon-room:exchange:{generation_id}"
        timestamp = utc_now()
        with self._lock, self.db:
            existing = self.db.execute("SELECT * FROM generations WHERE id=?", (generation_id,)).fetchone()
            if existing:
                messages = self.db.execute("SELECT role,content FROM messages WHERE generation_id=? ORDER BY id", (generation_id,)).fetchall()
                if existing["session_id"] != session_id or [(r["role"], r["content"]) for r in messages] != [("user", user), ("assistant", assistant)]:
                    raise ValueError("A completed generation cannot be reused for a different exchange")
                return dict(existing)
            metadata = dict(metadata or {})
            identity = IdentityConfig.model_validate(metadata.get("identity", {}))
            if "identity" in metadata:
                metadata["identity"] = identity.model_dump()
            user_timestamp = metadata.get("user_message_at") or timestamp
            # Prompt snapshots and credentials never enter the memory payload.
            payload = {"document_id": document_id, "timestamp": timestamp,
                       "content": (f"{identity.user_name} ({user_timestamp}): {user}\n\n"
                                   f"{identity.companion_name} ({timestamp}): {assistant}"),
                       "context": "Eidolon conversation" if self.get_session(session_id)["kind"] == "chat" else "Eidolon scheduled entry",
                       "metadata": {"source": "eidolon-room", "session_id": session_id}}
            self.db.execute(
                "INSERT INTO generations(id,session_id,created_at,metadata,document_id,memory_eligible) "
                "VALUES (?,?,?,?,?,?)",
                (generation_id, session_id, timestamp, json.dumps(metadata), document_id,
                 int(retain)),
            )
            self.db.executemany(
                "INSERT INTO messages(session_id,generation_id,role,content,created_at) "
                "VALUES (?,?,?,?,?)",
                [(session_id, generation_id, "user", user, user_timestamp),
                 (session_id, generation_id, "assistant", assistant, timestamp)],
            )
            if retain:
                operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, document_id))
                # New exchanges respect a durable service-capacity cooldown too.
                retry_at = self.db.execute(
                    "SELECT max(next_retry_at) FROM outbox "
                    "WHERE status!='completed' AND next_retry_at>?", (timestamp,),
                ).fetchone()[0]
                self.db.execute(
                    "INSERT INTO outbox(document_id,generation_id,payload,operation_id,"
                    "updated_at,next_retry_at) VALUES (?,?,?,?,?,?)",
                    (document_id, generation_id, json.dumps(payload), operation_id, timestamp, retry_at),
                )
        return {"id": generation_id, "generation_id": generation_id, "document_id": document_id,
                "session_id": session_id, "created_at": timestamp, "memory_eligible": int(retain)}

    def pending_outbox(self, limit: int = 20, *, ready_only: bool = False,
                       now: datetime | None = None) -> list[dict]:
        # Diagnostics still include delayed work; delivery filters before LIMIT so
        # a busy service cannot hide later, eligible exchanges.
        where = "status!='completed'"
        params: list[Any] = []
        if ready_only:
            now = now or datetime.now(dt_timezone.utc)
            if now.tzinfo is None:
                raise ValueError("Outbox retry time must include a timezone")
            where += (
                " AND review_state='approved' AND generation_id IN "
                "(SELECT id FROM generations WHERE memory_eligible=1) "
                "AND (next_retry_at IS NULL OR next_retry_at<=?)"
            )
            params.append(now.astimezone(dt_timezone.utc).isoformat())
        rows = self.db.execute("SELECT * FROM outbox WHERE " + where + " ORDER BY updated_at LIMIT ?", (*params, limit))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def pending_review_operations(self, limit: int = 20,
                                  now: datetime | None = None) -> list[dict]:
        """Uncertain legacy operations may be polled, but never replayed here."""
        now = now or datetime.now(dt_timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Outbox retry time must include a timezone")
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM outbox WHERE review_state='pending' "
                "AND status IN ('submitted','failed') "
                "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY updated_at LIMIT ?",
                (now.astimezone(dt_timezone.utc).isoformat(), limit),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def update_outbox(self, document_id: str, *, status: str, error: str | None = None,
                      attempted: bool = False, next_retry_at: str | None = None) -> None:
        if status not in {"queued", "submitted", "failed", "completed"}:
            raise ValueError("Invalid outbox status")
        if next_retry_at is not None:
            retry_time = datetime.fromisoformat(next_retry_at)
            if retry_time.tzinfo is None:
                raise ValueError("Outbox retry time must include a timezone")
            next_retry_at = retry_time.astimezone(dt_timezone.utc).isoformat()
        with self._lock, self.db:
            self.db.execute("UPDATE outbox SET status=?,error=?,attempts=attempts+?,updated_at=?,next_retry_at=? WHERE document_id=?", (status, error, int(attempted), utc_now(), next_retry_at, document_id))

    def defer_outbox(self, next_retry_at: str) -> None:
        """Persist a capacity cooldown without changing eligibility or identities."""
        retry_time = datetime.fromisoformat(next_retry_at)
        if retry_time.tzinfo is None:
            raise ValueError("Outbox retry time must include a timezone")
        next_retry_at = retry_time.astimezone(dt_timezone.utc).isoformat()
        with self._lock, self.db:
            self.db.execute(
                "UPDATE outbox SET next_retry_at=? WHERE status!='completed' "
                "AND (next_retry_at IS NULL OR next_retry_at<?)",
                (next_retry_at, next_retry_at),
            )

    def outbox_status(self) -> dict:
        with self._lock:
            counts = {r[0]: r[1] for r in self.db.execute(
                "SELECT status,count(*) FROM outbox WHERE review_state!='rejected' GROUP BY status"
            )}
            review = {r[0]: r[1] for r in self.db.execute(
                "SELECT review_state,count(*) FROM outbox WHERE status!='completed' "
                "GROUP BY review_state"
            )}
            polling = self.db.execute(
                "SELECT count(*) FROM outbox WHERE review_state='pending' "
                "AND status IN ('submitted','failed')"
            ).fetchone()[0]
            error = self.db.execute(
                "SELECT error FROM outbox WHERE error IS NOT NULL AND review_state!='rejected' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return {"counts": counts, "pending": sum(v for k, v in counts.items() if k != "completed"),
                "held": review.get("pending", 0), "eligible": review.get("approved", 0),
                "rejected": review.get("rejected", 0), "review_polling": polling,
                "delivery_paused": self.memory_delivery_paused(),
                "error": error[0] if error else None}

    @staticmethod
    def _continuity_hash(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _continuity_time(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Continuity dates must be timezone-aware ISO timestamps")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Continuity dates must include a timezone")
        return parsed.astimezone(dt_timezone.utc).isoformat()

    def _continuity_exchange_rows(self, *, scope: str, after: int = 0,
                                  limit: int = 8, newest: bool = False,
                                  exclude_session_id: str | None = None,
                                  exchange_ids: list[int] | None = None) -> list[dict]:
        where = ["s.kind='chat'", "g.memory_eligible=1", "a.id>?",
                 "coalesce(json_extract(g.metadata,'$.memory_scope'),"
                 "json_extract(g.metadata,'$.scope'),'real_life')=?"]
        params: list[Any] = [after, scope]
        if exclude_session_id is not None:
            where.append("s.id!=?")
            params.append(exclude_session_id)
        if exchange_ids is not None:
            if not exchange_ids:
                return []
            where.append("a.id IN (" + ",".join("?" for _ in exchange_ids) + ")")
            params.extend(exchange_ids)
        direction = "DESC" if newest else "ASC"
        rows = self.db.execute(
            "SELECT a.id,g.id AS generation_id,g.document_id,g.session_id,"
            "coalesce(json_extract(g.metadata,'$.user_message_at'),g.created_at) AS created_at,"
            "coalesce(json_extract(g.metadata,'$.user_message_at'),g.created_at) AS user_created_at,"
            "g.created_at AS assistant_created_at,g.created_at AS completed_at,"
            "g.metadata AS generation_metadata,u.content AS user,a.content AS assistant "
            "FROM generations g "
            "JOIN sessions s ON s.id=g.session_id "
            "JOIN messages u ON u.generation_id=g.id AND u.role='user' "
            "JOIN messages a ON a.generation_id=g.id AND a.role='assistant' WHERE "
            + " AND ".join(where) + f" ORDER BY a.id {direction} LIMIT ?",
            (*params, max(0, min(limit, 100))),
        ).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            metadata = json.loads(entry.pop("generation_metadata"))
            # Keep older input shapes intact, including saved maintenance batch hashes.
            if "identity" in metadata:
                entry["identity"] = metadata["identity"]
            entry.update(exchange_id=row["id"], scope=scope,
                         user_content=row["user"], assistant_content=row["assistant"])
            results.append(entry)
        return list(reversed(results)) if newest else results

    def continuity_inputs(self, after: int = 0, limit: int = 8,
                          scope: str = "real_life") -> list[dict]:
        """Oldest unseen completed eligible chat exchanges, ordered by immutable IDs.

        Retention delivery and backend indexing need not have completed. Jobs,
        unknown eligibility, explicit opt-outs, and other scopes never qualify.
        """
        with self._lock:
            return self._continuity_exchange_rows(scope=scope, after=after, limit=limit)

    def recent_continuity_inputs(self, after: int = 0, limit: int = 3,
                                 scope: str = "real_life",
                                 exclude_session_id: str | None = None) -> list[dict]:
        """A recent raw bridge; unlike maintenance, this intentionally selects newest."""
        with self._lock:
            return self._continuity_exchange_rows(
                scope=scope, after=after, limit=limit, newest=True,
                exclude_session_id=exclude_session_id,
            )

    def continuity_evidence(self, exchange_ids: list[int],
                            scope: str = "real_life") -> list[dict]:
        with self._lock:
            return self._continuity_exchange_rows(
                scope=scope, exchange_ids=list(dict.fromkeys(exchange_ids))[:32], limit=32,
            )

    def _continuity_scope(self, scope: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM continuity_scopes WHERE scope=?", (scope,),
        ).fetchone()
        return dict(row) if row else {"scope": scope, "revision": 0,
                                     "control_revision": 0, "watermark": 0}

    def _continuity_item(self, row: sqlite3.Row) -> dict:
        from ..memory.continuity import with_note_metadata
        item = {**json.loads(row["data"]), "id": row["id"], "item_id": row["id"],
                "scope": row["scope"], "revision": row["revision"],
                "updated_at": row["updated_at"], "invalidated": False}
        # User-authored evidence stands on its own. For learned claims, check the
        # source's current eligibility and complete snapshot, not just its ID.
        if item.get("origin") != "user_note":
            sources = item.get("sources", [])
            ids = list(dict.fromkeys(source.get("exchange_id") for source in sources))
            evidence = {}
            for start in range(0, len(ids), 32):
                evidence.update({entry["id"]: entry for entry in self._continuity_exchange_rows(
                    scope=row["scope"], exchange_ids=ids[start:start + 32], limit=32,
                )})
            for source in sources:
                entry = evidence.get(source.get("exchange_id"))
                if (entry is None or entry["document_id"] != source.get("document_id")
                        or self._continuity_hash(entry) != source.get("input_hash")):
                    item["invalidated"] = True
                    item["invalidation_reason"] = "Source changed or is no longer eligible"
                    break
        return with_note_metadata(item)

    def list_continuity_items(self, scope: str = "real_life", *,
                              include_inactive: bool = True, limit: int | None = 100) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM continuity_items WHERE scope=? ORDER BY updated_at DESC,id",
                (scope,),
            ).fetchall()
            items = [self._continuity_item(row) for row in rows]
        now = utc_now()
        def active(item: dict) -> bool:
            return (item["state"] == "open" and not item["invalidated"]
                    and (not item.get("expires_at") or item["expires_at"] > now))
        # A bounded inspector/snapshot must not hide current notes behind a long
        # archive. Within each group deliberate locks retain precedence.
        items.sort(key=lambda item: (not active(item), not item.get("locked", False)))
        if not include_inactive:
            items = [item for item in items if active(item)]
        return items if limit is None else items[:max(0, min(limit, 100))]

    def continuity_state(self, scope: str = "real_life", limit: int | None = 100, *,
                          maintenance: bool = False) -> dict:
        from ..memory.continuity import IMPORTANCE_RANK, active_items
        with self._lock:
            items = self.list_continuity_items(scope, limit=None)
            active_ids = {item["id"] for item in active_items(items, scope=scope)}
            if maintenance:
                # Old unlimited collections may exceed the bounded snapshot.
                # Keep eligible cleanup candidates ahead of protected notes so
                # a large protected prefix cannot prevent useful maintenance.
                items.sort(key=lambda item: (
                    item["id"] not in active_ids,
                    bool(item.get("locked") or item.get("user_created")),
                    IMPORTANCE_RANK.get(item.get("importance"), 1),
                ))
            return {**self._continuity_scope(scope),
                    "active_count": len(active_ids),
                    "items": items if limit is None else items[:max(0, min(limit, 100))]}

    def get_continuity_item(self, item_id: str,
                            scope: str = "real_life") -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM continuity_items WHERE id=? AND scope=?", (item_id, scope),
            ).fetchone()
            return self._continuity_item(row) if row else None

    def continuity_history(self, item_id: str | None = None, *,
                           scope: str = "real_life", limit: int = 100) -> list[dict]:
        from ..memory.continuity import with_note_metadata
        where, params = "scope=?", [scope]
        if item_id is not None:
            where += " AND item_id=?"
            params.append(item_id)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM continuity_history WHERE " + where
                + " ORDER BY id DESC LIMIT ?", (*params, max(0, min(limit, 100))),
            ).fetchall()
        return [{**dict(row), "data": with_note_metadata(json.loads(row["data"]))} for row in rows]

    def get_continuity_history_entry(self, history_id: int,
                                     scope: str = "real_life") -> dict | None:
        from ..memory.continuity import with_note_metadata
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM continuity_history WHERE id=? AND scope=?", (history_id, scope),
            ).fetchone()
        return {**dict(row), "data": with_note_metadata(json.loads(row["data"]))} if row else None

    get_continuity_history = get_continuity_history_entry

    def continuity_run_changes(self, task_id: str, *, scope: str = "real_life") -> dict | None:
        """Pair each committed note with its immediate predecessor in the ledger.

        Read by task, rather than the bounded recent-history view or current notes:
        later user edits and follow-ups must not change what an earlier run shows.
        A committed patch affects at most 32 distinct notes.
        """
        from ..memory.continuity import with_note_metadata
        with self._lock:
            run = self.db.execute(
                "SELECT CASE WHEN s.status!='completed' AND a.outcome='running' "
                "THEN 'running' ELSE s.status END AS status,"
                "CASE WHEN s.status!='completed' AND a.outcome='running' "
                "THEN a.started_at ELSE s.updated_at END AS updated_at FROM maintenance_stages s "
                "JOIN maintenance_tasks t ON t.id=s.task_id "
                "LEFT JOIN maintenance_attempts a ON a.id=("
                "SELECT latest.id FROM maintenance_attempts latest "
                "WHERE latest.task_id=t.id AND latest.stage='continuity' "
                "ORDER BY latest.started_at DESC,latest.id DESC LIMIT 1) "
                "WHERE t.id=? AND t.scope=? AND s.stage='continuity'", (task_id, scope),
            ).fetchone()
            if run is None:
                return None
            rows = self.db.execute(
                "SELECT h.data AS after_data,h.revision,p.data AS before_data "
                "FROM continuity_history h LEFT JOIN continuity_history p ON p.id=("
                "SELECT previous.id FROM continuity_history previous "
                "WHERE previous.item_id=h.item_id AND previous.scope=h.scope AND previous.id<h.id "
                "ORDER BY previous.id DESC LIMIT 1) "
                "WHERE h.task_id=? AND h.scope=? AND h.change_origin='automatic' ORDER BY h.id",
                (task_id, scope),
            ).fetchall()
        return {**dict(run), "changes": [{
            "after": with_note_metadata(json.loads(row["after_data"])),
            "before": with_note_metadata(json.loads(row["before_data"])) if row["before_data"] else None,
            "created": row["revision"] == 1,
        } for row in rows]}

    def _maintenance_task(self, task_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM maintenance_tasks WHERE id=?", (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Maintenance task does not exist: {task_id}")
        result = dict(row)
        for key in ("input_ids", "inputs"):
            result[key] = json.loads(result[key])
        result["stages"] = {stage["stage"]: self._stage_row(stage)
                            for stage in self.db.execute(
                                "SELECT * FROM maintenance_stages WHERE task_id=?", (task_id,),
                            )}
        return result

    @staticmethod
    def _stage_row(row: sqlite3.Row | dict) -> dict:
        from ..memory.diagnostics import read_reason
        result = dict(row)
        for key in ("usage", "usage_totals", "committed_execution", "reason", "attempt_counts"):
            result[key] = json.loads(result[key]) if result.get(key) else None
        result["attempt_counts"] = result["attempt_counts"] or {}
        result["reason"] = read_reason(result["reason"])
        limit = result.get("recovery_limit")
        limit = limit if limit is not None else 6
        used = result.get("model_calls", 0) - result.get("pass_start_calls", 0)
        result["budget"] = {"limit": limit, "used": used,
                            "remaining": max(0, limit - used), "exhausted": used >= limit,
                            "pass": result.get("recovery_pass", 1)}
        result["paused"] = bool(result.get("pause_fingerprint"))
        return result

    def get_maintenance_stage(self, task_id: str, stage: str = "continuity") -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT s.*,(SELECT count(*) FROM maintenance_stages problem "
                "WHERE problem.task_id=s.task_id AND problem.status='failed') AS unresolved_count "
                "FROM maintenance_stages s WHERE task_id=? AND stage=?", (task_id, stage),
            ).fetchone()
            return self._stage_row(row) if row else None

    def maintenance_conditions(self, task_id: str) -> list[dict]:
        """Unresolved stages remain inspectable alongside a successful continuity patch."""
        with self._lock:
            return [self._stage_row(row) for row in self.db.execute(
                "SELECT * FROM maintenance_stages WHERE task_id=? AND status='failed' "
                "ORDER BY stage", (task_id,),
            )]

    def create_maintenance_task(self, inputs: list[dict], *, scope: str = "real_life",
                                 expected_revision: int, control_revision: int,
                                 rewrite: bool = False, rewrite_task_id: str | None = None,
                                 review: bool = False, review_task_id: str | None = None,
                                 review_key: str = "", resume_task_id: str | None = None) -> dict:
        if scope not in {"real_life", "fiction", "unknown"}:
            raise ValueError("Invalid continuity scope")
        if (not inputs and not review) or len(inputs) > 32 or rewrite and review:
            raise ValueError("Maintenance batches require 1 to 32 completed exchanges")
        input_ids = [entry.get("id", entry.get("exchange_id")) for entry in inputs]
        if any(type(entry) is not int for entry in input_ids) or input_ids != sorted(set(input_ids)):
            raise ValueError("Maintenance input IDs must be unique and ordered")
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "eidolon-room:continuity:" + scope
                                + ":" + ",".join(map(str, input_ids))))
        if rewrite:
            # Separate identity for converting already-processed extractive notes.
            # State revisions permit another bounded conversion of shared sources.
            task_id = "narrative:" + str(uuid.uuid5(
                uuid.NAMESPACE_URL, task_id + ":" + str(expected_revision)))
            if rewrite_task_id is not None:
                if not rewrite_task_id.startswith("narrative:"):
                    raise ValueError("Invalid narrative rewrite identity")
                task_id = rewrite_task_id
        if review:
            task_id = "review:" + str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"{scope}:{expected_revision}:{control_revision}:{review_key}"))
            if review_task_id is not None:
                if not review_task_id.startswith("review:"):
                    raise ValueError("Invalid current life review identity")
                task_id = review_task_id
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if resume_task_id is None and rewrite_task_id is None and review_task_id is None:
                # A namespace change must not recreate completed or pending work.
                # Reviews use their own stable key; ordinary/rewrite batches also
                # have immutable evidence that can recover a predecessor identity.
                if not review:
                    candidates = self.db.execute(
                        "SELECT id,expected_revision FROM maintenance_tasks "
                        "WHERE scope=? AND input_ids=? AND input_hash=? ORDER BY created_at DESC",
                        (scope, json.dumps(input_ids), self._continuity_hash(inputs)),
                    ).fetchall()
                    for candidate in candidates:
                        narrative = candidate["id"].startswith("narrative:")
                        if (not candidate["id"].startswith("review:") and narrative == rewrite
                                and (not rewrite or candidate["expected_revision"] == expected_revision)):
                            task_id = candidate["id"]
                            break
            if resume_task_id is not None:
                saved = self.db.execute(
                    "SELECT scope,input_ids,input_hash FROM maintenance_tasks WHERE id=?",
                    (resume_task_id,),
                ).fetchone()
                if (saved is None or saved["scope"] != scope
                        or json.loads(saved["input_ids"]) != input_ids
                        or saved["input_hash"] != self._continuity_hash(inputs)):
                    raise ValueError("Resumed maintenance identity does not match its evidence")
                # Persisted identities precede branding namespaces and must survive upgrades.
                task_id = resume_task_id
            existing = self.db.execute(
                "SELECT 1 FROM maintenance_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if existing:
                task = self._maintenance_task(task_id)
                if task["stages"]["continuity"]["status"] == "completed":
                    return task
            other_pending = self.pending_maintenance_task(scope)
            if other_pending and other_pending["id"] != task_id:
                raise ValueError("An earlier maintenance batch still needs completion")
            state = self._continuity_scope(scope)
            if (state["revision"] != expected_revision
                    or state["control_revision"] != control_revision):
                raise ValueError("Continuity state changed; regenerate the proposal")
            if rewrite or review:
                current = self._continuity_exchange_rows(
                    scope=scope, exchange_ids=input_ids, limit=len(inputs))
                from ..memory.continuity import needs_note_metadata
                legacy = self.list_continuity_items(scope, include_inactive=False, limit=None)
                eligible_ids = {source["exchange_id"] for item in legacy
                                if (review or item.get("content_format") != "narrative"
                                    or needs_note_metadata(item))
                                and not item.get("locked") and item.get("origin") != "user_note"
                                for source in item.get("sources", [])}
                if ((not existing and not set(input_ids) <= eligible_ids)
                        or input_ids and input_ids[-1] > state["watermark"]):
                    raise ValueError("Narrative rewrites require existing eligible note evidence")
            else:
                current = self._continuity_exchange_rows(
                    scope=scope, after=state["watermark"], limit=len(inputs),
                )
            if current != inputs or [entry["id"] for entry in current] != input_ids:
                raise ValueError("Maintenance inputs changed or would skip eligible evidence")
            input_hash = self._continuity_hash(inputs)
            if existing:
                if task["input_hash"] != input_hash:
                    raise ValueError("Maintenance evidence changed")
                self.db.execute(
                    "UPDATE maintenance_tasks SET expected_revision=?,control_revision=?,"
                    "updated_at=? WHERE id=?",
                    (expected_revision, control_revision, utc_now(), task_id),
                )
            else:
                self.db.execute("INSERT OR IGNORE INTO continuity_scopes(scope) VALUES (?)", (scope,))
                stamp = utc_now()
                self.db.execute(
                    "INSERT INTO maintenance_tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, scope, json.dumps(input_ids), json.dumps(inputs), input_hash,
                     state["watermark"] if rewrite or review else input_ids[-1],
                     expected_revision, control_revision, stamp, stamp),
                )
                self.db.executemany(
                    "INSERT INTO maintenance_stages(task_id,stage,status,updated_at) VALUES (?,?,?,?)",
                    [(task_id, stage, "pending" if stage == "continuity" else "deferred", stamp)
                     for stage in ("continuity", "summary", "reflection")],
                )
            return self._maintenance_task(task_id)

    def pending_maintenance_task(self, scope: str = "real_life",
                                 stage: str = "continuity") -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT t.id FROM maintenance_tasks t JOIN maintenance_stages s ON s.task_id=t.id "
                "WHERE t.scope=? AND s.stage=? AND s.status IN ('pending','failed') "
                "ORDER BY t.input_watermark,t.id LIMIT 1", (scope, stage),
            ).fetchone()
            # Return the earliest task even during cooldown, so callers cannot
            # jump to newer evidence while a failed batch still needs retry.
            return self._maintenance_task(row["id"]) if row else None

    def prepare_maintenance_recovery(self, task_id: str, *, call_limit: int,
                                     fingerprint: str, retry: bool = False) -> bool:
        """Freeze a pass's allowance; only explicit retry can start another pass.

        Lifetime counters are never reset. Future split children must dispatch
        against their original task's allowance rather than preparing a new one.
        """
        if type(call_limit) is not int or not 1 <= call_limit <= 100:
            raise ValueError("Recovery call limit must be an integer from 1 to 100")
        with self._lock, self.db:
            row = self.get_maintenance_stage(task_id)
            if row is None or row["status"] == "completed":
                return False
            if retry:
                self.db.execute(
                    "UPDATE maintenance_stages SET recovery_limit=?,pass_start_calls=model_calls,"
                    "recovery_pass=recovery_pass+1,pause_fingerprint=NULL,next_retry_at=NULL "
                    "WHERE task_id=? AND stage='continuity'", (call_limit, task_id),
                )
            else:
                self.db.execute(
                    "UPDATE maintenance_stages SET recovery_limit=coalesce(recovery_limit,?),"
                    "pause_fingerprint=CASE WHEN pause_fingerprint!=? THEN NULL "
                    "ELSE pause_fingerprint END WHERE task_id=? AND stage='continuity'",
                    (call_limit, fingerprint, task_id),
                )
            row = self.get_maintenance_stage(task_id)
            return not row["paused"] and not row["budget"]["exhausted"]

    def fail_maintenance_stage(self, task_id: str, stage: str, error: str,
                                next_retry_at: str | None = None, *,
                                reason_code: str | None = None,
                                pause_fingerprint: str | None = None) -> None:
        from ..memory.diagnostics import safe_reason
        if stage not in {"continuity", "summary", "reflection"}:
            raise ValueError("Invalid maintenance stage")
        retry_at = self._continuity_time(next_retry_at)
        with self._lock, self.db:
            self._maintenance_task(task_id)
            self.db.execute(
                "UPDATE maintenance_stages SET status='failed',attempts=attempts+1,error=?,"
                "next_retry_at=?,updated_at=?,reason=?,pause_fingerprint=? "
                "WHERE task_id=? AND stage=? "
                "AND status!='completed'",
                (str(error)[:1000], retry_at, utc_now(), json.dumps(safe_reason(reason_code)),
                 pause_fingerprint, task_id, stage),
            )

    def maintenance_status(self, scope: str = "real_life") -> dict:
        with self._lock:
            stages = [dict(row) for row in self.db.execute(
                "SELECT s.*,t.scope,t.input_watermark,(SELECT count(*) FROM continuity_history h "
                "WHERE h.task_id=s.task_id AND h.scope=t.scope AND h.change_origin='automatic') "
                "AS change_count,(SELECT count(*) FROM maintenance_stages problem "
                "WHERE problem.task_id=s.task_id AND problem.status='failed') AS unresolved_count "
                "FROM maintenance_stages s "
                "JOIN maintenance_tasks t ON t.id=s.task_id WHERE t.scope=? "
                "ORDER BY (unresolved_count>0) DESC,t.input_watermark DESC,s.stage LIMIT 300",
                (scope,),
            )]
            counts = {row["status"]: row["count"] for row in self.db.execute(
                "SELECT s.status,count(*) AS count FROM maintenance_stages s "
                "JOIN maintenance_tasks t ON t.id=s.task_id WHERE t.scope=? GROUP BY s.status",
                (scope,),
            )}
            model_calls = self.db.execute(
                "SELECT coalesce(sum(s.model_calls),0) FROM maintenance_stages s "
                "JOIN maintenance_tasks t ON t.id=s.task_id WHERE t.scope=?", (scope,),
            ).fetchone()[0]
            error = self.db.execute(
                "SELECT s.error FROM maintenance_stages s JOIN maintenance_tasks t ON t.id=s.task_id "
                "WHERE t.scope=? AND s.error IS NOT NULL ORDER BY s.updated_at DESC LIMIT 1",
                (scope,),
            ).fetchone()
            last = self.db.execute(
                "SELECT committed_execution FROM maintenance_stages s "
                "JOIN maintenance_tasks t ON t.id=s.task_id WHERE t.scope=? "
                "AND committed_execution IS NOT NULL ORDER BY s.updated_at DESC LIMIT 1", (scope,),
            ).fetchone()
            latest = self.db.execute(
                "SELECT a.* FROM maintenance_attempts a JOIN maintenance_tasks t ON t.id=a.task_id "
                "WHERE t.scope=? ORDER BY a.started_at DESC LIMIT 1", (scope,),
            ).fetchone()
            unresolved = {"recovering": 0, "configuration_attention": 0, "set_aside": 0}
            from ..memory.diagnostics import problem_state, read_reason
            for row in self.db.execute(
                "SELECT coalesce(s.reason,a.reason),s.* FROM maintenance_stages s "
                "JOIN maintenance_tasks t ON t.id=s.task_id "
                "LEFT JOIN maintenance_attempts a ON a.id=(SELECT id FROM maintenance_attempts "
                "WHERE task_id=s.task_id AND stage=s.stage "
                "ORDER BY started_at DESC,id DESC LIMIT 1) "
                "WHERE t.scope=? AND (s.status='failed' OR "
                "s.status='pending' AND a.outcome='interrupted')", (scope,),
            ):
                saved = self._stage_row(row)
                condition = ("configuration_attention" if saved["paused"] else
                             "set_aside" if saved["budget"]["exhausted"] else
                             problem_state(read_reason(json.loads(row[0]) if row[0] else None)))
                unresolved[condition] += 1
            unresolved["total"] = sum(unresolved.values())
            successful = self.db.execute(
                "SELECT max(s.updated_at) FROM maintenance_stages s JOIN maintenance_tasks t "
                "ON t.id=s.task_id WHERE t.scope=? "
                "AND s.stage='continuity' AND s.status='completed'",
                (scope,),
            ).fetchone()[0]
        stages = [self._stage_row(stage) for stage in stages]
        return {"counts": counts, "pending": counts.get("pending", 0) + counts.get("failed", 0),
                "unresolved": unresolved, "last_successful_at": successful,
                "stages_omitted": max(0, sum(counts.values()) - len(stages)),
                "model_calls": model_calls, "error": error[0] if error else None, "stages": stages,
                "last_successful_execution": json.loads(last[0]) if last else None,
                "latest_attempt": self._attempt_row(latest)}

    def auxiliary_route(self) -> dict:
        with self._lock:
            row = self.db.execute("SELECT * FROM auxiliary_routing WHERE singleton=1").fetchone()
            return dict(row) if row else {"revision": 0, "fingerprint": ""}

    def reconcile_auxiliary_route(self, fingerprint: str) -> dict:
        """Monotonic even for A→B→A; reconcile saved config before worker recovery."""
        with self._lock, self.db:
            current = self.auxiliary_route()
            if current["fingerprint"] != fingerprint:
                self.db.execute(
                    "INSERT INTO auxiliary_routing VALUES (1,?,?) ON CONFLICT(singleton) "
                    "DO UPDATE SET revision=excluded.revision,fingerprint=excluded.fingerprint",
                    (current["revision"] + 1, fingerprint),
                )
            return self.auxiliary_route()

    def start_maintenance_attempt(self, task_id: str, provenance: dict,
                                  stage: str = "continuity") -> str:
        from ..memory.proposals import CONTRACT_VERSIONS
        from ..providers.maintenance import CAPABILITY_SOURCES, OUTPUT_MODES

        required = {"role", "mode", "provider", "model", "routing_revision", "route_fingerprint",
                    "execution_fingerprint", "request_options", "prompt_hash", "schema_hash"}
        safe_options = {"max_tokens", "max_completion_tokens", "reasoning_effort", "context_chars",
                        "context_window", "timeout_seconds", "stream_usage", "thinking_mode"}
        output_fields = {"contract", "output_mode", "capability_source", "provider_schema_hash"}
        if (not isinstance(provenance, dict) or not required <= set(provenance)
                or set(provenance) - required - output_fields
                or provenance["role"] != "auxiliary" or provenance["mode"] not in {"inherit", "separate"}
                or type(provenance["routing_revision"]) is not int
                or provenance["routing_revision"] < 1
                or not isinstance(provenance["request_options"], dict)
                or set(provenance["request_options"]) - safe_options):
            raise ValueError("Invalid auxiliary execution provenance")
        if set(provenance) & output_fields:
            if (not output_fields <= set(provenance)
                    or provenance["contract"] not in CONTRACT_VERSIONS
                    or provenance["output_mode"] not in OUTPUT_MODES
                    or provenance["capability_source"] not in CAPABILITY_SOURCES
                    or not isinstance(provenance["provider_schema_hash"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", provenance["provider_schema_hash"])):
                raise ValueError("Invalid maintenance output provenance")
        for key in ("provider", "model", "route_fingerprint", "execution_fingerprint",
                    "prompt_hash", "schema_hash"):
            value = provenance[key]
            if (not isinstance(value, str) or not value or len(value) > 500
                    or any(ord(char) < 32 for char in value)):
                raise ValueError("Invalid auxiliary execution identity")
        for key, value in provenance["request_options"].items():
            if (value is not None and type(value) not in {int, float, bool}
                    and not (key in {"reasoning_effort", "thinking_mode"}
                             and isinstance(value, str) and len(value) <= 80)):
                raise ValueError("Invalid auxiliary request provenance")
        serialized = json.dumps(provenance, ensure_ascii=False, allow_nan=False)
        if len(serialized) > 4000:
            raise ValueError("Auxiliary execution provenance exceeds its allowance")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            route = self.auxiliary_route()
            if (route["revision"] != provenance["routing_revision"]
                    or route["fingerprint"] != provenance["route_fingerprint"]):
                raise ValueError("Auxiliary route is obsolete")
            status = self.db.execute(
                "SELECT status FROM maintenance_stages WHERE task_id=? AND stage=?",
                (task_id, stage),
            ).fetchone()
            if status is None:
                raise ValueError("Maintenance stage is missing")
            if status["status"] == "completed":
                raise ValueError("Maintenance stage is already completed")
            recovery = self.get_maintenance_stage(task_id, stage)
            if recovery["paused"] or recovery["budget"]["exhausted"]:
                raise ValueError("Maintenance recovery is paused or exhausted")
            attempt_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO maintenance_attempts(id,task_id,stage,provenance,started_at,outcome) "
                "VALUES (?,?,?,?,?,'running')",
                (attempt_id, task_id, stage, serialized, utc_now()),
            )
            self.db.execute(
                "UPDATE maintenance_stages SET model_calls=model_calls+1 WHERE task_id=? AND stage=?",
                (task_id, stage),
            )
            self.db.execute(
                "DELETE FROM maintenance_attempts WHERE task_id=? AND stage=? AND id NOT IN "
                "(SELECT id FROM maintenance_attempts WHERE task_id=? AND stage=? "
                "ORDER BY started_at DESC,id DESC LIMIT 100)", (task_id, stage, task_id, stage),
            )
            return attempt_id

    @staticmethod
    def _attempt_row(row) -> dict | None:
        if row is None:
            return None
        from ..memory.diagnostics import read_reason
        return {**dict(row),
                "reason": read_reason(json.loads(row["reason"]) if row["reason"] else None),
                "provenance": json.loads(row["provenance"]),
                "usage": json.loads(row["usage"]) if row["usage"] else None}

    def get_maintenance_attempt(self, attempt_id: str) -> dict | None:
        with self._lock:
            return self._attempt_row(self.db.execute(
                "SELECT * FROM maintenance_attempts WHERE id=?", (attempt_id,),
            ).fetchone())

    def maintenance_attempts(self, task_id: str, stage: str = "continuity") -> list[dict]:
        with self._lock:
            return [self._attempt_row(row) for row in self.db.execute(
                "SELECT * FROM maintenance_attempts WHERE task_id=? AND stage=? "
                "ORDER BY started_at DESC,id DESC LIMIT 100", (task_id, stage),
            )]

    def _finish_maintenance_attempt(self, attempt_id: str, outcome: str, *,
                                    reason_code: str | None = None) -> None:
        from ..memory.diagnostics import safe_reason
        if outcome not in {"committed", "failed", "interrupted", "stale", "configuration_error"}:
            raise ValueError("Invalid auxiliary attempt outcome")
        attempt = self.get_maintenance_attempt(attempt_id)
        if not attempt or attempt["outcome"] != "running":
            return
        stamp = utc_now()
        elapsed = (datetime.fromisoformat(stamp)
                   - datetime.fromisoformat(attempt["started_at"])).total_seconds()
        self.db.execute(
            "UPDATE maintenance_attempts SET outcome=?,ended_at=?,elapsed_seconds=?,reason=? "
            "WHERE id=?",
            (outcome, stamp, max(0, elapsed), json.dumps(safe_reason(reason_code)), attempt_id),
        )

        row = self.db.execute(
            "SELECT attempt_counts FROM maintenance_stages WHERE task_id=? AND stage=?",
            (attempt["task_id"], attempt["stage"]),
        ).fetchone()
        counts = json.loads(row[0] or "{}")
        counts[outcome] = counts.get(outcome, 0) + 1
        self.db.execute(
            "UPDATE maintenance_stages SET attempt_counts=? WHERE task_id=? AND stage=?",
            (json.dumps(counts), attempt["task_id"], attempt["stage"]),
        )

    def finish_maintenance_attempt(self, attempt_id: str, outcome: str, *,
                                   reason_code: str | None = None) -> None:
        with self._lock, self.db:
            self._finish_maintenance_attempt(attempt_id, outcome, reason_code=reason_code)

    def record_maintenance_usage(self, task_id: str, stage: str = "continuity", *,
                                  usage: dict | None = None, called: bool = False,
                                  attempt_id: str | None = None) -> None:
        if stage not in {"continuity", "summary", "reflection"}:
            raise ValueError("Invalid maintenance stage")
        if usage is not None:
            if (not isinstance(usage, dict) or len(usage) > 32
                    or any(not isinstance(key, str) or len(key) > 100
                           or type(value) not in {int, float} or value < 0 or not math.isfinite(value)
                           for key, value in usage.items())):
                raise ValueError("Maintenance usage must contain bounded numeric counts")
        with self._lock, self.db:
            self._maintenance_task(task_id)
            if called:
                recovery = self.get_maintenance_stage(task_id, stage)
                if recovery is None or recovery["paused"] or recovery["budget"]["exhausted"]:
                    raise ValueError("Maintenance recovery is paused or exhausted")
            if usage and attempt_id:
                attempt = self.get_maintenance_attempt(attempt_id)
                if not attempt or attempt["task_id"] != task_id or attempt["stage"] != stage:
                    raise ValueError("Invalid usage attempt")
                previous = attempt["usage"] or {}
                row = self.db.execute(
                    "SELECT usage_totals FROM maintenance_stages WHERE task_id=? AND stage=?",
                    (task_id, stage),
                ).fetchone()
                totals = json.loads(row[0]) if row[0] else {}
                for key, value in usage.items():
                    totals[key] = totals.get(key, 0) + value - previous.get(key, 0)
                self.db.execute("UPDATE maintenance_attempts SET usage=? WHERE id=?",
                                (json.dumps(previous | usage), attempt_id))
                self.db.execute(
                    "UPDATE maintenance_stages SET usage_totals=? WHERE task_id=? AND stage=?",
                    (json.dumps(totals), task_id, stage),
                )
            self.db.execute(
                "UPDATE maintenance_stages SET model_calls=model_calls+?,usage=coalesce(?,usage),"
                "updated_at=? WHERE task_id=? AND stage=?",
                (int(called), json.dumps(usage) if usage is not None else None,
                 utc_now(), task_id, stage),
            )

    def _save_continuity_item(self, item: dict, *, task_id: str | None,
                              change_origin: str) -> None:
        from ..memory.continuity import with_note_metadata
        item = with_note_metadata(item)
        stamp = utc_now()
        data = json.dumps(item, ensure_ascii=False)
        self.db.execute(
            "INSERT INTO continuity_items VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision,data=excluded.data,updated_at=excluded.updated_at",
            (item["id"], item["scope"], item["revision"], data, stamp),
        )
        self.db.execute(
            "INSERT INTO continuity_history(item_id,scope,revision,task_id,change_origin,data,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (item["id"], item["scope"], item["revision"], task_id, change_origin, data, stamp),
        )

    def _continuity_operation_sources(self, sources: Any, inputs: dict[int, dict]) -> list[dict]:
        from ..memory.continuity import source_is_fictional, whole_source_excerpt
        if not isinstance(sources, list) or not sources or len(sources) > 32:
            raise ValueError("Continuity changes require bounded source evidence")
        checked = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("Invalid continuity evidence")
            if type(source.get("exchange_id")) is not int:
                raise ValueError("Continuity exchange IDs must be immutable integer IDs")
            exchange = inputs.get(source.get("exchange_id"))
            role, excerpt = source.get("role"), source.get("excerpt")
            if (exchange is None or source.get("document_id") != exchange["document_id"]
                    or role not in {"user", "assistant"} or not isinstance(excerpt, str)
                    or not excerpt.strip() or not whole_source_excerpt(excerpt, exchange[role])
                    or (exchange["scope"] == "real_life" and source_is_fictional(exchange[role]))
                    or source.get("origin") != f"{role}_statement"):
                raise ValueError("Continuity source is unsupported or ineligible")
            checked.append({**source, "input_hash": self._continuity_hash(exchange)})
        return checked

    def commit_continuity(self, task_id: str, operations: list[dict], *,
                          expected_revision: int, control_revision: int,
                          attempt_id: str | None = None, max_notes: int = 12) -> dict:
        """Apply a validated patch and its stage watermark in one CAS transaction."""
        if not isinstance(operations, list) or not operations or len(operations) > 32:
            raise ValueError("A maintenance result requires bounded explicit operations")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self._maintenance_task(task_id)
            scope = task["scope"]
            if task["stages"]["continuity"]["status"] == "completed":
                return self.continuity_state(scope)
            if attempt_id is not None:
                attempt = self.get_maintenance_attempt(attempt_id)
                route = self.auxiliary_route()
                if (attempt is None or attempt["task_id"] != task_id
                        or attempt["stage"] != "continuity" or attempt["outcome"] != "running"
                        or route["revision"] != attempt["provenance"]["routing_revision"]
                        or route["fingerprint"] != attempt["provenance"]["route_fingerprint"]):
                    raise ValueError("Auxiliary routing changed; retry the pending stage")
            state = self._continuity_scope(scope)
            if (state["revision"] != expected_revision
                    or state["control_revision"] != control_revision
                    or task["expected_revision"] != expected_revision
                    or task["control_revision"] != control_revision):
                raise ValueError("Continuity state changed; regenerate the proposal")
            if task_id.startswith(("narrative:", "review:")):
                current = self._continuity_exchange_rows(
                    scope=scope, exchange_ids=task["input_ids"], limit=len(task["input_ids"]))
            else:
                current = self._continuity_exchange_rows(
                    scope=scope, after=state["watermark"], limit=len(task["input_ids"]),
                )
            if (self._continuity_hash(current) != task["input_hash"]
                    or current != task["inputs"]):
                raise ValueError("Maintenance evidence changed or is no longer eligible")
            from ..memory.continuity import (
                POLICY_ARCHIVE_REASONS, is_legacy_rewrite, needs_note_metadata,
                validate_note_capacity, validate_note_metadata, validate_transition_evidence,
            )
            # Recheck the full saved scope inside this transaction. A bounded
            # inspector or model request cannot hide notes from the active cap.
            capacity_items = self.list_continuity_items(scope, limit=None)
            inputs = {entry["id"]: entry for entry in current}
            changed, targets = 0, set()
            for index, operation in enumerate(operations):
                if not isinstance(operation, dict):
                    raise ValueError("Invalid continuity operation")
                if (type(operation.get("expected_revision")) is not int
                        or operation["expected_revision"] < 0):
                    raise ValueError("Continuity operations require an explicit integer revision")
                action = operation.get("op", operation.get("operation"))
                if action == "no_change":
                    if (len(operations) != 1 or operation.get("expected_revision") != expected_revision
                            or set(operation) - {"op", "operation", "expected_revision", "sources"}
                            or operation.get("sources") or operation.get("item_id")
                            or operation.get("content")):
                        raise ValueError("Invalid no-change result")
                    continue
                if action not in {"upsert", "resolve", "archive"}:
                    raise ValueError("Invalid continuity operation")
                allowed = {"op", "operation", "item_id", "expected_revision", "sources",
                           "category", "content", "origin", "scope", "observed_at", "event_at",
                           "expires_at", "follow_up_at", "last_mention_at", "promise_owner",
                           "promise_kind", "content_format"}
                allowed.update({"topic", "importance", "replaces_item_id", "archive_reason",
                                "decision_reason"})
                if set(operation) - allowed:
                    raise ValueError("Malformed continuity operation")
                item_id = operation.get("item_id") or str(uuid.uuid5(
                    uuid.NAMESPACE_URL, f"eidolon-room:continuity-item:{task_id}:{index}",
                ))
                if not isinstance(item_id, str) or not item_id or item_id in targets:
                    raise ValueError("Continuity item IDs must be stable and unique in a patch")
                targets.add(item_id)
                row = self.db.execute(
                    "SELECT * FROM continuity_items WHERE id=?", (item_id,),
                ).fetchone()
                previous = json.loads(row["data"]) if row else None
                revision = row["revision"] if row else 0
                if operation.get("expected_revision") != revision:
                    raise ValueError("Continuity item revision changed")
                if row and (row["scope"] != scope or previous.get("locked")
                            or previous.get("user_created") or previous.get("suppressed")):
                    raise ValueError(
                        "Automatic maintenance cannot change user-created, locked or other-scope notes"
                    )
                if row and self._continuity_item(row)["invalidated"]:
                    raise ValueError("Prior continuity evidence changed")
                if not row and action != "upsert":
                    raise ValueError("A terminal operation requires an existing item")
                validate_note_metadata({**operation, "op": action}, previous)
                if task_id.startswith("narrative:") and (
                        action != "upsert" or not row
                        or previous.get("content_format") == "narrative"
                        and not needs_note_metadata(previous)):
                    raise ValueError("Narrative rewrites must update existing extractive notes")
                narrative = operation.get("content_format") == "narrative"
                if operation.get("content_format") not in {None, "narrative"}:
                    raise ValueError("Invalid continuity content format")
                evidence = dict(inputs)
                if narrative and row:
                    if self._continuity_item(row)["invalidated"]:
                        raise ValueError("Prior continuity evidence changed")
                    prior_ids = list(dict.fromkeys(
                        source["exchange_id"] for source in previous.get("sources", [])))
                    for start in range(0, len(prior_ids), 32):
                        evidence.update({entry["id"]: entry for entry in self.continuity_evidence(
                            prior_ids[start:start + 32], scope=scope)})
                    prior_keys = {(s["exchange_id"], s["document_id"], s["role"])
                                  for s in previous.get("sources", [])}
                    if any(s.get("exchange_id") not in inputs
                           and (s.get("exchange_id"), s.get("document_id"), s.get("role"))
                           not in prior_keys for s in operation.get("sources", [])):
                        raise ValueError("Narrative sources must belong to the target note")
                format_rewrite = is_legacy_rewrite(operation, previous)
                policy_archive = (action in {"resolve", "archive"}
                                  and operation.get("archive_reason") in POLICY_ARCHIVE_REASONS)
                if task_id.startswith("review:") and not (
                        action == "archive" and operation.get("archive_reason") == "capacity"
                        or format_rewrite):
                    raise ValueError("Current life reviews can only classify notes or archive excess")
                if (not any(s.get("exchange_id") in inputs for s in operation.get("sources", []))
                        and not format_rewrite and not policy_archive):
                    raise ValueError("Continuity changes require evidence from this task")
                sources = ([] if policy_archive and not operation.get("sources") else
                           self._continuity_operation_sources(operation.get("sources"), evidence))
                # Reuse the evidence policy for direct persistence callers as
                # well as the worker. Normalized dates are intentionally not
                # reparsed as if they were the original relative expression.
                validate_transition_evidence({**operation, "op": action}, previous,
                                             new_exchange_ids=set(inputs))
                supported = [source["excerpt"] for source in sources]
                if (not narrative and not policy_archive
                        and operation.get("content") not in [*supported, "\n".join(supported)]):
                    raise ValueError("Continuity content must preserve cited original wording")
                item = dict(previous or {"id": item_id, "scope": scope, "state": "open",
                                         "locked": False, "sources": []})
                if action == "upsert":
                    category, content, origin = (operation.get(key) for key in
                                                  ("category", "content", "origin"))
                    if category not in {"situation", "thread", "relationship", "emotion"}:
                        raise ValueError("Invalid continuity category")
                    if (not isinstance(content, str) or len(content) > 4000
                            or not content.strip()
                            or not narrative and (content not in [*supported, "\n".join(supported)]
                                or origin not in {"user_statement", "assistant_statement"}
                                or any(source["origin"] != origin for source in sources))):
                        raise ValueError("Continuity content must preserve cited original wording")
                    if operation.get("scope", scope) != scope:
                        raise ValueError("Continuity claims cannot cross scopes")
                    item.update(category=category, content=content, origin=origin, state="open")
                    item.update(topic=operation["topic"].strip(), importance=operation["importance"])
                    item.pop("metadata_pending", None)
                    for key in ("archive_reason", "decision_reason", "archived_at", "replaces_item_id"):
                        item.pop(key, None)
                    for key in ("replaces_item_id", "decision_reason"):
                        if operation.get(key):
                            item[key] = operation[key]
                    if narrative:
                        item["content_format"] = "narrative"
                    else:
                        item.pop("content_format", None)
                    # A new claim cannot inherit temporal assertions or promise
                    # classifications supported only by the superseded wording.
                    # Earlier values remain available in revision history.
                    for key in ("event_at", "expires_at", "follow_up_at", "promise_owner",
                                "promise_kind"):
                        if (key not in operation and not task_id.startswith("narrative:")
                                and not format_rewrite):
                            item.pop(key, None)
                    for key in ("observed_at", "event_at", "expires_at", "follow_up_at",
                                "last_mention_at"):
                        if key in operation:
                            item[key] = self._continuity_time(operation[key])
                    for key, allowed in (("promise_owner", {None, "user", "assistant"}),
                                         ("promise_kind", {None, "conversation", "scheduled"})):
                        if key in operation:
                            if operation[key] not in allowed:
                                raise ValueError("Invalid continuity promise metadata")
                            item[key] = operation[key]
                    from ..memory.continuity import source_timestamp
                    source_observed = self._continuity_time(max(
                        (source_timestamp(evidence[source["exchange_id"]], source["role"])
                         for source in sources), key=datetime.fromisoformat,
                    ))
                    if "observed_at" in operation and item["observed_at"] != source_observed:
                        raise ValueError("Continuity observation date must match its original evidence")
                    item["observed_at"] = source_observed
                    if category == "emotion" and not item.get("expires_at"):
                        observed = datetime.fromisoformat(item["observed_at"])
                        item["expires_at"] = (observed + timedelta(hours=48)).isoformat()
                else:
                    item["state"] = "archived"
                    item["archive_reason"] = operation.get("archive_reason") or (
                        "completed" if action == "resolve" else "no_longer_current")
                    item["archived_at"] = utc_now()
                    if operation.get("decision_reason"):
                        item["decision_reason"] = operation["decision_reason"]
                    if "last_mention_at" in operation:
                        item["last_mention_at"] = self._continuity_time(operation["last_mention_at"])
                # Only current claim support occupies the working item. Earlier
                # support is preserved in its immutable revision history. A
                # terminal transition still needs both the original claim and
                # the new evidence explaining its state change.
                prior_sources = item.get("sources", [])
                claim_source_count = item.get("claim_source_count", len(prior_sources))
                claim_sources = prior_sources[:claim_source_count]
                item["sources"] = (sources if action == "upsert" else
                                   claim_sources + [source for source in sources
                                                    if source not in claim_sources])
                item["claim_source_count"] = len(sources) if action == "upsert" else claim_source_count
                item["revision"] = revision + 1
                self._save_continuity_item(item, task_id=task_id, change_origin="automatic")
                changed += 1
            validate_note_capacity(operations, capacity_items, max_notes=max_notes, scope=scope)
            new_revision = state["revision"] + int(changed > 0)
            self.db.execute(
                "UPDATE continuity_scopes SET revision=?,watermark=? WHERE scope=?",
                (new_revision, task["input_watermark"], scope),
            )
            self.db.execute(
                "UPDATE maintenance_stages SET status='completed',attempts=attempts+1,"
                "error=NULL,reason=NULL,"
                "next_retry_at=NULL,committed_revision=?,updated_at=? WHERE task_id=? AND stage='continuity'",
                (new_revision, utc_now(), task_id),
            )
            if attempt_id is not None:
                self._finish_maintenance_attempt(attempt_id, "committed")
                attempt = self.get_maintenance_attempt(attempt_id)
                self.db.execute(
                    "UPDATE maintenance_stages SET committed_execution=? WHERE task_id=? AND stage=?",
                    (json.dumps(attempt), task_id, "continuity"),
                )
            return self.continuity_state(scope)

    def create_continuity_item(self, content: str, category: str, *,
                               topic: str, importance: str, scope: str = "real_life",
                               max_notes: int = 12) -> dict:
        """Create an authored note whose ownership cannot be changed by maintenance."""
        if not isinstance(scope, str) or scope not in {"real_life", "fiction", "unknown"}:
            raise ValueError("Invalid continuity scope")
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("Continuity content must be nonempty and bounded")
        if (not isinstance(category, str)
                or category not in {"situation", "thread", "relationship", "emotion"}):
            raise ValueError("Invalid continuity category")
        from ..memory.continuity import validate_note_metadata
        validate_note_metadata({"op": "upsert", "topic": topic, "importance": importance}, None)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_user_note_capacity(topic, scope=scope, max_notes=max_notes)
            self.db.execute("INSERT OR IGNORE INTO continuity_scopes(scope) VALUES (?)", (scope,))
            item = {"id": str(uuid.uuid4()), "scope": scope, "revision": 1,
                    "content": content, "category": category, "state": "open",
                    "topic": topic.strip(), "importance": importance,
                    "origin": "user_note", "user_created": True, "locked": True,
                    "sources": [], "claim_source_count": 0, "observed_at": utc_now()}
            self._save_continuity_item(item, task_id=None, change_origin="user")
            self.db.execute(
                "UPDATE continuity_scopes SET revision=revision+1,control_revision=control_revision+1 "
                "WHERE scope=?", (scope,),
            )
            return self.get_continuity_item(item["id"], scope=scope)

    def edit_continuity_item(self, item_id: str, changes: dict, *,
                             expected_revision: int, scope: str = "real_life",
                             max_notes: int = 12) -> dict:
        allowed = {"content", "category", "state", "locked", "manual_lock", "event_at",
                   "expires_at", "follow_up_at", "last_mention_at", "promise_owner", "promise_kind",
                   "topic", "importance"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError("Invalid user continuity edit")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Choose a saved continuity revision")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM continuity_items WHERE id=? AND scope=?", (item_id, scope),
            ).fetchone()
            if row is None:
                raise KeyError(f"Continuity item does not exist: {item_id}")
            if row["revision"] != expected_revision:
                raise ValueError("Continuity item revision changed")
            from ..memory.continuity import IMPORTANCE_RANK, valid_topic, with_note_metadata
            item = with_note_metadata(json.loads(row["data"]))
            if "content" in changes and not {"locked", "manual_lock"}.intersection(changes):
                # Editing a learned note does not change who created it.
                item["locked"] = bool(item.get("user_created"))
            for key, value in changes.items():
                key = "locked" if key == "manual_lock" else key
                if key == "content":
                    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                        raise ValueError("Continuity content must be nonempty and bounded")
                    item.update(origin="user_note", sources=[], claim_source_count=0)
                elif key == "locked" and type(value) is not bool:
                    raise ValueError("Continuity lock must be boolean")
                elif key == "category" and (not isinstance(value, str) or value not in {
                        "situation", "thread", "relationship", "emotion"}):
                    raise ValueError("Invalid continuity category")
                elif key == "state" and value not in {"open", "resolved", "archived"}:
                    raise ValueError("Invalid continuity state")
                elif key == "topic":
                    if not valid_topic(value):
                        raise ValueError("A topic must contain 1 to 100 characters on one line")
                    value = value.strip()
                elif key == "importance" and (not isinstance(value, str)
                                               or value not in IMPORTANCE_RANK):
                    raise ValueError("Choose low, moderate or high importance")
                elif key.endswith("_at"):
                    value = self._continuity_time(value)
                elif key == "promise_owner" and value not in {None, "user", "assistant"}:
                    raise ValueError("Invalid promise owner")
                elif key == "promise_kind" and value not in {None, "conversation", "scheduled"}:
                    raise ValueError("Invalid promise kind")
                if (key == "category" and value != item.get("category")
                        and not item.get("user_created") and "expires_at" not in changes):
                    if value == "emotion":
                        item["expires_at"] = (
                            datetime.now(dt_timezone.utc) + timedelta(hours=48)
                        ).isoformat()
                    elif item.get("category") == "emotion":
                        item.pop("expires_at", None)
                item[key] = value
            if {"topic", "importance"} <= changes.keys():
                item.pop("metadata_pending", None)
            if item["state"] == "open":
                self._check_user_note_capacity(item["topic"], scope=scope, max_notes=max_notes,
                                               item_id=item_id)
            if item.get("user_created"):
                item["locked"] = True
            item["revision"] = row["revision"] + 1
            self._save_continuity_item(item, task_id=None, change_origin="user")
            self.db.execute(
                "UPDATE continuity_scopes SET revision=revision+1,control_revision=control_revision+1 "
                "WHERE scope=?", (row["scope"],),
            )
            return self._continuity_item(self.db.execute(
                "SELECT * FROM continuity_items WHERE id=?", (item_id,),
            ).fetchone())

    def _check_user_note_capacity(self, topic: str, *, scope: str, max_notes: int,
                                  item_id: str | None = None) -> None:
        from ..memory.continuity import MAX_NOTES, active_items, topic_key
        if type(max_notes) is not int or not 1 <= max_notes <= MAX_NOTES:
            raise ValueError("Maximum active notes must be an integer from 1 to 64")
        active = active_items(self.list_continuity_items(scope, limit=None), scope=scope)
        others = [item for item in active if item["id"] != item_id]
        if any(topic_key(item["topic"]) == topic_key(topic) for item in others):
            raise ValueError("An active note already covers this topic")
        if len(others) >= max_notes and not any(item["id"] == item_id for item in active):
            raise ValueError("Current life is full; archive an active note before adding another")

    def delete_continuity_item(self, item_id: str, *, expected_revision: int,
                               scope: str = "real_life") -> None:
        """Remove a note and invalidate any proposal based on its old snapshot."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Choose a saved continuity revision")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM continuity_items WHERE id=? AND scope=?", (item_id, scope),
            ).fetchone()
            if row is None:
                raise KeyError(f"Continuity item does not exist: {item_id}")
            if row["revision"] != expected_revision:
                raise ValueError("Continuity item revision changed")
            self.db.execute("DELETE FROM continuity_items WHERE id=? AND scope=?", (item_id, scope))
            self.db.execute(
                "UPDATE continuity_scopes SET revision=revision+1,control_revision=control_revision+1 "
                "WHERE scope=?", (scope,),
            )

    def mark_continuity_followup(self, item_ids: list[str], at: str | None = None, *,
                                 expected_revision: int | None = None,
                                 evidence: dict | None = None) -> int:
        timestamp = self._continuity_time(at or utc_now())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            source = None
            if evidence is not None:
                generation = self.db.execute(
                    "SELECT g.id,g.document_id,a.id AS exchange_id,a.content,"
                    "coalesce(json_extract(g.metadata,'$.memory_scope'),"
                    "json_extract(g.metadata,'$.scope'),'real_life') AS scope FROM generations g "
                    "JOIN sessions s ON s.id=g.session_id "
                    "JOIN messages a ON a.generation_id=g.id AND a.role='assistant' "
                    "WHERE g.id=? AND g.memory_eligible=1 AND s.kind='chat'",
                    (evidence.get("generation_id"),),
                ).fetchone()
                if (generation is None or not isinstance(evidence.get("excerpt"), str)
                        or not evidence["excerpt"] or evidence["excerpt"] not in generation["content"]):
                    return 0
                source = {"generation_id": generation["id"], "document_id": generation["document_id"],
                          "exchange_id": generation["exchange_id"], "excerpt": evidence["excerpt"]}
            changed_scopes, changed = set(), 0
            for item_id in dict.fromkeys(item_ids[:100]):
                row = self.db.execute(
                    "SELECT * FROM continuity_items WHERE id=?", (item_id,),
                ).fetchone()
                if row is None:
                    continue
                scope_state = self._continuity_scope(row["scope"])
                if evidence is not None and generation["scope"] != row["scope"]:
                    continue
                if (expected_revision is not None
                        and scope_state["revision"] != expected_revision):
                    continue
                item = json.loads(row["data"])
                if (item["state"] != "open" or self._continuity_item(row)["invalidated"]
                        or (item.get("expires_at") and item["expires_at"] <= timestamp)
                        or item.get("last_followup_at") == timestamp):
                    continue
                item["last_followup_at"] = timestamp
                if source is not None:
                    item["last_followup_evidence"] = source
                item["revision"] = row["revision"] + 1
                self._save_continuity_item(item, task_id=None, change_origin="follow_up")
                changed_scopes.add(row["scope"])
                changed += 1
            for scope in changed_scopes:
                self.db.execute(
                    "UPDATE continuity_scopes SET revision=revision+1 WHERE scope=?", (scope,),
                )
            return changed

    def save_job(self, job_id: str, config: dict) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT INTO jobs(id,config,updated_at) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,updated_at=excluded.updated_at", (job_id, json.dumps(config), utc_now()))

    def job_cursor(self, job_id: str) -> str | None:
        row = self.db.execute("SELECT cursor FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def record_job_plan(self, job_id: str, runs: list[dict], cursor: str) -> None:
        with self._lock, self.db:
            if runs:
                # A job queued before sleep should not run an extra old occurrence.
                # Interrupted runs with a recovery error/result remain recoverable.
                self.db.execute("UPDATE job_runs SET status='skipped',updated_at=? WHERE job_id=? AND status='queued' AND scheduled_at<? AND result IS NULL AND error IS NULL", (utc_now(), job_id, runs[-1]["scheduled_at"]))
            for run in runs:
                self.db.execute("INSERT OR IGNORE INTO job_runs(id,job_id,scheduled_at,status,updated_at) VALUES (?,?,?,?,?)", (run["id"], job_id, run["scheduled_at"], run["status"], utc_now()))
            self.db.execute("UPDATE jobs SET cursor=?,updated_at=? WHERE id=?", (cursor, utc_now(), job_id))

    def list_job_runs(self, job_id: str | None = None, limit: int = 100) -> list[dict]:
        where, params = (" WHERE job_id=?", [job_id]) if job_id else ("", [])
        return [dict(r) for r in self.db.execute("SELECT * FROM job_runs" + where + " ORDER BY scheduled_at DESC LIMIT ?", (*params, limit))]

    def queued_job_runs(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM job_runs WHERE job_id=? AND status='queued' ORDER BY scheduled_at", (job_id,))]

    def recover_job_runs(self) -> None:
        # Call only after acquiring the service's exclusive state-owner lock.
        with self._lock, self.db:
            self.db.execute("UPDATE job_runs SET status='queued',error='Interrupted; recovering saved run',updated_at=? WHERE status='running'", (utc_now(),))

    def claim_job_run(self, run_id: str) -> bool:
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT job_id,status FROM job_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] != "queued":
                return False
            busy = self.db.execute("SELECT 1 FROM job_runs WHERE job_id=? AND status='running'", (row["job_id"],)).fetchone()
            if busy:
                return False
            self.db.execute("UPDATE job_runs SET status='running',updated_at=? WHERE id=?", (utc_now(), run_id))
        return True

    def update_job_run(self, run_id: str, *, status: str | None = None, result: str | None = None,
                       error: str | None = None, session_id: str | None = None) -> None:
        updates: dict[str, Any] = {"updated_at": utc_now(), "error": error}
        for name, value in [("status", status), ("result", result), ("session_id", session_id)]:
            if value is not None:
                updates[name] = value
        with self._lock, self.db:
            self.db.execute("UPDATE job_runs SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?", (*updates.values(), run_id))

    def retry_job_run(self, run_id: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE job_runs SET status='queued',error=NULL,updated_at=? WHERE id=? AND status='failed'", (utc_now(), run_id))
