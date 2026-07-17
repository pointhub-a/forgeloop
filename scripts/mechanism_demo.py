#!/usr/bin/env python3
"""Run ForgeLoop's deterministic offline mechanism demonstration."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

from forgeloop.demo import run_mechanism_demo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="forgeloop-script-") as temporary:
        result = run_mechanism_demo(Path(temporary))
    if args.as_json:
        print(result.model_dump_json(indent=2))
    else:
        print(
            "ForgeLoop mechanism demo: "
            f"final={result.final_status.value}, "
            f"no_progress={result.no_progress_status.value}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
