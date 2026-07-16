# ForgeLoop Coding Agent Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a distributable, web-accessible Coding Agent Harness whose self-written loop safely executes structured coding actions and uses deterministic validation feedback to drive correction.

**Architecture:** A dependency-inverted Python core owns the loop and protocol types. Infrastructure adapters implement filesystem/subprocess tools, SQLite persistence, keyring credentials, and an OpenAI-compatible one-shot provider; a thin FastAPI/Jinja layer exposes tasks, traces, approvals, settings, and a deterministic demo. No high-level agent framework is permitted.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Pydantic 2, Jinja2, SQLite, keyring, pytest, Docker, GitLab CI.

## Global Constraints

- `SPEC.md` is approved; implementation agents start at their assigned task and must not restart brainstorming.
- Native commands use `python3`; do not assume the host defines a `python` alias.
- Implement the complete agent loop in repository code; never use LangChain AgentExecutor, AutoGen, CrewAI, LlamaIndex Agent, or a coding-agent runner.
- All core mechanisms must run offline under `ScriptedProvider`; `make test` must not require network access or an API key.
- Execute subprocesses with `shell=False`, a fixed workspace, a minimal environment, bounded output, and bounded time.
- Resolve every file path and symlink beneath the configured workspace; workspace escape is never approvable.
- A task may become `succeeded` only after its most recent validation report passed.
- API keys must never enter Git, SQLite, task events, logs, HTML, or exception text.
- The Web server binds to `127.0.0.1` by default.
- TDD order is mandatory: failing test, observed failure, minimal implementation, passing test, refactor.
- Each task receives spec-compliance review followed by code-quality review before its commit is accepted.

## File Structure

```text
src/forgeloop/
  __init__.py           package version
  models.py             immutable actions, results, reports, tasks and events
  config.py             TOML configuration schema and loader
  policy.py             path boundary, command rules and governance decisions
  tools.py              workspace file tools and bounded subprocess runner
  feedback.py           validator execution, classification, fingerprints, progress
  memory.py             SQLite project memory with tagged budgeted retrieval
  credentials.py        keyring/secret-file credential service and redaction
  providers.py          provider protocol, scripted mock, one-shot HTTP adapter
  loop.py               self-written agent loop and deterministic stop logic
  repository.py         SQLite task, event and approval persistence
  service.py            task orchestration and resumable HITL coordination
  demo.py               deterministic mechanism demonstration
  cli.py                serve, demo and credential commands
  web.py                FastAPI composition and routes
  templates/            server-rendered pages
  static/               local CSS and JavaScript
tests/                  one focused test module per production module
scripts/mechanism_demo.py
```

## Dependencies and Parallel Work

Task 1 is foundational. Tasks 2–5 consume Task 1 and can be developed independently. Task 6 consumes Tasks 2–5. Task 7 consumes Tasks 1 and 6. Task 8 consumes Tasks 6–7. Task 9 consumes Tasks 2, 4, 6 and 8. Task 10 consumes all previous tasks. Cold-start validation happens after this plan and before Task 1.

---

### Task 1: Package Foundation, Domain Models, and Configuration

**Status:** Complete — commit `5c07791`; spec compliance ✅; task quality approved.

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `src/forgeloop/__init__.py`
- Create: `src/forgeloop/models.py`
- Create: `src/forgeloop/config.py`
- Create: `tests/test_models.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `Action(kind: ActionKind, arguments: dict[str, object])`, `ToolResult`, `ValidationReport`, `GovernanceDecision`, `TaskRecord`, `Event`, `HarnessConfig`, `load_config(path: Path) -> HarnessConfig`.
- Consumes: Python standard library and Pydantic only.

- [x] **Step 1: Write failing model tests**

```python
def test_action_rejects_unknown_kind():
    with pytest.raises(ValueError):
        Action.model_validate({"kind": "launch_missile", "arguments": {}})

def test_validation_report_is_serializable():
    report = ValidationReport.passed(["pytest"], 12, "1 passed")
    assert report.status is ValidationStatus.PASSED
    assert report.model_dump(mode="json")["exit_code"] == 0
```

- [x] **Step 2: Run model tests and observe red**

Run: `python3 -m pytest tests/test_models.py -q`
Expected: collection fails because `forgeloop.models` does not exist.

- [x] **Step 3: Implement strict domain models**

Define string enums for all action, task, validation, decision and event states. Configure models with `extra="forbid"`; add constructors `ToolResult.success/error` and `ValidationReport.passed/failed`; keep action arguments JSON-compatible.

- [x] **Step 4: Write failing configuration tests**

```python
def test_load_config_rejects_unknown_field(tmp_path):
    path = tmp_path / "forgeloop.toml"
    path.write_text('mystery = true\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mystery"):
        load_config(path)

def test_default_config_has_bounded_budgets():
    cfg = HarnessConfig()
    assert cfg.max_steps == 20
    assert cfg.command_timeout_seconds == 60
    assert cfg.max_output_bytes == 32768
```

- [x] **Step 5: Implement TOML schema and loader**

Use `tomllib`; define `ValidatorConfig(argv: list[str], timeout_seconds: int = 60)`. Define every `HarnessConfig` field with the exact type and default from SPEC §5.7: `max_steps=20`, `max_validation_runs=8`, `wall_time_seconds=900`, `command_timeout_seconds=60`, `provider_timeout_seconds=60`, `max_output_bytes=32768`, `max_file_bytes=1048576`, `max_identical_failures=2`, `max_identical_actions=3`, `memory_recall_limit=10`, `memory_char_budget=4096`, the five allowed executables, the seven approval rule IDs, empty validators, the HTTPS provider URL, and model `gpt-4.1-mini`. Reject unknown fields and invalid limits with a `ConfigError` that includes the field path.

- [x] **Step 6: Add packaging and one-command tests**

Declare runtime dependencies and `forgeloop = "forgeloop.cli:main"`; configure pytest with `pythonpath = ["src"]`; make `make test` run `python3 -m pytest -q` and `make dev` run `python3 -m forgeloop.cli serve`.

- [x] **Step 7: Verify and commit**

Run: `make test`
Expected: model and config tests pass.
Commit: `feat: define strict harness domain and configuration [agent: delegated-worker]`.

---

### Task 2: Deterministic Governance and Workspace Boundary

**Files:**
- Create: `src/forgeloop/policy.py`
- Create: `tests/test_policy.py`

**Interfaces:**
- Consumes: `Action`, `GovernanceDecision`, `HarnessConfig` from Task 1.
- Produces: `PolicyEngine.evaluate(action: Action, workspace: Path) -> GovernanceDecision`, `resolve_workspace_path(workspace: Path, requested: str) -> Path`, `action_fingerprint(action: Action) -> str`.

- [ ] **Step 1: Write failing path-boundary tests**

```python
def test_parent_escape_is_denied(tmp_path):
    decision = PolicyEngine().evaluate(
        Action(kind="read_file", arguments={"path": "../secret"}), tmp_path
    )
    assert decision.effect == "deny"
    assert decision.rule_id == "workspace.escape"

def test_symlink_escape_is_denied(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    decision = PolicyEngine().evaluate(
        Action(kind="read_file", arguments={"path": "link"}), tmp_path
    )
    assert decision.effect == "deny"
```

- [ ] **Step 2: Observe red, then implement canonical boundary checks**

Run the two tests and confirm import failure. Resolve workspace and candidate with `Path.resolve(strict=False)`; use `candidate.is_relative_to(root)`; return fail-closed decisions rather than raising.

- [ ] **Step 3: Write dangerous-command and metacharacter tests**

```python
@pytest.mark.parametrize("argv", [["rm", "-rf", "build"], ["git", "reset", "--hard"], ["git", "push", "--force"]])
def test_dangerous_commands_require_approval(tmp_path, argv):
    decision = PolicyEngine().evaluate(Action(kind="run_command", arguments={"argv": argv}), tmp_path)
    assert decision.effect == "require_approval"

def test_shell_metacharacters_are_denied(tmp_path):
    decision = PolicyEngine().evaluate(
        Action(kind="run_command", arguments={"argv": ["pytest", ";", "env"]}), tmp_path
    )
    assert decision.effect == "deny"
```

- [ ] **Step 4: Implement ordered policy rules and stable fingerprint**

Evaluate structural denials first, executable allowlist second, destructive signatures third, safe default last. Hash canonical JSON with sorted keys using SHA-256. Include `rule_id`, human reason and fingerprint in every decision.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_policy.py -q`
Expected: all policy cases pass.
Commit: `feat: enforce deterministic workspace and command policy [agent: delegated-worker]`.

---

### Task 3: Bounded Tool Runtime

**Files:**
- Create: `src/forgeloop/tools.py`
- Create: `tests/test_tools.py`

**Interfaces:**
- Consumes: approved `Action`, `ToolResult`, `HarnessConfig`, `resolve_workspace_path`.
- Produces: `ToolRuntime.execute(action: Action) -> ToolResult`; private handlers for read, write, replace and command.

- [ ] **Step 1: Write failing file-tool tests**

```python
def test_replace_requires_exact_occurrence(runtime, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n")
    result = runtime.execute(Action(kind="replace_text", arguments={"path": "a.py", "old": "x = 1", "new": "x = 2", "count": 1}))
    assert not result.ok
    assert result.error_code == "ambiguous_replacement"

def test_write_then_read_round_trip(runtime):
    assert runtime.execute(Action(kind="write_file", arguments={"path": "pkg/a.py", "content": "ok"})).ok
    result = runtime.execute(Action(kind="read_file", arguments={"path": "pkg/a.py"}))
    assert result.output == "ok"
```

- [ ] **Step 2: Observe red, then implement file handlers**

Use UTF-8 strict decoding, configured file-size limit, atomic same-directory temporary write plus `os.replace`, and exact occurrence validation before replacement.

- [ ] **Step 3: Write failing subprocess tests**

```python
def test_command_uses_workspace_and_minimal_environment(runtime, tmp_path):
    action = Action(kind="run_command", arguments={"argv": [sys.executable, "-c", "import os; print(os.getcwd()); print(os.getenv('OPENAI_API_KEY'))"]})
    result = runtime.execute(action)
    assert str(tmp_path) in result.output
    assert "secret-value" not in result.output

def test_command_timeout_returns_structured_error(short_timeout_runtime):
    result = short_timeout_runtime.execute(Action(kind="run_command", arguments={"argv": [sys.executable, "-c", "import time; time.sleep(2)"]}))
    assert result.error_code == "timeout"
```

- [ ] **Step 4: Implement subprocess runner**

Call `subprocess.run(argv, cwd=workspace, shell=False, capture_output=True, text=False, timeout=...)`; construct an environment from `PATH`, locale and test-specific safe variables only; decode with replacement; truncate deterministically and expose `exit_code` and duration metadata.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_tools.py -q`
Expected: all tool tests pass without network.
Commit: `feat: add bounded workspace tool runtime [agent: delegated-worker]`.

---

### Task 4: Validation Feedback and No-Progress Detection

**Files:**
- Create: `src/forgeloop/feedback.py`
- Create: `tests/test_feedback.py`

**Interfaces:**
- Consumes: `ValidatorConfig`, `ValidationReport`, bounded subprocess semantics.
- Produces: `classify_failure(argv, exit_code, stdout, stderr) -> FailureClass`, `report_fingerprint(report) -> str`, `ValidatorRunner.run_all() -> list[ValidationReport]`, `ProgressTracker.observe_action/observe_validation -> ProgressState`.

- [ ] **Step 1: Write failing classification tests**

```python
@pytest.mark.parametrize(("stderr", "expected"), [
    ("SyntaxError: invalid syntax", "syntax"),
    ("FAILED tests/test_x.py::test_x", "test_failure"),
    ("error: Incompatible types in assignment", "type_error"),
])
def test_failure_classification(stderr, expected):
    assert classify_failure(["pytest"], 1, "", stderr) == expected
```

- [ ] **Step 2: Observe red, implement reports and normalization**

Match ordered, documented regexes. Normalize temporary absolute paths, timestamps and whitespace before hashing the last bounded output segment so equivalent failures have equal fingerprints.

- [ ] **Step 3: Write failing progress tests**

```python
def test_repeated_failed_fingerprint_stops_progress():
    tracker = ProgressTracker(max_identical_failures=2, max_identical_actions=3)
    report = failed_report(fingerprint="same")
    assert tracker.observe_validation(report).should_stop is False
    state = tracker.observe_validation(report)
    assert state.should_stop is True
    assert state.reason == "no_progress"
```

- [ ] **Step 4: Implement runner, aggregate pass state, and tracker**

Run validators in configuration order. Overall validation passes only if every report passes. Infrastructure and timeout classifications remain distinct. Track consecutive identical validation fingerprints and actions, resetting counters on a different fingerprint.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_feedback.py -q`
Expected: classification, fingerprint and stop tests pass.
Commit: `feat: add objective feedback and no-progress detection [agent: delegated-worker]`.

---

### Task 5: Persistent Memory, Credentials, and Redaction

**Files:**
- Create: `src/forgeloop/memory.py`
- Create: `src/forgeloop/credentials.py`
- Create: `tests/test_memory.py`
- Create: `tests/test_credentials.py`

**Interfaces:**
- Consumes: SQLite path and credential backend protocol.
- Produces: `MemoryStore.upsert/recall`, `CredentialService.status/set/clear/get_for_provider`, `redact(text, secrets) -> str`.

- [ ] **Step 1: Write failing memory isolation and budget tests**

```python
def test_recall_is_project_and_tag_scoped(store):
    store.upsert("one", "style", "use ruff", ["python", "style"])
    store.upsert("two", "style", "use eslint", ["js", "style"])
    assert [m.value for m in store.recall("one", ["python"], limit=5, char_budget=100)] == ["use ruff"]

def test_recall_respects_character_budget(store):
    store.upsert("p", "a", "12345", ["x"])
    store.upsert("p", "b", "67890", ["x"])
    assert sum(len(m.value) for m in store.recall("p", ["x"], 10, 5)) <= 5
```

- [ ] **Step 2: Implement SQLite memory and verify persistence**

Create schema on initialization; use `(project_id, key)` primary key; encode sorted unique tags as JSON; order matches by tag overlap then `updated_at`; reject keys matching `secret|token|password|api[_-]?key`.

- [ ] **Step 3: Write failing credential non-disclosure tests**

```python
def test_status_and_repr_never_reveal_secret(fake_backend):
    service = CredentialService(fake_backend)
    service.set("openai", "sk-example-secret")
    assert service.status("openai").configured
    assert "sk-example-secret" not in repr(service.status("openai"))

def test_redact_masks_registered_and_token_shaped_values():
    text = redact("Authorization: Bearer sk-example-secret", ["sk-example-secret"])
    assert "secret" not in text
    assert "[REDACTED]" in text
```

- [ ] **Step 4: Implement backend protocol and secure sources**

Implement `KeyringBackend` using the `keyring` library, `SecretFileBackend` requiring a regular file with no group/other permission bits, and in-memory fake for tests. Never define a getter that returns a key to Web routes; only the provider composition root may call `get_for_provider`.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_memory.py tests/test_credentials.py -q`
Expected: persistence, isolation, budget and redaction tests pass.
Commit: `feat: persist scoped memory and secure credentials [agent: delegated-worker]`.

---

### Task 6: Provider Abstraction and Self-Written Agent Loop

**Files:**
- Create: `src/forgeloop/providers.py`
- Create: `src/forgeloop/loop.py`
- Create: `tests/test_providers.py`
- Create: `tests/test_loop.py`

**Interfaces:**
- Consumes: all domain interfaces, `PolicyEngine`, `ToolRuntime`, `ValidatorRunner`, `ProgressTracker`, `MemoryStore`.
- Produces: `Provider.complete(messages, action_schema) -> str`, `ScriptedProvider`, `OpenAICompatibleProvider`, `AgentLoop.start`, `AgentLoop.step`, `AgentLoop.resolve_approval`.

- [ ] **Step 1: Write failing scripted-provider tests**

```python
def test_scripted_provider_records_feedback_messages():
    provider = ScriptedProvider(['{"kind":"run_validation","arguments":{}}'])
    provider.complete([{"role": "feedback", "content": "tests failed"}], {})
    assert provider.calls[0][0][-1]["role"] == "feedback"

def test_scripted_provider_fails_when_exhausted():
    with pytest.raises(ProviderExhausted):
        ScriptedProvider([]).complete([], {})
```

- [ ] **Step 2: Implement provider protocol and strict action parser**

Strip an optional fenced JSON wrapper only; parse exactly one JSON object; validate via `Action`; return a structured parse observation on error. Implement HTTP provider with `urllib.request`, explicit timeout, bearer header, one request/response, and redacted exceptions.

- [ ] **Step 3: Write failing loop feedback-correction test**

```python
def test_failed_validation_is_fed_back_and_changes_next_action(harness):
    provider = ScriptedProvider([
        action("run_validation"),
        action("replace_text", path="calc.py", old="return 0", new="return 1", count=1),
        action("run_validation"),
        action("finish", summary="fixed"),
    ])
    result = harness(provider=provider, validation_results=[failed("assert 0 == 1"), passed()]).run("fix calc")
    assert result.status == "succeeded"
    assert any(message["role"] == "feedback" for message in provider.calls[1][0])
    assert provider.responses[0] != provider.responses[1]
```

- [ ] **Step 4: Write failing governance and finish-gate tests**

```python
def test_dangerous_action_pauses_before_tool_execution(harness):
    result = harness(ScriptedProvider([action("run_command", argv=["rm", "-rf", "build"])] )).run("clean")
    assert result.status == "waiting_approval"
    assert result.tool_calls == []

def test_finish_without_passing_validation_is_rejected(harness):
    result = harness(ScriptedProvider([action("finish", summary="done")]), max_steps=1).run("fix")
    assert result.status != "succeeded"
```

- [ ] **Step 5: Implement one-step loop and stop order**

For each step: build bounded context, call provider, parse action, emit action event, evaluate policy, pause/deny/execute, convert validation to feedback, update progress, then evaluate stop conditions in order: cancelled, waiting approval, no progress, wall clock, steps, provider failure. `finish` succeeds only with latest passing validation.

- [ ] **Step 6: Add memory actions and approval-resume tests**

Verify `remember`/`recall` never bypass policy or context budget. Approval accepts only the exact pending fingerprint, marks the token consumed, executes once, and resumes the next loop step.

- [ ] **Step 7: Verify and commit**

Run: `python3 -m pytest tests/test_providers.py tests/test_loop.py -q`
Expected: all deterministic loop states pass without network.
Commit: `feat: implement provider abstraction and agent loop [agent: delegated-worker]`.

---

### Task 7: SQLite Task Repository and Resumable Service

**Files:**
- Create: `src/forgeloop/repository.py`
- Create: `src/forgeloop/service.py`
- Create: `tests/test_repository.py`
- Create: `tests/test_service.py`

**Interfaces:**
- Consumes: `TaskRecord`, `Event`, `AgentLoop` state snapshots.
- Produces: `TaskRepository.create/get/save/append_event/list_events`, `ApprovalRepository`, `TaskService.create/advance/approve/reject/cancel`.

- [ ] **Step 1: Write failing repository consistency tests**

```python
def test_event_sequence_is_monotonic_across_reopen(db_path):
    first = TaskRepository(db_path)
    task = first.create("fix", "/tmp/work")
    first.append_event(task.id, "state", "created", {})
    second = TaskRepository(db_path)
    event = second.append_event(task.id, "state", "running", {})
    assert event.sequence == 2
```

- [ ] **Step 2: Implement migrations and transactional repository methods**

Create `schema_version`, `tasks`, `events`, and `approvals`; enable foreign keys and WAL; serialize JSON with sorted keys; allocate sequence and insert event in one immediate transaction.

- [ ] **Step 3: Write failing service state-transition tests**

```python
def test_reject_pending_action_returns_task_to_running(service, pending_task):
    task = service.reject(pending_task.id, reason="not allowed")
    assert task.status == "running"
    assert task.pending_action is None

def test_approval_for_wrong_fingerprint_fails_closed(service, pending_task):
    with pytest.raises(ApprovalMismatch):
        service.approve(pending_task.id, "wrong")
```

- [ ] **Step 4: Implement service and persisted snapshots**

Persist every task state change and event before returning. Reconstruct the loop from task snapshot and event context. Keep service synchronous; Web routes may call it in a worker thread for blocking execution.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_repository.py tests/test_service.py -q`
Expected: reopen, transitions, approval replay and cancellation tests pass.
Commit: `feat: persist tasks events and approvals [agent: delegated-worker]`.

---

### Task 8: Local WebUI and HTTP API

**Files:**
- Create: `src/forgeloop/web.py`
- Create: `src/forgeloop/templates/base.html`
- Create: `src/forgeloop/templates/index.html`
- Create: `src/forgeloop/templates/task.html`
- Create: `src/forgeloop/templates/settings.html`
- Create: `src/forgeloop/templates/demo.html`
- Create: `src/forgeloop/static/style.css`
- Create: `src/forgeloop/static/app.js`
- Create: `tests/test_web.py`

**Interfaces:**
- Consumes: `TaskService`, `CredentialService`, demo factory.
- Produces: `create_app(dependencies: AppDependencies | None = None) -> FastAPI` and documented HTML/JSON routes.

- [ ] **Step 1: Write failing smoke and disclosure tests**

```python
def test_home_explains_product_and_security_boundary(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "ForgeLoop" in response.text
    assert "工作区" in response.text

def test_settings_never_returns_credential(client, credential_service):
    credential_service.set("openai", "sk-never-render-this")
    response = client.get("/settings")
    assert "sk-never-render-this" not in response.text
    assert "已配置" in response.text
```

- [ ] **Step 2: Implement app composition, templates, and local assets**

Use Jinja templates with a restrained dark/light interface, semantic HTML, visible focus states and responsive layout. Serve no CDN assets. Expose health endpoint `/healthz` returning version and database readiness.

- [ ] **Step 3: Write failing task and approval route tests**

```python
def test_mock_task_can_be_created_and_viewed(client):
    created = client.post("/api/tasks", json={"description": "fix calc", "workspace": "/workspace", "provider": "demo"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert client.get(f"/api/tasks/{task_id}").status_code == 200

def test_approval_requires_matching_fingerprint(client, pending_task):
    response = client.post(f"/api/tasks/{pending_task.id}/approve", json={"fingerprint": "wrong"})
    assert response.status_code == 409
```

- [ ] **Step 4: Implement API routes and CSRF on browser forms**

Create typed request/response schemas. JSON API uses same-origin checks; HTML forms use a signed session CSRF token. Return 404 for unknown tasks, 409 for invalid state transitions, 422 for bad input, and never expose traceback or secrets.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_web.py -q`
Expected: page, API, approval, CSRF, health and disclosure tests pass.
Commit: `feat: add observable local WebUI and API [agent: delegated-worker]`.

---

### Task 9: Deterministic Mechanism Demo and CLI

**Files:**
- Create: `src/forgeloop/demo.py`
- Create: `src/forgeloop/cli.py`
- Create: `scripts/mechanism_demo.py`
- Create: `tests/test_demo.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: loop, policy, feedback, service and Web app.
- Produces: `run_mechanism_demo(base_dir: Path) -> DemoResult`, CLI commands `serve`, `demo`, `credentials status|set|clear`.

- [ ] **Step 1: Write failing required-demonstration test**

```python
def test_demo_proves_all_required_mechanisms(tmp_path):
    result = run_mechanism_demo(tmp_path)
    assert result.dangerous_action.effect == "require_approval"
    assert result.first_validation.status == "failed"
    assert result.feedback_seen_by_provider is True
    assert result.corrective_action.kind == "replace_text"
    assert result.final_status == "succeeded"
    assert result.no_progress_status == "no_progress"
```

- [ ] **Step 2: Implement isolated demo fixture and scripted responses**

Create a temporary `calc.py` and deterministic validator that checks exact content. Run one scenario for governance, one fail→feedback→edit→pass→finish scenario, and one repeated-fingerprint scenario. Return structured evidence; do not depend on pytest being installed inside the demo workspace.

- [ ] **Step 3: Write failing CLI tests**

```python
def test_demo_cli_prints_machine_readable_summary(capsys):
    assert main(["demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_status"] == "succeeded"

def test_credentials_set_uses_hidden_input(monkeypatch, fake_backend):
    monkeypatch.setattr(getpass, "getpass", lambda _: "sk-input")
    assert main(["credentials", "set", "openai"], backend=fake_backend) == 0
```

- [ ] **Step 4: Implement argparse CLI and executable script**

`serve` starts Uvicorn with host default `127.0.0.1`; reject non-loopback bind unless `--allow-remote` is explicit. `credentials set` uses `getpass`; status emits no secret. The script imports and calls the same demo, avoiding duplicate logic.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/test_demo.py tests/test_cli.py -q` and `python3 scripts/mechanism_demo.py --json`
Expected: tests pass and JSON proves all three course behaviors.
Commit: `feat: add deterministic mechanism demo and CLI [agent: delegated-worker]`.

---

### Task 10: Distribution, CI, Cold-Start Documentation, and Final QA

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yaml`
- Create: `.gitlab-ci.yml`
- Create: `README.md`
- Create: `forgeloop.example.toml`
- Create: `REFLECTION.md`
- Modify: `AGENT_LOG.md`
- Modify: `PLAN.md`
- Modify: `SPEC_PROCESS.md`

**Interfaces:**
- Consumes: complete application and test commands.
- Produces: repeatable local/container distribution and all course deliverables.

- [ ] **Step 1: Write packaging smoke test before container files**

Add `tests/test_distribution.py` asserting the example TOML loads, package metadata exposes the CLI, `.gitlab-ci.yml` has a top-level `unit-test` job, Dockerfile runs as non-root, and README contains exact required headings: 项目简介、安装与运行、分发、目录结构、凭据安全、安全边界、已知限制。

- [ ] **Step 2: Observe red and add distribution files**

Use `python:3.12-slim`; create an unprivileged `forgeloop` user; install the wheel; set read-only source layers; mount `/workspace`, `/data`, and `/run/secrets`; expose 8000; healthcheck `/healthz`. GitLab stages run `python3 -m pytest -q` in job exactly named `unit-test`, then build the image in `container-build`.

- [ ] **Step 3: Write complete README and example configuration**

Document native and Docker cold starts, Keychain lifecycle, secret-file mount, `.env` risk, Mock demo, real provider opt-in, WebUI, architecture, security guarantees/non-guarantees, supported platforms and troubleshooting. Include copy-paste commands whose paths match the repository.

- [ ] **Step 4: Complete process evidence without fabricating human reflection**

Update AGENT_LOG with each red/green result, subagent identifier, review outcome, commit hash and manual changes. Add cold-start findings and before/after SPEC/PLAN diffs to SPEC_PROCESS. Create `REFLECTION.md` as a clearly labeled student-authored worksheet with section prompts and evidence links; do not generate the required personal 1500–2500-word reflection on the student's behalf.

- [ ] **Step 5: Run the full local quality gate**

Run: `make test`
Expected: all tests pass offline.
Run: `python3 scripts/mechanism_demo.py --json`
Expected: required evidence fields show approval, failed validation, feedback, changed action, success and no progress.
Run: `python3 -m build`
Expected: wheel and source distribution created.

- [ ] **Step 6: Build and smoke-test the container**

Run: `docker build -t forgeloop:local .`
Expected: image builds successfully.
Run the image with a temporary data mount and query `/healthz`; expect HTTP 200 without an API key.

- [ ] **Step 7: Secret and placeholder audit**

Run repository searches for token-shaped strings, `.env`, absolute developer paths, `TODO`, `TBD`, skipped tests and debug prints. Any fixture token must use an unmistakable fake form and be filtered from outputs. Run `git status --short` and ensure every deliverable is tracked.

- [ ] **Step 8: Final reviews and commit**

Perform spec-compliance review, then code-quality/security review. Resolve all Critical and Important findings.
Commit: `docs: complete ForgeLoop delivery and verification [agent: delegated-worker]`.
Update each completed PLAN task with its commit hash.

## Plan Self-Review

- Spec coverage: Tasks 1–10 cover all six Harness dimensions, credentials, WebUI, distribution, CI, documentation and deterministic demo.
- Placeholder scan: no implementation step delegates unspecified work; `delegated-worker` appears only in prescribed commit attribution and must be replaced with the actual worker identifier at execution.
- Type consistency: shared types originate in Task 1; Tasks 2–5 provide protocols consumed by Task 6; persisted service and Web layers consume the loop without owning it.
- Scope: a single vertical product with one principal contribution; optional multi-agent, vector retrieval and IDE features remain excluded.
