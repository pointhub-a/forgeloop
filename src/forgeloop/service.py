"""Synchronous in-process orchestration over persisted ForgeLoop audit state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock

from forgeloop.loop import AgentLoop, ApprovalMismatch, LoopEvent
from forgeloop.models import EventKind, TaskRecord
from forgeloop.repository import (
    ApprovalDecision,
    ApprovalRepository,
    TaskNotFound,
    TaskRepository,
)


class TaskNotLoaded(RuntimeError):
    """Raised when a persisted task has no live in-process agent loop."""


LoopFactory = Callable[[Path, str], AgentLoop]


class TaskService:
    """Own live loops while persisting their task and event projections."""

    def __init__(
        self,
        repository: TaskRepository,
        approvals: ApprovalRepository,
        loop_factory: LoopFactory,
    ) -> None:
        self.repository = repository
        self.approvals = approvals
        self.loop_factory = loop_factory
        self._loops: dict[str, AgentLoop] = {}
        self._synced_event_counts: dict[str, int] = {}
        self._task_locks: dict[str, RLock] = {}
        self._task_locks_guard = Lock()

    def create(self, description: str, workspace: str | Path) -> TaskRecord:
        task = self.repository.create(description, workspace)
        with self._task_lock(task.id):
            loop = self.loop_factory(Path(task.workspace), task.id)
            self._loops[task.id] = loop
            self._synced_event_counts[task.id] = 0
            loop.start(description)
            return self._sync(task.id, loop)

    def advance(self, task_id: str) -> TaskRecord:
        with self._task_lock(task_id):
            loop = self._require_loop(task_id)
            loop.step()
            return self._sync(task_id, loop)

    def reject(self, task_id: str, reason: str = "") -> TaskRecord:
        with self._task_lock(task_id):
            loop = self._require_loop(task_id)
            fingerprint = self._pending_fingerprint(loop)
            loop.resolve_approval(fingerprint, approved=False)
            return self._sync(
                task_id,
                loop,
                approval=ApprovalDecision(fingerprint, "rejected"),
                final_event_data={"reason": reason},
            )

    def approve(self, task_id: str, fingerprint: str) -> TaskRecord:
        with self._task_lock(task_id):
            loop = self._require_loop(task_id)
            if self._pending_fingerprint(loop) != fingerprint:
                raise ApprovalMismatch(
                    "approval fingerprint does not match pending action"
                )
            persisted = self.repository.get(task_id)
            self.repository.commit_transition(
                persisted,
                [
                    LoopEvent(
                        kind=EventKind.APPROVAL,
                        summary="Approval intent recorded before execution.",
                        data={
                            "decision": "approved",
                            "fingerprint": fingerprint,
                            "phase": "intent",
                        },
                    )
                ],
                approval=ApprovalDecision(fingerprint, "approved"),
            )
            loop.resolve_approval(fingerprint, approved=True)
            return self._sync(task_id, loop)

    def cancel(self, task_id: str) -> TaskRecord:
        with self._task_lock(task_id):
            loop = self._require_loop(task_id)
            loop.cancel()
            return self._sync(task_id, loop)

    def _task_lock(self, task_id: str) -> RLock:
        with self._task_locks_guard:
            return self._task_locks.setdefault(task_id, RLock())

    @staticmethod
    def _pending_fingerprint(loop: AgentLoop) -> str:
        state = loop.state
        if state is None or state.pending_decision is None:
            raise ApprovalMismatch("task has no pending approval")
        return state.pending_decision.fingerprint

    def _require_loop(self, task_id: str) -> AgentLoop:
        loop = self._loops.get(task_id)
        if loop is not None:
            return loop
        self.repository.get(task_id)
        raise TaskNotLoaded(task_id)

    def _sync(
        self,
        task_id: str,
        loop: AgentLoop,
        *,
        approval: ApprovalDecision | None = None,
        final_event_data: dict[str, object] | None = None,
    ) -> TaskRecord:
        state = loop.state
        if state is None:
            raise RuntimeError("AgentLoop has not been started.")
        persisted = self.repository.get(task_id)
        task = TaskRecord(
            id=persisted.id,
            description=state.description,
            workspace=persisted.workspace,
            status=state.status,
            step_count=state.step_count,
            last_validation_passed=state.last_validation_passed,
            pending_action=state.pending_action,
            created_at=persisted.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        synced_count = self._synced_event_counts[task_id]
        pending_events = list(state.events[synced_count:])
        if final_event_data and pending_events:
            final_event = pending_events[-1]
            pending_events[-1] = LoopEvent(
                kind=final_event.kind,
                summary=final_event.summary,
                data={**final_event.data, **final_event_data},
            )
        self.repository.commit_transition(
            task, pending_events, approval=approval
        )
        self._synced_event_counts[task_id] = len(state.events)
        return task
