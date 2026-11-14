"""Physical Linux stream-copy preserves all tracks and source hardlinks."""

import importlib
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Linux ffmpeg filesystem contract",
)


def api():
    try:
        return importlib.import_module("homeserver_control.worker.audited_remux")
    except ModuleNotFoundError:
        pytest.fail("audited stream-copy remux implementation missing")


def create_part(root, name, *, color="red", rate=48000):
    sub = root / (name + ".srt")
    sub.write_text("1\n00:00:00,100 --> 00:00:00,800\n" + name + "\n")
    path = root / (name + ".mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=" + color + ":s=160x96:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=" + str(rate) + ":duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=" + str(rate) + ":duration=1",
            "-i",
            str(sub),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-map",
            "3:s",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=por",
            "-metadata:s:s:0",
            "language=eng",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.mark.asyncio
async def test_streamcopy_all_tracks_both_parts_and_native_hardlink(tmp_path):
    mod = api()
    a = create_part(tmp_path, "first")
    b = create_part(tmp_path, "second", color="blue")
    before = [mod.file_identity(p) for p in (a, b)]
    out = tmp_path / "output.partial.mp4"
    receipt = await mod.remux_parts(
        [a, b], out, max_bytes=2 * (a.stat().st_size + b.stat().st_size)
    )
    assert [mod.file_identity(p) for p in (a, b)] == before
    assert receipt["inputs"] == before
    assert receipt["recipe"] == "concat_mp4_streamcopy_v1"
    probe = mod.probe_streams(out)
    assert [s["codec_type"] for s in probe["streams"]] == ["video", "audio", "audio", "subtitle"]
    assert [s.get("tags", {}).get("language") for s in probe["streams"][1:]] == [
        "eng",
        "por",
        "eng",
    ]
    assert 1.9 < float(probe["format"]["duration"]) < 2.2
    assert int(probe["streams"][0]["nb_frames"]) == 50
    text = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(out),
            "-map",
            "0:s:0",
            "-c:s",
            "srt",
            "-f",
            "srt",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert b"first" in text and b"second" in text
    native = tmp_path / "library.mp4"
    os.link(out, native)
    assert native.stat().st_ino == out.stat().st_ino and native.stat().st_dev == out.stat().st_dev
    assert receipt["output"]["size_bytes"] == out.stat().st_size


@pytest.mark.asyncio
async def test_incompatible_part_keeps_sources_and_creates_no_output(tmp_path):
    mod = api()
    a = create_part(tmp_path, "first")
    b = create_part(tmp_path, "second", rate=44100)
    out = tmp_path / "output.partial.mp4"
    with pytest.raises(ValueError, match="incompatible"):
        await mod.remux_parts([a, b], out, max_bytes=2 * (a.stat().st_size + b.stat().st_size))
    assert a.is_file() and b.is_file() and not out.exists()


@pytest.mark.asyncio
async def test_unknown_existing_output_is_never_overwritten(tmp_path):
    mod = api()
    a = create_part(tmp_path, "first")
    b = create_part(tmp_path, "second")
    out = tmp_path / "output.partial.mp4"
    out.write_bytes(b"unknown")
    with pytest.raises((ValueError, FileExistsError)):
        await mod.remux_parts([a, b], out, max_bytes=100000)
    assert out.read_bytes() == b"unknown"


@pytest.mark.asyncio
async def test_real_output_byte_limit_holds_without_source_changes(tmp_path):
    mod = api()
    a = create_part(tmp_path, "first")
    b = create_part(tmp_path, "second")
    before = [mod.file_identity(p) for p in (a, b)]
    out = tmp_path / "output.partial.mp4"
    with pytest.raises(ValueError):
        await mod.remux_parts([a, b], out, max_bytes=512)
    assert [mod.file_identity(p) for p in (a, b)] == before
    assert not out.exists() or out.stat().st_size <= 512
