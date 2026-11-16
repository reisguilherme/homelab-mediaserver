#!/usr/bin/env python3
"""Core storage checks and explicit fixed-role supervision; no application configuration."""

import argparse
import hashlib
import json
import math
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LEASE = re.compile(
    r"version=2\nrole=core-writer\nstate=(ready|hold)\nboot_id=(" + UUID + r")\n"
    r"filesystem_uuid=(" + UUID + r")\ngeneration=([0-9a-f]{32})\n"
    r"physical_generation=([0-9a-f]{64}|none)\nsequence=([1-9][0-9]{0,18})\n"
    r"expires_uptime_seconds=(0|[1-9][0-9]{0,9})\n"
)
MAX_LEASE_BYTES = 4096
MAX_MOUNT_BYTES = 1024**2
MARKER_TARGET = "/run/core-filesystem-uuid"
LEASE_PATH = Path("/run/core-writer-admission/lease")
BOOT_PATH = Path("/run/guest-boot-id")
MARKER_PATH = Path(MARKER_TARGET)
HOST_MOUNT_PATH = Path("/run/host-mountinfo")
UPTIME_PATH = Path("/proc/uptime")
ACTIVATION_PATH = Path("/run/core-activation/receipt.json")
NATIVE_ROLES = ("sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "seerr", "byparr")
SHA256 = r"[0-9a-f]{64}"


class GuardError(RuntimeError):
    """Fixed public refusal codes; never native settings or filesystem contents."""


def require(condition, code):
    if not condition:
        raise GuardError(code)


def uptime_clock():
    # Match Linux /proc/uptime: suspension consumes the admitted lifetime too.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def parse_lease(lease, boot, uptime, expected_uuid):
    require(isinstance(expected_uuid, str) and re.fullmatch(UUID, expected_uuid), "expected_uuid")
    require(len(lease) <= MAX_LEASE_BYTES and len(boot) <= 64 and len(uptime) <= 128, "input_size")
    try:
        match = LEASE.fullmatch(lease.decode("ascii"))
        boot_text = boot.decode("ascii")
        uptime_text = uptime.decode("ascii")
    except UnicodeError:
        raise GuardError("protocol") from None
    require(match is not None and re.fullmatch(UUID + r"\n", boot_text), "protocol")
    require(
        re.fullmatch(r"[0-9]{1,10}\.[0-9]{1,9} [0-9]{1,16}\.[0-9]{1,9}\n", uptime_text), "uptime"
    )
    state, observed_boot, filesystem_uuid, generation, physical, sequence, expires = match.groups()
    require(state == "ready" and physical != "none", "hold_or_unproved")
    require(observed_boot == boot_text[:-1] and filesystem_uuid == expected_uuid, "lease_identity")
    require(int(sequence) <= 2**63 - 1, "sequence")
    remaining = Decimal(expires) - Decimal(uptime_text.split(" ")[0])
    require(0 < remaining <= 45, "lease_expired_or_future")
    return {
        "boot_id": observed_boot,
        "filesystem_uuid": filesystem_uuid,
        "generation": generation,
        "physical_generation": physical,
        "sequence": int(sequence),
        "expires_uptime_seconds": int(expires),
        "remaining": float(remaining),
    }


def mount_rows(value):
    require(0 < len(value) <= MAX_MOUNT_BYTES and value.endswith(b"\n"), "mountinfo_size")
    try:
        lines = value.decode("ascii").splitlines()
    except UnicodeError:
        raise GuardError("mountinfo_encoding") from None
    rows = []
    for line in lines:
        fields = line.split(" ")
        require(fields.count("-") == 1, "mountinfo_format")
        separator = fields.index("-")
        require(separator >= 6 and len(fields) == separator + 4 and all(fields), "mountinfo_format")
        require(
            re.fullmatch(r"[1-9][0-9]*", fields[0]) and re.fullmatch(r"[0-9]+:[0-9]+", fields[2]),
            "mountinfo_identity",
        )
        rows.append(
            {
                "id": fields[0],
                "device": fields[2],
                "root": fields[3],
                "target": fields[4],
                "options": set(fields[5].split(",")),
                "filesystem": fields[separator + 1],
                "source": fields[separator + 2],
                "super_options": set(fields[separator + 3].split(",")),
            }
        )
    return rows


def exact_mount(rows, target):
    selected = [row for row in rows if row["target"] == target]
    require(len(selected) == 1, "missing_or_stacked_mount")
    require(not any(row["target"].startswith(target + "/") for row in rows), "nested_mount")
    return selected[0]


def check_mounts(host, local, marker, expected_uuid, *, marker_device, data_mode):
    require(isinstance(expected_uuid, str) and re.fullmatch(UUID, expected_uuid), "expected_uuid")
    require(marker == (expected_uuid + "\n").encode("ascii"), "marker_identity")
    require(data_mode in {"ro", "rw", "none"}, "data_mode")
    host_rows, local_rows = mount_rows(host), mount_rows(local)
    guest_roots = [row for row in host_rows if row["target"] == "/"]
    require(len(guest_roots) == 1, "guest_root_mount")
    admission = exact_mount(local_rows, "/run/core-writer-admission")
    require(
        admission["root"] == "/var/lib/homelab-storage/core-writer-admission"
        and admission["device"] == guest_roots[0]["device"]
        and admission["source"] == guest_roots[0]["source"]
        and admission["filesystem"] == guest_roots[0]["filesystem"]
        and "ro" in admission["options"]
        and "rw" not in admission["options"],
        "lease_directory_mount",
    )
    backing = exact_mount(host_rows, "/srv/external3")
    marker_mount = exact_mount(local_rows, MARKER_TARGET)
    checks = [(backing, "/", "rw"), (marker_mount, "/.homelab-volume-id", "ro")]
    data_id = "none"
    if data_mode == "none":
        require(
            not any(
                row["target"] == "/data" or row["target"].startswith("/data/") for row in local_rows
            ),
            "unexpected_data_mount",
        )
    else:
        data = exact_mount(local_rows, "/data")
        checks.append((data, "/homeserver", data_mode))
        data_id = data["id"]
    for row, root, mode in checks:
        require(
            row["filesystem"] == "ext4"
            and row["source"].startswith("/dev/")
            and row["root"] == root,
            "mount_source",
        )
        require(
            mode in row["options"] and ({"ro", "rw"} - {mode}).isdisjoint(row["options"]),
            "mount_mode",
        )
        require("rw" in row["super_options"] and "ro" not in row["super_options"], "filesystem_ro")
        if row is not marker_mount:
            require({"nodev", "nosuid", "noexec"} <= row["options"], "mount_restrictions")
        require(
            row["device"] == backing["device"] and row["source"] == backing["source"],
            "mount_device",
        )
    require(
        marker_device == backing["device"] and backing["device"] != guest_roots[0]["device"],
        "marker_or_guest_root_device",
    )
    return backing["id"], data_id, marker_mount["id"], admission["id"], backing["device"]


def trusted_metadata(info, *, directory=False, expected_mode=None):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    require(kind(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "untrusted_input")
    if not directory:
        require(info.st_nlink == 1, "linked_input")
    if expected_mode is not None:
        require(stat.S_IMODE(info.st_mode) == expected_mode, "input_mode")


def file_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def read_trusted(path, limit, *, expected_mode=None):
    """Pin all root-owned ancestors and one regular bounded file; no alias traversal."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts and len(path.parts) > 1, "input_path")
    require(type(limit) is int and 0 < limit <= MAX_MOUNT_BYTES, "input_limit")
    directory = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        trusted_metadata(os.fstat(directory), directory=True)
        for part in path.parts[1:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = new
            trusted_metadata(os.fstat(directory), directory=True)
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            trusted_metadata(before, expected_mode=expected_mode)
            require(before.st_size <= limit, "input_size")
            value = source.read(limit + 1)
            after = os.fstat(source.fileno())
            require(file_identity(before) == file_identity(after), "input_changed")
            require(len(value) <= limit, "input_size")
            return value, f"{os.major(before.st_dev)}:{os.minor(before.st_dev)}"
    except OSError:
        raise GuardError("input_unavailable") from None
    finally:
        if directory is not None:
            os.close(directory)


def local_mount_path():
    # Numeric PID avoids accepting an arbitrary symlink under /proc.
    return Path(f"/proc/{os.getpid()}/mountinfo")


def read_kernel(path, limit):
    """Only two fixed kernel files; process mountinfo may belong to the non-root UID."""
    require(path in {UPTIME_PATH, local_mount_path()}, "kernel_input_path")
    require(type(limit) is int and 0 < limit <= MAX_MOUNT_BYTES, "input_limit")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            proc = os.stat("/proc", follow_symlinks=False)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_dev == proc.st_dev
                and info.st_nlink == 1
                and not info.st_mode & 0o222,
                "kernel_input_identity",
            )
            value = source.read(limit + 1)
            require(len(value) <= limit, "input_size")
            return value
    except OSError:
        raise GuardError("kernel_input_unavailable") from None


def check_runtime(
    expected_uuid, data_mode, *, read=read_trusted, kernel_read=read_kernel, clock=uptime_clock
):
    boot, _ = read(BOOT_PATH, 64, expected_mode=0o444)
    marker, marker_device = read(MARKER_PATH, 64, expected_mode=0o444)
    host, _ = read(HOST_MOUNT_PATH, MAX_MOUNT_BYTES, expected_mode=0o444)
    local = kernel_read(local_mount_path(), MAX_MOUNT_BYTES)
    mount_token = check_mounts(
        host, local, marker, expected_uuid, marker_device=marker_device, data_mode=data_mode
    )
    lease, _ = read(LEASE_PATH, MAX_LEASE_BYTES, expected_mode=0o644)
    # The deadline uses the earlier monotonic sample; parsing and I/O cannot add time.
    observed = clock()
    uptime = kernel_read(UPTIME_PATH, 128)
    admitted = parse_lease(lease, boot, uptime, expected_uuid)
    deadline = observed + admitted["remaining"]
    require(clock() < deadline, "lease_expired_during_check")
    return {
        "token": (
            *mount_token,
            admitted["boot_id"],
            admitted["filesystem_uuid"],
            admitted["generation"],
            admitted["physical_generation"],
        ),
        "sequence": admitted["sequence"],
        "lease_sha256": hashlib.sha256(lease).hexdigest(),
        "expires_at": deadline,
    }


def native_plan(role):
    """Fixed launchers; callers cannot inject commands, environment or data policy."""
    require(role in NATIVE_ROLES, "native_role")
    env = {"PATH": "/lsiopy/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/config",
           "TMPDIR": "/tmp", "LANG": "C.UTF-8", "TZ": "UTC"}
    data_mode = "rw" if role in {"sonarr", "radarr", "bazarr", "qbittorrent"} else "none"
    if role in {"sonarr", "radarr", "prowlarr"}:
        cwd = f"/app/{role}/bin"
        argv = [f"{cwd}/{role.capitalize()}", "-nobrowser", "-data=/config"]
        env.update(DOTNET_BUNDLE_EXTRACT_BASE_DIR="/config/.net", DOTNET_CLI_TELEMETRY_OPTOUT="1")
    elif role == "bazarr":
        cwd = "/app/bazarr/bin"
        argv = ["/lsiopy/bin/python3", f"{cwd}/bazarr.py", "--no-update", "--no-tasks",
                "--no-signalr", "--config", "/config"]
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    elif role == "qbittorrent":
        cwd = "/"
        argv = ["/usr/bin/qbittorrent-nox", "--webui-port=8080",
                "--confirm-legal-notice"]
        env.update(XDG_CONFIG_HOME="/config", XDG_CACHE_HOME="/config/.cache",
                   XDG_DATA_HOME="/config", QT_QPA_PLATFORM="offscreen")
    elif role == "seerr":
        cwd = "/app"
        argv = ["/usr/local/bin/node", "/app/dist/index.js"]
        env.update(NODE_ENV="production", CONFIG_DIRECTORY="/app/config", PORT="5055")
    else:
        cwd = "/app"
        argv = ["/app/.venv/bin/python", "/app/main.py"]
        env.update(PYTHONDONTWRITEBYTECODE="1", XDG_CACHE_HOME="/config/.cache")
    return {"argv": argv, "cwd": cwd, "env": env, "data_mode": data_mode}


def native_plan_digest(plan):
    serialized = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def parse_activation(raw, role, expected_uuid, pinned_sha256):
    """Read operator-pinned configuration; never infer proofs from HTTP readiness.

    Evidence digests are references audited by the operator. This parser does
    not certify unseen receipts or prove the current Docker image identity.
    """
    plan = native_plan(role)
    require(isinstance(expected_uuid, str) and re.fullmatch(UUID, expected_uuid), "expected_uuid")
    require(isinstance(pinned_sha256, str) and re.fullmatch(SHA256, pinned_sha256),
            "activation_pin")
    require(isinstance(raw, bytes) and 0 < len(raw) <= 8192, "activation_size")
    require(hashlib.sha256(raw).hexdigest() == pinned_sha256, "activation_changed")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "activation_duplicate")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise GuardError("activation_json") from None
    digest_fields = {"native_command_sha256", "restore_receipt_sha256",
                     "configuration_receipt_sha256", "native_process_group_receipt_sha256"}
    require(isinstance(value, dict) and set(value) == {
        "schema", "scope", "role", "filesystem_uuid", "data_mode", "image_id", *digest_fields,
    }, "activation_schema")
    require(type(value["schema"]) is int and value["schema"] == 1, "activation_schema")
    require(value["scope"] == "production-core-activation", "activation_scope")
    require(value["role"] == role and value["filesystem_uuid"] == expected_uuid,
            "activation_identity")
    require(value["data_mode"] == plan["data_mode"], "activation_data_mode")
    require(isinstance(value["image_id"], str)
            and re.fullmatch("sha256:" + SHA256, value["image_id"]), "activation_image")
    require(all(isinstance(value[key], str) and re.fullmatch(SHA256, value[key])
                for key in digest_fields), "activation_evidence_digest")
    require(value["native_command_sha256"] == native_plan_digest(plan), "activation_command")
    return value


def check_activation_mount(host, local, role, receipt_device):
    native_plan(role)
    host_rows, local_rows = mount_rows(host), mount_rows(local)
    root = exact_mount(host_rows, "/")
    activation = exact_mount(local_rows, "/run/core-activation")
    require(activation["root"] == f"/srv/appdata/core-activation/{role}"
            and activation["device"] == receipt_device == root["device"]
            and activation["source"] == root["source"]
            and activation["filesystem"] == root["filesystem"]
            and "ro" in activation["options"] and "rw" not in activation["options"],
            "activation_mount")
    return activation["id"], activation["device"]


def check_activation(role, expected_uuid, pinned_sha256, *, read=read_trusted,
                     kernel_read=read_kernel):
    raw, receipt_device = read(ACTIVATION_PATH, 8192, expected_mode=0o444)
    parse_activation(raw, role, expected_uuid, pinned_sha256)
    host, _ = read(HOST_MOUNT_PATH, MAX_MOUNT_BYTES, expected_mode=0o444)
    local = kernel_read(local_mount_path(), MAX_MOUNT_BYTES)
    return (*check_activation_mount(host, local, role, receipt_device), pinned_sha256)


def execution_check(role, expected_uuid, pinned_sha256):
    # Read immutable activation first; no delay may refresh an existing lease.
    activation_token = check_activation(role, expected_uuid, pinned_sha256)
    storage = check_runtime(expected_uuid, native_plan(role)["data_mode"])
    return {**storage, "token": (*storage["token"], *activation_token)}


def valid_snapshot(value):
    require(
        isinstance(value, dict)
        and set(value) == {"token", "sequence", "lease_sha256", "expires_at"},
        "snapshot_schema",
    )
    require(
        isinstance(value["token"], tuple)
        and value["token"]
        and all(isinstance(part, str) and 0 < len(part) <= 128 for part in value["token"]),
        "snapshot_token",
    )
    require(
        type(value["sequence"]) is int and 0 < value["sequence"] <= 2**63 - 1, "snapshot_sequence"
    )
    require(
        isinstance(value["lease_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", value["lease_sha256"]),
        "snapshot_digest",
    )
    expires = value["expires_at"]
    require(
        type(expires) in {float, int}
        and math.isfinite(expires)
        and 0 < expires - uptime_clock() <= 45,
        "snapshot_expired",
    )


def accept_update(previous, current):
    valid_snapshot(previous)
    valid_snapshot(current)
    require(
        current["token"] == previous["token"] and current["sequence"] >= previous["sequence"],
        "storage_identity_changed",
    )
    current = dict(current)
    if current["sequence"] == previous["sequence"]:
        require(current["lease_sha256"] == previous["lease_sha256"], "sequence_reused")
        current["expires_at"] = min(previous["expires_at"], current["expires_at"])
        valid_snapshot(current)
    return current


class ManualStop(Exception):
    """Requested stop, preserved without restart or changing storage authority."""


def begin_check(checker):
    """At most one in-flight reader; a blocked reader cannot block the supervising thread."""
    outcomes = queue.Queue(maxsize=1)

    def perform():
        try:
            value = checker()
        except BaseException:
            outcomes.put((False, None))
        else:
            outcomes.put((True, value))

    thread = threading.Thread(target=perform, name="core-storage-check", daemon=True)
    thread.start()
    return outcomes


def await_check(checker, stop_event, deadline):
    outcomes = begin_check(checker)
    while True:
        if stop_event.is_set():
            raise ManualStop
        remaining = deadline - uptime_clock()
        require(remaining > 0, "check_timeout")
        try:
            okay, value = outcomes.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        require(okay, "check_failed")
        valid_snapshot(value)
        return value


def native_exit_status(child):
    # WNOWAIT pins the leader PID until all group signals are finished. Reaping it
    # with poll()/wait() beforehand could permit that group ID to be reused.
    result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if result is None:
        return None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def group_has_live_members(identifier):
    """Observe the owned group; zombies cannot write and need their parent to reap."""
    with os.scandir("/proc") as entries:
        for count, entry in enumerate(entries):
            if count >= 65536:
                return True
            if not entry.name.isdecimal():
                continue
            try:
                with open(f"/proc/{entry.name}/stat", "rb") as source:
                    value = source.read(4097)
            except FileNotFoundError:
                continue
            if len(value) > 4096:
                return True
            fields = value.rsplit(b")", 1)[-1].split()
            if len(fields) < 4:
                return True
            if fields[2] == str(identifier).encode("ascii") and fields[0] != b"Z":
                return True
    return False


def await_group_stopped(identifier, grace):
    deadline = uptime_clock() + grace
    while group_has_live_members(identifier):
        remaining = deadline - uptime_clock()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def stop_group(child, grace):
    """Signal and verify the owned group, including descendants that ignore TERM."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    observed = True
    try:
        await_group_stopped(child.pid, grace)
    except OSError:
        observed = False
    # Kill remaining group members even when the original native process exited.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        contained = await_group_stopped(child.pid, grace) and observed
    except OSError:
        contained = False
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        # Kernel uninterruptible I/O is not proof of successful containment.
        return False
    return contained


def supervise(
    command, checker, *, stop_event=None, poll_interval=1.0, check_timeout=3.0, term_grace=2.0,
    cwd=None, env=None,
):
    """Existing process-group lifecycle, also used by the explicit fixed-role CLI.

    Caller must first establish version-proven native containment and its audited
    activation receipt. No receipt or native configuration is manufactured here.
    Native command must remain in the owned foreground session/process group.
    """
    child = None
    result = 78
    saved_handlers = {}
    stop_event = stop_event if stop_event is not None else threading.Event()
    try:
        require(threading.current_thread() is threading.main_thread(), "supervisor_main_thread")
        require(
            isinstance(command, (list, tuple))
            and command
            and all(isinstance(part, str) and part and "\x00" not in part for part in command),
            "native_command",
        )
        for value, maximum in ((poll_interval, 1), (check_timeout, 3), (term_grace, 2)):
            require(
                type(value) in {int, float} and math.isfinite(value) and 0 < value <= maximum,
                "supervisor_bounds",
            )
        for signum in (signal.SIGTERM, signal.SIGINT):
            saved_handlers[signum] = signal.signal(signum, lambda *_args: stop_event.set())
        initial = await_check(checker, stop_event, uptime_clock() + check_timeout)
        current = await_check(
            checker, stop_event, min(initial["expires_at"], uptime_clock() + check_timeout)
        )
        current = accept_update(initial, current)
        if stop_event.is_set():
            raise ManualStop
        valid_snapshot(current)
        child = subprocess.Popen(command, start_new_session=True, cwd=cwd, env=env)
        pending, pending_deadline = None, 0.0
        next_check = uptime_clock()
        while True:
            if stop_event.is_set():
                raise ManualStop
            native_status = native_exit_status(child)
            if native_status is not None:
                result = native_status
                break
            now = uptime_clock()
            require(now < current["expires_at"], "lease_expired")
            if pending is not None:
                require(now < pending_deadline, "check_timeout")
                try:
                    okay, value = pending.get_nowait()
                except queue.Empty:
                    pass
                else:
                    require(okay, "check_failed")
                    current = accept_update(current, value)
                    pending = None
                    next_check = uptime_clock() + poll_interval
            if pending is None and uptime_clock() >= next_check:
                pending = begin_check(checker)
                pending_deadline = min(current["expires_at"], uptime_clock() + check_timeout)
            deadline = min(
                current["expires_at"], pending_deadline if pending is not None else next_check
            )
            stop_event.wait(max(0, min(0.05, deadline - uptime_clock())))
    except ManualStop:
        result = 0
    except (GuardError, OSError, ValueError):
        result = 78
    finally:
        contained = True
        try:
            if child is not None:
                contained = stop_group(child, term_grace)
        except OSError:
            contained = False
        finally:
            for signum, handler in saved_handlers.items():
                signal.signal(signum, handler)
        if not contained:
            result = 78
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", choices=NATIVE_ROLES)
    parser.add_argument("--filesystem-uuid")
    parser.add_argument("--data-mode", choices=("ro", "rw", "none"))
    parser.add_argument("--activation-sha256")
    args = parser.parse_args(argv)
    if not args.check and args.run is None:
        print("core native startup denied: explicit mode required", file=sys.stderr)
        return 78
    try:
        if args.run is not None:
            require(args.data_mode is None, "native_data_mode_is_fixed")
            require(isinstance(args.filesystem_uuid, str)
                    and re.fullmatch(UUID, args.filesystem_uuid), "expected_uuid")
            require(isinstance(args.activation_sha256, str)
                    and re.fullmatch(SHA256, args.activation_sha256), "activation_pin")
            plan = native_plan(args.run)
            return supervise(
                plan["argv"],
                lambda: execution_check(args.run, args.filesystem_uuid, args.activation_sha256),
                cwd=plan["cwd"], env=plan["env"],
            )
        require(args.activation_sha256 is None, "check_does_not_activate")
        check_runtime(args.filesystem_uuid, args.data_mode or "rw")
    except GuardError as error:
        print(f"core guard refused: {error}", file=sys.stderr)
        return 78 if args.run is not None else 1
    print("core storage check passed; native startup remains denied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
