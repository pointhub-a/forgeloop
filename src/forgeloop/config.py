"""TOML configuration schema for ForgeLoop."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


PositiveInt = Annotated[int, Field(gt=0)]
TimeoutSeconds = Annotated[int, Field(ge=1, le=120)]
RepeatLimit = Annotated[int, Field(gt=1)]
NonEmptyString = Annotated[str, Field(min_length=1)]


class ConfigError(ValueError):
    """Raised when a configuration file cannot be parsed or validated."""


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, validate_assignment=True, validate_default=True
    )


class ValidatorConfig(ConfigModel):
    argv: list[str]
    timeout_seconds: TimeoutSeconds = 60


class HarnessConfig(ConfigModel):
    max_steps: PositiveInt = 20
    max_validation_runs: PositiveInt = 8
    wall_time_seconds: PositiveInt = 900
    command_timeout_seconds: TimeoutSeconds = 60
    provider_timeout_seconds: TimeoutSeconds = 60
    max_output_bytes: PositiveInt = 32768
    max_file_bytes: PositiveInt = 1048576
    max_identical_failures: RepeatLimit = 2
    max_identical_actions: RepeatLimit = 3
    memory_recall_limit: PositiveInt = 10
    memory_char_budget: PositiveInt = 4096
    allowed_executables: Annotated[
        list[NonEmptyString], Field(min_length=1)
    ] = Field(default_factory=lambda: ["python3", "pytest", "ruff", "mypy", "git"])
    approval_rule_ids: list[NonEmptyString] = Field(
        default_factory=lambda: [
            "command.recursive_delete",
            "git.force_push",
            "git.hard_reset",
            "database.drop",
            "privilege.escalation",
            "permission.change",
            "network.execute",
        ]
    )
    validators: list[ValidatorConfig] = Field(default_factory=list)
    provider_base_url: str = "https://api.openai.com/v1/chat/completions"
    provider_model: NonEmptyString = "gpt-4.1-mini"

    @field_validator("provider_base_url")
    @classmethod
    def provider_url_must_use_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("provider_base_url must be an HTTPS URL")
        return value


def _format_validation_error(exc: ValidationError) -> ConfigError:
    details = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
    return ConfigError("Invalid configuration at " + "; ".join(details))


def load_config(path: Path) -> HarnessConfig:
    """Load and strictly validate a ForgeLoop TOML configuration file."""

    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration {path}: {exc}") from exc

    try:
        return HarnessConfig.model_validate(document)
    except ValidationError as exc:
        raise _format_validation_error(exc) from exc
