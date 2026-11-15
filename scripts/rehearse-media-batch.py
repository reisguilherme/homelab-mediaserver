#!/usr/bin/env python3
"""Run only a synthetic Linux fixture; accepts no inventory or filesystem paths."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/control/src"))

from homeserver_control.migration_rehearsal import RehearsalScenario, run_rehearsal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--scenario",
        choices=[item.value for item in RehearsalScenario],
        default=RehearsalScenario.SUCCESS.value,
    )
    args = parser.parse_args()
    report = run_rehearsal(RehearsalScenario(args.scenario))
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
