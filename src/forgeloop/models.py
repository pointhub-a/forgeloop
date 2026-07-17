"""Strict domain models shared by the ForgeLoop harness."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ActionKind(str, Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    REPLACE_TEXT = "replace_text"
    RUN_COMMAND = "run_command"
    RUN_VALIDATION = "run_validation"
    REMEMBER = "remember"
    RECALL = "recall"
    FINISH = "finish"


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_PROGRESS = "no_progress"
    CANCELLED = "cancelled"


class ValidationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INFRA_ERROR = "infra_error"


class FailureClass(str, Enum):
    SYNTAX = "syntax"
    TEST_FAILURE = "test_failure"
    LINT = "lint"
    TYPE_ERROR = "type_error"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


class DecisionEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class EventKind(str, Enum):
    MODEL_REQUEST = "model_request"
    ACTION = "action"
    GOVERNANCE_DECISION = "governance_decision"
    TOOL_RESULT = "tool_result"
    VALIDATION = "validation"
    STATE = "state"
    APPROVAL = "approval"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


PathArgument = Annotated[
    str, Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")
]
TextArgument = Annotated[str, Field(max_length=16 * 1024 * 1024)]
NonEmptyTextArgument = Annotated[
    str, Field(min_length=1, max_length=16 * 1024 * 1024)
]
CommandArgument = Annotated[
    str, Field(min_length=1, max_length=32768, pattern=r"^[^\x00]+$")
]
TagArgument = Annotated[str, Field(min_length=1, max_length=128)]


class ReadFileArguments(StrictArguments):
    path: PathArgument


class WriteFileArguments(StrictArguments):
    path: PathArgument
    content: TextArgument


class ReplaceTextArguments(StrictArguments):
    path: PathArgument
    old: NonEmptyTextArgument
    new: TextArgument
    count: int = Field(ge=1, le=1_000_000)


class RunCommandArguments(StrictArguments):
    argv: Annotated[list[CommandArgument], Field(min_length=1, max_length=256)]
    timeout_seconds: int = Field(ge=1, le=120)


class RunValidationArguments(StrictArguments):
    pass


class RememberArguments(StrictArguments):
    key: Annotated[str, Field(min_length=1, max_length=256)]
    value: TextArgument
    tags: Annotated[list[TagArgument], Field(min_length=1, max_length=64)]


class RecallArguments(StrictArguments):
    tags: Annotated[list[TagArgument], Field(min_length=1, max_length=64)]
    limit: int = Field(ge=1, le=1000)


class FinishArguments(StrictArguments):
    summary: Annotated[str, Field(min_length=1, max_length=32768)]


_ACTION_ARGUMENT_MODELS: dict[ActionKind, type[StrictArguments]] = {
    ActionKind.READ_FILE: ReadFileArguments,
    ActionKind.WRITE_FILE: WriteFileArguments,
    ActionKind.REPLACE_TEXT: ReplaceTextArguments,
    ActionKind.RUN_COMMAND: RunCommandArguments,
    ActionKind.RUN_VALIDATION: RunValidationArguments,
    ActionKind.REMEMBER: RememberArguments,
    ActionKind.RECALL: RecallArguments,
    ActionKind.FINISH: FinishArguments,
}


def _validate_json_object(value: dict[str, object]) -> dict[str, object]:
    def require_string_keys(item: object) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for nested in item.values():
                require_string_keys(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                require_string_keys(nested)

    require_string_keys(value)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-compatible") from exc
    return value


class Action(StrictModel):
    kind: ActionKind
    arguments: dict[str, object]

    _json_arguments = field_validator("arguments")(_validate_json_object)

    @model_validator(mode="after")
    def arguments_match_kind(self) -> Action:
        _ACTION_ARGUMENT_MODELS[self.kind].model_validate(self.arguments)
        return self

    @classmethod
    def model_json_schema(cls, *args, **kwargs) -> dict[str, object]:
        """Advertise the exact kind-discriminated action surface to providers."""

        branches = []
        for kind, arguments_model in _ACTION_ARGUMENT_MODELS.items():
            branches.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "arguments"],
                    "properties": {
                        "kind": {"type": "string", "const": kind.value},
                        "arguments": arguments_model.model_json_schema(),
                    },
                }
            )
        return {
            "title": "ForgeLoopAction",
            "oneOf": branches,
            "discriminator": {"propertyName": "kind"},
        }


class ToolResult(StrictModel):
    ok: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_json_compatible(
        cls, value: dict[str, object]
    ) -> dict[str, object]:
        return _validate_json_object(value)

    @classmethod
    def success(
        cls, output: str = "", *, metadata: dict[str, object] | None = None
    ) -> ToolResult:
        return cls(ok=True, output=output, error=None, metadata=metadata or {})


def _tool_result_error(
    cls: type[ToolResult],
    message: str,
    *,
    output: str = "",
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    return cls(ok=False, output=output, error=message, metadata=metadata or {})


# The public constructor and the serialized field intentionally share the name
# ``error``. A non-data classmethod descriptor lets instances retain field access.
setattr(ToolResult, "error", classmethod(_tool_result_error))


class ValidationReport(StrictModel):
    argv: list[str]
    status: ValidationStatus
    classification: FailureClass | None = None
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    stdout: str = ""
    stderr: str = ""
    fingerprint: str = ""

    @classmethod
    def passed(
        cls,
        argv: list[str],
        duration_ms: int,
        stdout: str = "",
        stderr: str = "",
        *,
        fingerprint: str = "",
    ) -> ValidationReport:
        return cls(
            argv=argv,
            status=ValidationStatus.PASSED,
            classification=None,
            exit_code=0,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            fingerprint=fingerprint,
        )

    @classmethod
    def failed(
        cls,
        argv: list[str],
        duration_ms: int,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        *,
        classification: FailureClass = FailureClass.UNKNOWN,
        fingerprint: str = "",
    ) -> ValidationReport:
        return cls(
            argv=argv,
            status=ValidationStatus.FAILED,
            classification=classification,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            fingerprint=fingerprint,
        )


class GovernanceDecision(StrictModel):
    effect: DecisionEffect
    rule_id: str
    reason: str
    fingerprint: str


class TaskRecord(StrictModel):
    id: str
    description: str
    workspace: str
    status: TaskStatus
    step_count: int = Field(ge=0)
    last_validation_passed: bool = False
    pending_action: Action | None = None
    created_at: datetime
    updated_at: datetime


class Event(StrictModel):
    id: str
    task_id: str
    sequence: int = Field(ge=1)
    kind: EventKind
    summary: str
    data: dict[str, object] = Field(default_factory=dict)
    created_at: datetime

    _json_data = field_validator("data")(_validate_json_object)
