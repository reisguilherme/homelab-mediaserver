#!/usr/bin/env python3
"""Rehearse one pinned native image with fresh tmpfs, no network and no app state.

Run on the Linux Docker host as root. This proves only isolated startup and
shutdown observations; it does not admit restored state or a production writer.
No image environment, application output, HTTP body or credentials are recorded.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path

ROLES = ("sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "seerr", "byparr")
LABEL = "homeserver.core.rehearsal"
ROLE_LABEL = "homeserver.core.role"
IMAGE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
NONCE_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
MAX_OUTPUT = 1024 * 1024
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


class Hold(Exception):
    """Fixed, non-sensitive failure code suitable for a private receipt."""


def recipe(role: str) -> dict:
    if role not in ROLES:
        raise Hold("unknown_role")
    env = ["PATH=/lsiopy/bin:/usr/local/bin:/usr/bin:/bin", "HOME=/config",
           "TMPDIR=/tmp", "LANG=C.UTF-8", "TZ=UTC"]
    tmpfs = {
        "/config": "rw,nosuid,nodev,noexec,size=128m,uid=1000,gid=1000,mode=0700",
        "/tmp": "rw,nosuid,nodev,noexec,size=64m,uid=1000,gid=1000,mode=1777",
        "/run": "rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000,mode=0755",
    }
    if role in {"sonarr", "radarr", "prowlarr"}:
        cwd = f"/app/{role}/bin"
        argv = [f"{cwd}/{role.capitalize()}", "-nobrowser", "-data=/config"]
        port = {"sonarr": 8989, "radarr": 7878, "prowlarr": 9696}[role]
        endpoint, kind = "/ping", "arr"
        # A fresh cache path; all writable tmpfs remain noexec during this rehearsal.
        env += ["DOTNET_BUNDLE_EXTRACT_BASE_DIR=/config/.net",
                "DOTNET_CLI_TELEMETRY_OPTOUT=1"]
    elif role == "bazarr":
        cwd = "/app/bazarr/bin"
        argv = ["/lsiopy/bin/python3", f"{cwd}/bazarr.py", "--no-update",
                "--no-tasks", "--no-signalr", "--config", "/config"]
        env += ["PYTHONDONTWRITEBYTECODE=1"]
        port, endpoint, kind = 6767, "/", "html"
    elif role == "qbittorrent":
        cwd = "/"
        argv = ["/usr/bin/qbittorrent-nox", "--profile=/config", "--webui-port=8080",
                "--confirm-legal-notice"]
        env += ["XDG_CONFIG_HOME=/config", "XDG_CACHE_HOME=/config/.cache",
                "XDG_DATA_HOME=/config/.local/share", "QT_QPA_PLATFORM=offscreen"]
        port, endpoint, kind = 8080, "/", "html"
    elif role == "seerr":
        cwd = "/app"
        argv = ["/usr/local/bin/node", "/app/dist/index.js"]
        tmpfs["/app/config"] = tmpfs["/config"]
        env += ["NODE_ENV=production", "CONFIG_DIRECTORY=/app/config", "PORT=5055"]
        # /status may await GitHub release lookup even with a local healthy server.
        port, endpoint, kind = 5055, "/api/v1", "seerr"
    else:
        cwd = "/app"
        argv = ["/app/.venv/bin/python", "/app/main.py"]
        env += ["PYTHONDONTWRITEBYTECODE=1", "XDG_CACHE_HOME=/config/.cache"]
        port, endpoint, kind = 8191, "/openapi.json", "openapi"
    return copy.deepcopy({"cwd": cwd, "argv": argv, "env": env, "tmpfs": tmpfs,
                          "memory": (768 if role == "byparr" else 512) * 1024**2,
                          "port": port, "endpoint": endpoint, "http_kind": kind})


def validate_inputs(role: str, image: str, nonce: str) -> None:
    recipe(role)
    if not IMAGE_PATTERN.fullmatch(image) or not NONCE_PATTERN.fullmatch(nonce):
        raise Hold("invalid_image_or_nonce")


def create_arguments(role: str, image: str, nonce: str) -> list[str]:
    validate_inputs(role, image, nonce)
    plan = recipe(role)
    args = ["create", "--pull", "never", "--name", f"core-rehearsal-{nonce}",
            "--label", f"{LABEL}={nonce}", "--label", f"{ROLE_LABEL}={role}",
            "--network", "none", "--restart", "no", "--user", "1000:1000",
            "--entrypoint", "/usr/bin/env", "--workdir", plan["cwd"], "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--no-healthcheck", "--cpus", "1", "--pids-limit", "128", "--ipc", "private",
            "--memory", str(plan["memory"]), "--memory-swap", str(plan["memory"]),
            "--log-driver", "none"]
    for target, options in plan["tmpfs"].items():
        args += ["--tmpfs", f"{target}:{options}"]
    return [*args, image, "-i", *plan["env"], *plan["argv"]]


def validate_snapshot(row: dict, identifier: str, image: str, role: str, nonce: str) -> dict:
    """Validate the full fixture policy again before every mutation."""
    validate_inputs(role, image, nonce)
    if not ID_PATTERN.fullmatch(identifier):
        raise Hold("invalid_container_id")
    plan = recipe(role)
    try:
        config, host, network, state = (row[key] for key in
                                         ("Config", "HostConfig", "NetworkSettings", "State"))
        command = ["-i", *plan["env"], *plan["argv"]]
        expected_host = {
            "NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Memory": plan["memory"], "MemorySwap": plan["memory"],
            "NanoCpus": 1_000_000_000, "PidsLimit": 128, "PublishAllPorts": False,
            "PidMode": "", "IpcMode": "private", "UTSMode": "", "UsernsMode": "",
            "Tmpfs": plan["tmpfs"], "AutoRemove": False,
            "LogConfig": {"Type": "none", "Config": {}},
        }
        checks = [row["Id"] == identifier, row["Image"] == image,
                  row["Path"] == "/usr/bin/env", row["Args"] == command,
                  config["User"] == "1000:1000", config["WorkingDir"] == plan["cwd"],
                  config["Entrypoint"] == ["/usr/bin/env"], config["Cmd"] == command,
                  config["Labels"][LABEL] == nonce, config["Labels"][ROLE_LABEL] == role,
                  config["Healthcheck"]["Test"] == ["NONE"],
                  all(host[key] == value for key, value in expected_host.items()),
                  all(not host.get(key) for key in ("CapAdd", "Binds", "Mounts", "Devices",
                      "DeviceRequests", "PortBindings", "VolumesFrom", "Links", "ExtraHosts")),
                  all(not value for value in network["Ports"].values()),
                  set(network["Networks"]) == {"none"},
                  all(mount["Type"] == "tmpfs" and mount["Destination"] in plan["tmpfs"]
                      for mount in row["Mounts"]),
                  state["Restarting"] is False, state["Paused"] is False,
                  type(state["Pid"]) is int, state["Pid"] >= 0,
                  type(state["Running"]) is bool,
                  state["Running"] == (state["Pid"] > 0)]
        if not all(checks):
            raise Hold("container_policy_changed")
    except (KeyError, TypeError, AttributeError) as exc:
        raise Hold("container_inspection_invalid") from exc
    return state


INSPECT_FORMAT = ('{"Id":{{json .Id}},"Image":{{json .Image}},"Path":{{json .Path}},'
                  '"Args":{{json .Args}},"Config":{"User":{{json .Config.User}},'
                  '"WorkingDir":{{json .Config.WorkingDir}},'
                  '"Entrypoint":{{json .Config.Entrypoint}},"Cmd":{{json .Config.Cmd}},'
                  '"Labels":{{json .Config.Labels}},'
                  '"Healthcheck":{{json .Config.Healthcheck}}},'
                  '"HostConfig":{{json .HostConfig}},"Mounts":{{json .Mounts}},'
                  '"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}},'
                  '"Networks":{{json .NetworkSettings.Networks}}},"State":{{json .State}}}')


def bounded_command(args: list[str], *, timeout: float = 15, pass_fds: tuple = ()) -> bytes:
    """Discard stderr; never expose native/container output in error messages."""
    child = None
    selector = selectors.DefaultSelector()
    try:
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env=CLEAN_ENV, pass_fds=pass_fds)
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Hold("command_timeout")
            if not selector.select(remaining):
                raise Hold("command_timeout")
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT:
                raise Hold("command_output_too_large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Hold("command_timeout")
        if child.wait(timeout=remaining) != 0:
            raise Hold("command_failed")
        return bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise Hold("command_timeout") from exc
    except OSError as exc:
        raise Hold("command_unavailable") from exc
    finally:
        selector.close()
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()


class Docker:
    def __init__(self, config_directory: Path):
        self.prefix = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock",
                       "--config", str(config_directory)]

    def call(self, args: list[str], timeout: float = 15) -> bytes:
        return bounded_command([*self.prefix, *args], timeout=timeout)

    def inspect(self, identifier: str) -> dict:
        return decode_json(self.call(["inspect", "--type", "container", "--format",
                                      INSPECT_FORMAT, identifier]))

    def validate_image(self, image: str, role: str) -> None:
        row = decode_json(self.call(["image", "inspect", "--format",
                                    '{"Id":{{json .Id}},'
                                    '"Volumes":{{json (index .Config "Volumes")}}}',
                                    image]))
        if row.get("Id") != image or not isinstance(row.get("Volumes"), (dict, type(None))):
            raise Hold("image_identity_invalid")
        if not set(row.get("Volumes") or {}).issubset(recipe(role)["tmpfs"]):
            raise Hold("image_has_unmasked_volume")


def decode_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise Hold("invalid_json") from exc
    if not isinstance(value, dict):
        raise Hold("invalid_json_object")
    return value


class Evidence:
    """Exclusive root-only directory on the root filesystem, no shared log files."""

    def __init__(self, directory: Path):
        if os.geteuid() != 0 or not directory.is_absolute():
            raise Hold("private_root_evidence_required")
        parent = directory.parent
        root_device = os.stat("/").st_dev
        for component in [*reversed(parent.parents), parent]:
            info = component.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o022 or info.st_dev != root_device):
                raise Hold("evidence_parent_untrusted")
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise Hold("evidence_directory_exists") from exc
        self.directory = directory
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.config_directory = directory / "docker-config"
        self.config_directory.mkdir(mode=0o700)

    def write(self, filename: str, value: dict) -> None:
        if filename not in {"ownership.json", "receipt.json"}:
            raise Hold("evidence_filename_invalid")
        data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        if len(data) > 65536:
            raise Hold("evidence_too_large")
        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.fd)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)


def process_identity(pid: int) -> dict | None:
    """Read numeric process identity only; never read cmdline or environ."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        executable = os.fsencode(os.readlink(f"/proc/{pid}/exe"))
        cgroup = Path(f"/proc/{pid}/cgroup").read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > 8192 or len(cgroup) > 8192 or not cgroup:
        raise Hold("process_stat_invalid")
    try:
        tail = raw.rsplit(b") ", 1)[1].split()
        return {"pid": pid, "start_ticks": int(tail[19]), "state": tail[0].decode("ascii"),
                "ppid": int(tail[1]), "pgid": int(tail[2]), "sid": int(tail[3]),
                "exe_sha256": hashlib.sha256(executable).hexdigest(),
                "cgroup_sha256": hashlib.sha256(cgroup).hexdigest()}
    except (ValueError, IndexError, UnicodeError) as exc:
        raise Hold("process_stat_invalid") from exc


def same_process(identity: dict) -> bool:
    current = process_identity(identity["pid"])
    return bool(current and current["start_ticks"] == identity["start_ticks"]
                and current["state"] not in {"Z", "X"})


HTTP_PROBE = r'''
import http.client, json, sys
port, path, kind = int(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", path, headers={"Connection": "close"})
    response = connection.getresponse()
    body = response.read(65537)
    valid = response.status == 200 and 0 < len(body) <= 65536
    if valid and kind in {"arr", "seerr", "openapi"}:
        value = json.loads(body)
        valid = isinstance(value, dict)
        if kind == "arr":
            valid = valid and value.get("status") == "OK"
        elif kind == "seerr":
            valid = valid and value.get("api") == "Seerr API" and value.get("version") == "1.0"
        else:
            valid = valid and isinstance(value.get("openapi"), str)
    elif valid:
        valid = b"<html" in body.lower() or b"<!doctype html" in body.lower()
    print(json.dumps({"ready": bool(valid)}))
except Exception:
    print('{"ready":false}')
'''


class HostObserver:
    def __init__(self, docker: Docker):
        self.docker = docker

    def tree(self, identifier: str, leader_pid: int) -> list[dict]:
        raw = self.docker.call(["top", identifier, "-eo", "pid,ppid,pgid,sid"])
        try:
            lines = raw.decode("ascii").splitlines()
            if lines[0].split() != ["PID", "PPID", "PGID", "SID"]:
                raise Hold("process_tree_invalid")
            pids = []
            for line in lines[1:]:
                fields = line.split()
                if len(fields) != 4 or not all(field.isdecimal() for field in fields):
                    raise Hold("process_tree_invalid")
                pids.append(int(fields[0]))
            if len(pids) > 128 or leader_pid not in pids or len(pids) != len(set(pids)):
                raise Hold("process_tree_invalid")
        except (UnicodeError, IndexError) as exc:
            raise Hold("process_tree_invalid") from exc
        observed = [identity for pid in pids if (identity := process_identity(pid))]
        if not any(row["pid"] == leader_pid for row in observed):
            raise Hold("leader_disappeared")
        return observed

    def ready(self, leader: dict, role: str) -> bool:
        plan = recipe(role)
        try:
            descriptor = os.open(f"/proc/{leader['pid']}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        try:
            if not same_process(leader):
                return False
            result = bounded_command(["/usr/bin/nsenter", f"--net=/proc/self/fd/{descriptor}",
                                      "--", sys.executable, "-I", "-B", "-c", HTTP_PROBE,
                                      str(plan["port"]), plan["endpoint"], plan["http_kind"]],
                                     timeout=4, pass_fds=(descriptor,))
            return decode_json(result).get("ready") is True and same_process(leader)
        finally:
            os.close(descriptor)

    def gone(self, identities: list[dict]) -> bool:
        return all(not same_process(identity) for identity in identities)


def rehearse(role: str, image: str, evidence, docker, observer, *, nonce: str | None = None,
             startup_seconds: float = 90, stop_seconds: float = 15,
             clock=time.monotonic, sleep=time.sleep) -> dict:
    """State machine is injectable so failure and ownership tests need no Docker."""
    nonce = nonce or secrets.token_hex(16)
    validate_inputs(role, image, nonce)
    if not 1 <= startup_seconds <= 120 or not 1 <= stop_seconds <= 30:
        raise Hold("invalid_time_budget")
    result = {"schema": 1, "role": role, "image": image, "nonce": nonce,
              "production_ready": False, "under_writer_guard_verified": False,
              "restored_state_tested": False, "http_ready": False,
              "foreground_stable": False, "graceful_stop": False,
              "kill_required": False, "removed": False, "retained": False,
              "status": "HOLD", "code": "not_started", "process_samples": []}
    identifier = None
    create_attempted = False
    observed = {}
    ownership_saved = False

    def inspect():
        return validate_snapshot(docker.inspect(identifier), identifier, image, role, nonce)

    def sample(state):
        if not state["Running"] or state["Pid"] <= 0 or state["OOMKilled"]:
            raise Hold("native_process_exited")
        tree = observer.tree(identifier, state["Pid"])
        for entry in tree:
            observed[(entry["pid"], entry["start_ticks"])] = entry
        if len(observed) > 256:
            raise Hold("process_churn_limit")
        return next(entry for entry in tree if entry["pid"] == state["Pid"])

    def wait_stopped(deadline):
        while True:
            state = inspect()
            if not state["Running"] and state["Pid"] == 0:
                if not observer.gone(list(observed.values())):
                    raise Hold("descendants_remain")
                return True
            if clock() >= deadline:
                return False
            sleep(0.2)

    try:
        docker.validate_image(image, role)
        create_attempted = True
        raw = docker.call(create_arguments(role, image, nonce))
        try:
            candidate = raw.decode("ascii").strip()
        except UnicodeError as exc:
            raise Hold("create_identity_ambiguous") from exc
        if not ID_PATTERN.fullmatch(candidate):
            raise Hold("create_identity_ambiguous")
        identifier = candidate
        result["container_id"] = identifier
        evidence.write("ownership.json", {"schema": 1, "container_id": identifier,
                                         "image": image, "role": role, "nonce": nonce})
        ownership_saved = True
        state = inspect()
        if state["Running"] or state["Pid"] != 0 or state["Status"] != "created":
            raise Hold("fixture_not_fresh")
        docker.call(["start", identifier])
        deadline = clock() + startup_seconds
        leader = None
        ready_at = None
        ready_identity = None
        while clock() < deadline:
            current = sample(inspect())
            if leader and (current["pid"], current["start_ticks"]) != (
                    leader["pid"], leader["start_ticks"]):
                raise Hold("foreground_leader_changed")
            leader = current
            native_identity = (current.get("exe_sha256"), current.get("cgroup_sha256"))
            if ready_at is not None and native_identity != ready_identity:
                raise Hold("foreground_executable_changed")
            if observer.ready(leader, role):
                if ready_at is None:
                    ready_at = clock()
                    ready_identity = native_identity
                    result["process_samples"].append(list(observed.values()))
                elif clock() - ready_at >= 1:
                    result["http_ready"] = True
                    result["foreground_stable"] = True
                    result["process_samples"].append(list(observed.values()))
                    result["code"] = "native_ready"
                    break
            else:
                ready_at = None
            sleep(0.25)
        if not result["http_ready"]:
            raise Hold("native_readiness_timeout")
    except (Hold, OSError) as exc:
        result["code"] = str(exc) if isinstance(exc, Hold) else "local_io_error"
    finally:
        if identifier is not None:
            result["retained"] = True
            try:
                if not ownership_saved:
                    raise Hold("ownership_not_durable")
                state = inspect()
                if state["Running"]:
                    try:
                        sample(state)
                    except (Hold, OSError):
                        # Failed observation must not leave an identified fixture running.
                        # Its Docker policy/identity is still revalidated before signalling.
                        result["tree_observation_incomplete"] = True
                    # TERM is asynchronous; a host deadline bounds the wait.
                    inspect()
                    docker.call(["kill", "--signal", "TERM", identifier])
                    if wait_stopped(clock() + stop_seconds):
                        result["graceful_stop"] = True
                    else:
                        inspect()
                        docker.call(["kill", "--signal", "KILL", identifier])
                        result["kill_required"] = True
                        result["code"] = "fixture_required_kill"
                        if not wait_stopped(clock() + 5):
                            raise Hold("fixture_stop_unproven")
                elif state["Pid"] != 0 or not observer.gone(list(observed.values())):
                    raise Hold("fixture_stop_unproven")
                state = inspect()
                if state["Running"] or state["Pid"] != 0:
                    raise Hold("fixture_stop_unproven")
                docker.call(["rm", "--volumes", identifier])
                result["removed"] = True
                result["retained"] = False
            except (Hold, OSError) as exc:
                result["cleanup_code"] = str(exc) if isinstance(exc, Hold) else "local_io_error"
        elif create_attempted:
            result["retained"] = True
            result["cleanup_code"] = "creation_outcome_unknown_no_guessed_cleanup"
        if (result["http_ready"] and result["foreground_stable"] and result["graceful_stop"]
                and result["removed"] and not result["kill_required"]
                and not result.get("tree_observation_incomplete")):
            result["status"] = "PASS"
            result["code"] = "isolated_native_rehearsal_passed"
        evidence.write("receipt.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--image", required=True, help="Literal local sha256 image ID")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--startup-seconds", type=float, default=90)
    parser.add_argument("--stop-seconds", type=float, default=15)
    args = parser.parse_args(argv)
    evidence = None
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise Hold("linux_root_required")
        validate_inputs(args.role, args.image, "0" * 32)
        evidence = Evidence(args.output_directory)
        docker = Docker(evidence.config_directory)
        result = rehearse(args.role, args.image, evidence, docker, HostObserver(docker),
                          startup_seconds=args.startup_seconds, stop_seconds=args.stop_seconds)
        # Deliberately omit IDs, paths, native logs and HTTP bodies from stdout.
        print(json.dumps({"status": result["status"], "code": result["code"],
                          "removed": result["removed"], "retained": result["retained"]}))
        return 0 if result["status"] == "PASS" else 2
    except (Hold, OSError) as exc:
        code = str(exc) if isinstance(exc, Hold) else "local_io_error"
        print(json.dumps({"status": "HOLD", "code": code}))
        return 2
    finally:
        if evidence is not None:
            evidence.close()


if __name__ == "__main__":
    raise SystemExit(main())
