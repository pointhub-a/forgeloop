import getpass
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeloop.cli import main


class FakeCredentialBackend:
    source = "memory"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.reads: list[str] = []

    def get(self, provider: str) -> str | None:
        self.reads.append(provider)
        return self.values.get(provider)

    def set(self, provider: str, secret: str) -> None:
        self.values[provider] = secret

    def clear(self, provider: str) -> None:
        self.values.pop(provider, None)


class ExplodingCredentialBackend:
    source = "exploding"

    def __init__(self, failure: str) -> None:
        self.failure = failure

    def get(self, provider: str) -> str | None:
        raise RuntimeError(f"{provider}: {self.failure}")

    def set(self, provider: str, secret: str) -> None:
        raise RuntimeError(f"{provider}: {self.failure}: {secret}")

    def clear(self, provider: str) -> None:
        raise RuntimeError(f"{provider}: {self.failure}")


@pytest.fixture
def fake_backend() -> FakeCredentialBackend:
    return FakeCredentialBackend()


def test_demo_cli_prints_machine_readable_summary(capsys) -> None:
    assert main(["demo", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["final_status"] == "succeeded"
    assert payload["no_progress_status"] == "no_progress"


def test_credentials_set_uses_hidden_input(
    monkeypatch, fake_backend: FakeCredentialBackend, capsys
) -> None:
    monkeypatch.setattr(
        getpass, "getpass", lambda _prompt: "sk-unmistakably-fake-input"
    )

    assert main(
        ["credentials", "set", "openai"], backend=fake_backend
    ) == 0

    assert fake_backend.values == {"openai": "sk-unmistakably-fake-input"}
    assert "sk-unmistakably-fake-input" not in capsys.readouterr().out


def test_credentials_status_and_clear_never_reveal_secret(
    fake_backend: FakeCredentialBackend, capsys
) -> None:
    fake_backend.values["openai"] = "sk-unmistakably-fake-never-print"

    assert main(
        ["credentials", "status", "openai"], backend=fake_backend
    ) == 0
    status_output = capsys.readouterr().out
    assert "configured" in status_output
    assert "memory" in status_output
    assert "sk-unmistakably-fake-never-print" not in status_output

    assert main(
        ["credentials", "clear", "openai"], backend=fake_backend
    ) == 0
    clear_output = capsys.readouterr().out
    assert "cleared" in clear_output
    assert "sk-unmistakably-fake-never-print" not in clear_output
    assert fake_backend.values == {}


def test_credentials_set_redacts_registered_secret_from_backend_error(
    monkeypatch, capsys
) -> None:
    entered_secret = "ordinary-provider-credential"
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: entered_secret)
    backend = ExplodingCredentialBackend("backend rejected supplied value")

    assert main(["credentials", "set", "openai"], backend=backend) == 2

    error = capsys.readouterr().err
    assert entered_secret not in error
    assert "[REDACTED]" in error


@pytest.mark.parametrize("command", ["status", "clear"])
def test_credentials_read_and_clear_redact_token_shaped_backend_errors(
    command: str, capsys
) -> None:
    fake_token = "sk-unmistakably-fake-token"
    backend = ExplodingCredentialBackend(f"backend failed near {fake_token}")

    assert main(["credentials", command, "openai"], backend=backend) == 2

    error = capsys.readouterr().err
    assert fake_token not in error
    assert "[REDACTED]" in error


class CapturingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    def __call__(self, app, **kwargs: object) -> None:
        self.calls.append((app, kwargs))


def test_serve_demo_composes_app_without_reading_credentials(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()

    assert main(
        ["serve", "--provider", "demo", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 0

    assert fake_backend.reads == []
    assert len(runner.calls) == 1
    app, options = runner.calls[0]
    assert options == {"host": "127.0.0.1", "port": 8000}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200


def test_serve_demo_reaches_custom_no_progress_threshold(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    config_path = tmp_path / "forgeloop.toml"
    config_path.write_text("max_identical_actions = 4\n", encoding="utf-8")
    runner = CapturingRunner()

    assert main(
        [
            "serve",
            "--provider",
            "demo",
            "--data-dir",
            str(tmp_path),
            "--config",
            str(config_path),
        ],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 0

    app, _options = runner.calls[0]
    workspace = tmp_path / "custom-threshold-workspace"
    workspace.mkdir()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/tasks",
            json={
                "description": "reach configured no-progress threshold",
                "workspace": str(workspace),
                "provider": "demo",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        for _step in range(4):
            advanced = client.post(f"/api/tasks/{task_id}/advance")
            assert advanced.status_code == 200

        assert advanced.json()["status"] == "no_progress"
        assert advanced.json()["step_count"] == 4
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert all(
            event["data"].get("reason") != "provider_failure"
            for event in detail["events"]
        )


def test_serve_openai_fails_clearly_without_credential(
    tmp_path: Path,
    fake_backend: FakeCredentialBackend,
    capsys,
) -> None:
    runner = CapturingRunner()

    assert main(
        ["serve", "--provider", "openai", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 2

    assert fake_backend.reads == ["openai"]
    assert runner.calls == []
    assert "credential" in capsys.readouterr().err.lower()


def test_serve_newapi_fails_clearly_without_credential(
    tmp_path: Path,
    fake_backend: FakeCredentialBackend,
    capsys,
) -> None:
    runner = CapturingRunner()

    assert main(
        ["serve", "--provider", "newapi", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 2

    assert fake_backend.reads == ["newapi"]
    assert runner.calls == []
    assert (
        "forgeloop credentials set newapi" in capsys.readouterr().err
    )


def test_default_backend_reads_openai_credential_from_secret_file_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    secret_file = tmp_path / "openai-key"
    secret_file.write_text("ordinary-provider-credential\n", encoding="utf-8")
    secret_file.chmod(0o600)
    monkeypatch.setenv("FORGELOOP_SECRET_FILE", str(secret_file))

    assert main(["credentials", "status", "openai"]) == 0

    output = capsys.readouterr().out
    assert "configured" in output
    assert "secret_file" in output
    assert "ordinary-provider-credential" not in output


def test_default_backend_binds_secret_file_to_requested_newapi_provider(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    secret_file = tmp_path / "newapi-key"
    secret_file.write_text("ordinary-provider-credential\n", encoding="utf-8")
    secret_file.chmod(0o600)
    monkeypatch.setenv("FORGELOOP_SECRET_FILE", str(secret_file))

    assert main(["credentials", "status", "newapi"]) == 0

    output = capsys.readouterr().out
    assert "newapi: configured" in output
    assert "secret_file" in output
    assert "ordinary-provider-credential" not in output


def test_serve_redacts_credential_backend_error(
    tmp_path: Path, capsys
) -> None:
    fake_token = "sk-unmistakably-fake-token"
    backend = ExplodingCredentialBackend(f"keyring failed for {fake_token}")

    assert main(
        ["serve", "--provider", "openai", "--data-dir", str(tmp_path)],
        backend=backend,
        uvicorn_runner=CapturingRunner(),
    ) == 2

    error = capsys.readouterr().err
    assert fake_token not in error
    assert "[REDACTED]" in error


def test_serve_openai_uses_injected_opener_without_network(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()
    fake_backend.values["openai"] = "sk-unmistakably-fake-provider"
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            action = json.dumps(
                {
                    "kind": "recall",
                    "arguments": {"tags": ["demo"], "limit": 10},
                }
            )
            return json.dumps(
                {"choices": [{"message": {"content": action}}]}
            ).encode()

    def opener(request, *, timeout: int):
        requests.append((request, timeout))
        return Response()

    assert main(
        ["serve", "--provider", "openai", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
        opener=opener,
    ) == 0

    app, _options = runner.calls[0]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/tasks",
            json={
                "description": "recall demo memory",
                "workspace": str(workspace),
                "provider": "openai",
            },
        )
        assert created.status_code == 201
        advanced = client.post(f"/api/tasks/{created.json()['id']}/advance")
        assert advanced.status_code == 200

    assert len(requests) == 1
    assert fake_backend.reads == ["openai"]


def test_serve_newapi_uses_its_credential_and_json_object_mode(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()
    fake_backend.values["newapi"] = "sk-unmistakably-fake-newapi"
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            action = json.dumps(
                {
                    "kind": "recall",
                    "arguments": {"tags": ["demo"], "limit": 10},
                }
            )
            return json.dumps(
                {"choices": [{"message": {"content": action}}]}
            ).encode()

    def opener(request, *, timeout: int):
        requests.append((request, timeout))
        return Response()

    assert main(
        ["serve", "--provider", "newapi", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
        opener=opener,
    ) == 0

    app, _options = runner.calls[0]
    workspace = tmp_path / "newapi-workspace"
    workspace.mkdir()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/tasks",
            json={
                "description": "recall demo memory",
                "workspace": str(workspace),
                "provider": "newapi",
            },
        )
        assert created.status_code == 201
        advanced = client.post(f"/api/tasks/{created.json()['id']}/advance")
        assert advanced.status_code == 200

    assert fake_backend.reads == ["newapi"]
    assert len(requests) == 1
    body = json.loads(requests[0][0].data)
    assert body["response_format"] == {"type": "json_object"}


def test_newapi_composition_executes_a_complete_governed_task(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()
    config_path = tmp_path / "newapi-flow.toml"
    config_path.write_text(
        "[[validators]]\n"
        'argv = ["python3", "-c", '
        '"from pathlib import Path; '
        "assert Path('note.txt').read_text().startswith('new')\"]\n"
        "timeout_seconds = 10\n",
        encoding="utf-8",
    )
    api_key = "sk-unmistakably-fake-newapi-flow"
    fake_backend.values["newapi"] = api_key
    actions = [
        {"kind": "read_file", "arguments": {"path": "note.txt"}},
        {
            "kind": "replace_text",
            "arguments": {
                "path": "note.txt",
                "old": "old\n",
                "new": "new\n",
                "count": 1,
            },
        },
        {"kind": "run_validation", "arguments": {}},
        {"kind": "finish", "arguments": {"summary": "updated note"}},
    ]
    requests = []

    class Response:
        def __init__(self, action_payload):
            self.action_payload = action_payload

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(self.action_payload)
                            }
                        }
                    ]
                }
            ).encode()

    def opener(request, *, timeout: int):
        requests.append((request, timeout))
        return Response(actions.pop(0))

    assert main(
        [
            "serve",
            "--provider",
            "newapi",
            "--config",
            str(config_path),
            "--data-dir",
            str(tmp_path),
        ],
        backend=fake_backend,
        uvicorn_runner=runner,
        opener=opener,
    ) == 0

    app, _options = runner.calls[0]
    workspace = tmp_path / "newapi-complete-workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/tasks",
            json={
                "description": "replace old with new and validate",
                "workspace": str(workspace),
                "provider": "newapi",
            },
        )
        task_id = created.json()["id"]
        for _step in range(4):
            advanced = client.post(f"/api/tasks/{task_id}/advance")
            assert advanced.status_code == 200
        detail = client.get(f"/api/tasks/{task_id}").json()

    assert advanced.json()["status"] == "succeeded"
    assert target.read_text(encoding="utf-8") == "new\n"
    assert actions == []
    assert len(requests) == 4
    assert all(
        request.get_header("Authorization") == f"Bearer {api_key}"
        for request, _timeout in requests
    )
    assert all(
        json.loads(request.data)["response_format"] == {"type": "json_object"}
        for request, _timeout in requests
    )
    event_kinds = {event["kind"] for event in detail["events"]}
    assert {"action", "governance_decision", "tool_result", "validation"} <= (
        event_kinds
    )


def test_serve_redacts_injected_opener_error(
    tmp_path: Path, fake_backend: FakeCredentialBackend, capsys
) -> None:
    api_key = "ordinary-provider-credential"
    fake_token = "sk-unmistakably-fake-token"
    fake_backend.values["openai"] = api_key
    observed: dict[str, object] = {}

    def opener(_request, *, timeout: int):
        raise RuntimeError(
            f"opener timeout {timeout}: {api_key}: {fake_token}"
        )

    def runner(app, **_kwargs: object) -> None:
        workspace = tmp_path / "opener-failure-workspace"
        workspace.mkdir()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            created = client.post(
                "/api/tasks",
                json={
                    "description": "exercise provider error redaction",
                    "workspace": str(workspace),
                    "provider": "openai",
                },
            )
            assert created.status_code == 201
            advanced = client.post(
                f"/api/tasks/{created.json()['id']}/advance"
            )
            assert advanced.status_code == 200
            observed["advanced"] = advanced.json()
            observed["detail"] = client.get(
                f"/api/tasks/{created.json()['id']}"
            ).json()

    assert main(
        ["serve", "--provider", "openai", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
        opener=opener,
    ) == 0

    advanced_payload = observed["advanced"]
    assert isinstance(advanced_payload, dict)
    assert advanced_payload["status"] == "running"
    detail_payload = observed["detail"]
    assert isinstance(detail_payload, dict)
    assert any(
        event["data"].get("reason") == "provider_failure"
        for event in detail_payload["events"]
    )
    rendered_observations = json.dumps(observed)
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + rendered_observations
    assert api_key not in all_output
    assert fake_token not in all_output


def test_serve_redacts_runner_error_after_reading_api_key(
    tmp_path: Path, fake_backend: FakeCredentialBackend, capsys
) -> None:
    api_key = "ordinary-provider-credential"
    fake_token = "sk-unmistakably-fake-token"
    fake_backend.values["openai"] = api_key

    def runner(_app, **_kwargs: object) -> None:
        raise RuntimeError(f"runner failed: {api_key}: {fake_token}")

    assert main(
        ["serve", "--provider", "openai", "--data-dir", str(tmp_path)],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 2

    error = capsys.readouterr().err
    assert api_key not in error
    assert fake_token not in error
    assert "[REDACTED]" in error


def test_serve_rejects_non_loopback_host_without_explicit_opt_in(
    tmp_path: Path, fake_backend: FakeCredentialBackend, capsys
) -> None:
    runner = CapturingRunner()

    assert main(
        [
            "serve",
            "--provider",
            "demo",
            "--host",
            "0.0.0.0",
            "--data-dir",
            str(tmp_path),
        ],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 2

    assert runner.calls == []
    assert fake_backend.reads == []
    assert "--allow-remote" in capsys.readouterr().err


@pytest.mark.parametrize("wildcard_host", ["0.0.0.0", "::"])
def test_serve_rejects_wildcard_bind_without_concrete_allowed_host(
    wildcard_host: str,
    tmp_path: Path,
    fake_backend: FakeCredentialBackend,
    capsys,
) -> None:
    runner = CapturingRunner()

    assert main(
        [
            "serve",
            "--provider",
            "demo",
            "--host",
            wildcard_host,
            "--allow-remote",
            "--data-dir",
            str(tmp_path),
        ],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 2

    assert runner.calls == []
    assert "--allowed-host" in capsys.readouterr().err


def test_serve_wildcard_bind_trusts_only_explicit_allowed_hosts(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()

    assert main(
        [
            "serve",
            "--provider",
            "demo",
            "--host",
            "0.0.0.0",
            "--allow-remote",
            "--allowed-host",
            "example.test",
            "--allowed-host",
            "admin.example.test",
            "--data-dir",
            str(tmp_path),
        ],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 0

    app, options = runner.calls[0]
    assert options["host"] == "0.0.0.0"
    for trusted_host in ("example.test", "admin.example.test"):
        with TestClient(app, base_url=f"http://{trusted_host}") as client:
            assert client.get("/healthz").status_code == 200
    with TestClient(app, base_url="http://0.0.0.0") as client:
        assert client.get("/healthz").status_code == 403


def test_serve_adds_remote_bind_name_to_allowed_hosts(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()

    assert main(
        [
            "serve",
            "--provider",
            "demo",
            "--host",
            "deploy.internal",
            "--allow-remote",
            "--data-dir",
            str(tmp_path),
        ],
        backend=fake_backend,
        uvicorn_runner=runner,
    ) == 0

    app, options = runner.calls[0]
    assert options["host"] == "deploy.internal"
    with TestClient(app, base_url="http://deploy.internal") as client:
        assert client.get("/healthz").status_code == 200
