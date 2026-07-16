# Task 6 Report — Provider Abstraction and Self-Written Agent Loop

## Status

PASS. Implemented the provider abstraction, strict action parser, single-request
Chat Completions adapter, stateful one-decision agent loop, validation feedback,
governance/HITL resume, memory actions, bounded context, progress detection, and
deterministic stopping without an agent framework or network-dependent tests.

## TDD record

1. Scripted provider
   - RED: missing module, then 2 expected `NotImplementedError` failures.
   - GREEN: 2 passed; immutable response tuple, indexed consumption, copied call
     recording, and deterministic exhaustion.
2. Parser and HTTP provider
   - RED: missing interfaces, then 14 expected failures with 2 prior tests passing.
   - GREEN: 16 passed; optional JSON fence, one strict object, Action validation,
     one urllib request, role normalization, response-shape checks, and redaction.
3. Feedback correction loop
   - RED: missing loop module, then 1 expected `NotImplementedError` failure.
   - GREEN: 1 passed; failed validation became feedback and drove edit/revalidation.
4. Governance and finish gate
   - RED: 1 expected failure because the destructive command executed.
   - GREEN: 3 passed; approval pause occurs before execution and unvalidated finish
     is rejected.
5. Stop order and bounded context
   - RED: 7 expected failures for provider/parse recovery, no-progress, 64 KiB
     context, and cancellation; wall-clock test separately failed on missing reason.
   - GREEN: 12 passed; failures count as steps, stop ordering is deterministic, and
     each `step()` makes exactly one model decision.
6. Memory and HITL resume
   - RED: missing approval interface, then 3 expected failures.
   - GREEN: 15 passed; project-scoped bounded recall, exact single-use approvals,
     execute-once approval, and feedback-producing rejection.
7. Stale validation gate
   - RED: 1 expected failure because finish succeeded after a workspace mutation.
   - GREEN: 1 passed; successful mutations invalidate prior passing validation.

## Verification

- Focused: `.venv/bin/python -m pytest tests/test_providers.py tests/test_loop.py -q`
  — **32 passed in 0.05s**.
- Full: `.venv/bin/python -m pytest -q`
  — **155 passed in 5.42s**.
- `git diff --check` passed for tracked changes; the four implementation/test files
  were new and visually checked for whitespace and conflict markers.
- Tests are deterministic and make no network requests; HTTP behavior uses an
  injected one-response opener.

## Self-review

- Confirmed the HTTP layer uses one Chat Completions-style urllib request and does
  not use the OpenAI SDK or an agent framework.
- Confirmed internal `feedback`/`tool` roles remain unchanged for
  `ScriptedProvider` and are prefixed user messages only in the HTTP adapter.
- Confirmed API keys are excluded from provider repr and raised error text.
- Confirmed approval fingerprints are consumed before execution and cannot execute
  twice after mismatch, rejection, or reuse.
- Confirmed `LoopState` owns every required field and `run()` stops on terminal or
  waiting states.
- No unresolved concerns.

## External review remediation

External review reported two Critical and three Important issues. All five were
fixed test-first:

1. Canonical pending approval authority
   - RED: mutating `state.pending_action.arguments` caused the mutated command B to
     execute instead of approved command A.
   - GREEN: pending action JSON, fingerprint, rule ID, and deferred progress are
     private loop state. Resolution rebuilds `Action` from the canonical snapshot,
     recomputes its fingerprint, and re-evaluates the same `require_approval` policy
     decision before consuming the approval and executing the rebuilt action.
   - Approval focused result: **3 passed** after this fix.
2. Validation invalidation before mutation attempts
   - RED: a command wrote a side-effect file, exited nonzero, and then incorrectly
     finished using the earlier passing validation.
   - GREEN: `write_file`, `replace_text`, and `run_command` attempts invalidate prior
     validation immediately before runtime execution, independent of result status.
   - Mutation focused result: **2 passed**.
3. Re-proposed consumed approvals
   - RED: the same consumed fingerprint created a second waiting approval.
   - GREEN: consumed fingerprints now produce safe feedback and an approval event,
     stay non-pending/running, and allow the model to choose a different next action.
   - Focused result: **1 passed**.
4. Approval-resume stop settlement
   - RED: both approval and rejection returned `running` despite an exhausted step
     budget.
   - GREEN: both paths call the unified stop-order function with deferred progress;
     parameterized approval/rejection tests prove `max_steps=1` stops without a
     second provider call.
   - Approval focused result after all approval fixes: **6 passed**.
5. Memory backend exception boundary
   - RED: SQLite and OS errors escaped from both remember and recall paths (4 failed).
   - GREEN: `sqlite3.Error` and `OSError` become fixed, redacted tool feedback/events
     without exposing backend details.
   - Memory backend focused result: **4 passed**.

Post-remediation verification:

- Focused: `.venv/bin/python -m pytest tests/test_providers.py tests/test_loop.py -q`
  — **41 passed in 0.16s**.
- Full: `.venv/bin/python -m pytest -q`
  — **164 passed in 5.57s**.
- `git diff --check` passed.
- No unresolved concerns after remediation.
