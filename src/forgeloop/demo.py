"""Deterministic, offline proof of ForgeLoop's core mechanisms."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

from forgeloop.config import HarnessConfig, ValidatorConfig
from forgeloop.feedback import ProgressTracker, ValidatorRunner
from forgeloop.loop import AgentLoop, LoopState
from forgeloop.memory import MemoryStore
from forgeloop.models import (
    Action,
    GovernanceDecision,
    StrictModel,
    TaskStatus,
    ValidationReport,
)
from forgeloop.policy import PolicyEngine
from forgeloop.providers import ScriptedProvider
from forgeloop.tools import ToolRuntime


_BROKEN_CALC = "def add(a, b):\n    return a - b\n"
_FIXED_CALC = "def add(a, b):\n    return a + b\n"


class DemoResult(StrictModel):
    """Serializable evidence collected from the three offline scenarios."""

    dangerous_action: GovernanceDecision
    first_validation: ValidationReport
    feedback_seen_by_provider: bool
    corrective_action: Action
    final_status: TaskStatus
    no_progress_status: TaskStatus
    event_summaries: dict[str, list[str]]


def _action(kind: str, **arguments: object) -> str:
    return json.dumps(
        {"kind": kind, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )


def _demo_config() -> HarnessConfig:
    expected = repr(_FIXED_CALC)
    validation_code = (
        "from pathlib import Path; "
        f"assert Path('calc.py').read_text(encoding='utf-8') == {expected}"
    )
    return HarnessConfig(
        max_steps=8,
        max_validation_runs=4,
        wall_time_seconds=30,
        command_timeout_seconds=10,
        provider_timeout_seconds=10,
        max_identical_failures=2,
        max_identical_actions=3,
        validators=[
            ValidatorConfig(
                argv=[sys.executable, "-c", validation_code],
                timeout_seconds=10,
            )
        ],
    )


def _loop(
    workspace: Path,
    provider: ScriptedProvider,
    config: HarnessConfig,
    *,
    project_id: str,
) -> AgentLoop:
    runtime = ToolRuntime(workspace, config)
    return AgentLoop(
        provider=provider,
        policy=PolicyEngine(config),
        tools=runtime,
        validators=ValidatorRunner(runtime, config.validators),
        progress=ProgressTracker(
            max_identical_failures=config.max_identical_failures,
            max_identical_actions=config.max_identical_actions,
        ),
        memory=MemoryStore(":memory:"),
        config=config,
        project_id=project_id,
    )


def _first_validation(state: LoopState) -> ValidationReport:
    for event in state.events:
        reports = event.data.get("reports")
        if isinstance(reports, list) and reports:
            return ValidationReport.model_validate(reports[0])
    raise RuntimeError("demo did not produce validation evidence")


def _feedback_was_seen(provider: ScriptedProvider) -> bool:
    if len(provider.calls) < 2:
        return False
    messages, _schema = provider.calls[1]
    for message in messages:
        if message.get("role") != "feedback":
            continue
        try:
            payload = json.loads(message["content"])
        except (KeyError, TypeError, ValueError):
            continue
        if payload.get("type") == "validation" and payload.get("passed") is False:
            return True
    return False


def _summaries(state: LoopState) -> list[str]:
    return [event.summary for event in state.events]


def run_mechanism_demo(base_dir: Path) -> DemoResult:
    """Run three deterministic scenarios without credentials or network access."""

    root = Path(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    config = _demo_config()
    with tempfile.TemporaryDirectory(prefix="forgeloop-demo-", dir=root) as temporary:
        demo_root = Path(temporary)

        governance_workspace = demo_root / "governance"
        governance_workspace.mkdir()
        dangerous = _action(
            "run_command", argv=["rm", "-rf", "build-artifacts"]
        )
        governance_loop = _loop(
            governance_workspace,
            ScriptedProvider([dangerous]),
            config,
            project_id="demo-governance",
        )
        governance_state = governance_loop.start(
            "Show that destructive commands require approval."
        )
        governance_loop.step()
        if governance_state.pending_decision is None:
            raise RuntimeError("demo did not produce a governance decision")

        correction_workspace = demo_root / "correction"
        correction_workspace.mkdir()
        (correction_workspace / "calc.py").write_text(
            _BROKEN_CALC, encoding="utf-8"
        )
        correction_provider = ScriptedProvider(
            [
                _action("run_validation"),
                _action(
                    "replace_text",
                    path="calc.py",
                    old="return a - b",
                    new="return a + b",
                    count=1,
                ),
                _action("run_validation"),
                _action("finish", summary="calc.py now adds its operands."),
            ]
        )
        correction_loop = _loop(
            correction_workspace,
            correction_provider,
            config,
            project_id="demo-correction",
        )
        correction_state = correction_loop.run(
            "Repair calc.py, validate it, and finish only after validation passes."
        )
        corrective_action = next(
            action
            for action in correction_state.tool_calls
            if action.kind.value == "replace_text"
        )

        no_progress_workspace = demo_root / "no-progress"
        no_progress_workspace.mkdir()
        (no_progress_workspace / "calc.py").write_text(
            _BROKEN_CALC, encoding="utf-8"
        )
        no_progress_loop = _loop(
            no_progress_workspace,
            ScriptedProvider(
                [_action("run_validation"), _action("run_validation")]
            ),
            config,
            project_id="demo-no-progress",
        )
        no_progress_state = no_progress_loop.run(
            "Stop after the same validation failure repeats."
        )

        return DemoResult(
            dangerous_action=governance_state.pending_decision,
            first_validation=_first_validation(correction_state),
            feedback_seen_by_provider=_feedback_was_seen(correction_provider),
            corrective_action=corrective_action,
            final_status=correction_state.status,
            no_progress_status=no_progress_state.status,
            event_summaries={
                "governance": _summaries(governance_state),
                "correction": _summaries(correction_state),
                "no_progress": _summaries(no_progress_state),
            },
        )
