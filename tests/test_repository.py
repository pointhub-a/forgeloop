import pytest

from forgeloop.models import Action
from forgeloop.repository import ApprovalRepository, TaskNotFound, TaskRepository


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "forgeloop.db"


def test_event_sequence_is_monotonic_across_reopen(db_path):
    first = TaskRepository(db_path)
    task = first.create("fix", "/tmp/work")
    first.append_event(task.id, "state", "created", {})

    second = TaskRepository(db_path)
    event = second.append_event(task.id, "state", "running", {})

    assert event.sequence == 2


def test_task_projection_round_trips_through_save_and_get(db_path, tmp_path):
    repository = TaskRepository(db_path)
    task = repository.create("fix", tmp_path / "nested" / "..")

    task.status = "waiting_approval"
    task.step_count = 3
    task.last_validation_passed = True
    task.pending_action = Action(
        kind="run_command",
        arguments={"env": {"B": "2", "A": "1"}, "argv": ["make", "test"]},
    )

    repository.save(task)

    assert repository.get(task.id) == task
    assert task.workspace == str(tmp_path.resolve())


def test_list_events_returns_ordered_structured_audit_data(db_path):
    repository = TaskRepository(db_path)
    task = repository.create("fix", "/tmp/work")
    first = repository.append_event(
        task.id,
        "action",
        "proposed",
        {"nested": {"z": 1, "a": [True, None]}},
    )
    second = repository.append_event(task.id, "state", "running", {})

    events = repository.list_events(task.id)

    assert events == [first, second]


def test_approval_audit_records_persist_across_reopen(db_path):
    tasks = TaskRepository(db_path)
    task = tasks.create("fix", "/tmp/work")
    first = ApprovalRepository(db_path)

    recorded = first.record(task.id, "fingerprint-1", "approved")

    assert recorded.task_id == task.id
    assert recorded.action_fingerprint == "fingerprint-1"
    assert recorded.decision == "approved"
    assert recorded.used_at is not None
    assert ApprovalRepository(db_path).list_for_task(task.id) == [recorded]


def test_unknown_task_mutations_fail_with_task_not_found(db_path):
    repository = TaskRepository(db_path)
    task = repository.create("fix", "/tmp/work").model_copy(
        update={"id": "missing-task"}
    )

    with pytest.raises(TaskNotFound):
        repository.get("missing-task")
    with pytest.raises(TaskNotFound):
        repository.save(task)
    with pytest.raises(TaskNotFound):
        repository.append_event("missing-task", "state", "missing", {})


def test_save_renormalizes_workspace_path(db_path, tmp_path):
    repository = TaskRepository(db_path)
    task = repository.create("fix", tmp_path)
    task.workspace = str(tmp_path / "nested" / "..")

    repository.save(task)

    assert task.workspace == str(tmp_path.resolve())
    assert repository.get(task.id).workspace == str(tmp_path.resolve())


def test_approval_for_unknown_task_fails_with_task_not_found(db_path):
    approvals = ApprovalRepository(db_path)

    with pytest.raises(TaskNotFound):
        approvals.record("missing-task", "fingerprint", "approved")
