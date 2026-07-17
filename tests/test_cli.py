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
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "sk-input-secret")

    assert main(
        ["credentials", "set", "openai"], backend=fake_backend
    ) == 0

    assert fake_backend.values == {"openai": "sk-input-secret"}
    assert "sk-input-secret" not in capsys.readouterr().out


def test_credentials_status_and_clear_never_reveal_secret(
    fake_backend: FakeCredentialBackend, capsys
) -> None:
    fake_backend.values["openai"] = "sk-never-print-this"

    assert main(
        ["credentials", "status", "openai"], backend=fake_backend
    ) == 0
    status_output = capsys.readouterr().out
    assert "configured" in status_output
    assert "memory" in status_output
    assert "sk-never-print-this" not in status_output

    assert main(
        ["credentials", "clear", "openai"], backend=fake_backend
    ) == 0
    clear_output = capsys.readouterr().out
    assert "cleared" in clear_output
    assert "sk-never-print-this" not in clear_output
    assert fake_backend.values == {}


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


def test_serve_openai_uses_injected_opener_without_network(
    tmp_path: Path, fake_backend: FakeCredentialBackend
) -> None:
    runner = CapturingRunner()
    fake_backend.values["openai"] = "sk-provider-secret"
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            action = json.dumps(
                {"kind": "recall", "arguments": {"tags": ["demo"]}}
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
