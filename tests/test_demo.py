import json
from pathlib import Path
import subprocess
import sys

from forgeloop.demo import run_mechanism_demo


def test_demo_proves_all_required_mechanisms(tmp_path: Path) -> None:
    result = run_mechanism_demo(tmp_path)

    assert result.dangerous_action.effect == "require_approval"
    assert result.first_validation.status == "failed"
    assert result.feedback_seen_by_provider is True
    assert result.corrective_action.kind == "replace_text"
    assert result.final_status == "succeeded"
    assert result.no_progress_status == "no_progress"


def test_mechanism_demo_script_prints_the_same_json_contract() -> None:
    project_root = Path(__file__).parents[1]

    completed = subprocess.run(
        [sys.executable, "scripts/mechanism_demo.py", "--json"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dangerous_action"]["effect"] == "require_approval"
    assert payload["final_status"] == "succeeded"
