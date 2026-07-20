# ForgeLoop New API Provider Design

Date: 2026-07-20

## Context

ForgeLoop currently exposes `demo` and `openai` provider modes. The real HTTP
adapter sends an OpenAI Chat Completions request with a complex strict
`json_schema` response format. That request works with a fully compatible
endpoint, but the user's `njusehub.info` New API deployment was observed to
stall until the configured 60-second provider timeout.

The failure was isolated with the same credential, endpoint, model, prompt,
and action schema:

- ordinary Chat Completions and `response_format={"type":"json_object"}`
  completed in roughly one to two seconds;
- the current complex strict `json_schema` request timed out across several
  available models;
- `json_object` plus a compact system instruction containing the exact Action
  schema returned locally valid actions in three consecutive probes.

The existing `openai` name is also misleading when the selected endpoint is a
third-party OpenAI-compatible gateway. ForgeLoop therefore needs a separate,
explicit New API integration rather than hostname-specific behavior hidden in
the OpenAI adapter.

## Goals

1. Add a first-class `newapi` provider mode for njusehub/New API deployments.
2. Avoid the gateway's problematic complex `json_schema` request path.
3. Preserve ForgeLoop's strict local Action validation and all policy/tool
   boundaries.
4. Tolerate bounded transient gateway failures without consuming additional
   Agent steps.
5. Expose useful, non-secret failure reasons in the audit trail.
6. Keep `demo` and `openai` behavior backward compatible.
7. Provide a credential-safe njusehub example and end-to-end instructions.

## Non-goals

- Direct DeepSeek official API support.
- Direct Alibaba Cloud Model Studio/DashScope support.
- Automatic switching between models, endpoints, or billing groups.
- Streaming responses.
- Executing provider-native `tool_calls`.
- Persisting or resuming live provider context across service restarts.
- Adding real network calls or real credentials to the automated test suite.

Official-provider integrations can be added later behind the same Provider
protocol after the New API path is verified.

## Architecture

The public Provider protocol remains unchanged:

```python
complete(messages: list[dict[str, str]], action_schema: dict[str, object]) -> str
```

The provider family becomes:

```text
AgentLoop
  -> Provider protocol
       -> ScriptedProvider
       -> OpenAICompatibleProvider
            -> strict json_schema
       -> NewAPIProvider
            -> json_object + schema system instruction
```

`NewAPIProvider` is a separate class. It does not inherit behavioral decisions
from `OpenAICompatibleProvider`. The two HTTP providers share a small internal
JSON POST transport responsible for request construction, HTTPS I/O, response
decoding, and safe transport exceptions. Provider-specific payloads, retry
rules, and response extraction remain in their respective classes.

The `AgentLoop`, policy engine, tool runtime, feedback engine, memory store, and
approval system continue to depend only on the Provider protocol.

## New API Request Construction

For each decision, `NewAPIProvider` prepends one generated system message to
the normalized conversation. The message states that the model must return
exactly one JSON object without prose or Markdown and embeds a compact,
deterministically serialized copy of `Action.model_json_schema()`.

The HTTP payload is:

```json
{
  "model": "qwen-turbo",
  "messages": [
    {
      "role": "system",
      "content": "Return exactly one JSON object ... schema: {...}"
    }
  ],
  "response_format": {
    "type": "json_object"
  }
}
```

Existing `system`, `user`, and `assistant` roles retain their roles. Internal
`feedback` and `tool` roles continue to be normalized to prefixed user
messages. The generated schema instruction is not persisted in task messages;
it is reconstructed for each request from the trusted local Action model.

No provider-native tool object is supplied, and provider-native `tool_calls`
are never dispatched. Every action still returns as text and passes through
the existing `parse_action()` validation, policy evaluation, and bounded tool
runtime.

## Response Extraction

Response extraction follows this order:

1. A non-empty string in `choices[0].message.content` is returned.
2. If `content` is empty and `reasoning_content` is a string, it is accepted
   only when `parse_action(reasoning_content)` succeeds locally. Arbitrary
   chain-of-thought or prose is never returned or recorded.
3. Missing choices, malformed envelopes, and non-string response fields raise
   a safe structured provider error.
4. Provider-native `tool_calls` are ignored and cannot bypass the Harness.

A non-empty `content` value that does not satisfy the Action schema is returned
to `AgentLoop`. The normal action-parse feedback path consumes one Agent step
and gives the next model request a chance to correct the format. The Provider
does not hide model-format failures behind transport retries.

## Retry Semantics

One Agent decision performs at most two HTTP attempts. Both attempts together
still count as one Agent step.

Retryable conditions are:

- timeout;
- connection/URL transport failure;
- HTTP 429;
- HTTP 5xx;
- an otherwise valid response envelope with empty `content` and no locally
  valid Action in `reasoning_content`.

Non-retryable conditions are:

- HTTP 400, 401, 403, and other non-429 HTTP 4xx responses;
- a malformed JSON/envelope response;
- non-empty model content that fails Action parsing.

There is one bounded 250-millisecond delay before the second attempt. The
sleep function is injectable so tests remain fast and deterministic. Each
attempt uses `provider_timeout_seconds`; the njusehub example sets it to 20
seconds, making the worst normal timeout path approximately 40.25 seconds.
The README states that both attempts can be billed independently.

No automatic model fallback is performed. Changing models can change cost,
capabilities, and behavior and therefore remains an explicit operator choice.

## Safe Error Model and Audit Trail

`ProviderError` gains structured, non-secret metadata sufficient for the loop
to explain the failure safely:

- stable error code such as `timeout`, `http_error`, `empty_content`,
  `invalid_response`, or `connection_error`;
- HTTP status when available;
- number of attempts;
- whether the condition was retryable.

Its public message is generated from those fields. It never contains the API
key, Authorization header, request body, raw upstream response body, or model
reasoning. `AgentLoop` catches `ProviderError` separately and records a safe
feedback message and state event, for example:

```text
Provider timeout after 2 attempts.
Provider returned HTTP 429 after 2 attempts.
Provider returned empty content after 2 attempts.
Provider authentication failed with HTTP 401.
```

Unexpected exceptions retain the existing generic failure message. Provider
failures remain retryable within the Agent step budget exactly as before.

## CLI, Credentials, Configuration, and WebUI

CLI provider choices become:

```text
demo | openai | newapi
```

The normal local workflow is:

```bash
forgeloop credentials set newapi
forgeloop credentials status newapi
forgeloop serve --provider newapi --config njusehub.example.toml \
  --data-dir .forgeloop
```

The credential is stored under the separate `newapi` keyring slot. Existing
`openai` credentials are not copied automatically. When a secret-file backend
is selected, it is bound to the active provider name so containerized New API
mode can read the mounted secret without renaming it to `openai`.

The existing configuration fields are reused; no provider-specific retry
fields are added in this iteration:

- `provider_base_url`;
- `provider_model`;
- `provider_timeout_seconds`.

A tracked `njusehub.example.toml` contains the njusehub Chat Completions URL,
`qwen-turbo`, a 20-second provider timeout, the existing safety policy, and the
example validator. It contains no credential. The user's `njusehub.toml` is
added to `.gitignore` to reduce the risk of repeating the earlier plaintext
credential mistake.

Web dependencies expose the active provider name and configured model. The
home and task pages display:

```text
newapi · qwen-turbo
```

Credential status and settings operate on `newapi` when that mode is active.
The API continues to reject task requests whose provider does not match the
server's configured execution mode.

Configuration and credentials are loaded at service startup. Changing either
requires a restart and a newly created task; existing active tasks are not
migrated.

## Security Properties

- API keys remain in Keychain or an owner-only secret file, never TOML.
- The key is present only in the outbound Authorization header.
- Error and audit data use stable metadata, not upstream bodies.
- Schema instructions are generated from trusted local code, not model text.
- `json_object` is not treated as a security boundary; `parse_action()`, policy
  evaluation, approval binding, and the bounded runtime remain authoritative.
- `reasoning_content` is never logged and is accepted only when the entire
  value is already a valid strict Action.
- Provider-native tool calls are never executed.
- Retries are bounded to prevent unbounded cost and latency.

## Testing Strategy

All implementation follows red-green-refactor. Automated tests use fake
openers and an injected sleeper; they do not use network access or credentials.

Provider tests cover:

- the generated system instruction contains the deterministic Action schema;
- New API uses `response_format={"type":"json_object"}`;
- normal role preservation and internal-role normalization;
- valid `content` extraction;
- valid strict Action extraction from `reasoning_content` only when content is
  empty;
- rejection of arbitrary reasoning text and provider-native tool calls;
- exactly two attempts for timeout, connection failure, 429, 5xx, and empty
  content;
- no retry for 400, 401, 403, malformed envelopes, or non-empty invalid Action
  text;
- fixed bounded backoff through the injected sleeper;
- API key and Bearer header redaction from repr, exceptions, events, and test
  snapshots.

Loop, CLI, and Web tests cover:

- structured provider-error feedback and audit summaries;
- `newapi` CLI selection and separate credential lookup;
- secret-file binding to the selected provider;
- provider mismatch rejection;
- `newapi · model` rendering without exposing credentials;
- complete fake New API task flow: read, edit, validation, and finish;
- unchanged `demo` and `openai` behavior.

The full existing test suite must pass. A real-network smoke test is manual and
is never part of CI.

## Acceptance Criteria

1. `make test` passes without network access or an API key.
2. `demo` and `openai` modes retain their current tests and behavior.
3. `newapi` mode never sends the complex strict `json_schema` response format.
4. A fake New API integration completes the full Coding Agent feedback loop.
5. Retryable failures perform no more than two attempts in one Agent step.
6. Authentication and other non-retryable 4xx failures return immediately.
7. Audit events identify timeout, status, empty response, and attempt count
   without containing secrets or raw upstream bodies.
8. With the user's njusehub credential, `qwen-turbo` manually completes a
   `read -> edit -> validate -> finish` task.
9. With the same gateway, `deepseek-v4-flash` manually completes the same task
   or is documented as an upstream model availability failure with exact safe
   evidence.
10. README documents possible double billing from a retry, restart semantics,
    credential storage, and the distinction between `openai` and `newapi`.

## Migration and Rollout

The change is additive. Existing invocations using `--provider demo` or
`--provider openai` continue unchanged. Existing njusehub users perform the
explicit one-time migration:

```bash
forgeloop credentials set newapi
forgeloop serve --provider newapi --config njusehub.example.toml \
  --data-dir .forgeloop
```

The old `openai` keyring entry is left untouched so rollback remains possible.
No secrets are copied, printed, or committed during migration.
