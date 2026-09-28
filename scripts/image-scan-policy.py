#!/usr/bin/env python3
"""Validate public Trivy reports and fail on fixable CRITICAL vulnerabilities."""

import json
import sys
from pathlib import Path


def main():
    try:
        report = json.loads(Path(sys.argv[1]).read_text())
        results = report["Results"]
        if not isinstance(results, list):
            raise ValueError
        high = critical = fixable = 0
        for result in results:
            if not isinstance(result, dict):
                raise ValueError
            vulnerabilities = result.get("Vulnerabilities") or []
            if not isinstance(vulnerabilities, list):
                raise ValueError
            for item in vulnerabilities:
                if not isinstance(item, dict) or item.get("Severity") not in ("HIGH", "CRITICAL"):
                    raise ValueError
                high += item["Severity"] == "HIGH"
                critical += item["Severity"] == "CRITICAL"
                fixable += item["Severity"] == "CRITICAL" and bool(item.get("FixedVersion"))
        print(
            json.dumps(
                {
                    "state": "scanned",
                    "high": high,
                    "critical": critical,
                    "fixable_critical": fixable,
                }
            )
        )
        return 1 if fixable else 0
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        print(json.dumps({"state": "unknown", "reason": "invalid vulnerability report"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
