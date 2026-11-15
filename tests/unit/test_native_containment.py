"""Safety properties of the isolated native clone boundary."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from homeserver_control.native_containment import (
    ContainmentError,
    Docker,
    OwnedContainer,
    OwnedSet,
)


def fixture_set():
    owned = [
        OwnedContainer(
            str(i) * 64,
            "sha256:" + "a" * 64,
            "nonce",
            role,
            ((f"/tmp/private/{role}", "/config", True),),
        )
        for i, role in [(1, "jellyfin"), (2, "sonarr")]
    ]
    states = {
        item.identifier: {
            "Id": item.identifier,
            "Image": item.image,
            "Config": {
                "Labels": {"homeserver.native.nonce": "nonce", "homeserver.native.role": item.role}
            },
            "Mounts": [
                {"Type": "bind", "Source": item.binds[0][0], "Destination": "/config", "RW": True}
            ],
            "State": {
                "Running": True,
                "Restarting": False,
                "Paused": False,
                "Pid": 10,
                "StartedAt": "start1",
                "FinishedAt": "zero",
            },
        }
        for item in owned
    }

    class FakeDocker:
        mutations = []
        failure = False

        def inspect(self, identifiers):
            if self.failure:
                raise ContainmentError("docker_unavailable")
            return [copy.deepcopy(states[x]) for x in identifiers]

        def command(self, *args, **kwargs):
            self.mutations.append(args)
            if args[0] == "stop":
                state = states[args[-1]]["State"]
                state.update(Running=False, Pid=0, FinishedAt="finish1")
            return ""

    docker = FakeDocker()
    docker.mutations = []
    return OwnedSet(docker, owned), docker, states


@pytest.mark.parametrize("attack", ["id", "image", "nonce", "bind", "extra_bind"])
def test_entire_set_is_validated_before_any_stop(attack):
    group, docker, states = fixture_set()
    last = states[group.owned[-1].identifier]
    if attack == "id":
        last["Id"] = "3" * 64
    elif attack == "image":
        last["Image"] = "sha256:" + "b" * 64
    elif attack == "nonce":
        last["Config"]["Labels"]["homeserver.native.nonce"] = "other"
    elif attack == "bind":
        last["Mounts"][0]["Source"] = "/real/appdata"
    else:
        last["Mounts"].append(
            {"Type": "bind", "Source": "/extra", "Destination": "/extra", "RW": True}
        )
    with pytest.raises(ContainmentError):
        group.stop()
    assert docker.mutations == []


def test_stopped_guard_detects_restart_and_stop_before_effect():
    group, docker, states = fixture_set()
    guard = group.stop()
    guard()
    state = states[group.owned[0].identifier]["State"]
    state.update(StartedAt="start2", FinishedAt="finish2")
    effects = []
    with pytest.raises(ContainmentError, match="containment_changed"):
        guard()
        effects.append("capture")
    assert not effects
    assert all(command[:3] == ("stop", "--timeout", "-1") for command in docker.mutations)


def test_visibility_failure_retains_everything_without_remove():
    group, docker, _ = fixture_set()
    docker.failure = True
    assert group.cleanup() is False
    assert docker.mutations == []


def test_stop_failure_never_removes_or_force_kills():
    group, docker, _ = fixture_set()

    def fail(*args, **kwargs):
        docker.mutations.append(args)
        raise ContainmentError("docker_failed")

    docker.command = fail
    assert group.cleanup() is False
    assert docker.mutations == [("stop", "--timeout", "-1", group.owned[0].identifier)]


def test_docker_transport_ignores_external_environment_and_uses_local_socket(monkeypatch):
    seen = []
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")

    def runner(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="[{}]", stderr="")

    docker = Docker(runner=runner, executable="/usr/bin/docker")
    docker.inspect(["1" * 64])
    args, kwargs = seen[0]
    assert args[:3] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
    assert "DOCKER_HOST" not in kwargs["env"]
    assert "shell" not in kwargs or not kwargs["shell"]


@pytest.mark.parametrize(
    "option",
    ["--source", "--destination", "--container-id", "--docker-host", "--compose-project", "--env"],
)
def test_cli_rejects_external_inputs(option):
    import sys

    script = Path(__file__).parents[2] / "scripts/rehearse-native-checkpoint.py"
    result = subprocess.run(
        [sys.executable, str(script), option, "/outside"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_public_report_contains_no_private_values():
    from dataclasses import asdict

    from homeserver_control.native_rehearsal import NativeReport

    report = asdict(NativeReport())
    assert report["partial_restart_safe"] is False
    assert report["production_ready"] is False
    assert report["scope"] == "isolated_native_fixture"
    assert "/tmp/" not in json.dumps(report)


@pytest.mark.parametrize("output", ["not-json", "{}", "[]", "[{}, {}]"])
def test_docker_ambiguous_inspect_is_rejected(output):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="private")

    with pytest.raises(ContainmentError, match="docker_ambiguous"):
        Docker(runner=runner, executable="/usr/bin/docker").inspect(["1" * 64])


def test_running_after_stop_retains_all_containers():
    group, docker, _ = fixture_set()
    docker.command = lambda *args: docker.mutations.append(args)
    assert group.cleanup() is False
    assert all(command[0] == "stop" for command in docker.mutations)


def test_cleanup_validates_entire_set_before_first_removal():
    group, docker, states = fixture_set()
    group.stop()
    docker.mutations.clear()
    states[group.owned[-1].identifier]["Image"] = "sha256:" + "b" * 64
    assert group.cleanup() is False
    assert not docker.mutations


def test_failed_stop_latches_hold_even_if_docker_later_recovers():
    group, docker, _ = fixture_set()
    original = docker.command

    def failure(*args, **kwargs):
        raise ContainmentError("docker_failed")

    docker.command = failure
    with pytest.raises(ContainmentError):
        group.stop()
    docker.command = original
    assert group.cleanup() is False
    assert docker.mutations == []


def test_http_200_without_version_is_not_native_readiness(monkeypatch):
    from homeserver_control.native_rehearsal_apps import NativeApps

    apps = NativeApps(None, {})
    responses = iter([{}, {"Version": "12.2.0"}])
    apps.request = lambda *args: next(responses)
    monkeypatch.setattr("homeserver_control.native_rehearsal_apps.time.sleep", lambda _: None)
    assert apps.wait("jellyfin", "/System/Info/Public") == {"Version": "12.2.0"}


def test_jellyfin_observed_public_field_aliases_reject_conflicts():
    from homeserver_control.native_rehearsal_apps import _public_field

    assert _public_field({"version": "12.2.0"}, "Version") == "12.2.0"
    assert _public_field({"Version": "12.2.0"}, "Version") == "12.2.0"
    with pytest.raises(ContainmentError, match="native_schema_ambiguous"):
        _public_field({"version": "12.2.0", "Version": "other"}, "Version")


def test_native_empty_204_response_keeps_status_delimiter_and_secret_off_argv():
    from homeserver_control.native_rehearsal_apps import NativeApps

    group, _, states = fixture_set()
    seen = []

    def runner(args, **kwargs):
        seen.append((args, kwargs))
        output = json.dumps(list(states.values())) if "inspect" in args else "\n__HTTP_STATUS__204"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    group.docker = Docker(runner=runner, executable="/usr/bin/docker")
    apps = NativeApps(group, {})
    assert (
        apps.request("jellyfin", "/Startup/User", "POST", {"Password": "private-fixture"}) is None
    )
    args, options = seen[-1]
    assert "private-fixture" not in " ".join(args)
    assert "private-fixture" in options["input"]


@pytest.mark.parametrize("status,expected", [(200, {"Name": "fixture"}), (401, None)])
def test_native_http_status_is_checked_and_response_kept_only_privately(tmp_path, status, expected):
    from homeserver_control.native_rehearsal_apps import NativeApps

    apps = NativeApps(None, {}, evidence=tmp_path)
    apps.execute = lambda *args, **kwargs: '{"Name":"fixture"}\n__HTTP_STATUS__' + str(status)
    if status == 200:
        assert apps.request("jellyfin", "/Startup/User") == expected
    else:
        with pytest.raises(ContainmentError, match="native_http_failed"):
            apps.request("jellyfin", "/Startup/User")
    record = tmp_path / "native-http.private.jsonl"
    assert record.stat().st_mode & 0o777 == 0o600
    entry = json.loads(record.read_text())
    assert entry["status"] == str(status)
    assert entry["endpoint"] == "/Startup/User"


def test_startup_user_readiness_retries_read_only_503_before_any_post(monkeypatch):
    from homeserver_control.native_rehearsal_apps import NativeApps

    apps = NativeApps(None, {})
    calls = []

    def request(role, endpoint):
        calls.append(endpoint)
        if len(calls) == 1:
            raise ContainmentError("native_http_failed")
        return {"Name": "default"}

    apps.request = request
    monkeypatch.setattr("homeserver_control.native_rehearsal_apps.time.sleep", lambda _: None)
    assert apps.wait("jellyfin", "/Startup/User", required_key="Name") == {"Name": "default"}
    assert calls == ["/Startup/User", "/Startup/User"]


def test_reopened_jellyfin_waits_for_authenticated_get_before_authentication_post():
    from homeserver_control.native_rehearsal_apps import NativeApps

    apps = NativeApps(None, {})
    calls = []

    def wait(role, endpoint):
        calls.append(endpoint)
        return {"Version": "12.2.0"}

    def authenticate(*args):
        calls.append("authenticate_post")
        return "user"

    apps.wait = wait
    apps.authenticate = authenticate
    apps.verify_movie = lambda _: None
    apps.verify_jellyfin(
        {"jellyfin": "12.2.0"},
        {
            "authorization": "private-token",
            "username": "fixture",
            "password": "private",
            "user": "user",
        },
    )
    assert calls == ["/System/Info/Public", "/System/Info", "authenticate_post"]


def test_malformed_inspect_labels_latches_hold_without_public_exception():
    group, docker, states = fixture_set()
    states[group.owned[-1].identifier]["Config"]["Labels"] = None
    assert group.cleanup() is False
    assert not docker.mutations


def test_private_fixture_creation_failure_returns_fixed_public_code(monkeypatch):
    from homeserver_control.native_rehearsal import run_native_rehearsal

    def failure(*args, **kwargs):
        raise OSError("private-path-and-token")

    monkeypatch.setattr("homeserver_control.native_rehearsal.tempfile.mkdtemp", failure)
    report = run_native_rehearsal()
    assert report.blocked_reasons == ("private_fixture_unavailable",)
    assert "private-path-and-token" not in repr(report)


def test_library_readback_and_idle_scan_precede_single_explicit_refresh(monkeypatch):
    from homeserver_control.native_rehearsal_apps import NativeApps

    apps = NativeApps(None, {})
    calls = []
    states = iter(["Running", "Idle"])
    library = {}

    def request(role, endpoint, method="GET", body=None):
        calls.append((endpoint, method))
        if method == "POST" and endpoint.startswith("/Library/VirtualFolders?"):
            assert "refreshLibrary=false" in endpoint
            library.update(body["LibraryOptions"])
        elif endpoint == "/Library/VirtualFolders":
            return [
                {
                    "Name": "NativeProbe",
                    "Locations": ["/data/media/movies"],
                    "LibraryOptions": library,
                }
            ]
        elif endpoint == "/ScheduledTasks":
            return [{"Key": "RefreshLibrary", "State": next(states)}]

    apps.request = request
    monkeypatch.setattr("homeserver_control.native_rehearsal_apps.time.sleep", lambda _: None)
    apps.prepare_library()
    assert calls[-1] == ("/Library/Refresh", "POST")
    assert calls.count(("/Library/Refresh", "POST")) == 1
    assert calls.count(("/ScheduledTasks", "GET")) == 2
    assert {entry["Type"] for entry in library["TypeOptions"]} == {"Movie", "BoxSet"}
    assert all(
        entry["MetadataFetchers"] == entry["ImageFetchers"] == []
        for entry in library["TypeOptions"]
    )


@pytest.mark.parametrize("attack", ["path", "provider"])
def test_library_rejected_options_block_before_scan(attack):
    from homeserver_control.native_rehearsal_apps import NativeApps

    apps = NativeApps(None, {})
    library = {}
    calls = []

    def request(role, endpoint, method="GET", body=None):
        calls.append(endpoint)
        if method == "POST":
            library.update(copy.deepcopy(body["LibraryOptions"]))
        else:
            location = "/data/media/movies" if attack != "path" else "/unexpected"
            if attack == "provider":
                library["TypeOptions"][0]["MetadataFetchers"] = ["unexpected-provider"]
            return [{"Name": "NativeProbe", "Locations": [location], "LibraryOptions": library}]

    apps.request = request
    with pytest.raises(ContainmentError, match="native_library_options_failed"):
        apps.prepare_library()
    assert "/Library/Refresh" not in calls


@pytest.mark.parametrize("ambiguous_create", [True, False])
def test_unknown_create_retains_fixture_but_missing_image_has_no_cleanup_hold(
    monkeypatch, tmp_path, ambiguous_create
):
    from homeserver_control import native_rehearsal

    root = tmp_path / "private-fixture"
    calls = []

    def fixture_root(**kwargs):
        root.mkdir(mode=0o700)
        return str(root)

    class Transport:
        def __init__(self, **kwargs):
            pass

        def command(self, *args):
            calls.append(args)
            if args[:2] == ("image", "inspect") and ambiguous_create:
                return json.dumps([{"Id": "sha256:" + "a" * 64}])
            raise ContainmentError("docker_timeout" if ambiguous_create else "docker_failed")

    monkeypatch.setattr(native_rehearsal.tempfile, "mkdtemp", fixture_root)
    monkeypatch.setattr(native_rehearsal, "Docker", Transport)
    report = native_rehearsal.run_native_rehearsal()
    assert root.is_dir()  # uncertain create or failed evidence must remain private
    assert root.stat().st_mode & 0o777 == 0o700
    assert report.resources_retained
    assert report.owned_containers_created == report.owned_containers_removed == 0
    assert not any(args[0] in ("stop", "rm", "start", "kill") for args in calls)
    if ambiguous_create:
        assert (root / "create-in-progress").exists()
        assert report.blocked_reasons == ("docker_timeout", "cleanup_hold")
    else:
        assert not (root / "create-in-progress").exists()
        assert report.blocked_reasons == ("docker_failed",)
        assert not any(args[0] == "create" for args in calls)
