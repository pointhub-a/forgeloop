import sys

import pytest

from forgeloop.config import HarnessConfig, ValidatorConfig
from forgeloop.feedback import (
    ProgressTracker,
    ValidatorRunner,
    all_validations_passed,
    classify_failure,
    report_fingerprint,
)
from forgeloop.models import FailureClass, ValidationReport, ValidationStatus
from forgeloop.tools import ToolRuntime


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("SyntaxError: invalid syntax", "syntax"),
        ("FAILED tests/test_x.py::test_x", "test_failure"),
        ("error: Incompatible types in assignment", "type_error"),
    ],
)
def test_failure_classification(stderr, expected):
    assert classify_failure(["pytest"], 1, "", stderr) == expected


def test_report_fingerprint_normalizes_volatile_failure_output():
    first = ValidationReport.failed(
        ["pytest"],
        10,
        1,
        stderr=(
            "2026-07-17T09:01:02.123Z  failure in "
            "/tmp/pytest-of-alice/pytest-1/test_case0/example.py\nassert  1 == 2"
        ),
        classification=FailureClass.TEST_FAILURE,
    )
    second = ValidationReport.failed(
        ["pytest"],
        999,
        1,
        stderr=(
            "2027-08-18T10:02:03Z failure in "
            "/tmp/pytest-of-bob/pytest-9/test_case0/example.py\nassert 1 == 2"
        ),
        classification=FailureClass.TEST_FAILURE,
    )

    assert report_fingerprint(first) == report_fingerprint(second)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Command timed out after 2 seconds", "timeout"),
        ("No such file or directory: 'missing-validator'", "infrastructure"),
    ],
)
def test_timeout_and_infrastructure_classifications_remain_distinct(
    stderr, expected
):
    assert classify_failure(["missing-validator"], None, "", stderr) == expected


def test_test_named_timeout_is_not_misclassified_as_process_timeout():
    classification = classify_failure(
        ["pytest", "-k", "timeout"],
        1,
        "",
        "FAILED tests/test_timeout.py::test_timeout - AssertionError",
    )

    assert classification is FailureClass.TEST_FAILURE


def test_repeated_failed_fingerprint_stops_progress():
    tracker = ProgressTracker(max_identical_failures=2, max_identical_actions=3)
    report = ValidationReport.failed(
        ["pytest"],
        1,
        1,
        classification=FailureClass.TEST_FAILURE,
        fingerprint="same",
    )

    assert tracker.observe_validation(report).should_stop is False
    state = tracker.observe_validation(report)

    assert state.should_stop is True
    assert state.reason == "no_progress"


def test_different_action_resets_repeated_action_progress():
    tracker = ProgressTracker(max_identical_failures=2, max_identical_actions=3)

    assert tracker.observe_action("action-a").should_stop is False
    assert tracker.observe_action("action-a").should_stop is False
    assert tracker.observe_action("action-b").should_stop is False
    assert tracker.observe_action("action-a").should_stop is False
    assert tracker.observe_action("action-a").should_stop is False

    state = tracker.observe_action("action-a")
    assert state.should_stop is True
    assert state.reason == "no_progress"


def test_validator_runner_runs_in_config_order_and_aggregates_passes(tmp_path):
    marker = tmp_path / "order.txt"
    validators = [
        ValidatorConfig(
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('order.txt').write_text('one'); "
                "print('first passed')",
            ]
        ),
        ValidatorConfig(
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; p=Path('order.txt'); "
                "p.write_text(p.read_text() + ',two'); print('second passed')",
            ]
        ),
    ]
    runtime = ToolRuntime(tmp_path, HarnessConfig(max_output_bytes=1024))
    runner = ValidatorRunner(runtime, validators)

    reports = runner.run_all()

    assert [report.argv for report in reports] == [item.argv for item in validators]
    assert [report.status for report in reports] == [
        ValidationStatus.PASSED,
        ValidationStatus.PASSED,
    ]
    assert all(report.fingerprint.startswith("sha256:") for report in reports)
    assert marker.read_text() == "one,two"
    assert all_validations_passed(reports) is True
    assert all_validations_passed([]) is False


def test_validator_runner_converts_nonzero_exit_to_classified_failure(tmp_path):
    validator = ValidatorConfig(
        argv=[
            sys.executable,
            "-c",
            "import sys; print('FAILED tests/test_x.py::test_x', file=sys.stderr); "
            "raise SystemExit(1)",
        ]
    )
    runner = ValidatorRunner(ToolRuntime(tmp_path), [validator])

    reports = runner.run_all()

    assert len(reports) == 1
    assert reports[0].status is ValidationStatus.FAILED
    assert reports[0].classification is FailureClass.TEST_FAILURE
    assert reports[0].exit_code == 1
    assert all_validations_passed(reports) is False


def test_validator_runner_keeps_missing_executable_as_infrastructure_error(tmp_path):
    runner = ValidatorRunner(
        ToolRuntime(tmp_path),
        [ValidatorConfig(argv=["forgeloop-validator-that-does-not-exist"])],
    )

    report = runner.run_all()[0]

    assert report.status is ValidationStatus.INFRA_ERROR
    assert report.classification is FailureClass.INFRASTRUCTURE
    assert report.exit_code is None


def test_validator_runner_keeps_timeout_distinct_from_infrastructure(tmp_path):
    runner = ValidatorRunner(
        ToolRuntime(tmp_path),
        [
            ValidatorConfig(
                argv=[sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=1,
            )
        ],
    )

    report = runner.run_all()[0]

    assert report.status is ValidationStatus.TIMED_OUT
    assert report.classification is FailureClass.TIMEOUT
    assert report.exit_code is None


def test_validator_runner_converts_runtime_rejection_to_infrastructure(tmp_path):
    runner = ValidatorRunner(
        ToolRuntime(tmp_path),
        [ValidatorConfig(argv=[])],
    )

    report = runner.run_all()[0]

    assert report.status is ValidationStatus.INFRA_ERROR
    assert report.classification is FailureClass.INFRASTRUCTURE
