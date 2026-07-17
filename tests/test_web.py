from dataclasses import dataclass
import json
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from forgeloop.config import HarnessConfig
from forgeloop.credentials import CredentialService
from forgeloop.feedback import ProgressTracker
from forgeloop.loop import AgentLoop
from forgeloop.memory import MemoryStore
from forgeloop.models import TaskRecord
from forgeloop.policy import PolicyEngine
from forgeloop.policy import action_fingerprint
from forgeloop.providers import ScriptedProvider
from forgeloop.repository import ApprovalRepository, TaskRepository
from forgeloop.service import TaskService
from forgeloop.tools import ToolRuntime
from forgeloop.web import AppDependencies, create_app


class MemoryCredentialBackend:
    source = "memory"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.failure: str | None = None
        self.reads: list[str] = []

    def get(self, provider: str) -> str | None:
        self.reads.append(provider)
        if self.failure is not None:
            raise RuntimeError(self.failure)
        return self.values.get(provider)

    def set(self, provider: str, secret: str) -> None:
        if self.failure is not None:
            raise RuntimeError(f"{self.failure}: {secret}")
        self.values[provider] = secret

    def clear(self, provider: str) -> None:
        if self.failure is not None:
            raise RuntimeError(self.failure)
        self.values.pop(provider, None)


class NoValidators:
    def run_all(self) -> list[object]:
        return []


@dataclass(frozen=True)
class WebHarness:
    client: TestClient
    task_service: TaskService
    task_repository: TaskRepository


def loop_factory(workspace: Path, task_id: str) -> AgentLoop:
    config = HarnessConfig()
    dangerous_action = json.dumps(
        {"kind": "run_command", "arguments": {"argv": ["rm", "-rf", "build"]}}
    )
    return AgentLoop(
        provider=ScriptedProvider([dangerous_action]),
        policy=PolicyEngine(config),
        tools=ToolRuntime(workspace, config),
        validators=NoValidators(),
        progress=ProgressTracker(
            max_identical_failures=config.max_identical_failures,
            max_identical_actions=config.max_identical_actions,
        ),
        memory=MemoryStore(":memory:"),
        config=config,
        project_id=task_id,
    )


@pytest.fixture
def credential_backend() -> MemoryCredentialBackend:
    return MemoryCredentialBackend()


@pytest.fixture
def credential_service(
    credential_backend: MemoryCredentialBackend,
) -> CredentialService:
    return CredentialService(credential_backend)


@pytest.fixture
def web_harness(
    tmp_path: Path, credential_service: CredentialService
) -> WebHarness:
    database = tmp_path / "forgeloop.db"
    tasks = TaskRepository(database)
    service = TaskService(
        tasks,
        ApprovalRepository(database),
        loop_factory,
    )
    dependencies = AppDependencies(
        task_service=service,
        task_repository=tasks,
        credential_service=credential_service,
        csrf_secret=b"test-only-csrf-secret",
        demo_runner=lambda: {"status": "ok", "proof": ["policy", "validation"]},
        provider_name="demo",
    )
    with TestClient(create_app(dependencies)) as test_client:
        yield WebHarness(test_client, service, tasks)


@pytest.fixture
def client(web_harness: WebHarness) -> TestClient:
    return web_harness.client


@pytest.fixture
def pending_task(web_harness: WebHarness, tmp_path: Path) -> TaskRecord:
    workspace = tmp_path / "pending-workspace"
    workspace.mkdir()
    created = web_harness.client.post(
        "/api/tasks",
        json={
            "description": "clean build",
            "workspace": str(workspace),
            "provider": "demo",
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    advanced = web_harness.client.post(f"/api/tasks/{task_id}/advance")
    assert advanced.status_code == 200
    return web_harness.task_repository.get(task_id)


def test_home_explains_product_and_security_boundary(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "ForgeLoop" in response.text
    assert "工作区" in response.text


def test_settings_never_returns_credential(
    client: TestClient,
    credential_service: CredentialService,
    credential_backend: MemoryCredentialBackend,
) -> None:
    credential_service.set("demo", "sk-unmistakably-fake-never-render")

    response = client.get("/settings")

    assert response.status_code == 200
    assert "sk-unmistakably-fake-never-render" not in response.text
    assert "已配置" in response.text
    assert "demo" in response.text
    assert credential_backend.reads == ["demo"]


def test_mock_task_can_be_created_and_viewed(
    client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    created = client.post(
        "/api/tasks",
        json={
            "description": "fix calc",
            "workspace": str(workspace),
            "provider": "demo",
        },
    )

    assert created.status_code == 201
    task_id = created.json()["id"]
    assert client.get(f"/api/tasks/{task_id}").status_code == 200


def test_approval_requires_matching_fingerprint(
    client: TestClient, pending_task: TaskRecord
) -> None:
    response = client.post(
        f"/api/tasks/{pending_task.id}/approve", json={"fingerprint": "wrong"}
    )

    assert response.status_code == 409


def test_health_reports_version_and_database_readiness(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"version": "0.1.0", "database_ready": True}


def test_home_uses_accessible_local_assets(client: TestClient) -> None:
    response = client.get("/")

    assert 'href="/static/style.css"' in response.text
    assert 'src="/static/app.js"' in response.text
    assert "https://" not in response.text
    assert "安全边界" in response.text

    stylesheet = client.get("/static/style.css")
    assert stylesheet.status_code == 200
    assert all(
        marker in stylesheet.text
        for marker in (
            "--ink",
            "--amber",
            "--teal",
            ":focus-visible",
            "@media",
            ".trace",
            "monospace",
        )
    )
    assert client.get("/static/app.js").status_code == 200


def test_json_api_rejects_cross_origin_and_wrong_provider(
    client: TestClient, tmp_path: Path
) -> None:
    payload = {
        "description": "fix calc",
        "workspace": str(tmp_path),
        "provider": "demo",
    }

    cross_origin = client.post(
        "/api/tasks", json=payload, headers={"Origin": "https://evil.example"}
    )
    assert cross_origin.status_code == 403

    wrong_provider = client.post(
        "/api/tasks", json={**payload, "provider": "openai"}
    )
    assert wrong_provider.status_code == 422

    same_origin = client.post(
        "/api/tasks", json=payload, headers={"Origin": "http://testserver"}
    )
    assert same_origin.status_code == 201


def test_json_api_rejects_malformed_origin_without_error(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/tasks/missing", headers={"Origin": "http://testserver:not-a-port"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "cross-origin request denied"}


def test_untrusted_host_is_rejected_even_when_origin_matches(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/tasks/missing",
        headers={"Host": "evil.example", "Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "untrusted host"}


def test_origin_must_exactly_match_trusted_request_origin(
    client: TestClient,
) -> None:
    default_port = client.get(
        "/api/tasks/missing", headers={"Origin": "http://testserver:80"}
    )
    trusted = client.get(
        "/api/tasks/missing", headers={"Origin": "http://testserver"}
    )

    assert default_port.status_code == 403
    assert trusted.status_code == 404


def test_explicit_allowed_host_supports_deployment(
    web_harness: WebHarness,
) -> None:
    dependencies = AppDependencies(
        task_service=web_harness.task_service,
        task_repository=web_harness.task_repository,
        credential_service=CredentialService(MemoryCredentialBackend()),
        csrf_secret=b"deployment-csrf-secret",
        demo_runner=None,
        provider_name="demo",
        allowed_hosts=frozenset({"deploy.internal"}),
    )

    with TestClient(
        create_app(dependencies), base_url="http://deploy.internal"
    ) as deployment_client:
        assert deployment_client.get("/healthz").status_code == 200
        assert (
            deployment_client.get(
                "/healthz", headers={"Host": "testserver"}
            ).status_code
            == 403
        )


def test_task_detail_includes_ordered_audit_events(
    web_harness: WebHarness, tmp_path: Path
) -> None:
    created = web_harness.client.post(
        "/api/tasks",
        json={
            "description": "inspect trace",
            "workspace": str(tmp_path),
            "provider": "demo",
        },
    )
    task_id = created.json()["id"]

    response = web_harness.client.get(f"/api/tasks/{task_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task_id
    assert [event["sequence"] for event in body["events"]] == [1]
    assert body["events"][0]["kind"] == "state"


def test_unknown_task_is_404(client: TestClient) -> None:
    response = client.get("/api/tasks/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "task not found"}


def test_pending_task_rejects_advance_and_can_be_rejected(
    client: TestClient, pending_task: TaskRecord
) -> None:
    invalid = client.post(f"/api/tasks/{pending_task.id}/advance")
    assert invalid.status_code == 409

    rejected = client.post(
        f"/api/tasks/{pending_task.id}/reject", json={"reason": "too broad"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "running"
    assert rejected.json()["pending_action"] is None


def test_pending_task_can_be_approved_then_cancelled(
    client: TestClient, pending_task: TaskRecord
) -> None:
    assert pending_task.pending_action is not None
    fingerprint = action_fingerprint(pending_task.pending_action)

    approved = client.post(
        f"/api/tasks/{pending_task.id}/approve",
        json={"fingerprint": fingerprint},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "running"

    cancelled = client.post(f"/api/tasks/{pending_task.id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    repeated = client.post(f"/api/tasks/{pending_task.id}/cancel")
    assert repeated.status_code == 409


def test_credential_api_exposes_only_status_metadata(
    client: TestClient, credential_backend: MemoryCredentialBackend
) -> None:
    initial = client.get("/api/credentials/demo")
    assert initial.status_code == 200
    assert initial.json() == {"configured": False, "source": "memory"}

    secret = "sk-unmistakably-fake-never-return"
    configured = client.put(
        "/api/credentials/demo", json={"secret": secret}
    )
    assert configured.status_code == 200
    assert configured.json() == {"configured": True, "source": "memory"}
    assert secret not in configured.text
    assert credential_backend.values["demo"] == secret

    cleared = client.delete("/api/credentials/demo")
    assert cleared.status_code == 200
    assert cleared.json() == {"configured": False, "source": "memory"}


def test_bad_credential_input_is_422_without_disclosure(client: TestClient) -> None:
    secret = " " * 17_000

    response = client.put(
        "/api/credentials/demo", json={"secret": secret}
    )

    assert response.status_code == 422
    assert secret not in response.text


def csrf_token(response) -> str:
    match = re.search(r'name="_csrf" value="([a-f0-9]+)"', response.text)
    assert match is not None
    return match.group(1)


def test_browser_task_form_requires_cookie_bound_csrf(
    client: TestClient, tmp_path: Path
) -> None:
    page = client.get("/")
    token = csrf_token(page)
    set_cookie = page.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie

    fields = {
        "_csrf": token,
        "description": "fix from browser",
        "workspace": str(tmp_path),
        "provider": "demo",
    }
    missing = client.post(
        "/tasks", data={key: value for key, value in fields.items() if key != "_csrf"}
    )
    assert missing.status_code == 403

    client.cookies.clear()
    client.cookies.set("forgeloop_csrf", "attacker-controlled-nonce")
    mismatched = client.post("/tasks", data=fields)
    assert mismatched.status_code == 403


def test_browser_task_can_be_created_and_rendered(
    client: TestClient, tmp_path: Path
) -> None:
    token = csrf_token(client.get("/"))

    response = client.post(
        "/tasks",
        data={
            "_csrf": token,
            "description": "render audit trail",
            "workspace": str(tmp_path),
            "provider": "demo",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "render audit trail" in detail.text
    assert "Task started." in detail.text
    assert "demo" in detail.text


def test_browser_approval_and_settings_forms_use_csrf(
    client: TestClient,
    pending_task: TaskRecord,
    credential_backend: MemoryCredentialBackend,
) -> None:
    assert pending_task.pending_action is not None
    fingerprint = action_fingerprint(pending_task.pending_action)
    task_page = client.get(f"/tasks/{pending_task.id}")
    token = csrf_token(task_page)

    without_token = client.post(
        f"/tasks/{pending_task.id}/approve", data={"fingerprint": fingerprint}
    )
    assert without_token.status_code == 403
    approved = client.post(
        f"/tasks/{pending_task.id}/approve",
        data={"_csrf": token, "fingerprint": fingerprint},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    settings = client.get("/settings")
    settings_token = csrf_token(settings)
    secret = "sk-unmistakably-fake-browser-only"
    configured = client.post(
        "/settings/credentials/demo",
        data={"_csrf": settings_token, "secret": secret},
        follow_redirects=False,
    )
    assert configured.status_code == 303
    assert secret not in configured.text
    assert credential_backend.values["demo"] == secret

    cleared = client.post(
        "/settings/credentials/demo/clear",
        data={"_csrf": settings_token},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert "demo" not in credential_backend.values


def test_demo_runs_only_from_csrf_protected_form(client: TestClient) -> None:
    page = client.get("/demo")
    token = csrf_token(page)
    assert "确定性机制演示" in page.text

    denied = client.post("/demo", data={})
    assert denied.status_code == 403

    result = client.post("/demo", data={"_csrf": token})
    assert result.status_code == 200
    assert "policy" in result.text
    assert "validation" in result.text


def test_internal_errors_never_disclose_exception_or_secret(
    client: TestClient, credential_backend: MemoryCredentialBackend
) -> None:
    secret = "sk-unmistakably-fake-backend-failure"
    credential_backend.failure = secret

    response = client.get("/api/credentials/demo")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert secret not in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_credential_api_rejects_provider_mismatch(
    client: TestClient,
    credential_backend: MemoryCredentialBackend,
    method: str,
) -> None:
    kwargs = (
        {"json": {"secret": "sk-unmistakably-fake-not-stored"}}
        if method == "put"
        else {}
    )

    response = getattr(client, method)("/api/credentials/openai", **kwargs)

    assert response.status_code == 422
    assert credential_backend.values == {}
    assert credential_backend.reads == []


@pytest.mark.parametrize("suffix", ["", "/clear"])
def test_settings_form_rejects_provider_mismatch(
    client: TestClient,
    credential_backend: MemoryCredentialBackend,
    suffix: str,
) -> None:
    token = csrf_token(client.get("/settings"))
    credential_backend.reads.clear()
    fields = {"_csrf": token}
    if not suffix:
        fields["secret"] = "sk-unmistakably-fake-not-stored"

    response = client.post(f"/settings/credentials/openai{suffix}", data=fields)

    assert response.status_code == 422
    assert credential_backend.values == {}
    assert credential_backend.reads == []


def test_empty_csrf_secret_is_rejected(web_harness: WebHarness) -> None:
    with pytest.raises(ValueError, match="csrf_secret"):
        create_app(
            AppDependencies(
                task_service=web_harness.task_service,
                task_repository=web_harness.task_repository,
                credential_service=CredentialService(MemoryCredentialBackend()),
                csrf_secret=b"",
                demo_runner=None,
                provider_name="demo",
            )
        )
