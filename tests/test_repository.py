from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from forgeloop.loop import LoopEvent
from forgeloop.models import Action, TaskStatus
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


def test_commit_transition_rolls_back_projection_and_all_events_on_event_failure(
    db_path,
):
    repository = TaskRepository(db_path)
    original = repository.create("fix", "/tmp/work")
    changed = original.model_copy(
        update={"status": TaskStatus.RUNNING, "step_count": 1}
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_selected_event
            BEFORE INSERT ON events
            WHEN NEW.summary = 'forced failure'
            BEGIN
                SELECT RAISE(ABORT, 'forced event failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
        repository.commit_transition(
            changed,
            [
                LoopEvent(kind="state", summary="would persist"),
                LoopEvent(kind="state", summary="forced failure"),
            ],
        )

    assert repository.get(original.id) == original
    assert repository.list_events(original.id) == []


def test_concurrent_repositories_allocate_unique_contiguous_event_sequences(db_path):
    first = TaskRepository(db_path)
    second = TaskRepository(db_path)
    task = first.create("fix", "/tmp/work")
    start = threading.Barrier(3)

    def append_many(repository, label):
        start.wait()
        return [
            repository.append_event(task.id, "state", f"{label}-{index}", {})
            for index in range(10)
        ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(append_many, first, "first"),
            executor.submit(append_many, second, "second"),
        ]
        start.wait()
        stored = [event for future in futures for event in future.result()]

    assert sorted(event.sequence for event in stored) == list(range(1, 21))
    assert [event.sequence for event in first.list_events(task.id)] == list(
        range(1, 21)
    )


def test_unknown_schema_version_is_rejected(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version (version) VALUES (99)")

    with pytest.raises(RuntimeError, match="unsupported schema version: 99"):
        TaskRepository(db_path)


def test_schema_version_requires_one_authoritative_record(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.executemany(
            "INSERT INTO schema_version (version) VALUES (?)", [(1,), (1,)]
        )

    with pytest.raises(
        RuntimeError, match="schema_version must contain exactly one record"
    ):
        TaskRepository(db_path)


def test_migration_ddl_and_version_insert_roll_back_together(db_path):
    class DenyEventsMigrationRepository(TaskRepository):
        def _connect(self):
            connection = super()._connect()

            def authorizer(action_code, arg1, _arg2, _database, _source):
                if action_code == sqlite3.SQLITE_CREATE_TABLE and arg1 == "events":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorizer)
            return connection

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        DenyEventsMigrationRepository(db_path)

    with sqlite3.connect(db_path) as connection:
        created = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert created == set()
