"""Stateful, deterministic ForgeLoop agent decision loop."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import sqlite3
import time

from forgeloop.feedback import all_validations_passed
from forgeloop.models import (
    Action,
    DecisionEffect,
    EventKind,
    GovernanceDecision,
    TaskStatus,
)
from forgeloop.policy import action_fingerprint
from forgeloop.providers import ActionParseError, Provider, parse_action


_CONTEXT_CONTENT_BYTES = 64 * 1024


class ApprovalMismatch(ValueError):
    """Raised when an approval does not match the one pending decision."""


@dataclass(frozen=True)
class LoopEvent:
    """One auditable transition emitted by an agent loop."""

    kind: EventKind
    summary: str
    data: dict[str, object] = field(default_factory=dict)


@dataclass
class LoopState:
    """All mutable state owned by one running task."""

    description: str
    status: TaskStatus = TaskStatus.CREATED
    messages: list[dict[str, str]] = field(default_factory=list)
    events: list[LoopEvent] = field(default_factory=list)
    step_count: int = 0
    validation_count: int = 0
    last_validation_passed: bool = False
    pending_action: Action | None = None
    pending_decision: GovernanceDecision | None = None
    used_approvals: set[str] = field(default_factory=set)
    summary: str = ""
    tool_calls: list[Action] = field(default_factory=list)


@dataclass(frozen=True)
class LoopCheckpoint:
    """Restorable approval-resolution state for one live loop."""

    state: LoopState
    pending_action_snapshot: str | None
    pending_fingerprint: str | None
    pending_rule_id: str | None
    pending_no_progress: bool


class AgentLoop:
    """Coordinate exactly one provider decision per step."""

    def __init__(
        self,
        provider: Provider,
        policy,
        tools,
        validators,
        progress,
        memory,
        config,
        project_id: str,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.tools = tools
        self.validators = validators
        self.progress = progress
        self.memory = memory
        self.config = config
        self.project_id = project_id
        self.state: LoopState | None = None
        self._started_at: float | None = None
        self._pending_action_snapshot: str | None = None
        self._pending_fingerprint: str | None = None
        self._pending_rule_id: str | None = None
        self._pending_no_progress = False

    def start(self, description: str) -> LoopState:
        self.state = LoopState(
            description=description,
            status=TaskStatus.RUNNING,
            messages=[{"role": "user", "content": description}],
        )
        self._started_at = time.monotonic()
        self._clear_private_pending()
        self._event(EventKind.STATE, "Task started.", {"status": "running"})
        return self.state

    def step(self) -> LoopState:
        state = self._require_state()
        if state.status is not TaskStatus.RUNNING:
            return state

        state.step_count += 1
        self._event(EventKind.MODEL_REQUEST, "Requested one model action.")
        try:
            response = self.provider.complete(
                self._bounded_context(), Action.model_json_schema()
            )
        except Exception:
            self._message(
                "feedback",
                {
                    "type": "provider_error",
                    "message": "The model provider failed to return an action.",
                },
            )
            self._finish_step(provider_failed=True)
            return state

        state.messages.append({"role": "assistant", "content": response})
        try:
            action = parse_action(response)
        except ActionParseError:
            self._message(
                "feedback",
                {
                    "type": "action_parse_error",
                    "message": "Return exactly one action matching the JSON schema.",
                },
            )
            self._finish_step(provider_failed=True)
            return state
        self._event(
            EventKind.ACTION,
            f"Model proposed {action.kind.value}.",
            {"action": action.model_dump(mode="json")},
        )

        fingerprint = action_fingerprint(action)
        action_progress = self.progress.observe_action(fingerprint)
        decision = self.policy.evaluate(action, self.tools.workspace)
        self._event(
            EventKind.GOVERNANCE_DECISION,
            f"Policy decision: {decision.effect.value}.",
            {"decision": decision.model_dump(mode="json")},
        )
        if decision.effect is DecisionEffect.REQUIRE_APPROVAL:
            if fingerprint in state.used_approvals:
                self._message(
                    "feedback",
                    {
                        "type": "approval_already_used",
                        "message": "That exact action approval was already consumed.",
                        "fingerprint": fingerprint,
                    },
                )
                self._event(
                    EventKind.APPROVAL,
                    "Already-consumed approval fingerprint was proposed again.",
                    {"fingerprint": fingerprint, "used": True},
                )
            else:
                snapshot = json.dumps(
                    action.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                state.pending_action = Action.model_validate_json(snapshot)
                state.pending_decision = decision
                self._pending_action_snapshot = snapshot
                self._pending_fingerprint = decision.fingerprint
                self._pending_rule_id = decision.rule_id
                self._pending_no_progress = action_progress.should_stop
                state.status = TaskStatus.WAITING_APPROVAL
                self._event(
                    EventKind.STATE,
                    "Waiting for approval.",
                    {
                        "status": state.status.value,
                        "fingerprint": decision.fingerprint,
                    },
                )
        elif decision.effect is DecisionEffect.DENY:
            state.status = TaskStatus.DENIED
            self._message(
                "feedback",
                {
                    "type": "policy_denial",
                    "rule_id": decision.rule_id,
                    "message": decision.reason,
                },
            )
            self._event(
                EventKind.STATE,
                "Action denied by policy.",
                {"status": state.status.value, "rule_id": decision.rule_id},
            )
            state.status = TaskStatus.RUNNING
            self._event(
                EventKind.STATE,
                "Task resumed after policy denial.",
                {"status": state.status.value, "reason": "policy_denial"},
            )
        else:
            validation_no_progress = self._execute(action)

        no_progress = action_progress.should_stop
        if decision.effect is DecisionEffect.ALLOW:
            no_progress = no_progress or validation_no_progress
        self._finish_step(no_progress=no_progress)
        return state

    def run(self, description: str) -> LoopState:
        state = self.start(description)
        while state.status is TaskStatus.RUNNING:
            self.step()
        return state

    def checkpoint(self) -> LoopCheckpoint:
        """Capture a deep, restorable copy of live approval state."""

        return LoopCheckpoint(
            state=deepcopy(self._require_state()),
            pending_action_snapshot=self._pending_action_snapshot,
            pending_fingerprint=self._pending_fingerprint,
            pending_rule_id=self._pending_rule_id,
            pending_no_progress=self._pending_no_progress,
        )

    def restore(self, checkpoint: LoopCheckpoint) -> LoopState:
        """Restore a previously captured live-loop checkpoint."""

        self.state = deepcopy(checkpoint.state)
        self._pending_action_snapshot = checkpoint.pending_action_snapshot
        self._pending_fingerprint = checkpoint.pending_fingerprint
        self._pending_rule_id = checkpoint.pending_rule_id
        self._pending_no_progress = checkpoint.pending_no_progress
        return self.state

    def cancel(self) -> LoopState:
        state = self._require_state()
        if state.status is TaskStatus.CANCELLED:
            return state
        state.status = TaskStatus.CANCELLED
        state.pending_action = None
        state.pending_decision = None
        self._clear_private_pending()
        self._event(
            EventKind.STATE,
            "Task cancelled.",
            {"status": state.status.value},
        )
        return state

    def validate_pending_approval(self, fingerprint: str) -> Action:
        """Return the canonical pending action after complete side-effect-free checks."""

        state = self._require_state()
        if (
            state.status is not TaskStatus.WAITING_APPROVAL
            or self._pending_action_snapshot is None
            or self._pending_fingerprint != fingerprint
            or self._pending_rule_id is None
            or fingerprint in state.used_approvals
        ):
            raise ApprovalMismatch("approval fingerprint does not match pending action")

        try:
            action = Action.model_validate_json(self._pending_action_snapshot)
        except ValueError:
            raise ApprovalMismatch("pending approval snapshot is invalid") from None
        if action_fingerprint(action) != fingerprint:
            raise ApprovalMismatch("pending approval snapshot fingerprint changed")
        current_decision = self.policy.evaluate(action, self.tools.workspace)
        if (
            current_decision.effect is not DecisionEffect.REQUIRE_APPROVAL
            or current_decision.fingerprint != fingerprint
            or current_decision.rule_id != self._pending_rule_id
        ):
            raise ApprovalMismatch("pending action no longer has the same policy decision")
        return action

    def resolve_approval(self, fingerprint: str, approved: bool) -> LoopState:
        state = self._require_state()
        action = self.validate_pending_approval(fingerprint)

        no_progress = self._pending_no_progress
        state.used_approvals.add(fingerprint)
        state.pending_action = None
        state.pending_decision = None
        self._clear_private_pending()
        state.status = TaskStatus.RUNNING
        self._event(
            EventKind.APPROVAL,
            "Pending action approved." if approved else "Pending action rejected.",
            {"approved": approved, "fingerprint": fingerprint},
        )
        if approved:
            no_progress = self._execute(action) or no_progress
        else:
            self._message(
                "feedback",
                {
                    "type": "approval_rejected",
                    "message": "The pending action was rejected by the operator.",
                    "fingerprint": fingerprint,
                },
            )
        self._finish_step(no_progress=no_progress)
        return state

    def _clear_private_pending(self) -> None:
        self._pending_action_snapshot = None
        self._pending_fingerprint = None
        self._pending_rule_id = None
        self._pending_no_progress = False

    def _execute(self, action: Action) -> bool:
        state = self._require_state()
        if action.kind.value == "run_validation":
            if state.validation_count >= self.config.max_validation_runs:
                state.status = TaskStatus.BUDGET_EXHAUSTED
                self._message(
                    "feedback",
                    {
                        "type": "validation_budget_exhausted",
                        "message": "The validation run budget is exhausted.",
                    },
                )
                self._event(
                    EventKind.STATE,
                    "Validation budget exhausted.",
                    {"status": state.status.value, "reason": "validation_runs"},
                )
                return False
            state.tool_calls.append(action)
            reports = self.validators.run_all()
            state.validation_count += 1
            state.last_validation_passed = all_validations_passed(reports)
            no_progress = self.progress.observe_validation_run(reports).should_stop
            self._message(
                "feedback",
                {
                    "type": "validation",
                    "passed": state.last_validation_passed,
                    "reports": [
                        report.model_dump(mode="json") for report in reports
                    ],
                },
            )
            self._event(
                EventKind.VALIDATION,
                "Validation passed."
                if state.last_validation_passed
                else "Validation failed.",
                {"reports": [report.model_dump(mode="json") for report in reports]},
            )
            return no_progress

        if action.kind.value == "remember":
            arguments = action.arguments
            key = arguments.get("key")
            value = arguments.get("value")
            tags = arguments.get("tags")
            if (
                set(arguments) != {"key", "value", "tags"}
                or not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not isinstance(tags, list)
                or any(not isinstance(tag, str) or not tag for tag in tags)
            ):
                self._memory_failure(
                    action,
                    "remember requires key, value, and a string tag array.",
                )
                return False
            state.tool_calls.append(action)
            try:
                self.memory.upsert(self.project_id, key, value, tags)
            except (TypeError, ValueError, sqlite3.Error, OSError):
                self._memory_backend_failure(action)
                return False
            self._message(
                "tool",
                {"type": "memory_remember", "key": key, "ok": True},
            )
            self._event(
                EventKind.TOOL_RESULT,
                "Memory stored.",
                {"action": "remember", "ok": True, "key": key},
            )
            return False

        if action.kind.value == "recall":
            arguments = action.arguments
            tags = arguments.get("tags")
            requested_limit = arguments.get("limit")
            if (
                set(arguments) != {"tags", "limit"}
                or not isinstance(tags, list)
                or any(not isinstance(tag, str) or not tag for tag in tags)
                or not isinstance(requested_limit, int)
                or isinstance(requested_limit, bool)
                or requested_limit < 1
            ):
                self._memory_failure(
                    action, "recall requires a string tag array and positive limit."
                )
                return False
            state.tool_calls.append(action)
            try:
                records = self.memory.recall(
                    self.project_id,
                    tags,
                    min(requested_limit, self.config.memory_recall_limit),
                    self.config.memory_char_budget,
                )
            except (sqlite3.Error, OSError):
                self._memory_backend_failure(action)
                return False
            serialized = [
                {
                    "key": record.key,
                    "value": record.value,
                    "tags": list(record.tags),
                }
                for record in records
            ]
            self._message(
                "tool",
                {"type": "memory_recall", "records": serialized},
            )
            self._event(
                EventKind.TOOL_RESULT,
                f"Recalled {len(records)} memory record(s).",
                {"action": "recall", "count": len(records)},
            )
            return False

        if action.kind.value == "finish":
            summary = action.arguments.get("summary")
            if state.last_validation_passed and isinstance(summary, str):
                state.summary = summary
                state.status = TaskStatus.SUCCEEDED
                self._event(
                    EventKind.STATE,
                    "Task succeeded.",
                    {"status": state.status.value, "summary": summary},
                )
            else:
                self._message(
                    "feedback",
                    {
                        "type": "finish_rejected",
                        "message": "Finish requires a latest passing validation.",
                    },
                )
            return False

        state.tool_calls.append(action)
        if action.kind.value in {"write_file", "replace_text", "run_command"}:
            state.last_validation_passed = False
        result = self.tools.execute(action)
        self._message(
            "tool",
            {
                "type": "tool_result",
                "action": action.kind.value,
                **result.model_dump(mode="json"),
            },
        )
        self._event(
            EventKind.TOOL_RESULT,
            f"Tool {action.kind.value} {'succeeded' if result.ok else 'failed'}.",
            {"result": result.model_dump(mode="json")},
        )
        return False

    def _memory_failure(self, action: Action, message: str) -> None:
        self._message(
            "tool",
            {
                "type": "memory_error",
                "action": action.kind.value,
                "message": message,
            },
        )
        self._event(
            EventKind.TOOL_RESULT,
            f"Memory {action.kind.value} failed.",
            {"action": action.kind.value, "ok": False},
        )

    def _memory_backend_failure(self, action: Action) -> None:
        self._message(
            "tool",
            {
                "type": "memory_error",
                "action": action.kind.value,
                "message": "The memory backend could not be accessed safely.",
            },
        )
        self._event(
            EventKind.TOOL_RESULT,
            f"Memory {action.kind.value} backend failed.",
            {"action": action.kind.value, "ok": False},
        )

    def _finish_step(
        self, *, no_progress: bool = False, provider_failed: bool = False
    ) -> None:
        state = self._require_state()
        if state.status is TaskStatus.CANCELLED:
            return
        if state.status is TaskStatus.WAITING_APPROVAL:
            return
        if state.status is not TaskStatus.RUNNING:
            return
        if no_progress:
            state.status = TaskStatus.NO_PROGRESS
            self._event(
                EventKind.STATE,
                "Stopped because no progress was detected.",
                {"status": state.status.value, "reason": "no_progress"},
            )
            return
        assert self._started_at is not None
        if time.monotonic() - self._started_at >= self.config.wall_time_seconds:
            state.status = TaskStatus.BUDGET_EXHAUSTED
            self._event(
                EventKind.STATE,
                "Wall-time budget exhausted.",
                {"status": state.status.value, "reason": "wall_time"},
            )
            return
        if state.step_count >= self.config.max_steps:
            state.status = TaskStatus.BUDGET_EXHAUSTED
            self._event(
                EventKind.STATE,
                "Step budget exhausted.",
                {"status": state.status.value, "reason": "steps"},
            )
            return
        if provider_failed:
            self._event(
                EventKind.STATE,
                "Provider failure recorded; retrying within the step budget.",
                {"status": state.status.value, "reason": "provider_failure"},
            )

    def _bounded_context(self) -> list[dict[str, str]]:
        state = self._require_state()
        remaining = _CONTEXT_CONTENT_BYTES
        newest_first: list[dict[str, str]] = []
        for message in reversed(state.messages):
            content = message["content"]
            encoded = content.encode("utf-8")
            if len(encoded) <= remaining:
                bounded_content = content
                remaining -= len(encoded)
            else:
                if remaining <= 0:
                    break
                bounded_content = encoded[-remaining:].decode(
                    "utf-8", errors="ignore"
                )
                remaining = 0
            newest_first.append(
                {"role": message["role"], "content": bounded_content}
            )
            if remaining == 0:
                break
        return list(reversed(newest_first))

    def _message(self, role: str, observation: dict[str, object]) -> None:
        self._require_state().messages.append(
            {
                "role": role,
                "content": json.dumps(
                    observation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            }
        )

    def _event(
        self,
        kind: EventKind,
        summary: str,
        data: dict[str, object] | None = None,
    ) -> None:
        self._require_state().events.append(
            LoopEvent(kind=kind, summary=summary, data=data or {})
        )

    def _require_state(self) -> LoopState:
        if self.state is None:
            raise RuntimeError("AgentLoop has not been started.")
        return self.state
