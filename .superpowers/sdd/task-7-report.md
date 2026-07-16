# Task 7 Report — SQLite Task Repository and In-Process Service

## Status

PASS. Implemented transactional SQLite task/event/approval persistence and a
synchronous `TaskService` that owns live `AgentLoop` instances in process.

## Delivered files

- `src/forgeloop/repository.py`
- `src/forgeloop/service.py`
- `tests/test_repository.py`
- `tests/test_service.py`

## Interface clarification

The controller fixed the factory contract as
`loop_factory(workspace: Path, task_id: str) -> AgentLoop`. `TaskService.create`
first obtains the repository-generated task ID, then passes the normalized
workspace path and task ID to the factory. No provider argument or fabricated
restart reconstruction was added.

## TDD evidence

Production behavior was added through focused RED/GREEN cycles:

1. Repository module and monotonic events
   - RED: `ModuleNotFoundError: No module named 'forgeloop.repository'`.
   - GREEN: cross-instance append returned sequence 2.
2. Task projection and event audit reads
   - RED: missing `save`, then missing `list_events`.
   - GREEN: task/action JSON and ordered structured events round-tripped.
3. Approval audit persistence
   - RED: missing `ApprovalRepository` import.
   - GREEN: approval records survived repository reopen.
4. Repository error and path boundaries
   - RED: unknown event/approval writes leaked SQLite foreign-key errors; saved
     workspace remained non-canonical.
   - GREEN: writes raise `TaskNotFound` transactionally and saved paths are
     normalized absolute paths.
5. Service create and advance
   - RED: missing service module, then missing `advance`.
   - GREEN: loop creation uses the exact factory signature; task projections,
     pending action, and only newly emitted loop events persist.
6. Approval rejection and fail-closed approval
   - RED: missing `reject` and `approve`.
   - GREEN: rejection resumes running, mismatch changes no audit state, correct
     approval executes once, persists once, and cannot be replayed.
7. Cancellation and restart boundary
   - RED: missing `cancel`; four restart mutations reached an intentional
     unimplemented sentinel instead of `TaskNotLoaded`.
   - GREEN: cancellation is persisted without duplicate events; advance,
     approve, reject, and cancel on a persisted but unloaded active task all
     raise `TaskNotLoaded` without invoking the factory.
8. Missing-task distinction
   - RED: `TaskNotFound` was not exported by the service module.
   - GREEN: unknown IDs raise `TaskNotFound`; persisted unloaded IDs raise
     `TaskNotLoaded`.

## Verification

Focused suite:

```text
.venv/bin/python -m pytest tests/test_repository.py tests/test_service.py -q
..................                                                       [100%]
18 passed in 0.09s
```

Full regression suite:

```text
.venv/bin/python -m pytest -q
........................................................................ [ 39%]
........................................................................ [ 79%]
......................................                                   [100%]
182 passed in 5.59s
```

`git diff --check` completed with no whitespace errors before the report was
written.

## Self-review

- SQLite enables foreign keys per connection and WAL during migration.
- `schema_version`, `tasks`, `events`, and `approvals` share one database.
- Event sequence allocation and insertion occur in one `BEGIN IMMEDIATE`
  transaction under a per-task unique constraint.
- Approval validation and insertion occur in one immediate transaction; the
  `(task_id, action_fingerprint)` unique constraint enforces one-time audit use.
- Pending actions and event data use compact, sorted-key JSON.
- The service persists the loop projection and advances its synced event index
  only after each newly emitted event is stored, preventing normal-operation
  duplicates.
- Tests use real SQLite and a real `AgentLoop` with scripted provider and real
  policy/tool runtime; no test-only production methods or mock assertions were
  added.
- Controller-owned `PLAN.md` remains outside this task's staging set.

## Concerns / handoff

No unresolved implementation concern remains. The explicit product limitation
is preserved: task/event/approval audit data survives restart, while active
provider/loop continuation does not. Mutations after restart fail with
`TaskNotLoaded`; no snapshot or provider-state recovery is fabricated.
