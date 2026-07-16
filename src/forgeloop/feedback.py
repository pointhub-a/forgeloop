"""Deterministic validation feedback for ForgeLoop."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from forgeloop.config import ValidatorConfig
from forgeloop.models import (
    Action,
    FailureClass,
    ValidationReport,
    ValidationStatus,
)
from forgeloop.tools import ToolRuntime


# Ordering is intentional: a test runner can include type or lint-looking source
# text in a traceback, while parser failures should always remain syntax failures.
_FAILURE_PATTERNS: tuple[tuple[FailureClass, tuple[re.Pattern[str], ...]], ...] = (
    (
        FailureClass.SYNTAX,
        (
            re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b", re.I),
            re.compile(r"\binvalid syntax\b", re.I),
        ),
    ),
    (
        FailureClass.TEST_FAILURE,
        (
            re.compile(r"^FAILED\s+\S+", re.I | re.M),
            re.compile(r"\bAssertionError\b", re.I),
            re.compile(r"\b\d+\s+failed\b", re.I),
        ),
    ),
    (
        FailureClass.LINT,
        (
            re.compile(r"(?:^|\s)[A-Z]\d{3}(?:\s|$)", re.M),
            re.compile(r"\b(?:ruff|flake8|pylint)\b", re.I),
        ),
    ),
    (
        FailureClass.TYPE_ERROR,
        (
            re.compile(r"\bincompatible types?\b", re.I),
            re.compile(r"\b(?:mypy|pyright)\b", re.I),
            re.compile(r"^.+:\d+:\s+error:", re.I | re.M),
        ),
    ),
)

_FINGERPRINT_OUTPUT_CHARS = 8192
_TEMPORARY_PATH = re.compile(
    r"(?<!\w)(?:[A-Za-z]:[\\/](?:Temp|Tmp)[\\/][^\s]+|"
    r"/(?:private/)?(?:tmp|var/tmp|var/folders)/[^\s]+)",
    re.I,
)
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_WHITESPACE = re.compile(r"\s+")


def classify_failure(
    argv: list[str], exit_code: int | None, stdout: str, stderr: str
) -> FailureClass:
    """Classify a failed validator using ordered command/output patterns."""

    evidence = "\n".join((" ".join(argv), stdout, stderr))
    output_evidence = "\n".join((stdout, stderr))
    if re.search(
        r"\b(?:timed out|timeout(?: expired| after|:)|TimeoutExpired)\b",
        output_evidence,
        re.I,
    ):
        return FailureClass.TIMEOUT
    if exit_code is None:
        return FailureClass.INFRASTRUCTURE
    for classification, patterns in _FAILURE_PATTERNS:
        if any(pattern.search(evidence) for pattern in patterns):
            return classification
    return FailureClass.UNKNOWN


def report_fingerprint(report: ValidationReport) -> str:
    """Hash stable failure evidence from the bounded tail of validator output."""

    output = f"stdout:\n{report.stdout}\nstderr:\n{report.stderr}"
    normalized = _TEMPORARY_PATH.sub("<TEMP_PATH>", output)
    normalized = _TIMESTAMP.sub("<TIMESTAMP>", normalized)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    evidence = {
        "classification": (
            report.classification.value if report.classification is not None else None
        ),
        "exit_code": report.exit_code,
        "output_tail": normalized[-_FINGERPRINT_OUTPUT_CHARS:],
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def all_validations_passed(reports: Sequence[ValidationReport]) -> bool:
    """Return true only for a non-empty, entirely passing validation run."""

    return bool(reports) and all(
        report.status is ValidationStatus.PASSED for report in reports
    )


class ValidatorRunner:
    """Run configured validators in order through the bounded tool runtime."""

    def __init__(
        self, runtime: ToolRuntime, validators: list[ValidatorConfig]
    ) -> None:
        self.runtime = runtime
        self.validators = validators

    def run_all(self) -> list[ValidationReport]:
        reports: list[ValidationReport] = []
        for validator in self.validators:
            result = self.runtime.execute(
                Action(
                    kind="run_command",
                    arguments={
                        "argv": validator.argv,
                        "timeout_seconds": validator.timeout_seconds,
                    },
                )
            )
            metadata = result.metadata
            exit_code = metadata.get("exit_code")
            duration_ms = metadata.get("duration_ms", 0)
            error_code = metadata.get("error_code")
            if not isinstance(exit_code, int):
                exit_code = None
            if not isinstance(duration_ms, int):
                duration_ms = 0

            stderr = result.error or ""
            if not result.ok:
                if error_code == "timeout":
                    report = ValidationReport(
                        argv=validator.argv,
                        status=ValidationStatus.TIMED_OUT,
                        classification=FailureClass.TIMEOUT,
                        exit_code=None,
                        duration_ms=duration_ms,
                        stdout=result.output,
                        stderr=stderr,
                    )
                elif error_code == "command_failed":
                    report = ValidationReport.failed(
                        validator.argv,
                        duration_ms,
                        exit_code if exit_code is not None else 1,
                        stdout=result.output,
                        stderr=stderr,
                        classification=classify_failure(
                            validator.argv, exit_code, result.output, stderr
                        ),
                    )
                else:
                    report = ValidationReport(
                        argv=validator.argv,
                        status=ValidationStatus.INFRA_ERROR,
                        classification=FailureClass.INFRASTRUCTURE,
                        exit_code=None,
                        duration_ms=duration_ms,
                        stdout=result.output,
                        stderr=stderr,
                    )
            else:
                report = ValidationReport.passed(
                    validator.argv,
                    duration_ms,
                    stdout=result.output,
                    stderr=stderr,
                )
            report.fingerprint = report_fingerprint(report)
            reports.append(report)
        return reports


@dataclass(frozen=True)
class ProgressState:
    """The deterministic stop decision after one progress observation."""

    should_stop: bool
    reason: str | None = None


class ProgressTracker:
    """Detect consecutive identical failed validations and actions."""

    def __init__(
        self, *, max_identical_failures: int, max_identical_actions: int
    ) -> None:
        self.max_identical_failures = max_identical_failures
        self.max_identical_actions = max_identical_actions
        self._last_failure_fingerprint: str | None = None
        self._identical_failure_count = 0
        self._last_action_fingerprint: str | None = None
        self._identical_action_count = 0

    def observe_action(self, fingerprint: str) -> ProgressState:
        if fingerprint == self._last_action_fingerprint:
            self._identical_action_count += 1
        else:
            self._last_action_fingerprint = fingerprint
            self._identical_action_count = 1

        should_stop = self._identical_action_count >= self.max_identical_actions
        return ProgressState(
            should_stop=should_stop,
            reason="no_progress" if should_stop else None,
        )

    def observe_validation(self, report: ValidationReport) -> ProgressState:
        if report.status is ValidationStatus.PASSED:
            self._last_failure_fingerprint = None
            self._identical_failure_count = 0
            return ProgressState(should_stop=False)

        fingerprint = report.fingerprint or report_fingerprint(report)
        if fingerprint == self._last_failure_fingerprint:
            self._identical_failure_count += 1
        else:
            self._last_failure_fingerprint = fingerprint
            self._identical_failure_count = 1

        should_stop = self._identical_failure_count >= self.max_identical_failures
        return ProgressState(
            should_stop=should_stop,
            reason="no_progress" if should_stop else None,
        )
