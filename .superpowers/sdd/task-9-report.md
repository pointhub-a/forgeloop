# Task 9 Report: Deterministic Mechanism Demo and CLI

## Scope

Implemented the approved Task 9 design in:

- `src/forgeloop/demo.py`
- `src/forgeloop/cli.py`
- `scripts/mechanism_demo.py`
- `tests/test_demo.py`
- `tests/test_cli.py`

No `PLAN.md` changes are included.

## TDD evidence

### Demo RED

Command:

```text
.venv/bin/python -m pytest tests/test_demo.py -q
```

Observed expected collection failure:

```text
ModuleNotFoundError: No module named 'forgeloop.demo'
```

### Demo GREEN

After implementing the three real offline scenarios:

```text
1 passed in 0.13s
```

### CLI credentials/demo RED

Command:

```text
.venv/bin/python -m pytest tests/test_cli.py -q
```

Observed expected collection failure:

```text
ModuleNotFoundError: No module named 'forgeloop.cli'
```

### CLI credentials/demo GREEN

After implementing `demo --json` and all three credential commands:

```text
3 passed in 0.13s
```

### Serve composition RED

Five new tests failed for the expected missing behavior: `serve` was not an
argparse choice and `main` did not yet accept the injected opener.

```text
5 failed, 3 passed in 0.39s
```

### Serve composition GREEN

After implementing repositories, provider/loop factory, memory, services, Web
dependencies, host gating, allowed hosts, credential selection, injected opener,
and injected/default Uvicorn execution:

```text
8 passed in 0.24s
```

### Executable script RED/GREEN

The independent-process test first failed because
`scripts/mechanism_demo.py` did not exist. After adding the thin entry point,
the complete focused gate passed:

```text
.venv/bin/python -m pytest tests/test_demo.py tests/test_cli.py -q
10 passed in 0.48s
```

## Full regression

Command:

```text
.venv/bin/python -m pytest -q
```

Result:

```text
233 passed in 6.43s
```

`git diff --check` and `.venv/bin/python -m compileall -q src scripts` also
completed successfully. Ruff and mypy are not installed in this environment.

## Demo JSON evidence

Command:

```text
.venv/bin/python scripts/mechanism_demo.py --json
```

The key proof projection from the emitted JSON was:

```json
{
  "dangerous_action": {
    "effect": "require_approval",
    "rule_id": "command.recursive_delete"
  },
  "first_validation": {
    "status": "failed",
    "classification": "test_failure",
    "exit_code": 1
  },
  "feedback_seen_by_provider": true,
  "corrective_action": {
    "kind": "replace_text",
    "arguments": {
      "path": "calc.py",
      "old": "return a - b",
      "new": "return a + b",
      "count": 1
    }
  },
  "final_status": "succeeded",
  "no_progress_status": "no_progress",
  "event_summaries": {
    "governance": [
      "Policy decision: require_approval.",
      "Waiting for approval."
    ],
    "correction": [
      "Validation failed.",
      "Tool replace_text succeeded.",
      "Validation passed.",
      "Task succeeded."
    ],
    "no_progress": [
      "Validation failed.",
      "Validation failed.",
      "Stopped because no progress was detected."
    ]
  }
}
```

The complete command output also contains the policy fingerprint and full real
`ValidationReport`, including validator argv, bounded output, duration, and
failure fingerprint.

## Self-review

- The mechanism demo creates disposable real workspaces and uses real
  `ToolRuntime`, `PolicyEngine`, `ValidatorRunner`, `ProgressTracker`, and
  `MemoryStore` instances with three independent `ScriptedProvider` scenarios.
- The destructive governance action is proposed and evaluated but never
  approved or executed; its trace terminates at `waiting_approval`.
- The validator invokes the active Python executable directly and checks exact
  `calc.py` content. It does not depend on pytest in the demo workspace.
- Provider call history proves the failed validation feedback was present before
  the corrective `replace_text` response.
- Demo commands do not construct a credential backend or make network calls.
- Demo serve mode constructs credential services for Web injection but does not
  read a credential. OpenAI serve mode calls `get_for_provider` once in the CLI
  composition root and fails before starting Uvicorn when no key exists.
- The real OpenAI-compatible provider composition is exercised with an injected
  opener, so the test performs no network call. Uvicorn is likewise injected.
- Non-loopback binds return status 2 unless `--allow-remote` is present. Every
  selected bind host is explicitly included in Web `allowed_hosts`.
- Credential set uses `getpass.getpass`; status, set confirmation, and clear
  confirmation never print the secret.
- Review was performed locally because the task explicitly prohibited spawning
  agents. No Critical or Important issues remain.

## Known constraints

- Live task loops remain process-local, matching the existing TaskService/Web
  limitation documented by Task 7; persisted audit records survive restart but
  active provider state is not reconstructed.
- Demo-mode Web tasks use a finite safe scripted recall sequence and terminate by
  the existing no-progress mechanism.

## External review follow-up

Three Important findings from the external review were addressed with separate
RED/GREEN cycles.

### Configured demo no-progress threshold

RED used a real composed Web app with `max_identical_actions = 4`, created a
task through the API, and advanced the real `TaskService` four times. The fourth
step incorrectly remained `running` because the fixed three-response provider
was exhausted:

```text
FAILED test_serve_demo_reaches_custom_no_progress_threshold
assert 'running' == 'no_progress'
1 failed
```

GREEN sizes each demo-mode `ScriptedProvider` response sequence from
`config.max_identical_actions`. The task now reaches `no_progress` exactly at
step 4, and its persisted events contain no `provider_failure`:

```text
1 passed in 0.13s
```

### Wildcard binding and explicit allowed hosts

RED covered both `0.0.0.0` and `::` wildcard binds without an allowed host, plus
a wildcard bind with two repeatable `--allowed-host` values:

```text
3 failed
```

GREEN adds repeatable `--allowed-host`, requires at least one concrete explicit
host for wildcard binds, and excludes the wildcard address itself from Web's
trusted Host set. Non-wildcard remote binds retain their literal-host behavior
and merge explicit hosts:

```text
4 passed in 0.13s
```

The composed app accepts `example.test` and `admin.example.test` while rejecting
`Host: 0.0.0.0` for a wildcard listener.

### CLI error redaction

RED injected credential backend, provider opener, and Uvicorn runner failures
whose messages contained an unmistakably fake token. Credential commands raised
instead of returning a CLI status, serve composition printed a raw backend
exception, and runner errors escaped:

```text
6 failed
```

GREEN routes credential, demo, serve composition, and runner exceptions through
`forgeloop.credentials.redact`. Credential/backend/composition/runner failures
return 2, omit both registered secrets and token-shaped values, and include
`[REDACTED]` where sensitive text was removed. A provider opener failure remains
inside the existing AgentLoop safety boundary: the task deterministically stays
`running`, a sanitized `provider_failure` event is persisted, and API responses,
events, stdout, and stderr contain neither the API key nor the fake token.

```text
6 passed in 0.13s
```

### Follow-up verification

Focused gate:

```text
.venv/bin/python -m pytest tests/test_demo.py tests/test_cli.py -q
20 passed in 0.54s
```

Full regression:

```text
.venv/bin/python -m pytest -q
243 passed in 6.49s
```

The real `.venv/bin/python scripts/mechanism_demo.py --json` command still
reports `require_approval`, the initial failed validation, observed feedback,
`replace_text`, final `succeeded`, and repeated-fingerprint `no_progress`.
