import json
import sqlite3

import pytest

from forgeloop.config import HarnessConfig
from forgeloop.feedback import ProgressTracker
from forgeloop.loop import AgentLoop, ApprovalMismatch
from forgeloop.memory import MemoryStore
from forgeloop.models import FailureClass, ValidationReport
from forgeloop.policy import PolicyEngine
from forgeloop.providers import ProviderError, ScriptedProvider
from forgeloop.tools import ToolRuntime


def action(kind, **arguments):
    if kind == "run_command":
        arguments.setdefault("timeout_seconds", 60)
    if kind == "recall":
        arguments.setdefault("limit", 10)
    return json.dumps({"kind": kind, "arguments": arguments})


def failed(message):
    return ValidationReport.failed(
        ["pytest"],
        duration_ms=1,
        exit_code=1,
        stderr=message,
        classification=FailureClass.TEST_FAILURE,
    )


def passed():
    return ValidationReport.passed(["pytest"], duration_ms=1)


class ScriptedValidators:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def run_all(self):
        result = self.results[self.calls]
        self.calls += 1
        return result


def make_loop(
    tmp_path,
    provider,
    *,
    validation_results=(),
    max_steps=20,
    config=None,
    memory=None,
):
    config = config or HarnessConfig(max_steps=max_steps)
    tools = ToolRuntime(tmp_path, config)
    return AgentLoop(
        provider=provider,
        policy=PolicyEngine(config),
        tools=tools,
        validators=ScriptedValidators(validation_results),
        progress=ProgressTracker(
            max_identical_failures=config.max_identical_failures,
            max_identical_actions=config.max_identical_actions,
        ),
        memory=memory or MemoryStore(":memory:"),
        config=config,
        project_id="test-project",
    )


def test_failed_validation_is_fed_back_and_changes_next_action(tmp_path):
    (tmp_path / "calc.py").write_text("def value():\n    return 0\n")
    provider = ScriptedProvider(
        [
            action("run_validation"),
            action(
                "replace_text",
                path="calc.py",
                old="return 0",
                new="return 1",
                count=1,
            ),
            action("run_validation"),
            action("finish", summary="fixed"),
        ]
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[failed("assert 0 == 1")], [passed()]],
    ).run("fix calc")

    assert result.status == "succeeded"
    assert any(
        message["role"] == "feedback" for message in provider.calls[1][0]
    )
    assert provider.responses[0] != provider.responses[1]
    assert (tmp_path / "calc.py").read_text() == "def value():\n    return 1\n"


def test_dangerous_action_pauses_before_tool_execution(tmp_path):
    provider = ScriptedProvider(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )

    result = make_loop(tmp_path, provider).run("clean")

    assert result.status == "waiting_approval"
    assert result.tool_calls == []
    assert result.pending_decision is not None
    assert result.pending_action is not None


def test_policy_denial_is_audited_then_resumes_for_model_self_correction(tmp_path):
    provider = ScriptedProvider(
        [
            action(
                "run_command",
                argv=["unknown-tool"],
                timeout_seconds=60,
            ),
            action("run_validation"),
            action("finish", summary="corrected"),
        ]
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[passed()]],
        max_steps=3,
    ).run("recover from unsafe proposal")

    assert result.status == "succeeded"
    denied = [
        event
        for event in result.events
        if event.kind.value == "state" and event.data.get("status") == "denied"
    ]
    assert len(denied) == 1
    assert denied[0].data["rule_id"] == "command.executable_not_allowed"
    assert any(
        json.loads(message["content"]).get("type") == "policy_denial"
        for message in provider.calls[1][0]
        if message["role"] == "feedback"
    )


def test_finish_without_passing_validation_is_rejected(tmp_path):
    result = make_loop(
        tmp_path,
        ScriptedProvider([action("finish", summary="done")]),
        max_steps=1,
    ).run("fix")

    assert result.status != "succeeded"
    assert result.status == "budget_exhausted"


def test_step_performs_exactly_one_model_decision(tmp_path):
    provider = ScriptedProvider([action("finish", summary="too soon")])
    loop = make_loop(tmp_path, provider, max_steps=2)
    loop.start("fix")

    state = loop.step()

    assert len(provider.calls) == 1
    assert state.step_count == 1
    assert state.status == "running"


class FailsOnceProvider:
    def __init__(self, responses, failure_message):
        self.responses = list(responses)
        self.failure_message = failure_message
        self.calls = []

    def complete(self, messages, action_schema):
        self.calls.append((list(messages), action_schema))
        if len(self.calls) == 1:
            raise ProviderError(self.failure_message)
        return self.responses[len(self.calls) - 2]


def test_provider_failure_is_safe_feedback_and_counts_as_a_step(tmp_path):
    secret = "provider-secret-that-must-not-leak"
    provider = FailsOnceProvider(
        [action("run_validation"), action("finish", summary="fixed")],
        failure_message=secret,
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[passed()]],
        max_steps=3,
    ).run("fix")

    assert result.status == "succeeded"
    assert result.step_count == 3
    feedback = [
        message
        for message in provider.calls[1][0]
        if message["role"] == "feedback"
    ]
    assert feedback
    assert all(secret not in message["content"] for message in feedback)


def test_parse_failure_is_feedback_until_step_budget_is_exhausted(tmp_path):
    invalid = "not an action with sensitive-looking raw output"
    provider = ScriptedProvider([invalid, invalid])

    result = make_loop(tmp_path, provider, max_steps=2).run("fix")

    assert result.status == "budget_exhausted"
    assert result.step_count == 2
    assert len(provider.calls) == 2
    assert any(
        message["role"] == "feedback" for message in provider.calls[1][0]
    )
    assert invalid not in provider.calls[1][0][-1]["content"]


def test_invalid_action_arguments_are_parse_feedback_and_count_as_a_step(tmp_path):
    invalid = json.dumps(
        {
            "kind": "run_command",
            "arguments": {"argv": ["pytest"], "timeout_seconds": 0},
        }
    )
    provider = ScriptedProvider([invalid, action("run_validation")])

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[passed()]],
        max_steps=2,
    ).run("fix")

    assert result.step_count == 2
    assert result.validation_count == 1
    feedback = json.loads(provider.calls[1][0][-1]["content"])
    assert feedback["type"] == "action_parse_error"


def test_provider_exhaustion_retries_safely_until_step_budget(tmp_path):
    provider = ScriptedProvider([])

    result = make_loop(tmp_path, provider, max_steps=2).run("fix")

    assert result.status == "budget_exhausted"
    assert result.step_count == 2
    assert len(provider.calls) == 2


def test_repeated_actions_stop_for_no_progress_before_step_budget(tmp_path):
    (tmp_path / "value.txt").write_text("same")
    repeated = action("read_file", path="value.txt")
    provider = ScriptedProvider([repeated, repeated, repeated])

    result = make_loop(tmp_path, provider, max_steps=3).run("inspect")

    assert result.status == "no_progress"
    assert result.step_count == 3
    assert len(result.tool_calls) == 3


def test_repeated_failed_validations_stop_for_no_progress(tmp_path):
    report = failed("same failure")
    provider = ScriptedProvider(
        [action("run_validation"), action("run_validation")]
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[report], [report]],
    ).run("fix")

    assert result.status == "no_progress"
    assert result.validation_count == 2


def test_multi_validator_progress_is_observed_once_per_validation_run(tmp_path):
    repeated_failure = failed("same failure")
    provider = ScriptedProvider(
        [action("run_validation"), action("run_validation")]
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[
            [repeated_failure, passed()],
            [repeated_failure, passed()],
        ],
        max_steps=2,
    ).run("fix")

    assert result.status == "no_progress"
    assert result.validation_count == 2


def test_model_context_is_bounded_to_newest_64_kib_of_content(tmp_path):
    provider = ScriptedProvider([action("finish", summary="too soon")])
    loop = make_loop(tmp_path, provider, max_steps=1)
    loop.start("fix")
    loop.state.messages.extend(
        [
            {"role": "feedback", "content": "old:" + "a" * 50_000},
            {"role": "tool", "content": "new:" + "b" * 50_000},
        ]
    )

    loop.step()

    sent_messages = provider.calls[0][0]
    assert sum(
        len(message["content"].encode("utf-8")) for message in sent_messages
    ) <= 64 * 1024
    assert sent_messages[-1]["content"].endswith("b" * 100)
    assert all("old:" not in message["content"] for message in sent_messages)


def test_cancel_is_idempotent_and_prevents_model_decisions(tmp_path):
    provider = ScriptedProvider([action("run_validation")])
    loop = make_loop(tmp_path, provider)
    loop.start("fix")

    first = loop.cancel()
    event_count = len(first.events)
    second = loop.cancel()

    assert first is second
    assert second.status == "cancelled"
    assert len(second.events) == event_count
    assert provider.calls == []


def test_wall_clock_budget_precedes_step_budget(monkeypatch, tmp_path):
    ticks = iter([10.0, 12.0])
    monkeypatch.setattr("forgeloop.loop.time.monotonic", lambda: next(ticks))
    provider = ScriptedProvider([action("finish", summary="too soon")])
    config = HarnessConfig(max_steps=1, wall_time_seconds=1)
    loop = AgentLoop(
        provider=provider,
        policy=PolicyEngine(config),
        tools=ToolRuntime(tmp_path, config),
        validators=ScriptedValidators([]),
        progress=ProgressTracker(
            max_identical_failures=config.max_identical_failures,
            max_identical_actions=config.max_identical_actions,
        ),
        memory=MemoryStore(":memory:"),
        config=config,
        project_id="test-project",
    )

    result = loop.run("fix")

    assert result.status == "budget_exhausted"
    assert result.events[-1].data["reason"] == "wall_time"


def test_remember_and_recall_are_project_scoped_and_config_bounded(tmp_path):
    config = HarnessConfig(
        max_steps=3,
        memory_recall_limit=1,
        memory_char_budget=5,
    )
    memory = MemoryStore(":memory:")
    memory.upsert("other-project", "foreign", "other", ["fact"])
    provider = ScriptedProvider(
        [
            action(
                "remember",
                key="local",
                value="three",
                tags=["fact"],
            ),
            action("recall", tags=["fact"]),
            action("finish", summary="too soon"),
        ]
    )

    result = make_loop(
        tmp_path,
        provider,
        config=config,
        memory=memory,
    ).run("remember context")

    assert result.status == "budget_exhausted"
    recall_feedback = [
        json.loads(message["content"])
        for message in provider.calls[2][0]
        if message["role"] == "tool"
        and json.loads(message["content"]).get("type") == "memory_recall"
    ]
    assert len(recall_feedback) == 1
    assert recall_feedback[0]["records"] == [
        {"key": "local", "tags": ["fact"], "value": "three"}
    ]
    assert [record.key for record in memory.recall(
        "test-project", ["fact"], limit=10, char_budget=100
    )] == ["local"]


def test_approval_mismatch_and_consumption_never_double_execute(tmp_path):
    provider = ScriptedProvider(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    loop = make_loop(tmp_path, provider)
    state = loop.run("clean")
    fingerprint = state.pending_decision.fingerprint

    with pytest.raises(ApprovalMismatch):
        loop.resolve_approval("wrong-fingerprint", approved=True)
    assert state.tool_calls == []

    resumed = loop.resolve_approval(fingerprint, approved=True)

    assert resumed.status == "running"
    assert resumed.pending_action is None
    assert resumed.pending_decision is None
    assert fingerprint in resumed.used_approvals
    assert len(resumed.tool_calls) == 1
    with pytest.raises(ApprovalMismatch):
        loop.resolve_approval(fingerprint, approved=True)
    assert len(resumed.tool_calls) == 1


def test_approval_executes_canonical_snapshot_not_mutated_display_action(tmp_path):
    (tmp_path / "build-a").mkdir()
    (tmp_path / "build-b").mkdir()
    provider = ScriptedProvider(
        [action("run_command", argv=["rm", "-rf", "build-a"])]
    )
    loop = make_loop(tmp_path, provider)
    state = loop.run("clean")
    fingerprint = state.pending_decision.fingerprint

    state.pending_action.arguments["argv"][-1] = "build-b"
    resumed = loop.resolve_approval(fingerprint, approved=True)

    assert resumed.tool_calls[-1].arguments["argv"][-1] == "build-a"
    assert not (tmp_path / "build-a").exists()
    assert (tmp_path / "build-b").exists()


def test_rejected_approval_resumes_with_feedback_and_cannot_be_reused(tmp_path):
    provider = ScriptedProvider(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    loop = make_loop(tmp_path, provider)
    state = loop.run("clean")
    fingerprint = state.pending_decision.fingerprint

    resumed = loop.resolve_approval(fingerprint, approved=False)

    assert resumed.status == "running"
    assert resumed.pending_action is None
    assert resumed.pending_decision is None
    assert resumed.tool_calls == []
    assert fingerprint in resumed.used_approvals
    assert resumed.messages[-1]["role"] == "feedback"
    assert json.loads(resumed.messages[-1]["content"])["type"] == "approval_rejected"
    with pytest.raises(ApprovalMismatch):
        loop.resolve_approval(fingerprint, approved=False)


def test_used_approval_fingerprint_is_feedback_not_a_new_pending_action(tmp_path):
    repeated = action("run_command", argv=["rm", "-rf", "build"])
    provider = ScriptedProvider(
        [repeated, repeated, action("run_validation")]
    )
    loop = make_loop(
        tmp_path,
        provider,
        validation_results=[[passed()]],
        max_steps=4,
    )
    state = loop.run("clean")
    fingerprint = state.pending_decision.fingerprint
    loop.resolve_approval(fingerprint, approved=True)

    after_repeat = loop.step()

    assert after_repeat.status == "running"
    assert after_repeat.pending_action is None
    assert after_repeat.pending_decision is None
    assert len(after_repeat.tool_calls) == 1
    assert after_repeat.messages[-1]["role"] == "feedback"
    assert json.loads(after_repeat.messages[-1]["content"])["type"] == (
        "approval_already_used"
    )

    after_change = loop.step()
    assert after_change.validation_count == 1
    assert len(provider.calls) == 3


@pytest.mark.parametrize("approved,expected_tool_calls", [(True, 1), (False, 0)])
def test_approval_resolution_settles_step_budget_without_another_model_call(
    tmp_path, approved, expected_tool_calls
):
    provider = ScriptedProvider(
        [action("run_command", argv=["rm", "-rf", "build"])]
    )
    loop = make_loop(tmp_path, provider, max_steps=1)
    state = loop.run("clean")
    fingerprint = state.pending_decision.fingerprint

    resolved = loop.resolve_approval(fingerprint, approved=approved)

    assert resolved.status == "budget_exhausted"
    assert resolved.events[-1].data["reason"] == "steps"
    assert len(resolved.tool_calls) == expected_tool_calls
    assert len(provider.calls) == 1


def test_successful_workspace_mutation_invalidates_prior_validation(tmp_path):
    (tmp_path / "calc.py").write_text("return 0\n")
    provider = ScriptedProvider(
        [
            action("run_validation"),
            action(
                "replace_text",
                path="calc.py",
                old="return 0",
                new="return 1",
                count=1,
            ),
            action("finish", summary="not revalidated"),
        ]
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[passed()]],
        max_steps=3,
    ).run("fix")

    assert result.status == "budget_exhausted"
    assert result.last_validation_passed is False


def test_failed_mutation_attempt_invalidates_prior_validation(tmp_path):
    (tmp_path / "side_effect.py").write_text(
        "from pathlib import Path\n"
        "Path('side-effect.txt').write_text('changed')\n"
        "raise SystemExit(1)\n"
    )
    provider = ScriptedProvider(
        [
            action("run_validation"),
            action("run_command", argv=["python3", "side_effect.py"]),
            action("finish", summary="not revalidated"),
        ]
    )

    result = make_loop(
        tmp_path,
        provider,
        validation_results=[[passed()]],
        max_steps=3,
    ).run("fix")

    assert (tmp_path / "side-effect.txt").read_text() == "changed"
    assert result.status == "budget_exhausted"
    assert result.last_validation_passed is False


class FailingMemoryBackend:
    def __init__(self, error):
        self.error = error

    def upsert(self, project_id, key, value, tags):
        raise self.error

    def recall(self, project_id, tags, limit, char_budget):
        raise self.error


@pytest.mark.parametrize(
    "memory_action,error",
    [
        (
            action("remember", key="fact", value="value", tags=["tag"]),
            sqlite3.OperationalError("sqlite detail must not leak"),
        ),
        (
            action("remember", key="fact", value="value", tags=["tag"]),
            OSError("filesystem detail must not leak"),
        ),
        (
            action("recall", tags=["tag"]),
            sqlite3.OperationalError("sqlite detail must not leak"),
        ),
        (
            action("recall", tags=["tag"]),
            OSError("filesystem detail must not leak"),
        ),
    ],
)
def test_memory_backend_failures_become_redacted_tool_feedback(
    tmp_path, memory_action, error
):
    provider = ScriptedProvider([memory_action])
    loop = make_loop(
        tmp_path,
        provider,
        max_steps=2,
        memory=FailingMemoryBackend(error),
    )
    loop.start("use memory")

    state = loop.step()

    assert state.status == "running"
    assert len(state.tool_calls) == 1
    assert state.messages[-1]["role"] == "tool"
    feedback = json.loads(state.messages[-1]["content"])
    assert feedback["type"] == "memory_error"
    assert str(error) not in state.messages[-1]["content"]
    assert state.events[-1].data["ok"] is False
