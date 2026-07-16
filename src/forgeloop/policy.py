"""Deterministic governance for proposed ForgeLoop actions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from forgeloop.config import HarnessConfig
from forgeloop.models import Action, GovernanceDecision


_PATH_ACTIONS = {"read_file", "write_file", "replace_text"}
_SHELL_METACHARACTERS = (";", "&&", "|", ">", "<", "`", "$(")


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
            elif argv[:2] == ["git", "reset"] and "--hard" in argv[2:]:
                rule_id = "git.hard_reset"
            elif argv[:2] == ["git", "push"] and any(
                argument in {"--force", "-f", "--force-with-lease"}
                for argument in argv[2:]
            ):
                rule_id = "git.force_push"

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
