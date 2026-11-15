#!/usr/bin/env python3
"""Run an isolated native fixture; accepts no external resources or settings."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/control/src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--scenario", choices=("success", "unexpected_restart"), default="success")
    options = parser.parse_args()
    from homeserver_control.native_rehearsal import run_native_rehearsal

    report = run_native_rehearsal(options.scenario)
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
