# ForgeLoop New API Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** Add a first-class, credential-safe `newapi` execution mode that works reliably with the njusehub New API gateway while preserving ForgeLoop's strict local action validation and governance boundaries.

**Architecture:** Keep the public `Provider` protocol unchanged. Add a dedicated `NewAPIProvider` that sends `json_object` requests with a deterministic Action-schema system instruction, extracts only safe textual actions, and retries bounded transient failures. Keep `OpenAICompatibleProvider` on its existing strict `json_schema` path, expose structured provider failures to `AgentLoop`, and compose `newapi` explicitly through the CLI, credential service, and Web UI.

**Tech Stack:** Python 3.11+, urllib, Pydantic v2, FastAPI/Jinja2, SQLite, pytest, Hatchling.

## Global Constraints

- Follow test-driven development: add one failing behavior test, run it to confirm the expected failure, make the minimum implementation change, then rerun it.
- Never print, log, commit, or place an API key in TOML. Use fake keys in automated tests.
- Do not execute provider-native `tool_calls`; all returned text must still pass through `parse_action()`, policy evaluation, and `ToolRuntime`.
- Preserve current `demo` and `openai` request behavior and tests.
- One Agent step may make at most two New API HTTP attempts, separated by one injectable 250 ms delay.
- Commit after each task using the commit messages below. Stage only the files listed for that task; never stage `njusehub.toml`.

---

## Task 1: Add structured errors and the New API HTTP adapter

**Files:**

- Modify: `src/forgeloop/providers.py`
- Modify: `tests/test_providers.py`

- [ ] **Step 1: Add failing tests for structured provider errors and New API request construction**

Extend the provider imports with `NewAPIProvider`. Add tests that construct the provider with a fake opener and assert:

```python
provider = NewAPIProvider(
    "https://provider.example/v1/chat/completions",
    "qwen-turbo",
    "unmistakably-fake-provider-key",
    20,
    opener=opener,
    sleeper=sleeps.append,
)
result = provider.complete(
    [{"role": "feedback", "content": "validation failed"}],
    {"type": "object", "required": ["kind", "arguments"]},
)

assert result == '{"kind":"run_validation","arguments":{}}'
body = json.loads(opened[0][0].data)
assert body["response_format"] == {"type": "json_object"}
assert body["messages"][0]["role"] == "system"
assert "Return exactly one JSON object" in body["messages"][0]["content"]
assert '"required":["kind","arguments"]' in body["messages"][0]["content"]
assert body["messages"][1] == {
    "role": "user",
    "content": "[feedback] validation failed",
}
assert len(opened) == 1
assert sleeps == []
```

Also assert deterministic compact schema serialization by calling `complete()` twice with dictionaries whose keys were inserted in different orders and comparing the generated first system-message content.

- [ ] **Step 2: Run the focused request tests and verify the expected failure**

Run:

```bash
.venv/bin/pytest -q tests/test_providers.py -k 'newapi and (request or schema)'
```

Expected: collection/import failure because `NewAPIProvider` does not exist.

- [ ] **Step 3: Implement the backward-compatible structured error type and shared request helpers**

In `src/forgeloop/providers.py`, import `socket`, `time`, `HTTPError`, and `URLError`. Keep legacy callers such as `ProviderError("message")` valid:

```python
class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        http_status: int | None = None,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.attempts = attempts
        self.retryable = retryable
```

Extract module-private helpers used by both HTTP providers:

```python
def _normalize_message(message: dict[str, str]) -> dict[str, str]: ...

def _post_json(
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    payload: dict[str, object],
    opener: Callable[..., object],
) -> object: ...
```

`_post_json` must create the POST `Request`, JSON-encode with `allow_nan=False`, send the bearer header, decode UTF-8 JSON, and never include the key, request body, or raw upstream body in an exception. Preserve the existing `OpenAICompatibleProvider` payload exactly, and have it translate helper failures to the same public messages its current tests expect.

- [ ] **Step 4: Implement `NewAPIProvider` request construction**

Add `NewAPIProvider` with this injectable constructor:

```python
def __init__(
    self,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: int,
    opener: Callable[..., object] = urlopen,
    sleeper: Callable[[float], object] = time.sleep,
    max_attempts: int = 2,
    retry_delay_seconds: float = 0.25,
) -> None: ...
```

Build the generated instruction with `json.dumps(action_schema, sort_keys=True, separators=(",", ":"), allow_nan=False)` and prepend:

```text
Return exactly one JSON object and no prose or Markdown. The object must satisfy this JSON Schema: <compact schema>
```

Send `{"type": "json_object"}` as `response_format`. Implement a redacted `__repr__` matching the OpenAI adapter's secrecy property.

- [ ] **Step 5: Add failing response-extraction tests**

Cover all extraction rules:

- non-empty string `content` wins even if `reasoning_content` is present;
- empty/whitespace `content` accepts `reasoning_content` only when `parse_action()` succeeds;
- invalid reasoning is never exposed in the raised error;
- native `tool_calls` without a textual action produce `empty_content` and are never dispatched;
- missing choices/message, malformed JSON, and non-string fields produce `invalid_response`;
- non-empty invalid action content is returned unchanged, making format correction the loop's responsibility.

For a structured error, assert its fields rather than upstream text:

```python
with pytest.raises(ProviderError) as raised:
    provider.complete([], {})
assert raised.value.code == "invalid_response"
assert raised.value.attempts == 1
assert raised.value.retryable is False
assert sensitive_reasoning not in str(raised.value)
```

- [ ] **Step 6: Add failing retry-classification tests**

Use a queued fake opener and an injected `sleeps` list. Test successful retry after each of:

- `TimeoutError` and `socket.timeout`;
- `URLError`;
- `HTTPError` 429 and 503;
- a valid response envelope whose content is empty and whose reasoning is absent or invalid.

Assert exactly two opener calls, exactly `[0.25]` sleeps, and a successful action. Then test terminal failures:

- two retryable failures report `attempts == 2` and `retryable is True`;
- HTTP 400, 401, and 403 make one call and no sleep;
- malformed JSON/envelopes make one call and no sleep;
- non-empty invalid model content makes one call and no sleep.

Create `HTTPError` instances with an in-memory body, then assert neither that body nor the fake API key appears in `repr(provider)` or `str(error)`.

- [ ] **Step 7: Implement extraction and bounded retry behavior**

Use one loop whose maximum is fixed by `max_attempts`; validate `max_attempts >= 1` in the constructor. Classify failures as follows:

| Condition | Code | Retry |
|---|---|---|
| timeout | `timeout` | yes |
| URL/connection failure | `connection_error` | yes |
| HTTP 429 or 5xx | `http_error` | yes |
| valid envelope, no usable action text | `empty_content` | yes |
| other HTTP 4xx | `http_error` | no |
| decode/envelope/type failure | `invalid_response` | no |

Only call `sleeper(retry_delay_seconds)` when another attempt will occur. Preserve the last safe code/status in the terminal `ProviderError`. A non-empty `content` string must be returned without provider-side action validation. Validate fallback `reasoning_content` with `parse_action()` before returning it.

- [ ] **Step 8: Run provider tests and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_providers.py
```

Expected: all provider tests pass, including unchanged OpenAI payload assertions.

Commit:

```bash
git add src/forgeloop/providers.py tests/test_providers.py
git commit -m "feat: add bounded New API provider adapter [agent: Codex]"
```

---

## Task 2: Surface safe structured provider failures in the audit trail

**Files:**

- Modify: `src/forgeloop/loop.py`
- Modify: `tests/test_loop.py`

- [ ] **Step 1: Add failing tests for safe audit feedback**

Import `ProviderError` in `loop.py` tests and parameterize cases for timeout, HTTP 429, HTTP 401, empty content, and a legacy unstructured error. Construct errors with a secret as the original provider detail only in the legacy case. After one step, assert:

```python
assert state.step_count == 1
assert state.status.value == "running"
assert state.events[-1].summary == "Provider timeout after 2 attempts."
assert state.events[-1].data == {
    "status": "running",
    "reason": "provider_failure",
    "provider_error_code": "timeout",
    "provider_attempts": 2,
    "provider_retryable": True,
}
assert secret not in json.dumps(state.messages)
assert secret not in json.dumps([event.summary for event in state.events])
```

HTTP summaries must be exactly `Provider returned HTTP 429 after 2 attempts.` and `Provider authentication failed with HTTP 401.` Empty content must be `Provider returned empty content after 2 attempts.` Legacy/unexpected failures retain `The model provider failed to return an action.`

- [ ] **Step 2: Run the focused loop test and verify it fails**

Run:

```bash
.venv/bin/pytest -q tests/test_loop.py -k 'provider_failure'
```

Expected: existing generic feedback does not expose the new safe summary/metadata.

- [ ] **Step 3: Implement the dedicated `ProviderError` catch path**

Import `ProviderError` beside `ActionParseError`. Add a small pure formatter that depends only on `code`, `http_status`, and `attempts`; never interpolate `str(exc)`. Catch `ProviderError` before the generic `Exception` branch, append safe feedback, and emit a state event with safe fields. Keep `_finish_step(provider_failed=True)` so each model decision still consumes exactly one Agent step even when the adapter used two HTTP attempts.

The generic exception branch must stay present for defense in depth and must not reveal exception text.

- [ ] **Step 4: Run loop tests and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_loop.py
```

Expected: all loop tests pass, including `FailsOnceProvider(ProviderError(message))` compatibility.

Commit:

```bash
git add src/forgeloop/loop.py tests/test_loop.py
git commit -m "feat: audit provider failures safely [agent: Codex]"
```

---

## Task 3: Compose `newapi` through CLI, credentials, and Web UI

**Files:**

- Modify: `src/forgeloop/cli.py`
- Modify: `src/forgeloop/web.py`
- Modify: `src/forgeloop/templates/index.html`
- Modify: `src/forgeloop/templates/task.html`
- Modify: `src/forgeloop/templates/settings.html`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_web.py`

- [ ] **Step 1: Add failing CLI composition tests**

Add tests that assert:

- `serve --provider newapi` is accepted;
- only the `newapi` credential slot is read;
- a missing key prints `New API credential is not configured; use 'forgeloop credentials set newapi'.` and returns 2;
- `FORGELOOP_SECRET_FILE` binds `SecretFileBackend` to the provider passed to credential or serve composition;
- the OpenAI path still reads only `openai`.

Use a fake opener returning one action and a `CapturingRunner`. Never make a network request.

- [ ] **Step 2: Run the focused CLI tests and verify failure**

Run:

```bash
.venv/bin/pytest -q tests/test_cli.py -k 'newapi or secret_file'
```

Expected: argparse rejects `newapi`, and the secret-file backend is still hard-coded to `openai`.

- [ ] **Step 3: Implement provider-aware CLI and credential composition**

Make these exact composition changes:

```python
serve.add_argument(
    "--provider", choices=("demo", "openai", "newapi"), default="demo"
)

def _default_credential_backend(provider: str) -> CredentialBackend:
    secret_file = os.environ.get("FORGELOOP_SECRET_FILE")
    if secret_file:
        return SecretFileBackend(secret_file, provider=provider)
    return KeyringBackend()
```

Pass `args.provider` from `_run_credentials` and `_run_serve`. For both real modes, retrieve `credential_service.get_for_provider(args.provider)`. In `_loop_factory`, branch explicitly:

```python
if provider_name == "openai":
    provider_class = OpenAICompatibleProvider
elif provider_name == "newapi":
    provider_class = NewAPIProvider
else:
    provider_class = None
```

Instantiate either real provider from the same existing config fields and injected opener; keep the scripted provider only for `demo`. Use provider-specific missing-credential messages and never include the key in errors.

- [ ] **Step 4: Add failing Web UI display tests**

Extend `AppDependencies` fixtures with `provider_model`. Assert the home, task, and settings pages render `newapi · qwen-turbo`, while form/API provider identity remains the exact machine value `newapi`. Add a demo assertion that it renders only `demo` without a dangling separator.

- [ ] **Step 5: Implement a display-only provider label**

Add `provider_model: str | None = None` to `AppDependencies` and a property:

```python
@property
def provider_label(self) -> str:
    if self.provider_model:
        return f"{self.provider_name} · {self.provider_model}"
    return self.provider_name
```

Expose `provider_label` in the template context. Replace visible `{{ provider_name }}` instances with `{{ provider_label }}` in the three templates, but preserve `provider_name` in hidden inputs, route URLs, and provider comparisons. The CLI supplies `config.provider_model` for `openai` and `newapi`, and `None` for `demo`.

- [ ] **Step 6: Add one fake end-to-end New API task test**

In `tests/test_cli.py`, compose an app with `newapi`, a fake key backend, and a queued opener that returns these textual actions:

1. `read_file` for a file inside the temporary workspace;
2. `replace_text` for a deterministic edit;
3. `run_validation`;
4. `finish`.

Create and advance the task through the API. Assert the file changed, the task succeeded, all HTTP bodies use `json_object`, every Authorization header uses the fake key, and the task audit contains action/governance/tool-result events. No live network is permitted.

- [ ] **Step 7: Run CLI and Web tests and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_cli.py tests/test_web.py
```

Expected: all CLI/Web tests pass; existing `demo` and `openai` behavior remains green.

Commit:

```bash
git add src/forgeloop/cli.py src/forgeloop/web.py \
  src/forgeloop/templates/index.html src/forgeloop/templates/task.html \
  src/forgeloop/templates/settings.html tests/test_cli.py tests/test_web.py
git commit -m "feat: compose New API execution mode [agent: Codex]"
```

---

## Task 4: Add safe njusehub configuration and operator documentation

**Files:**

- Modify: `.gitignore`
- Create: `njusehub.example.toml`
- Modify: `README.md`
- Modify: `SPEC.md`
- Modify: `AGENT_LOG.md`
- Modify: `tests/test_distribution.py`

- [ ] **Step 1: Add failing distribution tests**

Assert that:

```python
example = tomllib.loads((ROOT / "njusehub.example.toml").read_text())
assert example["provider_base_url"] == (
    "https://njusehub.info/v1/chat/completions"
)
assert example["provider_model"] == "qwen-turbo"
assert example["provider_timeout_seconds"] == 20
assert "api_key" not in json.dumps(example).lower()
assert "njusehub.toml" in (ROOT / ".gitignore").read_text().splitlines()
```

Also retain the existing packaged-template and source-distribution assertions.

- [ ] **Step 2: Run the focused distribution test and verify failure**

Run:

```bash
.venv/bin/pytest -q tests/test_distribution.py -k njusehub
```

Expected: `njusehub.example.toml` is absent and `njusehub.toml` is not ignored.

- [ ] **Step 3: Add the credential-free example and ignore local configuration**

Copy the complete existing safety and validator sections from `forgeloop.example.toml`. Change only:

```toml
provider_base_url = "https://njusehub.info/v1/chat/completions"
provider_model = "qwen-turbo"
provider_timeout_seconds = 20
```

Add exactly `njusehub.toml` to `.gitignore`. Do not modify or stage the user's existing local `njusehub.toml`.

- [ ] **Step 4: Document the exact New API workflow and limitations**

In `README.md`, add:

```bash
source .venv/bin/activate
forgeloop credentials set newapi
forgeloop credentials status newapi
forgeloop serve --provider newapi \
  --config njusehub.example.toml \
  --data-dir .forgeloop
```

Document that `json_object` is paired with strict local Action validation; each Agent step may issue two billable HTTP requests for transient failures; 400/401/403 and invalid non-empty actions are not retried; config/key changes require restarting the server and creating a new task. Include port-conflict recovery (`--port 8001`) and the reason `source .venv/bin/activate` fixes `command not found: forgeloop`.

Update `SPEC.md` to list `demo | openai | newapi`, retry/error semantics, separate credential names, and the provider/model UI label. Append `AGENT_LOG.md` entries containing the focused and full validation commands, without secrets or raw upstream bodies.

- [ ] **Step 5: Run distribution tests and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_distribution.py
```

Expected: all distribution tests pass.

Commit:

```bash
git add .gitignore njusehub.example.toml README.md SPEC.md AGENT_LOG.md \
  tests/test_distribution.py
git commit -m "docs: add safe njusehub New API workflow [agent: Codex]"
```

---

## Task 5: Run full verification and one controlled live smoke test

**Files:**

- Modify only if verification exposes a defect: the owning source/test file
- Modify: `AGENT_LOG.md` only to record final non-secret evidence

- [ ] **Step 1: Run formatting/static sanity checks and the full suite**

Run:

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -q
.venv/bin/forgeloop demo --json
```

Expected: compilation succeeds; the full test suite is green; demo JSON reports `final_status: succeeded` and `no_progress_status: no_progress`.

- [ ] **Step 2: Build and inspect the distributable artifacts**

Run:

```bash
rm -rf dist
.venv/bin/python -m build
tar -tf dist/*.tar.gz | sort
unzip -l dist/*.whl
```

Expected: source and wheel builds succeed and contain ForgeLoop application/templates/static assets; neither artifact contains `njusehub.toml`, `.forgeloop`, a credential, nor local database files.

- [ ] **Step 3: Perform a secret and repository hygiene audit**

Run:

```bash
git status --short
git diff --check
git ls-files | rg '(^|/)(njusehub\.toml|\.forgeloop|.*\.sqlite3)$' && exit 1 || true
git grep -nE '(Authorization: Bearer|api[_-]?key\s*=\s*["'"'][^"'"']+)' -- . \
  ':(exclude)docs/superpowers/plans/2026-07-20-newapi-provider.md'
```

Expected: `git diff --check` is clean; no sensitive local files are tracked; grep finds no real credential. The plan is excluded because it contains a documentation-only search expression, not a key.

- [ ] **Step 4: Run a controlled njusehub smoke task**

Use the credential already stored under `newapi`; never print it. Start a temporary server on an unused loopback port with `--provider newapi`, the user's local `njusehub.toml`, and a fresh temporary data directory. Create a disposable workspace containing one small text file. Submit a task that reads the file, makes one harmless deterministic replacement, runs validation, and finishes.

Verify from the task API/audit, without copying model reasoning or credentials into logs:

- provider label is `newapi · qwen-turbo`;
- an action arrives well below the former 60-second strict-schema stall in the normal case;
- read/edit/validation actions pass through policy and tool-result events;
- the task can finish only after validation succeeds;
- any transient provider failure reports only safe code/status/attempt counts.

Stop the temporary server and delete only the disposable workspace/data directory created for this smoke test.

- [ ] **Step 5: Record final evidence and make a verification commit only if needed**

Append the exact test count, build result, and redacted live-smoke outcome to `AGENT_LOG.md`. If this changes the file, commit it:

```bash
git add AGENT_LOG.md
git commit -m "test: record New API delivery verification [agent: Codex]"
```

- [ ] **Step 6: Request code review and finish the branch**

Invoke the `requesting-code-review` skill, address all correctness/security findings, rerun the affected focused tests and the full suite, then invoke `finishing-a-development-branch`. Present the verified branch/merge options to the user; do not push or create a PR unless explicitly requested.
