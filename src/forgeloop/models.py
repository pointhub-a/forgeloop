"""Strict domain models shared by the ForgeLoop harness."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
