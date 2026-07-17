import re
import tomllib
from pathlib import Path

from forgeloop.config import HarnessConfig, load_config


ROOT = Path(__file__).resolve().parents[1]


def test_example_configuration_loads() -> None:
    config = load_config(ROOT / "forgeloop.example.toml")

    assert isinstance(config, HarnessConfig)
    assert config.validators


def test_package_metadata_exposes_cli_and_build_tool() -> None:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert project["project"]["scripts"]["forgeloop"] == "forgeloop.cli:main"
    assert "build>=1.2,<2" in project["project"]["optional-dependencies"]["dev"]


def test_gitlab_ci_defines_top_level_unit_test_job() -> None:
    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^unit-test:\s*$", pipeline)
    assert "python3 -m pytest -q" in pipeline


def test_dockerfile_runs_as_unprivileged_user() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"(?m)^USER\s+forgeloop\s*$", dockerfile)


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
