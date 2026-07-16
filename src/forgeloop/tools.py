"""Bounded file and subprocess tools for a ForgeLoop workspace."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable

from forgeloop.config import HarnessConfig
from forgeloop.models import Action, ToolResult
from forgeloop.policy import resolve_workspace_path


class ToolRuntime:
    """Execute supported actions within deterministic resource boundaries."""

    def __init__(self, workspace: Path, config: HarnessConfig | None = None) -> None:
        self.workspace = Path(workspace).resolve(strict=False)
        self.config = config or HarnessConfig()

    def execute(self, action: Action) -> ToolResult:
        handlers: dict[str, Callable[[dict[str, object]], ToolResult]] = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "replace_text": self._replace_text,
            "run_command": self._run_command,
        }
        handler = handlers.get(action.kind.value)
        if handler is None:
            return self._failure(
                "unsupported_action",
                f"ToolRuntime does not execute {action.kind.value!r} actions.",
            )
        return handler(action.arguments)

    def _read_file(self, arguments: dict[str, object]) -> ToolResult:
        path = self._file_path(arguments, {"path"})
        if isinstance(path, ToolResult):
            return path

        content = self._read_utf8(path)
        if isinstance(content, ToolResult):
            return content
        return ToolResult.success(content, metadata={"path": str(path)})

    def _write_file(self, arguments: dict[str, object]) -> ToolResult:
        path = self._file_path(arguments, {"path", "content"})
        content = arguments.get("content")
        if isinstance(path, ToolResult):
            return path
        if not isinstance(content, str):
            return self._failure(
                "invalid_arguments", "write_file content must be a string."
            )

        failure = self._atomic_write_utf8(path, content)
        if failure is not None:
            return failure
        return ToolResult.success(metadata={"path": str(path)})

    def _replace_text(self, arguments: dict[str, object]) -> ToolResult:
        path = self._file_path(arguments, {"path", "old", "new", "count"})
        old = arguments.get("old")
        new = arguments.get("new")
        count = arguments.get("count")
        if isinstance(path, ToolResult):
            return path
        if (
            not isinstance(old, str)
            or not old
            or not isinstance(new, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            return self._failure(
                "invalid_arguments",
                "replace_text requires non-empty old text, string new text, and a "
                "positive integer count.",
            )

        content = self._read_utf8(path)
        if isinstance(content, ToolResult):
            return content
        occurrences = content.count(old)
        if occurrences != count:
            return self._failure(
                "ambiguous_replacement",
                f"Expected exactly {count} occurrence(s), found {occurrences}.",
                path=str(path),
                expected_count=count,
                actual_count=occurrences,
            )

        failure = self._atomic_write_utf8(path, content.replace(old, new, count))
        if failure is not None:
            return failure
        return ToolResult.success(metadata={"path": str(path), "replacements": count})

    def _run_command(self, arguments: dict[str, object]) -> ToolResult:
        if not {"argv"} <= set(arguments) <= {"argv", "timeout_seconds"}:
            return self._failure(
                "invalid_arguments",
                "run_command requires argv and optionally timeout_seconds.",
            )

        argv = arguments.get("argv")
        timeout_override = arguments.get("timeout_seconds")
        has_timeout_override = "timeout_seconds" in arguments
        if (
            not isinstance(argv, list)
            or not argv
            or any(
                not isinstance(argument, str)
                or not argument
                or "\x00" in argument
                for argument in argv
            )
            or (
                has_timeout_override
                and (
                    not isinstance(timeout_override, int)
                    or isinstance(timeout_override, bool)
                    or timeout_override < 1
                    or timeout_override > 120
                )
            )
        ):
            return self._failure(
                "invalid_arguments",
                "run_command argv must be a non-empty string array and timeout_seconds "
                "must be a positive integer.",
            )

        timeout_seconds = self.config.command_timeout_seconds
        if isinstance(timeout_override, int):
            timeout_seconds = min(timeout_seconds, timeout_override)

        started_ns = time.monotonic_ns()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.workspace,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                env=self._command_environment(),
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as exc:
            return ToolResult.error(
                f"Command executable was not found: {exc}",
                metadata={
                    "error_code": "command_not_found",
                    "exit_code": None,
                    "duration_ms": self._duration_ms(started_ns),
                    "output_truncated": False,
                },
            )
        except (OSError, ValueError) as exc:
            return ToolResult.error(
                f"Unable to execute command: {exc}",
                metadata={
                    "error_code": "execution_error",
                    "exit_code": None,
                    "duration_ms": self._duration_ms(started_ns),
                    "output_truncated": False,
                },
            )

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]
        reader_threads = [
            threading.Thread(
                target=self._drain_pipe,
                args=(process.stdout, stdout_buffer, stdout_truncated),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain_pipe,
                args=(process.stderr, stderr_buffer, stderr_truncated),
                daemon=True,
            ),
        ]
        for reader_thread in reader_threads:
            reader_thread.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_timed_out_process(process)
            exit_code = None

        for reader_thread in reader_threads:
            reader_thread.join(timeout=2)
        for pipe, reader_thread in zip(
            (process.stdout, process.stderr), reader_threads, strict=True
        ):
            if reader_thread.is_alive():
                pipe.close()
                reader_thread.join(timeout=1)

        duration_ms = self._duration_ms(started_ns)
        output = (bytes(stdout_buffer) + bytes(stderr_buffer)).decode(
            "utf-8", errors="replace"
        )
        truncated = stdout_truncated[0] or stderr_truncated[0]
        metadata: dict[str, object] = {
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "output_truncated": truncated,
        }
        if timed_out:
            return ToolResult.error(
                f"Command timed out after {timeout_seconds} second(s).",
                output=output,
                metadata={"error_code": "timeout", **metadata},
            )
        if exit_code == 0:
            return ToolResult.success(output, metadata=metadata)
        return ToolResult.error(
            f"Command exited with status {exit_code}.",
            output=output,
            metadata={"error_code": "command_failed", **metadata},
        )

    def _file_path(
        self, arguments: dict[str, object], expected_keys: set[str]
    ) -> Path | ToolResult:
        requested = arguments.get("path")
        if set(arguments) != expected_keys or not isinstance(requested, str):
            return self._failure(
                "invalid_arguments",
                "File action arguments do not match the required schema.",
            )
        try:
            return resolve_workspace_path(self.workspace, requested)
        except (OSError, RuntimeError, ValueError):
            return self._failure(
                "workspace_escape", "The requested path is outside the workspace."
            )

    def _read_utf8(self, path: Path) -> str | ToolResult:
        try:
            with path.open("rb") as file:
                data = file.read(self.config.max_file_bytes + 1)
        except OSError as exc:
            return self._failure("io_error", f"Unable to read file: {exc}")

        if len(data) > self.config.max_file_bytes:
            return self._failure(
                "file_too_large",
                f"File exceeds the {self.config.max_file_bytes}-byte limit.",
                path=str(path),
            )
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._failure(
                "invalid_utf8", "File content is not valid UTF-8.", path=str(path)
            )

    def _atomic_write_utf8(self, path: Path, content: str) -> ToolResult | None:
        try:
            data = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return self._failure(
                "invalid_utf8", "File content cannot be encoded as valid UTF-8."
            )
        if len(data) > self.config.max_file_bytes:
            return self._failure(
                "file_too_large",
                f"Content exceeds the {self.config.max_file_bytes}-byte limit.",
                path=str(path),
            )

        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing_mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                existing_mode = None
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            if existing_mode is not None:
                temporary_path.chmod(existing_mode)
            os.replace(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return self._failure("io_error", f"Unable to write file: {exc}")
        return None

    @staticmethod
    def _command_environment() -> dict[str, str]:
        environment = {"PATH": os.environ.get("PATH", os.defpath)}
        for key, value in os.environ.items():
            if key == "LANG" or key.startswith("LC_") or key.startswith(
                "FORGELOOP_TEST_"
            ):
                environment[key] = value
        return environment

    def _drain_pipe(
        self,
        pipe: BinaryIO,
        output: bytearray,
        truncated: list[bool],
    ) -> None:
        try:
            while chunk := pipe.read(65536):
                remaining = self.config.max_output_bytes - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[0] = True
        finally:
            pipe.close()

    @staticmethod
    def _terminate_timed_out_process(process: subprocess.Popen[bytes]) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()

        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    @staticmethod
    def _duration_ms(started_ns: int) -> int:
        return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)

    @staticmethod
    def _failure(
        error_code: str, message: str, **metadata: object
    ) -> ToolResult:
        return ToolResult.error(
            message, metadata={"error_code": error_code, **metadata}
        )
