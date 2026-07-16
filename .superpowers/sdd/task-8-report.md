# Task 8 Report — Local WebUI and HTTP API

## Status

PASS. Implemented the dependency-injected FastAPI/Jinja WebUI and typed JSON
API, local visual assets, task trace and approval controls, credential status and
mutation routes, health readiness, same-origin enforcement, and HMAC-bound CSRF
protection.

## Delivered files

- `pyproject.toml`
- `src/forgeloop/web.py`
- `src/forgeloop/templates/base.html`
- `src/forgeloop/templates/index.html`
- `src/forgeloop/templates/task.html`
- `src/forgeloop/templates/settings.html`
- `src/forgeloop/templates/demo.html`
- `src/forgeloop/static/style.css`
- `src/forgeloop/static/app.js`
- `tests/test_web.py`

## Interface clarification

`TaskService.create` has no provider argument and the injected dependency set
contains exactly one task service. Per controller direction, `AppDependencies`
therefore adds `provider_name: str = "demo"`. Both HTML and JSON task creation
require a provider value that exactly matches this injected name; the route does
not construct, choose, or inspect a Provider. Task 9 can select demo or real
execution only by injecting the matching service plus provider name.

## TDD evidence

Production behavior was added through focused RED/GREEN cycles:

1. App composition and disclosure boundary
   - RED: `ModuleNotFoundError: No module named 'forgeloop.web'`.
   - GREEN: the home page explains ForgeLoop and its workspace boundary; the
     settings page renders only non-disclosing credential status metadata.
2. Task creation and approval API
   - RED: task endpoints returned 404; wrong approval could not be exercised.
   - GREEN: typed create/get/advance/approve routes returned persisted task data,
     and a mismatched approval fingerprint returned 409.
3. Health and local assets
   - RED: `/healthz` and `/static/*` returned 404.
   - GREEN: health reports version and schema readiness; local CSS/JS provide the
     ink/amber/teal console palette, visible focus, trace monospace, and mobile
     breakpoint without CDN or SVG assets.
4. API origin and transition boundaries
   - RED: cross-origin task creation returned 201; GET lacked events; advancing a
     pending task returned 200; reject/cancel routes were absent.
   - GREEN: present foreign origins fail 403, task detail includes ordered audit
     events, and invalid state transitions fail 409 while valid
     advance/approve/reject/cancel flows persist normally.
5. Credential API disclosure
   - RED: REST credential routes returned 404.
   - GREEN: status/set/clear return exactly `configured/source`; sanitized 422
     responses remove Pydantic input values, so even oversized submitted secrets
     are not echoed.
6. Browser forms and CSRF
   - RED: pages had no signed token, task/detail/demo routes were absent, and
     settings forms could not mutate credentials.
   - GREEN: every browser mutation validates a URL-encoded form HMAC bound to an
     HttpOnly, SameSite=Strict nonce cookie. Missing tokens and tokens paired with
     a different cookie fail 403. Successful task, approval, settings, and demo
     forms redirect or render as designed.
7. Generic error disclosure
   - RED: a backend exception containing a token-shaped secret propagated through
     TestClient; an empty CSRF secret was accepted.
   - GREEN: the outer app boundary returns only a generic 500, and app creation
     rejects empty CSRF bytes.
8. Malformed Origin parsing
   - RED: `Origin: http://testserver:not-a-port` raised `ValueError` before a
     response could be returned.
   - GREEN: malformed origin components are caught and rejected with the same
     generic 403 response.
9. Wheel resources
   - RED: the first built wheel contained Python modules but no templates or
     static assets.
   - GREEN: explicit setuptools package data includes all five templates and both
     local assets in the wheel.

## Verification

Focused suite:

```text
.venv/bin/python -m pytest tests/test_web.py -q
....................                                                     [100%]
20 passed in 0.28s
```

Full regression suite:

```text
.venv/bin/python -m pytest -q
........................................................................ [ 33%]
........................................................................ [ 67%]
.....................................................................    [100%]
213 passed in 5.98s
```

Additional checks:

- `.venv/bin/python -m py_compile src/forgeloop/web.py` passed.
- `git diff --check` passed before the report was written.
- A no-dependency wheel build was inspected and contains
  `forgeloop/templates/*.html` and `forgeloop/static/*`.
- Both focused and full pytest output are warning-free.

## Dependency evolution

The original brief requested `httpx>=0.28,<1`. Resolving current allowed
FastAPI installed Starlette 1.3.1, whose TestClient emitted a deprecation warning
requiring `httpx2`. No `httpx2` release exists below version 2, so the final dev
dependency is `httpx2>=2,<3`; TestClient and all suites pass without warnings.

## Self-review

- `create_app` consumes only injected services and never constructs a Provider or
  calls `CredentialService.get_for_provider` from a route.
- JSON requests reject a present Origin unless normalized scheme, host, and port
  match the request; malformed origins fail closed.
- Browser forms accept only bounded URL-encoded bodies, reject duplicate fields,
  and compare HMACs with `compare_digest`.
- Task mutation routes check persisted state before invoking `TaskService`; unknown
  tasks return 404, invalid transitions and unloaded tasks return 409, and bad
  inputs return sanitized 422 responses.
- Credential output models contain only `configured` and `source`. Unexpected
  exceptions are converted to a generic 500 without traceback or exception text.
- The first viewport centers the concrete task form and security boundary. HTML
  uses semantic landmarks, labels, heading relationships, keyboard-visible focus,
  a 760px responsive breakpoint, and reduced-motion-aware styling.
- Static inspection found no remote URLs, CDN references, or SVG markup.
- Tests exercise real FastAPI routing, SQLite repositories, TaskService, AgentLoop,
  policy, and TestClient behavior; no test-only production methods or mock
  assertions were added.
- The controller-owned `PLAN.md` remains outside this task's staging set.

## Concerns / handoff

No unresolved functional concern remains. The in-app browser runtime reported no
available browser backend, so screenshot-based desktop/mobile visual QA could not
be performed. Static visual/accessibility review and rendered TestClient HTML
coverage passed; a later manual browser glance is optional, not a release blocker.

---

## External review remediation

The external review identified one critical Host/DNS-rebinding boundary and two
important consistency/concurrency gaps. All three were repaired test-first. This
section supersedes the original self-review statements about normalized Origin
comparison and Web-owned task state prechecks.

### RED / GREEN evidence

1. Trusted Host before exact Origin
   - RED: `Host: evil.example` paired with `Origin: http://evil.example` reached
     the task route and returned 404; `Origin: http://testserver:80` was treated
     as equivalent to `http://testserver`; `AppDependencies` rejected the new
     explicit `allowed_hosts` argument.
   - GREEN: every request first parses the Host header to a hostname and checks
     the injected frozenset allowlist. Defaults are `localhost`, `127.0.0.1`,
     `::1`, and `testserver`; deployments can inject a different hostname set.
     Only after Host trust succeeds does the JSON API compare Origin scheme and
     netloc exactly against the request origin. Malformed Host/Origin values and
     DNS-rebinding pairs fail 403.
2. One injected provider across settings and credential routes
   - RED: a demo-mode settings page read `openai`, while mismatched credential
     GET/PUT/DELETE and HTML set/clear paths returned success and touched the
     backend.
   - GREEN: settings status, labels, and form actions use
     `dependencies.provider_name`. Every credential path must match it exactly or
     return 422 before any credential backend read or mutation. Status/set/clear
     mismatch tests verify zero backend access.
3. Lock-owned task state transitions
   - RED: importing `InvalidStateTransition` failed; after adding the requested
     public exception, repeated and concurrent cancel calls both returned success,
     and advancing a cancelled task returned silently. Removing Web's repository
     prechecks exposed generic 500 responses.
   - GREEN: `TaskService.advance` requires RUNNING and `cancel` rejects every
     terminal status while holding the same per-task `RLock` used for mutation.
     Two synchronized cancel callers now yield exactly one cancelled result and
     one `InvalidStateTransition`, with one cancellation event. Terminal advance
     leaves loop step count and audit events unchanged. Web performs no lock-free
     state precheck and maps the service exception to 409; approve/reject retain
     their existing lock-owned pending-approval validation.

### Post-remediation verification

Focused service/Web suite:

```text
.venv/bin/python -m pytest tests/test_service.py tests/test_web.py -q
...............................................                          [100%]
47 passed in 0.49s
```

Full regression suite:

```text
.venv/bin/python -m pytest -q
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 96%]
.......                                                                  [100%]
223 passed in 6.08s
```

`.venv/bin/python -m py_compile src/forgeloop/service.py src/forgeloop/web.py`
and `git diff --check` passed. Both pytest runs were warning-free. Tests use the
real FastAPI application, SQLite repositories, `TaskService`, and live
`AgentLoop` state; no test-only production method or mock assertion was added.
The controller-owned `PLAN.md` remains outside this remediation's staging set.
No unresolved remediation concern remains.
