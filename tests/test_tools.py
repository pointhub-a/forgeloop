import os
import signal
import stat
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from forgeloop.config import HarnessConfig
from forgeloop.models import Action
from forgeloop.tools import ToolRuntime


@pytest.fixture
def runtime(tmp_path):
    return ToolRuntime(tmp_path, HarnessConfig())


@pytest.fixture
def small_file_runtime(tmp_path):
    return ToolRuntime(tmp_path, HarnessConfig(max_file_bytes=4))


@pytest.fixture
def short_timeout_runtime(tmp_path):
    return ToolRuntime(tmp_path, HarnessConfig(command_timeout_seconds=1))


@pytest.fixture
def small_output_runtime(tmp_path):
    return ToolRuntime(tmp_path, HarnessConfig(max_output_bytes=5))


def test_replace_requires_exact_occurrence(runtime, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")

    result = runtime.execute(
        Action(
            kind="replace_text",
            arguments={
                "path": "a.py",
                "old": "x = 1",
                "new": "x = 2",
                "count": 1,
            },
        )
    )

    assert not result.ok
    assert result.metadata["error_code"] == "ambiguous_replacement"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_write_then_read_round_trip(runtime, tmp_path):
    write_result = runtime.execute(
        Action(
            kind="write_file",
            arguments={"path": "pkg/a.py", "content": "ok"},
        )
    )

    assert write_result.ok
    assert (tmp_path / "pkg").is_dir()
    read_result = runtime.execute(
        Action(kind="read_file", arguments={"path": "pkg/a.py"})
    )
    assert read_result.ok
    assert read_result.output == "ok"


def test_read_rejects_oversized_file(small_file_runtime, tmp_path):
    (tmp_path / "large.txt").write_bytes(b"12345")

    result = small_file_runtime.execute(
        Action(kind="read_file", arguments={"path": "large.txt"})
    )

    assert not result.ok
    assert result.metadata["error_code"] == "file_too_large"


def test_write_limit_counts_utf8_bytes(small_file_runtime, tmp_path):
    result = small_file_runtime.execute(
        Action(
            kind="write_file",
            arguments={"path": "large.txt", "content": "ééé"},
        )
    )

    assert not result.ok
    assert result.metadata["error_code"] == "file_too_large"
    assert not (tmp_path / "large.txt").exists()


def test_read_requires_strict_utf8(runtime, tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"\xff")

    result = runtime.execute(
        Action(kind="read_file", arguments={"path": "binary.dat"})
    )

    assert not result.ok
    assert result.metadata["error_code"] == "invalid_utf8"


def test_write_rejects_text_that_cannot_be_encoded_as_utf8(runtime, tmp_path):
    result = runtime.execute(
        Action(
            kind="write_file",
            arguments={"path": "invalid.txt", "content": "\ud800"},
        )
    )

    assert not result.ok
    assert result.metadata["error_code"] == "invalid_utf8"
    assert not (tmp_path / "invalid.txt").exists()


def test_replace_writes_atomically(runtime, tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_text("before", encoding="utf-8")

    def fail_replace(source, destination):
        assert Path(source).parent == target.parent
        assert Path(destination) == target
        raise OSError("simulated replace failure")

    monkeypatch.setattr("forgeloop.tools.os.replace", fail_replace)

    result = runtime.execute(
        Action(
            kind="replace_text",
            arguments={"path": "a.py", "old": "before", "new": "after", "count": 1},
        )
    )

    assert not result.ok
    assert result.metadata["error_code"] == "io_error"
    assert target.read_text(encoding="utf-8") == "before"
    assert os.listdir(tmp_path) == ["a.py"]


def test_replace_updates_file_when_count_matches(runtime, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("before before", encoding="utf-8")

    result = runtime.execute(
        Action(
            kind="replace_text",
            arguments={
                "path": "a.py",
                "old": "before",
                "new": "after",
                "count": 2,
            },
        )
    )

    assert result.ok
    assert target.read_text(encoding="utf-8") == "after after"


def test_file_action_rejects_workspace_escape(runtime, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = runtime.execute(
        Action(kind="read_file", arguments={"path": "../outside.txt"})
    )

    assert not result.ok
    assert result.metadata["error_code"] == "workspace_escape"
    assert result.output == ""


def test_file_action_rejects_invalid_arguments(runtime):
    result = runtime.execute(Action(kind="read_file", arguments={"path": 7}))

    assert not result.ok
    assert result.metadata["error_code"] == "invalid_arguments"


def test_command_uses_workspace_and_minimal_environment(
    runtime, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    monkeypatch.setenv("FORGELOOP_TEST_SAFE", "safe-value")
    action = Action(
        kind="run_command",
        arguments={
            "argv": [
                sys.executable,
                "-c",
                "import os; print(os.getcwd()); "
                "print(os.getenv('OPENAI_API_KEY')); "
                "print(os.getenv('FORGELOOP_TEST_SAFE'))",
            ]
        },
    )

    result = runtime.execute(action)

    assert result.ok
    assert str(tmp_path) in result.output
    assert "secret-value" not in result.output
    assert "safe-value" in result.output
    assert result.metadata["exit_code"] == 0
    assert result.metadata["duration_ms"] >= 0


def test_command_timeout_returns_structured_error(short_timeout_runtime):
    result = short_timeout_runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(2)",
                ]
            },
        )
    )

    assert not result.ok
    assert result.metadata["error_code"] == "timeout"
    assert result.metadata["exit_code"] is None
    assert result.metadata["duration_ms"] >= 0
    assert "started" in result.output


def test_command_timeout_override_can_shorten_configured_limit(runtime):
    result = runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                "timeout_seconds": 1,
            },
        )
    )

    assert result.metadata["error_code"] == "timeout"


def test_command_timeout_override_cannot_exceed_configured_limit(
    short_timeout_runtime,
):
    result = short_timeout_runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                "timeout_seconds": 120,
            },
        )
    )

    assert result.metadata["error_code"] == "timeout"


def test_command_nonzero_exit_preserves_output_and_exit_code(runtime):
    result = runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print('failed'); sys.exit(7)",
                ]
            },
        )
    )

    assert not result.ok
    assert result.output == "failed\n"
    assert result.metadata["error_code"] == "command_failed"
    assert result.metadata["exit_code"] == 7


def test_command_output_is_truncated_deterministically(small_output_runtime):
    result = small_output_runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'abcdefgh')",
                ]
            },
        )
    )

    assert result.ok
    assert result.output == "abcde"
    assert result.metadata["output_truncated"] is True


def test_large_command_output_keeps_a_bounded_prefix_from_each_stream(
    small_output_runtime,
):
    result = small_output_runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; "
                    "chunk_out = b'O' * 65536; chunk_err = b'E' * 65536; "
                    "[sys.stdout.buffer.write(chunk_out) for _ in range(64)]; "
                    "[sys.stderr.buffer.write(chunk_err) for _ in range(64)]",
                ]
            },
        )
    )

    assert result.ok
    assert result.output == "OOOOOEEEEE"
    assert result.metadata["output_truncated"] is True


def test_large_command_output_does_not_scale_parent_python_memory(tmp_path):
    runtime = ToolRuntime(tmp_path, HarnessConfig(max_output_bytes=16))
    action = Action(
        kind="run_command",
        arguments={
            "argv": [
                sys.executable,
                "-c",
                "import sys; chunk = b'x' * 65536; "
                "[sys.stdout.buffer.write(chunk) for _ in range(128)]",
            ]
        },
    )

    tracemalloc.start()
    try:
        result = runtime.execute(action)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.ok
    assert result.output == "x" * 16
    assert peak_bytes < 1_000_000


def test_command_output_decodes_invalid_utf8_with_replacement(runtime):
    result = runtime.execute(
        Action(
            kind="run_command",
            arguments={
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(bytes([255]))",
                ]
            },
        )
    )

    assert result.ok
    assert result.output == "\ufffd"


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": "python3 -V"},
        {"argv": []},
        {"argv": [sys.executable], "timeout_seconds": 0},
        {
            "argv": [sys.executable, "-c", "pass"],
            "timeout_seconds": None,
        },
        {
            "argv": [sys.executable, "-c", "pass"],
            "timeout_seconds": 121,
        },
        {"argv": [sys.executable], "unexpected": True},
    ],
)
def test_command_rejects_invalid_arguments(runtime, arguments):
    result = runtime.execute(Action(kind="run_command", arguments=arguments))

    assert not result.ok
    assert result.metadata["error_code"] == "invalid_arguments"


def test_command_not_found_returns_structured_error(runtime, tmp_path):
    result = runtime.execute(
        Action(
            kind="run_command",
            arguments={"argv": [str(tmp_path / "missing-command")]},
        )
    )

    assert not result.ok
    assert result.metadata["error_code"] == "command_not_found"
    assert result.metadata["exit_code"] is None


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_command_timeout_terminates_descendant_process(
    short_timeout_runtime, tmp_path
):
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "pathlib.Path('child.pid').write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    result = short_timeout_runtime.execute(
        Action(
            kind="run_command",
            arguments={"argv": [sys.executable, "-c", parent_code]},
        )
    )

    assert result.metadata["error_code"] == "timeout"
    child_pid = int((tmp_path / "child.pid").read_text())
    try:
        deadline = time.monotonic() + 2
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _process_exists(child_pid)
    finally:
        if _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes")
@pytest.mark.parametrize("action_kind", ["write_file", "replace_text"])
def test_atomic_overwrite_preserves_existing_permission_mode(
    runtime, tmp_path, action_kind
):
    target = tmp_path / "script.py"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o751)
    arguments = {"path": "script.py", "content": "after"}
    if action_kind == "replace_text":
        arguments = {
            "path": "script.py",
            "old": "before",
            "new": "after",
            "count": 1,
        }

    result = runtime.execute(Action(kind=action_kind, arguments=arguments))

    assert result.ok
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
