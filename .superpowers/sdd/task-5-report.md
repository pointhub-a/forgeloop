# Task 5 Report: Persistent Memory, Credentials, and Redaction

## Outcome

Implemented the Task 5 SQLite memory and credential security boundaries:

- `MemoryStore.upsert/recall` persists project-scoped records under the
  `(project_id, key)` primary key, normalizes tags as sorted unique JSON, ranks
  matches by tag overlap and recency, and enforces both result and character
  budgets;
- memory rejects credential-shaped keys before executing a database write;
- `CredentialService` exposes non-disclosing status and keeps credential
  retrieval confined to `get_for_provider`;
- `KeyringBackend` is a thin operating-system keyring adapter and
  `SecretFileBackend` accepts only an owner-only regular file, rejects
  symlinks, revalidates the opened file descriptor, and remains read-only;
- `redact` masks explicitly registered secrets, bearer values, and common
  token shapes;
- the runtime dependency now includes `keyring>=25,<26`.

## TDD Evidence

### RED

1. Memory tests were written first and collection failed with
   `ModuleNotFoundError: No module named 'forgeloop.memory'`.
2. Credential tests were written next and collection failed with
   `ModuleNotFoundError: No module named 'forgeloop.credentials'`.
3. During self-review, the SPEC schema requirement for `tags_json` and
   `created_at` received a focused regression assertion; it failed with
   `sqlite3.OperationalError: no such column: tags_json` before the schema was
   updated.

### GREEN

- Focused: `.venv/bin/python -m pytest tests/test_memory.py tests/test_credentials.py -q`
  - Result: `23 passed in 0.02s`.
- Full: `.venv/bin/python -m pytest -q`
  - Result: `118 passed in 5.39s`.
- Diff hygiene: `git diff --check`
  - Result: clean.

## Self-Review

- Confirmed every recall query filters by `project_id`, excludes memories with
  no requested-tag overlap, ranks overlap before `updated_at`, and never
  returns values whose aggregate length exceeds `char_budget`.
- Confirmed upsert preserves `created_at`, updates `updated_at`, canonicalizes
  duplicate tags, and rejects the brief's complete
  `secret|token|password|api[_-]?key` pattern case-insensitively before SQL.
- Confirmed credential status and its representation contain only provider,
  configured state, and source; no credential is persisted in SQLite, logged,
  placed in an event, or included in a production exception.
- Confirmed the secret-file adapter rejects directories, symlinks, and any
  group/other permission bits, validates the opened descriptor, strips only a
  trailing line ending, and does not support plaintext mutation.
- Confirmed tests use real SQLite and real temporary files. The credential
  service uses an in-memory fake; the keyring adapter test replaces only the
  unavailable external keyring module and does not access a system Keychain.
- Confirmed `PLAN.md` is an unrelated main-controller modification and is not
  included in this task's staging set.

No unresolved Task 5 concern was found. The main controller requested that the
internal reviewer be stopped and will perform the external review.
