import pytest

from forgeloop.config import HarnessConfig
from forgeloop.models import Action
from forgeloop.policy import PolicyEngine, action_fingerprint


def test_parent_escape_is_denied(tmp_path):
    decision = PolicyEngine().evaluate(
        Action(kind="read_file", arguments={"path": "../secret"}), tmp_path
    )

    assert decision.effect == "deny"
    assert decision.rule_id == "workspace.escape"


def test_symlink_escape_is_denied(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside)

    decision = PolicyEngine().evaluate(
        Action(kind="read_file", arguments={"path": "link"}), tmp_path
    )

    assert decision.effect == "deny"


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "build"],
        ["git", "reset", "--hard"],
        ["git", "push", "--force"],
    ],
)
def test_dangerous_commands_require_approval(tmp_path, argv):
    decision = PolicyEngine().evaluate(
        Action(
            kind="run_command",
            arguments={"argv": argv, "timeout_seconds": 60},
        ),
        tmp_path,
    )

    assert decision.effect == "require_approval"


@pytest.mark.parametrize(
    ("argv", "rule_id"),
    [
        (["git", "-C", "repo", "reset", "--hard"], "git.hard_reset"),
        (["git", "-C", "repo", "push", "--force"], "git.force_push"),
    ],
)
def test_git_global_options_cannot_bypass_dangerous_command_rules(
    tmp_path, argv, rule_id
):
    decision = PolicyEngine().evaluate(
        Action(
            kind="run_command",
            arguments={"argv": argv, "timeout_seconds": 60},
        ),
        tmp_path,
    )

    assert decision.effect == "require_approval"
    assert decision.rule_id == rule_id


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-c", "x=y", "push", "--force"],
        ["git", "-cx=y", "push", "--force"],
        ["git", "--config-env", "x=ENV_NAME", "status"],
        ["git", "--config-env=x=ENV_NAME", "status"],
    ],
)
def test_action_supplied_git_config_options_are_denied(tmp_path, argv):
    decision = PolicyEngine().evaluate(
        Action(
            kind="run_command",
            arguments={"argv": argv, "timeout_seconds": 60},
        ),
        tmp_path,
    )

    assert decision.effect == "deny"


@pytest.mark.parametrize("metacharacter", [";", "&", "(", ")", "\n", "\r"])
def test_shell_metacharacters_are_denied(tmp_path, metacharacter):
    decision = PolicyEngine().evaluate(
        Action(
            kind="run_command",
            arguments={
                "argv": ["pytest", metacharacter, "env"],
                "timeout_seconds": 60,
            },
        ),
        tmp_path,
    )

    assert decision.effect == "deny"


def test_non_recursive_rm_option_is_not_treated_as_recursive_delete(tmp_path):
    decision = PolicyEngine().evaluate(
        Action(
            kind="run_command",
            arguments={
                "argv": ["rm", "--force", "build"],
                "timeout_seconds": 60,
            },
        ),
        tmp_path,
    )

    assert decision.effect == "deny"
    assert decision.rule_id == "command.executable_not_allowed"


def test_action_fingerprint_is_stable_for_argument_key_order():
    first = Action(
        kind="remember",
        arguments={"key": "fact", "value": "ordered", "tags": ["a", "b"]},
    )
    second = Action(
        kind="remember",
        arguments={"tags": ["a", "b"], "value": "ordered", "key": "fact"},
    )

    first_fingerprint = action_fingerprint(first)

    assert first_fingerprint == action_fingerprint(second)
    assert len(first_fingerprint) == 64
    int(first_fingerprint, 16)


@pytest.mark.parametrize(
    ("action", "effect", "rule_id"),
    [
        (
            Action(
                kind="run_command",
                arguments={"argv": ["pytest", "-q"], "timeout_seconds": 60},
            ),
            "allow",
            "default.allow",
        ),
        (
            Action(
                kind="run_command",
                arguments={"argv": ["unknown-tool"], "timeout_seconds": 60},
            ),
            "deny",
            "command.executable_not_allowed",
        ),
        (
            Action(
                kind="run_command",
                arguments={
                    "argv": ["git", "reset", "--hard"],
                    "timeout_seconds": 60,
                },
            ),
            "require_approval",
            "git.hard_reset",
        ),
    ],
)
def test_command_decisions_include_rule_reason_and_fingerprint(
    tmp_path, action, effect, rule_id
):
    decision = PolicyEngine().evaluate(action, tmp_path)

    assert decision.effect == effect
    assert decision.rule_id == rule_id
    assert decision.reason
    assert decision.fingerprint == action_fingerprint(action)


@pytest.mark.parametrize(
    ("argv", "rule_id"),
    [
        (["dropdb", "application"], "database.drop"),
        (["psql", "-c", "DROP DATABASE application"], "database.drop"),
        (["sudo", "pytest", "-q"], "privilege.escalation"),
        (["chmod", "700", "deploy.sh"], "permission.change"),
        (
            ["python3", "-m", "pip", "install", "https://example.test/pkg.whl"],
            "network.execute",
        ),
    ],
)
def test_destructive_signatures_are_matched_before_executable_allowlist(
    tmp_path, argv, rule_id
):
    action = Action(
        kind="run_command",
        arguments={"argv": argv, "timeout_seconds": 60},
    )

    approved = PolicyEngine().evaluate(action, tmp_path)
    denied = PolicyEngine(HarnessConfig(approval_rule_ids=[])).evaluate(
        action, tmp_path
    )

    assert approved.effect == "require_approval"
    assert approved.rule_id == rule_id
    assert denied.effect == "deny"
    assert denied.rule_id == rule_id


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-c", "DROP DATABASE application"],
        ["python3", "-c", "https://example.test/pkg.whl"],
        ["pytest", "-k", "chmod"],
        ["git", "status", "--short"],
    ],
)
def test_dangerous_signature_words_do_not_match_safe_argument_boundaries(
    tmp_path, argv
):
    decision = PolicyEngine().evaluate(
        Action(
            kind="run_command",
            arguments={"argv": argv, "timeout_seconds": 60},
        ),
        tmp_path,
    )

    assert decision.effect == "allow"
    assert decision.rule_id == "default.allow"
