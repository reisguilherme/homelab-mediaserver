"""Isolated native rehearsals must never inherit production state or ownership."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rehearse-core-native.py"
IMAGE = "sha256:" + "a" * 64
IDENTIFIER = "b" * 64
NONCE = "c" * 32
ROLES = ("sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "seerr", "byparr")


@pytest.fixture
def runner():
    assert SCRIPT.is_file(), "core native rehearsal runner is missing"
    spec = importlib.util.spec_from_file_location("core_native_rehearsal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("role", ROLES)
def test_create_is_fixed_isolated_and_has_clean_native_environment(runner, role):
    recipe = runner.recipe(role)
    command = runner.create_arguments(role, IMAGE, NONCE)
    assert command[0] == "create"
    assert command[command.index("--entrypoint") + 1] == "/usr/bin/env"
    assert command[command.index(IMAGE) + 1:] == ["-i", *recipe["env"], *recipe["argv"]]
    for option, value in (("--pull", "never"), ("--network", "none"), ("--restart", "no"),
                          ("--user", "1000:1000"), ("--cap-drop", "ALL"),
                          ("--cpus", "1"), ("--pids-limit", "128")):
        assert command[command.index(option) + 1] == value
    assert "--read-only" in command and "--no-healthcheck" in command
    assert not {"--mount", "--volume", "-v", "--privileged", "--publish", "-p", "--env-file",
                "--device", "--init", "--rm"}.intersection(command)
    assert recipe["memory"] == (768 if role == "byparr" else 512) * 1024**2
    assert all("/data" not in target for target in recipe["tmpfs"])
    assert not any(part in {"/init", "npm", "uv", "--daemon", "-d"}
                   for part in recipe["argv"])


@pytest.mark.parametrize("image", ["latest", "repo:tag", "sha256:" + "A" * 64, "a" * 64,
                                   "sha256:" + "a" * 63])
def test_unpinned_image_is_rejected_before_docker(runner, image):
    with pytest.raises(runner.Hold):
        runner.create_arguments("sonarr", image, NONCE)


def test_unknown_role_or_nonce_is_rejected(runner):
    for role, nonce in (("jellyfin", NONCE), ("sonarr", "../other")):
        with pytest.raises(runner.Hold):
            runner.create_arguments(role, IMAGE, nonce)


def snapshot(runner, role="sonarr"):
    plan = runner.recipe(role)
    return {
        "Id": IDENTIFIER, "Image": IMAGE, "Path": "/usr/bin/env",
        "Args": ["-i", *plan["env"], *plan["argv"]],
        "Config": {"User": "1000:1000", "WorkingDir": plan["cwd"],
                   "Entrypoint": ["/usr/bin/env"], "Cmd": ["-i", *plan["env"], *plan["argv"]],
                   "Labels": {"homeserver.core.rehearsal": NONCE, "homeserver.core.role": role},
                   "Healthcheck": {"Test": ["NONE"]}},
        "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                       "CapAdd": None, "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges:true"],
                       "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                       "Memory": plan["memory"], "MemorySwap": plan["memory"],
                       "NanoCpus": 1_000_000_000, "PidsLimit": 128, "PortBindings": {},
                       "PublishAllPorts": False, "Binds": None, "Mounts": None,
                       "Devices": [], "DeviceRequests": None, "PidMode": "",
                       "IpcMode": "private", "UTSMode": "", "UsernsMode": "",
                       "Tmpfs": plan["tmpfs"], "AutoRemove": False,
                       "LogConfig": {"Type": "none", "Config": {}},
                       "VolumesFrom": None, "Links": None, "ExtraHosts": None},
        "Mounts": [], "NetworkSettings": {"Ports": {}, "Networks": {"none": {}}},
        "State": {"Status": "created", "Running": False, "Restarting": False,
                  "Paused": False, "Pid": 0, "ExitCode": 0, "OOMKilled": False,
                  "StartedAt": "zero", "FinishedAt": "zero"},
    }


@pytest.mark.parametrize("role", ROLES)
def test_exact_container_policy_is_accepted(runner, role):
    assert runner.validate_snapshot(snapshot(runner, role), IDENTIFIER, IMAGE, role, NONCE)


@pytest.mark.parametrize("attack", ["identity", "image", "nonce", "bind", "volume", "network",
                                    "port", "writable", "cap", "health", "command", "restart",
                                    "memory", "tmpfs", "privileged", "logs", "hostpid"])
def test_changed_container_policy_is_rejected_before_action(runner, attack):
    row = snapshot(runner)
    if attack == "identity":
        row["Id"] = "d" * 64
    elif attack == "image":
        row["Image"] = "sha256:" + "d" * 64
    elif attack == "nonce":
        row["Config"]["Labels"]["homeserver.core.rehearsal"] = "other"
    elif attack in {"bind", "volume"}:
        row["Mounts"] = [{"Type": attack, "Source": "/real/state", "Destination": "/config"}]
    elif attack == "network":
        row["HostConfig"]["NetworkMode"] = "host"
    elif attack == "port":
        row["HostConfig"]["PortBindings"] = {"8080/tcp": [{"HostPort": "8080"}]}
    elif attack == "writable":
        row["HostConfig"]["ReadonlyRootfs"] = False
    elif attack == "cap":
        row["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    elif attack == "health":
        row["Config"]["Healthcheck"]["Test"] = ["CMD", "/init"]
    elif attack == "command":
        row["Args"] = ["/init"]
    elif attack == "restart":
        row["HostConfig"]["RestartPolicy"]["Name"] = "always"
    elif attack == "memory":
        row["HostConfig"]["Memory"] = 0
    elif attack == "tmpfs":
        row["HostConfig"]["Tmpfs"]["/config"] = "rw"
    elif attack == "privileged":
        row["HostConfig"]["Privileged"] = True
    elif attack == "logs":
        row["HostConfig"]["LogConfig"]["Type"] = "json-file"
    elif attack == "hostpid":
        row["HostConfig"]["PidMode"] = "host"
    with pytest.raises(runner.Hold):
        runner.validate_snapshot(row, IDENTIFIER, IMAGE, "sonarr", NONCE)


def test_recipe_results_do_not_share_mutable_state(runner):
    value = runner.recipe("sonarr")
    value["tmpfs"].clear()
    assert runner.recipe("sonarr")["tmpfs"]


class MemoryEvidence:
    def __init__(self, fail_ownership=False):
        self.files = {}
        self.fail_ownership = fail_ownership

    def write(self, name, value):
        if name == "ownership.json" and self.fail_ownership:
            raise OSError("PRIVATE_SENTINEL")
        assert name not in self.files
        self.files[name] = copy.deepcopy(value)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


class FakeDocker:
    def __init__(self, runner, evidence, *, stubborn=False):
        self.runner = runner
        self.evidence = evidence
        self.row = snapshot(runner)
        self.calls = []
        self.stubborn = stubborn
        self.after_start_error = False
        self.create_reply = (IDENTIFIER + "\n").encode()

    def validate_image(self, image, role):
        assert image == IMAGE and role == "sonarr"

    def inspect(self, identifier):
        assert identifier == IDENTIFIER
        self.calls.append(["inspect", identifier])
        return copy.deepcopy(self.row)

    def call(self, args):
        self.calls.append(args)
        if args[0] == "create":
            return self.create_reply
        assert args[-1] == IDENTIFIER
        assert "ownership.json" in self.evidence.files
        if args[0] == "start":
            self.row["State"].update(Running=True, Status="running", Pid=12345)
            if self.after_start_error:
                raise self.runner.Hold("command_timeout")
        elif args[0] == "kill":
            if args[2] == "KILL" or not self.stubborn:
                self.row["State"].update(Running=False, Status="exited", Pid=0)
        elif args[0] == "rm":
            assert args == ["rm", "--volumes", IDENTIFIER]
            assert not self.row["State"]["Running"] and self.row["State"]["Pid"] == 0
        return b""


class FakeObserver:
    def __init__(self, docker, ready=True):
        self.docker = docker
        self.is_ready = ready
        self.tree_calls = 0

    def tree(self, identifier, leader_pid):
        assert identifier == IDENTIFIER and leader_pid == 12345
        self.tree_calls += 1
        return [{"pid": 12345, "start_ticks": 123, "state": "S", "ppid": 100,
                 "pgid": 12345, "sid": 12345},
                {"pid": 12346, "start_ticks": 124, "state": "S", "ppid": 12345,
                 "pgid": 12345, "sid": 12345}]

    def ready(self, leader, role):
        assert leader["pid"] == 12345 and role == "sonarr"
        return self.is_ready

    def gone(self, identities):
        return not self.docker.row["State"]["Running"]


def exercise(runner, *, ready=True, stubborn=False, evidence=None, docker=None, observer=None):
    evidence = evidence or MemoryEvidence()
    docker = docker or FakeDocker(runner, evidence, stubborn=stubborn)
    observer = observer or FakeObserver(docker, ready=ready)
    clock = FakeClock()
    result = runner.rehearse("sonarr", IMAGE, evidence, docker, observer, nonce=NONCE,
                            startup_seconds=2, stop_seconds=1, clock=clock, sleep=clock.sleep)
    return result, docker, evidence


def test_success_has_two_tree_samples_durable_ownership_and_only_own_cleanup(runner):
    result, docker, evidence = exercise(runner)
    assert result["status"] == "PASS"
    assert result["http_ready"] and result["foreground_stable"] and result["graceful_stop"]
    assert len(result["process_samples"]) == 2
    assert len(result["process_samples"][0]) == 2
    assert result["removed"] and not result["retained"]
    assert not result["production_ready"] and not result["under_writer_guard_verified"]
    assert not result["restored_state_tested"]
    assert evidence.files["ownership.json"]["container_id"] == IDENTIFIER
    mutations = [call for call in docker.calls if call[0] in {"start", "kill", "rm"}]
    assert mutations == [["start", IDENTIFIER], ["kill", "--signal", "TERM", IDENTIFIER],
                         ["rm", "--volumes", IDENTIFIER]]
    for index, call in enumerate(docker.calls):
        if call[0] in {"start", "kill", "rm"}:
            assert docker.calls[index - 1] == ["inspect", IDENTIFIER]


def test_readiness_failure_still_stops_and_removes_own_fixture(runner):
    result, _, _ = exercise(runner, ready=False)
    assert result["status"] == "HOLD" and result["code"] == "native_readiness_timeout"
    assert result["removed"] and result["graceful_stop"]


def test_stubborn_fixture_gets_owned_kill_and_never_passes(runner):
    result, docker, _ = exercise(runner, stubborn=True)
    assert result["status"] == "HOLD" and result["kill_required"]
    assert result["removed"] and not result["graceful_stop"]
    assert ["kill", "--signal", "KILL", IDENTIFIER] in docker.calls


def test_start_timeout_with_started_fixture_is_cleaned_up(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)
    docker.after_start_error = True
    result, _, _ = exercise(runner, evidence=evidence, docker=docker)
    assert result["status"] == "HOLD" and result["code"] == "command_timeout"
    assert result["removed"] and result["graceful_stop"]


def test_ambiguous_create_never_guesses_container_from_name(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)
    docker.create_reply = b"PRIVATE_SENTINEL not-an-id"
    result, _, _ = exercise(runner, evidence=evidence, docker=docker)
    assert result["retained"] and result["code"] == "create_identity_ambiguous"
    assert [call[0] for call in docker.calls] == ["create"]
    assert "PRIVATE_SENTINEL" not in json.dumps(evidence.files)


def test_failed_ownership_persistence_prevents_start_and_cleanup_mutations(runner):
    evidence = MemoryEvidence(fail_ownership=True)
    result, docker, _ = exercise(runner, evidence=evidence)
    assert result["retained"] and not result["removed"]
    assert not any(call[0] in {"start", "kill", "rm"} for call in docker.calls)
    assert "PRIVATE_SENTINEL" not in json.dumps(evidence.files)


def test_identity_loss_during_rehearsal_forbids_stop_or_removal(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)
    observer = FakeObserver(docker)

    def lose_identity(leader, role):
        docker.row["Config"]["Labels"]["homeserver.core.rehearsal"] = "another-owner"
        return False

    observer.ready = lose_identity
    result, _, _ = exercise(runner, evidence=evidence, docker=docker, observer=observer)
    assert result["retained"] and result["status"] == "HOLD"
    assert not any(call[0] in {"kill", "rm"} for call in docker.calls)


def test_descendant_surviving_container_exit_prevents_remove(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)
    observer = FakeObserver(docker)
    observer.gone = lambda _: False
    result, _, _ = exercise(runner, evidence=evidence, docker=docker, observer=observer)
    assert result["retained"] and result["cleanup_code"] == "descendants_remain"
    assert not any(call[0] == "rm" for call in docker.calls)


def test_tree_observation_failure_still_stops_identified_fixture(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)
    observer = FakeObserver(docker)

    def fail_tree(*_):
        raise runner.Hold("process_tree_invalid")

    observer.tree = fail_tree
    result, _, _ = exercise(runner, evidence=evidence, docker=docker, observer=observer)
    assert result["status"] == "HOLD" and result["removed"]
    assert ["kill", "--signal", "TERM", IDENTIFIER] in docker.calls


@pytest.mark.parametrize("running,pid", [(False, 12), (True, 0)])
def test_inconsistent_state_is_rejected(runner, running, pid):
    row = snapshot(runner)
    row["State"].update(Running=running, Pid=pid)
    with pytest.raises(runner.Hold):
        runner.validate_snapshot(row, IDENTIFIER, IMAGE, "sonarr", NONCE)


def test_seerr_masks_its_default_config_even_if_environment_is_ignored(runner):
    assert "/app/config" in runner.recipe("seerr")["tmpfs"]


@pytest.mark.skipif(sys.platform != "linux", reason="real procfs identity")
def test_process_identity_disappears_after_real_child_is_reaped(runner):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"],
                             start_new_session=True)
    try:
        identity = runner.process_identity(child.pid)
        assert identity["pid"] == child.pid and identity["pgid"] == child.pid
        assert len(identity["exe_sha256"]) == 64 and len(identity["cgroup_sha256"]) == 64
        assert runner.same_process(identity)
        wrong_identity = {**identity, "start_ticks": identity["start_ticks"] + 1}
        assert not runner.same_process(wrong_identity)
    finally:
        child.terminate()
        child.wait(timeout=3)
    assert not runner.same_process(identity)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-private real filesystem fixture")
def test_evidence_is_exclusive_private_and_rejects_links(runner):
    with tempfile.TemporaryDirectory(prefix="core-rehearsal-test-", dir="/root") as parent:
        target = Path(parent) / "evidence"
        evidence = runner.Evidence(target)
        try:
            evidence.write("ownership.json", {"synthetic": True})
            assert target.stat().st_mode & 0o777 == 0o700
            assert (target / "ownership.json").stat().st_mode & 0o777 == 0o600
            with pytest.raises(FileExistsError):
                evidence.write("ownership.json", {})
            with pytest.raises(runner.Hold):
                runner.Evidence(target)
            link = Path(parent) / "linked"
            link.symlink_to(target, target_is_directory=True)
            with pytest.raises(runner.Hold):
                runner.Evidence(link / "new")
        finally:
            evidence.close()


def test_probe_implementation_contains_no_application_credentials_or_mutations(runner):
    assert "connection.request(\"GET\"" in runner.HTTP_PROBE
    assert "response.read(65537)" in runner.HTTP_PROBE
    assert not any(word in runner.HTTP_PROBE for word in ["POST", "Authorization", "password"])


@pytest.mark.parametrize("kind,body,ready", [
    ("arr", b'{"status":"OK"}', True),
    ("arr", b'{"status":"not ready"}', False),
    ("arr", b'{"status":"OK","secret":"PRIVATE_SENTINEL"}', True),
    ("seerr", b'{"version":"3.5.0"}', True),
    ("seerr", b'[]', False),
    ("openapi", b'{"openapi":"3.1.0"}', True),
    ("html", b'<!doctype html><title>fixture</title>', True),
    ("html", b'not html', False),
    ("arr", b'{"status":"OK"}' + b' ' * 65536, False),
])
def test_http_probe_real_loopback_is_bounded_and_never_returns_body(runner, kind, body, ready):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = runner.bounded_command([sys.executable, "-I", "-B", "-c", runner.HTTP_PROBE,
                                         str(server.server_port), "/", kind], timeout=4)
        assert json.loads(output) == {"ready": ready}
        assert b"PRIVATE_SENTINEL" not in output
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_command_output_limit_is_enforced_before_child_finishes(runner):
    script = "import os,time;os.write(1,b'x'*(2*1024*1024));time.sleep(10)"
    start = time.monotonic()
    with pytest.raises(runner.Hold, match="command_output_too_large"):
        runner.bounded_command([sys.executable, "-I", "-c", script], timeout=3)
    assert time.monotonic() - start < 2


def test_command_timeout_is_bounded_without_echoing_child_stderr(runner):
    script = "import sys,time;print('PRIVATE_SENTINEL',file=sys.stderr);time.sleep(10)"
    with pytest.raises(runner.Hold, match="^command_timeout$"):
        runner.bounded_command([sys.executable, "-I", "-c", script], timeout=0.1)


def test_http_namespace_is_pinned_without_entering_mount_namespace(runner, monkeypatch):
    identity = runner.process_identity(os.getpid())
    captured = {}

    def fake_command(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        descriptor = kwargs["pass_fds"][0]
        assert os.fstat(descriptor).st_ino == os.stat(f"/proc/{os.getpid()}/ns/net").st_ino
        return b'{"ready":true}'

    monkeypatch.setattr(runner, "bounded_command", fake_command)
    assert runner.HostObserver(None).ready(identity, "sonarr")
    args = captured["args"]
    assert args[0] == "/usr/bin/nsenter" and args[1].startswith("--net=/proc/self/fd/")
    assert not {"-m", "--mount", "-t", "--target"}.intersection(args)
    with pytest.raises(OSError):
        os.fstat(captured["kwargs"]["pass_fds"][0])


@pytest.mark.parametrize("volumes", [{"/unexpected": {}}, [], "PRIVATE_SENTINEL"])
def test_uncovered_image_volume_never_reaches_create(runner, volumes):
    docker = runner.Docker(Path("/synthetic/config"))
    docker.call = lambda _: json.dumps({"Id": IMAGE, "Volumes": volumes}).encode()
    with pytest.raises(runner.Hold):
        docker.validate_image(IMAGE, "sonarr")


def test_image_inspection_omits_environment(runner):
    docker = runner.Docker(Path("/synthetic/config"))
    calls = []

    def call(args):
        calls.append(args)
        return json.dumps({"Id": IMAGE, "Volumes": {"/config": {}}}).encode()

    docker.call = call
    docker.validate_image(IMAGE, "sonarr")
    assert "Env" not in json.dumps(calls)
    assert ".Env" not in runner.INSPECT_FORMAT


def test_foreground_executable_change_after_readiness_cannot_pass(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)
    observer = FakeObserver(docker)
    old_tree = observer.tree

    def changed_tree(*args):
        result = old_tree(*args)
        result[0]["exe_sha256"] = str(observer.tree_calls)
        result[0]["cgroup_sha256"] = "constant"
        return result

    observer.tree = changed_tree
    result, _, _ = exercise(runner, evidence=evidence, docker=docker, observer=observer)
    assert result["status"] == "HOLD" and result["removed"]
    assert result["code"] == "foreground_executable_changed"


def test_image_without_declared_volumes_uses_missing_safe_docker_lookup(runner):
    docker = runner.Docker(Path("/synthetic/config"))

    def call(args):
        template = args[args.index("--format") + 1]
        # Docker's strict template evaluator raises on .Config.Volumes when the
        # image has no Volumes member. index returns null for that legitimate case.
        if ".Config.Volumes" in template:
            raise runner.Hold("command_failed")
        assert 'index .Config "Volumes"' in template
        return json.dumps({"Id": IMAGE, "Volumes": None}).encode()

    docker.call = call
    docker.validate_image(IMAGE, "seerr")


def test_metadata_failure_before_create_does_not_claim_unknown_fixture(runner):
    evidence = MemoryEvidence()
    docker = FakeDocker(runner, evidence)

    def unavailable(*_):
        raise runner.Hold("command_failed")

    docker.validate_image = unavailable
    result, _, _ = exercise(runner, evidence=evidence, docker=docker)
    assert result["status"] == "HOLD" and result["code"] == "command_failed"
    assert not result["retained"] and not result["removed"]
    assert docker.calls == [] and "cleanup_code" not in result
