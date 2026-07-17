"""Command-line composition root for ForgeLoop."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import getpass
import ipaddress
import json
from pathlib import Path
import secrets
import sys
import tempfile

import uvicorn

from forgeloop.config import ConfigError, HarnessConfig, load_config
from forgeloop.credentials import CredentialBackend, CredentialService, KeyringBackend
from forgeloop.demo import run_mechanism_demo
from forgeloop.feedback import ProgressTracker, ValidatorRunner
from forgeloop.loop import AgentLoop
from forgeloop.memory import MemoryStore
from forgeloop.policy import PolicyEngine
from forgeloop.providers import OpenAICompatibleProvider, ScriptedProvider
from forgeloop.repository import ApprovalRepository, TaskRepository
from forgeloop.service import TaskService
from forgeloop.tools import ToolRuntime
from forgeloop.web import AppDependencies, create_app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgeloop")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run the deterministic offline demo")
    demo.add_argument("--json", action="store_true", dest="as_json")

    serve = commands.add_parser("serve", help="run the local Web application")
    serve.add_argument("--provider", choices=("demo", "openai"), default="demo")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--data-dir", type=Path, default=Path.home() / ".forgeloop"
    )
    serve.add_argument("--config", type=Path)
    serve.add_argument("--allow-remote", action="store_true")

    credentials = commands.add_parser("credentials", help="manage credentials")
    credential_commands = credentials.add_subparsers(
        dest="credential_command", required=True
    )
    for command in ("status", "set", "clear"):
        credential = credential_commands.add_parser(command)
        credential.add_argument("provider")

    return parser


def _run_demo(*, as_json: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="forgeloop-cli-") as temporary:
        result = run_mechanism_demo(Path(temporary))
    if as_json:
        print(result.model_dump_json(indent=2))
    else:
        print(
            "ForgeLoop mechanism demo: "
            f"final={result.final_status.value}, "
            f"no_progress={result.no_progress_status.value}"
        )
    return 0


def _run_credentials(
    args: argparse.Namespace, backend: CredentialBackend | None
) -> int:
    service = CredentialService(backend if backend is not None else KeyringBackend())
    provider = args.provider
    if args.credential_command == "set":
        service.set(provider, getpass.getpass(f"Credential for {provider}: "))
        print(f"Credential for {provider} stored.")
    elif args.credential_command == "clear":
        service.clear(provider)
        print(f"Credential for {provider} cleared.")
    else:
        status = service.status(provider)
        configured = "configured" if status.configured else "not configured"
        print(f"{provider}: {configured} ({status.source})")
    return 0


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _loop_factory(
    *,
    provider_name: str,
    config: HarnessConfig,
    memory_database: Path,
    api_key: str | None,
    opener: Callable[..., object] | None,
):
    demo_response = json.dumps(
        {"kind": "recall", "arguments": {"tags": ["demo"]}},
        separators=(",", ":"),
        sort_keys=True,
    )

    def factory(workspace: Path, task_id: str) -> AgentLoop:
        runtime = ToolRuntime(workspace, config)
        if provider_name == "openai":
            if api_key is None:
                raise RuntimeError("OpenAI provider credential is unavailable")
            provider_options: dict[str, object] = {
                "base_url": config.provider_base_url,
                "model": config.provider_model,
                "api_key": api_key,
                "timeout_seconds": config.provider_timeout_seconds,
            }
            if opener is not None:
                provider_options["opener"] = opener
            provider = OpenAICompatibleProvider(**provider_options)
        else:
            provider = ScriptedProvider([demo_response] * 3)
        return AgentLoop(
            provider=provider,
            policy=PolicyEngine(config),
            tools=runtime,
            validators=ValidatorRunner(runtime, config.validators),
            progress=ProgressTracker(
                max_identical_failures=config.max_identical_failures,
                max_identical_actions=config.max_identical_actions,
            ),
            memory=MemoryStore(memory_database),
            config=config,
            project_id=task_id,
        )

    return factory


def _run_serve(
    args: argparse.Namespace,
    *,
    backend: CredentialBackend | None,
    uvicorn_runner: Callable[..., object] | None,
    opener: Callable[..., object] | None,
) -> int:
    if not _is_loopback(args.host) and not args.allow_remote:
        print(
            "Refusing a non-loopback bind without --allow-remote.",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(args.config) if args.config is not None else HarnessConfig()
        data_dir = args.data_dir.expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        credential_service = CredentialService(
            backend if backend is not None else KeyringBackend()
        )
        api_key = None
        if args.provider == "openai":
            api_key = credential_service.get_for_provider("openai")
            if api_key is None:
                print(
                    "OpenAI credential is not configured; use "
                    "'forgeloop credentials set openai'.",
                    file=sys.stderr,
                )
                return 2

        database = data_dir / "forgeloop.sqlite3"
        repository = TaskRepository(database)
        approvals = ApprovalRepository(database)
        task_service = TaskService(
            repository,
            approvals,
            _loop_factory(
                provider_name=args.provider,
                config=config,
                memory_database=data_dir / "memory.sqlite3",
                api_key=api_key,
                opener=opener,
            ),
        )
        dependencies = AppDependencies(
            task_service=task_service,
            task_repository=repository,
            credential_service=credential_service,
            csrf_secret=secrets.token_bytes(32),
            demo_runner=lambda: run_mechanism_demo(
                data_dir / "demo"
            ).model_dump(mode="json"),
            provider_name=args.provider,
            allowed_hosts=frozenset(
                {"localhost", "127.0.0.1", "::1", "testserver", args.host.lower()}
            ),
        )
        app = create_app(dependencies)
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to compose ForgeLoop: {exc}", file=sys.stderr)
        return 2

    runner = uvicorn_runner if uvicorn_runner is not None else uvicorn.run
    runner(app, host=args.host, port=args.port)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: CredentialBackend | None = None,
    uvicorn_runner: Callable[..., object] | None = None,
    opener: Callable[..., object] | None = None,
) -> int:
    """Parse arguments and run one ForgeLoop command."""

    args = _parser().parse_args(argv)
    if args.command == "demo":
        return _run_demo(as_json=args.as_json)
    if args.command == "credentials":
        return _run_credentials(args, backend)
    if args.command == "serve":
        return _run_serve(
            args,
            backend=backend,
            uvicorn_runner=uvicorn_runner,
            opener=opener,
        )
    raise RuntimeError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())
