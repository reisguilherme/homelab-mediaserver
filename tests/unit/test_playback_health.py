"""Playback telemetry fails closed without mutating application or storage state."""

import http.client
import importlib.util
import os
import stat
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/playback-health.py"
BOOT = "ca9a5731-8b28-4400-8ee9-138e1485de61"
GENERATION = "21ea0f809f77421a9ce2a040376a43da"


@pytest.fixture
def health():
    assert SCRIPT.is_file(), "playback health producer is missing"
    spec = importlib.util.spec_from_file_location("playback_health", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lease(*, state="ready", boot=BOOT, expires="145", generation=GENERATION):
    return (
        f"version=1\nstate={state}\nboot_id={boot}\ngeneration={generation}\n"
        f"expires_uptime_seconds={expires}\n"
    ).encode()


@pytest.mark.parametrize(
    "uptime,expires,remaining", [("100.00", "145", 45), ("100.99", "101", 0.01)]
)
def test_fresh_lease_observes_guard_lifetime(health, uptime, expires, remaining):
    admitted, seconds = health.admission(
        lease(expires=expires), (BOOT + "\n").encode(), f"{uptime} 90.00\n".encode()
    )
    assert admitted == 1
    assert seconds == pytest.approx(remaining)


@pytest.mark.parametrize(
    "value",
    [
        b"",
        lease(state="hold"),
        lease(state="hold", expires="0"),
        lease(expires="100"),
        lease(expires="99"),
        lease(expires="146"),
        lease(boot="ca9a5731-8b28-4400-8ee9-138e1485de62"),
        lease(boot=BOOT.upper()),
        lease(generation=GENERATION.upper()),
        lease(generation="z" * 32),
        lease(expires="0145"),
        lease(expires="145.0"),
        lease(expires="-1"),
        lease(expires="10000000000"),
        lease().replace(b"version=1", b"version=2"),
        lease().replace(b"state=ready", b"state=ready\x00"),
        lease().replace(b"\n", b"\r\n"),
        lease().rstrip(b"\n"),
        lease() + b"state=ready\n",
        lease() + b"\n",
        b" " * 513,
        lease().replace(b"version=1\nstate=ready", b"state=ready\nversion=1"),
    ],
)
def test_invalid_hold_expired_or_old_boot_never_admits(health, value):
    assert health.admission(value, (BOOT + "\n").encode(), b"100.00 90.00\n") == (0, 0)


@pytest.mark.parametrize(
    "boot,uptime",
    [
        (BOOT.encode(), b"100.00 90.00\n"),
        ((BOOT + "\n\n").encode(), b"100.00 90.00\n"),
        ((BOOT + "\n").encode(), b"100.00\n"),
        ((BOOT + "\n").encode(), b"nan 0.00\n"),
        ((BOOT + "\n").encode(), b"100.00 90.00"),
    ],
)
def test_invalid_clock_or_boot_is_not_admission(health, boot, uptime):
    assert health.admission(lease(), boot, uptime) == (0, 0)


@pytest.fixture
def trusted_fixture(health, tmp_path, monkeypatch):
    directory = tmp_path / "admission"
    directory.mkdir(mode=0o755)
    path = directory / "lease"
    path.write_bytes(lease())
    path.chmod(0o644)
    original = os.fstat

    def fixture_root_owner(fd):
        values = list(original(fd))
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(health.os, "fstat", fixture_root_owner)
    return path


def test_secure_read_accepts_bounded_regular_root_owned_lease(health, trusted_fixture):
    assert health.read_lease(trusted_fixture) == lease()


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "symlink",
        "hardlink",
        "writable",
        "oversize",
        "directory",
        "parent_symlink",
        "parent_writable",
        "fifo",
    ],
)
def test_secure_read_refuses_untrusted_files(health, trusted_fixture, kind):
    path = trusted_fixture
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        path.rename(path.with_suffix(".real"))
        path.symlink_to(path.with_suffix(".real"))
    elif kind == "hardlink":
        os.link(path, path.with_suffix(".link"))
    elif kind == "writable":
        path.chmod(0o666)
    elif kind == "oversize":
        path.write_bytes(b"x" * 513)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "parent_symlink":
        linked = path.parent.with_name("linked")
        linked.symlink_to(path.parent, target_is_directory=True)
        path = linked / "lease"
    elif kind == "parent_writable":
        path.parent.chmod(0o777)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        health.read_lease(path)


def test_nonroot_owner_is_refused(health, tmp_path, monkeypatch):
    path = tmp_path / "lease"
    path.write_bytes(lease())
    original = os.fstat

    def untrusted_owner(fd):
        values = list(original(fd))
        values[4] = 12345
        return os.stat_result(values)

    monkeypatch.setattr(health.os, "fstat", untrusted_owner)
    with pytest.raises(ValueError):
        health.read_lease(path)


@pytest.mark.parametrize(
    "address",
    [
        "https://10.0.0.1",
        "user@10.0.0.1",
        "10.0.0.1/path",
        "10.0.0.1:8096",
        "10.0.0.1\n",
        "8.8.8.8",
        "127.0.0.1",
        "169.254.1.1",
        "::1",
        "192.0.2.20",
        "0.0.0.0",
        "224.0.0.1",
        "localhost",
    ],
)
def test_only_literal_private_ipv4_is_a_probe_target(health, address):
    with pytest.raises(ValueError):
        health.private_ipv4(address)


@pytest.mark.parametrize("address", ["10.0.0.1", "172.16.0.1", "192.168.1.1"])
def test_private_target_is_preserved(health, address):
    assert health.private_ipv4(address) == address


class Response:
    def __init__(self, status=200, body=b"Healthy"):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        assert size == 32
        return self.body[:size]


@pytest.mark.parametrize(
    "status,body,want",
    [
        (200, b"Healthy", True),
        (200, b"Healthy\n", True),
        (200, b"Healthy\r\n", True),
        (503, b"Healthy", False),
        (302, b"Healthy", False),
        (200, b"Healthy ", False),
        (200, b"Healthy\n\n", False),
        (200, b"x" * 100, False),
        (200, b"Healthy" + b" " * 100, False),
    ],
)
def test_probe_requires_exact_bounded_health_response(health, status, body, want):
    def open_response(url, timeout):
        assert url == "http://10.0.0.1:8096/jellyfin/health"
        assert timeout == 3
        return Response(status, body)

    assert health.probe("10.0.0.1", open_url=open_response) is want


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionRefusedError(),
        urllib.error.URLError("fixture"),
        http.client.BadStatusLine("fixture"),
    ],
)
def test_probe_failure_is_zero(health, error):
    def fail(*_args, **_kwargs):
        raise error

    assert health.probe("10.0.0.1", open_url=fail) is False


def test_http_opener_does_not_follow_redirects_or_environment_proxy(health, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    opener = health.http_opener()
    assert not any(getattr(handler, "proxies", {}) for handler in opener.handlers)
    redirect = next(
        handler
        for handler in opener.handlers
        if isinstance(handler, health.urllib.request.HTTPRedirectHandler)
    )
    assert redirect.redirect_request(None, None, 302, "Found", {}, "http://other.invalid") is None


@pytest.mark.parametrize(
    "lease_bytes,up,admitted",
    [
        (lease(), True, 1),
        (lease(state="hold"), True, 0),
        (lease(expires="100"), True, 0),
        (lease(), False, 1),
    ],
)
def test_observation_reports_admission_independently_without_healthy_override(
    health, lease_bytes, up, admitted
):
    reads = {
        health.BOOT_PATH: (BOOT + "\n").encode(),
        health.UPTIME_PATH: b"100.00 90.00\n",
    }
    metrics = health.collect(
        "10.0.0.1",
        lease_reader=lambda: lease_bytes,
        read=reads.__getitem__,
        probe_fn=lambda _address: up,
        now=lambda: 1234.0,
    )
    assert metrics["homeserver_playback_jellyfin_up"] == int(up and admitted)
    assert metrics["homeserver_playback_storage_admitted"] == admitted
    assert metrics["homeserver_playback_lease_seconds_remaining"] == (45 if admitted else 0)
    assert metrics["homeserver_playback_observed_timestamp_seconds"] == 1234
    assert len(metrics) == 4


def test_missing_lease_still_publishes_failed_admission(health):
    def missing():
        raise FileNotFoundError()

    metrics = health.collect(
        "10.0.0.1",
        lease_reader=missing,
        read=lambda _path: b"",
        probe_fn=lambda _address: True,
        now=lambda: 1234.0,
    )
    assert metrics["homeserver_playback_storage_admitted"] == 0
    assert metrics["homeserver_playback_jellyfin_up"] == 0


def test_atomic_output_has_mode_and_no_labels_or_partial_file(health, tmp_path):
    target = tmp_path / "playback.prom"
    target.write_text("old observation\n")
    old_reader = target.open()
    health.publish({"homeserver_playback_jellyfin_up": 0}, target)
    assert old_reader.read() == "old observation\n"
    old_reader.close()
    assert target.read_text() == "homeserver_playback_jellyfin_up 0\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert list(tmp_path.iterdir()) == [target]


def test_producer_failure_preserves_previous_timestamp(health, tmp_path, monkeypatch):
    target = tmp_path / "playback.prom"
    target.write_text("homeserver_playback_observed_timestamp_seconds 1234\n")

    def fail_replace(*_args):
        raise OSError("fixture write failure")

    monkeypatch.setattr(health.os, "replace", fail_replace)
    with pytest.raises(OSError):
        health.publish({"homeserver_playback_observed_timestamp_seconds": 5678}, target)
    assert target.read_text() == "homeserver_playback_observed_timestamp_seconds 1234\n"
    assert list(tmp_path.iterdir()) == [target]


def test_epoch_timestamp_keeps_seconds_precision(health, tmp_path):
    target = tmp_path / "playback.prom"
    health.publish({"homeserver_playback_observed_timestamp_seconds": 1794877213.125}, target)
    assert float(target.read_text().split()[1]) == 1794877213.125
