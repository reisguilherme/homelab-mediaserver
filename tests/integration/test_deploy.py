import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def local_tmp():
    path = Path(".runtime") / f"test-deploy-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_deploy_rejects_manifest_without_image_digests(local_tmp: Path) -> None:
    tmp_path = local_tmp
    bash = (
        r"C:\Program Files\Git\bin\bash.exe"
        if Path(r"C:\Program Files\Git\bin\bash.exe").exists()
        else shutil.which("bash")
    )
    if not Path(bash).exists():
        pytest.skip("bash is required")
    artifact = tmp_path / "release.tar"
    artifact.write_bytes(b"release")
    manifest = tmp_path / "release.json"
    manifest.write_text(
        '{"schema_version":1,"git_commit":"'
        + "1" * 40
        + (
            '","database_schema":3,"requires_backup":false,"images":{},'
            '"config_checksums":{},"artifact_sha256":"'
        )
        + "0" * 64
        + '"}',
        encoding="utf-8",
    )
    config = tmp_path / "deploy.env"

    def bash_path(path: Path) -> str:
        value = path.resolve().as_posix()
        return f"/{value[0].lower()}{value[2:]}" if len(value) > 1 and value[1] == ":" else value

    config.write_text(f"HOMESERVER_ROOT={bash_path(tmp_path / 'homeserver')}\n", encoding="utf-8")
    result = subprocess.run(
        [
            bash,
            bash_path(Path("scripts/deploy.sh")),
            "--release",
            "1" * 40,
            "--artifact",
            bash_path(artifact),
            "--config",
            bash_path(config),
            "--manifest",
            bash_path(manifest),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "homeserver" / "current").exists()
