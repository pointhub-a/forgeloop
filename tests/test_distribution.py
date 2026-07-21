import json
import re
import tomllib
from pathlib import Path

from forgeloop.config import HarnessConfig, load_config


ROOT = Path(__file__).resolve().parents[1]


def test_example_configuration_loads() -> None:
    config = load_config(ROOT / "forgeloop.example.toml")

    assert isinstance(config, HarnessConfig)
    assert config.validators


def test_njusehub_example_is_safe_and_local_override_is_ignored() -> None:
    with (ROOT / "njusehub.example.toml").open("rb") as example_file:
        example = tomllib.load(example_file)

    assert example["provider_base_url"] == (
        "https://njusehub.info/v1/chat/completions"
    )
    assert example["provider_model"] == "qwen-turbo"
    assert example["provider_timeout_seconds"] == 20
    assert "api_key" not in json.dumps(example).lower()
    assert "njusehub.toml" in (
        ROOT / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()
    assert load_config(ROOT / "njusehub.example.toml").validators


def test_package_metadata_exposes_cli_and_build_tool() -> None:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert project["project"]["scripts"]["forgeloop"] == "forgeloop.cli:main"
    assert "build>=1.2,<2" in project["project"]["optional-dependencies"]["dev"]


def test_gitlab_ci_defines_top_level_unit_test_job() -> None:
    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^unit-test:\s*$", pipeline)
    assert "python3 -m pytest -q" in pipeline


def test_dockerfile_has_complete_runtime_security_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(
        r"(?m)^FROM\s+python:3\.12-slim\s+AS\s+builder\s*$", dockerfile
    )
    assert len(re.findall(r"(?m)^FROM\s+python:3\.12-slim\b", dockerfile)) == 2
    assert "python3 -m pip install /wheels/*.whl" in dockerfile
    assert re.search(r"(?m)^USER\s+forgeloop\s*$", dockerfile)
    assert 'VOLUME ["/workspace", "/data", "/run/secrets"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/healthz" in dockerfile

    command_match = re.search(r"(?m)^CMD\s+(\[.*\])\s*$", dockerfile)
    assert command_match is not None
    command = json.loads(command_match.group(1))
    assert "--allow-remote" in command
    allowed_hosts = {
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument == "--allowed-host"
    }
    assert {"localhost", "127.0.0.1"} <= allowed_hosts


def test_readme_has_required_course_sections() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for heading in (
        "项目简介",
        "安装与运行",
        "分发",
        "目录结构",
        "凭据安全",
        "安全边界",
        "已知限制",
    ):
        assert re.search(rf"(?m)^## {re.escape(heading)}\s*$", readme)


def test_readme_uses_cross_shell_hidden_secret_file_setup() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "read -rsp" not in readme
    assert "getpass.getpass" in readme
    assert "os.open" in readme
    assert "0o600" in readme
    assert "os.fchmod" in readme
