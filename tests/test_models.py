from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from forgeloop.models import (
    Action,
    ActionKind,
    DecisionEffect,
    Event,
    EventKind,
    FailureClass,
    GovernanceDecision,
    TaskRecord,
    TaskStatus,
    ToolResult,
    ValidationReport,
    ValidationStatus,
)


def test_action_rejects_unknown_kind():
    with pytest.raises(ValueError):
        Action.model_validate({"kind": "launch_missile", "arguments": {}})


def test_action_kinds_cover_supported_tool_surface():
    assert {kind.value for kind in ActionKind} == {
        "read_file",
        "write_file",
        "replace_text",
        "run_command",
        "run_validation",
        "remember",
        "recall",
        "finish",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": b"not-json"},
        {"tags": {"not", "json"}},
        {"nested": {1: "non-string key"}},
    ],
)
def test_action_rejects_non_json_arguments(arguments):
    with pytest.raises(ValidationError):
        Action(kind="write_file", arguments=arguments)


def test_action_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Action.model_validate(
            {"kind": "read_file", "arguments": {"path": "README.md"}, "extra": True}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "read_file", "arguments": {"path": "README.md"}},
        {
            "kind": "write_file",
            "arguments": {"path": "note.txt", "content": "hello"},
        },
        {
            "kind": "replace_text",
            "arguments": {
                "path": "note.txt",
                "old": "hello",
                "new": "hi",
                "count": 1,
            },
        },
        {
            "kind": "run_command",
            "arguments": {"argv": ["pytest", "-q"], "timeout_seconds": 60},
        },
        {"kind": "run_validation", "arguments": {}},
        {
            "kind": "remember",
            "arguments": {"key": "style", "value": "ruff", "tags": ["python"]},
        },
        {"kind": "recall", "arguments": {"tags": ["python"], "limit": 5}},
        {"kind": "finish", "arguments": {"summary": "complete"}},
    ],
)
def test_action_accepts_each_exact_argument_shape(payload):
    assert Action.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "read_file",
            "arguments": {"path": "README.md", "content": "extra"},
        },
        {"kind": "read_file", "arguments": {"path": "bad\x00path"}},
        {"kind": "write_file", "arguments": {"path": "note.txt"}},
        {
            "kind": "replace_text",
            "arguments": {"path": "a", "old": "x", "new": "y", "count": 0},
        },
        {
            "kind": "run_command",
            "arguments": {"argv": [], "timeout_seconds": 60},
        },
        {
            "kind": "run_command",
            "arguments": {"argv": ["pytest"], "timeout_seconds": 121},
        },
        {
            "kind": "run_command",
            "arguments": {"argv": ["bad\x00command"], "timeout_seconds": 60},
        },
        {"kind": "run_validation", "arguments": {"unexpected": True}},
        {"kind": "remember", "arguments": {"key": "k", "value": "v", "tags": []}},
        {"kind": "recall", "arguments": {"tags": ["python"]}},
        {"kind": "recall", "arguments": {"tags": ["python"], "limit": 0}},
        {"kind": "finish", "arguments": {"summary": ""}},
    ],
)
def test_action_rejects_unknown_missing_or_out_of_range_arguments(payload):
    with pytest.raises(ValidationError):
        Action.model_validate(payload)


def test_action_json_schema_is_a_strict_discriminated_union():
    schema = Action.model_json_schema()

    assert len(schema["oneOf"]) == len(ActionKind)
    assert {branch["properties"]["kind"]["const"] for branch in schema["oneOf"]} == {
        kind.value for kind in ActionKind
    }
    assert all(branch["additionalProperties"] is False for branch in schema["oneOf"])
    assert all(
        branch["properties"]["arguments"]["additionalProperties"] is False
        for branch in schema["oneOf"]
    )


def test_tool_result_constructors_create_structured_outcomes():
    success = ToolResult.success("done", metadata={"path": "a.py"})
    failure = ToolResult.error("missing file", metadata={"path": "a.py"})

    assert success == ToolResult(
        ok=True, output="done", error=None, metadata={"path": "a.py"}
    )
    assert failure == ToolResult(
        ok=False, output="", error="missing file", metadata={"path": "a.py"}
    )


def test_validation_report_is_serializable():
    report = ValidationReport.passed(["pytest"], 12, "1 passed")
    assert report.status is ValidationStatus.PASSED
    assert report.model_dump(mode="json")["exit_code"] == 0


def test_failed_validation_report_keeps_failure_evidence():
    report = ValidationReport.failed(
        ["pytest", "-q"],
        19,
        1,
        "1 failed",
        "traceback",
        classification=FailureClass.TEST_FAILURE,
        fingerprint="sha256:test",
    )

    assert report.status is ValidationStatus.FAILED
    assert report.classification is FailureClass.TEST_FAILURE
    assert report.exit_code == 1
    assert report.stderr == "traceback"


def test_governance_decision_uses_declared_effects():
    decision = GovernanceDecision(
        effect="allow",
        rule_id="default.allow",
        reason="safe action",
        fingerprint="sha256:action",
    )

    assert decision.effect is DecisionEffect.ALLOW


def test_task_record_and_event_use_strict_state_enums():
    now = datetime.now(timezone.utc)
    task = TaskRecord(
        id="task-1",
        description="fix bug",
        workspace="/tmp/work",
        status="created",
        step_count=0,
        last_validation_passed=False,
        pending_action=None,
        created_at=now,
        updated_at=now,
    )
    event = Event(
        id="event-1",
        task_id=task.id,
        sequence=1,
        kind="state",
        summary="created",
        data={},
        created_at=now,
    )

    assert task.status is TaskStatus.CREATED
    assert event.kind is EventKind.STATE


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ToolResult,
            {"ok": True, "output": "", "error": None, "metadata": {}},
        ),
        (
            GovernanceDecision,
            {
                "effect": "allow",
                "rule_id": "default.allow",
                "reason": "safe action",
                "fingerprint": "sha256:action",
            },
        ),
    ],
)
def test_domain_models_reject_unknown_fields(model, payload):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({**payload, "mystery": True})
