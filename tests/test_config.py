import pytest

from forgeloop.config import ConfigError, HarnessConfig, ValidatorConfig, load_config


def test_load_config_rejects_unknown_field(tmp_path):
    path = tmp_path / "forgeloop.toml"
    path.write_text("mystery = true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="mystery"):
        load_config(path)


def test_default_config_has_bounded_budgets():
    cfg = HarnessConfig()

    assert cfg.model_dump() == {
        "max_steps": 20,
        "max_validation_runs": 8,
        "wall_time_seconds": 900,
        "command_timeout_seconds": 60,
        "provider_timeout_seconds": 60,
        "max_output_bytes": 32768,
        "max_file_bytes": 1048576,
        "max_identical_failures": 2,
        "max_identical_actions": 3,
        "memory_recall_limit": 10,
        "memory_char_budget": 4096,
        "allowed_executables": ["python3", "pytest", "ruff", "mypy", "git"],
        "approval_rule_ids": [
            "command.recursive_delete",
            "git.force_push",
            "git.hard_reset",
            "database.drop",
            "privilege.escalation",
            "permission.change",
            "network.execute",
        ],
        "validators": [],
        "provider_base_url": "https://api.openai.com/v1/chat/completions",
        "provider_model": "gpt-4.1-mini",
    }


def test_mutable_config_defaults_are_not_shared():
    first = HarnessConfig()
    second = HarnessConfig()

    first.allowed_executables.append("custom")
    first.approval_rule_ids.clear()
    first.validators.append(ValidatorConfig(argv=["pytest"]))

    assert "custom" not in second.allowed_executables
    assert second.approval_rule_ids
    assert second.validators == []


def test_load_config_parses_valid_toml(tmp_path):
    path = tmp_path / "forgeloop.toml"
    path.write_text(
        """
max_steps = 4
allowed_executables = ["python3"]

[[validators]]
argv = ["python3", "-m", "pytest", "-q"]
timeout_seconds = 45
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.max_steps == 4
    assert cfg.allowed_executables == ["python3"]
    assert cfg.validators == [
        ValidatorConfig(
            argv=["python3", "-m", "pytest", "-q"], timeout_seconds=45
        )
    ]
    assert cfg.provider_model == "gpt-4.1-mini"


@pytest.mark.parametrize(
    ("document", "field_path"),
    [
        ("max_steps = 0\n", "max_steps"),
        ("command_timeout_seconds = 121\n", "command_timeout_seconds"),
        ("provider_timeout_seconds = 0\n", "provider_timeout_seconds"),
        ("max_identical_failures = 1\n", "max_identical_failures"),
        ("max_identical_actions = 1\n", "max_identical_actions"),
        ("allowed_executables = []\n", "allowed_executables"),
        ("allowed_executables = [\"\"]\n", "allowed_executables.0"),
        ("provider_base_url = \"http://example.com/v1\"\n", "provider_base_url"),
        ("provider_model = \"\"\n", "provider_model"),
        (
            "[[validators]]\nargv = [\"pytest\"]\ntimeout_seconds = 0\n",
            "validators.0.timeout_seconds",
        ),
        ("max_steps = \"20\"\n", "max_steps"),
    ],
)
def test_load_config_reports_invalid_field_path(tmp_path, document, field_path):
    path = tmp_path / "forgeloop.toml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match=field_path.replace(".", r"\.")):
        load_config(path)


def test_validator_rejects_unknown_field_with_nested_path(tmp_path):
    path = tmp_path / "forgeloop.toml"
    path.write_text(
        '[[validators]]\nargv = ["pytest"]\nmystery = true\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=r"validators\.0\.mystery"):
        load_config(path)


def test_load_config_wraps_malformed_toml(tmp_path):
    path = tmp_path / "forgeloop.toml"
    path.write_text("max_steps = [\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)
