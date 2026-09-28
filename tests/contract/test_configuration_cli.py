import json

from homeserver_common.cli import main


def test_init_validate_show_and_no_overwrite(tmp_path, capsys):
    path = tmp_path / ".env"
    assert main(["env", "init", "--env-file", str(path), "--mode", "dev"]) == 0
    capsys.readouterr()
    assert main(["config", "validate", "--env-file", str(path), "--mode", "dev"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["config", "show", "--redacted", "--env-file", str(path), "--mode", "dev"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admin_password"] == "[redacted]"
    original = path.read_bytes()
    assert main(["env", "init", "--env-file", str(path)]) == 2
    assert original == path.read_bytes()
