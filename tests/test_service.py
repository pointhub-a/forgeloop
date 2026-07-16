from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading
import time

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
    InvalidStateTransition,
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


class OverlapDetectProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.max_active = 0
        self._active = 0
        self._guard = threading.Lock()

    def complete(self, messages, action_schema):
        with self._guard:
            index = len(self.calls)
            self.calls.append((list(messages), action_schema))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.03)
            return self.responses[index]
        finally:
            with self._guard:
                self._active -= 1


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
    assert events[-1].data == {
        "approved": False,
        "fingerprint": fingerprint,
        "reason": "not allowed",
    }


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

    with pytest.raises(InvalidStateTransition):
        service.cancel(task.id)

    assert tasks.list_events(task.id) == events


def test_concurrent_cancel_allows_exactly_one_transition(repositories, tmp_path):
    tasks, approvals = repositories
    factory, _ = loop_factory_for([])
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = service.create("fix", workspace)
    start = threading.Barrier(3)

    def cancel_once():
        start.wait()
        try:
            return service.cancel(task.id)
        except InvalidStateTransition as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(cancel_once) for _ in range(2)]
        start.wait()
        results = [future.result() for future in futures]

    assert sum(isinstance(result, InvalidStateTransition) for result in results) == 1
    assert sum(getattr(result, "status", None) == "cancelled" for result in results) == 1
    assert [event.data for event in tasks.list_events(task.id)] == [
        {"status": "running"},
        {"status": "cancelled"},
    ]


def test_terminal_task_cannot_advance(repositories, tmp_path):
    tasks, approvals = repositories
    factory, _ = loop_factory_for([action("read_file", path="note.txt")])
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = service.create("read", workspace)
    service.cancel(task.id)
    loop = service._loops[task.id]
    events_before = tasks.list_events(task.id)

    with pytest.raises(InvalidStateTransition):
        service.advance(task.id)

    assert loop.state.step_count == 0
    assert tasks.list_events(task.id) == events_before


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


def test_approval_intent_failure_never_executes_pending_tool(
    repositories, tmp_path
):
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
    events_before = tasks.list_events(pending.id)
    with sqlite3.connect(tasks.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_approval_intent
            BEFORE INSERT ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'forced approval intent failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="forced approval intent failure"
    ):
        service.approve(pending.id, fingerprint)

    assert (workspace / "build").is_dir()
    assert tasks.get(pending.id) == pending
    assert tasks.list_events(pending.id) == events_before
    assert approvals.list_for_task(pending.id) == []


def test_rejection_audit_failure_rolls_back_approval_projection_and_events(
    repositories, tmp_path
):
    tasks, approvals = repositories
    factory, _ = loop_factory_for(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    service = TaskService(tasks, approvals, factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pending = service.advance(service.create("clean", workspace).id)
    loop = service._loops[pending.id]
    events_before = tasks.list_events(pending.id)
    with sqlite3.connect(tasks.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_rejection_audit
            BEFORE INSERT ON events
            WHEN NEW.summary = 'Pending action rejected.'
            BEGIN
                SELECT RAISE(ABORT, 'forced rejection audit failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="forced rejection audit failure"
    ):
        service.reject(pending.id, reason="not allowed")

    assert tasks.get(pending.id) == pending
    assert tasks.list_events(pending.id) == events_before
    assert approvals.list_for_task(pending.id) == []
    assert loop.state.status == "waiting_approval"
    assert loop.state.pending_action is not None

    with sqlite3.connect(tasks.db_path) as connection:
        connection.execute("DROP TRIGGER fail_rejection_audit")
    retried = service.reject(pending.id, reason="not allowed")

    assert retried.status == "running"
    assert retried.pending_action is None
    assert len(approvals.list_for_task(pending.id)) == 1


def test_same_task_advances_are_serialized_through_provider_and_persistence(
    repositories, tmp_path
):
    tasks, approvals = repositories
    provider = OverlapDetectProvider(
        [
            action("read_file", path="note.txt"),
            action("read_file", path="note.txt"),
        ]
    )

    def factory(workspace: Path, task_id: str):
        config = HarnessConfig(max_steps=10)
        return AgentLoop(
            provider=provider,
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

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello")
    service = TaskService(tasks, approvals, factory)
    task = service.create("read", workspace)
    start = threading.Barrier(3)

    def advance_once():
        start.wait()
        return service.advance(task.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(advance_once) for _ in range(2)]
        start.wait()
        results = [future.result() for future in futures]

    persisted = tasks.get(task.id)
    events = tasks.list_events(task.id)
    assert provider.max_active == 1
    assert len(provider.calls) == 2
    assert persisted.step_count == 2
    assert all(result.step_count in {1, 2} for result in results)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len(events) == 9


def test_full_pending_validation_happens_before_approval_intent(
    repositories, tmp_path
):
    tasks, approvals = repositories
    created = {}

    def factory(workspace: Path, task_id: str):
        config = HarnessConfig(max_steps=10)
        loop = AgentLoop(
            provider=ScriptedProvider(
                [action("run_command", argv=["rm", "-rf", "build"])]
            ),
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
        created.update(config=config, loop=loop)
        return loop

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = TaskService(tasks, approvals, factory)
    pending = service.advance(service.create("clean", workspace).id)
    fingerprint = action_fingerprint(pending.pending_action)
    events_before = tasks.list_events(pending.id)
    created["config"].approval_rule_ids = []

    with pytest.raises(
        ApprovalMismatch, match="pending action no longer has the same policy decision"
    ):
        service.approve(pending.id, fingerprint)

    assert created["loop"].state.status == "waiting_approval"
    assert tasks.get(pending.id) == pending
    assert tasks.list_events(pending.id) == events_before
    assert approvals.list_for_task(pending.id) == []


def test_approval_final_sync_failure_retries_without_reexecuting_tool_or_intent(
    repositories, tmp_path
):
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
    events_before = tasks.list_events(pending.id)
    with sqlite3.connect(tasks.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_approval_final_sync
            BEFORE INSERT ON events
            WHEN NEW.summary = 'Pending action approved.'
            BEGIN
                SELECT RAISE(ABORT, 'forced approval final sync failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="forced approval final sync failure"
    ):
        service.approve(pending.id, fingerprint)

    assert not (workspace / "build").exists()
    assert tasks.get(pending.id) == pending
    intent_events = tasks.list_events(pending.id)
    assert intent_events[: len(events_before)] == events_before
    assert [event.data.get("phase") for event in intent_events].count("intent") == 1
    assert len(approvals.list_for_task(pending.id)) == 1

    with sqlite3.connect(tasks.db_path) as connection:
        connection.execute("DROP TRIGGER fail_approval_final_sync")
    (workspace / "build").mkdir()

    finalized = service.approve(pending.id, fingerprint)

    assert finalized.status == "running"
    assert finalized.pending_action is None
    assert (workspace / "build").is_dir()
    events = tasks.list_events(pending.id)
    assert [event.data.get("phase") for event in events].count("intent") == 1
    assert len(approvals.list_for_task(pending.id)) == 1


def test_rejection_reason_is_attached_to_approval_when_state_event_follows(
    repositories, tmp_path
):
    tasks, approvals = repositories

    def factory(workspace: Path, task_id: str):
        config = HarnessConfig(max_steps=1)
        return AgentLoop(
            provider=ScriptedProvider(
                [action("run_command", argv=["rm", "-rf", "build"])]
            ),
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

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = TaskService(tasks, approvals, factory)
    pending = service.advance(service.create("clean", workspace).id)
    event_count = len(tasks.list_events(pending.id))

    rejected = service.reject(pending.id, reason="operator denied")

    assert rejected.status == "budget_exhausted"
    new_events = tasks.list_events(pending.id)[event_count:]
    rejection = next(
        event
        for event in new_events
        if event.kind.value == "approval" and event.data.get("approved") is False
    )
    assert rejection.data["reason"] == "operator denied"
    assert new_events[-1].kind.value == "state"
    assert new_events[-1].data["reason"] == "steps"
