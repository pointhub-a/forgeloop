import json
from pathlib import Path

import pytest

from forgeloop.config import HarnessConfig
from forgeloop.feedback import ProgressTracker
from forgeloop.loop import AgentLoop
from forgeloop.memory import MemoryStore
from forgeloop.policy import PolicyEngine, action_fingerprint
from forgeloop.providers import ScriptedProvider
from forgeloop.repository import ApprovalRepository, TaskRepository
from forgeloop.service import (
    ApprovalMismatch,
    TaskNotFound,
    TaskNotLoaded,
    TaskService,
)
from forgeloop.tools import ToolRuntime


def action(kind, **arguments):
    return json.dumps({"kind": kind, "arguments": arguments})


class NoValidators:
    def run_all(self):
        return []


def loop_factory_for(responses):
    calls = []

    def factory(workspace: Path, task_id: str):
        calls.append((workspace, task_id))
        config = HarnessConfig(max_steps=10)
        return AgentLoop(
            provider=ScriptedProvider(list(responses)),
            policy=PolicyEngine(config),
            tools=ToolRuntime(workspace, config),
            validators=NoValidators(),
            progress=ProgressTracker(
                max_identical_failures=config.max_identical_failures,
                max_identical_actions=config.max_identical_actions,
            ),
            memory=MemoryStore(":memory:"),
            config=config,
            project_id=task_id,
        )

    return factory, calls


@pytest.fixture
def repositories(tmp_path):
    db_path = tmp_path / "forgeloop.db"
    return TaskRepository(db_path), ApprovalRepository(db_path)


def test_create_starts_loop_and_persists_projection_and_event(repositories, tmp_path):
    tasks, approvals = repositories
    factory, factory_calls = loop_factory_for([])
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    task = service.create("fix", workspace / ".")

    assert factory_calls == [(workspace.resolve(), task.id)]
    assert task.status == "running"
    assert tasks.get(task.id) == task
    events = tasks.list_events(task.id)
    assert [(event.sequence, event.kind.value) for event in events] == [(1, "state")]
    assert events[0].data == {"status": "running"}


def test_advance_persists_pending_action_and_only_new_loop_events(
    repositories, tmp_path
):
    tasks, approvals = repositories
    factory, _ = loop_factory_for(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = service.create("clean", workspace)
    created_events = tasks.list_events(task.id)

    pending = service.advance(task.id)

    assert pending.status == "waiting_approval"
    assert pending.step_count == 1
    assert pending.pending_action is not None
    assert tasks.get(task.id) == pending
    events = tasks.list_events(task.id)
    assert events[:1] == created_events
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.kind.value for event in events] == [
        "state",
        "model_request",
        "action",
        "governance_decision",
        "state",
    ]


def test_reject_pending_action_returns_task_to_running(repositories, tmp_path):
    tasks, approvals = repositories
    factory, _ = loop_factory_for(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pending = service.advance(service.create("clean", workspace).id)
    fingerprint = action_fingerprint(pending.pending_action)
    events_before = tasks.list_events(pending.id)

    task = service.reject(pending.id, reason="not allowed")

    assert task.status == "running"
    assert task.pending_action is None
    assert tasks.get(task.id) == task
    decisions = approvals.list_for_task(task.id)
    assert [(item.action_fingerprint, item.decision) for item in decisions] == [
        (fingerprint, "rejected")
    ]
    events = tasks.list_events(task.id)
    assert events[: len(events_before)] == events_before
    assert events[-1].kind.value == "approval"
    assert events[-1].data == {"approved": False, "fingerprint": fingerprint}


def test_approval_for_wrong_fingerprint_fails_closed(repositories, tmp_path):
    tasks, approvals = repositories
    factory, _ = loop_factory_for(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pending = service.advance(service.create("clean", workspace).id)
    events_before = tasks.list_events(pending.id)

    with pytest.raises(ApprovalMismatch):
        service.approve(pending.id, "wrong")

    assert tasks.get(pending.id) == pending
    assert tasks.list_events(pending.id) == events_before
    assert approvals.list_for_task(pending.id) == []


def test_approval_is_persisted_and_cannot_be_replayed(repositories, tmp_path):
    tasks, approvals = repositories
    factory, _ = loop_factory_for(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "build").mkdir()
    pending = service.advance(service.create("clean", workspace).id)
    fingerprint = action_fingerprint(pending.pending_action)

    approved = service.approve(pending.id, fingerprint)

    assert approved.status == "running"
    assert approved.pending_action is None
    assert not (workspace / "build").exists()
    records = approvals.list_for_task(pending.id)
    assert [(item.action_fingerprint, item.decision) for item in records] == [
        (fingerprint, "approved")
    ]
    events_after_approval = tasks.list_events(pending.id)
    (workspace / "build").mkdir()

    with pytest.raises(ApprovalMismatch):
        service.approve(pending.id, fingerprint)

    assert (workspace / "build").exists()
    assert approvals.list_for_task(pending.id) == records
    assert tasks.list_events(pending.id) == events_after_approval


def test_cancel_persists_state_without_duplicate_events(repositories, tmp_path):
    tasks, approvals = repositories
    factory, _ = loop_factory_for([])
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = service.create("fix", workspace)

    cancelled = service.cancel(task.id)

    assert cancelled.status == "cancelled"
    assert tasks.get(task.id) == cancelled
    events = tasks.list_events(task.id)
    assert [event.kind.value for event in events] == ["state", "state"]
    assert events[-1].data == {"status": "cancelled"}

    service.cancel(task.id)

    assert tasks.list_events(task.id) == events


@pytest.mark.parametrize("operation", ["advance", "approve", "reject", "cancel"])
def test_active_task_cannot_be_mutated_after_service_restart(
    repositories, tmp_path, operation
):
    tasks, approvals = repositories
    factory, _ = loop_factory_for(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = TaskService(tasks, approvals, factory)
    pending = original.advance(original.create("clean", workspace).id)
    fingerprint = action_fingerprint(pending.pending_action)
    persisted_events = tasks.list_events(pending.id)
    restart_factory, restart_calls = loop_factory_for([])
    restarted = TaskService(tasks, approvals, restart_factory)

    with pytest.raises(TaskNotLoaded):
        if operation == "advance":
            restarted.advance(pending.id)
        elif operation == "approve":
            restarted.approve(pending.id, fingerprint)
        elif operation == "reject":
            restarted.reject(pending.id, reason="no")
        else:
            restarted.cancel(pending.id)

    assert restart_calls == []
    assert tasks.get(pending.id) == pending
    assert tasks.list_events(pending.id) == persisted_events


def test_unknown_task_is_distinct_from_persisted_not_loaded_task(repositories):
    tasks, approvals = repositories
    factory, _ = loop_factory_for([])
    service = TaskService(tasks, approvals, factory)

    with pytest.raises(TaskNotFound):
        service.advance("missing-task")
