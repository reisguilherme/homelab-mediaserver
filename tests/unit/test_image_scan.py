import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check-images.sh"


def test_image_scanner_requires_explicit_inventory():
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_image_scanner_rejects_option_as_image(tmp_path):
    result = subprocess.run(["bash", str(SCRIPT), "--all"], capture_output=True, text=True)
    assert result.returncode == 2


def test_scan_policy_reports_fixable_critical_only(tmp_path):
    # Exercise the public report-policy parser independently of an external DB.
    policy = Path(__file__).resolve().parents[2] / "scripts/image-scan-policy.py"
    report = tmp_path / "scan.json"
    report.write_text(
        json.dumps(
            {
                "Results": [
                    {
                        "Vulnerabilities": [
                            {"VulnerabilityID": "fixture", "Severity": "HIGH", "FixedVersion": "2"},
                            {
                                "VulnerabilityID": "fixture-critical",
                                "Severity": "CRITICAL",
                                "FixedVersion": "",
                            },
                        ]
                    }
                ]
            }
        )
    )
    assert subprocess.run(["python3", str(policy), str(report)]).returncode == 0
    report.write_text(
        json.dumps(
            {
                "Results": [
                    {
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": "fixture-critical",
                                "Severity": "CRITICAL",
                                "FixedVersion": "2",
                            }
                        ]
                    }
                ]
            }
        )
    )
    assert subprocess.run(["python3", str(policy), str(report)]).returncode == 1
    report.write_text("{}")
    assert subprocess.run(["python3", str(policy), str(report)]).returncode == 3


@pytest.mark.parametrize(
    "scenario, expected", [("database_failure", 3), ("critical", 1), ("clean", 0)]
)
def test_script_database_and_finding_statuses(tmp_path, scenario, expected):
    tools = tmp_path / "bin"
    tools.mkdir()
    archive = tmp_path / "fixture.tar.gz"
    vulnerabilities = (
        [{"Severity": "CRITICAL", "FixedVersion": "2"}] if scenario == "critical" else []
    )
    report = json.dumps({"Results": [{"Vulnerabilities": vulnerabilities}]})
    database_status = 1 if scenario == "database_failure" else 0
    script = (
        '#!/bin/sh\nfor arg in "$@"; do\n'
        f'  if [ "$arg" = "--download-db-only" ]; then exit {database_status}; fi\ndone\n'
        'while [ "$1" != "--output" ]; do shift; done\n'
        f"printf '%s' '{report}' > \"$2\"\n"
    ).encode()
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("trivy")
        info.size, info.mode = len(script), 0o755
        output.addfile(info, io.BytesIO(script))
    fixtures = {
        "docker": '#!/bin/sh\nif [ "$2" = "inspect" ]; then echo sha256:'
        + "0" * 64
        + '\nelse : > "$4"; fi\n',
        "curl": f'#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\ncp "{archive}" "$2"\n',
        # Fixture transport substitutes the official archive; production still pins its SHA.
        "sha256sum": "#!/bin/sh\ncat >/dev/null\nexit 0\n",
    }
    for name, contents in fixtures.items():
        path = tools / name
        path.write_text(contents)
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--output-dir", str(tmp_path / "reports"), "fixture:dev"],
        env={**os.environ, "PATH": str(tools) + ":" + os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected
    if expected == 3:
        assert "unknown" in result.stderr
        assert json.loads((tmp_path / "reports/scan-status.json").read_text())["state"] == "unknown"
        assert not list((tmp_path / "reports").glob("image-*.json"))
    else:
        assert len(list((tmp_path / "reports").glob("*.json"))) == 4
        assert json.loads((tmp_path / "reports/scan-status.json").read_text())["state"] == "scanned"
