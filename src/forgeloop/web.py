"""Injected FastAPI composition for ForgeLoop's local Web UI and API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from forgeloop.credentials import CredentialService
from forgeloop.loop import ApprovalMismatch
from forgeloop.models import Event, TaskRecord, TaskStatus
from forgeloop.policy import action_fingerprint
from forgeloop.repository import TaskNotFound, TaskRepository
from forgeloop.service import TaskNotLoaded, TaskService


_PACKAGE_DIR = Path(__file__).parent
_CSRF_COOKIE = "forgeloop_csrf"
_CSRF_NONCE = re.compile(r"[A-Za-z0-9_-]{32,128}")


@dataclass(frozen=True)
class AppDependencies:
    """Runtime services supplied by the CLI or another composition root."""

    task_service: TaskService
    task_repository: TaskRepository
    credential_service: CredentialService
    csrf_secret: bytes
    demo_runner: Callable[[], dict[str, object]] | None
    provider_name: str = "demo"


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=10_000)
    workspace: str = Field(min_length=1, max_length=4096)
    provider: str = Field(min_length=1, max_length=100)

    @field_validator("description", "workspace", "provider")
    @classmethod
    def value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1, max_length=256)


class RejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)


class TaskDetailResponse(TaskRecord):
    events: list[Event]


class CredentialSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=1, max_length=16_384, repr=False)


class CredentialStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configured: bool
    source: str


_TERMINAL_TASK_STATUSES = {
    TaskStatus.DENIED,
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.BUDGET_EXHAUSTED,
    TaskStatus.NO_PROGRESS,
    TaskStatus.CANCELLED,
}


def _normalized_origin(
    scheme: str, hostname: str | None, port: int | None
) -> tuple[str, str, int] | None:
    if scheme not in {"http", "https"} or hostname is None:
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    return scheme, hostname.lower(), effective_port


def create_app(dependencies: AppDependencies) -> FastAPI:
    """Create an app without constructing providers or reading credentials."""

    if not isinstance(dependencies.csrf_secret, bytes) or not dependencies.csrf_secret:
        raise ValueError("csrf_secret must be non-empty bytes")
    app = FastAPI(title="ForgeLoop", version="0.1.0")
    templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")

    def csrf_context(request: Request) -> tuple[str, str, bool]:
        nonce = request.cookies.get(_CSRF_COOKIE, "")
        set_cookie = _CSRF_NONCE.fullmatch(nonce) is None
        if set_cookie:
            nonce = secrets.token_urlsafe(32)
        token = hmac.new(
            dependencies.csrf_secret, nonce.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return nonce, token, set_cookie

    def render(
        request: Request, template_name: str, context: dict[str, object] | None = None
    ) -> HTMLResponse:
        nonce, token, set_cookie = csrf_context(request)
        response = templates.TemplateResponse(
            request,
            template_name,
            {
                "csrf_token": token,
                "provider_name": dependencies.provider_name,
                **(context or {}),
            },
        )
        if set_cookie:
            response.set_cookie(
                _CSRF_COOKIE,
                nonce,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return response

    async def verified_form(request: Request) -> dict[str, str]:
        media_type = request.headers.get("content-type", "").split(";", 1)[0]
        if media_type != "application/x-www-form-urlencoded":
            raise HTTPException(status_code=403, detail="invalid browser form")
        body = await request.body()
        if len(body) > 32_768:
            raise HTTPException(status_code=422, detail="browser form is too large")
        try:
            parsed = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=20,
            )
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid browser form") from None
        if any(len(values) != 1 for values in parsed.values()):
            raise HTTPException(status_code=422, detail="invalid browser form")
        fields = {key: values[0] for key, values in parsed.items()}
        nonce = request.cookies.get(_CSRF_COOKIE, "")
        supplied = fields.pop("_csrf", "")
        if _CSRF_NONCE.fullmatch(nonce) is None:
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        expected = hmac.new(
            dependencies.csrf_secret, nonce.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        return fields

    def task_redirect(task_id: str) -> RedirectResponse:
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    @app.middleware("http")
    async def enforce_json_same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and origin is not None:
            try:
                parsed = urlsplit(origin)
                origin_value = _normalized_origin(
                    parsed.scheme, parsed.hostname, parsed.port
                )
                request_value = _normalized_origin(
                    request.url.scheme, request.url.hostname, request.url.port
                )
                malformed = bool(
                    parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in {"", "/"}
                )
            except ValueError:
                malformed = True
                origin_value = None
                request_value = None
            if malformed or origin_value is None or origin_value != request_value:
                return JSONResponse(
                    status_code=403, content={"detail": "cross-origin request denied"}
                )
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse(
                status_code=500, content={"detail": "internal server error"}
            )

    @app.exception_handler(TaskNotFound)
    async def task_not_found(_request: Request, _exc: TaskNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "task not found"})

    @app.exception_handler(RequestValidationError)
    async def request_validation_failed(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "type": error["type"],
                "loc": list(error["loc"]),
                "msg": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(TaskNotLoaded)
    async def task_not_loaded(_request: Request, _exc: TaskNotLoaded) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "task is not active in this process"},
        )

    @app.exception_handler(ApprovalMismatch)
    async def approval_mismatch(
        _request: Request, _exc: ApprovalMismatch
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": "invalid approval transition"}
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return render(request, "index.html")

    @app.get("/healthz")
    def health() -> dict[str, str | bool]:
        try:
            with sqlite3.connect(dependencies.task_repository.db_path) as connection:
                row = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()
            database_ready = row == (1,)
        except (OSError, sqlite3.Error):
            database_ready = False
        return {"version": app.version, "database_ready": database_ready}

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        return render(
            request,
            "settings.html",
            {"credential": dependencies.credential_service.status("openai")},
        )

    @app.post("/tasks")
    async def create_task_from_form(request: Request) -> RedirectResponse:
        fields = await verified_form(request)
        try:
            payload = TaskCreateRequest.model_validate(fields)
        except ValidationError:
            raise HTTPException(status_code=422, detail="invalid task input") from None
        if payload.provider != dependencies.provider_name:
            raise HTTPException(
                status_code=422,
                detail="provider does not match the configured execution mode",
            )
        task = dependencies.task_service.create(
            payload.description, payload.workspace
        )
        return task_redirect(task.id)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(request: Request, task_id: str) -> HTMLResponse:
        task = dependencies.task_repository.get(task_id)
        fingerprint = (
            action_fingerprint(task.pending_action)
            if task.pending_action is not None
            else None
        )
        return render(
            request,
            "task.html",
            {
                "task": task,
                "events": dependencies.task_repository.list_events(task_id),
                "fingerprint": fingerprint,
            },
        )

    @app.post("/tasks/{task_id}/advance")
    async def advance_task_from_form(
        request: Request, task_id: str
    ) -> RedirectResponse:
        await verified_form(request)
        require_task_status(task_id, TaskStatus.RUNNING)
        dependencies.task_service.advance(task_id)
        return task_redirect(task_id)

    @app.post("/tasks/{task_id}/approve")
    async def approve_task_from_form(
        request: Request, task_id: str
    ) -> RedirectResponse:
        fields = await verified_form(request)
        try:
            payload = ApprovalRequest.model_validate(fields)
        except ValidationError:
            raise HTTPException(status_code=422, detail="invalid approval input") from None
        require_task_status(task_id, TaskStatus.WAITING_APPROVAL)
        dependencies.task_service.approve(task_id, payload.fingerprint)
        return task_redirect(task_id)

    @app.post("/tasks/{task_id}/reject")
    async def reject_task_from_form(
        request: Request, task_id: str
    ) -> RedirectResponse:
        fields = await verified_form(request)
        try:
            payload = RejectionRequest.model_validate(fields)
        except ValidationError:
            raise HTTPException(status_code=422, detail="invalid rejection input") from None
        require_task_status(task_id, TaskStatus.WAITING_APPROVAL)
        dependencies.task_service.reject(task_id, reason=payload.reason)
        return task_redirect(task_id)

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task_from_form(
        request: Request, task_id: str
    ) -> RedirectResponse:
        await verified_form(request)
        task = dependencies.task_repository.get(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            raise HTTPException(status_code=409, detail="invalid task transition")
        dependencies.task_service.cancel(task_id)
        return task_redirect(task_id)

    @app.post("/settings/credentials/{provider}")
    async def set_credential_from_form(
        request: Request, provider: str
    ) -> RedirectResponse:
        fields = await verified_form(request)
        try:
            payload = CredentialSetRequest.model_validate(fields)
            dependencies.credential_service.set(provider, payload.secret)
        except ValidationError:
            raise HTTPException(status_code=422, detail="invalid credential input") from None
        except ValueError:
            raise HTTPException(
                status_code=422, detail="credential value is invalid"
            ) from None
        except TypeError:
            raise HTTPException(
                status_code=409, detail="credential source is read-only"
            ) from None
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/settings/credentials/{provider}/clear")
    async def clear_credential_from_form(
        request: Request, provider: str
    ) -> RedirectResponse:
        await verified_form(request)
        try:
            dependencies.credential_service.clear(provider)
        except TypeError:
            raise HTTPException(
                status_code=409, detail="credential source is read-only"
            ) from None
        return RedirectResponse(url="/settings", status_code=303)

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page(request: Request) -> HTMLResponse:
        return render(request, "demo.html", {"demo_result": None})

    @app.post("/demo", response_class=HTMLResponse)
    async def run_demo(request: Request) -> HTMLResponse:
        await verified_form(request)
        if dependencies.demo_runner is None:
            raise HTTPException(status_code=503, detail="demo runner is unavailable")
        return render(
            request,
            "demo.html",
            {"demo_result": dependencies.demo_runner()},
        )

    @app.post(
        "/api/tasks",
        response_model=TaskRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(payload: TaskCreateRequest) -> TaskRecord:
        if payload.provider != dependencies.provider_name:
            raise HTTPException(
                status_code=422,
                detail="provider does not match the configured execution mode",
            )
        return dependencies.task_service.create(
            payload.description, payload.workspace
        )

    def require_task_status(task_id: str, expected: TaskStatus) -> TaskRecord:
        task = dependencies.task_repository.get(task_id)
        if task.status is not expected:
            raise HTTPException(status_code=409, detail="invalid task transition")
        return task

    @app.get("/api/tasks/{task_id}", response_model=TaskDetailResponse)
    def get_task(task_id: str) -> TaskDetailResponse:
        task = dependencies.task_repository.get(task_id)
        return TaskDetailResponse.model_validate(
            {
                **task.model_dump(),
                "events": dependencies.task_repository.list_events(task_id),
            }
        )

    @app.post("/api/tasks/{task_id}/advance", response_model=TaskRecord)
    def advance_task(task_id: str) -> TaskRecord:
        require_task_status(task_id, TaskStatus.RUNNING)
        return dependencies.task_service.advance(task_id)

    @app.post("/api/tasks/{task_id}/approve", response_model=TaskRecord)
    def approve_task(task_id: str, payload: ApprovalRequest) -> TaskRecord:
        require_task_status(task_id, TaskStatus.WAITING_APPROVAL)
        return dependencies.task_service.approve(task_id, payload.fingerprint)

    @app.post("/api/tasks/{task_id}/reject", response_model=TaskRecord)
    def reject_task(
        task_id: str, payload: RejectionRequest | None = None
    ) -> TaskRecord:
        require_task_status(task_id, TaskStatus.WAITING_APPROVAL)
        return dependencies.task_service.reject(
            task_id, reason=payload.reason if payload is not None else ""
        )

    @app.post("/api/tasks/{task_id}/cancel", response_model=TaskRecord)
    def cancel_task(task_id: str) -> TaskRecord:
        task = dependencies.task_repository.get(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            raise HTTPException(status_code=409, detail="invalid task transition")
        return dependencies.task_service.cancel(task_id)

    def credential_status(provider: str) -> CredentialStatusResponse:
        credential = dependencies.credential_service.status(provider)
        return CredentialStatusResponse(
            configured=credential.configured,
            source=credential.source,
        )

    @app.get(
        "/api/credentials/{provider}", response_model=CredentialStatusResponse
    )
    def get_credential_status(provider: str) -> CredentialStatusResponse:
        return credential_status(provider)

    @app.put(
        "/api/credentials/{provider}", response_model=CredentialStatusResponse
    )
    def set_credential(
        provider: str, payload: CredentialSetRequest
    ) -> CredentialStatusResponse:
        try:
            dependencies.credential_service.set(provider, payload.secret)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="credential value is invalid"
            ) from None
        except TypeError:
            raise HTTPException(
                status_code=409, detail="credential source is read-only"
            ) from None
        return credential_status(provider)

    @app.delete(
        "/api/credentials/{provider}", response_model=CredentialStatusResponse
    )
    def clear_credential(provider: str) -> CredentialStatusResponse:
        try:
            dependencies.credential_service.clear(provider)
        except TypeError:
            raise HTTPException(
                status_code=409, detail="credential source is read-only"
            ) from None
        return credential_status(provider)

    return app
