import asyncio
import sys
from hashlib import sha1
from types import SimpleNamespace

import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.worker import magnet_metadata as resolver

INFO = b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e"
TORRENT = b"d4:info" + INFO + b"e"
MAGNET = "magnet:?xt=urn:btih:" + sha1(INFO).hexdigest()


class FakeLT:
    torrent_flags = SimpleNamespace(upload_mode=1, auto_managed=2, paused=4)

    def __init__(self, info=INFO, payload_bytes=0):
        self.info = info
        self.payload_bytes = payload_bytes
        self.params = SimpleNamespace(flags=6, url_seeds=["http://payload"], http_seeds=[])
        self.removed = False

    def parse_magnet_uri(self, magnet):
        assert magnet == MAGNET
        return self.params

    def session(self, settings):
        self.settings = settings
        return self

    def add_torrent(self, params):
        assert params is self.params
        return self

    def status(self):
        return SimpleNamespace(has_metadata=True, total_payload_download=self.payload_bytes)

    def torrent_file(self):
        return self

    def metadata(self):
        return self.info

    def remove_torrent(self, handle):
        assert handle is self
        self.removed = True


def test_native_parameters_prevent_payload_and_file_creation():
    native = FakeLT()
    assert resolver._fetch_info(MAGNET, native) == TORRENT
    assert native.params.flags & native.torrent_flags.upload_mode
    assert not native.params.flags & (
        native.torrent_flags.auto_managed | native.torrent_flags.paused
    )
    assert native.params.file_priorities == [0] * 4096
    assert native.params.save_path.startswith("/dev/null/")
    assert native.params.url_seeds == native.params.http_seeds == []
    assert native.params.max_connections == 20
    assert native.settings["connections_limit"] == 20
    assert native.settings["max_metadata_size"] == 16 * 1024 * 1024
    assert not native.settings["enable_upnp"]
    assert not native.settings["enable_natpmp"]
    assert not native.settings["enable_lsd"]
    assert native.settings["listen_interfaces"] == "0.0.0.0:0"
    assert native.removed


@pytest.mark.parametrize("info", [b"invalid", INFO.replace(b"test.mp4", b"fake.mp4")])
def test_native_metadata_must_match_trusted_hash(info):
    native = FakeLT(info)
    assert resolver._fetch_info(MAGNET, native) is None
    assert native.removed


def test_native_payload_invariant_refuses_even_valid_metadata():
    native = FakeLT(payload_bytes=1)
    assert resolver._fetch_info(MAGNET, native) is None
    assert native.removed


def test_real_linux_binding_accepts_flags_and_raw_info_without_network():
    import libtorrent as lt

    captured = []

    def parse_magnet(magnet):
        params = lt.parse_magnet_uri(magnet)
        # Supply a tiny fixture directly: no discovery or metadata peer is used.
        params.ti = lt.torrent_info(TORRENT)
        captured.append(params)
        return params

    def isolated_session(settings):
        return lt.session({**settings, "enable_dht": False})

    binding = SimpleNamespace(torrent_flags=lt.torrent_flags,
                              parse_magnet_uri=parse_magnet, session=isolated_session)
    assert resolver._fetch_info(MAGNET, binding) == TORRENT
    assert captured[0].max_connections == 20
    assert captured[0].file_priorities == [0] * 4096
    assert captured[0].save_path == "/dev/null/homeserver-metadata"


def child_script(monkeypatch, source):
    monkeypatch.setattr(resolver, "_child_command", lambda: [sys.executable, "-c", source])


@pytest.mark.asyncio
async def test_valid_child_bytes_keep_hash_and_merge_magnet_trackers(monkeypatch):
    child_script(monkeypatch,
                 "import sys; sys.stdin.read(); sys.stdout.buffer.write(" + repr(TORRENT) + ")")
    magnet = MAGNET + "&tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce"
    result = await resolver.resolve_magnet_metadata(magnet)
    assert inspect_torrent(result).infohash == sha1(INFO).hexdigest()
    assert b"udp://tracker.example:80/announce" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [b"broken", TORRENT.replace(b"test.mp4", b"fake.mp4")])
async def test_untrusted_child_bytes_fail_closed(monkeypatch, output):
    child_script(monkeypatch, "import sys; sys.stdout.buffer.write(" + repr(output) + ")")
    assert await resolver.resolve_magnet_metadata(MAGNET) is None


@pytest.mark.asyncio
async def test_child_failure_is_unknown_and_does_not_log_urls(monkeypatch, capsys):
    child_script(monkeypatch,
                 "import sys; print('secret tracker URL', file=sys.stderr); sys.exit(1)")
    assert await resolver.resolve_magnet_metadata(MAGNET) is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
async def test_oversized_stdout_is_rejected_and_child_reaped(monkeypatch):
    monkeypatch.setattr(resolver, "_MAX_METADATA_BYTES", 100)
    child_script(monkeypatch,
                 "import sys, time; sys.stdout.buffer.write(b'x'*1000000); "
                 "sys.stdout.flush(); time.sleep(60)")
    processes = record_processes(monkeypatch)
    assert await asyncio.wait_for(resolver.resolve_magnet_metadata(MAGNET), 3) is None
    assert processes[0].returncode is not None


def record_processes(monkeypatch):
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", recording)
    return processes


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_child(monkeypatch):
    monkeypatch.setattr(resolver, "_TIMEOUT_SECONDS", 0.1)
    child_script(monkeypatch, "import time; time.sleep(60)")
    processes = record_processes(monkeypatch)
    assert await resolver.resolve_magnet_metadata(MAGNET) is None
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_child(monkeypatch):
    child_script(monkeypatch, "import time; time.sleep(60)")
    processes = record_processes(monkeypatch)
    task = asyncio.create_task(resolver.resolve_magnet_metadata(MAGNET))
    for _ in range(100):
        if processes:
            break
        await asyncio.sleep(0.01)
    assert processes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_invalid_magnet_never_starts_native_process(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("native process started")

    monkeypatch.setattr(resolver.asyncio, "create_subprocess_exec", forbidden)
    assert await resolver.resolve_magnet_metadata("magnet:?xt=urn:btih:bad") is None
