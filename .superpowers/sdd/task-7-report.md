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

---

## External review remediation

The external review identified transaction, side-effect ordering, concurrency,
migration, and audit-detail gaps. All requested fixes were completed test-first.

### RED / GREEN evidence

1. Atomic task/event transitions
   - RED: a real SQLite trigger failed the second event insert; the new public
     `commit_transition` interface was initially absent.
   - GREEN: task projection update, contiguous event allocation, and all event
     inserts now share one `BEGIN IMMEDIATE`; the injected failure rolls back
     the projection and every event.
2. Durable approval intent before external effects
   - RED: an approvals-table trigger aborted the decision write, but the old
     service had already executed `rm -rf build`.
   - GREEN: approve first atomically records the unique approved decision and an
     `approval` intent event, then calls `resolve_approval`; an intent failure
     leaves the directory, task, events, and approval table unchanged.
3. Rejection transaction and reason audit
   - RED: the rejection reason was absent from event data, and an injected event
     failure left a separately committed rejected approval row.
   - GREEN: rejection enriches its approval event with `reason` and commits the
     decision, post-resolution projection, and loop events in one transaction.
4. Same-task serialization
   - RED: two synchronized worker threads entered the same provider concurrently
     (`max_active == 2`) and raced event synchronization.
   - GREEN: a per-task `threading.RLock`, created under a small lock-map guard,
     covers create-after-ID, advance, approve, reject, cancel, and persistence;
     provider overlap is 1 and the audit sequence contains exactly one copy of
     each event.
5. Concurrent repository allocation
   - Two `TaskRepository` instances appending from two threads produced the
     complete unique sequence `1..20`, directly exercising `BEGIN IMMEDIATE`.
6. Explicit migration transaction
   - RED: unknown version 99 was silently accepted; duplicate authoritative
     version rows were accepted; an authorizer-injected DDL failure left
     `schema_version` and `tasks` partially committed.
   - GREEN: migration now begins an explicit immediate transaction, validates
     exactly one supported version record, and atomically creates all four
     tables plus version 1. Unknown/multiple versions fail closed and DDL
     failure leaves no created tables.

### Post-remediation verification

Focused repository/service suite:

```text
.venv/bin/python -m pytest tests/test_repository.py tests/test_service.py -q
..........................                                               [100%]
26 passed in 0.20s
```

Full regression suite:

```text
.venv/bin/python -m pytest -q
........................................................................ [ 37%]
........................................................................ [ 75%]
..............................................                           [100%]
190 passed in 5.71s
```

`git diff --check` passed. Tests use real SQLite transactions/triggers and a
complete deterministic Provider test double only where provider overlap must be
observed. No test-only production methods or fabricated restart recovery were
added. The controller-owned `PLAN.md` modification remains outside the Task 7
staging set. No unresolved concern remains.

---

## Second external review remediation

The follow-up review found four approval-lifecycle gaps. Each was repaired in a
focused RED/GREEN cycle.

1. Complete validation before durable intent
   - RED: after the live policy stopped requiring the pending approval, service
     still persisted an approved intent and only then failed inside
     `resolve_approval`.
   - GREEN: `AgentLoop.validate_pending_approval(fingerprint) -> Action`
     performs the complete side-effect-free canonical snapshot, fingerprint,
     consumption, policy, and rule checks. `resolve_approval` reuses it, and
     service calls it before writing intent; invalidated policy leaves no intent,
     approval row, task change, or event.
2. Rejection rollback restores the live loop
   - RED: a trigger-rolled-back rejection left SQLite waiting but the in-process
     loop running, so rejection could not be retried.
   - GREEN: `AgentLoop.checkpoint/restore` deep-copy `LoopState` and preserve the
     four private pending fields. Service checkpoints before rejection and
     restores on transaction failure; after removing the trigger, the same live
     task rejects successfully once.
3. Idempotent approval finalization
   - RED: after intent persisted and the tool executed, an injected final-event
     failure left the tool result unsynced; retrying the same fingerprint raised
     mismatch instead of finishing persistence.
   - GREEN: service tracks in-process task-to-fingerprint pending finalization.
     The same fingerprint retries only `_sync`, never the intent or tool, and
     clears the marker after success. The regression recreates the command
     target before retry and proves it remains untouched, with one intent and
     one approval row.
4. Rejection reason targets the rejection event
   - RED: with `max_steps=1`, rejection emitted an approval event followed by a
     budget state event; the old final-event merge omitted reason from approval
     and overwrote the state's `reason=steps`.
   - GREEN: `_sync` searches the current pending events in reverse for the
     `EventKind.APPROVAL` rejection event and enriches only it. The following
     state event retains its stop reason.

Post-remediation focused verification:

```text
.venv/bin/python -m pytest tests/test_loop.py tests/test_repository.py tests/test_service.py -q
......................................................                   [100%]
54 passed in 0.34s
```

Full regression verification:

```text
.venv/bin/python -m pytest -q
........................................................................ [ 37%]
........................................................................ [ 74%]
.................................................                        [100%]
193 passed in 5.74s
```

`git diff --check` passed. The controller-owned `PLAN.md` remains unstaged.
No unresolved concern remains.
