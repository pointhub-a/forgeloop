"""Transactional SQLite persistence for ForgeLoop tasks and audit events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from forgeloop.models import Event, TaskRecord, TaskStatus


class TaskNotFound(LookupError):
    """Raised when a task identifier has no persisted record."""


@dataclass(frozen=True)
class ApprovalRecord:
    """One consumed human approval decision."""

    id: str
    task_id: str
    action_fingerprint: str
    decision: str
    used_at: datetime | None
    created_at: datetime


class TaskRepository:
    """Persist task projections and their ordered audit events."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._migrate()

    def create(self, description: str, workspace: str | Path) -> TaskRecord:
        now = datetime.now(timezone.utc)
        task = TaskRecord(
            id=str(uuid4()),
            description=description,
            workspace=str(Path(workspace).expanduser().resolve()),
            status=TaskStatus.CREATED,
            step_count=0,
            last_validation_passed=False,
            pending_action=None,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, description, workspace, status, step_count,
                    last_validation_passed, pending_action_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.description,
                    task.workspace,
                    task.status.value,
                    task.step_count,
                    int(task.last_validation_passed),
                    None,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )
        return task

    def get(self, task_id: str) -> TaskRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return self._task_from_row(row)

    def save(self, task: TaskRecord) -> TaskRecord:
        task.workspace = str(Path(task.workspace).expanduser().resolve())
        pending_action_json = None
        if task.pending_action is not None:
            pending_action_json = json.dumps(
                task.pending_action.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET description = ?, workspace = ?, status = ?, step_count = ?,
                    last_validation_passed = ?, pending_action_json = ?,
                    created_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    task.description,
                    task.workspace,
                    task.status.value,
                    task.step_count,
                    int(task.last_validation_passed),
                    pending_action_json,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.id,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskNotFound(task.id)
        return task

    def append_event(
        self,
        task_id: str,
        kind: str,
        summary: str,
        data: dict[str, object],
    ) -> Event:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task_exists = connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_exists is None:
                raise TaskNotFound(task_id)
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            sequence = int(row[0])
            event = Event(
                id=str(uuid4()),
                task_id=task_id,
                sequence=sequence,
                kind=kind,
                summary=summary,
                data=data,
                created_at=datetime.now(timezone.utc),
            )
            connection.execute(
                """
                INSERT INTO events (
                    id, task_id, sequence, kind, summary, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.task_id,
                    event.sequence,
                    event.kind.value,
                    event.summary,
                    json.dumps(
                        event.data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    event.created_at.isoformat(),
                ),
            )
            connection.commit()
            return event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_events(self, task_id: str) -> list[Event]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        pending_action = (
            json.loads(row["pending_action_json"])
            if row["pending_action_json"] is not None
            else None
        )
        return TaskRecord(
            id=row["id"],
            description=row["description"],
            workspace=row["workspace"],
            status=row["status"],
            step_count=row["step_count"],
            last_validation_passed=bool(row["last_validation_passed"]),
            pending_action=pending_action,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            task_id=row["task_id"],
            sequence=row["sequence"],
            kind=row["kind"],
            summary=row["summary"],
            data=json.loads(row["data_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );

                INSERT INTO schema_version (version)
                SELECT 1
                WHERE NOT EXISTS (SELECT 1 FROM schema_version);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    step_count INTEGER NOT NULL,
                    last_validation_passed INTEGER NOT NULL,
                    pending_action_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    action_fingerprint TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, action_fingerprint)
                );
                """
            )


class ApprovalRepository:
    """Persist one-time approval decisions alongside task audit data."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        TaskRepository(db_path)

    def record(
        self, task_id: str, action_fingerprint: str, decision: str
    ) -> ApprovalRecord:
        now = datetime.now(timezone.utc)
        approval = ApprovalRecord(
            id=str(uuid4()),
            task_id=task_id,
            action_fingerprint=action_fingerprint,
            decision=decision,
            used_at=now,
            created_at=now,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task_exists = connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_exists is None:
                raise TaskNotFound(task_id)
            connection.execute(
                """
                INSERT INTO approvals (
                    id, task_id, action_fingerprint, decision, used_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.task_id,
                    approval.action_fingerprint,
                    approval.decision,
                    approval.used_at.isoformat() if approval.used_at else None,
                    approval.created_at.isoformat(),
                ),
            )
            connection.commit()
            return approval
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_for_task(self, task_id: str) -> list[ApprovalRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                WHERE task_id = ?
                ORDER BY created_at, id
                """,
                (task_id,),
            ).fetchall()
        return [
            ApprovalRecord(
                id=row["id"],
                task_id=row["task_id"],
                action_fingerprint=row["action_fingerprint"],
                decision=row["decision"],
                used_at=(
                    datetime.fromisoformat(row["used_at"])
                    if row["used_at"] is not None
                    else None
                ),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
