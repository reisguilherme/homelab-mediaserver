"""Core writers require their own fresh lease and exact local ext4 identity."""

import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/core-storage-guard.py"
BOOT = "ca9a5731-8b28-4400-8ee9-138e1485de61"
FILESYSTEM = "b2256ce1-b29c-4608-a227-c78a50ea169e"
GENERATION = "21ea0f809f77421a9ce2a040376a43da"
PHYSICAL = "c" * 64


@pytest.fixture
def guard():
    assert SCRIPT.is_file(), "core writer storage consumer is missing"
    spec = importlib.util.spec_from_file_location("core_storage_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lease(**changes):
    values = dict(
        version="2",
        role="core-writer",
        state="ready",
        boot_id=BOOT,
        filesystem_uuid=FILESYSTEM,
        generation=GENERATION,
        physical_generation=PHYSICAL,
        sequence="7",
        expires_uptime_seconds="145",
    )
    values.update(changes)
    return "".join(f"{key}={value}\n" for key, value in values.items()).encode()


def admit(guard, value=None, *, uptime=b"100.00 90.00\n", boot=None, expected=FILESYSTEM):
    return guard.parse_lease(
        lease() if value is None else value,
        (BOOT + "\n").encode() if boot is None else boot,
        uptime,
        expected,
    )


def test_exact_writer_lease_admits_only_its_current_boot_and_filesystem(guard):
    admitted = admit(guard)
    assert admitted["boot_id"] == BOOT
    assert admitted["filesystem_uuid"] == FILESYSTEM
    assert admitted["generation"] == GENERATION
    assert admitted["sequence"] == 7
    assert admitted["remaining"] == pytest.approx(45)


@pytest.mark.parametrize(
    "changed",
    [
        {"version": "1"},
        {"role": "playback"},
        {"state": "hold"},
        {"state": "READY"},
        {"boot_id": FILESYSTEM},
        {"filesystem_uuid": BOOT},
        {"generation": "z" * 32},
        {"generation": GENERATION.upper()},
        {"physical_generation": "none"},
        {"physical_generation": "D" * 64},
        {"sequence": "0"},
        {"sequence": "-1"},
        {"sequence": "07"},
        {"sequence": "1.0"},
        {"sequence": "9" * 100},
        {"expires_uptime_seconds": "100"},
        {"expires_uptime_seconds": "99"},
        {"expires_uptime_seconds": "146"},
        {"expires_uptime_seconds": "0145"},
        {"expires_uptime_seconds": "145.0"},
        {"expires_uptime_seconds": "9" * 100},
    ],
)
def test_wrong_or_noncanonical_lease_fields_deny(guard, changed):
    with pytest.raises(guard.GuardError):
        admit(guard, lease(**changed))


@pytest.mark.parametrize(
    "transform",
    [
        lambda value: b"",
        lambda value: value.replace(b"\n", b"\r\n"),
        lambda value: value[:-1],
        lambda value: value + b"\n",
        lambda value: value + b"role=core-writer\n",
        lambda value: value.replace(b"version=2\nrole=core-writer", b"role=core-writer\nversion=2"),
        lambda value: value.replace(b"state=ready", b"state=ready\x00"),
        lambda value: b"x" * 4097,
        lambda value: (
            b"version=1\nstate=ready\nboot_id=" + BOOT.encode() + b"\n"
            b"generation=" + GENERATION.encode() + b"\nexpires_uptime_seconds=145\n"
        ),
    ],
)
def test_playback_and_malformed_writer_protocols_deny(guard, transform):
    with pytest.raises(guard.GuardError):
        admit(guard, transform(lease()))


@pytest.mark.parametrize(
    "clock",
    [b"nan 0\n", b"100 90\n", b"-1.00 0.00\n", b"100.00 0.0", b"100.00 0.00\nextra\n", b"1" * 512],
)
def test_clock_must_be_bounded_kernel_uptime(guard, clock):
    with pytest.raises(guard.GuardError):
        admit(guard, uptime=clock)


def test_fractional_uptime_does_not_extend_the_maximum_lease(guard):
    assert admit(guard, lease(expires_uptime_seconds="101"), uptime=b"100.99 0.00\n")[
        "remaining"
    ] == pytest.approx(0.01)
    with pytest.raises(guard.GuardError):
        admit(guard, lease(expires_uptime_seconds="146"), uptime=b"100.99 0.00\n")


def mount(target, *, identifier=20, device="8:16", root="/", mode="rw", fs="ext4"):
    return (
        f"{identifier} 1 {device} {root} {target} {mode},nodev,nosuid,noexec "
        f"- {fs} /dev/sdb rw,errors=remount-ro\n"
    )


def mounts():
    host = mount("/", identifier=1, device="8:1") + mount("/srv/external3")
    local = mount("/data", identifier=30, root="/homeserver")
    local += mount(
        "/run/core-filesystem-uuid", identifier=31, root="/.homelab-volume-id", mode="ro"
    )
    local += mount(
        "/run/core-writer-admission",
        identifier=32,
        device="8:1",
        root="/var/lib/homelab-storage/core-writer-admission",
        mode="ro",
    )
    return host.encode(), local.encode()


def check_mounts(guard, host=None, local=None, **changes):
    default_host, default_local = mounts()
    return guard.check_mounts(
        default_host if host is None else host,
        default_local if local is None else local,
        (FILESYSTEM + "\n").encode(),
        FILESYSTEM,
        marker_device="8:16",
        data_mode="rw",
        **changes,
    )


def test_exact_local_ext4_and_readonly_same_device_marker_are_accepted(guard):
    token = check_mounts(guard)
    assert token == ("20", "30", "31", "32", "8:16")


@pytest.mark.parametrize(
    "change",
    [
        "missing-host",
        "missing-data",
        "missing-marker",
        "nfs",
        "wrong-root",
        "wrong-device",
        "marker-device",
        "marker-rw",
        "marker-root",
        "host-ro",
        "super-ro",
        "bind-ro",
        "missing-nodev",
        "root-fallback",
        "stacked",
        "nested-host",
        "nested-local",
        "bad-source",
    ],
)
def test_wrong_mount_or_fallback_is_refused(guard, change):
    host, local = (value.decode() for value in mounts())
    if change == "missing-host":
        host = host.splitlines(keepends=True)[0]
    elif change == "missing-data":
        local = local.splitlines(keepends=True)[1]
    elif change == "missing-marker":
        local = local.splitlines(keepends=True)[0]
    elif change == "nfs":
        host = host.replace("- ext4", "- nfs4")
    elif change == "wrong-root":
        local = local.replace(" /homeserver ", " /other ")
    elif change == "wrong-device":
        local = local.replace("8:16", "8:17", 1)
    elif change == "marker-device":
        local = local.replace("31 1 8:16", "31 1 8:17")
    elif change == "marker-rw":
        local = local.replace(" ro,", " rw,")
    elif change == "marker-root":
        local = local.replace("/.homelab-volume-id", "/elsewhere")
    elif change == "host-ro":
        host = host.replace("/srv/external3 rw,", "/srv/external3 ro,")
    elif change == "super-ro":
        host = host.replace("/dev/sdb rw,", "/dev/sdb ro,")
    elif change == "bind-ro":
        local = local.replace("/data rw,", "/data ro,")
    elif change == "missing-nodev":
        host = host.replace(",nodev", "")
    elif change == "root-fallback":
        host = host.replace("1 1 8:1", "1 1 8:16")
    elif change == "stacked":
        local += local.splitlines(keepends=True)[0]
    elif change == "nested-host":
        host += mount("/srv/external3/homeserver/media", identifier=40)
    elif change == "nested-local":
        local += mount("/data/media", identifier=40)
    elif change == "bad-source":
        host = host.replace("/dev/sdb", "server:/media")
    with pytest.raises(guard.GuardError):
        check_mounts(guard, host.encode(), local.encode())


@pytest.mark.parametrize(
    "marker", [b"", (BOOT + "\n").encode(), FILESYSTEM.encode(), (FILESYSTEM + "\r\n").encode()]
)
def test_marker_is_exact_pinned_filesystem_uuid(guard, marker):
    with pytest.raises(guard.GuardError):
        guard.check_mounts(*mounts(), marker, FILESYSTEM, marker_device="8:16", data_mode="rw")


def test_readonly_data_consumers_still_require_same_local_filesystem(guard):
    host, local = mounts()
    local = local.replace(b"/data rw,", b"/data ro,")
    token = guard.check_mounts(
        host, local, (FILESYSTEM + "\n").encode(), FILESYSTEM, marker_device="8:16", data_mode="ro"
    )
    assert token == ("20", "30", "31", "32", "8:16")


def test_role_without_data_bind_requires_exact_marker_and_no_data_mount(guard):
    host, local = mounts()
    local = b"".join(local.splitlines(keepends=True)[1:])
    token = guard.check_mounts(
        host,
        local,
        (FILESYSTEM + "\n").encode(),
        FILESYSTEM,
        marker_device="8:16",
        data_mode="none",
    )
    assert token == ("20", "none", "31", "32", "8:16")
    with pytest.raises(guard.GuardError):
        guard.check_mounts(
            *mounts(),
            (FILESYSTEM + "\n").encode(),
            FILESYSTEM,
            marker_device="8:16",
            data_mode="none",
        )


@pytest.mark.parametrize(
    "change", ["missing", "writable", "other-source", "other-directory", "nested", "stacked"]
)
def test_lease_directory_is_the_exact_readonly_ssd_bind(guard, change):
    host, local = mounts()
    if change == "missing":
        local = b"".join(local.splitlines(keepends=True)[:2])
    elif change == "writable":
        local = local.replace(b"/run/core-writer-admission ro,", b"/run/core-writer-admission rw,")
    elif change == "other-source":
        local = local.replace(b"32 1 8:1", b"32 1 8:16")
    elif change == "other-directory":
        local = local.replace(b"/var/lib/homelab-storage/core-writer-admission", b"/tmp/lease")
    elif change == "nested":
        local += mount("/run/core-writer-admission/lease", identifier=40).encode()
    elif change == "stacked":
        local += local.splitlines(keepends=True)[2]
    with pytest.raises(guard.GuardError):
        check_mounts(guard, host, local)


@pytest.fixture
def trusted_tree():
    if os.geteuid() != 0:
        pytest.skip("disposable Linux root fixture required for real root-owned input checks")
    with tempfile.TemporaryDirectory(prefix="core-guard-fixture-", dir="/var/lib") as temporary:
        root = Path(temporary)
        path = root / "lease"
        path.write_bytes(lease())
        path.chmod(0o644)
        yield root, path


def test_trusted_read_uses_one_snapshot_across_atomic_replacement(guard, trusted_tree):
    root, path = trusted_tree
    blob, device = guard.read_trusted(path, 4096, expected_mode=0o644)
    assert blob == lease()
    assert device == f"{os.major(path.stat().st_dev)}:{os.minor(path.stat().st_dev)}"
    replacement = root / "replacement"
    replacement.write_bytes(lease(sequence="8"))
    replacement.chmod(0o644)
    replacement.replace(path)
    assert guard.read_trusted(path, 4096, expected_mode=0o644)[0] == lease(sequence="8")


@pytest.mark.parametrize(
    "change",
    [
        "file-symlink",
        "parent-symlink",
        "hardlink",
        "fifo",
        "owner",
        "file-write",
        "parent-write",
        "oversize",
        "missing",
        "wrong-mode",
    ],
)
def test_untrusted_real_files_cannot_supply_writer_authority(guard, trusted_tree, change):
    root, path = trusted_tree
    if change == "file-symlink":
        path.rename(root / "actual")
        path.symlink_to(root / "actual")
    elif change == "parent-symlink":
        (root / "alias").symlink_to(root, target_is_directory=True)
        path = root / "alias/lease"
    elif change == "hardlink":
        os.link(path, root / "alias")
    elif change == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif change == "owner":
        os.chown(path, 1000, 1000)
    elif change == "file-write":
        path.chmod(0o666)
    elif change == "parent-write":
        root.chmod(0o777)
    elif change == "oversize":
        path.write_bytes(b"x" * 4097)
    elif change == "missing":
        path.unlink()
    elif change == "wrong-mode":
        path.chmod(0o600)
    try:
        with pytest.raises(guard.GuardError):
            guard.read_trusted(path, 4096, expected_mode=0o644)
    finally:
        root.chmod(0o700)


def test_metadata_predicate_requires_root_regular_single_link(guard):
    from types import SimpleNamespace

    safe = dict(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1)
    guard.trusted_metadata(SimpleNamespace(**safe), expected_mode=0o644)
    for changes in (
        {"st_uid": 1000},
        {"st_nlink": 2},
        {"st_mode": stat.S_IFREG | 0o664},
        {"st_mode": stat.S_IFLNK | 0o644},
        {"st_mode": stat.S_IFIFO | 0o644},
    ):
        with pytest.raises(guard.GuardError):
            guard.trusted_metadata(SimpleNamespace(**dict(safe, **changes)), expected_mode=0o644)


def test_runtime_check_reads_uptime_last_and_returns_storage_only_snapshot(guard, trusted_tree):
    root, lease_path = trusted_tree
    actual_device = lease_path.stat().st_dev
    device = f"{os.major(actual_device)}:{os.minor(actual_device)}"
    host, local = (value.replace(b"8:16", device.encode()) for value in mounts())
    files = {guard.LEASE_PATH: lease_path}
    for runtime_path, name, value, mode in (
        (guard.BOOT_PATH, "boot", (BOOT + "\n").encode(), 0o444),
        (guard.MARKER_PATH, "marker", (FILESYSTEM + "\n").encode(), 0o444),
        (guard.HOST_MOUNT_PATH, "host", host, 0o444),
    ):
        path = root / name
        path.write_bytes(value)
        path.chmod(mode)
        files[runtime_path] = path
    order = []

    def read(path, limit, **kwargs):
        order.append(path)
        return guard.read_trusted(files[path], limit, **kwargs)

    def kernel_read(path, limit):
        order.append(path)
        return b"100.00 90.00\n" if path == guard.UPTIME_PATH else local

    result = guard.check_runtime(FILESYSTEM, "rw", read=read, kernel_read=kernel_read)
    assert order[-1] == guard.UPTIME_PATH
    assert result["token"] == ("20", "30", "31", "32", device, BOOT, FILESYSTEM, GENERATION)
    assert result["sequence"] == 7
    assert 44 < result["expires_at"] - time.clock_gettime(time.CLOCK_BOOTTIME) <= 45
    assert len(result["lease_sha256"]) == 64
    assert "startup_allowed" not in result


def test_runtime_check_refuses_proof_that_expired_during_processing(guard):
    host, local = mounts()
    inputs = {
        guard.LEASE_PATH: lease(),
        guard.BOOT_PATH: (BOOT + "\n").encode(),
        guard.MARKER_PATH: (FILESYSTEM + "\n").encode(),
        guard.HOST_MOUNT_PATH: host,
    }
    times = iter((1000.0, 1046.0))
    with pytest.raises(guard.GuardError):
        guard.check_runtime(
            FILESYSTEM,
            "rw",
            read=lambda path, limit, **kwargs: (inputs[path], "8:16"),
            kernel_read=lambda path, limit: (
                b"100.00 90.00\n" if path == guard.UPTIME_PATH else local
            ),
            clock=lambda: next(times),
        )


def test_kernel_reader_only_accepts_fixed_proc_inputs(guard, tmp_path):
    assert guard.read_kernel(guard.UPTIME_PATH, 128).endswith(b"\n")
    assert guard.read_kernel(guard.local_mount_path(), guard.MAX_MOUNT_BYTES)
    path = tmp_path / "fake-uptime"
    path.write_text("100.00 90.00\n")
    with pytest.raises(guard.GuardError):
        guard.read_kernel(path, 128)


def snapshot(*, sequence=7, token=("stable",), remaining=10, digest="a" * 64):
    return {
        "token": token,
        "sequence": sequence,
        "expires_at": time.clock_gettime(time.CLOCK_BOOTTIME) + remaining,
        "lease_sha256": digest,
    }


def test_new_proof_sequence_can_renew_without_changing_authority_or_mounts(guard):
    previous = snapshot(remaining=5)
    current = snapshot(sequence=8, remaining=10, digest="b" * 64)
    assert guard.accept_update(previous, current) == current


@pytest.mark.parametrize("change", ["regression", "authority", "reused-sequence", "expired"])
def test_changed_or_replayed_identity_stops_existing_writer(guard, change):
    previous = snapshot()
    current = dict(previous)
    if change == "regression":
        current["sequence"] = 6
    elif change == "authority":
        current["token"] = ("different-mount-or-boot-or-generation",)
    elif change == "reused-sequence":
        current["lease_sha256"] = "b" * 64
    elif change == "expired":
        current["expires_at"] = time.clock_gettime(time.CLOCK_BOOTTIME) - 1
    with pytest.raises(guard.GuardError):
        guard.accept_update(previous, current)


def test_repeated_same_lease_cannot_extend_monotonic_deadline(guard):
    previous = snapshot(remaining=5)
    current = dict(previous, expires_at=previous["expires_at"] + 0.01)
    assert guard.accept_update(previous, current)["expires_at"] == previous["expires_at"]


def test_renewal_cannot_bridge_a_gap_after_previous_authority_expired(guard):
    with pytest.raises(guard.GuardError):
        guard.accept_update(snapshot(remaining=-1), snapshot(sequence=8, digest="b" * 64))


def test_suspend_time_counts_against_admitted_lifetime(guard, monkeypatch):
    admitted = snapshot()
    monkeypatch.setattr(guard.time, "clock_gettime", lambda _clock: admitted["expires_at"] + 1)
    with pytest.raises(guard.GuardError):
        guard.valid_snapshot(admitted)


def test_public_cli_cannot_execute_a_native_command(guard, tmp_path):
    marker = tmp_path / "started"
    for arguments in ([], ["--run", "/bin/sh", "-c", f"touch {marker}"]):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, check=False
        )
        assert result.returncode != 0
        assert not marker.exists()


def child_command(path, *, ignore_term=False, exit_code=None, grandchild=False):
    program = (
        "import os, pathlib, signal, subprocess, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "pids = [os.getpid()]\n"
    )
    if grandchild:
        program += (
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)'])\n"
            "pids.append(child.pid)\n"
        )
    program += "pathlib.Path(sys.argv[1]).write_text(','.join(map(str, pids)))\n"
    program += f"sys.exit({exit_code})\n" if exit_code is not None else "time.sleep(30)\n"
    return [sys.executable, "-c", program, str(path)]


def assert_recorded_processes_stopped(path):
    for identifier in path.read_text().split(","):
        proc = Path("/proc") / identifier / "stat"
        try:
            state = proc.read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            continue
        assert state == "Z"


def supervise(guard, command, checker, **kwargs):
    return guard.supervise(
        command, checker, poll_interval=0.01, check_timeout=0.1, term_grace=0.05, **kwargs
    )


def test_denial_never_spawns_the_native_child(guard, tmp_path):
    marker = tmp_path / "child"

    def refused():
        raise guard.GuardError("hold")

    assert supervise(guard, child_command(marker), refused) == 78
    assert not marker.exists()


def test_expiry_stops_child_and_term_ignoring_grandchild(guard, tmp_path):
    marker = tmp_path / "children"
    initial = snapshot(remaining=0.3)
    before = time.monotonic()
    result = supervise(
        guard, child_command(marker, ignore_term=True, grandchild=True), lambda: initial
    )
    assert result == 78
    assert time.monotonic() - before < 1.0
    assert marker.exists()
    assert_recorded_processes_stopped(marker)


def test_stuck_checker_cannot_prevent_expiry_containment_or_spawn_more_checks(guard, tmp_path):
    marker = tmp_path / "child"
    release = threading.Event()
    calls = []
    initial = snapshot(remaining=0.3)

    def checker():
        calls.append(1)
        if len(calls) > 2:
            release.wait(5)
        return initial

    before = time.monotonic()
    try:
        result = guard.supervise(
            child_command(marker), checker, poll_interval=0.01, check_timeout=2, term_grace=0.05
        )
    finally:
        release.set()
    assert result == 78
    assert time.monotonic() - before < 1.0
    assert len(calls) == 3
    assert_recorded_processes_stopped(marker)


def test_native_exit_is_preserved_and_orphan_descendants_are_stopped(guard, tmp_path):
    marker = tmp_path / "children"
    initial = snapshot()
    result = supervise(guard, child_command(marker, exit_code=17, grandchild=True), lambda: initial)
    assert result == 17
    assert_recorded_processes_stopped(marker)


def test_manual_stop_is_preserved_without_restart_or_state_mutation(guard, tmp_path):
    marker = tmp_path / "child"
    stopped = threading.Event()
    timer = threading.Timer(0.2, stopped.set)
    timer.start()
    initial = snapshot()
    try:
        result = supervise(guard, child_command(marker), lambda: initial, stop_event=stopped)
    finally:
        timer.cancel()
        timer.join()
    assert result == 0
    assert_recorded_processes_stopped(marker)
    assert stopped.is_set()


def test_check_exception_after_start_contains_child(guard, tmp_path):
    marker = tmp_path / "child"
    initial = snapshot()
    calls = []

    def checker():
        calls.append(1)
        if marker.exists():
            raise OSError("PRIVATE error must not be printed")
        return initial

    assert supervise(guard, child_command(marker), checker) == 78
    assert_recorded_processes_stopped(marker)


def test_second_prelaunch_check_must_preserve_the_original_identity(guard, tmp_path):
    marker = tmp_path / "child"
    values = iter((snapshot(), snapshot(token=("changed",))))
    assert supervise(guard, child_command(marker), lambda: next(values)) == 78
    assert not marker.exists()


def test_manual_sigterm_stops_the_native_group_and_returns_success(tmp_path):
    marker = tmp_path / "children"
    program = (
        "import importlib.util, time\n"
        f"spec = importlib.util.spec_from_file_location('guard', {str(SCRIPT)!r})\n"
        "guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)\n"
        f"value = {snapshot()!r}\n"
        "value['expires_at'] = time.clock_gettime(time.CLOCK_BOOTTIME) + 10\n"
        f"raise SystemExit(guard.supervise({child_command(marker, grandchild=True)!r}, "
        "lambda: value, poll_interval=0.01, term_grace=2))\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        parent.send_signal(signal.SIGTERM)
        out, error = parent.communicate(timeout=8)
        assert (parent.returncode, out, error) == (0, b"", b"")
        assert_recorded_processes_stopped(marker)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if marker.exists():
            identifier = int(marker.read_text().split(",")[0])
            try:
                os.killpg(identifier, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stuck_preflight_never_starts_child(guard, tmp_path):
    marker = tmp_path / "child"
    release = threading.Event()

    def checker():
        release.wait(5)
        return snapshot()

    before = time.monotonic()
    try:
        assert supervise(guard, child_command(marker), checker) == 78
    finally:
        release.set()
    assert time.monotonic() - before < 1
    assert not marker.exists()


def test_observation_error_still_kills_term_ignoring_group(guard, tmp_path, monkeypatch):
    marker = tmp_path / "child"
    stopped = threading.Event()
    initial = snapshot()

    def checker():
        if marker.exists():
            stopped.set()
        return initial

    def unavailable(_identifier):
        raise PermissionError("synthetic proc observation failure")

    monkeypatch.setattr(guard, "group_has_live_members", unavailable)
    try:
        assert (
            supervise(guard, child_command(marker, ignore_term=True), checker, stop_event=stopped)
            == 78
        )
        assert_recorded_processes_stopped(marker)
    finally:
        if marker.exists():
            try:
                os.killpg(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_suspend_jump_expires_writer_while_new_check_is_blocked(guard, tmp_path, monkeypatch):
    marker = tmp_path / "child"
    release = threading.Event()
    offset = [0]
    calls, blocked_at = [], []
    real_clock = guard.uptime_clock
    initial = snapshot()
    monkeypatch.setattr(guard, "uptime_clock", lambda: real_clock() + offset[0])

    def checker():
        calls.append(1)
        if marker.exists():
            blocked_at.append(len(calls))
            offset[0] = 60
            release.wait(5)
        return initial

    before = time.monotonic()
    try:
        assert (
            guard.supervise(
                child_command(marker), checker, poll_interval=0.01, check_timeout=3, term_grace=0.1
            )
            == 78
        )
    finally:
        release.set()
    assert time.monotonic() - before < 1
    assert offset == [60]
    assert blocked_at == [len(calls)]
    assert_recorded_processes_stopped(marker)


NATIVE_ROLES = ("sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "seerr", "byparr")


@pytest.mark.parametrize("role", NATIVE_ROLES)
def test_native_role_is_fixed_foreground_with_clean_environment(guard, role):
    plan = guard.native_plan(role)
    assert plan["data_mode"] == (
        "rw" if role in {"sonarr", "radarr", "bazarr", "qbittorrent"} else "none"
    )
    assert plan["argv"][0].startswith("/") and plan["cwd"].startswith("/")
    assert not {"/init", "sh", "-c", "--daemon", "-d", "uv", "npm"}.intersection(plan["argv"])
    assert "QBT_DAEMON" not in plan["env"] and "LD_PRELOAD" not in plan["env"]
    assert "HOME" in plan["env"] and "PATH" in plan["env"]
    first_hash = guard.native_plan_digest(plan)
    assert len(first_hash) == 64
    plan["argv"].append("--not-approved")
    assert guard.native_plan_digest(guard.native_plan(role)) == first_hash
    assert guard.native_plan_digest(plan) != first_hash


def activation_bytes(guard, role="sonarr", **changes):
    value = {"schema": 1, "scope": "production-core-activation", "role": role,
             "filesystem_uuid": FILESYSTEM, "data_mode": guard.native_plan(role)["data_mode"],
             "image_id": "sha256:" + "a" * 64,
             "native_command_sha256": guard.native_plan_digest(guard.native_plan(role)),
             "restore_receipt_sha256": "b" * 64, "configuration_receipt_sha256": "c" * 64,
             "native_process_group_receipt_sha256": "d" * 64}
    value.update(changes)
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def parse_activation(guard, raw, role="sonarr", **kwargs):
    return guard.parse_activation(
        raw, role, FILESYSTEM, hashlib.sha256(raw).hexdigest(), **kwargs
    )


@pytest.mark.parametrize("role", NATIVE_ROLES)
def test_pinned_operational_receipt_is_distinct_from_readiness_fixture(guard, role):
    raw = activation_bytes(guard, role)
    result = parse_activation(guard, raw, role)
    assert result["role"] == role
    fixture = json.dumps({"schema": 1, "role": role, "status": "PASS", "http_ready": True,
                          "production_ready": False, "under_writer_guard_verified": False})
    with pytest.raises(guard.GuardError):
        parse_activation(guard, fixture.encode(), role)


@pytest.mark.parametrize("changes", [
    {"scope": "isolated-native-rehearsal"}, {"schema": True}, {"schema": 2},
    {"role": "seerr"}, {"filesystem_uuid": BOOT}, {"data_mode": "ro"},
    {"native_command_sha256": "f" * 64}, {"image_id": "repo:latest"},
    {"restore_receipt_sha256": None}, {"configuration_receipt_sha256": "pending"},
    {"native_process_group_receipt_sha256": "D" * 64}, {"approved": True},
])
def test_incomplete_or_wrong_activation_receipt_is_refused(guard, changes):
    with pytest.raises(guard.GuardError):
        parse_activation(guard, activation_bytes(guard, **changes))


def test_activation_receipt_requires_pinned_hash_and_unique_fields(guard):
    raw = activation_bytes(guard)
    with pytest.raises(guard.GuardError):
        guard.parse_activation(raw, "sonarr", FILESYSTEM, "0" * 64)
    duplicate = raw.rstrip()[:-1] + b', "schema":1}'
    with pytest.raises(guard.GuardError):
        parse_activation(guard, duplicate)
    for invalid in (b"[]", b"null", b"x" * 8193):
        with pytest.raises(guard.GuardError):
            parse_activation(guard, invalid)


def activation_mounts(role="sonarr", **changes):
    row = dict(identifier=80, device="8:1", root=f"/srv/appdata/core-activation/{role}",
               mode="ro")
    row.update(changes)
    return mount("/run/core-activation", **row).encode()


def test_activation_mount_is_exact_readonly_guest_ssd_bind(guard):
    host, local = mounts()
    token = guard.check_activation_mount(host, local + activation_mounts(), "sonarr", "8:1")
    assert token == ("80", "8:1")


@pytest.mark.parametrize("change", ["rw", "device", "role", "file", "stacked", "nested"])
def test_wrong_activation_mount_never_supplies_runtime_authority(guard, change):
    host, local = mounts()
    extra = activation_mounts()
    if change == "rw":
        extra = activation_mounts(mode="rw")
    elif change == "device":
        extra = activation_mounts(device="8:16")
    elif change == "role":
        extra = activation_mounts("seerr")
    elif change == "file":
        extra = extra.replace(b"/run/core-activation ", b"/run/core-activation/receipt.json ")
    elif change == "stacked":
        extra += extra
    elif change == "nested":
        extra += mount("/run/core-activation/receipt.json", identifier=81).encode()
    with pytest.raises(guard.GuardError):
        guard.check_activation_mount(host, local + extra, "sonarr", "8:1")


def test_execution_checker_pins_activation_as_part_of_storage_identity(guard, monkeypatch):
    storage = snapshot()
    monkeypatch.setattr(guard, "check_runtime", lambda *_: dict(storage))
    monkeypatch.setattr(guard, "check_activation", lambda *_: ("80", "8:1", "f" * 64))
    checked = guard.execution_check("sonarr", FILESYSTEM, "f" * 64)
    assert checked["token"] == (*storage["token"], "80", "8:1", "f" * 64)
    assert checked["expires_at"] == storage["expires_at"]


def test_run_cli_has_no_arbitrary_command_or_fixture_override(guard, tmp_path):
    for args in (["--run", "shell"], ["--run", "sonarr", "--data-mode", "ro"],
                 ["--run", "sonarr", "--fixture"],
                 ["--run", "sonarr", "--", "/bin/sh", "-c", "echo invalid"]):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                                check=False, timeout=3)
        assert result.returncode != 0


def test_cli_run_delegates_only_fixed_context_and_checker(guard, monkeypatch):
    seen = {}

    def supervise_native(command, checker, **kwargs):
        seen.update(command=command, kwargs=kwargs, check=checker())
        return 17

    monkeypatch.setattr(guard, "execution_check", lambda *args: ("checked", args))
    monkeypatch.setattr(guard, "supervise", supervise_native)
    code = guard.main(["--run", "sonarr", "--filesystem-uuid", FILESYSTEM,
                       "--activation-sha256", "f" * 64])
    plan = guard.native_plan("sonarr")
    assert code == 17
    assert seen["command"] == plan["argv"]
    assert seen["kwargs"] == {"cwd": plan["cwd"], "env": plan["env"]}
    assert seen["check"] == ("checked", ("sonarr", FILESYSTEM, "f" * 64))


def test_activation_reader_checks_real_root_file_and_rejects_changed_bytes(guard, trusted_tree):
    trusted_tree, _ = trusted_tree
    receipt = trusted_tree / "activation.json"
    raw = activation_bytes(guard)
    receipt.write_bytes(raw)
    receipt.chmod(0o444)
    host, local = mounts()
    host_path = trusted_tree / "host-mountinfo"
    host_path.write_bytes(host)
    host_path.chmod(0o444)

    def read(path, limit, **kwargs):
        value, _ = guard.read_trusted(
            receipt if path == guard.ACTIVATION_PATH else host_path, limit, **kwargs
        )
        return value, "8:1"  # Synthetic mount identity, real trusted file reader.

    digest = hashlib.sha256(raw).hexdigest()
    token = guard.check_activation("sonarr", FILESYSTEM, digest, read=read,
                                  kernel_read=lambda *_: local + activation_mounts())
    assert token == ("80", "8:1", digest)
    receipt.write_bytes(b'{"PRIVATE_SENTINEL":true}')
    with pytest.raises(guard.GuardError, match="^activation_changed$"):
        guard.check_activation("sonarr", FILESYSTEM, digest, read=read,
                               kernel_read=lambda *_: local + activation_mounts())


@pytest.mark.parametrize("stop", ["preflight_hold", "hold", "expiry", "receipt_change", "manual"])
def test_run_cli_real_subprocess_contains_group_without_restart(guard, trusted_tree, stop):
    """Only mount/device observations and the native executable are synthetic.

    The shipped CLI, receipt reader, lease parser, watchdog and process signals
    execute in a separate interpreter. No fixture option exists in the public CLI.
    """
    trusted_tree, _ = trusted_tree
    marker = trusted_tree / "children"
    plan = guard.native_plan("sonarr")
    plan["argv"] = child_command(marker, grandchild=True)
    plan["argv"][2] = "import os; assert 'PRIVATE_SENTINEL' not in os.environ\n" + plan["argv"][2]
    plan["cwd"] = str(trusted_tree)
    original_plan = guard.native_plan
    guard.native_plan = lambda _: plan
    try:
        receipt = activation_bytes(guard)
    finally:
        guard.native_plan = original_plan
    digest = hashlib.sha256(receipt).hexdigest()
    host, local = mounts()
    files = {
        str(guard.BOOT_PATH): ("boot", (BOOT + "\n").encode(), 0o444),
        str(guard.MARKER_PATH): ("marker", (FILESYSTEM + "\n").encode(), 0o444),
        str(guard.HOST_MOUNT_PATH): ("host", host, 0o444),
        str(guard.ACTIVATION_PATH): ("activation", receipt, 0o444),
    }
    mapped = {}
    for source, (name, raw, mode) in files.items():
        path = trusted_tree / name
        path.write_bytes(raw)
        path.chmod(mode)
        mapped[source] = str(path)
    lease_path = trusted_tree / "lease"
    seconds = 2 if stop == "expiry" else 20
    lease_path.write_bytes(lease(expires_uptime_seconds=str(int(guard.uptime_clock()) + seconds)))
    if stop == "preflight_hold":
        lease_path.write_bytes(lease(state="hold", physical_generation="none",
                                     expires_uptime_seconds="0"))
    lease_path.chmod(0o644)
    mapped[str(guard.LEASE_PATH)] = str(lease_path)
    program = f"""
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('guard', {str(SCRIPT)!r})
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
mapping = {mapped!r}
local = {local + activation_mounts()!r}
plan = {plan!r}
def read_fixture(path, limit, **kwargs):
    raw, _ = guard.read_trusted(Path(mapping[str(path)]), limit, **kwargs)
    return raw, '8:16' if path == guard.MARKER_PATH else '8:1'
def kernel_fixture(path, limit):
    return guard.read_kernel(path, limit) if path == guard.UPTIME_PATH else local
runtime = guard.check_runtime
activation = guard.check_activation
guard.check_runtime = lambda uuid, mode: runtime(
    uuid, mode, read=read_fixture, kernel_read=kernel_fixture)
guard.check_activation = lambda role, uuid, pin: activation(
    role, uuid, pin, read=read_fixture, kernel_read=kernel_fixture)
guard.native_plan = lambda role: plan
raise SystemExit(guard.main(['--run', 'sonarr', '--filesystem-uuid', {FILESYSTEM!r},
                            '--activation-sha256', {digest!r}]))
"""
    environment = {**os.environ, "PRIVATE_SENTINEL": "must-not-reach-native"}
    parent = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environment)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if stop == "preflight_hold":
            assert not marker.exists()
        else:
            assert marker.exists()
            if stop == "manual":
                parent.send_signal(signal.SIGTERM)
            elif stop == "hold":
                lease_path.write_bytes(lease(state="hold", physical_generation="none",
                                             expires_uptime_seconds="0", sequence="8"))
            elif stop == "receipt_change":
                Path(mapped[str(guard.ACTIVATION_PATH)]).write_bytes(b"{}")
        out, error = parent.communicate(timeout=10)
        assert parent.returncode == (0 if stop == "manual" else 78)
        assert out == b"" and error == b""
        if marker.exists():
            assert_recorded_processes_stopped(marker)
            assert len(marker.read_text().split(",")) == 2
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if marker.exists():
            try:
                os.killpg(int(marker.read_text().split(",")[0]), signal.SIGKILL)
            except ProcessLookupError:
                pass
