"""Synchronous in-process orchestration over persisted ForgeLoop audit state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from forgeloop.loop import AgentLoop, ApprovalMismatch
from forgeloop.models import TaskRecord
from forgeloop.repository import ApprovalRepository, TaskNotFound, TaskRepository


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

    def create(self, description: str, workspace: str | Path) -> TaskRecord:
        task = self.repository.create(description, workspace)
        loop = self.loop_factory(Path(task.workspace), task.id)
        self._loops[task.id] = loop
        self._synced_event_counts[task.id] = 0
        loop.start(description)
        return self._sync(task.id, loop)

    def advance(self, task_id: str) -> TaskRecord:
        loop = self._require_loop(task_id)
        loop.step()
        return self._sync(task_id, loop)

    def reject(self, task_id: str, reason: str = "") -> TaskRecord:
        loop = self._require_loop(task_id)
        fingerprint = self._pending_fingerprint(loop)
        loop.resolve_approval(fingerprint, approved=False)
        self.approvals.record(task_id, fingerprint, "rejected")
        return self._sync(task_id, loop)

    def approve(self, task_id: str, fingerprint: str) -> TaskRecord:
        loop = self._require_loop(task_id)
        loop.resolve_approval(fingerprint, approved=True)
        self.approvals.record(task_id, fingerprint, "approved")
        return self._sync(task_id, loop)

    def cancel(self, task_id: str) -> TaskRecord:
        loop = self._require_loop(task_id)
        loop.cancel()
        return self._sync(task_id, loop)

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

    def _sync(self, task_id: str, loop: AgentLoop) -> TaskRecord:
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
        self.repository.save(task)

        synced_count = self._synced_event_counts[task_id]
        for event in state.events[synced_count:]:
            self.repository.append_event(
                task_id,
                event.kind.value,
                event.summary,
                event.data,
            )
            synced_count += 1
            self._synced_event_counts[task_id] = synced_count
        return task
