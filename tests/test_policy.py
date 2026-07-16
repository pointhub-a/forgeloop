import pytest

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
        Action(kind="run_command", arguments={"argv": argv}), tmp_path
    )

    assert decision.effect == "require_approval"


def test_shell_metacharacters_are_denied(tmp_path):
    decision = PolicyEngine().evaluate(
        Action(kind="run_command", arguments={"argv": ["pytest", ";", "env"]}),
        tmp_path,
    )

    assert decision.effect == "deny"


def test_non_recursive_rm_option_is_not_treated_as_recursive_delete(tmp_path):
    decision = PolicyEngine().evaluate(
        Action(kind="run_command", arguments={"argv": ["rm", "--force", "build"]}),
        tmp_path,
    )

    assert decision.effect == "deny"
    assert decision.rule_id == "command.executable_not_allowed"


def test_action_fingerprint_is_stable_for_argument_key_order():
    first = Action(kind="remember", arguments={"key": "fact", "value": {"b": 2, "a": 1}})
    second = Action(kind="remember", arguments={"value": {"a": 1, "b": 2}, "key": "fact"})

    first_fingerprint = action_fingerprint(first)

    assert first_fingerprint == action_fingerprint(second)
    assert len(first_fingerprint) == 64
    int(first_fingerprint, 16)


@pytest.mark.parametrize(
    ("action", "effect", "rule_id"),
    [
        (
            Action(kind="run_command", arguments={"argv": ["pytest", "-q"]}),
            "allow",
            "default.allow",
        ),
        (
            Action(kind="run_command", arguments={"argv": ["unknown-tool"]}),
            "deny",
            "command.executable_not_allowed",
        ),
        (
            Action(kind="run_command", arguments={"argv": ["git", "reset", "--hard"]}),
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
