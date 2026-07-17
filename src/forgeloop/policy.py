"""Deterministic governance for proposed ForgeLoop actions."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from forgeloop.config import HarnessConfig
from forgeloop.models import Action, GovernanceDecision


_PATH_ACTIONS = {"read_file", "write_file", "replace_text"}
_SHELL_METACHARACTERS = (";", "&", "|", ">", "<", "(", ")", "`", "\n", "\r")
_DISALLOWED_GIT_CONFIG_PREFIXES = ("-c", "--config-env=")
_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "--git-dir",
    "--namespace",
    "--work-tree",
}
_GIT_GLOBAL_OPTIONS_WITH_INLINE_VALUE = (
    "--exec-path=",
    "--git-dir=",
    "--namespace=",
    "--work-tree=",
)
_GIT_GLOBAL_FLAGS = {
    "-h",
    "--help",
    "-p",
    "--paginate",
    "-P",
    "--no-pager",
    "-v",
    "--version",
    "--bare",
    "--glob-pathspecs",
    "--html-path",
    "--icase-pathspecs",
    "--info-path",
    "--literal-pathspecs",
    "--man-path",
    "--no-optional-locks",
    "--no-replace-objects",
    "--noglob-pathspecs",
}
_DATABASE_CLIENTS = {"mysql", "mysqlsh", "psql", "sqlite3"}
_DATABASE_DROP = re.compile(r"\bdrop\s+(?:database|schema|table)\b", re.I)
_PRIVILEGE_EXECUTABLES = {"doas", "pkexec", "su", "sudo"}
_PERMISSION_EXECUTABLES = {"chgrp", "chmod", "chown", "setfacl"}


def _dangerous_command_rule(argv: list[str]) -> str | None:
    executable = Path(argv[0]).name.lower()
    lowered = [argument.lower() for argument in argv]

    if executable == "dropdb" or (
        executable in _DATABASE_CLIENTS
        and any(_DATABASE_DROP.search(argument) for argument in argv[1:])
    ):
        return "database.drop"
    if executable in _PRIVILEGE_EXECUTABLES:
        return "privilege.escalation"
    if executable in _PERMISSION_EXECUTABLES:
        return "permission.change"
    pip_install = executable in {"pip", "pip3"} and "install" in lowered[1:]
    python_pip_install = (
        executable in {"python", "python3", "pypy", "pypy3"}
        and len(lowered) >= 4
        and lowered[1:4] == ["-m", "pip", "install"]
    )
    package_exec = (
        executable == "npx"
        or (
            executable in {"npm", "pnpm", "yarn"}
            and any(argument in {"exec", "dlx"} for argument in lowered[1:])
        )
        or executable in {"bunx", "uvx"}
        or (executable == "pipx" and "run" in lowered[1:])
    )
    if pip_install or python_pip_install or package_exec:
        return "network.execute"
    return None


def resolve_workspace_path(workspace: Path, requested: str) -> Path:
    """Return a canonical in-workspace path or reject an escape."""

    root = workspace.resolve(strict=False)
    candidate = (root / requested).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError("requested path escapes the workspace")
    return candidate


def action_fingerprint(action: Action) -> str:
    """Hash an action's canonical JSON representation."""

    canonical = json.dumps(
        action.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_git_subcommand(argv: list[str]) -> tuple[str, list[str]] | None:
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            index += 1
            break
        if argument == "--config-env" or argument.startswith(
            _DISALLOWED_GIT_CONFIG_PREFIXES
        ):
            return None
        if argument in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if argument.startswith(_GIT_GLOBAL_OPTIONS_WITH_INLINE_VALUE):
            index += 1
            continue
        if argument in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        if argument.startswith("-"):
            return None
        break

    if index >= len(argv):
        return "", []
    return argv[index], argv[index + 1 :]


class PolicyEngine:
    """Evaluate proposed actions against deterministic safety rules."""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or HarnessConfig()

    def evaluate(self, action: Action, workspace: Path) -> GovernanceDecision:
        fingerprint = action_fingerprint(action)

        if action.kind.value in _PATH_ACTIONS:
            requested = action.arguments.get("path")
            try:
                if not isinstance(requested, str):
                    raise ValueError("file action path must be a string")
                resolve_workspace_path(workspace, requested)
            except (OSError, RuntimeError, ValueError):
                return GovernanceDecision(
                    effect="deny",
                    rule_id="workspace.escape",
                    reason="The requested path is outside the workspace boundary.",
                    fingerprint=fingerprint,
                )

        if action.kind.value == "run_command":
            argv = action.arguments.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(argument, str) or not argument for argument in argv)
            ):
                return GovernanceDecision(
                    effect="deny",
                    rule_id="command.invalid_argv",
                    reason="Commands require a non-empty array of string arguments.",
                    fingerprint=fingerprint,
                )

            if any(
                metacharacter in argument
                for argument in argv
                for metacharacter in _SHELL_METACHARACTERS
            ):
                return GovernanceDecision(
                    effect="deny",
                    rule_id="command.shell_metacharacter",
                    reason="Shell metacharacters are not permitted in command arguments.",
                    fingerprint=fingerprint,
                )

            git_command: tuple[str, list[str]] | None = ("", [])
            if argv[0] == "git":
                git_command = _parse_git_subcommand(argv)
                if git_command is None:
                    return GovernanceDecision(
                        effect="deny",
                        rule_id="command.invalid_argv",
                        reason="The Git command has invalid or disallowed global options.",
                        fingerprint=fingerprint,
                    )

            rule_id: str | None = None
            if argv[0] == "rm" and any(
                argument in {"-r", "-R", "--recursive"}
                or (
                    argument.startswith("-")
                    and not argument.startswith("--")
                    and "r" in argument[1:].lower()
                )
                for argument in argv[1:]
            ):
                rule_id = "command.recursive_delete"
            elif git_command[0] == "reset" and "--hard" in git_command[1]:
                rule_id = "git.hard_reset"
            elif git_command[0] == "push" and any(
                argument in {"--force", "-f", "--force-with-lease"}
                for argument in git_command[1]
            ):
                rule_id = "git.force_push"
            else:
                rule_id = _dangerous_command_rule(argv)

            if rule_id is not None:
                effect = (
                    "require_approval"
                    if rule_id in self.config.approval_rule_ids
                    else "deny"
                )
                return GovernanceDecision(
                    effect=effect,
                    rule_id=rule_id,
                    reason="The command matches a destructive operation policy.",
                    fingerprint=fingerprint,
                )

            if argv[0] not in self.config.allowed_executables:
                return GovernanceDecision(
                    effect="deny",
                    rule_id="command.executable_not_allowed",
                    reason="The command executable is not in the configured allowlist.",
                    fingerprint=fingerprint,
                )

        return GovernanceDecision(
            effect="allow",
            rule_id="default.allow",
            reason="The action passed the configured policy rules.",
            fingerprint=fingerprint,
        )
