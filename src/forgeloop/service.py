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
        self._pending_finalizations: dict[str, str] = {}
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
            checkpoint = loop.checkpoint()
            try:
                loop.resolve_approval(fingerprint, approved=False)
                return self._sync(
                    task_id,
                    loop,
                    approval=ApprovalDecision(fingerprint, "rejected"),
                    rejection_event_data={"reason": reason},
                )
            except Exception:
                loop.restore(checkpoint)
                raise

    def approve(self, task_id: str, fingerprint: str) -> TaskRecord:
        with self._task_lock(task_id):
            loop = self._require_loop(task_id)
            pending_finalization = self._pending_finalizations.get(task_id)
            if pending_finalization is not None:
                if pending_finalization != fingerprint:
                    raise ApprovalMismatch(
                        "approval fingerprint does not match pending finalization"
                    )
                return self._finalize_approval(task_id, loop, fingerprint)
            loop.validate_pending_approval(fingerprint)
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
            self._pending_finalizations[task_id] = fingerprint
            return self._finalize_approval(task_id, loop, fingerprint)

    def cancel(self, task_id: str) -> TaskRecord:
        with self._task_lock(task_id):
            loop = self._require_loop(task_id)
            loop.cancel()
            return self._sync(task_id, loop)

    def _task_lock(self, task_id: str) -> RLock:
        with self._task_locks_guard:
            return self._task_locks.setdefault(task_id, RLock())

    def _finalize_approval(
        self, task_id: str, loop: AgentLoop, fingerprint: str
    ) -> TaskRecord:
        task = self._sync(task_id, loop)
        if self._pending_finalizations.get(task_id) == fingerprint:
            del self._pending_finalizations[task_id]
        return task

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
        rejection_event_data: dict[str, object] | None = None,
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
        if rejection_event_data:
            for index in range(len(pending_events) - 1, -1, -1):
                event = pending_events[index]
                if (
                    event.kind == EventKind.APPROVAL
                    and event.data.get("approved") is False
                ):
                    pending_events[index] = LoopEvent(
                        kind=event.kind,
                        summary=event.summary,
                        data={**event.data, **rejection_event_data},
                    )
                    break
            else:
                raise RuntimeError("pending rejection approval event was not found")
        self.repository.commit_transition(
            task, pending_events, approval=approval
        )
        self._synced_event_counts[task_id] = len(state.events)
        return task
